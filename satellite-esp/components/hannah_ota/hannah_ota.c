/**
 * hannah_ota — periodischer Firmware-Update-Check
 *
 * Ablauf:
 *   1. 60 Sekunden warten (WiFi + MQTT-Verbindung abwarten)
 *   2. GET {ota_url}/latest mit Bearer-Token
 *   3. Aktuelle Version mit Firmware-Version vergleichen
 *   4. Bei Unterschied: hannah/satellite/<device>/ota/pending publizieren
 *   5. Auf hannah/satellite/<device>/ota/ok warten → esp_https_ota + Neustart
 *   6. Alle HANNAH_OTA_POLL_INTERVAL_S Sekunden wiederholen
 */

#include "hannah_ota.h"
#include "hannah_net.h"
#include "hannah_config.h"
#include "hannah_audio.h"
#include "hannah_wakeword.h"
#include "hannah_asset.h"
#include "hannah_webserver.h"

#include <string.h>
#include <stdio.h>
#include <inttypes.h>
#include "esp_ota_ops.h"
#include "esp_log.h"
#include "esp_app_desc.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_crt_bundle.h"
#include "esp_spiffs.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "cJSON.h"

static const char *TAG = "ota";

#define OTA_HTTP_BUF_SIZE   512
#define OTA_STARTUP_DELAY_S 60

#define NVS_NAMESPACE   "ota"
#define NVS_KEY_REV     "revision"

static int32_t s_pending_revision = -1;  /* Revision die gerade heruntergeladen wird */

static int32_t nvs_read_revision(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return 0;
    int32_t rev = 0;
    nvs_get_i32(h, NVS_KEY_REV, &rev);
    nvs_close(h);
    return rev;
}

static void nvs_write_revision(int32_t rev)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_i32(h, NVS_KEY_REV, rev);
    nvs_commit(h);
    nvs_close(h);
}

/* ── Semver-Vergleich ─────────────────────────────────────────────────────── */

/* Parst "MAJOR.MINOR.PATCH" und optionalen git-describe-Commit-Offset
 * (z.B. "0.8.2-7-gabcdef" → patch=2, commits=7).
 * Gibt 0 zurück wenn das Parsen fehlschlägt. */
static int parse_semver(const char *ver, int *major, int *minor, int *patch, int *commits)
{
    *commits = 0;
    if (sscanf(ver, "%d.%d.%d", major, minor, patch) != 3) return 0;
    /* Suche nach "-N-g" Suffix */
    const char *p = strchr(ver, '-');
    if (p) sscanf(p + 1, "%d", commits);
    return 1;
}

/* Gibt 1 zurück wenn `server` neuer als `local` ist.
 * Im Dev-Channel wird bei gleichem Semver der git-describe-Commit-Offset verglichen. */
static int semver_gt(const char *server, const char *local, int dev_channel)
{
    int smaj = 0, smin = 0, spat = 0, scom = 0;
    int lmaj = 0, lmin = 0, lpat = 0, lcom = 0;

    if (!parse_semver(server, &smaj, &smin, &spat, &scom) ||
        !parse_semver(local,  &lmaj, &lmin, &lpat, &lcom)) {
        return strcmp(server, local) != 0;
    }

    if (smaj != lmaj) return smaj > lmaj;
    if (smin != lmin) return smin > lmin;
    if (spat != lpat) return spat > lpat;
    if (dev_channel)  return scom > lcom;
    return 0;
}


static char s_response_buf[OTA_HTTP_BUF_SIZE];
static int  s_response_len = 0;
static char s_pending_url[192];
static SemaphoreHandle_t s_ota_sem;

/* ── HTTP-Event-Handler ───────────────────────────────────────────────────── */

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    if (evt->event_id == HTTP_EVENT_ON_DATA) {
        int copy = evt->data_len;
        int remaining = (int)sizeof(s_response_buf) - 1 - s_response_len;
        if (copy > remaining) copy = remaining;
        if (copy > 0) {
            memcpy(s_response_buf + s_response_len, evt->data, copy);
            s_response_len += copy;
        }
    }
    return ESP_OK;
}

/* ── Update-Check ─────────────────────────────────────────────────────────── */

static void check_for_update(void)
{
    const hannah_config_t *cfg = hannah_config_get();

    if (cfg->ota_url[0] == '\0') {
        ESP_LOGD(TAG, "Kein OTA-URL konfiguriert — überspringe Check.");
        return;
    }

    const char *current = esp_app_get_description()->version;
    char url[256];
    if (cfg->ota_channel[0] != '\0')
        snprintf(url, sizeof(url), "%s/latest?channel=%s&current=%s&device=%s", cfg->ota_url, cfg->ota_channel, current, cfg->device_id);
    else
        snprintf(url, sizeof(url), "%s/latest?current=%s&device=%s", cfg->ota_url, current, cfg->device_id);

    char auth_header[192];
    snprintf(auth_header, sizeof(auth_header), "Bearer %s", cfg->ota_token);

    s_response_len = 0;
    memset(s_response_buf, 0, sizeof(s_response_buf));

    esp_http_client_config_t http_cfg = {
        .url               = url,
        .event_handler     = http_event_handler,
        .crt_bundle_attach = hannah_config_get()->tls_skip_verify ? NULL : esp_crt_bundle_attach,
        .timeout_ms        = 10000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
    if (!client) {
        ESP_LOGE(TAG, "http_client_init fehlgeschlagen.");
        return;
    }

    esp_http_client_set_header(client, "Authorization", auth_header);

    esp_err_t err = esp_http_client_perform(client);
    int status    = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "OTA-HTTP-Fehler: %s", esp_err_to_name(err));
        return;
    }
    if (status != 200) {
        ESP_LOGW(TAG, "OTA-Server antwortete mit HTTP %d.", status);
        return;
    }

    s_response_buf[s_response_len] = '\0';
    ESP_LOGD(TAG, "OTA-Antwort: %s", s_response_buf);

    cJSON *root = cJSON_Parse(s_response_buf);
    if (!root) {
        ESP_LOGW(TAG, "OTA: JSON-Parse fehlgeschlagen.");
        return;
    }

    const cJSON *jver = cJSON_GetObjectItemCaseSensitive(root, "version");
    const cJSON *jurl = cJSON_GetObjectItemCaseSensitive(root, "url");
    if (!cJSON_IsString(jver) || !cJSON_IsString(jurl)) {
        ESP_LOGW(TAG, "OTA: Kein 'version'/'url'-Feld in Antwort.");
        cJSON_Delete(root);
        return;
    }

    const char *latest = jver->valuestring;

    /* Revision aus Server-Antwort lesen (optional, default 0) */
    int32_t server_rev = 0;
    const cJSON *jrev = cJSON_GetObjectItemCaseSensitive(root, "revision");
    if (cJSON_IsNumber(jrev)) server_rev = (int32_t)jrev->valueint;

    int32_t local_rev = nvs_read_revision();

    ESP_LOGI(TAG, "Firmware: aktuell=%s rev=%"PRId32"  verfügbar=%s rev=%"PRId32,
             current, local_rev, latest, server_rev);

    /* Nach einem Rollback: die zuletzt ungültige Partition enthält dieselbe Version
     * wie der Server → statt ota/pending wird ota/failed publiziert damit Hannah
     * keinen erneuten Update-Befehl schickt. */
    const esp_partition_t *invalid = esp_ota_get_last_invalid_partition();
    if (invalid) {
        esp_app_desc_t invalid_desc;
        if (esp_ota_get_partition_description(invalid, &invalid_desc) == ESP_OK &&
            strcmp(invalid_desc.version, latest) == 0) {
            char topic[96];
            snprintf(topic, sizeof(topic), "hannah/satellite/%s/ota/failed", cfg->device_id);
            char payload[128];
            snprintf(payload, sizeof(payload),
                     "{\"version\":\"%s\",\"reason\":\"rollback\"}", latest);
            hannah_net_mqtt_publish(topic, payload, 1, 0);
            ESP_LOGW(TAG, "OTA-Rollback erkannt — %s wurde als ungültig markiert. ota/failed publiziert.", latest);
            cJSON_Delete(root);
            return;
        }
    }

    int dev_channel = (cfg->ota_channel[0] != '\0' && strcmp(cfg->ota_channel, "stable") != 0);
    int needs_update = semver_gt(latest, current, dev_channel) ||
                       (strcmp(latest, current) == 0 && server_rev > local_rev);

    if (needs_update) {
        /* Download-URL mit device-ID anreichern */
        const char *base_url = jurl->valuestring;
        int has_query = (strchr(base_url, '?') != NULL);
        snprintf(s_pending_url, sizeof(s_pending_url), "%s%sdevice=%s",
                 base_url, has_query ? "&" : "?", cfg->device_id);

        s_pending_revision = server_rev;

        char payload[128];
        snprintf(payload, sizeof(payload),
                 "{\"version\":\"%s\",\"revision\":%"PRId32",\"pending\":true}", latest, server_rev);
        char topic[96];
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/ota/pending", cfg->device_id);
        hannah_net_mqtt_publish(topic, payload, 1, 0);
        ESP_LOGI(TAG, "OTA-pending publiziert → %s: %s", topic, payload);
    } else {
        ESP_LOGI(TAG, "Firmware ist aktuell.");
    }

    cJSON_Delete(root);
}

/* ── OTA-Flash ────────────────────────────────────────────────────────────── */

static esp_err_t ota_http_init_cb(esp_http_client_handle_t client)
{
    const hannah_config_t *cfg = hannah_config_get();
    char auth[192];
    snprintf(auth, sizeof(auth), "Bearer %s", cfg->ota_token);
    return esp_http_client_set_header(client, "Authorization", auth);
}

static void ota_update_task(void *arg)
{
    while (1) {
        xSemaphoreTake(s_ota_sem, portMAX_DELAY);

        if (s_pending_url[0] == '\0') {
            ESP_LOGW(TAG, "OTA-OK empfangen, aber keine URL bekannt.");
            continue;
        }

        ESP_LOGI(TAG, "Starte OTA von %s", s_pending_url);
        hannah_audio_pause_wakeword();
        hannah_wakeword_deinit();  /* PSRAM für mbedTLS-Download freigeben (#171) */
        esp_vfs_spiffs_unregister("spiffs");
        /* Business-Logik komplett abschalten (#193) — Satellit ist während OTA
         * ohnehin schon "taub" (Wakeword pausiert), vollständig offline für die
         * paar Sekunden ist kein zusätzlicher UX-Verlust. Gibt echtes internes
         * DRAM frei (I2S-DMA-Puffer), nicht nur PSRAM wie der Wakeword-Schritt
         * oben — der größte bislang ungenutzte Headroom-Hebel während OTA. */
        hannah_webserver_stop();
        hannah_audio_deinit_for_ota();
        ESP_LOGI(TAG, "Free heap vor OTA: %lu", esp_get_free_heap_size());

        esp_http_client_config_t http_cfg = {
            .url               = s_pending_url,
            .crt_bundle_attach = hannah_config_get()->tls_skip_verify ? NULL : esp_crt_bundle_attach,
            .timeout_ms        = 60000,
            .keep_alive_enable = true,
            .buffer_size       = 4096,
        };
        esp_https_ota_config_t ota_cfg = {
            .http_config          = &http_cfg,
            .http_client_init_cb  = ota_http_init_cb,
            .bulk_flash_erase     = true,
        };

        esp_err_t err = esp_https_ota(&ota_cfg);
        if (err == ESP_OK) {
            if (s_pending_revision >= 0) {
                nvs_write_revision(s_pending_revision);
                ESP_LOGI(TAG, "OTA erfolgreich — Revision %"PRId32" gespeichert — Neustart.", s_pending_revision);
            } else {
                ESP_LOGI(TAG, "OTA erfolgreich — Neustart.");
            }
            hannah_net_mark_restart_source("ota");
            esp_restart();
        } else {
            ESP_LOGE(TAG, "OTA fehlgeschlagen: %s", esp_err_to_name(err));
            hannah_asset_remount();
            hannah_wakeword_reinit();
            hannah_audio_reinit_after_ota_failure();
            hannah_audio_resume_wakeword();
            hannah_webserver_start();
        }
    }
}

/* ── Callbacks ────────────────────────────────────────────────────────────── */

static void on_ota_ok(void)
{
    ESP_LOGI(TAG, "OTA-Freigabe (ota/ok) empfangen — starte Update.");
    xSemaphoreGive(s_ota_sem);
}

/* ── Task ─────────────────────────────────────────────────────────────────── */

static void ota_poll_task(void *arg)
{
    ESP_LOGI(TAG, "Warte %ds auf WiFi/MQTT...", OTA_STARTUP_DELAY_S);
    vTaskDelay(pdMS_TO_TICKS(OTA_STARTUP_DELAY_S * 1000));

    {
        const hannah_config_t *cfg = hannah_config_get();
        const char *current = esp_app_get_description()->version;
        char fw_topic[96];
        char fw_payload[160];
        snprintf(fw_topic,   sizeof(fw_topic),   "hannah/satellite/%s/firmware", cfg->device_id);
        snprintf(fw_payload, sizeof(fw_payload),
                 "{\"version\":\"%s\",\"restart_reason\":\"%s\",\"restart_count\":%lu}",
                 current, hannah_net_get_restart_reason(), (unsigned long)hannah_net_get_restart_count());
        hannah_net_mqtt_publish(fw_topic, fw_payload, 1, 1);
        ESP_LOGI(TAG, "Firmware-Version publiziert: %s = %s", fw_topic, fw_payload);
    }

    hannah_net_wait_sntp(10000);

    while (1) {
        check_for_update();
        vTaskDelay(pdMS_TO_TICKS((uint32_t)CONFIG_HANNAH_OTA_POLL_INTERVAL_S * 1000));
    }
}

/* ── Öffentliche API ─────────────────────────────────────────────────────── */

void hannah_ota_init(void)
{
    s_ota_sem = xSemaphoreCreateBinary();
    hannah_net_set_ota_ok_callback(on_ota_ok);
    xTaskCreate(ota_poll_task,   "ota_poll",   12288, NULL, 3, NULL);
    xTaskCreate(ota_update_task, "ota_update", 8192,  NULL, 5, NULL);
    ESP_LOGI(TAG, "OTA-Check aktiv (Erstprüfung in %ds, Intervall %ds).",
             OTA_STARTUP_DELAY_S, CONFIG_HANNAH_OTA_POLL_INTERVAL_S);
}

#include "hannah_asset.h"
#include "hannah_audio.h"
#include "hannah_config.h"
#include "hannah_net.h"

#include <stdio.h>
#include <string.h>
#include <stdint.h>

#include "esp_log.h"
#include "esp_spiffs.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "cJSON.h"
#include "psa/crypto.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG        = "hannah_asset";
#define ASSET_MOUNT   "/assets"
#define ASSET_NVS_NS  "hna"   /* max 15 chars für nvs namespace */

static hannah_asset_play_result_cb_t s_play_result_cb = NULL;


/* ── WAV-Chunk-Scanner ───────────────────────────────────────────────────── */

static bool wav_find_data(FILE *f, uint32_t *sr_out, uint16_t *ch_out, uint32_t *data_size_out)
{
    char     id[4];
    uint32_t chunk_size;
    bool     got_fmt = false;

    /* RIFF + WAVE */
    if (fread(id, 1, 4, f) != 4 || strncmp(id, "RIFF", 4) != 0) return false;
    fread(&chunk_size, 4, 1, f);                           /* file size − 8, ignoriert */
    if (fread(id, 1, 4, f) != 4 || strncmp(id, "WAVE", 4) != 0) return false;

    while (fread(id, 1, 4, f) == 4 && fread(&chunk_size, 4, 1, f) == 1) {
        if (strncmp(id, "fmt ", 4) == 0) {
            uint16_t audio_fmt, channels;
            uint32_t sample_rate, byte_rate;
            uint16_t block_align, bits;
            fread(&audio_fmt,   2, 1, f);
            fread(&channels,    2, 1, f);
            fread(&sample_rate, 4, 1, f);
            fread(&byte_rate,   4, 1, f);
            fread(&block_align, 2, 1, f);
            fread(&bits,        2, 1, f);
            *sr_out = sample_rate;
            *ch_out = channels;
            got_fmt = true;
            if (chunk_size > 16) fseek(f, (long)(chunk_size - 16), SEEK_CUR);
        } else if (strncmp(id, "data", 4) == 0) {
            *data_size_out = chunk_size;
            return got_fmt;
        } else {
            /* Unbekannten Chunk überspringen (RIFF-Alignment: gerade Byte-Anzahl) */
            fseek(f, (long)((chunk_size + 1) & ~1u), SEEK_CUR);
        }
    }
    return false;
}

/* ── SHA256 über Datei ───────────────────────────────────────────────────── */

/* Berechnet den SHA256 der Datei unter `path` und schreibt ihn als
 * 64-Zeichen-Hex-String (+ NUL) nach `out_hex` (>= 65 Bytes).
 * false bei Datei-/Lesefehler. */
static bool file_sha256_hex(const char *path, char *out_hex)
{
    FILE *f = fopen(path, "rb");
    if (!f) return false;

    psa_crypto_init();   /* idempotent — TLS-Stack hat PSA i.d.R. schon initialisiert */
    psa_hash_operation_t op = PSA_HASH_OPERATION_INIT;
    if (psa_hash_setup(&op, PSA_ALG_SHA_256) != PSA_SUCCESS) {
        fclose(f);
        return false;
    }

    uint8_t buf[1024];
    size_t  n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
        psa_hash_update(&op, buf, n);
    }
    fclose(f);

    uint8_t digest[32];
    size_t  digest_len = 0;
    if (psa_hash_finish(&op, digest, sizeof(digest), &digest_len) != PSA_SUCCESS) {
        psa_hash_abort(&op);
        return false;
    }

    for (int i = 0; i < 32; i++) {
        snprintf(out_hex + i * 2, 3, "%02x", digest[i]);
    }
    out_hex[64] = '\0';
    return true;
}

/* ── HTTP-Hilfsfunktionen ────────────────────────────────────────────────── */

static void set_auth_header(esp_http_client_handle_t client)
{
    const hannah_config_t *cfg = hannah_config_get();
    char auth[280];
    snprintf(auth, sizeof(auth), "Bearer %s", cfg->asset_token);
    esp_http_client_set_header(client, "Authorization", auth);
}

/* Manifest als allokierten String zurückgeben (Aufrufer muss free() rufen).
 * NULL bei Fehler. */
static char *fetch_manifest(void)
{
    const hannah_config_t *hcfg = hannah_config_get();
    char url[256];
    snprintf(url, sizeof(url), "%s/manifest?namespace=satellite", hcfg->asset_url);
    ESP_LOGI(TAG, "Manifest abrufen: %s", url);

    esp_http_client_config_t cfg = {
        .url        = url,
        .timeout_ms = 10000,
        .crt_bundle_attach = hannah_config_get()->tls_skip_verify ? NULL : esp_crt_bundle_attach,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    set_auth_header(client);

    char *body = NULL;

    if (esp_http_client_open(client, 0) != ESP_OK) goto done;
    esp_http_client_fetch_headers(client);
    if (esp_http_client_get_status_code(client) != 200) {
        ESP_LOGE(TAG, "Manifest: HTTP %d", esp_http_client_get_status_code(client));
        goto done;
    }

    body = calloc(1, 4096);
    if (!body) goto done;

    int total = 0, read;
    while ((read = esp_http_client_read(client, body + total, 4094 - total)) > 0) {
        total += read;
        if (total >= 4094) break;
    }
    body[total] = '\0';
    if (total == 0) { free(body); body = NULL; }

done:
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return body;
}

static bool download_asset(const char *asset_id)
{
    char url[256];
    const hannah_config_t *hcfg = hannah_config_get();
    snprintf(url, sizeof(url), "%s/assets/%s", hcfg->asset_url, asset_id);

    char path[72];
    snprintf(path, sizeof(path), ASSET_MOUNT "/%s", asset_id);

    esp_http_client_config_t cfg = {
        .url         = url,
        .timeout_ms  = 60000,
        .buffer_size = 4096,
        .crt_bundle_attach = hannah_config_get()->tls_skip_verify ? NULL : esp_crt_bundle_attach,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    set_auth_header(client);

    bool ok = false;

    if (esp_http_client_open(client, 0) != ESP_OK) {
        ESP_LOGE(TAG, "HTTP open fehlgeschlagen: %s", url);
        goto done;
    }
    esp_http_client_fetch_headers(client);
    if (esp_http_client_get_status_code(client) != 200) {
        ESP_LOGE(TAG, "Asset %s: HTTP %d", asset_id,
                 esp_http_client_get_status_code(client));
        goto done;
    }

    FILE *f = fopen(path, "wb");
    if (!f) { ESP_LOGE(TAG, "fopen %s fehlgeschlagen", path); goto done; }

    char  buf[4096];
    int   read_len, total = 0;
    while ((read_len = esp_http_client_read(client, buf, sizeof(buf))) > 0) {
        fwrite(buf, 1, read_len, f);
        total += read_len;
    }
    fclose(f);
    ESP_LOGI(TAG, "Asset %s: %d bytes → %s", asset_id, total, path);
    ok = (total > 0);

done:
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return ok;
}

/* ── NVS-Hilfsfunktionen (sha256-Cache) ──────────────────────────────────── */

/* NVS-Keys sind max. 15 Zeichen. Wir nehmen die ersten 11 Zeichen der asset_id + "_s". */
static void make_nvs_key(const char *asset_id, char *key_out)
{
    snprintf(key_out, 16, "%.11s_s", asset_id);
}

static bool sha256_matches(const char *asset_id, const char *sha256)
{
    char key[16];
    make_nvs_key(asset_id, key);

    nvs_handle_t h;
    if (nvs_open(ASSET_NVS_NS, NVS_READONLY, &h) != ESP_OK) return false;

    char cached[72] = {0};
    size_t sz = sizeof(cached);
    bool ok = (nvs_get_str(h, key, cached, &sz) == ESP_OK) &&
              (strcmp(cached, sha256) == 0);
    nvs_close(h);
    return ok;
}

static void store_sha256(const char *asset_id, const char *sha256)
{
    char key[16];
    make_nvs_key(asset_id, key);

    nvs_handle_t h;
    if (nvs_open(ASSET_NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_str(h, key, sha256);
    nvs_commit(h);
    nvs_close(h);
}

/* ── Update-Task ─────────────────────────────────────────────────────────── */

static void update_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(50000));
    hannah_net_wait_sntp(10000);

    while (1) {
        char *body = NULL;
        for (int attempt = 1; attempt <= 3; attempt++) {
            ESP_LOGI(TAG, "Manifest-Fetch Versuch %d/3 (free heap: %lu)",
                     attempt, esp_get_free_heap_size());
            body = fetch_manifest();
            if (body) break;
            ESP_LOGW(TAG, "Manifest nicht abrufbar (Versuch %d/3).", attempt);
            if (attempt < 3) vTaskDelay(pdMS_TO_TICKS(30000));
        }
        if (!body) {
            ESP_LOGW(TAG, "Manifest nach 3 Versuchen nicht abrufbar — Retry in 30 min.");
            vTaskDelay(pdMS_TO_TICKS(1800000));
            continue;
        }

        cJSON *root = cJSON_Parse(body);
        free(body);
        if (!root) {
            ESP_LOGE(TAG, "Manifest-JSON ungültig.");
            vTaskDelete(NULL);
            return;
        }

        cJSON *assets = cJSON_GetObjectItemCaseSensitive(root, "assets");
        if (cJSON_IsObject(assets)) {
            cJSON *item;
            cJSON_ArrayForEach(item, assets) {
                const char *id    = item->string;
                const cJSON *jsha = cJSON_GetObjectItemCaseSensitive(item, "sha256");
                if (!cJSON_IsString(jsha)) continue;

                if (sha256_matches(id, jsha->valuestring)) {
                    ESP_LOGI(TAG, "Asset %s aktuell.", id);
                    continue;
                }

                ESP_LOGI(TAG, "Asset %s herunterladen...", id);
                if (!download_asset(id)) continue;

                /* SHA256 der heruntergeladenen Datei gegen das Manifest prüfen —
                 * verhindert, dass abgebrochene Teildownloads als gültig gecacht
                 * werden. */
                char path[72];
                snprintf(path, sizeof(path), ASSET_MOUNT "/%s", id);
                char actual[65];
                if (file_sha256_hex(path, actual) &&
                    strcmp(actual, jsha->valuestring) == 0) {
                    store_sha256(id, jsha->valuestring);
                    ESP_LOGI(TAG, "Asset %s verifiziert (sha256 ok).", id);
                } else {
                    ESP_LOGW(TAG, "Asset %s: sha256-Mismatch — verwerfe Datei.", id);
                    remove(path);
                }
            }
        }

        cJSON_Delete(root);
        ESP_LOGI(TAG, "Asset-Update abgeschlossen.");
        vTaskDelete(NULL);
        return;
    }
}

/* ── Öffentliche API ─────────────────────────────────────────────────────── */

void hannah_asset_init(void)
{
    esp_vfs_spiffs_conf_t conf = {
        .base_path              = ASSET_MOUNT,
        .partition_label        = "spiffs",
        .max_files              = 8,
        .format_if_mount_failed = true,
    };
    esp_err_t ret = esp_vfs_spiffs_register(&conf);
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "SPIFFS mount fehlgeschlagen: %s", esp_err_to_name(ret));
        return;
    }
    size_t total = 0, used = 0;
    esp_spiffs_info("spiffs", &total, &used);
    ESP_LOGI(TAG, "SPIFFS: %u/%u bytes", used, total);

    xTaskCreate(update_task, "asset_upd", 16384, NULL, 3, NULL);
}

bool hannah_asset_play(const char *asset_id)
{
    char path[72];
    snprintf(path, sizeof(path), ASSET_MOUNT "/%s", asset_id);

    FILE *f = fopen(path, "rb");
    if (!f) {
        ESP_LOGW(TAG, "Asset '%s' nicht im Cache: %s", asset_id, path);
        return false;
    }

    uint32_t sample_rate = 16000;
    uint16_t channels    = 1;
    uint32_t data_size   = 0;

    if (!wav_find_data(f, &sample_rate, &channels, &data_size)) {
        ESP_LOGE(TAG, "WAV-Header ungültig: %s", path);
        fclose(f);
        return false;
    }

    ESP_LOGI(TAG, "Asset %s: %"PRIu32"Hz %uch, %"PRIu32" bytes PCM",
             asset_id, sample_rate, channels, data_size);

    uint8_t buf[2048];
    size_t  rlen;
    while ((rlen = fread(buf, 1, sizeof(buf), f)) > 0) {
        hannah_audio_play(buf, rlen, (int)sample_rate);
    }
    hannah_audio_play_end();
    fclose(f);
    return true;
}

static void play_task(void *arg)
{
    char *asset_id = (char *)arg;
    bool ok = hannah_asset_play(asset_id);
    if (s_play_result_cb) s_play_result_cb(asset_id, ok);
    free(asset_id);
    vTaskDelete(NULL);
}

void hannah_asset_play_async(const char *asset_id)
{
    char *id = strdup(asset_id);
    if (!id) return;
    if (xTaskCreate(play_task, "asset_play", 8192, id, 5, NULL) != pdPASS) {
        free(id);
        ESP_LOGE(TAG, "Play-Task konnte nicht gestartet werden.");
    }
}

void hannah_asset_set_play_result_callback(hannah_asset_play_result_cb_t cb)
{
    s_play_result_cb = cb;
}

bool hannah_asset_read_to_psram(const char *asset_id, uint8_t **out_buf, size_t *out_size)
{
    char path[72];
    snprintf(path, sizeof(path), ASSET_MOUNT "/%s", asset_id);

    FILE *f = fopen(path, "rb");
    if (!f) return false;

    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (size <= 0) {
        fclose(f);
        return false;
    }

    uint8_t *buf = (uint8_t *)heap_caps_malloc((size_t)size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buf) {
        ESP_LOGE(TAG, "PSRAM-Allokation für Asset '%s' fehlgeschlagen (%ld Bytes)", asset_id, size);
        fclose(f);
        return false;
    }

    size_t read_len = fread(buf, 1, (size_t)size, f);
    fclose(f);
    if (read_len != (size_t)size) {
        heap_caps_free(buf);
        return false;
    }

    *out_buf  = buf;
    *out_size = (size_t)size;
    return true;
}

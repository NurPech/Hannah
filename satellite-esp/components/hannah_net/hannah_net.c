/**
 * hannah_net — WiFi (STA mit AP-Fallback), MQTT-Discovery, UDP-Audio-Stream
 *
 * Ablauf:
 *   1. Credentials aus NVS laden (hannah_config)
 *   2a. Credentials vorhanden → WiFi STA
 *   2b. Keine Credentials oder max. Versuche erreicht → AP-Modus "Hannah-Setup-XXXX"
 *   3. STA: MQTT-Client → "hannah/server" → UDP-Socket + Register
 *   4. UDP-Receive-Task: TTS + Status empfangen
 *   5. Heartbeat-Task: alle N Sekunden an Proxy senden
 *
 * UDP-Protokoll (1-Byte Type-Prefix):
 *   0x01 + JSON  = Control  (beide Richtungen)
 *   0x02 + PCM   = Audio    (Satellit → Proxy)
 *   0x03 + PCM   = TTS      (Proxy → Satellit)
 */

#include "hannah_net.h"
#include "hannah_config.h"

#include <string.h>
#include <stdio.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_sntp.h"
#include "esp_system.h"
#include "esp_ota_ops.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "esp_task_wdt.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/timers.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "mqtt_client.h"
#include "mbedtls/platform.h"
#include "cJSON.h"

static const char *TAG = "hannah_net";

#define TYPE_CONTROL    0x01
#define TYPE_AUDIO      0x02
#define TYPE_TTS        0x03
#define UDP_RX_BUF_SIZE 65536
#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_FAIL_BIT       BIT1
#define NET_SNTP_SYNCED_BIT BIT0
#define AP_RECOVERY_INTERVAL_MS (10 * 60 * 1000)  /* Retry-Intervall fürs Original-Netz im AP-Setup-Modus */

/* ── Zustand ─────────────────────────────────────────────────────────────── */

static volatile bool           s_muted      = false;
static hannah_net_hw_mute_cb_t s_hw_mute_cb = NULL;
static int                     s_udp_sock   = -1;
static struct sockaddr_in      s_proxy_addr;
static bool                    s_proxy_ready = false;
static int                     s_wifi_retry  = 0;
static char                    s_proxy_host[64] = {0};
static int                     s_proxy_port     = 0;
static volatile bool           s_ap_mode        = false;
static volatile bool           s_ap_pending_exit = false;  /* Recovery erfolgreich, wartet auf letzten AP-Client */
static bool                    s_sntp_started   = false;
static volatile int64_t        s_last_net_activity_ms = 0;  /* Netzwerk-Watchdog, siehe net_activity_mark() */

/* Neustart-Diagnose (#165) — siehe hannah_net.h für die öffentliche API. */
#define DIAG_NVS_NAMESPACE  "diag"
#define DIAG_NVS_KEY_COUNT  "restart_count"
#define DIAG_NVS_KEY_SRC    "restart_src"
static uint32_t  s_restart_count  = 0;
static char      s_restart_reason[16] = "unbekannt";

static EventGroupHandle_t        s_wifi_event_group;
static EventGroupHandle_t        s_net_events  = NULL;
static TimerHandle_t             s_sntp_retry  = NULL;
static TimerHandle_t             s_ap_recovery_retry = NULL;
static esp_mqtt_client_handle_t  s_mqtt_client = NULL;
static esp_netif_t              *s_sta_netif   = NULL;
static esp_netif_t              *s_ap_netif    = NULL;

static hannah_net_status_cb_t   s_status_cb   = NULL;
static hannah_net_tts_cb_t      s_tts_cb      = NULL;
static hannah_net_tts_end_cb_t  s_tts_end_cb  = NULL;
static hannah_net_playback_cb_t s_playback_cb = NULL;
static hannah_net_ota_ok_cb_t          s_ota_ok_cb          = NULL;
static hannah_net_registered_cb_t      s_registered_cb      = NULL;
static hannah_net_ble_watchlist_cb_t   s_ble_watchlist_cb   = NULL;
static char s_ble_watchlist_cache[512];
static int  s_ble_watchlist_cache_len = 0;
static hannah_net_asset_relevant_cb_t  s_asset_relevant_cb  = NULL;
static char s_asset_relevant_cache[512];
static int  s_asset_relevant_cache_len = 0;
static hannah_net_volume_cb_t          s_volume_cb          = NULL;
static hannah_net_sampling_cb_t        s_sampling_cb        = NULL;
static hannah_net_virtual_ptt_cb_t        s_virtual_ptt_cb        = NULL;
static hannah_net_play_asset_cb_t         s_play_asset_cb         = NULL;
static hannah_net_start_listening_cb_t    s_start_listening_cb    = NULL;

/* ── Hilfsfunktionen ─────────────────────────────────────────────────────── */

/* Markiert ein bestätigtes Netzwerk-Lebenszeichen (IP bezogen, MQTT verbunden
 * oder Daten empfangen) für den Netzwerk-Watchdog in heartbeat_task(). Bewusst
 * nicht an WIFI_EVENT_STA_DISCONNECTED gekoppelt — im "Zombie"-WLAN-Fall
 * feuert genau dieses Event nicht mehr, daher braucht es ein aktives Signal. */
static void net_activity_mark(void)
{
    s_last_net_activity_ms = esp_timer_get_time() / 1000;
}

static void send_control(const char *json_str)
{
    if (s_udp_sock < 0 || !s_proxy_ready) return;
    size_t json_len = strlen(json_str);
    size_t pkt_len  = 1 + json_len;
    uint8_t *pkt    = malloc(pkt_len);
    if (!pkt) return;
    pkt[0] = TYPE_CONTROL;
    memcpy(pkt + 1, json_str, json_len);
    sendto(s_udp_sock, pkt, pkt_len, 0,
           (struct sockaddr *)&s_proxy_addr, sizeof(s_proxy_addr));
    free(pkt);
}

static void send_register(void)
{
    const hannah_config_t *cfg = hannah_config_get();

    char msg[400];
    if (cfg->seed[0]) {
        snprintf(msg, sizeof(msg),
                 "{\"type\":\"register\",\"device\":\"%s\","
                 "\"listen_port\":%d,\"seed\":\"%s\"}",
                 cfg->device_id, CONFIG_HANNAH_UDP_LISTEN_PORT,
                 cfg->seed);
    } else {
        snprintf(msg, sizeof(msg),
                 "{\"type\":\"register\",\"device\":\"%s\","
                 "\"listen_port\":%d}",
                 cfg->device_id, CONFIG_HANNAH_UDP_LISTEN_PORT);
    }
    send_control(msg);
    ESP_LOGI(TAG, "Register: device=%s port=%d%s",
             cfg->device_id, CONFIG_HANNAH_UDP_LISTEN_PORT,
             cfg->seed[0] ? " (mit Seed)" : "");
}

/* ── UDP ─────────────────────────────────────────────────────────────────── */

static void udp_connect(const char *host, int port)
{
    if (s_udp_sock >= 0) {
        close(s_udp_sock);
        s_udp_sock = -1;
        s_proxy_ready = false;
    }

    memset(&s_proxy_addr, 0, sizeof(s_proxy_addr));
    s_proxy_addr.sin_family = AF_INET;
    s_proxy_addr.sin_port   = htons(port);
    if (inet_aton(host, &s_proxy_addr.sin_addr) == 0) {
        ESP_LOGE(TAG, "Ungültige Proxy-IP: %s", host);
        return;
    }

    s_udp_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s_udp_sock < 0) {
        ESP_LOGE(TAG, "socket() fehlgeschlagen: errno=%d", errno);
        return;
    }

    struct sockaddr_in local = {
        .sin_family      = AF_INET,
        .sin_port        = htons(CONFIG_HANNAH_UDP_LISTEN_PORT),
        .sin_addr.s_addr = INADDR_ANY,
    };
    if (bind(s_udp_sock, (struct sockaddr *)&local, sizeof(local)) < 0) {
        ESP_LOGE(TAG, "bind(%d) fehlgeschlagen: errno=%d",
                 CONFIG_HANNAH_UDP_LISTEN_PORT, errno);
        close(s_udp_sock);
        s_udp_sock = -1;
        return;
    }

    struct timeval tv = {
        .tv_sec  = 0,
        .tv_usec = CONFIG_HANNAH_UDP_TIMEOUT_MS * 1000,
    };
    setsockopt(s_udp_sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    s_proxy_ready = true;
    ESP_LOGI(TAG, "UDP-Socket → Proxy %s:%d (listen :%d)",
             host, port, CONFIG_HANNAH_UDP_LISTEN_PORT);
    send_register();
    if (s_registered_cb) s_registered_cb();
}

static void udp_receive_task(void *arg)
{
    uint8_t *buf = malloc(UDP_RX_BUF_SIZE);
    if (!buf) { vTaskDelete(NULL); return; }

    while (1) {
        if (s_udp_sock < 0) { vTaskDelay(pdMS_TO_TICKS(200)); continue; }

        int len = recv(s_udp_sock, buf, UDP_RX_BUF_SIZE, 0);
        if (len < 2) continue;

        uint8_t  type    = buf[0];
        uint8_t *payload = buf + 1;
        size_t   plen    = len - 1;

        if (type == TYPE_TTS) {
            if (s_tts_cb) s_tts_cb(payload, plen);

        } else if (type == TYPE_CONTROL) {
            buf[len] = '\0';
            cJSON *root = cJSON_ParseWithLength((char *)payload, plen);
            if (!root) continue;

            const cJSON *jtype = cJSON_GetObjectItemCaseSensitive(root, "type");
            if (!cJSON_IsString(jtype)) { cJSON_Delete(root); continue; }

            if (strcmp(jtype->valuestring, "status") == 0) {
                const cJSON *jstate = cJSON_GetObjectItemCaseSensitive(root, "state");
                if (cJSON_IsString(jstate) && s_status_cb)
                    s_status_cb(jstate->valuestring);

            } else if (strcmp(jtype->valuestring, "tts_end") == 0) {
                int sr = 16000;
                const cJSON *jsr = cJSON_GetObjectItemCaseSensitive(root, "sample_rate");
                if (cJSON_IsNumber(jsr)) sr = (int)jsr->valuedouble;
                if (s_tts_end_cb) s_tts_end_cb(sr);

            } else if (strcmp(jtype->valuestring, "stop")   == 0 ||
                       strcmp(jtype->valuestring, "pause")  == 0 ||
                       strcmp(jtype->valuestring, "resume") == 0) {
                if (s_playback_cb) s_playback_cb(jtype->valuestring);

            } else if (strcmp(jtype->valuestring, "start_listening") == 0) {
                if (s_start_listening_cb) s_start_listening_cb();

            } else if (strcmp(jtype->valuestring, "paired") == 0) {
                ESP_LOGI(TAG, "Pairing bestätigt.");
                hannah_config_clear_seed();

            } else if (strcmp(jtype->valuestring, "reregister") == 0) {
                ESP_LOGW(TAG, "Re-Registrierung angefordert.");
                send_register();

            } else if (strcmp(jtype->valuestring, "heartbeat_ack") == 0) {
                net_activity_mark();
            }

            cJSON_Delete(root);
        }
    }
    free(buf);
    vTaskDelete(NULL);
}

/* ── Heartbeat ───────────────────────────────────────────────────────────── */

/* Hängt heartbeat_task an den Task-Watchdog (TWDT) — deckt einen Hang der
 * Task selbst ab, nicht nur "WLAN tot" (siehe Netzwerk-Watchdog unten). Die
 * Idle-Tasks sind bereits per CONFIG_ESP_TASK_WDT_INIT mit
 * CONFIG_ESP_TASK_WDT_TIMEOUT_S (Default 5s) subscribed; da heartbeat_task
 * nur einmal pro CONFIG_HANNAH_HEARTBEAT_INTERVAL_S (bis zu 300s) füttert,
 * muss das TWDT-Timeout auf einen Wert über diesem Intervall angehoben
 * werden — das gilt dann für alle subscribten Tasks (TWDT hat nur ein
 * gemeinsames Timeout), die Idle-Task-Hang-Erkennung wird also entsprechend
 * länger, statt bei den Default-5s zu bleiben. */
static void heartbeat_task_wdt_setup(void)
{
    uint32_t idle_mask = 0;
#if CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0
    idle_mask |= (1 << 0);
#endif
#if CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1
    idle_mask |= (1 << 1);
#endif
    esp_task_wdt_config_t wdt_cfg = {
        .timeout_ms     = (CONFIG_HANNAH_HEARTBEAT_INTERVAL_S + 5) * 1000,
        .idle_core_mask = idle_mask,
        .trigger_panic  = true,
    };
    esp_task_wdt_reconfigure(&wdt_cfg);
    esp_task_wdt_add(NULL);
}

static void heartbeat_task(void *arg)
{
    heartbeat_task_wdt_setup();

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(CONFIG_HANNAH_HEARTBEAT_INTERVAL_S * 1000));
        esp_task_wdt_reset();

        if (s_proxy_ready) {
            char msg[96];
            snprintf(msg, sizeof(msg),
                     "{\"type\":\"heartbeat\",\"device\":\"%s\"}",
                     hannah_config_get()->device_id);
            send_control(msg);
            ESP_LOGD(TAG, "Heartbeat.");
        }

        /* Periodisches Heap-Logging — Diagnose für Ressourcenerschöpfungs-
         * Verdachtsfälle (#150): ein Satellit kann UDP-Heartbeats weiter
         * verarbeiten (bereits offener Socket, keine neue Allokation nötig),
         * während OTA (HTTPS/TLS) oder der Webserver (neue eingehende
         * Verbindung) an einem fragmentierten/erschöpften Heap scheitern.
         * Landet im Ringpuffer und damit in /log/last. intern=... zeigt
         * zusätzlich gezielt das MALLOC_CAP_INTERNAL-Freiheap (#184) — der
         * kombinierte Wert (frei=) ist seit #191 von PSRAM dominiert und
         * damit für die Kalibrierung des Heap-Watchdog-Schwellwerts unten
         * nutzlos, da er selbst bei fast leerem internen DRAM kaum sinkt. */
        ESP_LOGI(TAG, "Heap: frei=%lu min_je=%lu intern=%lu",
                 (unsigned long)esp_get_free_heap_size(),
                 (unsigned long)esp_get_minimum_free_heap_size(),
                 (unsigned long)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));

        /* GOT_IP/MQTT_CONNECTED feuern nur einmal pro Verbindung, MQTT_DATA nur
         * bei eingehenden Befehlen — im ruhigen Idle-Betrieb kommt sonst über
         * lange Strecken kein einziges Lebenszeichen mehr rein. Der obige
         * Heartbeat schließt genau diese Lücke: Proxy/Core bestätigen jeden
         * Heartbeat mit "heartbeat_ack" (siehe udp_task-Handler oben), was
         * net_activity_mark() auslöst — eine echte, serverbestätigte
         * Zustellung statt nur eines lokalen Treiberzustands. (Vorher wurde
         * hier zusätzlich esp_wifi_sta_get_ap_info() geprüft — verworfen,
         * weil das nur den WiFi-Treiberzustand abfragt und im "Zombie"-Fall
         * genau dann fälschlich "verbunden" meldet, wenn es nicht stimmt.) */

        /* Netzwerk-Watchdog: greift aktiv ein, falls seit dem letzten
         * bestätigten Lebenszeichen zu viel Zeit vergangen ist — deckt den
         * "Zombie"-WLAN-Fall ab, bei dem WIFI_EVENT_STA_DISCONNECTED nie
         * feuert und die reaktive Reconnect-Logik in on_wifi_event() daher
         * untätig bleibt. Im AP-Setup-Modus bewusst inaktiv (kein reguläres
         * STA-Netz erwartet). */
        if (!s_ap_mode && s_last_net_activity_ms > 0) {
            int64_t elapsed_ms = esp_timer_get_time() / 1000 - s_last_net_activity_ms;
            if (elapsed_ms > (int64_t)CONFIG_HANNAH_NET_WATCHDOG_TIMEOUT_S * 1000) {
                ESP_LOGE(TAG, "Netzwerk-Watchdog: %lld s ohne Lebenszeichen — Neustart.",
                         elapsed_ms / 1000);
                /* Erst vom TWDT abmelden — sonst kann der ordentliche Shutdown
                 * (WiFi/MQTT stoppen, Log-Flush in hannah_webserver via
                 * esp_register_shutdown_handler()) selbst den Task-Watchdog
                 * auslösen und landet als harter Panic-Reset statt eines
                 * sauberen esp_restart(), der den Shutdown-Handler-Chain
                 * überspringt (persist_log_to_flash() liefe dann nie durch). */
                esp_task_wdt_delete(NULL);
                esp_restart();
            }
        }

        /* Heap-Watchdog (#184, Follow-up zu #150/#161): der Netzwerk-Watchdog
         * oben deckt nur Netzwerk-Liveness ab — genau die blieb in allen drei
         * bisherigen Vorfällen intakt, während ressourcenlastige Pfade
         * (Webserver, TLS/OTA, zuletzt auch MQTT-Befehlsverarbeitung, siehe
         * #184) bereits an einem erschöpften internen Heap scheiterten.
         * Reagiert direkt auf das interne (MALLOC_CAP_INTERNAL) Freiheap,
         * bevor der Satellit komplett unerreichbar wird — über denselben
         * geordneten Neustart-Pfad wie oben, damit anders als beim
         * EN-Pin-Hard-Reset in #184 der Log-Trail (persist_log_to_flash())
         * diesmal erhalten bleibt. Reine Diagnose-/Rejuvenation-Maßnahme,
         * kein Fix der eigentlichen Fragmentierungsursache. */
        if (CONFIG_HANNAH_HEAP_WATCHDOG_THRESHOLD_BYTES > 0) {
            size_t free_internal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
            if (free_internal < CONFIG_HANNAH_HEAP_WATCHDOG_THRESHOLD_BYTES) {
                ESP_LOGE(TAG, "Heap-Watchdog: %lu Byte internes Freiheap < Schwellwert %d — Neustart.",
                         (unsigned long)free_internal, CONFIG_HANNAH_HEAP_WATCHDOG_THRESHOLD_BYTES);
                hannah_net_mark_restart_source("heap");
                esp_task_wdt_delete(NULL);
                esp_restart();
            }
        }
    }
}

/* ── MQTT ────────────────────────────────────────────────────────────────── */

static void on_mqtt_event(void *handler_arg, esp_event_base_t base,
                          int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)event_data;

    switch (event_id) {
    case MQTT_EVENT_CONNECTED: {
        ESP_LOGI(TAG, "MQTT verbunden.");
        net_activity_mark();
        esp_ota_mark_app_valid_cancel_rollback();
        esp_mqtt_client_subscribe(s_mqtt_client, "hannah/server", 0);
        char topic[128];
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/mute/set",
                 hannah_config_get()->device_id);
        esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/volume/set",
                 hannah_config_get()->device_id);
        esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/ota/ok",
                 hannah_config_get()->device_id);
        esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/ble/watchlist",
                 hannah_config_get()->device_id);
        esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/assets/relevant",
                 hannah_config_get()->device_id);
        esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/sampling",
                 hannah_config_get()->device_id);
        esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/ptt",
                 hannah_config_get()->device_id);
        esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/play_asset",
                 hannah_config_get()->device_id);
        esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/listen",
                 hannah_config_get()->device_id);
        esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        snprintf(topic, sizeof(topic), "hannah/satellite/%s/restart",
                 hannah_config_get()->device_id);
        esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        break;
    }

    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT getrennt.");
        break;

    case MQTT_EVENT_DATA: {
        net_activity_mark();
        char topic[128] = {0};
        int  tlen = event->topic_len < (int)sizeof(topic) - 1
                    ? event->topic_len : (int)sizeof(topic) - 1;
        memcpy(topic, event->topic, tlen);

        char data[256] = {0};
        int  dlen = event->data_len < (int)sizeof(data) - 1
                    ? event->data_len : (int)sizeof(data) - 1;
        memcpy(data, event->data, dlen);

        if (strcmp(topic, "hannah/server") == 0) {
            char host[64] = {0};
            int  port     = 0;
            cJSON *root = cJSON_ParseWithLength(data, dlen);
            if (root) {
                const cJSON *jh = cJSON_GetObjectItemCaseSensitive(root, "host");
                const cJSON *jp = cJSON_GetObjectItemCaseSensitive(root, "port");
                if (cJSON_IsString(jh) && cJSON_IsNumber(jp)) {
                    strncpy(host, jh->valuestring, sizeof(host) - 1);
                    port = (int)jp->valuedouble;
                }
                cJSON_Delete(root);
            }
            if (port == 0) sscanf(data, "%63[^:]:%d", host, &port);
            if (host[0] && port > 0) {
                if (strcmp(host, s_proxy_host) == 0 && port == s_proxy_port && s_proxy_ready) {
                    ESP_LOGD(TAG, "Proxy %s:%d unverändert.", host, port);
                } else {
                    strncpy(s_proxy_host, host, sizeof(s_proxy_host) - 1);
                    s_proxy_port = port;
                    udp_connect(host, port);
                }
            }
        } else if (strstr(topic, "/mute/set")) {
            bool muted = (data[0] == '1') || (strncmp(data, "true", 4) == 0);
            hannah_net_set_mute(muted);

        } else if (strstr(topic, "/volume/set")) {
            int vol = atoi(data);
            if (vol < 0)   vol = 0;
            if (vol > 100) vol = 100;
            if (s_volume_cb) s_volume_cb(vol);

        } else {
            char ota_ok_topic[128];
            snprintf(ota_ok_topic, sizeof(ota_ok_topic), "hannah/satellite/%s/ota/ok",
                     hannah_config_get()->device_id);
            if (strcmp(topic, ota_ok_topic) == 0) {
                ESP_LOGI(TAG, "OTA-ok empfangen.");
                if (s_ota_ok_cb) s_ota_ok_cb();
            } else {
                char ble_topic[128];
                snprintf(ble_topic, sizeof(ble_topic), "hannah/satellite/%s/ble/watchlist",
                         hannah_config_get()->device_id);
                if (strcmp(topic, ble_topic) == 0) {
                    if (s_ble_watchlist_cb) {
                        s_ble_watchlist_cb(event->data, event->data_len);
                    } else {
                        int len = event->data_len < (int)sizeof(s_ble_watchlist_cache)
                                  ? event->data_len : (int)sizeof(s_ble_watchlist_cache);
                        memcpy(s_ble_watchlist_cache, event->data, len);
                        s_ble_watchlist_cache_len = len;
                        ESP_LOGD(TAG, "BLE-Watchlist zwischengespeichert (%d Bytes).", len);
                    }
                } else {
                    char asset_relevant_topic[128];
                    snprintf(asset_relevant_topic, sizeof(asset_relevant_topic),
                             "hannah/satellite/%s/assets/relevant",
                             hannah_config_get()->device_id);
                    if (strcmp(topic, asset_relevant_topic) == 0) {
                        if (s_asset_relevant_cb) {
                            s_asset_relevant_cb(event->data, event->data_len);
                        } else {
                            int len = event->data_len < (int)sizeof(s_asset_relevant_cache)
                                      ? event->data_len : (int)sizeof(s_asset_relevant_cache);
                            memcpy(s_asset_relevant_cache, event->data, len);
                            s_asset_relevant_cache_len = len;
                            ESP_LOGD(TAG, "Asset-Relevanzliste zwischengespeichert (%d Bytes).", len);
                        }
                    } else {
                        char sampling_topic[128];
                        snprintf(sampling_topic, sizeof(sampling_topic),
                                 "hannah/satellite/%s/sampling",
                                 hannah_config_get()->device_id);
                        if (strcmp(topic, sampling_topic) == 0 && s_sampling_cb) {
                            /* Payload: {"enabled":true,"type":"noise"|"hey_hannah"} */
                            bool enabled = false;
                            char sample_type[32] = "noise";
                            cJSON *sroot = cJSON_ParseWithLength(event->data, event->data_len);
                            if (sroot) {
                                const cJSON *jen = cJSON_GetObjectItemCaseSensitive(sroot, "enabled");
                                const cJSON *jty = cJSON_GetObjectItemCaseSensitive(sroot, "type");
                                if (cJSON_IsBool(jen))   enabled = cJSON_IsTrue(jen);
                                if (cJSON_IsString(jty)) strncpy(sample_type, jty->valuestring, sizeof(sample_type) - 1);
                                cJSON_Delete(sroot);
                            } else {
                                enabled = (strstr(event->data, "\"enabled\":true") != NULL ||
                                           strstr(event->data, "\"enabled\": true") != NULL);
                            }
                            ESP_LOGI(TAG, "Sampling-Modus: %s (type=%s)", enabled ? "an" : "aus", sample_type);
                            s_sampling_cb(enabled, sample_type);
                        } else {
                            char ptt_topic[128];
                            snprintf(ptt_topic, sizeof(ptt_topic),
                                     "hannah/satellite/%s/ptt",
                                     hannah_config_get()->device_id);
                            if (strcmp(topic, ptt_topic) == 0 && s_virtual_ptt_cb) {
                                bool active = (strncmp(data, "true", 4) == 0 || data[0] == '1');
                                ESP_LOGI(TAG, "Virtual PTT: %s", active ? "AN" : "AUS");
                                s_virtual_ptt_cb(active);
                            } else {
                                char play_asset_topic[128];
                                snprintf(play_asset_topic, sizeof(play_asset_topic),
                                         "hannah/satellite/%s/play_asset",
                                         hannah_config_get()->device_id);
                                if (strcmp(topic, play_asset_topic) == 0 && s_play_asset_cb) {
                                    char asset_id[64] = {0};
                                    cJSON *proot = cJSON_ParseWithLength(event->data, event->data_len);
                                    if (proot) {
                                        const cJSON *jid = cJSON_GetObjectItemCaseSensitive(proot, "asset_id");
                                        if (cJSON_IsString(jid))
                                            strncpy(asset_id, jid->valuestring, sizeof(asset_id) - 1);
                                        cJSON_Delete(proot);
                                    }
                                    if (asset_id[0]) {
                                        ESP_LOGI(TAG, "PlayAsset: %s", asset_id);
                                        s_play_asset_cb(asset_id);
                                    }
                                } else {
                                    char listen_topic[128];
                                    snprintf(listen_topic, sizeof(listen_topic),
                                             "hannah/satellite/%s/listen",
                                             hannah_config_get()->device_id);
                                    if (strcmp(topic, listen_topic) == 0) {
                                        ESP_LOGI(TAG, "start_listening via MQTT.");
                                        if (s_start_listening_cb) s_start_listening_cb();
                                    } else {
                                        char restart_topic[128];
                                        snprintf(restart_topic, sizeof(restart_topic),
                                                 "hannah/satellite/%s/restart",
                                                 hannah_config_get()->device_id);
                                        if (strcmp(topic, restart_topic) == 0) {
                                            /* Geordneter Neustart auf Zuruf (#161) — Diagnose-/
                                             * Rejuvenation-Werkzeug für den Ressourcenerschöpfungs-
                                             * Verdacht aus #150: läuft über denselben Pfad wie der
                                             * Netzwerk-Watchdog oben (erst TWDT abmelden, dann
                                             * esp_restart()), damit der Shutdown-Handler-Chain
                                             * (persist_log_to_flash() in hannah_webserver) durchläuft
                                             * statt in einen harten Panic-Reset zu laufen. */
                                            ESP_LOGW(TAG, "Remote-Neustart via MQTT angefordert.");
                                            hannah_net_mark_restart_source("remote");
                                            esp_task_wdt_delete(NULL);
                                            esp_restart();
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        break;
    }

    case MQTT_EVENT_ERROR:
        ESP_LOGW(TAG, "MQTT-Fehler.");
        break;

    default: break;
    }
}

static void mqtt_init(void)
{
    const hannah_config_t *cfg = hannah_config_get();
    char broker_uri[128];
    snprintf(broker_uri, sizeof(broker_uri),
             "mqtt://%s:%d", cfg->mqtt_broker, cfg->mqtt_port);

    char client_id[72];
    snprintf(client_id, sizeof(client_id), "%s-%04lx",
             cfg->device_id, esp_random() & 0xFFFF);

    esp_mqtt_client_config_t mc = {
        .broker.address.uri                  = broker_uri,
        .credentials.username                = cfg->mqtt_user,
        .credentials.authentication.password = cfg->mqtt_pass,
        .credentials.client_id               = client_id,
        .network.reconnect_timeout_ms        = 5000,
    };

    s_mqtt_client = esp_mqtt_client_init(&mc);
    esp_mqtt_client_register_event(s_mqtt_client, ESP_EVENT_ANY_ID,
                                   on_mqtt_event, NULL);
    esp_mqtt_client_start(s_mqtt_client);
    ESP_LOGI(TAG, "MQTT → %s (id=%s)", broker_uri, client_id);
}

/* ── WiFi ────────────────────────────────────────────────────────────────── */

static void wifi_start_ap(void);

/* Periodischer Versuch, das Original-Netz aus dem AP-Setup-Modus heraus
 * wiederzufinden — läuft parallel zum AP weiter (APSTA), damit eine laufende
 * Konfiguration über das Captive Portal nicht durch einen verschwindenden AP
 * unterbrochen wird. Bei Erfolg übernimmt IP_EVENT_STA_GOT_IP den Cutover. */
static void ap_recovery_retry_cb(TimerHandle_t t)
{
    if (!s_ap_mode) { xTimerStop(t, 0); return; }
    ESP_LOGI(TAG, "AP-Modus: Versuche Original-Netz wiederzufinden...");
    esp_wifi_connect();
}

/* True wenn aktuell jemand mit dem Setup-AP verbunden ist — verhindert, dass
 * der AP mitten in einer laufenden Konfiguration verschwindet. */
static bool ap_has_clients(void)
{
    wifi_sta_list_t sta_list;
    if (esp_wifi_ap_get_sta_list(&sta_list) != ESP_OK) return false;
    return sta_list.num > 0;
}

static void ap_exit_setup_mode(void)
{
    ESP_LOGW(TAG, "AP-Modus: kein Client mehr verbunden — verlasse Setup-Modus.");
    s_ap_pending_exit = false;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
}

/* Task zum Wechsel in den AP-Modus (nicht direkt aus Event-Handler aufrufen). */
static void ap_switch_task(void *arg)
{
    if (s_mqtt_client) {
        esp_mqtt_client_stop(s_mqtt_client);
        s_mqtt_client = NULL;
    }
    if (s_udp_sock >= 0) { close(s_udp_sock); s_udp_sock = -1; }
    s_proxy_ready = false;

    esp_wifi_stop();
    wifi_start_ap();
    vTaskDelete(NULL);
}

static void on_wifi_event(void *arg, esp_event_base_t base,
                          int32_t event_id, void *event_data)
{
    if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();

    } else if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_ap_mode) return;   /* AP-Modus aktiv oder Wechsel läuft — ignorieren */

        if (s_wifi_retry < CONFIG_HANNAH_WIFI_MAX_RETRY) {
            s_wifi_retry++;
            ESP_LOGW(TAG, "WiFi getrennt — Versuch %d/%d",
                     s_wifi_retry, CONFIG_HANNAH_WIFI_MAX_RETRY);
            esp_wifi_connect();
        } else {
            ESP_LOGE(TAG, "WiFi: maximale Versuche — starte AP-Modus.");
            s_ap_mode = true;
            xTaskCreate(ap_switch_task, "ap_switch", 4096, NULL, 5, NULL);
        }
        xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);

    } else if (base == WIFI_EVENT && event_id == WIFI_EVENT_AP_STADISCONNECTED) {
        if (s_ap_pending_exit && !ap_has_clients()) ap_exit_setup_mode();

    } else if (base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "IP: " IPSTR, IP2STR(&ev->ip_info.ip));
        s_wifi_retry = 0;
        net_activity_mark();
        if (s_ap_mode) {
            /* Recovery aus dem AP-Setup-Modus: Original-Netz wieder da. AP-Rolle
             * nur sofort abwerfen, wenn niemand mehr am Captive Portal hängt —
             * sonst würde eine laufende Konfiguration (z.B. neuer PSK für einen
             * Netz-Umzug) durch den verschwindenden AP unterbrochen. */
            ESP_LOGW(TAG, "AP-Modus: Original-Netz wiedergefunden.");
            if (s_ap_recovery_retry) xTimerStop(s_ap_recovery_retry, 0);
            s_ap_mode = false;
            if (ap_has_clients()) {
                ESP_LOGW(TAG, "AP-Modus: Client verbunden — AP bleibt bestehen bis Verbindung endet.");
                s_ap_pending_exit = true;
            } else {
                ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
            }
        }
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        mqtt_init();
        if (s_sntp_retry) xTimerStart(s_sntp_retry, 0);
    }
}

static void wifi_driver_init(void)
{
    s_wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    s_sta_netif = esp_netif_create_default_wifi_sta();
    s_ap_netif  = esp_netif_create_default_wifi_ap();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi_event, NULL, NULL));
}

static void wifi_start_sta(void)
{
    const hannah_config_t *cfg = hannah_config_get();
    wifi_config_t wifi_cfg = {
        .sta = { .threshold.authmode = WIFI_AUTH_WPA2_PSK },
    };
    strncpy((char *)wifi_cfg.sta.ssid,     cfg->wifi_ssid, sizeof(wifi_cfg.sta.ssid)     - 1);
    strncpy((char *)wifi_cfg.sta.password, cfg->wifi_pass, sizeof(wifi_cfg.sta.password) - 1);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "WiFi STA → SSID: %s", cfg->wifi_ssid);
}

static void wifi_start_ap(void)
{
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_STA, mac);

    wifi_config_t ap_cfg = {
        .ap = {
            .max_connection = 3,
            .authmode       = WIFI_AUTH_OPEN,
        },
    };
    snprintf((char *)ap_cfg.ap.ssid, sizeof(ap_cfg.ap.ssid),
             "Hannah-Setup-%02x%02x", mac[4], mac[5]);
    ap_cfg.ap.ssid_len = strlen((char *)ap_cfg.ap.ssid);

    /* APSTA statt AP — ermöglicht WiFi-Scan im Setup-Modus */
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* AP hat statische IP 192.168.4.1 — kein GOT_IP-Event, daher hier setzen */
    s_ap_mode = true;
    xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);

    ESP_LOGW(TAG, "AP-Modus: SSID=%s  IP=192.168.4.1", (char *)ap_cfg.ap.ssid);
    ESP_LOGW(TAG, "Webinterface: http://192.168.4.1/");

    /* Nur retrien wenn überhaupt ein Original-Netz bekannt ist — bei
     * Erstinbetriebnahme ohne Credentials wäre das sinnlos. */
    if (hannah_config_has_wifi()) {
        if (!s_ap_recovery_retry) {
            s_ap_recovery_retry = xTimerCreate(
                "ap_recovery", pdMS_TO_TICKS(AP_RECOVERY_INTERVAL_MS),
                pdTRUE, NULL, ap_recovery_retry_cb);
        }
        if (s_ap_recovery_retry) xTimerStart(s_ap_recovery_retry, 0);
    }
}

/* ── Öffentliche API ─────────────────────────────────────────────────────── */

void hannah_net_set_status_callback(hannah_net_status_cb_t cb)      { s_status_cb   = cb; }
void hannah_net_set_tts_callback(hannah_net_tts_cb_t cb)            { s_tts_cb      = cb; }
void hannah_net_set_tts_end_callback(hannah_net_tts_end_cb_t cb)    { s_tts_end_cb  = cb; }
void hannah_net_set_playback_callback(hannah_net_playback_cb_t cb)  { s_playback_cb = cb; }

/* ── SNTP ────────────────────────────────────────────────────────────────── */

static void sntp_sync_cb(struct timeval *tv)
{
    if (s_net_events) xEventGroupSetBits(s_net_events, NET_SNTP_SYNCED_BIT);
    if (s_sntp_retry)  xTimerStop(s_sntp_retry, 0);
    time_t now = tv->tv_sec;
    ESP_LOGI(TAG, "SNTP sync OK: %s", ctime(&now));
}

static void sntp_retry_cb(TimerHandle_t t)
{
    if (s_net_events && (xEventGroupGetBits(s_net_events) & NET_SNTP_SYNCED_BIT)) {
        xTimerStop(t, 0);
        return;
    }
    ESP_LOGW(TAG, "SNTP: Wiederhole Sync-Versuch...");
    esp_sntp_restart();
}

static void *mbedtls_spiram_calloc(size_t n, size_t size)
{
    return heap_caps_calloc(n, size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
}

/* Refs #86 Punkt 2 — hilft nachträglich zu verifizieren, ob ein Reset durch den
 * Netzwerk-Watchdog tatsächlich greift statt an einem Brownout/Panic zu liegen.
 * Nur für "harte" Reset-Gründe relevant — die vier bewussten esp_restart()-Aufrufer
 * (Watchdog/remote/OTA/heap) liefern hier alle denselben Wert (ESP_RST_SW), siehe
 * diag_init() unten für die eigentliche Unterscheidung. */
static const char *reset_reason_str(esp_reset_reason_t r)
{
    switch (r) {
    case ESP_RST_POWERON:   return "Power-On";
    case ESP_RST_EXT:       return "Externer Reset";
    case ESP_RST_SW:        return "Software (esp_restart)";
    case ESP_RST_PANIC:     return "Panic";
    case ESP_RST_INT_WDT:   return "Interrupt-Watchdog";
    case ESP_RST_TASK_WDT:  return "Task-Watchdog";
    case ESP_RST_WDT:       return "Sonstiger Watchdog";
    case ESP_RST_DEEPSLEEP: return "Deep-Sleep-Wakeup";
    case ESP_RST_BROWNOUT:  return "Brownout";
    case ESP_RST_SDIO:      return "SDIO";
    default:                return "Unbekannt";
    }
}

/* Liest+inkrementiert den persistenten Neustart-Zähler und den vor dem letzten
 * bewussten esp_restart() gesetzten Marker (falls vorhanden, dann konsumiert/
 * zurückgesetzt) — leitet daraus s_restart_reason ab (#165). Reihenfolge im Boot
 * unkritisch, da hier bewusst nicht geloggt wird (siehe main.c-Kommentar zum
 * Log-Ringpuffer, der erst nach hannah_webserver_start() nutzbar ist). */
static void diag_init(void)
{
    esp_reset_reason_t reason = esp_reset_reason();

    nvs_handle_t h;
    if (nvs_open(DIAG_NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
        uint32_t count = 0;
        nvs_get_u32(h, DIAG_NVS_KEY_COUNT, &count);
        count++;
        nvs_set_u32(h, DIAG_NVS_KEY_COUNT, count);
        s_restart_count = count;

        char marker[16] = {0};
        size_t len = sizeof(marker);
        nvs_get_str(h, DIAG_NVS_KEY_SRC, marker, &len);
        nvs_set_str(h, DIAG_NVS_KEY_SRC, "");  /* konsumiert — für den nächsten Boot zurückgesetzt */
        nvs_commit(h);
        nvs_close(h);

        if (marker[0] != '\0') {
            strncpy(s_restart_reason, marker, sizeof(s_restart_reason) - 1);
        } else if (reason == ESP_RST_SW) {
            /* Kein Marker + Software-Reset → einzige verbleibende Aufrufstelle
             * ohne Marker ist der reaktive Netzwerk-Watchdog. */
            strncpy(s_restart_reason, "watchdog", sizeof(s_restart_reason) - 1);
        } else {
            strncpy(s_restart_reason, reset_reason_str(reason), sizeof(s_restart_reason) - 1);
        }
        s_restart_reason[sizeof(s_restart_reason) - 1] = '\0';
    }
}

const char *hannah_net_get_restart_reason(void) { return s_restart_reason; }
uint32_t    hannah_net_get_restart_count(void)  { return s_restart_count; }

void hannah_net_mark_restart_source(const char *source)
{
    nvs_handle_t h;
    if (nvs_open(DIAG_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_str(h, DIAG_NVS_KEY_SRC, source);
    nvs_commit(h);
    nvs_close(h);
}

void hannah_net_init(void)
{
    diag_init();

    mbedtls_platform_set_calloc_free(mbedtls_spiram_calloc, free);
    wifi_driver_init();

    if (hannah_config_has_wifi()) {
        esp_sntp_config_t sntp_cfg = {
            .smooth_sync                = false,
            .server_from_dhcp           = true,
            .wait_for_sync              = false,
            .start                      = true,
            .renew_servers_after_new_IP = true,
            .ip_event_to_renew          = IP_EVENT_STA_GOT_IP,
            .index_of_first_server      = 1,
            .num_of_servers             = 1,
            .servers                    = { "pool.ntp.org" },
        };
        esp_netif_sntp_init(&sntp_cfg);
        s_sntp_started = true;
        s_net_events = xEventGroupCreate();
        esp_sntp_set_time_sync_notification_cb(sntp_sync_cb);
        s_sntp_retry = xTimerCreate("sntp_retry", pdMS_TO_TICKS(30000), pdTRUE, NULL, sntp_retry_cb);
        ESP_LOGI(TAG, "SNTP gestartet (DHCP-NTP bevorzugt, Fallback: pool.ntp.org, Retry: 30s).");
        wifi_start_sta();
    } else {
        ESP_LOGW(TAG, "Keine WiFi-Config — starte AP-Modus.");
        wifi_start_ap();
    }

    xTaskCreate(udp_receive_task, "udp_rx",   8192, NULL, 6, NULL);
    xTaskCreate(heartbeat_task,   "heartbeat", 2048, NULL, 3, NULL);
    ESP_LOGI(TAG, "hannah_net initialisiert.");
}

static void send_audio_raw(const uint8_t *pcm, size_t len)
{
    if (s_udp_sock < 0 || !s_proxy_ready) return;
    size_t offset = 0;
    while (offset < len) {
        size_t chunk = len - offset;
        if (chunk > 60000) chunk = 60000;
        uint8_t *pkt = malloc(1 + chunk);
        if (!pkt) return;
        pkt[0] = TYPE_AUDIO;
        memcpy(pkt + 1, pcm + offset, chunk);
        sendto(s_udp_sock, pkt, 1 + chunk, 0,
               (struct sockaddr *)&s_proxy_addr, sizeof(s_proxy_addr));
        free(pkt);
        offset += chunk;
    }
}

void hannah_net_send_audio(const uint8_t *pcm, size_t len)
{
    if (s_muted) return;
    send_audio_raw(pcm, len);
}

/* Wie hannah_net_send_audio, ignoriert aber Mute-Status (für Sampling-Mode). */
void hannah_net_send_audio_sampling(const uint8_t *pcm, size_t len)
{
    send_audio_raw(pcm, len);
}

void hannah_net_send_audio_end(void)
{
    char msg[96];
    snprintf(msg, sizeof(msg),
             "{\"type\":\"audio_end\",\"device\":\"%s\"}",
             hannah_config_get()->device_id);
    send_control(msg);
}

bool hannah_net_is_muted(void) { return s_muted; }

void hannah_net_set_hw_mute_callback(hannah_net_hw_mute_cb_t cb) { s_hw_mute_cb = cb; }

void hannah_net_set_mute(bool muted)
{
    if (muted == s_muted) return;
    s_muted = muted;
    if (s_hw_mute_cb) s_hw_mute_cb(muted);
    ESP_LOGI(TAG, "Mute: %s", muted ? "AN" : "AUS");
    char topic[128];
    snprintf(topic, sizeof(topic), "hannah/satellite/%s/mute/state",
             hannah_config_get()->device_id);
    hannah_net_mqtt_publish(topic, muted ? "true" : "false", 1, 1);
}

bool hannah_net_is_ap_mode(void)
{
    return s_ap_mode;
}

void hannah_net_get_ip_str(char *buf, size_t len)
{
    esp_netif_t     *netif = s_ap_mode ? s_ap_netif : s_sta_netif;
    esp_netif_ip_info_t info;
    if (netif && esp_netif_get_ip_info(netif, &info) == ESP_OK)
        snprintf(buf, len, IPSTR, IP2STR(&info.ip));
    else
        snprintf(buf, len, "0.0.0.0");
}

void hannah_net_set_ota_ok_callback(hannah_net_ota_ok_cb_t cb)        { s_ota_ok_cb        = cb; }
void hannah_net_set_registered_callback(hannah_net_registered_cb_t cb) { s_registered_cb    = cb; }
void hannah_net_set_ble_watchlist_callback(hannah_net_ble_watchlist_cb_t cb)
{
    s_ble_watchlist_cb = cb;
    if (cb && s_ble_watchlist_cache_len > 0) {
        cb(s_ble_watchlist_cache, s_ble_watchlist_cache_len);
        s_ble_watchlist_cache_len = 0;
    }
}
void hannah_net_set_asset_relevant_callback(hannah_net_asset_relevant_cb_t cb)
{
    s_asset_relevant_cb = cb;
    if (cb && s_asset_relevant_cache_len > 0) {
        cb(s_asset_relevant_cache, s_asset_relevant_cache_len);
        s_asset_relevant_cache_len = 0;
    }
}
void hannah_net_set_volume_callback(hannah_net_volume_cb_t cb)        { s_volume_cb        = cb; }
void hannah_net_set_sampling_callback(hannah_net_sampling_cb_t cb)       { s_sampling_cb      = cb; }
void hannah_net_set_virtual_ptt_callback(hannah_net_virtual_ptt_cb_t cb)       { s_virtual_ptt_cb        = cb; }
void hannah_net_set_play_asset_callback(hannah_net_play_asset_cb_t cb)         { s_play_asset_cb         = cb; }
void hannah_net_set_start_listening_callback(hannah_net_start_listening_cb_t cb) { s_start_listening_cb  = cb; }

void hannah_net_publish_volume(int vol)
{
    char topic[128];
    snprintf(topic, sizeof(topic), "hannah/satellite/%s/volume/state",
             hannah_config_get()->device_id);
    char payload[8];
    snprintf(payload, sizeof(payload), "%d", vol);
    hannah_net_mqtt_publish(topic, payload, 1, 1);
}

bool hannah_net_wait_sntp(uint32_t timeout_ms)
{
    if (s_ap_mode || !s_sntp_started || !s_net_events) return false;
    EventBits_t bits = xEventGroupWaitBits(s_net_events, NET_SNTP_SYNCED_BIT,
                                           pdFALSE, pdTRUE,
                                           pdMS_TO_TICKS(timeout_ms));
    if (!(bits & NET_SNTP_SYNCED_BIT)) {
        ESP_LOGW(TAG, "SNTP sync timeout nach %lu ms.", timeout_ms);
        return false;
    }
    return true;
}

void hannah_net_mqtt_publish(const char *topic, const char *payload, int qos, int retain)
{
    if (!s_mqtt_client) {
        ESP_LOGW(TAG, "mqtt_publish: kein Client — %s", topic);
        return;
    }
    int msg_id = esp_mqtt_client_publish(s_mqtt_client, topic, payload, 0, qos, retain);
    if (msg_id < 0)
        ESP_LOGW(TAG, "mqtt_publish fehlgeschlagen: %s", topic);
    else
        ESP_LOGD(TAG, "mqtt_publish OK (id=%d): %s", msg_id, topic);
}

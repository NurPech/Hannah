/**
 * hannah_ble — passiver BLE-Scanner für Indoor-Lokalisierung
 *
 * Scannt kontinuierlich BLE-Advertisements. Für jede MAC aus der Watchlist
 * wird der RSSI-Wert periodisch via MQTT gemeldet (Rate-Limit per Kconfig).
 *
 * BLE/WiFi-Koexistenz: ESP32-S3 unterstützt Koexistenz nativ.
 * Voraussetzung in sdkconfig: CONFIG_BT_ENABLED + CONFIG_BT_NIMBLE_ENABLED
 * + CONFIG_ESP_COEX_SW_COEXIST_ENABLE
 */

#include "hannah_ble.h"
#include "hannah_config.h"
#include "hannah_net.h"

#include <string.h>
#include <stdio.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"

static const char *TAG = "ble";

#if CONFIG_HANNAH_BLE_ENABLED
    #include "nimble/nimble_port.h"
    #include "nimble/nimble_port_freertos.h"
    #include "host/ble_hs.h"
    #include "host/ble_gap.h"
    #include "cJSON.h"

    typedef struct {
        uint8_t  mac[6];
        int64_t  last_report_us;
    } ble_watch_entry_t;

    typedef struct {
        uint8_t mac[6];
        int8_t  rssi;
    } ble_report_item_t;

    static ble_watch_entry_t s_watchlist[CONFIG_HANNAH_BLE_WATCHLIST_MAX];
    static int               s_watchlist_count = 0;
    static SemaphoreHandle_t s_mutex;
    static QueueHandle_t     s_report_queue;

    /* ── Hilfsfunktionen ─────────────────────────────────────────────────────── */

    static bool parse_mac(const char *str, uint8_t out[6])
    {
        unsigned int b[6];
        if (sscanf(str, "%x:%x:%x:%x:%x:%x",
                &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6) {
            return false;
        }
        for (int i = 0; i < 6; i++) out[i] = (uint8_t)b[i];
        return true;
    }

    /* ── Watchlist ───────────────────────────────────────────────────────────── */

    void hannah_ble_set_watchlist_json(const char *json, int len)
    {
        cJSON *root = cJSON_ParseWithLength(json, len);
        if (!root) {
            ESP_LOGW(TAG, "Watchlist-JSON ungültig.");
            return;
        }

        const cJSON *macs = cJSON_GetObjectItemCaseSensitive(root, "macs");
        if (!cJSON_IsArray(macs)) {
            ESP_LOGW(TAG, "Watchlist: kein 'macs'-Array.");
            cJSON_Delete(root);
            return;
        }

        xSemaphoreTake(s_mutex, portMAX_DELAY);
        s_watchlist_count = 0;
        const cJSON *item;
        cJSON_ArrayForEach(item, macs) {
            if (!cJSON_IsString(item)) continue;
            if (s_watchlist_count >= CONFIG_HANNAH_BLE_WATCHLIST_MAX) break;
            uint8_t mac[6];
            if (parse_mac(item->valuestring, mac)) {
                memcpy(s_watchlist[s_watchlist_count].mac, mac, 6);
                s_watchlist[s_watchlist_count].last_report_us = 0;
                s_watchlist_count++;
            }
        }
        ESP_LOGI(TAG, "Watchlist aktualisiert: %d MAC(s).", s_watchlist_count);
        xSemaphoreGive(s_mutex);

        cJSON_Delete(root);
    }

    /* ── BLE-Scan-Event-Handler ──────────────────────────────────────────────── */

    /* Läuft im Kontext des nimble_host-Tasks (siehe ble_host_task()) — dessen
     * Stack ist knapp bemessen (CONFIG_BT_NIMBLE_HOST_TASK_STACK_SIZE) und
     * bereits durch NimBLEs eigene Verarbeitung des Discovery-Events
     * ausgelastet. Deshalb hier nur die schnelle Watchlist-Prüfung, alles
     * Weitere (String-Formatierung, MQTT-Publish) läuft in ble_report_task()
     * mit eigenem Stack (Refs #291 — Stack-Overflow im nimble_host-Task).
     */
    static int ble_gap_event_handler(struct ble_gap_event *event, void *arg)
    {
        if (event->type != BLE_GAP_EVENT_DISC) return 0;

        const uint8_t *addr = event->disc.addr.val;
        int8_t         rssi = event->disc.rssi;

        xSemaphoreTake(s_mutex, portMAX_DELAY);
        for (int i = 0; i < s_watchlist_count; i++) {
            /* NimBLE liefert MAC in umgekehrter Byte-Reihenfolge */
            bool match = true;
            for (int b = 0; b < 6; b++) {
                if (addr[b] != s_watchlist[i].mac[5 - b]) { match = false; break; }
            }
            if (!match) continue;

            int64_t now = esp_timer_get_time();
            int64_t interval_us = (int64_t)CONFIG_HANNAH_BLE_REPORT_INTERVAL_MS * 1000;
            if (now - s_watchlist[i].last_report_us < interval_us) break;

            s_watchlist[i].last_report_us = now;

            ble_report_item_t report;
            memcpy(report.mac, s_watchlist[i].mac, 6);
            report.rssi = rssi;
            xSemaphoreGive(s_mutex);

            if (xQueueSend(s_report_queue, &report, 0) != pdTRUE) {
                ESP_LOGW(TAG, "Report-Queue voll — RSSI-Update verworfen.");
            }
            return 0;
        }
        xSemaphoreGive(s_mutex);
        return 0;
    }

    /* ── Report-Task (entkoppelt von nimble_host, siehe oben) ───────────────── */

    static void ble_report_task(void *arg)
    {
        ble_report_item_t report;
        while (1) {
            if (xQueueReceive(s_report_queue, &report, portMAX_DELAY) != pdTRUE) continue;

            char mac_str[18];
            snprintf(mac_str, sizeof(mac_str),
                    "%02x:%02x:%02x:%02x:%02x:%02x",
                    report.mac[0], report.mac[1], report.mac[2],
                    report.mac[3], report.mac[4], report.mac[5]);

            char topic[128], payload[64];
            snprintf(topic,   sizeof(topic),   "hannah/satellite/%s/ble/report",
                    hannah_config_get()->device_id);
            snprintf(payload, sizeof(payload), "{\"mac\":\"%s\",\"rssi\":%d}",
                    mac_str, report.rssi);
            hannah_net_mqtt_publish(topic, payload, 0, 0);
            ESP_LOGD(TAG, "BLE: %s RSSI=%d", mac_str, report.rssi);
        }
    }

    /* ── Scan starten / neustarten ───────────────────────────────────────────── */

    static void start_scan(void)
    {
        struct ble_gap_disc_params params = {
            .passive          = 1,
            .filter_duplicates = 0,
            .itvl             = 0,
            .window           = 0,
            .filter_policy    = BLE_HCI_SCAN_FILT_NO_WL,
            .limited          = 0,
        };
        int rc = ble_gap_disc(BLE_OWN_ADDR_PUBLIC, BLE_HS_FOREVER, &params,
                            ble_gap_event_handler, NULL);
        if (rc != 0) {
            ESP_LOGW(TAG, "ble_gap_disc fehlgeschlagen: %d — Retry in 5s.", rc);
            vTaskDelay(pdMS_TO_TICKS(5000));
            start_scan();
        } else {
            ESP_LOGI(TAG, "BLE-Scan gestartet (passiv, kontinuierlich).");
        }
    }

    static void ble_on_sync(void)
    {
        start_scan();
    }

    static void ble_host_task(void *arg)
    {
        nimble_port_run();
        nimble_port_freertos_deinit();
    }

    /* ── Öffentliche API ─────────────────────────────────────────────────────── */

    void hannah_ble_init(void)
    {
        s_mutex = xSemaphoreCreateMutex();
        s_report_queue = xQueueCreate(8, sizeof(ble_report_item_t));

        esp_err_t ret = nimble_port_init();
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "nimble_port_init fehlgeschlagen: %d", ret);
            return;
        }

        ble_hs_cfg.sync_cb = ble_on_sync;

        hannah_net_set_ble_watchlist_callback(hannah_ble_set_watchlist_json);

        xTaskCreate(ble_report_task, "ble_report", 3072, NULL, 3, NULL);
        nimble_port_freertos_init(ble_host_task);
        ESP_LOGI(TAG, "BLE-Scanner initialisiert.");

    }
#else
    void hannah_ble_init(void)
    {
        ESP_LOGW(TAG, "BLE-Scanner deaktiviert (Kconfig).");
    }
    void hannah_ble_set_watchlist_json(const char *json, int len)
    {
        ESP_LOGW(TAG, "Watchlist-Update ignoriert: BLE-Scanner deaktiviert (Kconfig).");
    }
#endif

/**
 * Hannah Satellite — ESP32-S3
 *
 * Pin-Übersicht: main/pinmap.h
 */

#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"
#include "esp_system.h"
#include "driver/gpio.h"

#include "hannah_config.h"
#include "hannah_net.h"
#include "hannah_audio.h"
#include "hannah_led.h"
#include "hannah_sensors.h"
#include "hannah_webserver.h"
#include "hannah_ota.h"
#include "hannah_ble.h"
#include "hannah_asset.h"
#include "hannah_sd.h"

static const char *TAG = "main";

/* Refs #86 Punkt 2 — hilft nachträglich zu verifizieren, ob ein Reset durch
 * den Netzwerk-Watchdog (hannah_net.c) tatsächlich greift, statt z.B. an
 * einem Brownout oder Panic zu liegen. Geloggt erst nach hannah_webserver_start(),
 * damit es im Ringpuffer landet und ohne seriellen Zugang sichtbar ist. */
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

static void on_play_asset(const char *asset_id)
{
    hannah_asset_play_async(asset_id);
}

/* Meldet das Ergebnis eines play_asset-Versuchs an Core zurück (#116) — vorher war
 * play_asset komplett Fire-and-Forget, ein fehlgeschlagenes Play (Asset nicht im
 * Cache, kaputter WAV-Header) blieb rein lokal auf dem Gerät sichtbar (ESP_LOGW). */
static void on_play_asset_result(const char *asset_id, bool ok)
{
    char topic[128];
    snprintf(topic, sizeof(topic), "hannah/satellite/%s/play_asset/result",
             hannah_config_get()->device_id);
    char payload[96];
    snprintf(payload, sizeof(payload), "{\"asset_id\":\"%s\",\"ok\":%s}",
             asset_id, ok ? "true" : "false");
    hannah_net_mqtt_publish(topic, payload, 1, false);
}

/* Mute beim Start gedrückt halten → WiFi-Einstellungen löschen → AP-Modus */
static void check_factory_reset(void)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << CONFIG_HANNAH_MUTE_GPIO),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    vTaskDelay(pdMS_TO_TICKS(50));

    if (gpio_get_level(CONFIG_HANNAH_MUTE_GPIO) == 0) {
        ESP_LOGW(TAG, "*** Factory Reset: Mute beim Start gedrückt ***");
        ESP_LOGW(TAG, "*** WiFi-Einstellungen werden gelöscht — startet im AP-Modus ***");
        nvs_handle_t h;
        if (nvs_open("hannah", NVS_READWRITE, &h) == ESP_OK) {
            nvs_set_str(h, "wifi_ssid", "");
            nvs_set_str(h, "wifi_pass", "");
            nvs_commit(h);
            nvs_close(h);
        }
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "Hannah Satellite starting...");

    /* NVS initialisieren (wird von hannah_config und WiFi-Stack genutzt) */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* Status-LED sofort einschalten */
    hannah_status_led_init();

    /* Mute beim Start gedrückt? → WiFi löschen → AP-Modus */
    check_factory_reset();

    /* Konfiguration aus NVS laden (sdkconfig-Defaults beim Erststart) */
    hannah_config_init();

    /* LED-Ring — sofort visuelles Feedback */
    hannah_led_init();
    hannah_led_set_state(LED_STATE_BOOT);

    /* Netzwerk: STA wenn Config vorhanden, sonst AP-Setup-Modus */
    hannah_net_init();

    /* Webserver — immer aktiv (STA: erreichbar über LAN-IP, AP: 192.168.4.1) */
    hannah_webserver_start();

    /* Reset-Grund — erst jetzt loggen, damit er im Log-Ringpuffer landet
     * (siehe /log bzw. /log/last) statt nur auf UART zu verschwinden. */
    esp_reset_reason_t reset_reason = esp_reset_reason();
    ESP_LOGI(TAG, "Reset-Grund: %s (%d)", reset_reason_str(reset_reason), reset_reason);

    /* Sensoren — vor Audio-Pipeline initialisieren: auf PCB Rev.5+ teilt
     * sich der ADAU7118 (TDM-Mic-Wandler) den I2C-Bus mit dem BME680, der
     * hier angelegt wird (hannah_sensors_get_i2c_bus()). */
    hannah_sensors_init();

    /* Audio-Pipeline */
    hannah_audio_init();

    /* SD-Karte */
    hannah_sd_init();

    /* OTA-Update-Check (Poll im Hintergrund, kein Flash-Vorgang) */
    hannah_ota_init();

    /* Asset-Cache (WAV-Sounds) */
    hannah_net_set_play_asset_callback(on_play_asset);
    hannah_asset_set_play_result_callback(on_play_asset_result);
    hannah_asset_init();

    /* BLE-Scanner für Indoor-Lokalisierung */
    hannah_ble_init();

    /* LED bleibt in BOOT — hannah_audio mic_task setzt LED_STATE_IDLE nach Warmup */
    ESP_LOGI(TAG, "All components initialized.");
}

/**
 * Hannah Satellite — ESP32-S3
 *
 * Haupteintrittspunkt. Initialisiert alle Komponenten und startet die Tasks.
 *
 * Abhängigkeiten:
 *   hannah_net   — WiFi-Verbindung, MQTT-Discovery, UDP-Audio-Stream
 *   hannah_audio — I2S Mic (INMP441 × 2), I2S Speaker (MAX98357A), ESP-SR AFE
 *   hannah_led   — WS2812B-Ring, State-Machine (idle/wake/stream/speak/mute/error)
 */

#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "esp_log.h"

#include "hannah_net.h"
#include "hannah_audio.h"
#include "hannah_led.h"

static const char *TAG = "main";

void app_main(void)
{
    ESP_LOGI(TAG, "Hannah Satellite starting...");

    /* NVS für WiFi-Credentials */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* LED-Ring zuerst — gibt sofort visuelles Feedback beim Start */
    hannah_led_init();
    hannah_led_set_state(LED_STATE_BOOT);

    /* Netzwerk + MQTT + UDP (WiFi-Verbindung läuft asynchron weiter) */
    hannah_net_init();

    /* Audio-Pipeline: I2S Mic + Speaker + PTT-Button + hannah_net-Callbacks */
    hannah_audio_init();

    /* LED auf IDLE — Verbindungsaufbau läuft im Hintergrund */
    hannah_led_set_state(LED_STATE_IDLE);

    ESP_LOGI(TAG, "All components initialized.");
    /* Tasks laufen ab hier autonom in FreeRTOS */
}

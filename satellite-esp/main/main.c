/**
 * Hannah Satellite — ESP32-S3
 *
 * Pin-Übersicht: main/pinmap.h
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_partition.h"
#include "esp_flash.h"
#include "esp_core_dump.h"
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

static void on_play_asset(const char *asset_id)
{
    hannah_asset_play_async(asset_id);
}

/* Connect-Sound (#7) — der Satellit spielt ihn selbst, sobald er sich registriert
 * hat, statt auf ein Kommando von Core zu warten. hannah_asset_play_async() ist
 * bewusst tolerant: liegt "connect" noch nicht im Cache (z.B. allererster Boot
 * nach Erst-Flash, bevor der erste Asset-Sync durchgelaufen ist), passiert
 * einfach nichts — genau das gleiche eventually-consistent Verhalten wie beim
 * Wakeword-Modell-Override, das nach einem Update auch erst den nächsten Reboot
 * braucht. */
static void on_satellite_registered(void)
{
    hannah_asset_play_async("connect");
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

/* ── Partitionstabellen-Selfupdate (#280) ────────────────────────────────── */

/* #280 fügt der Partitionstabelle eine neue "coredump"-Partition hinzu. Normales
 * OTA (esp_https_ota(), siehe hannah_ota.c) schreibt nur den App-Slot, nie die
 * Partitionstabelle selbst (eigener Flash-Bereich bei CONFIG_PARTITION_TABLE_OFFSET,
 * außerhalb von partitions.csv) — bereits deployte Geräte bekommen die neue
 * Partition also nie automatisch über OTA. Diese Funktion gleicht das aus: sie
 * vergleicht die aktive Tabelle gegen die ins Firmware-Image eingebettete
 * Ziel-Tabelle (siehe main/CMakeLists.txt, target_add_binary_data) und schreibt
 * sie bei Bedarf einmalig um — als Allererstes in app_main(), bevor irgendein
 * anderes Subsystem den Flash-Bus braucht.
 *
 * Akzeptiertes Restrisiko (Leonie, 2026-09-11): ein Stromausfall exakt während
 * esp_flash_erase_region()/esp_flash_write() unten würde das Gerät ohne
 * Recovery-Möglichkeit bricken (kein UART/Download-Mode-Zugriff mehr auf
 * bereits verbaute, im Gehäuse steckende Rev5-Satelliten). Risiko ist auf
 * dieses kurze Schreibfenster begrenzt, da alle bestehenden Partitionen
 * (nvs/otadata/app0/app1/spiffs) ihre Offsets/Größen unverändert behalten —
 * die neue Tabelle unterscheidet sich nur um einen zusätzlichen Eintrag im
 * bisher ungenutzten Bereich nach spiffs. Deshalb: Readback-Verifikation vor
 * jedem Neustart — bei Fehlschlag KEIN Neustart, Gerät bootet mit der alten
 * (weiterhin funktionsfähigen) Tabelle normal weiter, nur ohne
 * Coredump-Feature für diesen Boot. */

extern const uint8_t coredump_partition_table_start[] asm("_binary_coredump_partition_table_start");
extern const uint8_t coredump_partition_table_end[]   asm("_binary_coredump_partition_table_end");

static void apply_partition_table_update_if_needed(void)
{
    if (esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_COREDUMP, NULL)) {
        return; /* Tabelle bereits aktuell (Neu-Flash, RMA, oder schon selbst-aktualisiert) */
    }

    const uint8_t *new_table = coredump_partition_table_start;
    size_t new_table_size = (size_t)(coredump_partition_table_end - coredump_partition_table_start);

    ESP_LOGW(TAG, "Partitionstabelle veraltet (#280) — schreibe neue Tabelle (%u Bytes) nach 0x%x",
             (unsigned)new_table_size, CONFIG_PARTITION_TABLE_OFFSET);

    uint8_t *readback = malloc(new_table_size);
    if (!readback) {
        ESP_LOGE(TAG, "Partitionstabellen-Update: kein Speicher für Readback-Puffer — abgebrochen.");
        return;
    }

    bool ok = false;
    for (int attempt = 1; attempt <= 3 && !ok; attempt++) {
        if (esp_flash_erase_region(esp_flash_default_chip, CONFIG_PARTITION_TABLE_OFFSET, 0x1000) != ESP_OK) {
            ESP_LOGE(TAG, "Partitionstabellen-Update: Erase fehlgeschlagen (Versuch %d)", attempt);
            continue;
        }
        if (esp_flash_write(esp_flash_default_chip, new_table, CONFIG_PARTITION_TABLE_OFFSET, new_table_size) != ESP_OK) {
            ESP_LOGE(TAG, "Partitionstabellen-Update: Write fehlgeschlagen (Versuch %d)", attempt);
            continue;
        }
        if (esp_flash_read(esp_flash_default_chip, readback, CONFIG_PARTITION_TABLE_OFFSET, new_table_size) != ESP_OK ||
            memcmp(readback, new_table, new_table_size) != 0) {
            ESP_LOGE(TAG, "Partitionstabellen-Update: Verifikation fehlgeschlagen (Versuch %d)", attempt);
            continue;
        }
        ok = true;
    }
    free(readback);

    if (!ok) {
        ESP_LOGE(TAG, "Partitionstabellen-Update endgültig fehlgeschlagen — bootet mit alter Tabelle weiter.");
        return;
    }

    ESP_LOGW(TAG, "Partitionstabelle erfolgreich aktualisiert — Neustart.");
    esp_restart();
}

void app_main(void)
{
    ESP_LOGI(TAG, "Hannah Satellite starting...");

    /* Muss vor allem anderen laufen, das den Flash-Bus nutzt (#280) */
    apply_partition_table_update_if_needed();

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

    /* Reset-Grund + Neustart-Zähler (#165, ermittelt in hannah_net_init() oben) —
     * erst jetzt loggen, damit es im Log-Ringpuffer landet (siehe /log bzw.
     * /log/last) statt nur auf UART zu verschwinden. */
    ESP_LOGI(TAG, "Reset-Grund: %s, Neustart #%lu",
             hannah_net_get_restart_reason(), (unsigned long)hannah_net_get_restart_count());

    /* Coredump-Check (#280) — rein lokal fürs Log, der eigentliche
     * MQTT-Announce folgt verzögert in hannah_ota's ota_poll_task, sobald
     * WiFi/MQTT stehen (siehe dort). */
    if (esp_core_dump_image_check() == ESP_OK) {
        ESP_LOGW(TAG, "Coredump im Flash vorhanden — abrufbar via GET /debug/coredump");
    }

    /* Sensoren — vor Audio-Pipeline initialisieren: auf PCB Rev.5+ teilt
     * sich der ADAU7118 (TDM-Mic-Wandler) den I2C-Bus mit dem BME680, der
     * hier angelegt wird (hannah_sensors_get_i2c_bus()). */
    hannah_sensors_init();

    /* Asset-Cache — vor Audio-Pipeline initialisieren: hannah_audio_init()
     * ruft synchron hannah_wakeword_init() auf, das beim Start einen
     * gecachten Wakeword-Modell-Override aus SPIFFS lesen will (#166).
     * SPIFFS muss dafür schon gemountet sein. */
    hannah_net_set_play_asset_callback(on_play_asset);
    hannah_asset_set_play_result_callback(on_play_asset_result);
    hannah_asset_init();
    hannah_net_set_registered_callback(on_satellite_registered);

    /* Audio-Pipeline */
    hannah_audio_init();

    /* SD-Karte */
    hannah_sd_init();

    /* OTA-Update-Check (Poll im Hintergrund, kein Flash-Vorgang) */
    hannah_ota_init();

    /* BLE-Scanner für Indoor-Lokalisierung */
    hannah_ble_init();

    /* LED bleibt in BOOT — hannah_audio mic_task setzt LED_STATE_IDLE nach Warmup */
    ESP_LOGI(TAG, "All components initialized.");
}

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * hannah_asset — generischer Asset-Cache via SPIFFS + Asset-Server (Namespace per NVS
 * überschreibbar, Default "satellite" — Refs #187)
 *
 * Ablauf:
 *   hannah_asset_init() — SPIFFS mounten, dann reaktiv warten: ein Sync-Durchlauf
 *                          (Manifest prüfen, fehlende/veraltete Assets herunterladen,
 *                          nicht mehr relevante Cache-Einträge löschen) läuft erst,
 *                          wenn Core seine Relevanzliste über das retained MQTT-Topic
 *                          hannah/satellite/<device>/assets/relevant publiziert (#170)
 *                          — kein blindes "alles im Manifest laden" mehr und kein
 *                          fester Boot-Timer. Zusätzlich zur Core-Relevanzliste bleibt
 *                          eine kleine, fest im Firmware-Code verankerte Ausnahmeliste
 *                          relevant, unabhängig von Core ("wakeword", #166 Modell-Override,
 *                          und "connect", #7 Connect-Sound). Assets werden unverändert (ohne
 *                          Dateiendung) unter ihrer Asset-ID gecacht — der Inhalt ist
 *                          dem Cache egal, nur den Konsumenten (Sound-Player,
 *                          Wakeword-Modell, ...) nicht.
 *   hannah_asset_play() — Asset als WAV aus SPIFFS lesen und über hannah_audio
 *                          abspielen. Gibt false zurück wenn das Asset nicht im
 *                          Cache liegt oder der WAV-Header ungültig ist (#116).
 *   hannah_asset_play_async() — wie play(), aber in eigenem Task (MQTT-safe).
 *                          Meldet das Ergebnis an den per
 *                          hannah_asset_set_play_result_callback() registrierten
 *                          Callback (falls gesetzt) — main.c nutzt das, um Core
 *                          per MQTT über fehlgeschlagene Play-Versuche zu informieren.
 *   hannah_asset_read_to_psram() — beliebiges gecachtes Asset komplett in einen
 *                          PSRAM-Buffer lesen, für Nicht-Audio-Konsumenten wie
 *                          ein Wakeword-Modell-Override (siehe hannah_wakeword,
 *                          #166). Buffer wird nie freigegeben (für die
 *                          Lebensdauer des Programms gedacht) — kein free() nötig.
 *
 * Konfiguration (Kconfig / sdkconfig.defaults.ci):
 *   HANNAH_ASSET_SERVER_URL   — Asset-Server-URL (ohne abschließenden Slash)
 *   HANNAH_ASSET_SERVER_TOKEN — Bearer-Token
 */

typedef void (*hannah_asset_play_result_cb_t)(const char *asset_id, bool ok);

void hannah_asset_init(void);

/* SPIFFS erneut mounten, ohne den Rest von hannah_asset_init() zu wiederholen
 * (Sync-Task/Callback nur einmal beim Boot). Für den Fall, dass OTA nach
 * esp_vfs_spiffs_unregister() fehlschlägt und der Override-Zugriff
 * (hannah_asset_read_to_psram) wieder funktionieren muss. */
bool hannah_asset_remount(void);
bool hannah_asset_play(const char *asset_id);
void hannah_asset_play_async(const char *asset_id);
void hannah_asset_set_play_result_callback(hannah_asset_play_result_cb_t cb);
bool hannah_asset_read_to_psram(const char *asset_id, uint8_t **out_buf, size_t *out_size);

#ifdef __cplusplus
}
#endif

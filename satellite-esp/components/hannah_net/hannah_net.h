#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

/**
 * hannah_net — WiFi + MQTT-Discovery + UDP-Audio-Stream
 *
 * Konfiguration über sdkconfig (menuconfig):
 *   HANNAH_WIFI_SSID         — WLAN-SSID
 *   HANNAH_WIFI_PASS         — WLAN-Passwort
 *   HANNAH_DEVICE_ID         — Geräte-ID (z.B. "wohnzimmer-esp")
 *   HANNAH_ROOM_NAME         — Raum-Name (z.B. "Wohnzimmer")
 *   HANNAH_MQTT_BROKER       — Broker-IP (z.B. "192.168.8.1")
 *   HANNAH_MQTT_PORT         — Broker-Port (Standard 1883)
 *   HANNAH_MQTT_USER/PASS    — Credentials
 *   HANNAH_UDP_LISTEN_PORT   — Lokaler Port für TTS-Empfang
 *   HANNAH_HEARTBEAT_INTERVAL_S — Heartbeat-Intervall in Sekunden
 *
 * Ablauf:
 *   1. WiFi STA verbinden
 *   2. MQTT-Client starten → "hannah/server" abonnieren (Discovery)
 *   3. Bei Discovery-Payload "IP:Port": UDP-Socket aufbauen, Register senden
 *   4. UDP-Receive-Task: TTS-Chunks + Status-Meldungen empfangen
 *   5. Heartbeat-Task: periodisch Heartbeat an Proxy senden
 *
 * Callbacks:
 *   on_status  — Status-Meldung vom Server ("idle"/"listening"/"processing"/"speaking")
 *   on_tts     — TTS-PCM-Chunk empfangen (Chunk-weise, sample_rate bei tts_end)
 *   on_tts_end — TTS-Stream abgeschlossen (sample_rate in Hz)
 */

/* Callback-Typen */
typedef void (*hannah_net_status_cb_t)(const char *state);
typedef void (*hannah_net_tts_cb_t)(const uint8_t *pcm, size_t len);
typedef void (*hannah_net_tts_end_cb_t)(int sample_rate);
typedef void (*hannah_net_playback_cb_t)(const char *cmd);  /* "stop"|"pause"|"resume" */

void hannah_net_init(void);

/* Callbacks registrieren — vor hannah_net_init() aufrufen */
void hannah_net_set_status_callback(hannah_net_status_cb_t cb);
void hannah_net_set_tts_callback(hannah_net_tts_cb_t cb);
void hannah_net_set_tts_end_callback(hannah_net_tts_end_cb_t cb);
void hannah_net_set_playback_callback(hannah_net_playback_cb_t cb);

/* PCM-Bytes über UDP zum Proxy senden (TYPE_AUDIO = 0x02). */
void hannah_net_send_audio(const uint8_t *pcm, size_t len);

/* Wie hannah_net_send_audio, ignoriert aber Mute-Status (für Sampling-Mode). */
void hannah_net_send_audio_sampling(const uint8_t *pcm, size_t len);

/* audio_end-Kontrollnachricht senden — Aufnahme abgeschlossen. */
void hannah_net_send_audio_end(void);

/* Mute-Status */
bool hannah_net_is_muted(void);
void hannah_net_set_mute(bool muted);

/* Hardware-Mute-Callback: wird von hannah_audio registriert und bei
 * jedem hannah_net_set_mute()-Aufruf ausgeführt. */
typedef void (*hannah_net_hw_mute_cb_t)(bool muted);
void hannah_net_set_hw_mute_callback(hannah_net_hw_mute_cb_t cb);

/* Volume-Callback: wird aufgerufen wenn hannah/satellite/<device>/volume/set empfangen. */
typedef void (*hannah_net_volume_cb_t)(int volume);
void hannah_net_set_volume_callback(hannah_net_volume_cb_t cb);

/* Publiziert den aktuellen Volume-Level auf hannah/satellite/<device>/volume/state. */
void hannah_net_publish_volume(int vol);

/* True wenn WiFi im AP-Setup-Modus läuft (keine STA-Verbindung). */
bool hannah_net_is_ap_mode(void);

/* Schreibt die aktuelle IP-Adresse als String in buf (z.B. "192.168.8.42"
 * im STA-Modus oder "192.168.4.1" im AP-Modus). */
void hannah_net_get_ip_str(char *buf, size_t len);

/* MQTT-Nachricht veröffentlichen — für andere Komponenten (z.B. hannah_ota). */
void hannah_net_mqtt_publish(const char *topic, const char *payload, int qos, int retain);

/* OTA-ok-Callback: wird aufgerufen wenn hannah/satellite/<device>/ota/ok empfangen. */
typedef void (*hannah_net_ota_ok_cb_t)(void);
void hannah_net_set_ota_ok_callback(hannah_net_ota_ok_cb_t cb);

/* BLE-Watchlist-Callback: wird aufgerufen wenn hannah/satellite/<device>/ble/watchlist empfangen.
 * json/len zeigen direkt auf den MQTT-Payload-Puffer (nur während des Callbacks gültig). */
typedef void (*hannah_net_ble_watchlist_cb_t)(const char *json, int len);
void hannah_net_set_ble_watchlist_callback(hannah_net_ble_watchlist_cb_t cb);

/* Asset-Relevanzlisten-Callback: wird aufgerufen wenn hannah/satellite/<device>/assets/relevant
 * empfangen wird (retained, von Core gepflegt — #170). Payload: JSON-Array von Asset-IDs,
 * z.B. ["alarm_ring","timer_jingle"]. json/len zeigen direkt auf den MQTT-Payload-Puffer
 * (nur während des Callbacks gültig). */
typedef void (*hannah_net_asset_relevant_cb_t)(const char *json, int len);
void hannah_net_set_asset_relevant_callback(hannah_net_asset_relevant_cb_t cb);

/* Virtual-PTT-Callback: wird aufgerufen wenn hannah/satellite/<device>/ptt empfangen.
 * active=true: PTT gedrückt (Aufnahme starten), active=false: PTT losgelassen. */
typedef void (*hannah_net_virtual_ptt_cb_t)(bool active);
void hannah_net_set_virtual_ptt_callback(hannah_net_virtual_ptt_cb_t cb);

/* Sampling-Callback: wird aufgerufen wenn hannah/satellite/<device>/sampling empfangen.
 * enabled=true: Wakeword-Capture-Modus an (Speaker muten, LED_STATE_CAPTURE).
 * enabled=false: normaler Betrieb wiederhergestellt.
 * sample_type: "noise" (Dauerstrom) oder "hey_hannah" (nur bei PTT streamen). */
typedef void (*hannah_net_sampling_cb_t)(bool enabled, const char *sample_type);
void hannah_net_set_sampling_callback(hannah_net_sampling_cb_t cb);

/* Start-Listening-Callback: wird aufgerufen wenn Hannah einen `start_listening`-
 * UDP-Control-Befehl sendet (nach ask-TTS). */
typedef void (*hannah_net_start_listening_cb_t)(void);
void hannah_net_set_start_listening_callback(hannah_net_start_listening_cb_t cb);

/* Play-Asset-Callback: wird aufgerufen wenn hannah/satellite/<device>/play_asset empfangen.
 * Payload: {"asset_id":"timer_jingle"}
 * asset_id ist nur während des Callbacks gültig — bei asynchroner Nutzung kopieren. */
typedef void (*hannah_net_play_asset_cb_t)(const char *asset_id);
void hannah_net_set_play_asset_callback(hannah_net_play_asset_cb_t cb);

/* Wartet bis SNTP die Systemzeit synchronisiert hat oder timeout_ms abläuft.
 * Gibt true zurück wenn die Zeit erfolgreich synchronisiert wurde.
 * Im AP-Modus oder wenn SNTP noch nicht gestartet wurde: sofort false. */
bool hannah_net_wait_sntp(uint32_t timeout_ms);

/* Neustart-Diagnose (#165): Grund + persistenter Zähler, überlebt Neustarts (NVS).
 * Grund ist "watchdog" (reaktiver Netzwerk-Watchdog), "remote" (MQTT .../restart-
 * Kommando, #161 manuell oder #162 Scheduler), "ota" (nach erfolgreichem Update)
 * oder der rohe ESP-IDF-Reset-Grund (z.B. "Brownout", "Panic", "Power-On") —
 * esp_reset_reason() allein liefert für die ersten drei Fälle denselben Wert
 * (ESP_RST_SW), daher der Marker-Mechanismus unten. */
const char *hannah_net_get_restart_reason(void);
uint32_t    hannah_net_get_restart_count(void);

/* Markiert den Grund für den unmittelbar folgenden bewussten esp_restart()-Aufruf
 * persistent in NVS — überlebt den Neustart, damit hannah_net_init() beim nächsten
 * Boot zwischen "remote"/"ota" und dem reaktiven Watchdog unterscheiden kann. Vor
 * jedem bewussten esp_restart()-Aufruf aufrufen (außer dem reaktiven Watchdog
 * selbst — dessen Fehlen ist gerade das Unterscheidungsmerkmal). */
void hannah_net_mark_restart_source(const char *source);

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

/* audio_end-Kontrollnachricht senden — Aufnahme abgeschlossen. */
void hannah_net_send_audio_end(void);

/* Mute-Status */
bool hannah_net_is_muted(void);
void hannah_net_set_mute(bool muted);

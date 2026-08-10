#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    char     wifi_ssid[64];
    char     wifi_pass[64];
    char     device_id[32];
    char     mqtt_broker[64];
    uint16_t mqtt_port;
    char     mqtt_user[32];
    char     mqtt_pass[32];
    uint8_t  wakeword_threshold;  /* Erkennungsschwelle 0–100 (entspricht 0.00–1.00) */
    uint16_t vad_silence_ms;      /* VAD-Stille bis audio_end, Runtime-Override für CONFIG_HANNAH_VAD_SILENCE_MS */
    uint8_t  tdm_downmix_gain;    /* ADAU7118-TDM-Downmix-Verstärkung, Runtime-Override für CONFIG_HANNAH_TDM_DOWNMIX_GAIN (Refs #222) */
    char     ota_url[128];
    char     ota_token[128];
    char     ota_channel[32];
    char     asset_url[128];
    char     asset_token[128];
    char     asset_namespace[32]; /* Override für Asset-Manifest-Namespace — leer = Default "satellite" (Refs #187) */
    bool     tls_skip_verify;  /* Skip TLS certificate validation (insecure, for self-signed certs) */
    char     seed[64];         /* one-time pairing token written by WebFlash; cleared after "paired" ACK */
    char     nvs_token[128];   /* shared secret for the inbound POST /nvs endpoint (Refs #36) */
    char     syslog_host[64];  /* Syslog-UDP-Empfänger, IPv4-Literal (keine DNS-Auflösung im Log-Hot-Path) — leer = deaktiviert */
    uint16_t syslog_port;      /* Standard 514 */
} hannah_config_t;

/* Lädt Einstellungen aus NVS — sdkconfig-Werte als Fallback beim Erststart. */
void hannah_config_init(void);

/* True wenn wifi_ssid nicht leer ist. */
bool hannah_config_has_wifi(void);

/* Zeiger auf aktuell geladene Konfiguration (read-only). */
const hannah_config_t *hannah_config_get(void);

/* Speichert neue Konfiguration in NVS und aktualisiert den In-Memory-Cache. */
void hannah_config_save(const hannah_config_t *cfg);

/* Löscht den Pairing-Seed aus NVS und RAM (nach erfolgreichem Pairing aufrufen). */
void hannah_config_clear_seed(void);

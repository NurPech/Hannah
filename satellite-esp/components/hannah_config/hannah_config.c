#include "hannah_config.h"

#include <string.h>
#include "esp_log.h"
#include "esp_mac.h"
#include "nvs_flash.h"
#include "nvs.h"

static const char *TAG = "config";

#define NVS_NAMESPACE "hannah"

static hannah_config_t s_cfg;

/* ── NVS-Hilfsmakros ─────────────────────────────────────────────────────── */

#define NVS_STR(h, key, field, fallback)                                       \
    do {                                                                       \
        size_t _l = sizeof(s_cfg.field);                                      \
        if (nvs_get_str((h), (key), s_cfg.field, &_l) != ESP_OK)             \
            snprintf(s_cfg.field, sizeof(s_cfg.field), "%s", (fallback));     \
    } while (0)

/* ── Öffentliche API ─────────────────────────────────────────────────────── */

static void _load_device_id(void)
{
    uint8_t mac[6];
    esp_efuse_mac_get_default(mac);
    snprintf(s_cfg.device_id, sizeof(s_cfg.device_id),
             "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

void hannah_config_init(void)
{
    _load_device_id();

    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "NVS open fehlgeschlagen — nutze sdkconfig-Defaults");
        snprintf(s_cfg.wifi_ssid,   sizeof(s_cfg.wifi_ssid),   "%s", CONFIG_HANNAH_WIFI_SSID);
        snprintf(s_cfg.wifi_pass,   sizeof(s_cfg.wifi_pass),   "%s", CONFIG_HANNAH_WIFI_PASS);
        snprintf(s_cfg.mqtt_broker, sizeof(s_cfg.mqtt_broker), "%s", CONFIG_HANNAH_MQTT_BROKER);
        s_cfg.mqtt_port = CONFIG_HANNAH_MQTT_PORT;
        snprintf(s_cfg.mqtt_user,   sizeof(s_cfg.mqtt_user),   "%s", CONFIG_HANNAH_MQTT_USER);
        snprintf(s_cfg.mqtt_pass,   sizeof(s_cfg.mqtt_pass),   "%s", CONFIG_HANNAH_MQTT_PASS);
        snprintf(s_cfg.asset_url,   sizeof(s_cfg.asset_url),   "%s", CONFIG_HANNAH_ASSET_SERVER_URL);
        snprintf(s_cfg.asset_token, sizeof(s_cfg.asset_token), "%s", CONFIG_HANNAH_ASSET_SERVER_TOKEN);
        s_cfg.asset_namespace[0] = '\0';
        snprintf(s_cfg.nvs_token,   sizeof(s_cfg.nvs_token),   "%s", CONFIG_HANNAH_NVS_TOKEN);
        s_cfg.tls_skip_verify = false;
#ifdef CONFIG_HANNAH_WAKEWORD_THRESHOLD
        s_cfg.wakeword_threshold = CONFIG_HANNAH_WAKEWORD_THRESHOLD;
#else
        s_cfg.wakeword_threshold = 75;
#endif
        s_cfg.vad_silence_ms = CONFIG_HANNAH_VAD_SILENCE_MS;
#ifdef CONFIG_HANNAH_TDM_DOWNMIX_GAIN
        s_cfg.tdm_downmix_gain = CONFIG_HANNAH_TDM_DOWNMIX_GAIN;
#else
        s_cfg.tdm_downmix_gain = 16;
#endif
#ifdef CONFIG_HANNAH_SPK_OUTPUT_GAIN_PERCENT
        s_cfg.spk_output_gain_percent = CONFIG_HANNAH_SPK_OUTPUT_GAIN_PERCENT;
#else
        s_cfg.spk_output_gain_percent = 100;
#endif
#ifdef CONFIG_HANNAH_TDM_BEAM_DIRECTION_DEG
        s_cfg.tdm_beam_direction_deg = CONFIG_HANNAH_TDM_BEAM_DIRECTION_DEG;
#else
        s_cfg.tdm_beam_direction_deg = 180;
#endif
        s_cfg.tdm_debug_raw_slot = -1;
        s_cfg.syslog_host[0] = '\0';
        s_cfg.syslog_port    = 514;
        return;
    }

    NVS_STR(h, "wifi_ssid",   wifi_ssid,   CONFIG_HANNAH_WIFI_SSID);
    NVS_STR(h, "wifi_pass",   wifi_pass,   CONFIG_HANNAH_WIFI_PASS);
    NVS_STR(h, "mqtt_broker", mqtt_broker, CONFIG_HANNAH_MQTT_BROKER);
    NVS_STR(h, "mqtt_user",   mqtt_user,   CONFIG_HANNAH_MQTT_USER);
    NVS_STR(h, "mqtt_pass",   mqtt_pass,   CONFIG_HANNAH_MQTT_PASS);

    uint16_t port = CONFIG_HANNAH_MQTT_PORT;
    nvs_get_u16(h, "mqtt_port", &port);
    s_cfg.mqtt_port = port;

#ifdef CONFIG_HANNAH_WAKEWORD_THRESHOLD
    uint8_t thr = CONFIG_HANNAH_WAKEWORD_THRESHOLD;
#else
    uint8_t thr = 75;
#endif
    nvs_get_u8(h, "ww_threshold", &thr);
    s_cfg.wakeword_threshold = thr;

    uint16_t vad_ms = CONFIG_HANNAH_VAD_SILENCE_MS;
    nvs_get_u16(h, "vad_ms", &vad_ms);
    s_cfg.vad_silence_ms = vad_ms;

#ifdef CONFIG_HANNAH_TDM_DOWNMIX_GAIN
    uint8_t tdm_gain = CONFIG_HANNAH_TDM_DOWNMIX_GAIN;
#else
    uint8_t tdm_gain = 16;
#endif
    nvs_get_u8(h, "tdm_gain", &tdm_gain);
    s_cfg.tdm_downmix_gain = tdm_gain;

#ifdef CONFIG_HANNAH_SPK_OUTPUT_GAIN_PERCENT
    uint8_t spk_gain = CONFIG_HANNAH_SPK_OUTPUT_GAIN_PERCENT;
#else
    uint8_t spk_gain = 100;
#endif
    nvs_get_u8(h, "spk_gain", &spk_gain);
    s_cfg.spk_output_gain_percent = spk_gain;

#ifdef CONFIG_HANNAH_TDM_BEAM_DIRECTION_DEG
    uint16_t tdm_beam_dir = CONFIG_HANNAH_TDM_BEAM_DIRECTION_DEG;
#else
    uint16_t tdm_beam_dir = 180;
#endif
    nvs_get_u16(h, "tdm_beam_dir", &tdm_beam_dir);
    s_cfg.tdm_beam_direction_deg = tdm_beam_dir;

    int8_t tdm_debug_slot = -1;
    nvs_get_i8(h, "tdm_dbg_slot", &tdm_debug_slot);
    s_cfg.tdm_debug_raw_slot = tdm_debug_slot;

    NVS_STR(h, "ota_url",     ota_url,     CONFIG_HANNAH_OTA_URL);
    NVS_STR(h, "ota_channel", ota_channel, CONFIG_HANNAH_OTA_CHANNEL);
    NVS_STR(h, "ota_token",   ota_token,   CONFIG_HANNAH_OTA_TOKEN);
    NVS_STR(h, "asset_url",   asset_url,   CONFIG_HANNAH_ASSET_SERVER_URL);
    NVS_STR(h, "asset_token", asset_token, CONFIG_HANNAH_ASSET_SERVER_TOKEN);
    NVS_STR(h, "asset_namespace", asset_namespace, "");
    NVS_STR(h, "nvs_token",   nvs_token,   CONFIG_HANNAH_NVS_TOKEN);

    /* Einmalige Migration: alte Firmware hat "ota_channel" immer explizit in
     * NVS geschrieben (auch leer), wodurch NVS_STR's Fallback auf einen neuen
     * CONFIG_HANNAH_OTA_CHANNEL-Compile-Default nie mehr greift. Ohne diesen
     * Fix würde ein Channel-Umzug per neuem Firmware-Release auf bereits
     * ausgerollten Geräten wirkungslos bleiben. */
    uint8_t ota_ch_migrated = 0;
    nvs_get_u8(h, "ota_ch_migrated", &ota_ch_migrated);
    if (!ota_ch_migrated) {
        if (s_cfg.ota_channel[0] == '\0' && CONFIG_HANNAH_OTA_CHANNEL[0] != '\0') {
            snprintf(s_cfg.ota_channel, sizeof(s_cfg.ota_channel), "%s", CONFIG_HANNAH_OTA_CHANNEL);
            nvs_set_str(h, "ota_channel", s_cfg.ota_channel);
            ESP_LOGI(TAG, "OTA-Channel einmalig auf Compile-Default migriert: %s", s_cfg.ota_channel);
        }
        nvs_set_u8(h, "ota_ch_migrated", 1);
    }

    uint8_t tls_skip = 0;
    nvs_get_u8(h, "tls_skip", &tls_skip);
    s_cfg.tls_skip_verify = (bool)tls_skip;

    NVS_STR(h, "seed", seed, "");

    NVS_STR(h, "syslog_host", syslog_host, "");
    uint16_t syslog_port = 514;
    nvs_get_u16(h, "syslog_port", &syslog_port);
    s_cfg.syslog_port = syslog_port;

    nvs_commit(h);
    nvs_close(h);

    ESP_LOGI(TAG, "Config: device=%s wifi=%s mqtt=%s:%u (device_id from eFuse)",
             s_cfg.device_id,
             s_cfg.wifi_ssid[0] ? s_cfg.wifi_ssid : "(leer)",
             s_cfg.mqtt_broker, s_cfg.mqtt_port);
}

bool hannah_config_has_wifi(void)
{
    return s_cfg.wifi_ssid[0] != '\0';
}

const hannah_config_t *hannah_config_get(void)
{
    return &s_cfg;
}

void hannah_config_save(const hannah_config_t *cfg)
{
    s_cfg = *cfg;
    _load_device_id();  // device_id is always derived from eFuse, not persisted

    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "NVS open fehlgeschlagen — Einstellungen nicht gespeichert");
        return;
    }

    nvs_set_str(h, "wifi_ssid",   cfg->wifi_ssid);
    nvs_set_str(h, "wifi_pass",   cfg->wifi_pass);
    nvs_set_str(h, "mqtt_broker", cfg->mqtt_broker);
    nvs_set_u16(h, "mqtt_port",   cfg->mqtt_port);
    nvs_set_str(h, "mqtt_user",   cfg->mqtt_user);
    nvs_set_str(h, "mqtt_pass",   cfg->mqtt_pass);
    nvs_set_u8 (h, "ww_threshold", cfg->wakeword_threshold);
    nvs_set_u16(h, "vad_ms",       cfg->vad_silence_ms);
    nvs_set_u8 (h, "tdm_gain",     cfg->tdm_downmix_gain);
    nvs_set_u8 (h, "spk_gain",     cfg->spk_output_gain_percent);
    nvs_set_u16(h, "tdm_beam_dir", cfg->tdm_beam_direction_deg);
    nvs_set_i8 (h, "tdm_dbg_slot", cfg->tdm_debug_raw_slot);
    nvs_set_str(h, "ota_url",      cfg->ota_url);
    nvs_set_str(h, "ota_channel",  cfg->ota_channel);
    nvs_set_str(h, "ota_token",    cfg->ota_token);
    nvs_set_str(h, "asset_url",    cfg->asset_url);
    nvs_set_str(h, "asset_token",  cfg->asset_token);
    nvs_set_str(h, "asset_namespace", cfg->asset_namespace);
    nvs_set_str(h, "nvs_token",    cfg->nvs_token);
    nvs_set_u8 (h, "tls_skip",    (uint8_t)cfg->tls_skip_verify);
    nvs_set_str(h, "seed",        cfg->seed);
    nvs_set_str(h, "syslog_host", cfg->syslog_host);
    nvs_set_u16(h, "syslog_port", cfg->syslog_port);

    nvs_commit(h);
    nvs_close(h);

    ESP_LOGI(TAG, "Config gespeichert.");
}

void hannah_config_clear_seed(void)
{
    if (s_cfg.seed[0] == '\0') return;
    s_cfg.seed[0] = '\0';
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_str(h, "seed", "");
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "Pairing-Seed gelöscht.");
}

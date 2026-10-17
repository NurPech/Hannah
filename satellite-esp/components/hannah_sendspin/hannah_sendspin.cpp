// hannah_sendspin — Sendspin-Player für Music Assistant (Refs #440).
//
// Aus dem Spike (hannah#48) übernommen: unpaired (Gastzugriff), server-initiiert per mDNS, PCM mono
// 16 Bit 48 kHz, kein Decoder auf dem ESP. Die Musik geht über hannah_audio_music_write() auf den
// Lautsprecher-I2S, der Takt bleibt fest bei 48 kHz (CONFIG_HANNAH_SPK_SAMPLE_RATE).

#include "hannah_sendspin.h"
#include "sdkconfig.h"

#if CONFIG_HANNAH_SENDSPIN_ENABLED

#include <atomic>
#include <cstdio>
#include <cstring>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mdns.h"
#include "nvs.h"

#include "sendspin/client.h"
#include "sendspin/player_role.h"

// Die Satelliten-Header sind C-Header ohne extern "C".
extern "C" {
#include "hannah_audio.h"
#include "hannah_config.h"
}

using namespace sendspin;

static const char *TAG = "hannah_sendspin";
static constexpr uint16_t kSendspinPort = 8928;  // server-initiiert (Spec-Empfehlung)
static constexpr uint32_t kRate = 48000;

static_assert(CONFIG_HANNAH_SPK_SAMPLE_RATE == 48000,
              "Sendspin braucht CONFIG_HANNAH_SPK_SAMPLE_RATE=48000 (siehe sdkconfig.defaults)");

static std::string g_name;
static std::string g_hostname;
static std::atomic<float> g_sync_error{0.0f};
// Zuletzt an Music Assistant gemeldete bzw. von dort übernommene Musik-Lautstärke (-1 = noch nichts gemeldet).
// Ändert sich die Lautstärke an den Tasten (oder per MQTT), weicht sie hiervon ab und wird nach MA gemeldet.
static std::atomic<int> g_music_volume_sent{-1};

// --------------------------------------------------------------------------- NVS-Persistenz

// NVS-Schlüssel sind auf 15 Zeichen begrenzt, die Schlüssel der Bibliothek können länger sein
// -> auf einen kurzen Hash abbilden.
static std::string short_key(const std::string &key)
{
    uint32_t h = 2166136261u;
    for (unsigned char c : key) {
        h ^= c;
        h *= 16777619u;
    }
    char buf[16];
    snprintf(buf, sizeof(buf), "k%08x", static_cast<unsigned>(h));
    return buf;
}

class NvsPersistence : public SendspinPersistenceProvider {
public:
    std::optional<std::vector<uint8_t>> load_blob(const std::string &key) override
    {
        nvs_handle_t h;
        if (nvs_open("sendspin", NVS_READONLY, &h) != ESP_OK) return std::nullopt;
        std::string k = short_key(key);
        size_t len = 0;
        std::optional<std::vector<uint8_t>> out;
        if (nvs_get_blob(h, k.c_str(), nullptr, &len) == ESP_OK && len > 0) {
            std::vector<uint8_t> v(len);
            if (nvs_get_blob(h, k.c_str(), v.data(), &len) == ESP_OK) out = std::move(v);
        }
        nvs_close(h);
        return out;
    }

    bool save_blob(const std::string &key, const uint8_t *data, size_t len) override
    {
        nvs_handle_t h;
        if (nvs_open("sendspin", NVS_READWRITE, &h) != ESP_OK) return false;
        bool ok = nvs_set_blob(h, short_key(key).c_str(), data, len) == ESP_OK && nvs_commit(h) == ESP_OK;
        nvs_close(h);
        return ok;
    }
};

// --------------------------------------------------------------------------- Listener

struct PlayerListener : PlayerRoleListener {
    PlayerRole *player = nullptr;

    // Läuft auf dem Sync-Thread der Bibliothek.
    size_t on_audio_write(uint8_t *data, size_t length, uint32_t timeout_ms) override
    {
        uint32_t frames = 0;
        int64_t finish_us = 0;
        const size_t used = hannah_audio_music_write(data, length, timeout_ms, &frames, &finish_us);
        if (frames > 0) player->notify_audio_played(frames, finish_us);
        return used;
    }

    void on_stream_start() override
    {
        const auto &p = player->get_current_stream_params();
        if (!p.is_complete()) {
            ESP_LOGW(TAG, "Stream-Start mit unvollstaendigen Parametern");
            return;
        }
        ESP_LOGI(TAG, "Stream-Start: codec=%d %u Hz, %u ch, %u bit", static_cast<int>(*p.codec),
                 static_cast<unsigned>(*p.sample_rate), static_cast<unsigned>(*p.channels),
                 static_cast<unsigned>(*p.bit_depth));
        if (*p.sample_rate != kRate || *p.channels != 1 || *p.bit_depth != 16)
            ESP_LOGW(TAG, "Unerwartetes Format, der Lautsprecher laeuft fest mit 48 kHz mono 16 Bit");
    }

    void on_stream_end() override { ESP_LOGI(TAG, "Stream-Ende"); }

    void on_volume_changed(uint8_t v) override
    {
        ESP_LOGI(TAG, "Lautstaerke von MA: %u", static_cast<unsigned>(v));
        g_music_volume_sent = v;  // kommt von MA, nicht zurückmelden
        hannah_audio_set_music_volume(v);
    }

    void on_mute_changed(bool m) override
    {
        ESP_LOGI(TAG, "Mute von MA: %d", m ? 1 : 0);
        hannah_audio_music_set_muted(m);
    }

    void on_output_delay_changed(uint16_t ms) override
    {
        ESP_LOGI(TAG, "Output-Delay: %u ms", static_cast<unsigned>(ms));
    }
};

struct ClientListener : SendspinClientListener {
    void on_time_sync_updated(float error) override { g_sync_error = error; }
    void on_trust_changed(ConnectionTrust trust) override { ESP_LOGI(TAG, "Trust: %d", static_cast<int>(trust)); }
    void on_pairing_started(const std::string &) override { ESP_LOGI(TAG, "Pairing gestartet"); }
    void on_pairing_succeeded(const std::string &) override { ESP_LOGI(TAG, "Pairing erfolgreich"); }
    void on_group_update(const GroupUpdateObject &group) override
    {
        if (group.playback_state) ESP_LOGI(TAG, "Gruppe: playback_state=%d", static_cast<int>(*group.playback_state));
    }
};

struct Network : SendspinNetworkProvider {
    bool is_network_ready() override { return net_ready(); }

    static bool net_ready()
    {
        esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        esp_netif_ip_info_t ip = {};
        return netif && esp_netif_get_ip_info(netif, &ip) == ESP_OK && ip.ip.addr != 0;
    }
};

// --------------------------------------------------------------------------- mDNS

// Instanzname = Client-ID (so melden sich auch MA und andere Sendspin-Geräte an), der freundliche Name steht
// im TXT-Feld "name".
static void mdns_start(const std::string &client_id)
{
    ESP_ERROR_CHECK(mdns_init());
    ESP_ERROR_CHECK(mdns_hostname_set(g_hostname.c_str()));
    ESP_ERROR_CHECK(mdns_instance_name_set(g_name.c_str()));
    mdns_txt_item_t txt[] = {{"name", g_name.c_str()}, {"path", "/sendspin"}};
    ESP_ERROR_CHECK(mdns_service_add(client_id.c_str(), "_sendspin", "_tcp", kSendspinPort, txt, 2));
    ESP_LOGI(TAG, "mDNS: _sendspin._tcp, Instanz=%s, name=%s, Port %u, Hostname %s", client_id.c_str(),
             g_name.c_str(), kSendspinPort, g_hostname.c_str());
}

// --------------------------------------------------------------------------- Task

static void sendspin_task(void *)
{
    // Ohne Netzwerk gibt es nichts zu tun, die Bibliothek fragt es zwar selbst ab, mDNS braucht es aber vorher.
    while (!Network::net_ready()) vTaskDelay(pdMS_TO_TICKS(500));

    const char *device_id = hannah_config_get()->device_id;
    g_name = std::string("Hannah ") + device_id;
    g_hostname = std::string("hannah-") + device_id;

    SendspinClientConfig config;
    config.name = g_name;
    config.product_name = g_name;  // MA benennt den Universal Player danach
    config.manufacturer = "Hannah";
    config.software_version = esp_app_get_description()->version;
    config.server_port = kSendspinPort;
    // Internen RAM schonen (Refs #440): der Satellit hat im Normalbetrieb nur rund 48 KB internen Heap frei, der
    // Heap-Watchdog startet unter 20 KB neu. Die Task-Stacks der Bibliothek liegen deshalb im PSRAM. Der Protokoll-Task
    // schreibt nur beim allerersten Start einen Schluessel in den NVS (Flash-Zugriff mit Stack im PSRAM ist dort heikel).
    config.httpd_psram_stack = true;
    config.protocol_task_psram_stack = true;
    config.json_arena_size = 0;         // JSON-Dokumente komplett ins PSRAM
    config.server_max_connections = 2;  // weniger gleichzeitige Verbindungen (Sockets, RAM)
    static SendspinClient client(std::move(config));

    PlayerRoleConfig pc;
    pc.audio_formats = {
        {SendspinCodecFormat::PCM, 1, kRate, 16},
    };
    pc.audio_buffer_capacity = 1000000;
    pc.fixed_delay_us = 0;
    pc.initial_output_delay_ms = 0;
    pc.extra_startup_silence_ms = 50;
    pc.min_buffer_ms = 500;
    pc.psram_stack = true;  // Stack des Sync-/Decode-Tasks ins PSRAM (internen RAM schonen)
    PlayerRole &player = client.add_player(std::move(pc));

    static PlayerListener player_listener;
    player_listener.player = &player;
    player.set_listener(&player_listener);

    static ClientListener client_listener;
    static Network network;
    static NvsPersistence persistence;
    client.set_listener(&client_listener);
    client.set_network_provider(&network);
    client.set_persistence_provider(&persistence);
    client.set_unpaired_access_enabled(true);  // Gastzugriff ohne Pairing, trotzdem per Noise verschluesselt

    if (!client.start()) {
        ESP_LOGE(TAG, "SendspinClient.start() fehlgeschlagen");
        vTaskDelete(nullptr);
        return;
    }
    g_music_volume_sent = hannah_audio_get_music_volume();
    player.update_volume(static_cast<uint8_t>(g_music_volume_sent.load()));
    player.update_muted(false);
    ESP_LOGI(TAG, "Sendspin-Client gestartet");

    mdns_start(client.client_id());

    bool was_connected = false;
    bool ever_connected = false;
    const int64_t started_us = esp_timer_get_time();
    int64_t last_stats = 0;
#if CONFIG_HANNAH_SENDSPIN_STATS_PERIOD_S > 0
    hannah_audio_music_stats_t prev_music{};
#endif
    for (;;) {
        client.loop();
        const bool connected = client.is_connected();
        if (connected && !was_connected) {
            // MA verlangt direkt nach dem Verbinden einen ersten Status, sonst gilt der Client als nicht konform.
            ESP_LOGI(TAG, "Mit Server verbunden, sende Status");
            client.publish_state();
            g_music_volume_sent = hannah_audio_get_music_volume();
            player.update_volume(static_cast<uint8_t>(g_music_volume_sent.load()));
            player.update_muted(false);
        } else if (!connected && was_connected) {
            ESP_LOGW(TAG, "Verbindung zum Server verloren");
        }
        was_connected = connected;
        if (connected) ever_connected = true;

#if CONFIG_HANNAH_SENDSPIN_GIVE_UP_S > 0
        // Kein Music Assistant gefunden (Leonie, hannah#440): nach der Frist nicht mehr weitersuchen. Die Sperre gilt nur
        // zur Laufzeit, ein Neustart des Satelliten startet den Client wieder.
        if (!ever_connected &&
            esp_timer_get_time() - started_us > static_cast<int64_t>(CONFIG_HANNAH_SENDSPIN_GIVE_UP_S) * 1000000LL) {
            ESP_LOGW(TAG, "Kein Music Assistant in %d s gefunden, Sendspin ist bis zum naechsten Neustart aus",
                     CONFIG_HANNAH_SENDSPIN_GIVE_UP_S);
            client.stop();
            mdns_free();
            vTaskDelete(nullptr);
            return;
        }
#endif

        // Rückrichtung (hannah#446): wurde die Musik-Lautstärke am Satelliten geändert (Tasten, MQTT), MA informieren.
        if (connected) {
            const int music_volume = hannah_audio_get_music_volume();
            if (music_volume != g_music_volume_sent.load()) {
                g_music_volume_sent = music_volume;
                player.update_volume(static_cast<uint8_t>(music_volume));
                ESP_LOGI(TAG, "Musik-Lautstaerke an MA gemeldet: %d", music_volume);
            }
        }

#if CONFIG_HANNAH_SENDSPIN_STATS_PERIOD_S > 0
        const int64_t now = esp_timer_get_time();
        if (now - last_stats > static_cast<int64_t>(CONFIG_HANNAH_SENDSPIN_STATS_PERIOD_S) * 1000000LL) {
            last_stats = now;
            ESP_LOGI(TAG, "Stats: verbunden=%d zeit_sync=%d sync_err=%.2f | heap intern frei=%u (min %u) psram frei=%u | stack frei=%u",
                     connected ? 1 : 0, client.is_time_synced() ? 1 : 0, g_sync_error.load(),
                     static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                     static_cast<unsigned>(heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL)),
                     static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)),
                     static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr)));

            // Diagnose gegen "Stille trotz laufendem Stream" (hannah#440): Differenzen seit der letzten Zeile.
            // in_peak 0 = Music Assistant liefert Stille, in_peak > 0 und out_peak 0 = stumm oder Lautstärke 0
            // bei uns, alles 0 = es wird nichts geschrieben (fehl zählt Aufrufe ohne geschriebene Frames).
            hannah_audio_music_stats_t m;
            hannah_audio_music_get_stats(&m);
            ESP_LOGI(TAG, "Musik: aufrufe=%u fehl=%u frames=%u dma_frames=%u | in_peak=%d out_peak=%d | musik_vol=%d hannah_vol=%d stumm=%d duck=%d%%",
                     static_cast<unsigned>(m.calls - prev_music.calls), static_cast<unsigned>(m.fails - prev_music.fails),
                     static_cast<unsigned>(m.frames - prev_music.frames),
                     static_cast<unsigned>(m.frames_sent - prev_music.frames_sent),
                     static_cast<int>(m.in_peak), static_cast<int>(m.out_peak), m.music_volume, m.volume,
                     m.muted ? 1 : 0, m.duck_percent);
            prev_music = m;
        }
#endif
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

extern "C" void hannah_sendspin_start(void)
{
    // Interner Stack (der Task ruft den NVS-Persistenz-Provider der Bibliothek auf, Flash-Zugriffe brauchen ihn im internen
    // RAM). Kleiner als die 8 KB des Spikes; die Statistik meldet den freien Stack ("stack frei").
    if (xTaskCreate(sendspin_task, "sendspin", 6144, nullptr, 5, nullptr) != pdPASS)
        ESP_LOGE(TAG, "Task konnte nicht gestartet werden");
}

#else  // !CONFIG_HANNAH_SENDSPIN_ENABLED

extern "C" void hannah_sendspin_start(void) {}

#endif

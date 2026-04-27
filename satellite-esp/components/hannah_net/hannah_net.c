/**
 * hannah_net — WiFi, MQTT-Discovery, UDP-Audio-Stream, TTS-Empfang
 *
 * UDP-Protokoll (1-Byte Type-Prefix):
 *   0x01 + JSON  = Control  (beide Richtungen)
 *   0x02 + PCM   = Audio    (Satellit → Proxy)
 *   0x03 + PCM   = TTS      (Proxy → Satellit)
 */

#include "hannah_net.h"

#include <string.h>
#include <stdio.h>

#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "mqtt_client.h"
#include "cJSON.h"

static const char *TAG = "hannah_net";

/* UDP Paket-Typen */
#define TYPE_CONTROL 0x01
#define TYPE_AUDIO   0x02
#define TYPE_TTS     0x03

/* Maximale UDP-Paketgröße */
#define UDP_RX_BUF_SIZE 65536

/* WiFi Event-Bits */
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

/* ------------------------------------------------------------------ */
/* Zustand                                                              */

static volatile bool s_muted     = false;
static int           s_udp_sock  = -1;
static struct sockaddr_in s_proxy_addr;
static bool          s_proxy_ready = false;
static int           s_wifi_retry  = 0;

static EventGroupHandle_t   s_wifi_event_group;
static esp_mqtt_client_handle_t s_mqtt_client = NULL;

static hannah_net_status_cb_t   s_status_cb   = NULL;
static hannah_net_tts_cb_t      s_tts_cb      = NULL;
static hannah_net_tts_end_cb_t  s_tts_end_cb  = NULL;
static hannah_net_playback_cb_t s_playback_cb = NULL;

/* ------------------------------------------------------------------ */
/* Hilfsfunktionen                                                      */

static void send_control(const char *json_str)
{
    if (s_udp_sock < 0 || !s_proxy_ready) return;
    size_t json_len = strlen(json_str);
    size_t pkt_len  = 1 + json_len;
    uint8_t *pkt    = malloc(pkt_len);
    if (!pkt) return;
    pkt[0] = TYPE_CONTROL;
    memcpy(pkt + 1, json_str, json_len);
    sendto(s_udp_sock, pkt, pkt_len, 0,
           (struct sockaddr *)&s_proxy_addr, sizeof(s_proxy_addr));
    free(pkt);
}

static void send_register(void)
{
    char msg[256];
    snprintf(msg, sizeof(msg),
             "{\"type\":\"register\",\"device\":\"%s\","
             "\"room\":\"%s\",\"listen_port\":%d}",
             CONFIG_HANNAH_DEVICE_ID,
             CONFIG_HANNAH_ROOM_NAME,
             CONFIG_HANNAH_UDP_LISTEN_PORT);
    send_control(msg);
    ESP_LOGI(TAG, "Register gesendet: device=%s room=%s listen_port=%d",
             CONFIG_HANNAH_DEVICE_ID, CONFIG_HANNAH_ROOM_NAME,
             CONFIG_HANNAH_UDP_LISTEN_PORT);
}

/* ------------------------------------------------------------------ */
/* UDP: Socket aufbauen + registrieren                                  */

static void udp_connect(const char *host, int port)
{
    /* Alten Socket schließen falls vorhanden */
    if (s_udp_sock >= 0) {
        close(s_udp_sock);
        s_udp_sock = -1;
        s_proxy_ready = false;
    }

    /* Proxy-Adresse speichern */
    memset(&s_proxy_addr, 0, sizeof(s_proxy_addr));
    s_proxy_addr.sin_family = AF_INET;
    s_proxy_addr.sin_port   = htons(port);
    if (inet_aton(host, &s_proxy_addr.sin_addr) == 0) {
        ESP_LOGE(TAG, "Ungültige Proxy-IP: %s", host);
        return;
    }

    /* Socket erstellen */
    s_udp_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s_udp_sock < 0) {
        ESP_LOGE(TAG, "socket() fehlgeschlagen: errno=%d", errno);
        return;
    }

    /* Auf lokalem Port binden (für TTS-Empfang) */
    struct sockaddr_in local = {
        .sin_family      = AF_INET,
        .sin_port        = htons(CONFIG_HANNAH_UDP_LISTEN_PORT),
        .sin_addr.s_addr = INADDR_ANY,
    };
    if (bind(s_udp_sock, (struct sockaddr *)&local, sizeof(local)) < 0) {
        ESP_LOGE(TAG, "bind(%d) fehlgeschlagen: errno=%d",
                 CONFIG_HANNAH_UDP_LISTEN_PORT, errno);
        close(s_udp_sock);
        s_udp_sock = -1;
        return;
    }

    /* Send-Timeout setzen */
    struct timeval tv = {
        .tv_sec  = 0,
        .tv_usec = CONFIG_HANNAH_UDP_TIMEOUT_MS * 1000,
    };
    setsockopt(s_udp_sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    s_proxy_ready = true;
    ESP_LOGI(TAG, "UDP-Socket bereit → Proxy %s:%d (listen :%d)",
             host, port, CONFIG_HANNAH_UDP_LISTEN_PORT);

    send_register();
}

/* ------------------------------------------------------------------ */
/* UDP: Empfangs-Task                                                   */

static void udp_receive_task(void *arg)
{
    uint8_t *buf = malloc(UDP_RX_BUF_SIZE);
    if (!buf) {
        ESP_LOGE(TAG, "udp_receive_task: kein Speicher");
        vTaskDelete(NULL);
        return;
    }

    while (1) {
        if (s_udp_sock < 0) {
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }

        int len = recv(s_udp_sock, buf, UDP_RX_BUF_SIZE, 0);
        if (len < 2) continue;

        uint8_t type   = buf[0];
        uint8_t *payload = buf + 1;
        size_t   plen    = len - 1;

        if (type == TYPE_TTS) {
            /* TTS-PCM-Chunk → an Audio-Komponente weiterleiten */
            if (s_tts_cb) s_tts_cb(payload, plen);

        } else if (type == TYPE_CONTROL) {
            /* JSON-Kontrollnachricht parsen */
            buf[len] = '\0';
            cJSON *root = cJSON_ParseWithLength((char *)payload, plen);
            if (!root) continue;

            const cJSON *jtype = cJSON_GetObjectItemCaseSensitive(root, "type");
            if (!cJSON_IsString(jtype)) { cJSON_Delete(root); continue; }

            if (strcmp(jtype->valuestring, "status") == 0) {
                const cJSON *jstate = cJSON_GetObjectItemCaseSensitive(root, "state");
                if (cJSON_IsString(jstate) && s_status_cb) {
                    s_status_cb(jstate->valuestring);
                }

            } else if (strcmp(jtype->valuestring, "tts_end") == 0) {
                int sample_rate = 16000;
                const cJSON *jsr = cJSON_GetObjectItemCaseSensitive(root, "sample_rate");
                if (cJSON_IsNumber(jsr)) sample_rate = (int)jsr->valuedouble;
                if (s_tts_end_cb) s_tts_end_cb(sample_rate);

            } else if (strcmp(jtype->valuestring, "stop")   == 0 ||
                       strcmp(jtype->valuestring, "pause")  == 0 ||
                       strcmp(jtype->valuestring, "resume") == 0) {
                if (s_playback_cb) s_playback_cb(jtype->valuestring);

            } else if (strcmp(jtype->valuestring, "registered") == 0) {
                ESP_LOGI(TAG, "Proxy-Registrierung bestätigt.");

            } else if (strcmp(jtype->valuestring, "heartbeat_ack") == 0) {
                ESP_LOGD(TAG, "Heartbeat ACK empfangen.");

            } else if (strcmp(jtype->valuestring, "reregister") == 0) {
                ESP_LOGW(TAG, "Core fordert Re-Registrierung (nach Neustart?)");
                send_register();
            }

            cJSON_Delete(root);
        }
    }
    /* Wird nie erreicht */
    free(buf);
    vTaskDelete(NULL);
}

/* ------------------------------------------------------------------ */
/* Heartbeat-Task                                                        */

static void heartbeat_task(void *arg)
{
    char msg[128];
    snprintf(msg, sizeof(msg),
             "{\"type\":\"heartbeat\",\"device\":\"%s\"}",
             CONFIG_HANNAH_DEVICE_ID);

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(CONFIG_HANNAH_HEARTBEAT_INTERVAL_S * 1000));
        if (s_proxy_ready) {
            send_control(msg);
            ESP_LOGD(TAG, "Heartbeat gesendet.");
        }
    }
}

/* ------------------------------------------------------------------ */
/* MQTT                                                                  */

static void on_mqtt_event(void *handler_arg, esp_event_base_t base,
                          int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)event_data;

    switch (event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT verbunden.");
        /* hannah/server: IP:Port des Proxys (retained) */
        esp_mqtt_client_subscribe(s_mqtt_client, "hannah/server", 0);
        /* Mute-Steuerung per MQTT */
        {
            char topic[128];
            snprintf(topic, sizeof(topic),
                     "hannah/satellite/%s/mute", CONFIG_HANNAH_DEVICE_ID);
            esp_mqtt_client_subscribe(s_mqtt_client, topic, 0);
        }
        break;

    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT getrennt — automatischer Reconnect.");
        break;

    case MQTT_EVENT_DATA: {
        char topic[128] = {0};
        int  tlen = event->topic_len < (int)sizeof(topic) - 1
                    ? event->topic_len
                    : (int)sizeof(topic) - 1;
        memcpy(topic, event->topic, tlen);

        char data[256] = {0};
        int  dlen = event->data_len < (int)sizeof(data) - 1
                    ? event->data_len
                    : (int)sizeof(data) - 1;
        memcpy(data, event->data, dlen);

        if (strcmp(topic, "hannah/server") == 0) {
            /* Payload: "IP:Port" */
            char host[64] = {0};
            int  port     = 0;
            if (sscanf(data, "%63[^:]:%d", host, &port) == 2 && port > 0) {
                ESP_LOGI(TAG, "Discovery: Proxy %s:%d", host, port);
                udp_connect(host, port);
            } else {
                ESP_LOGW(TAG, "Ungültige Discovery-Payload: '%s'", data);
            }

        } else if (strstr(topic, "/mute") != NULL) {
            bool mute = (data[0] == '1');
            hannah_net_set_mute(mute);
        }
        break;
    }

    case MQTT_EVENT_ERROR:
        ESP_LOGW(TAG, "MQTT-Fehler.");
        break;

    default:
        break;
    }
}

static void mqtt_init(void)
{
    char broker_uri[128];
    snprintf(broker_uri, sizeof(broker_uri),
             "mqtt://%s:%d", CONFIG_HANNAH_MQTT_BROKER, CONFIG_HANNAH_MQTT_PORT);

    esp_mqtt_client_config_t cfg = {
        .broker.address.uri                     = broker_uri,
        .credentials.username                   = CONFIG_HANNAH_MQTT_USER,
        .credentials.authentication.password    = CONFIG_HANNAH_MQTT_PASS,
        .credentials.client_id                  = CONFIG_HANNAH_DEVICE_ID,
        .network.reconnect_timeout_ms           = 5000,
    };

    s_mqtt_client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(s_mqtt_client, ESP_EVENT_ANY_ID,
                                   on_mqtt_event, NULL);
    esp_mqtt_client_start(s_mqtt_client);
    ESP_LOGI(TAG, "MQTT-Client gestartet → %s", broker_uri);
}

/* ------------------------------------------------------------------ */
/* WiFi                                                                  */

static void on_wifi_event(void *arg, esp_event_base_t base,
                          int32_t event_id, void *event_data)
{
    if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();

    } else if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_wifi_retry < CONFIG_HANNAH_WIFI_MAX_RETRY) {
            s_wifi_retry++;
            ESP_LOGW(TAG, "WiFi getrennt — Versuch %d/%d",
                     s_wifi_retry, CONFIG_HANNAH_WIFI_MAX_RETRY);
            esp_wifi_connect();
        } else {
            ESP_LOGE(TAG, "WiFi: maximale Versuche erreicht — Neustart.");
            esp_restart();
        }
        xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);

    } else if (base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "IP: " IPSTR, IP2STR(&ev->ip_info.ip));
        s_wifi_retry = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        mqtt_init();
    }
}

static void wifi_init_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi_event, NULL, NULL));

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid     = CONFIG_HANNAH_WIFI_SSID,
            .password = CONFIG_HANNAH_WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "WiFi STA gestartet → SSID: %s", CONFIG_HANNAH_WIFI_SSID);
}

/* ------------------------------------------------------------------ */
/* Öffentliche API                                                       */

void hannah_net_set_status_callback(hannah_net_status_cb_t cb)      { s_status_cb   = cb; }
void hannah_net_set_tts_callback(hannah_net_tts_cb_t cb)            { s_tts_cb      = cb; }
void hannah_net_set_tts_end_callback(hannah_net_tts_end_cb_t cb)    { s_tts_end_cb  = cb; }
void hannah_net_set_playback_callback(hannah_net_playback_cb_t cb)  { s_playback_cb = cb; }

void hannah_net_init(void)
{
    wifi_init_sta();

    /* UDP-Receive-Task (wartet bis Socket bereit ist) */
    xTaskCreate(udp_receive_task, "udp_rx", 8192, NULL, 6, NULL);
    /* Heartbeat-Task */
    xTaskCreate(heartbeat_task, "heartbeat", 2048, NULL, 3, NULL);

    ESP_LOGI(TAG, "hannah_net initialisiert.");
}

void hannah_net_send_audio(const uint8_t *pcm, size_t len)
{
    if (s_muted || s_udp_sock < 0 || !s_proxy_ready) return;

    /* Audio-Pakete in Chunks ≤ 60 KB senden */
    size_t offset = 0;
    while (offset < len) {
        size_t chunk = len - offset;
        if (chunk > 60000) chunk = 60000;

        size_t pkt_len = 1 + chunk;
        uint8_t *pkt   = malloc(pkt_len);
        if (!pkt) return;
        pkt[0] = TYPE_AUDIO;
        memcpy(pkt + 1, pcm + offset, chunk);
        sendto(s_udp_sock, pkt, pkt_len, 0,
               (struct sockaddr *)&s_proxy_addr, sizeof(s_proxy_addr));
        free(pkt);
        offset += chunk;
    }
}

void hannah_net_send_audio_end(void)
{
    char msg[128];
    snprintf(msg, sizeof(msg),
             "{\"type\":\"audio_end\",\"device\":\"%s\"}",
             CONFIG_HANNAH_DEVICE_ID);
    send_control(msg);
    ESP_LOGD(TAG, "audio_end gesendet.");
}

bool hannah_net_is_muted(void) { return s_muted; }

void hannah_net_set_mute(bool muted)
{
    s_muted = muted;
    ESP_LOGI(TAG, "Mute: %s", muted ? "AN" : "AUS");
}

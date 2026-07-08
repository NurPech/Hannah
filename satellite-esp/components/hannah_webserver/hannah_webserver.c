#include "hannah_webserver.h"

#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <math.h>

#include "esp_log.h"
#include "esp_system.h"
#include "esp_mac.h"
#include "esp_ota_ops.h"
#include "esp_app_format.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/portmacro.h"
#include "esp_http_server.h"
#include "cJSON.h"

#include "hannah_config.h"
#include "hannah_net.h"
#include "hannah_sensors.h"

static const char *TAG = "webserver";
static httpd_handle_t s_server = NULL;

/* ── HTML-Bausteine ─────────────────────────────────────────────────────── */

static const char S_HEAD[] =
    "<!DOCTYPE html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    "<title>Hannah</title><style>"
    "body{font-family:sans-serif;max-width:640px;margin:2em auto;padding:0 1em;color:#222}"
    "nav{margin:.8em 0 1.2em}nav a{margin-right:1.2em;color:#0066cc;text-decoration:none}"
    "h1{margin-bottom:.2em}h3{margin:1em 0 .3em;color:#444}"
    "table{border-collapse:collapse;width:100%}"
    "td{padding:.3em .5em}td:first-child{color:#888;white-space:nowrap}"
    "label{display:block;color:#555;font-size:.9em;margin-top:.6em}"
    "input:not([type=checkbox]):not([type=file]){display:block;width:100%;"
      "box-sizing:border-box;padding:.4em;border:1px solid #ccc;border-radius:3px}"
    "input[type=checkbox]{margin-right:.4em}"
    ".btn{display:inline-block;background:#0066cc;color:#fff;border:0;"
      "padding:.5em 1.2em;border-radius:3px;cursor:pointer;font-size:1em;margin-top:1em}"
    ".btn-red{background:#c00}"
    "#status{margin-top:1em;padding:.5em;background:#f0f0f0;border-radius:3px;display:none}"
    "</style></head><body>"
    "<nav><a href=/>Status</a><a href=/settings>Einstellungen</a>"
    "<a href=/ota>Firmware</a><a href=/log>Log</a></nav>";

/* ── Log-Ringpuffer ──────────────────────────────────────────────────────── */

#define LOG_BUF_SIZE (8 * 1024)

static char             s_log_buf[LOG_BUF_SIZE];
static volatile size_t  s_log_wp   = 0;
static volatile bool    s_log_full = false;
static portMUX_TYPE     s_log_mux  = portMUX_INITIALIZER_UNLOCKED;
static vprintf_like_t   s_orig_vprintf = NULL;

static int log_capture(const char *fmt, va_list args)
{
    /* Erst Original-Handler (UART), dann in Ringpuffer */
    int ret = s_orig_vprintf ? s_orig_vprintf(fmt, args) : 0;

    va_list copy;
    va_copy(copy, args);
    char line[256];
    int n = vsnprintf(line, sizeof(line), fmt, copy);
    va_end(copy);

    if (n > 0) {
        int actual = n < (int)sizeof(line) ? n : (int)sizeof(line) - 1;
        portENTER_CRITICAL(&s_log_mux);
        for (int i = 0; i < actual; i++) {
            s_log_buf[s_log_wp] = line[i];
            if (++s_log_wp >= LOG_BUF_SIZE) { s_log_wp = 0; s_log_full = true; }
        }
        portEXIT_CRITICAL(&s_log_mux);
    }
    return ret;
}

/* Kopiert Ringpuffer in der richtigen Reihenfolge nach out, gibt Länge zurück. */
static size_t log_snapshot(char *out)
{
    size_t wp   = s_log_wp;
    bool   full = s_log_full;
    if (!full) {
        memcpy(out, s_log_buf, wp);
        return wp;
    }
    size_t tail = LOG_BUF_SIZE - wp;
    memcpy(out,        s_log_buf + wp, tail);
    memcpy(out + tail, s_log_buf,      wp);
    return LOG_BUF_SIZE;
}

/* Auf der von hannah_asset gemounteten SPIFFS-Partition — Log-Datei liegt also
 * nur mit, sobald deren Mount steht (siehe hannah_asset_init(), läuft früh im
 * Boot, aber nach hannah_net_init()). Eigener Name statt "<asset_id>.wav",
 * damit der Asset-Sync sie nicht anfasst. */
#define CRASH_LOG_PATH "/assets/_hannah_last_log.txt"

/* Als esp_restart()-Shutdown-Handler registriert (siehe hannah_webserver_start()) —
 * läuft bei jedem geordneten Neustart, u.a. dem Netzwerk-Watchdog in
 * hannah_net.c. Rettet den sonst rein flüchtigen RAM-Ringpuffer über den
 * Neustart hinweg, abrufbar danach über GET /log/last. */
static void persist_log_to_flash(void)
{
    char *buf = malloc(LOG_BUF_SIZE);
    if (!buf) return;
    size_t len = log_snapshot(buf);

    FILE *f = fopen(CRASH_LOG_PATH, "wb");
    if (!f) {
        ESP_LOGW(TAG, "Log-Sicherung vor Neustart fehlgeschlagen (SPIFFS nicht gemountet?)");
        free(buf);
        return;
    }
    fwrite(buf, 1, len, f);
    fclose(f);
    free(buf);
    ESP_LOGI(TAG, "Log vor Neustart gesichert (%u Bytes) -> %s", (unsigned)len, CRASH_LOG_PATH);
}

static const char S_FOOT[] = "</body></html>";

/* ── URL-Decode + Form-Parser ───────────────────────────────────────────── */

static void url_decode(char *out, const char *src, size_t out_len)
{
    char *d = out;
    size_t rem = out_len - 1;
    while (*src && rem > 0) {
        if (src[0] == '%' && src[1] && src[2]) {
            char h[3] = {src[1], src[2], 0};
            *d++ = (char)strtol(h, NULL, 16);
            src += 3;
        } else {
            *d++ = (*src == '+') ? ' ' : *src;
            src++;
        }
        rem--;
    }
    *d = '\0';
}

/* Sucht key= im URL-encoded body. Anker: Anfang oder '&' davor. */
static bool form_get(const char *body, const char *key, char *out, size_t out_len)
{
    size_t klen = strlen(key);
    const char *p = body;
    while ((p = strstr(p, key)) != NULL) {
        bool at_start = (p == body || *(p - 1) == '&');
        if (at_start && p[klen] == '=') {
            p += klen + 1;
            const char *end = strchr(p, '&');
            size_t vlen = end ? (size_t)(end - p) : strlen(p);
            char tmp[256] = {0};
            if (vlen >= sizeof(tmp)) vlen = sizeof(tmp) - 1;
            memcpy(tmp, p, vlen);
            url_decode(out, tmp, out_len);
            return true;
        }
        p++;
    }
    out[0] = '\0';
    return false;
}

/* ── Handler: / (Status) ─────────────────────────────────────────────────── */

static esp_err_t status_handler(httpd_req_t *req)
{
    char ip[24];
    hannah_net_get_ip_str(ip, sizeof(ip));

    /* Serial aus eFuse-MAC erzeugen (gleiche Logik wie in hannah_net) */
    uint8_t mac[6];
    esp_efuse_mac_get_default(mac);
    char serial[13];
    snprintf(serial, sizeof(serial), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    hannah_sensor_data_t sens = {0};
    bool has_sens = hannah_sensors_get(&sens);

    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_app_desc_t  *app     = esp_app_get_description();
    uint32_t uptime = xTaskGetTickCount() * portTICK_PERIOD_MS / 1000;

    char *buf = malloc(3072);
    if (!buf) return ESP_ERR_NO_MEM;

    int n = snprintf(buf, 3072,
        "%s<h1>Hannah Satellite</h1>"
        "<table>"
        "<tr><td>Serial</td><td><b>%s</b></td></tr>"
        "<tr><td>IP</td><td>%s%s</td></tr>"
        "<tr><td>Partition</td><td>%s</td></tr>"
        "<tr><td>Firmware</td><td>%s (%s %s)</td></tr>"
        "<tr><td>Uptime</td><td>%lu s</td></tr>",
        S_HEAD,
        serial, ip,
        hannah_net_is_ap_mode() ? " <b style=color:orange>(Setup-Modus)</b>" : "",
        running ? running->label : "?",
        app ? app->version : "?",
        app ? app->date : "", app ? app->time : "",
        (unsigned long)uptime);

    if (has_sens) {
        n += snprintf(buf + n, 3072 - n,
            "<tr><td>Temperatur</td><td>%.1f °C</td></tr>"
            "<tr><td>Luftfeuchte</td><td>%.1f %%</td></tr>"
            "<tr><td>Luftdruck</td><td>%.1f hPa</td></tr>",
            sens.temperature, sens.humidity, sens.pressure);
        if (!isnan(sens.gas_resistance))
            n += snprintf(buf + n, 3072 - n,
                "<tr><td>Luftqualität</td><td>%.0f Ω</td></tr>",
                sens.gas_resistance);
    }

    n += snprintf(buf + n, 3072 - n, "</table>%s", S_FOOT);

    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, buf, n);
    free(buf);
    return ESP_OK;
}

/* ── Handler: GET /settings ─────────────────────────────────────────────── */

static esp_err_t settings_get_handler(httpd_req_t *req)
{
    const hannah_config_t *cfg = hannah_config_get();
    char *buf = malloc(6144);
    if (!buf) return ESP_ERR_NO_MEM;

    int n = snprintf(buf, 6144,
        "%s<h1>Einstellungen</h1>"
        "<form method=post action=/settings>"
        "<h3>WiFi</h3>"
        "<label>SSID"
        "<div style='display:flex;gap:.4em'>"
        "<input id=si name=ssid value='%s' style='flex:1'>"
        "<button type=button class=btn id=sb onclick=scanWifi() "
          "style='margin:0;white-space:nowrap;padding:.4em .8em'>Suchen</button>"
        "</div></label>"
        "<div id=sl style='display:none;border:1px solid #ccc;border-radius:3px;"
          "max-height:180px;overflow-y:auto;margin-bottom:.5em'></div>"
        "<label>Passwort<input type=password name=pass placeholder='(unverändert lassen)'></label>"
        "<h3>MQTT</h3>"
        "<label>Broker<input name=mqtt_broker value='%s'></label>"
        "<label>Port<input name=mqtt_port value='%u'></label>"
        "<label>Benutzer<input name=mqtt_user value='%s'></label>"
        "<label>Passwort<input type=password name=mqtt_pass placeholder='(unverändert lassen)'></label>"
        "<h3>Features</h3>"
        "<label>Erkennungsschwelle: <b id=tv>%d%%</b>"
        "<input type=range name=ww_threshold min=0 max=100 value=%d "
          "oninput=\"document.getElementById('tv').textContent=this.value+'%%'\"></label>"
        "<label>VAD-Stille (ms)<input type=number name=vad_ms min=200 max=10000 step=100 value=%u></label>"
        "<h3>Firmware</h3>"
        "<label>Update-Server URL<input name=ota_url value='%s'></label>"
        "<label>Update-Channel<input name=ota_channel value='%s' placeholder='(leer = stable)'></label>"
        "<label>Update-Server Token<input type=password name=ota_token placeholder='(unverändert lassen)'></label>"
        "<h3>Asset Server</h3>"
        "<label>URL<input name=asset_url value='%s'></label>"
        "<label>Token<input type=password name=asset_token placeholder='(unverändert lassen)'></label>"
        "<h3>NVS Update API</h3>"
        "<label>Bearer-Token für POST /nvs<input type=password name=nvs_token "
          "placeholder='(unverändert lassen, leer = deaktiviert)'></label>"
        "<h3>Sicherheit</h3>"
        "<label><input type=checkbox name=tls_skip_verify value=1%s> "
          "TLS-Zertifikatsprüfung deaktivieren <span style='color:#c00'>(unsicher)</span></label>"
        "<br><button type=submit class=btn>Speichern &amp; Neustart</button>"
        "</form>"
        "<script>"
        "async function scanWifi(){"
          "const sb=document.getElementById('sb');"
          "sb.textContent='Suche...';sb.disabled=true;"
          "const sl=document.getElementById('sl');"
          "sl.style.display='none';sl.innerHTML='';"
          "try{"
            "const r=await fetch('/wifi/scan');"
            "const nets=await r.json();"
            "nets.sort((a,b)=>b.rssi-a.rssi);"
            "nets.forEach(n=>{"
              "const d=document.createElement('div');"
              "d.style='padding:.4em .6em;cursor:pointer;border-bottom:1px solid #eee';"
              "d.onmouseover=()=>d.style.background='#f5f5f5';"
              "d.onmouseout=()=>d.style.background='';"
              "d.textContent=(n.auth?'[S] ':'[O] ')+n.ssid+' ('+n.rssi+' dBm)';"
              "d.onclick=()=>{"
                "document.getElementById('si').value=n.ssid;"
                "sl.style.display='none';"
              "};"
              "sl.appendChild(d);"
            "});"
            "sl.style.display=nets.length?'block':'none';"
          "}catch(e){alert('Scan fehlgeschlagen: '+e);}"
          "sb.textContent='Suchen';sb.disabled=false;"
        "}"
        "</script>%s",
        S_HEAD,
        cfg->wifi_ssid,
        cfg->mqtt_broker, cfg->mqtt_port, cfg->mqtt_user,
        cfg->wakeword_threshold, cfg->wakeword_threshold,
        cfg->vad_silence_ms,
        cfg->ota_url,
        cfg->ota_channel,
        cfg->asset_url,
        cfg->tls_skip_verify ? " checked" : "",
        S_FOOT);

    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, buf, n);
    free(buf);
    return ESP_OK;
}

/* ── Handler: POST /settings ─────────────────────────────────────────────── */

static esp_err_t settings_post_handler(httpd_req_t *req)
{
    if (req->content_len > 2048) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Body too large");
        return ESP_FAIL;
    }

    char *body = malloc(req->content_len + 1);
    if (!body) return ESP_ERR_NO_MEM;

    int got = httpd_req_recv(req, body, req->content_len);
    if (got <= 0) { free(body); return ESP_FAIL; }
    body[got] = '\0';

    hannah_config_t new_cfg = *hannah_config_get();

    /* Felder auslesen — leere Passwörter = unverändert */
    form_get(body, "ssid",        new_cfg.wifi_ssid,   sizeof(new_cfg.wifi_ssid));
    form_get(body, "device_id",   new_cfg.device_id,   sizeof(new_cfg.device_id));
    form_get(body, "mqtt_broker", new_cfg.mqtt_broker, sizeof(new_cfg.mqtt_broker));
    form_get(body, "mqtt_user",   new_cfg.mqtt_user,   sizeof(new_cfg.mqtt_user));

    char tmp[64] = {0};
    if (form_get(body, "pass",      tmp, sizeof(tmp)) && tmp[0])
        strncpy(new_cfg.wifi_pass, tmp, sizeof(new_cfg.wifi_pass) - 1);
    if (form_get(body, "mqtt_pass", tmp, sizeof(tmp)) && tmp[0])
        strncpy(new_cfg.mqtt_pass, tmp, sizeof(new_cfg.mqtt_pass) - 1);

    char port_str[8] = {0};
    if (form_get(body, "mqtt_port", port_str, sizeof(port_str))) {
        int p = atoi(port_str);
        if (p > 0 && p < 65536) new_cfg.mqtt_port = (uint16_t)p;
    }

    form_get(body, "ota_url",     new_cfg.ota_url,     sizeof(new_cfg.ota_url));
    form_get(body, "ota_channel", new_cfg.ota_channel, sizeof(new_cfg.ota_channel));
    form_get(body, "asset_url",   new_cfg.asset_url,   sizeof(new_cfg.asset_url));

    char tok[128] = {0};
    if (form_get(body, "ota_token",   tok, sizeof(tok)) && tok[0])
        strncpy(new_cfg.ota_token,   tok, sizeof(new_cfg.ota_token)   - 1);
    memset(tok, 0, sizeof(tok));
    if (form_get(body, "asset_token", tok, sizeof(tok)) && tok[0])
        strncpy(new_cfg.asset_token, tok, sizeof(new_cfg.asset_token) - 1);
    memset(tok, 0, sizeof(tok));
    if (form_get(body, "nvs_token", tok, sizeof(tok)) && tok[0])
        strncpy(new_cfg.nvs_token, tok, sizeof(new_cfg.nvs_token) - 1);

    char vad_str[8] = {0};
    if (form_get(body, "vad_ms", vad_str, sizeof(vad_str))) {
        int v = atoi(vad_str);
        if (v >= 200 && v <= 10000) new_cfg.vad_silence_ms = (uint16_t)v;
    }

    char thr_str[8] = {0};
    if (form_get(body, "ww_threshold", thr_str, sizeof(thr_str))) {
        int t = atoi(thr_str);
        if (t >= 0 && t <= 100) new_cfg.wakeword_threshold = (uint8_t)t;
    }

    /* Checkbox: nur im Body wenn angehakt; fehlt = abgehakt */
    char skip_str[4] = {0};
    new_cfg.tls_skip_verify = form_get(body, "tls_skip_verify", skip_str, sizeof(skip_str));

    free(body);
    hannah_config_save(&new_cfg);

    httpd_resp_set_type(req, "text/html");
    httpd_resp_sendstr(req,
        "<!DOCTYPE html><html><head><meta charset=utf-8>"
        "<meta http-equiv=refresh content='3;url=/'>"
        "<title>Hannah</title></head><body>"
        "<h2>Gespeichert</h2>"
        "<p>Einstellungen übernommen. Neustart in 3 Sekunden…</p>"
        "</body></html>");

    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
    return ESP_OK;
}

/* ── Handler: GET /ota ───────────────────────────────────────────────────── */

static esp_err_t ota_get_handler(httpd_req_t *req)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *next    = esp_ota_get_next_update_partition(NULL);
    const esp_app_desc_t  *app     = esp_app_get_description();

    char *buf = malloc(2048);
    if (!buf) return ESP_ERR_NO_MEM;

    int n = snprintf(buf, 2048,
        "%s<h1>Firmware Update</h1>"
        "<table>"
        "<tr><td>Aktive Partition</td><td>%s</td></tr>"
        "<tr><td>Ziel-Partition</td><td>%s</td></tr>"
        "<tr><td>Version</td><td>%s</td></tr>"
        "</table><br>"
        "<input type=file id=fw accept=.bin>"
        "<button class=btn onclick=upload()>Flashen</button>"
        "<div id=status></div>"
        "<script>"
        "async function upload(){"
          "const f=document.getElementById('fw').files[0];"
          "if(!f){alert('Keine Datei ausgewählt');return;}"
          "const s=document.getElementById('status');"
          "s.style.display='block';s.textContent='Upload läuft… bitte warten';"
          "try{"
            "const r=await fetch('/ota',{method:'POST',body:f,"
              "headers:{'Content-Type':'application/octet-stream'}});"
            "s.textContent=await r.text();"
          "}catch(e){"
            "s.textContent='Fehler: '+e;"
          "}"
        "}"
        "</script>%s",
        S_HEAD,
        running ? running->label : "?",
        next    ? next->label    : "?",
        app     ? app->version   : "?",
        S_FOOT);

    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, buf, n);
    free(buf);
    return ESP_OK;
}

/* ── Handler: POST /ota ──────────────────────────────────────────────────── */

static esp_err_t ota_post_handler(httpd_req_t *req)
{
    const esp_partition_t *update_part = esp_ota_get_next_update_partition(NULL);
    if (!update_part) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Keine OTA-Partition gefunden");
        return ESP_FAIL;
    }

    esp_ota_handle_t ota_handle;
    esp_err_t err = esp_ota_begin(update_part, OTA_SIZE_UNKNOWN, &ota_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin: %s", esp_err_to_name(err));
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, esp_err_to_name(err));
        return ESP_FAIL;
    }

    char *buf = malloc(4096);
    if (!buf) { esp_ota_abort(ota_handle); return ESP_ERR_NO_MEM; }

    int remaining = req->content_len;
    bool ok = true;

    while (remaining > 0) {
        int chunk = remaining > 4096 ? 4096 : remaining;
        int recv  = httpd_req_recv(req, buf, chunk);
        if (recv == HTTPD_SOCK_ERR_TIMEOUT) continue;
        if (recv <= 0) { ok = false; break; }
        if (esp_ota_write(ota_handle, buf, recv) != ESP_OK) { ok = false; break; }
        remaining -= recv;
    }
    free(buf);

    if (!ok || esp_ota_end(ota_handle) != ESP_OK) {
        esp_ota_abort(ota_handle);
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Flash fehlgeschlagen");
        return ESP_FAIL;
    }

    if (esp_ota_set_boot_partition(update_part) != ESP_OK) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Boot-Partition konnte nicht gesetzt werden");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "OTA erfolgreich → %s. Neustart.", update_part->label);
    httpd_resp_sendstr(req, "Firmware erfolgreich geflasht. Neustart in 3 Sekunden…");
    vTaskDelay(pdMS_TO_TICKS(3000));
    esp_restart();
    return ESP_OK;
}

/* ── Handler: GET /wifi/scan ─────────────────────────────────────────────── */

static void json_escape(const char *src, char *dst, size_t dst_len)
{
    char *d = dst;
    size_t rem = dst_len - 1;
    for (const char *s = src; *s && rem > 1; s++) {
        if (*s == '"' || *s == '\\') { *d++ = '\\'; *d++ = *s; rem -= 2; }
        else { *d++ = *s; rem--; }
    }
    *d = '\0';
}

static esp_err_t wifi_scan_handler(httpd_req_t *req)
{
    wifi_scan_config_t scan_cfg = { .scan_type = WIFI_SCAN_TYPE_ACTIVE };
    esp_wifi_scan_start(&scan_cfg, true);   /* ~2s blockierend */

    uint16_t count = 20;
    wifi_ap_record_t *aps = malloc(count * sizeof(wifi_ap_record_t));
    if (!aps) { esp_wifi_scan_stop(); return ESP_ERR_NO_MEM; }

    esp_wifi_scan_get_ap_records(&count, aps);

    char *buf = malloc(3072);
    if (!buf) { free(aps); return ESP_ERR_NO_MEM; }

    int n = snprintf(buf, 3072, "[");
    for (int i = 0; i < count && n < 3000; i++) {
        char ssid_esc[68];
        json_escape((char *)aps[i].ssid, ssid_esc, sizeof(ssid_esc));
        n += snprintf(buf + n, 3072 - n,
            "%s{\"ssid\":\"%s\",\"rssi\":%d,\"auth\":%d}",
            i > 0 ? "," : "", ssid_esc, aps[i].rssi, aps[i].authmode);
    }
    n += snprintf(buf + n, 3072 - n, "]");

    free(aps);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_send(req, buf, n);
    free(buf);
    return ESP_OK;
}

/* ── Handler: GET /log ───────────────────────────────────────────────────── */

static esp_err_t log_page_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_sendstr(req,
        "<!DOCTYPE html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width'>"
        "<title>Hannah Log</title><style>"
        "body{font-family:sans-serif;max-width:900px;margin:2em auto;padding:0 1em;color:#222}"
        "nav{margin:.8em 0 1.2em}nav a{margin-right:1.2em;color:#0066cc;text-decoration:none}"
        ".btn{background:#0066cc;color:#fff;border:0;padding:.4em 1em;"
          "border-radius:3px;cursor:pointer;margin-right:.5em}"
        ".btn-red{background:#c00}"
        "#log{background:#111;color:#0f0;padding:.8em;height:500px;overflow-y:auto;"
          "font-size:.75em;font-family:monospace;border-radius:4px;"
          "white-space:pre-wrap;word-break:break-all;margin-top:.8em}"
        "</style></head><body>"
        "<nav><a href=/>Status</a><a href=/settings>Einstellungen</a>"
        "<a href=/ota>Firmware</a><a href=/log>Log</a></nav>"
        "<h1>Log-Viewer</h1>"
        "<button class=btn id=pb onclick=\"paused=!paused;"
          "this.textContent=paused?'▶ Fortsetzen':'⏸ Pause'\">⏸ Pause</button>"
        "<button class='btn btn-red' onclick=clearLog()>Löschen</button>"
        "<a class=btn href=/log/last target=_blank>Log vor letztem Neustart</a>"
        "<div id=log></div>"
        "<script>"
        "let paused=false;"
        "async function poll(){"
          "if(!paused)try{"
            "const r=await fetch('/log/data');"
            "const t=await r.text();"
            "const el=document.getElementById('log');"
            "const bot=el.scrollHeight-el.clientHeight<=el.scrollTop+40;"
            "el.textContent=t;"
            "if(bot)el.scrollTop=el.scrollHeight;"
          "}catch(e){}"
          "setTimeout(poll,1000);"
        "}"
        "async function clearLog(){"
          "await fetch('/log/clear',{method:'POST'});"
          "document.getElementById('log').textContent='';"
        "}"
        "poll();"
        "</script></body></html>");
    return ESP_OK;
}

static esp_err_t log_data_handler(httpd_req_t *req)
{
    char *buf = malloc(LOG_BUF_SIZE);
    if (!buf) return ESP_ERR_NO_MEM;
    size_t len = log_snapshot(buf);
    httpd_resp_set_type(req, "text/plain");
    httpd_resp_send(req, buf, (int)len);
    free(buf);
    return ESP_OK;
}

static esp_err_t log_clear_handler(httpd_req_t *req)
{
    portENTER_CRITICAL(&s_log_mux);
    s_log_wp   = 0;
    s_log_full = false;
    portEXIT_CRITICAL(&s_log_mux);
    httpd_resp_sendstr(req, "OK");
    return ESP_OK;
}

/* Log-Stand von vor dem letzten Neustart (siehe persist_log_to_flash()) —
 * einzige Möglichkeit, nach einem chicken-and-egg-Neustart (Ringpuffer war
 * rein im RAM) noch an die Umstände davor zu kommen, ganz ohne seriellen
 * Zugang oder PC-Anwesenheit im richtigen Moment. */
static esp_err_t log_last_handler(httpd_req_t *req)
{
    FILE *f = fopen(CRASH_LOG_PATH, "rb");
    if (!f) {
        httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "Kein gesichertes Log vorhanden.");
        return ESP_OK;
    }
    httpd_resp_set_type(req, "text/plain");
    char chunk[512];
    size_t n;
    while ((n = fread(chunk, 1, sizeof(chunk), f)) > 0)
        httpd_resp_send_chunk(req, chunk, n);
    httpd_resp_send_chunk(req, NULL, 0);
    fclose(f);
    return ESP_OK;
}

/* ── Handler: POST /nvs (Refs #36) ───────────────────────────────────────── */

/* Nur diese Keys sind über /nvs schreibbar. Alles andere wird abgelehnt —
 * insbesondere nvs_token selbst, das bleibt /settings vorbehalten. */
static const char *NVS_ALLOWED_KEYS[] = {
    "wifi_ssid", "wifi_pass", "mqtt_broker", "mqtt_port",
    "ota_channel", "seed", "ww_threshold",
};

static esp_err_t nvs_post_handler(httpd_req_t *req)
{
    const hannah_config_t *cfg = hannah_config_get();

    /* Fail closed: kein Token konfiguriert = Endpoint komplett deaktiviert. */
    if (cfg->nvs_token[0] == '\0') {
        httpd_resp_send_err(req, HTTPD_401_UNAUTHORIZED, "NVS-Token nicht konfiguriert");
        return ESP_FAIL;
    }

    size_t hdr_len = httpd_req_get_hdr_value_len(req, "Authorization");
    if (hdr_len == 0) {
        httpd_resp_send_err(req, HTTPD_401_UNAUTHORIZED, "Authorization-Header fehlt");
        return ESP_FAIL;
    }

    char *auth = malloc(hdr_len + 1);
    if (!auth) return ESP_ERR_NO_MEM;
    esp_err_t hdr_err = httpd_req_get_hdr_value_str(req, "Authorization", auth, hdr_len + 1);

    char expected[7 + sizeof(cfg->nvs_token)];
    snprintf(expected, sizeof(expected), "Bearer %s", cfg->nvs_token);
    bool authorized = (hdr_err == ESP_OK) && (strcmp(auth, expected) == 0);
    free(auth);

    if (!authorized) {
        httpd_resp_send_err(req, HTTPD_401_UNAUTHORIZED, "Token ungültig");
        return ESP_FAIL;
    }

    if (req->content_len == 0 || req->content_len > 2048) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Body fehlt oder zu groß");
        return ESP_FAIL;
    }

    char *body = malloc(req->content_len + 1);
    if (!body) return ESP_ERR_NO_MEM;
    int got = httpd_req_recv(req, body, req->content_len);
    if (got <= 0) { free(body); return ESP_FAIL; }
    body[got] = '\0';

    cJSON *root = cJSON_Parse(body);
    free(body);
    if (!root || !cJSON_IsObject(root)) {
        if (root) cJSON_Delete(root);
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Ungültiges JSON");
        return ESP_FAIL;
    }

    /* Erst validieren (alle Keys bekannt), dann erst übernehmen — kein Teilerfolg. */
    cJSON *item = NULL;
    cJSON_ArrayForEach(item, root) {
        bool known = false;
        for (size_t i = 0; i < sizeof(NVS_ALLOWED_KEYS) / sizeof(NVS_ALLOWED_KEYS[0]); i++) {
            if (strcmp(item->string, NVS_ALLOWED_KEYS[i]) == 0) { known = true; break; }
        }
        if (!known) {
            ESP_LOGW(TAG, "/nvs: unbekannter Key '%s' abgelehnt", item->string);
            cJSON_Delete(root);
            httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Unbekannter Key");
            return ESP_FAIL;
        }
    }

    hannah_config_t new_cfg = *cfg;
    cJSON *v;

    if ((v = cJSON_GetObjectItemCaseSensitive(root, "wifi_ssid")) && cJSON_IsString(v))
        strncpy(new_cfg.wifi_ssid, v->valuestring, sizeof(new_cfg.wifi_ssid) - 1);
    if ((v = cJSON_GetObjectItemCaseSensitive(root, "wifi_pass")) && cJSON_IsString(v))
        strncpy(new_cfg.wifi_pass, v->valuestring, sizeof(new_cfg.wifi_pass) - 1);
    if ((v = cJSON_GetObjectItemCaseSensitive(root, "mqtt_broker")) && cJSON_IsString(v))
        strncpy(new_cfg.mqtt_broker, v->valuestring, sizeof(new_cfg.mqtt_broker) - 1);
    if ((v = cJSON_GetObjectItemCaseSensitive(root, "mqtt_port")) && cJSON_IsNumber(v)) {
        int p = v->valueint;
        if (p > 0 && p < 65536) new_cfg.mqtt_port = (uint16_t)p;
    }
    if ((v = cJSON_GetObjectItemCaseSensitive(root, "ota_channel")) && cJSON_IsString(v))
        strncpy(new_cfg.ota_channel, v->valuestring, sizeof(new_cfg.ota_channel) - 1);
    if ((v = cJSON_GetObjectItemCaseSensitive(root, "seed")) && cJSON_IsString(v))
        strncpy(new_cfg.seed, v->valuestring, sizeof(new_cfg.seed) - 1);
    if ((v = cJSON_GetObjectItemCaseSensitive(root, "ww_threshold")) && cJSON_IsNumber(v)) {
        int t = v->valueint;
        if (t >= 0 && t <= 100) new_cfg.wakeword_threshold = (uint8_t)t;
    }

    cJSON_Delete(root);
    hannah_config_save(&new_cfg);

    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, "{\"ok\":true}");

    ESP_LOGI(TAG, "NVS per POST /nvs aktualisiert. Neustart.");
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
    return ESP_OK;
}

/* ── Öffentliche API ─────────────────────────────────────────────────────── */

void hannah_webserver_start(void)
{
    if (s_server) return;

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.stack_size        = 8192;
    config.recv_wait_timeout = 60;
    config.send_wait_timeout = 60;
    config.max_uri_handlers  = 16;

    if (httpd_start(&s_server, &config) != ESP_OK) {
        ESP_LOGE(TAG, "httpd_start fehlgeschlagen");
        return;
    }

    httpd_uri_t routes[] = {
        { .uri = "/",          .method = HTTP_GET,  .handler = status_handler       },
        { .uri = "/settings",  .method = HTTP_GET,  .handler = settings_get_handler },
        { .uri = "/settings",  .method = HTTP_POST, .handler = settings_post_handler },
        { .uri = "/ota",       .method = HTTP_GET,  .handler = ota_get_handler      },
        { .uri = "/ota",       .method = HTTP_POST, .handler = ota_post_handler     },
        { .uri = "/wifi/scan", .method = HTTP_GET,  .handler = wifi_scan_handler    },
        { .uri = "/log",       .method = HTTP_GET,  .handler = log_page_handler     },
        { .uri = "/log/data",  .method = HTTP_GET,  .handler = log_data_handler     },
        { .uri = "/log/clear", .method = HTTP_POST, .handler = log_clear_handler    },
        { .uri = "/log/last",  .method = HTTP_GET,  .handler = log_last_handler     },
        { .uri = "/nvs",       .method = HTTP_POST, .handler = nvs_post_handler     },
    };
    for (size_t i = 0; i < sizeof(routes)/sizeof(routes[0]); i++) {
        esp_err_t err = httpd_register_uri_handler(s_server, &routes[i]);
        if (err != ESP_OK)
            ESP_LOGE(TAG, "Route '%s' konnte nicht registriert werden: %s",
                     routes[i].uri, esp_err_to_name(err));
    }

    /* Log-Ringpuffer aktivieren — ab jetzt werden alle ESP_LOG* Aufrufe gepuffert */
    if (!s_orig_vprintf)
        s_orig_vprintf = esp_log_set_vprintf(log_capture);
    esp_register_shutdown_handler(persist_log_to_flash);

    char ip[24];
    hannah_net_get_ip_str(ip, sizeof(ip));
    ESP_LOGI(TAG, "Webserver gestartet — http://%s/",
             hannah_net_is_ap_mode() ? "192.168.4.1" : ip);
}

void hannah_webserver_stop(void)
{
    if (!s_server) return;
    httpd_stop(s_server);
    s_server = NULL;
    ESP_LOGI(TAG, "Webserver gestoppt.");
}

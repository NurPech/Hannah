/**
 * hannah_led — WS2812B-Ring Steuerung mit Animationen
 *
 * Jeder LED-State hat eine eigene Animation:
 *   BOOT   — weißes Lauflicht (einmal rum, dann IDLE)
 *   IDLE   — aus
 *   WAKE   — pulsierendes Blau
 *   STREAM — rotierendes Blau (lauscht)
 *   SPEAK  — grüner Atemeffekt
 *   MUTE   — statisches Rot
 *   ERROR  — schnell blinkendes Rot
 *
 * Der Animations-Task läuft mit 50 Hz (20ms-Tick).
 * Frame-Counter wird bei jedem State-Wechsel zurückgesetzt.
 */

#include "hannah_led.h"
#include "hannah_config.h"
#include "led_strip.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include <math.h>
#include <string.h>

#define LED_GPIO   CONFIG_HANNAH_LED_GPIO
#define LED_COUNT  CONFIG_HANNAH_LED_COUNT

/* Animations-Tick: 20ms → 50 Hz */
#define TICK_MS    20

static const char *TAG = "hannah_led";
static led_strip_handle_t   s_strip         = NULL;
static volatile led_state_t s_current_state = LED_STATE_IDLE;

/* Legt den RMT-Kanal des Rings an (#548): mit DMA, sonst füllt ein Interrupt den RMT-Puffer nach. Wird
 * er zu spät bedient (WLAN-Interrupt auf demselben Kern), ist die Übertragung verfälscht und der Ring
 * zeigt falsche Farben und zu viele leuchtende LEDs (so beschreibt es die Doku des led_strip-Moduls).
 * Mit DMA schickt die Hardware den ganzen Puffer ohne Interrupt, 1024 Symbole wie im Beispiel des
 * Moduls. Klappt das nicht (kein DMA-Kanal frei), läuft der Ring wie bisher ohne DMA weiter.
 * Wird vom Start und vom Ende des Pin-Tests benutzt, damit der Ring danach nicht ohne DMA bleibt. */
static esp_err_t led_open_strip(bool *dma_used)
{
    led_strip_config_t strip_cfg = {
        .strip_gpio_num = LED_GPIO,
        .max_leds       = LED_COUNT,
    };
    led_strip_rmt_config_t rmt_cfg = {
        .resolution_hz     = 10 * 1000 * 1000,
        .mem_block_symbols = 1024,
        .flags.with_dma    = true,
    };
    esp_err_t err = led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_strip);
    if (err == ESP_OK) {
        if (dma_used) *dma_used = true;
        return ESP_OK;
    }
    ESP_LOGW(TAG, "RMT mit DMA nicht möglich (%s), der Ring läuft ohne DMA.", esp_err_to_name(err));
    rmt_cfg.mem_block_symbols = 0;       /* Standardgröße */
    rmt_cfg.flags.with_dma    = false;
    if (dma_used) *dma_used = false;
    return led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_strip);
}

/* Lautstärke-Overlay — überlagert kurzzeitig die laufende Animation, ohne
 * den State selbst zu ändern (s_current_state wird im Hintergrund weiter
 * korrekt gepflegt, z.B. MUTE/CAPTURE). */
#define VOLUME_DISPLAY_MS      1500
#define VOLUME_DISPLAY_FRAMES  (VOLUME_DISPLAY_MS / TICK_MS)
static volatile int     s_volume_overlay_frames  = 0;
static volatile uint8_t s_volume_overlay_percent = 0;

/* Globale Helligkeit in Prozent (0..100, Refs #452). Skaliert alle Pixel zentral in put_pixel(),
 * die Animationen rechnen weiter mit ihren festen Werten. Wird im NVS gespeichert, das Schreiben
 * läuft verzögert, damit ein Regler mit vielen schnellen Änderungen nur einen Schreibvorgang
 * auslöst (wie bei der Lautstärke). */
#define BRIGHTNESS_NVS_KEY         "led_bright"
#define BRIGHTNESS_SAVE_DELAY_US   (2 * 1000 * 1000)
static volatile uint8_t  s_brightness              = CONFIG_HANNAH_LED_BRIGHTNESS_DEFAULT;
static uint8_t           s_brightness_saved        = CONFIG_HANNAH_LED_BRIGHTNESS_DEFAULT;
static esp_timer_handle_t s_brightness_save_timer  = NULL;

/* ── Hilfsfunktionen ─────────────────────────────────────────────────────── */

/* Skaliert einen Kanalwert mit der globalen Helligkeit. Ein Wert größer 0 bleibt bei einer
 * Helligkeit größer 0 mindestens 1, damit dunkle Zustände (z.B. MUTE) sichtbar bleiben. Bei 0
 * ist der Ring in allen Zuständen aus. */
static inline uint8_t scale(uint8_t v)
{
    const uint8_t pct = s_brightness;
    if (pct >= 100 || v == 0) return v;
    if (pct == 0) return 0;
    const uint8_t out = (uint8_t)(((uint32_t)v * pct + 50) / 100);
    return out ? out : 1;
}

static inline void put_pixel(int idx, uint8_t r, uint8_t g, uint8_t b)
{
    led_strip_set_pixel(s_strip, idx, scale(r), scale(g), scale(b));
}

static inline void set_all(uint8_t r, uint8_t g, uint8_t b)
{
    for (int i = 0; i < LED_COUNT; i++)
        put_pixel(i, r, g, b);
}

/* Gibt einen Helligkeitswert 0..255 für einen pulsierenden Sinus zurück.
 * period_frames: Dauer einer vollständigen Periode in Frames (bei 50Hz). */
static inline float pulse(uint32_t frame, uint32_t period_frames)
{
    return 0.5f + 0.5f * sinf(2.0f * (float)M_PI * (float)frame / (float)period_frames);
}

/* ── Render-Funktionen (eine pro State) ─────────────────────────────────── */

static void render_boot(uint32_t frame)
{
    /* Weißes Lauflicht: 1 heller Kern + Abfall auf beide Seiten */
    set_all(0, 0, 0);
    /* 1 Umlauf in ~1.4s: Position wechselt alle 6 Frames (120ms/LED) */
    uint32_t pos = (frame / 6) % LED_COUNT;
    for (int d = -2; d <= 2; d++) {
        int idx = ((int)pos + d + LED_COUNT) % LED_COUNT;
        uint8_t bright;
        switch (d < 0 ? -d : d) {
            case 0: bright = 80;  break;
            case 1: bright = 35;  break;
            default: bright = 10; break;
        }
        put_pixel(idx, bright, bright, bright);
    }
}

static void render_idle(void)
{
    set_all(0, 0, 0);
}

static void render_wake(uint32_t frame)
{
    /* Pulsierendes Blau, Periode 2s = 100 Frames */
    uint8_t b = (uint8_t)(20.0f + 60.0f * pulse(frame, 100));
    set_all(0, 0, b);
}

static void render_stream(uint32_t frame)
{
    /* Rotierendes Blau: 3-LED-Bogen dreht einmal in ~1.6s = 80 Frames */
    set_all(0, 0, 0);
    /* Position in Subframe-Auflösung für flüssige Bewegung */
    float pos = fmodf((float)frame * (float)LED_COUNT / 80.0f, (float)LED_COUNT);
    int center = (int)pos % LED_COUNT;
    uint8_t bright[] = {60, 30, 10};
    for (int d = 0; d <= 2; d++) {
        int idx = (center + d) % LED_COUNT;
        put_pixel(idx, 0, 0, bright[d]);
        idx = ((center - d) + LED_COUNT) % LED_COUNT;
        put_pixel(idx, 0, 0, bright[d]);
    }
}

static void render_speak(uint32_t frame)
{
    /* Grüner Atemeffekt, Periode 2.4s = 120 Frames */
    uint8_t g = (uint8_t)(15.0f + 55.0f * pulse(frame, 120));
    set_all(0, g, 0);
}

static void render_mute(void)
{
    set_all(12, 0, 0);  /* Dunkles Rot — dauerhaft sichtbar aber nicht blendend */
}

static void render_notify(void)
{
    set_all(15, 12, 0);  /* Dunkles Gelb — ungelesene Messages, #234 */
}

static void render_capture(uint32_t frame)
{
    /* Lila Atemeffekt: mehr Blau als Rot → auch bei Minimum eindeutig lila */
    uint8_t r = (uint8_t)(5.0f  + 25.0f * pulse(frame, 75));
    uint8_t b = (uint8_t)(25.0f + 35.0f * pulse(frame, 75));
    set_all(r, 0, b);
}

static void render_volume(uint8_t percent)
{
    /* Standardrundung (0.5 aufrunden) — bei 24 LEDs ergibt das die
     * vorgerechnete Reihe 10%→2, 20%→5, ..., 100%→24. */
    int lit = (int)((percent * LED_COUNT + 50) / 100);
    set_all(0, 0, 0);
    for (int i = 0; i < lit && i < LED_COUNT; i++)
        put_pixel(i, 60, 60, 60);
}

static void render_error(uint32_t frame)
{
    /* Schnelles Blinken: 10 Frames an, 10 Frames aus = 0.4s Periode */
    if ((frame / 10) % 2 == 0)
        set_all(80, 0, 0);
    else
        set_all(0, 0, 0);
}

/* ── Pin-Test der LED-Datenleitung ─────────────────────────
 * Diagnose fuer einen Ring, der nicht reagiert (Hobbyraum01, Rev5), ohne Oszilloskop: Statt der kurzen
 * Datenpulse legt der led_task die Datenleitung (LED_GPIO) auf einen festen Pegel, den ein Multimeter
 * an jedem Punkt des Pfades sieht (ESP IO3 -> U7 SN74AHCT125 -> R10 -> D2 DIN).
 *   MQTT hannah/satellite/<device>/led_pin_test/set  mit  high | low | toggle | off  (nicht retained senden!)
 * toggle wechselt im Sekundentakt, nach 5 min ohne neuen Befehl geht es von selbst zurueck in den
 * Normalbetrieb. Alles laeuft im led_task, der RMT-Kanal wird dort freigegeben und wieder geoeffnet. */
enum { PIN_TEST_OFF = 0, PIN_TEST_HIGH, PIN_TEST_LOW, PIN_TEST_TOGGLE };
#define PIN_TEST_TIMEOUT_US (5 * 60 * 1000000LL)
static volatile int s_pin_test_req       = PIN_TEST_OFF;
static int          s_pin_test_cur       = PIN_TEST_OFF;
static int64_t      s_pin_test_since_us  = 0;
static int64_t      s_pin_test_err_log_us = 0;

void hannah_led_pin_test(const char *mode)
{
    int req;
    if (!mode) return;
    if (strcmp(mode, "high") == 0 || strcmp(mode, "1") == 0)        req = PIN_TEST_HIGH;
    else if (strcmp(mode, "low") == 0 || strcmp(mode, "0") == 0)    req = PIN_TEST_LOW;
    else if (strcmp(mode, "toggle") == 0)                           req = PIN_TEST_TOGGLE;
    else if (strcmp(mode, "off") == 0)                              req = PIN_TEST_OFF;
    else {
        ESP_LOGW(TAG, "led_pin_test/set: unbekannter Wert '%s' (erlaubt: high, low, toggle, off).", mode);
        return;
    }
    s_pin_test_req = req;
}

/* true, solange der Pin-Test laeuft: dann zeichnet der led_task nichts und sendet keine Pulse. */
static bool pin_test_step(void)
{
    int           req = s_pin_test_req;
    const int64_t now = esp_timer_get_time();

    if (s_pin_test_cur != PIN_TEST_OFF && (now - s_pin_test_since_us) > PIN_TEST_TIMEOUT_US) {
        req = PIN_TEST_OFF;
        s_pin_test_req = PIN_TEST_OFF;
        ESP_LOGI(TAG, "LED-Pin-Test: 5 min ohne neuen Befehl, zurueck in den Normalbetrieb.");
    }

    if (req != s_pin_test_cur) {
        if (s_pin_test_cur == PIN_TEST_OFF) {
            led_strip_clear(s_strip);
            led_strip_del(s_strip);
            s_strip = NULL;
            gpio_reset_pin(LED_GPIO);
            gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);
            s_pin_test_since_us = now;
        }
        if (req == PIN_TEST_OFF) {
            gpio_reset_pin(LED_GPIO);
            esp_err_t err = led_open_strip(NULL);
            if (err != ESP_OK) {
                /* Kein Abbruch: der Pin-Test bleibt aktiv (der led_task zeichnet dann nichts) und der
                 * RMT-Kanal wird beim naechsten Durchlauf erneut geoeffnet. Der Fehler wird gedrosselt geloggt. */
                s_strip = NULL;
                if (now - s_pin_test_err_log_us > 5 * 1000000LL) {
                    s_pin_test_err_log_us = now;
                    ESP_LOGE(TAG, "LED-Pin-Test: RMT-Kanal nicht wieder geoeffnet (%s), neuer Versuch.", esp_err_to_name(err));
                }
                return true;
            }
            led_strip_clear(s_strip);
            s_pin_test_cur = PIN_TEST_OFF;
            ESP_LOGI(TAG, "LED-Pin-Test beendet, Ring wieder im Normalbetrieb.");
            return false;
        }
        s_pin_test_cur = req;
        ESP_LOGI(TAG, "LED-Pin-Test: GPIO %d %s.", LED_GPIO,
                 req == PIN_TEST_HIGH ? "dauerhaft HIGH" : req == PIN_TEST_LOW ? "dauerhaft LOW" : "toggelt im Sekundentakt");
    }

    if (s_pin_test_cur == PIN_TEST_OFF) return false;

    int level = 0;
    if (s_pin_test_cur == PIN_TEST_HIGH)        level = 1;
    else if (s_pin_test_cur == PIN_TEST_TOGGLE) level = (int)(((now - s_pin_test_since_us) / 1000000LL) & 1);
    gpio_set_level(LED_GPIO, level);
    return true;
}

/* ── Animations-Task ─────────────────────────────────────────────────────── */

static void led_task(void *arg)
{
    uint32_t      frame      = 0;
    led_state_t   last_state = LED_STATE_BOOT;

    while (1) {
        if (pin_test_step()) {
            vTaskDelay(pdMS_TO_TICKS(TICK_MS));
            continue;
        }
        if (s_volume_overlay_frames > 0) {
            render_volume(s_volume_overlay_percent);
            s_volume_overlay_frames--;
        } else {
            led_state_t state = s_current_state;
            if (state != last_state) {
                frame      = 0;
                last_state = state;
            }

            switch (state) {
                case LED_STATE_BOOT:   render_boot(frame);   break;
                case LED_STATE_IDLE:   render_idle();        break;
                case LED_STATE_WAKE:   render_wake(frame);   break;
                case LED_STATE_STREAM: render_stream(frame); break;
                case LED_STATE_SPEAK:  render_speak(frame);  break;
                case LED_STATE_MUTE:    render_mute();         break;
                case LED_STATE_ERROR:   render_error(frame);   break;
                case LED_STATE_CAPTURE: render_capture(frame); break;
                case LED_STATE_NOTIFY:  render_notify();       break;
            }

            frame++;
        }

        led_strip_refresh(s_strip);
        vTaskDelay(pdMS_TO_TICKS(TICK_MS));
    }
}

/* ── Öffentliche API ─────────────────────────────────────────────────────── */

static void brightness_save_cb(void *arg)
{
    (void)arg;
    const uint8_t b = s_brightness;
    if (b != s_brightness_saved) {
        hannah_config_runtime_set_u8(BRIGHTNESS_NVS_KEY, b);
        s_brightness_saved = b;
        ESP_LOGI(TAG, "LED-Helligkeit gespeichert: %d%%", b);
    }
}

void hannah_led_init(void)
{
    /* Zuletzt eingestellte Helligkeit aus dem NVS; ohne gespeicherten Wert gilt
     * CONFIG_HANNAH_LED_BRIGHTNESS_DEFAULT. hannah_config_init() läuft vorher (main.c). */
    uint8_t saved;
    if (hannah_config_runtime_get_u8(BRIGHTNESS_NVS_KEY, &saved) && saved <= 100) {
        s_brightness = saved;
        ESP_LOGI(TAG, "LED-Helligkeit aus NVS: %d%%", saved);
    }
    s_brightness_saved = s_brightness;
    const esp_timer_create_args_t save_timer_args = {
        .callback = brightness_save_cb,
        .name     = "led_bright_save",
    };
    if (esp_timer_create(&save_timer_args, &s_brightness_save_timer) != ESP_OK)
        ESP_LOGW(TAG, "Timer zum Speichern der LED-Helligkeit nicht angelegt — Helligkeit bleibt nicht erhalten.");

    bool use_dma = false;
    ESP_ERROR_CHECK(led_open_strip(&use_dma));
    led_strip_clear(s_strip);

    xTaskCreate(led_task, "hannah_led", 2048, NULL, 3, NULL);
    ESP_LOGI(TAG, "LED ring initialized (%d LEDs, GPIO %d, RMT %s)", LED_COUNT, LED_GPIO,
             use_dma ? "mit DMA" : "ohne DMA");
}

void hannah_led_set_state(led_state_t state)
{
    if (state != s_current_state) {
        static const char *names[] = {
            "BOOT","IDLE","WAKE","STREAM","SPEAK","MUTE","ERROR","CAPTURE","NOTIFY"
        };
        ESP_LOGI(TAG, "LED %s → %s",
                 names[(int)s_current_state], names[(int)state]);
    }
    s_current_state = state;
}

void hannah_led_show_volume(uint8_t percent)
{
    s_volume_overlay_percent = percent;
    s_volume_overlay_frames  = VOLUME_DISPLAY_FRAMES;
}

void hannah_led_set_brightness(uint8_t percent)
{
    if (percent > 100) percent = 100;
    s_brightness = percent;
    ESP_LOGI(TAG, "LED-Helligkeit gesetzt: %d%%", percent);
    if (!s_brightness_save_timer) return;
    esp_timer_stop(s_brightness_save_timer);   /* ESP_ERR_INVALID_STATE, wenn er nicht lief: ok */
    esp_timer_start_once(s_brightness_save_timer, BRIGHTNESS_SAVE_DELAY_US);
}

uint8_t hannah_led_get_brightness(void)
{
    return s_brightness;
}

void hannah_status_led_init(void)
{
#if CONFIG_HANNAH_STATUS_LED_ENABLED
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << CONFIG_HANNAH_STATUS_LED_GPIO),
        .mode         = GPIO_MODE_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    gpio_set_level(CONFIG_HANNAH_STATUS_LED_GPIO, 1);
    ESP_LOGI(TAG, "Status-LED an GPIO %d", CONFIG_HANNAH_STATUS_LED_GPIO);
#endif
}

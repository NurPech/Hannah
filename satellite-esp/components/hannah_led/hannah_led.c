/**
 * hannah_led — WS2812B-Ring Steuerung
 *
 * Hardware: 12-LED WS2812B-Ring, verbunden via RMT-Peripheral.
 * GPIO und LED-Anzahl über sdkconfig konfigurierbar (HANNAH_LED_GPIO, HANNAH_LED_COUNT).
 *
 * TODO: Animationen (Pulse, Rotate) per FreeRTOS-Task implementieren.
 *       Aktuell nur statische Farben als Platzhalter.
 */

#include "hannah_led.h"
#include "led_strip.h"
#include "esp_log.h"
#include "sdkconfig.h"

#define LED_GPIO   CONFIG_HANNAH_LED_GPIO
#define LED_COUNT  CONFIG_HANNAH_LED_COUNT

static const char *TAG = "hannah_led";
static led_strip_handle_t s_strip = NULL;
static led_state_t s_current_state = LED_STATE_IDLE;

void hannah_led_init(void)
{
    led_strip_config_t strip_cfg = {
        .strip_gpio_num = LED_GPIO,
        .max_leds       = LED_COUNT,
    };
    led_strip_rmt_config_t rmt_cfg = {
        .resolution_hz = 10 * 1000 * 1000, /* 10 MHz */
    };
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_strip));
    led_strip_clear(s_strip);
    ESP_LOGI(TAG, "LED ring initialized (%d LEDs, GPIO %d)", LED_COUNT, LED_GPIO);
}

void hannah_led_set_state(led_state_t state)
{
    if (!s_strip) return;
    s_current_state = state;

    uint8_t r = 0, g = 0, b = 0;
    switch (state) {
        case LED_STATE_BOOT:   r = 20; g = 20; b = 20; break; /* Warmweiß */
        case LED_STATE_IDLE:   r =  0; g =  0; b =  0; break; /* Aus      */
        case LED_STATE_WAKE:   r =  0; g =  0; b = 80; break; /* Blau     */
        case LED_STATE_STREAM: r =  0; g =  0; b = 40; break; /* Blau dim */
        case LED_STATE_SPEAK:  r =  0; g = 60; b =  0; break; /* Grün     */
        case LED_STATE_MUTE:   r = 80; g =  0; b =  0; break; /* Rot      */
        case LED_STATE_ERROR:  r = 80; g =  0; b =  0; break; /* Rot      */
    }

    for (int i = 0; i < LED_COUNT; i++) {
        led_strip_set_pixel(s_strip, i, r, g, b);
    }
    led_strip_refresh(s_strip);
}

#pragma once

#include <stdint.h>

typedef enum {
    LED_STATE_BOOT,       /* Warmweiß, rotierend — Initialisierung */
    LED_STATE_IDLE,       /* Aus / sehr dunkel                     */
    LED_STATE_WAKE,       /* Blau, pulsierend — Wake-Word erkannt  */
    LED_STATE_STREAM,     /* Blau, umlaufend — Audio wird gestreamt */
    LED_STATE_SPEAK,      /* Grün, pulsierend — TTS-Ausgabe        */
    LED_STATE_MUTE,       /* Rot, statisch — Mikrofon stummgeschaltet */
    LED_STATE_ERROR,      /* Rot, schnell blinkend — Fehler        */
    LED_STATE_CAPTURE,    /* Lila, pulsierend — Wakeword-Capture-Modus */
    LED_STATE_NOTIFY,     /* Gelb, gedimmt, statisch — ungelesene Messages (#234) */
} led_state_t;

void hannah_led_init(void);
void hannah_led_set_state(led_state_t state);
void hannah_status_led_init(void);

/* Überlagert die aktuelle Animation kurzzeitig mit einem gefüllten
 * Lautstärke-Balken (gedimmtes Weiß), unabhängig vom laufenden State —
 * siehe hannah_led.c für Details. */
void hannah_led_show_volume(uint8_t percent);

/* Globale Helligkeit des Rings in Prozent (0..100, Refs #452). Gilt für alle States und das
 * Lautstärke-Overlay. 0 = Ring in allen Zuständen aus, auch MUTE und ERROR. Der Wert wird
 * verzögert im NVS gespeichert und beim Start wieder geladen. */
void    hannah_led_set_brightness(uint8_t percent);
uint8_t hannah_led_get_brightness(void);

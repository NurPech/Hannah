#pragma once

typedef enum {
    LED_STATE_BOOT,       /* Warmweiß, rotierend — Initialisierung */
    LED_STATE_IDLE,       /* Aus / sehr dunkel                     */
    LED_STATE_WAKE,       /* Blau, pulsierend — Wake-Word erkannt  */
    LED_STATE_STREAM,     /* Blau, umlaufend — Audio wird gestreamt */
    LED_STATE_SPEAK,      /* Grün, pulsierend — TTS-Ausgabe        */
    LED_STATE_MUTE,       /* Rot, statisch — Mikrofon stummgeschaltet */
    LED_STATE_ERROR,      /* Rot, schnell blinkend — Fehler        */
} led_state_t;

void hannah_led_init(void);
void hannah_led_set_state(led_state_t state);

#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * hannah_wakeword — lokale Wake-Word-Erkennung (microWakeWord / TFLite Micro)
 *
 * Pipeline:
 *   PCM (160 Samples / 10ms) → Mel-Spektrogramm (40 Bins) →
 *   TFLite-Inference → Confidence [0.0 – 1.0]
 *
 * Wenn HANNAH_WAKEWORD_ENABLED=n oder kein Modell eingebunden:
 *   hannah_wakeword_process() gibt immer 0.0f zurück (Stub).
 *
 * Modell einbinden (nach dem Training):
 *   1. model/placeholder.h ersetzen durch model/model.h (xxd -i model.tflite)
 *   2. HANNAH_WAKEWORD_ENABLED=y in menuconfig
 *   3. In CMakeLists.txt: REQUIRES esp-tflite-micro hinzufügen
 *
 * Mel-Parameter müssen mit dem microWakeWord-Training übereinstimmen
 * (menuconfig → Hannah Wake Word).
 */

/* Samples pro Schritt (10ms @ 16kHz) — fest, unabhängig vom Modell. */
#define WAKEWORD_STEP_SAMPLES 160

void  hannah_wakeword_init(void);

/**
 * Verarbeitet einen 10ms-PCM-Frame und gibt die Wake-Word-Confidence zurück.
 * @param pcm   Zeiger auf WAKEWORD_STEP_SAMPLES int16-Samples (mono, 16kHz)
 * @return      Wahrscheinlichkeit [0.0, 1.0] — 0.0 im Placeholder-Modus
 */
float hannah_wakeword_process(const int16_t *pcm);

#ifdef __cplusplus
}
#endif

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
 * Gibt die TFLite-Arena (PSRAM) und den Interpreter frei. Danach liefert
 * hannah_wakeword_process() konstant 0.0f, bis hannah_wakeword_reinit()
 * läuft. Für OTA: gibt PSRAM für den mbedTLS-Download frei (siehe #171-Saga).
 */
void  hannah_wakeword_deinit(void);

/**
 * Gegenstück zu hannah_wakeword_deinit() — allokiert Arena + Interpreter neu
 * (AudioFrontend-Zustand bleibt unangetastet). Für den Fall, dass OTA
 * fehlschlägt und kein Neustart folgt.
 */
void  hannah_wakeword_reinit(void);

/**
 * Verarbeitet einen 10ms-PCM-Frame und gibt die Wake-Word-Confidence zurück.
 * @param pcm   Zeiger auf WAKEWORD_STEP_SAMPLES int16-Samples (mono, 16kHz)
 * @return      Wahrscheinlichkeit [0.0, 1.0] — 0.0 im Placeholder-Modus
 */
float hannah_wakeword_process(const int16_t *pcm);

/* Input-Tensor ist (1,1,40) — 40 deckt einen kompletten Feature-Frame ab. */
#define HANNAH_WAKEWORD_DEBUG_PREVIEW_LEN 40

/**
 * Komplette Diagnosekette eines hannah_wakeword_process()-Aufrufs (#173):
 * on-device Confidence bleibt trotz offline validiertem Modell flach bei 0.0,
 * ohne Instrumentierung war nicht unterscheidbar ob das Frontend überhaupt
 * Frames liefert, die Quantisierung entartet, oder Invoke() selbst fehlschlägt.
 */
typedef struct {
    size_t   feat_size;         /* AudioFrontend-Framegröße (0 = kein vollständiger Frame, Invoke() lief nicht) */
    size_t   num_read;          /* von FrontendProcessSamples tatsächlich konsumierte PCM-Samples (soll == WAKEWORD_STEP_SAMPLES sein) */
    uint16_t mel_preview[HANNAH_WAKEWORD_DEBUG_PREVIEW_LEN];   /* rohe Frontend-Werte vor der Quantisierung — 1:1 vergleichbar mit pymicro_features */
    int8_t   input_preview[HANNAH_WAKEWORD_DEBUG_PREVIEW_LEN]; /* quantisierte int8-Werte, die tatsächlich in den Input-Tensor geschrieben wurden */
    uint8_t  output_raw;        /* unskalierter uint8-Output-Tensor-Wert (confidence = output_raw / 256.0) */
    uint32_t invoke_fail_count; /* kumulative Anzahl fehlgeschlagener Invoke()-Aufrufe seit Boot */
} hannah_wakeword_debug_t;

/**
 * Schreibt die Debug-Info aus dem letzten hannah_wakeword_process()-Aufruf
 * nach *out. Kein Storage nötig, nur für Live-Logging gedacht.
 */
void hannah_wakeword_last_debug(hannah_wakeword_debug_t *out);

#ifdef __cplusplus
}
#endif

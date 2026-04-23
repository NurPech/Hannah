/**
 * Platzhalter-Modell — ersetzt durch das trainierte microWakeWord-Modell.
 *
 * Nach dem Training:
 *   1. TFLite-Modell als C-Array exportieren:
 *        xxd -i hey_hannah_int8.tflite > model.h
 *   2. Diese Datei löschen, model.h hier ablegen.
 *   3. In hannah_wakeword.c: #include "model/placeholder.h"
 *                          → #include "model/model.h"
 *   4. In CMakeLists.txt: esp-tflite-micro zu REQUIRES hinzufügen.
 *   5. In menuconfig: HANNAH_WAKEWORD_ENABLED = y
 *
 * Mel-Parameter prüfen (müssen mit Training übereinstimmen):
 *   menuconfig → Hannah Wake Word → Mel-Frame-Länge + Mel-Filterbank-Bänder
 */

#define WAKEWORD_MODEL_IS_PLACEHOLDER 1

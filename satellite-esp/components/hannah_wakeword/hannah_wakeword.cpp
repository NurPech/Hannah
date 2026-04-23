/**
 * hannah_wakeword — Wake-Word-Erkennung mit microWakeWord (TFLite Micro)
 *
 * Feature-Pipeline: TFLite Micro AudioFrontend (FrontendProcessSamples)
 *   Identisch zu pymicro-features / Trainingspipeline:
 *   30ms Fenster, 10ms Schritt, 40 Mel-Bänder, 125–7500 Hz, PCAN.
 *
 * Quantisierung uint16 → int8:
 *   Python:   float = uint16 / 128.0
 *   Modell:   int8  = round(float / 0.10196) − 128
 *   Kombiniert: int8 = round(uint16 / 13.051) − 128
 *
 * Modell: hey_hannah_int8.tflite (inception, streaming state_internal)
 *   Input:  (1, 1, 40) int8  — scale=0.10196, zero_point=−128
 *   Output: (1, 1)     uint8 — scale=1/256,   zero_point=0
 */

#include "hannah_wakeword.h"
#include "model/model.h"
#include "esp_log.h"

#include "tensorflow/lite/experimental/microfrontend/lib/frontend.h"
#include "tensorflow/lite/experimental/microfrontend/lib/frontend_util.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"

static const char *TAG = "wakeword";

/* Skalierungskonstante: uint16_C / (128 × model_input_scale) → float-Äquivalent */
static constexpr float  FEATURE_SCALE    = 128.0f * 0.10196078568696976f;  /* ≈ 13.051 */
static constexpr int    INPUT_ZERO_POINT = -128;
static constexpr float  OUTPUT_SCALE     = 1.0f / 256.0f;

static constexpr size_t ARENA_SIZE = CONFIG_HANNAH_TFLITE_ARENA_KB * 1024;
static uint8_t s_arena[ARENA_SIZE];

static struct FrontendState              s_frontend;
static tflite::MicroMutableOpResolver<20> s_resolver;
static tflite::MicroInterpreter          *s_interpreter = nullptr;
static TfLiteTensor                      *s_input       = nullptr;
static TfLiteTensor                      *s_output      = nullptr;

/* ------------------------------------------------------------------ */

static void tflite_init(void)
{
    s_resolver.AddConv2D();
    s_resolver.AddDepthwiseConv2D();
    s_resolver.AddFullyConnected();
    s_resolver.AddReshape();
    s_resolver.AddMean();
    s_resolver.AddConcatenation();
    s_resolver.AddLogistic();
    s_resolver.AddAdd();
    s_resolver.AddMul();
    s_resolver.AddStridedSlice();
    s_resolver.AddQuantize();
    s_resolver.AddDequantize();
    s_resolver.AddCallOnce();
    s_resolver.AddVarHandle();
    s_resolver.AddAssignVariable();
    s_resolver.AddReadVariable();

    const tflite::Model *model = tflite::GetModel(hey_hannah_int8_tflite);
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        ESP_LOGE(TAG, "TFLite schema version mismatch: %lu vs %d",
                 (unsigned long)model->version(), TFLITE_SCHEMA_VERSION);
        return;
    }

    static tflite::MicroInterpreter interp(model, s_resolver, s_arena, ARENA_SIZE);
    if (interp.AllocateTensors() != kTfLiteOk) {
        ESP_LOGE(TAG, "AllocateTensors fehlgeschlagen — Arena zu klein? (%u KB)",
                 (unsigned)(ARENA_SIZE / 1024));
        return;
    }
    s_interpreter = &interp;
    s_input       = s_interpreter->input(0);
    s_output      = s_interpreter->output(0);

    ESP_LOGI(TAG, "TFLite geladen: Arena %u KB, verwendet %u B.",
             (unsigned)(ARENA_SIZE / 1024),
             (unsigned)s_interpreter->arena_used_bytes());
}

/* ------------------------------------------------------------------ */

void hannah_wakeword_init(void)
{
    struct FrontendConfig cfg;
    FrontendFillConfigWithDefaults(&cfg);
    cfg.window.size_ms                         = 30;
    cfg.window.step_size_ms                    = 10;
    cfg.filterbank.num_channels                = 40;
    cfg.filterbank.lower_band_limit            = 125.0f;
    cfg.filterbank.upper_band_limit            = 7500.0f;
    cfg.pcan_gain_control.enable_pcan          = 1;
    cfg.noise_reduction.min_signal_remaining = 1.0f;

    if (!FrontendPopulateState(&cfg, &s_frontend, 16000)) {
        ESP_LOGE(TAG, "FrontendPopulateState fehlgeschlagen");
        return;
    }

    tflite_init();
    ESP_LOGI(TAG, "Wakeword bereit (AudioFrontend + TFLite Micro).");
}

float hannah_wakeword_process(const int16_t *pcm)
{
    if (!s_interpreter) return 0.0f;

    size_t num_read;
    struct FrontendOutput feat = FrontendProcessSamples(
        &s_frontend, pcm, WAKEWORD_STEP_SAMPLES, &num_read);

    if (feat.size == 0) return 0.0f;   /* Noch kein vollständiger Frame */

    /* uint16 → int8 quantisieren */
    for (size_t i = 0; i < feat.size; i++) {
        int32_t q = (int32_t)roundf((float)feat.values[i] / FEATURE_SCALE) + INPUT_ZERO_POINT;
        if      (q < -128) q = -128;
        else if (q >  127) q =  127;
        s_input->data.int8[i] = (int8_t)q;
    }

    if (s_interpreter->Invoke() != kTfLiteOk) return 0.0f;

    return (float)(uint8_t)s_output->data.uint8[0] * OUTPUT_SCALE;
}

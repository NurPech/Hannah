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
 *
 * Modell-Override (#166): Liegt im Asset-Cache (hannah_asset, Asset-ID
 * "wakeword") eine gültige .tflite-Datei, wird diese statt des eingebauten
 * Default-Arrays geladen — erlaubt Testen neuer Modelle per Asset-Upload,
 * ohne Firmware-Release. Fällt bei fehlendem/ungültigem Override automatisch
 * auf das eingebaute Modell zurück.
 */

#include "hannah_wakeword.h"
#include "hannah_asset.h"
#include "model/model.h"
#include "esp_log.h"

#include "tensorflow/lite/experimental/microfrontend/lib/frontend.h"
#include "tensorflow/lite/experimental/microfrontend/lib/frontend_util.h"
#include "esp_heap_caps.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/micro/micro_allocator.h"
#include "tensorflow/lite/schema/schema_generated.h"

static const char *TAG = "wakeword";

/* Skalierungskonstante: uint16_C / (128 × model_input_scale) → float-Äquivalent */
static constexpr float  FEATURE_SCALE    = 128.0f * 0.10196078568696976f;  /* ≈ 13.051 */
static constexpr int    INPUT_ZERO_POINT = -128;
static constexpr float  OUTPUT_SCALE     = 1.0f / 256.0f;

static constexpr size_t ARENA_SIZE    = CONFIG_HANNAH_TFLITE_ARENA_KB * 1024;
static constexpr size_t RV_ARENA_SIZE = 4096;
static uint8_t *s_arena               = nullptr;
static uint8_t  s_rv_arena[RV_ARENA_SIZE];

static struct FrontendState               s_frontend;
static tflite::MicroMutableOpResolver<20> s_resolver;
static tflite::MicroInterpreter          *s_interpreter      = nullptr;
static tflite::MicroResourceVariables    *s_resource_vars    = nullptr;
static TfLiteTensor                      *s_input            = nullptr;
static TfLiteTensor                      *s_output           = nullptr;

/* ------------------------------------------------------------------ */

/* Wakeword-Modell laden: Override aus dem Asset-Cache bevorzugen, sonst das
 * eingebaute Default-Array (#166). */
static const tflite::Model *load_model(void)
{
    uint8_t *override_buf  = nullptr;
    size_t   override_size = 0;

    if (hannah_asset_read_to_psram("wakeword", &override_buf, &override_size)) {
        const tflite::Model *m = tflite::GetModel(override_buf);
        if (m->version() == TFLITE_SCHEMA_VERSION) {
            ESP_LOGI(TAG, "Wakeword-Override aus Asset-Cache geladen (%u Bytes).",
                     (unsigned)override_size);
            return m;
        }
        ESP_LOGW(TAG, "Wakeword-Override ungültig (Schema-Version) — "
                      "falle zurück auf eingebautes Modell.");
        heap_caps_free(override_buf);
    }

    return tflite::GetModel(hey_hannah_int8_tflite);
}

static void tflite_init(void)
{
    s_arena = (uint8_t *)heap_caps_malloc(ARENA_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_arena) {
        ESP_LOGE(TAG, "PSRAM-Allokation fehlgeschlagen (%u KB)", (unsigned)(ARENA_SIZE / 1024));
        return;
    }

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

    const tflite::Model *model = load_model();
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        ESP_LOGE(TAG, "TFLite schema version mismatch: %lu vs %d",
                 (unsigned long)model->version(), TFLITE_SCHEMA_VERSION);
        return;
    }

    auto *rv_allocator = tflite::MicroAllocator::Create(s_rv_arena, RV_ARENA_SIZE);
    s_resource_vars    = tflite::MicroResourceVariables::Create(rv_allocator, 20);

    static tflite::MicroInterpreter interp(model, s_resolver, s_arena, ARENA_SIZE, s_resource_vars);
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
    if (s_interpreter) {
        ESP_LOGI(TAG, "Wakeword bereit (AudioFrontend + TFLite Micro).");
    } else {
        ESP_LOGE(TAG, "Wakeword-Init fehlgeschlagen — Erkennung deaktiviert (liefert konstant 0.0).");
    }
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

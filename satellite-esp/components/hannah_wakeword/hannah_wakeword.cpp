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
#include <new>
#include <cstring>

#include "tensorflow/lite/experimental/microfrontend/lib/frontend.h"
#include "tensorflow/lite/experimental/microfrontend/lib/frontend_util.h"
#include "esp_heap_caps.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/micro/micro_allocator.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "tensorflow/lite/schema/schema_utils.h"

static const char *TAG = "wakeword";

/* Skalierungskonstante: uint16_C / (128 × model_input_scale) → float-Äquivalent */
static constexpr float  FEATURE_SCALE    = 128.0f * 0.10196078568696976f;  /* ≈ 13.051 */
static constexpr int    INPUT_ZERO_POINT = -128;
static constexpr float  OUTPUT_SCALE     = 1.0f / 256.0f;
static constexpr size_t FRONTEND_NUM_CHANNELS = 40;  /* Mel-Bänder pro AudioFrontend-Frame */

static constexpr size_t ARENA_SIZE    = CONFIG_HANNAH_TFLITE_ARENA_KB * 1024;
/* War 4096 B im internen DRAM, fix und unabhängig vom geladenen Modell (#166
 * Asset-Override erlaubt ja beliebige Modelle) — #179: bei okay_nabu.tflite
 * (Home-Assistant-Modell, zum Differenzialtest gegen hey_hannah geladen)
 * brauchen allein die Streaming-Resource-Variablen ~8 KB (11 States, der
 * größte einzelne 7104 B) — passte schon für dessen größte einzelne Variable
 * nicht in die alte Arena. AllocateTensors()/Invoke() melden dabei keinen
 * Fehler (Haupt-Arena ist groß genug), der Streaming-State selbst lief aber
 * vermutlich über die zu kleine RV-Arena und blieb degeneriert — passt exakt
 * zum beobachteten Symptom (Modell "läuft", Output bewegt sich aber nie
 * sinnvoll). Jetzt großzügig auf PSRAM, analog zu ARENA_SIZE. */
static constexpr size_t RV_ARENA_SIZE = 32 * 1024;
static uint8_t *s_arena               = nullptr;
static uint8_t *s_rv_arena            = nullptr;

static struct FrontendState               s_frontend;
static tflite::MicroMutableOpResolver<21> s_resolver;
static bool                               s_resolver_ready   = false;
static tflite::MicroInterpreter          *s_interpreter      = nullptr;
static tflite::MicroResourceVariables    *s_resource_vars    = nullptr;
static TfLiteTensor                      *s_input            = nullptr;
static TfLiteTensor                      *s_output           = nullptr;
static uint8_t                           *s_model_override_buf = nullptr;
static size_t                             s_input_frame_count = 1;  /* #181: aggregierte Frames pro Invoke */

/* Debug-Snapshot des letzten process()-Aufrufs (#173) */
static size_t   s_last_feat_size                                            = 0;
static size_t   s_last_num_read                                             = 0;
static int8_t   s_last_input_preview[HANNAH_WAKEWORD_DEBUG_PREVIEW_LEN];
static uint16_t s_last_mel_preview[HANNAH_WAKEWORD_DEBUG_PREVIEW_LEN];    /* rohe Frontend-Werte vor der Quantisierung */
static uint8_t  s_last_output_raw                                           = 0;
static uint32_t s_invoke_fail_count                                        = 0;

/* ------------------------------------------------------------------ */

/* Wakeword-Modell laden: Override aus dem Asset-Cache bevorzugen, sonst das
 * eingebaute Default-Array (#166). */
static const tflite::Model *load_model(void)
{
    size_t override_size = 0;

    if (hannah_asset_read_to_psram("wakeword", &s_model_override_buf, &override_size)) {
        const tflite::Model *m = tflite::GetModel(s_model_override_buf);
        if (m->version() == TFLITE_SCHEMA_VERSION) {
            ESP_LOGI(TAG, "Wakeword-Override aus Asset-Cache geladen (%u Bytes).",
                     (unsigned)override_size);
            return m;
        }
        ESP_LOGW(TAG, "Wakeword-Override ungültig (Schema-Version) — "
                      "falle zurück auf eingebautes Modell.");
        heap_caps_free(s_model_override_buf);
        s_model_override_buf = nullptr;
    }

    return tflite::GetModel(hey_hannah_int8_tflite);
}

static void ensure_resolver(void)
{
    if (s_resolver_ready) return;

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
    s_resolver.AddTranspose();
    s_resolver.AddSub();
    s_resolver.AddSqrt();
    s_resolver.AddDiv();
    s_resolver.AddSplitV();  /* #183: okay_nabu_v2 (DepthwiseConv2D+SplitV-Architektur) */

    s_resolver_ready = true;
}

static void free_model_override(void)
{
    if (s_model_override_buf) {
        heap_caps_free(s_model_override_buf);
        s_model_override_buf = nullptr;
    }
}

/* #183: prüft, ob alle im Modell verwendeten Ops im Resolver registriert sind
 * — VOR Interpreter-Konstruktion/AllocateTensors(). Ein Modell mit einem
 * fehlenden Op (z.B. okay_nabu_v2s SplitV, damals nicht registriert) ließ
 * AllocateTensors() mitten im Op-Graph-Aufbau abbrechen; der anschließende
 * Cleanup (delete auf einem nur teilweise aufgebauten Interpreter) stürzte
 * dabei selbst hart ab (Guru Meditation Error, Boot-Loop, nur per physischem
 * Download-Modus behebbar — siehe #183). Diese Prüfung lässt ein
 * inkompatibles Modell sauber ablehnen, bevor der gefährliche Pfad überhaupt
 * erreicht wird. */
static bool validate_ops_supported(const tflite::Model *model)
{
    auto *op_codes = model->operator_codes();
    if (!op_codes) return true;

    bool all_supported = true;
    for (unsigned i = 0; i < op_codes->size(); i++) {
        const tflite::OperatorCode *oc = op_codes->Get(i);
        tflite::BuiltinOperator builtin = tflite::GetBuiltinCode(oc);

        if (builtin == tflite::BuiltinOperator_CUSTOM) {
            const char *custom = oc->custom_code() ? oc->custom_code()->c_str() : "?";
            if (s_resolver.FindOp(custom) == nullptr) {
                ESP_LOGE(TAG, "Wakeword-Modell nutzt nicht registrierten Custom-Op '%s'.", custom);
                all_supported = false;
            }
            continue;
        }

        if (s_resolver.FindOp(builtin) == nullptr) {
            ESP_LOGE(TAG, "Wakeword-Modell nutzt nicht registrierten Op '%s' (Code %d).",
                     tflite::EnumNameBuiltinOperator(builtin), (int)builtin);
            all_supported = false;
        }
    }
    return all_supported;
}

static void tflite_deinit(void);

/* Allokiert Arena + Interpreter neu. Wiederholt aufrufbar (OTA-Reinit) —
 * der Resolver wird dabei nur beim ersten Mal befüllt. */
static void tflite_init(void)
{
    s_arena = (uint8_t *)heap_caps_malloc(ARENA_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_arena) {
        ESP_LOGE(TAG, "PSRAM-Allokation fehlgeschlagen (%u KB)", (unsigned)(ARENA_SIZE / 1024));
        return;
    }

    s_rv_arena = (uint8_t *)heap_caps_malloc(RV_ARENA_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_rv_arena) {
        ESP_LOGE(TAG, "PSRAM-Allokation (Resource-Variablen) fehlgeschlagen (%u KB)",
                 (unsigned)(RV_ARENA_SIZE / 1024));
        heap_caps_free(s_arena);
        s_arena = nullptr;
        return;
    }

    ensure_resolver();

    const tflite::Model *model = load_model();
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        ESP_LOGE(TAG, "TFLite schema version mismatch: %lu vs %d",
                 (unsigned long)model->version(), TFLITE_SCHEMA_VERSION);
        free_model_override();
        heap_caps_free(s_rv_arena);
        s_rv_arena = nullptr;
        heap_caps_free(s_arena);
        s_arena = nullptr;
        return;
    }

    /* #183: Op-Kompatibilität VOR Interpreter-Konstruktion/AllocateTensors()
     * prüfen — verhindert, dass ein Modell mit fehlendem Op überhaupt in den
     * gefährlichen Teilweise-aufgebaut-dann-Cleanup-Pfad läuft. */
    if (!validate_ops_supported(model)) {
        ESP_LOGE(TAG, "Wakeword-Modell inkompatibel (siehe obige Op-Meldung(en)) — "
                      "Erkennung bleibt deaktiviert.");
        free_model_override();
        heap_caps_free(s_rv_arena);
        s_rv_arena = nullptr;
        heap_caps_free(s_arena);
        s_arena = nullptr;
        return;
    }

    auto *rv_allocator = tflite::MicroAllocator::Create(s_rv_arena, RV_ARENA_SIZE);
    s_resource_vars    = tflite::MicroResourceVariables::Create(rv_allocator, 20);

    auto *interpreter = new (std::nothrow) tflite::MicroInterpreter(
        model, s_resolver, s_arena, ARENA_SIZE, s_resource_vars);
    if (!interpreter) {
        ESP_LOGE(TAG, "MicroInterpreter-Allokation fehlgeschlagen");
        free_model_override();
        heap_caps_free(s_rv_arena);
        s_rv_arena = nullptr;
        heap_caps_free(s_arena);
        s_arena = nullptr;
        return;
    }

    if (interpreter->AllocateTensors() != kTfLiteOk) {
        ESP_LOGE(TAG, "AllocateTensors fehlgeschlagen — Arena zu klein? (%u KB Arena, %u B davon belegt vor Abbruch)",
                 (unsigned)(ARENA_SIZE / 1024),
                 (unsigned)interpreter->arena_used_bytes());
        /* #183: bewusst KEIN delete hier — bei einem nur teilweise
         * aufgebauten Op-Graphen kann der Destruktor selbst hart abstürzen
         * (beobachtet: Guru Meditation Error beim Aufräumen nach fehlendem
         * Op, Boot-Loop, nur per physischem Download-Modus behebbar). Der
         * verwaiste Interpreter (paar hundert Byte internes Heap) bleibt bis
         * zum nächsten Reboot liegen — sicherer Trade-off gegenüber einem
         * Crash. Arena/RV-Arena (PSRAM, die eigentlich relevante Größe)
         * werden weiterhin sauber freigegeben. */
        free_model_override();
        heap_caps_free(s_rv_arena);
        s_rv_arena = nullptr;
        heap_caps_free(s_arena);
        s_arena = nullptr;
        return;
    }
    s_interpreter = interpreter;
    s_input       = s_interpreter->input(0);
    s_output      = s_interpreter->output(0);

    ESP_LOGI(TAG, "TFLite geladen: Arena %u KB, verwendet %u B.",
             (unsigned)(ARENA_SIZE / 1024),
             (unsigned)s_interpreter->arena_used_bytes());

    /* #181: Frame-Anzahl aus der tatsächlichen Input-Tensor-Größe ableiten
     * statt einen einzelnen 10ms-Streaming-Schritt anzunehmen — manche Modelle
     * (z.B. okay_nabu) erwarten mehrere aggregierte AudioFrontend-Frames als
     * einen Input. hey_hannah (Shape (1,1,40)) ergibt 1 — unverändertes
     * Verhalten. */
    if (s_input->bytes == 0 || s_input->bytes % FRONTEND_NUM_CHANNELS != 0) {
        ESP_LOGE(TAG, "Wakeword-Input-Tensor unerwartete Größe (%u B, nicht durch %u teilbar) — "
                      "Erkennung bleibt deaktiviert.",
                 (unsigned)s_input->bytes, (unsigned)FRONTEND_NUM_CHANNELS);
        tflite_deinit();
        return;
    }
    s_input_frame_count = s_input->bytes / FRONTEND_NUM_CHANNELS;
    ESP_LOGI(TAG, "Wakeword-Input: %u Frame(s) à %u Werte (%u B gesamt).",
             (unsigned)s_input_frame_count, (unsigned)FRONTEND_NUM_CHANNELS,
             (unsigned)s_input->bytes);
}

/* Gibt Arena, Interpreter und ggf. den Asset-Cache-Modell-Override frei. */
static void tflite_deinit(void)
{
    if (s_interpreter) {
        delete s_interpreter;
        s_interpreter = nullptr;
    }
    s_input         = nullptr;
    s_output        = nullptr;
    s_resource_vars = nullptr;
    s_input_frame_count = 1;

    free_model_override();

    if (s_rv_arena) {
        heap_caps_free(s_rv_arena);
        s_rv_arena = nullptr;
    }

    if (s_arena) {
        heap_caps_free(s_arena);
        s_arena = nullptr;
    }
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
    /* noise_reduction.min_signal_remaining bewusst NICHT überschrieben (#174):
     * FrontendFillConfigWithDefaults() setzt hier bereits 0.05, exakt der Wert,
     * den auch die Trainings-Pipeline (pymicro-features, MicroFrontend() ohne
     * Parameter) verwendet. Eine frühere IDF-6.0-Kompatibilitätsänderung hatte
     * das versehentlich auf 1.0 überschrieben (= Rauschunterdrückung
     * wirkungslos) und damit die Live-Features systematisch von den
     * Trainings-Features abweichen lassen — Modell hat nie ausgelöst. */

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

void hannah_wakeword_deinit(void)
{
    tflite_deinit();
    ESP_LOGI(TAG, "Wakeword deinitialisiert — %u KB PSRAM freigegeben.",
             (unsigned)(ARENA_SIZE / 1024));
}

void hannah_wakeword_reinit(void)
{
    tflite_init();
    if (s_interpreter) {
        ESP_LOGI(TAG, "Wakeword reaktiviert (Re-Init nach OTA-Fehlschlag).");
    } else {
        ESP_LOGE(TAG, "Wakeword-Reinit fehlgeschlagen — Erkennung bleibt deaktiviert.");
    }
}

float hannah_wakeword_process(const int16_t *pcm)
{
    if (!s_interpreter) return 0.0f;

    size_t num_read;
    struct FrontendOutput feat = FrontendProcessSamples(
        &s_frontend, pcm, WAKEWORD_STEP_SAMPLES, &num_read);

    s_last_feat_size = feat.size;
    s_last_num_read  = num_read;
    if (feat.size == 0) return 0.0f;   /* Noch kein vollständiger Frame */

    /* #181: bei Modellen mit mehr als einem Frame pro Input (z.B. okay_nabu)
     * ältere Frames nach vorne schieben, neuesten Frame hinten anhängen
     * (Sliding Window). Bei s_input_frame_count==1 (hey_hannah) ist der
     * memmove ein No-Op — identisch zum bisherigen Verhalten. Annahme:
     * Frame-Reihenfolge im Tensor ist zeitlich aufsteigend (älteste zuerst). */
    size_t tail_offset = (s_input_frame_count - 1) * feat.size;
    if (s_input_frame_count > 1) {
        memmove(s_input->data.int8, s_input->data.int8 + feat.size, tail_offset);
    }

    /* uint16 → int8 quantisieren, neuesten Frame ans Ende schreiben */
    for (size_t i = 0; i < feat.size; i++) {
        int32_t q = (int32_t)roundf((float)feat.values[i] / FEATURE_SCALE) + INPUT_ZERO_POINT;
        if      (q < -128) q = -128;
        else if (q >  127) q =  127;
        s_input->data.int8[tail_offset + i] = (int8_t)q;
        if (i < HANNAH_WAKEWORD_DEBUG_PREVIEW_LEN) {
            s_last_input_preview[i] = (int8_t)q;
            s_last_mel_preview[i]   = feat.values[i];
        }
    }

    if (s_interpreter->Invoke() != kTfLiteOk) {
        s_invoke_fail_count++;
        /* Nicht bei jedem Fehlschlag loggen (könnte pro 10ms-Frame passieren und
         * den Log fluten) — Zähler wird vom periodischen Debug-Log mitgeloggt. */
        return 0.0f;
    }

    s_last_output_raw = s_output->data.uint8[0];
    return (float)s_last_output_raw * OUTPUT_SCALE;
}

void hannah_wakeword_last_debug(hannah_wakeword_debug_t *out)
{
    if (!out) return;
    out->feat_size         = s_last_feat_size;
    out->num_read          = s_last_num_read;
    out->output_raw        = s_last_output_raw;
    out->invoke_fail_count = s_invoke_fail_count;
    memcpy(out->input_preview, s_last_input_preview, sizeof(out->input_preview));
    memcpy(out->mel_preview,   s_last_mel_preview,   sizeof(out->mel_preview));
}

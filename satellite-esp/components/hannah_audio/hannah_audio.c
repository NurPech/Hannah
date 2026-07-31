/**
 * hannah_audio — I2S Mic-Array, Speaker, Wake-Word / PTT, VAD
 *
 * Betriebsmodi (Kconfig HANNAH_WAKEWORD_ENABLED):
 *
 *   PTT-Modus (Standard bis Modell trainiert):
 *     GPIO-Taster halten → Aufnahme streamen → Loslassen → audio_end
 *
 *   Wakeword-Modus (nach Modell-Training):
 *     Kontinuierliche Inference → Wake-Word erkannt →
 *     Aufnahme streamen → Stille (VAD) → audio_end
 *
 * State Machine (Wakeword-Modus):
 *   IDLE → [Wake-Word > Threshold] → DETECTED → STREAMING →
 *   [Stille > VAD_SILENCE_MS]     → audio_end → IDLE
 */

#include "hannah_audio.h"
#include "hannah_config.h"
#include "hannah_net.h"
#include "hannah_led.h"
#include "hannah_wakeword.h"
#include "libhannah_audio.h"

#include <string.h>
#include <math.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "driver/gpio.h"
#include "driver/i2s_std.h"
#if CONFIG_HANNAH_MIC_TYPE_PDM
#include "driver/i2s_pdm.h"
#endif
#if CONFIG_HANNAH_MIC_TYPE_TDM
#include "driver/i2s_tdm.h"
#include "hannah_sensors.h"
#endif

static const char *TAG = "hannah_audio";

/* ------------------------------------------------------------------ */
/* Konstanten                                                            */

#define SAMPLE_RATE       CONFIG_HANNAH_AUDIO_SAMPLE_RATE
#define STEP_SAMPLES      WAKEWORD_STEP_SAMPLES          /* 160 (10ms)  */
#define STEP_BYTES_MONO (STEP_SAMPLES * 2)
#if !CONFIG_HANNAH_MIC_TYPE_NONE
#  if CONFIG_HANNAH_MIC_TYPE_PDM
#  define STEP_BYTES_RAW  (STEP_SAMPLES * 4)   /* 16-bit stereo PDM */
#  elif CONFIG_HANNAH_MIC_TYPE_TDM
#  define STEP_BYTES_RAW  (STEP_SAMPLES * 8)   /* 4× 16-bit TDM-Slots (ADAU7118) */
#  else
#  define STEP_BYTES_RAW  (STEP_SAMPLES * 8)   /* 32-bit slots I2S  */
#  endif
#endif

/* VAD_SILENCE_FRAMES wird zur Laufzeit aus hannah_config_get()->vad_silence_ms berechnet */

/* Speaker-Ring-Buffer (internes DRAM, NOSPLIT — kein malloc/free pro Chunk)
 * 32KB ≈ ~22 Chunks × 29ms @ 24kHz = ~640ms Buffer — I2S-DMA braucht cache-kohärenten Speicher */
#define SPK_RINGBUF_SIZE (32 * 1024)

/* Mic-Warmup: erste Frames nach Boot verwerfen (PDM-Transienten, Fehlauslöser) */
#define WARMUP_FRAMES 500  /* 500 × 10ms = 5s */

/* Sampling-Mode "noise": Auto-Flush-Intervall (Dauerstrom-Aufnahme) */
#define NOISE_AUTOFLUSH_FRAMES 5000  /* 5000 × 10ms = 50s */

/* ------------------------------------------------------------------ */
/* Typen                                                                 */

/* Ring-Buffer-Item: Header + inline PCM. len==0 → End-Sentinel. */
typedef struct {
    uint32_t len;
    uint8_t  data[];  /* flexible array — len Bytes PCM folgen direkt */
} spk_rb_item_t;

typedef enum {
    AUDIO_STATE_IDLE,
    AUDIO_STATE_STREAMING,
} audio_state_t;

/* ------------------------------------------------------------------ */
/* Zustand                                                               */

static i2s_chan_handle_t s_rx_chan    = NULL;
static i2s_chan_handle_t s_tx_chan    = NULL;
static RingbufHandle_t   s_spk_ringbuf = NULL;
static volatile bool     s_ptt_active        = false;
static volatile bool     s_streaming_paused  = false;
static volatile bool     s_wakeword_paused   = false;
static volatile bool     s_vol_up_req        = false;
static volatile bool     s_vol_down_req      = false;
static volatile bool     s_speaking_active        = false;
static volatile bool     s_listen_after_tts       = false;
static volatile int      s_virtual_listen_frames  = 0;
static volatile int      s_volume                 = CONFIG_HANNAH_VOLUME_DEFAULT;
static hannah_webrtc_vad_state_t s_webrtc_vad;
static float              s_noise_floor_ema = 0.020f; /* adaptiver Noise-Floor-Schätzer */
static int                s_stream_frames   = 0;

/* ------------------------------------------------------------------ */
/* Button ISRs                                                           */

static volatile bool s_mute_toggle_req    = false;
static volatile bool s_sampling_mode      = false;
static volatile bool s_sampling_hey_hannah = false;  /* true = hey_hannah, false = noise */

static void IRAM_ATTR mute_isr_handler(void *arg)
{
    if (gpio_get_level(CONFIG_HANNAH_MUTE_GPIO) == 0)
        s_mute_toggle_req = true;
}

static void IRAM_ATTR ptt_isr_handler(void *arg)
{
    s_ptt_active = (gpio_get_level(CONFIG_HANNAH_PTT_GPIO) == 0);
}

static void IRAM_ATTR vol_up_isr_handler(void *arg)
{
    if (gpio_get_level(CONFIG_HANNAH_VOL_UP_GPIO) == 0)
        s_vol_up_req = true;
}

static void IRAM_ATTR vol_down_isr_handler(void *arg)
{
    if (gpio_get_level(CONFIG_HANNAH_VOL_DOWN_GPIO) == 0)
        s_vol_down_req = true;
}

#if CONFIG_HANNAH_MIC_TYPE_TDM
/* ------------------------------------------------------------------ */
/* ADAU7118 — I2C-Steuerung (PDM→TDM-Wandler, PCB Rev.5+)               */

#define ADAU7118_I2C_ADDR  0x14  /* Datenblatt-Default, ADDR-Pin auf GND */

static esp_err_t adau7118_init(void)
{
    i2c_master_bus_handle_t bus = hannah_sensors_get_i2c_bus();
    if (!bus) {
        ESP_LOGE(TAG, "ADAU7118: I2C-Bus nicht verfügbar (hannah_sensors_init() nicht vor hannah_audio_init() aufgerufen?)");
        return ESP_ERR_INVALID_STATE;
    }

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = ADAU7118_I2C_ADDR,
        .scl_speed_hz    = 400000,
    };
    i2c_master_dev_handle_t dev;
    esp_err_t err = i2c_master_bus_add_device(bus, &dev_cfg, &dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADAU7118: I2C-Device konnte nicht angelegt werden (%s)", esp_err_to_name(err));
        return err;
    }

    /* TODO(#160): Register-Sequenz fehlt — ADAU7118-Datenblatt noch nicht
     * vorliegend. Nötig u.a.: TDM-Modus aktivieren, 4 PDM-Eingänge auf die
     * 4 TDM-Slots mappen, ggf. Hochpassfilter/Gain konfigurieren. Ohne
     * diese Konfiguration liefert der Chip vermutlich seinen Power-on-
     * Default (oft bereits ein sinnvoller TDM-Modus, aber nicht verifiziert) —
     * bewusst als No-Op belassen statt Register zu raten. */
    (void)dev;

    ESP_LOGW(TAG, "ADAU7118: I2C-Device angelegt, Register-Init ist Platzhalter (Refs #160)");
    return ESP_OK;
}
#endif /* CONFIG_HANNAH_MIC_TYPE_TDM */

/* ------------------------------------------------------------------ */
/* I2S Mic initialisieren (I2S0, RX, stereo, INMP441)                   */

#if !CONFIG_HANNAH_MIC_TYPE_NONE
static esp_err_t mic_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(
        CONFIG_HANNAH_MIC_I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num  = 8;
    chan_cfg.dma_frame_num = STEP_SAMPLES * 4;

    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &s_rx_chan));

#if CONFIG_HANNAH_MIC_TYPE_PDM
    i2s_pdm_rx_config_t pdm_cfg = {
        .clk_cfg  = I2S_PDM_RX_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .clk = (gpio_num_t)CONFIG_HANNAH_MIC_CLK_GPIO,
            .din = (gpio_num_t)CONFIG_HANNAH_MIC_DATA_GPIO,
            .invert_flags = { .clk_inv = true },
        },
    };
    pdm_cfg.clk_cfg.dn_sample_mode = I2S_PDM_DSR_16S;
    ESP_ERROR_CHECK(i2s_channel_init_pdm_rx_mode(s_rx_chan, &pdm_cfg));
    ESP_LOGI(TAG, "Mic PDM I2S%d: %dHz stereo DSR_16S", CONFIG_HANNAH_MIC_I2S_PORT, SAMPLE_RATE);
#elif CONFIG_HANNAH_MIC_TYPE_TDM
    ESP_ERROR_CHECK(adau7118_init());
    i2s_tdm_config_t tdm_cfg = {
        .clk_cfg  = I2S_TDM_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_TDM_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO,
            I2S_TDM_SLOT0 | I2S_TDM_SLOT1 | I2S_TDM_SLOT2 | I2S_TDM_SLOT3),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)CONFIG_HANNAH_MIC_BCK_GPIO,
            .ws   = (gpio_num_t)CONFIG_HANNAH_MIC_WS_GPIO,
            .dout = I2S_GPIO_UNUSED,
            .din  = (gpio_num_t)CONFIG_HANNAH_MIC_DATA_GPIO,
            .invert_flags = {false, false, false},
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_tdm_mode(s_rx_chan, &tdm_cfg));
    ESP_LOGI(TAG, "Mic TDM I2S%d: %dHz, 4 Slots (ADAU7118)", CONFIG_HANNAH_MIC_I2S_PORT, SAMPLE_RATE);
#else
    // INMP441 requires ≥32 BCLK cycles per channel — use 32-bit slot width.
    // Data sits in bits [31:8]; we shift down in mic_task.
    i2s_std_config_t std_cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)CONFIG_HANNAH_MIC_BCK_GPIO,
            .ws   = (gpio_num_t)CONFIG_HANNAH_MIC_WS_GPIO,
            .dout = I2S_GPIO_UNUSED,
            .din  = (gpio_num_t)CONFIG_HANNAH_MIC_DATA_GPIO,
            .invert_flags = {false, false, false},
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx_chan, &std_cfg));
    ESP_LOGI(TAG, "Mic I2S%d: %dHz stereo", CONFIG_HANNAH_MIC_I2S_PORT, SAMPLE_RATE);
#endif

    ESP_ERROR_CHECK(i2s_channel_enable(s_rx_chan));
    return ESP_OK;
}
#endif /* !CONFIG_HANNAH_MIC_TYPE_NONE */

/* ------------------------------------------------------------------ */
/* I2S Speaker initialisieren (I2S1, TX, mono, MAX98357A)               */

#if CONFIG_HANNAH_SPEAKER_ENABLED
static esp_err_t speaker_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(
        CONFIG_HANNAH_SPK_I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num  = 8;
    chan_cfg.dma_frame_num = STEP_SAMPLES * 4;
    chan_cfg.auto_clear    = true;

    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &s_tx_chan, NULL));

    i2s_std_config_t std_cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)CONFIG_HANNAH_SPK_BCK_GPIO,
            .ws   = (gpio_num_t)CONFIG_HANNAH_SPK_WS_GPIO,
            .dout = (gpio_num_t)CONFIG_HANNAH_SPK_DATA_GPIO,
            .din  = I2S_GPIO_UNUSED,
            .invert_flags = {false, false, false},
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx_chan, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx_chan));
    ESP_LOGI(TAG, "Speaker I2S%d: %dHz mono", CONFIG_HANNAH_SPK_I2S_PORT, SAMPLE_RATE);
    return ESP_OK;
}
#endif /* CONFIG_HANNAH_SPEAKER_ENABLED */

/* ------------------------------------------------------------------ */
/* Mic-Task                                                              */

#if !CONFIG_HANNAH_MIC_TYPE_NONE
static inline void mic_led(led_state_t state)
{
    if (!s_sampling_mode)
        hannah_led_set_state(state);
}

static void mic_task(void *arg)
{
    uint8_t  *raw  = malloc(STEP_BYTES_RAW);
    int16_t  *mono = malloc(STEP_BYTES_MONO);
    if (!raw || !mono) {
        ESP_LOGE(TAG, "mic_task: kein Speicher"); vTaskDelete(NULL); return;
    }

    audio_state_t state           = AUDIO_STATE_IDLE;
    bool          was_ptt         = false;
    int           warmup_remaining = WARMUP_FRAMES;

#if CONFIG_HANNAH_WAKEWORD_ENABLED
    ESP_LOGI(TAG, "Mic-Task: Wakeword-Modus (Schwelle=%.2f, VAD=%dms).",
             hannah_config_get()->wakeword_threshold / 100.0f,
             hannah_config_get()->vad_silence_ms);
#else
    ESP_LOGI(TAG, "Mic-Task: PTT-Modus.");
#endif

    while (1) {
        if (s_mute_toggle_req) {
            s_mute_toggle_req = false;
            hannah_net_set_mute(!hannah_net_is_muted());
            if (!hannah_net_is_muted())
                hannah_led_set_state(LED_STATE_IDLE);
        }
        if (s_vol_up_req) {
            s_vol_up_req = false;
            int v = s_volume + CONFIG_HANNAH_VOLUME_STEP;
            s_volume = v > 100 ? 100 : v;
            ESP_LOGI(TAG, "Lautstärke: %d%%", s_volume);
            hannah_net_publish_volume(s_volume);
        }
        if (s_vol_down_req) {
            s_vol_down_req = false;
            int v = s_volume - CONFIG_HANNAH_VOLUME_STEP;
            s_volume = v < 0 ? 0 : v;
            ESP_LOGI(TAG, "Lautstärke: %d%%", s_volume);
            hannah_net_publish_volume(s_volume);
        }

        size_t bytes_read = 0;
        i2s_channel_read(s_rx_chan, raw, STEP_BYTES_RAW,
                         &bytes_read, pdMS_TO_TICKS(200));

#if CONFIG_HANNAH_MIC_TYPE_PDM
        /* PDM: 16-bit stereo → linker Kanal (SPH0641: SEL=GND → L, Index 0) */
        size_t frames    = bytes_read / 4;
        int16_t *s16     = (int16_t *)raw;
        for (size_t i = 0; i < frames; i++) {
            mono[i] = (int16_t)((int32_t)s16[i * 2] * 64 > 32767 ? 32767 : (int32_t)s16[i * 2] * 64 < -32768 ? -32768 : (int32_t)s16[i * 2] * 64);
        }
#elif CONFIG_HANNAH_MIC_TYPE_TDM
        /* TDM: 4× 16-bit Slots (ADAU7118) → Slot 0 (ein fester Kanal von 4,
         * kein Beamforming — s. mic_init()-Kommentar / Issue #160) */
        size_t frames    = bytes_read / 8;
        int16_t *s16     = (int16_t *)raw;
        for (size_t i = 0; i < frames; i++) {
            mono[i] = s16[i * 4];
        }
#else
        /* I2S: 32-bit slots → linker Kanal (INMP441: MSB in bits[31:8]) */
        size_t frames    = bytes_read / 8;
        int32_t *s32     = (int32_t *)raw;
        for (size_t i = 0; i < frames; i++) {
            mono[i] = (int16_t)(s32[i * 2] >> 14);
        }
#endif
        size_t mono_samples = frames;

        /* Warmup: Frontend füttern aber nicht triggern */
        if (warmup_remaining > 0) {
            --warmup_remaining;
#if CONFIG_HANNAH_WAKEWORD_ENABLED
            hannah_wakeword_process(mono);
#endif
            /* Noise-Floor während Warmup kalibrieren (nicht während TTS) */
            if (!s_speaking_active) {
                float rms_warmup = hannah_rms(mono, (int)mono_samples);
                s_noise_floor_ema = s_noise_floor_ema * 0.99f + rms_warmup * 0.01f;
            }
            if (warmup_remaining == 0) {
                hannah_led_set_state(LED_STATE_IDLE);
                ESP_LOGI(TAG, "Mic-Warmup abgeschlossen. noise_ema=%.4f", s_noise_floor_ema);
            }
            vTaskDelay(pdMS_TO_TICKS(1));  /* taskYIELD reicht nicht — IDLE hat prio 0 und kommt bei vielen laufenden Boot-Tasks nie dran */
            continue;
        }

        if (hannah_net_is_muted()) {
            state = AUDIO_STATE_IDLE;
            if (!s_sampling_mode)
                hannah_led_set_state(LED_STATE_MUTE);
            was_ptt = false;
            if (!s_sampling_mode) {
                vTaskDelay(pdMS_TO_TICKS(1));
                continue;  /* Im Sampling-Mode: Audio trotz Mute streamen */
            }
        }

        if (s_streaming_paused) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

        if (s_wakeword_paused) {
            /* OTA läuft — Inference pausieren damit IDLE0 den WDT zurücksetzen kann */
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

/* -- Wakeword-Modus -------------------------------------------------- */
#if CONFIG_HANNAH_WAKEWORD_ENABLED
        {
            float confidence = hannah_wakeword_process(mono);

            if (s_sampling_mode) {
                if (s_sampling_hey_hannah) {
                    /* hey_hannah: nur bei PTT streamen, Flush bei PTT-Release */
                    if (s_ptt_active)
                        hannah_net_send_audio_sampling((uint8_t *)mono, mono_samples * 2);
                    if (was_ptt && !s_ptt_active)
                        hannah_net_send_audio_end();
                } else {
                    /* noise: Dauerstrom, Auto-Flush alle 50s, Pre-Flush bei PTT-Press */
                    if (!was_ptt && s_ptt_active && s_stream_frames > 0) {
                        hannah_net_send_audio_end();
                        s_stream_frames = 0;
                    }
                    hannah_net_send_audio_sampling((uint8_t *)mono, mono_samples * 2);
                    s_stream_frames++;
                    bool ptt_flush  = (was_ptt && !s_ptt_active);
                    bool auto_flush = (s_stream_frames >= NOISE_AUTOFLUSH_FRAMES);
                    if (ptt_flush || auto_flush) {
                        hannah_net_send_audio_end();
                        s_stream_frames = 0;
                    }
                }
                was_ptt = s_ptt_active;
                vTaskDelay(pdMS_TO_TICKS(1));
                continue;
            }

            switch (state) {
            case AUDIO_STATE_IDLE: {
                /* Noise-Floor-Tracking: schneller Anstieg, langsamer Abfall.
                 * Frames > 0.05 (Wakeword-Sprache) werden ignoriert. */
                float rms_idle = hannah_rms(mono, (int)mono_samples);
                /* Guard: Sprach-/Speaker-Frames ausschließen */
                if (!s_speaking_active && rms_idle < 0.05f) {
                    if (rms_idle > s_noise_floor_ema)
                        s_noise_floor_ema = s_noise_floor_ema * 0.90f + rms_idle * 0.10f;
                    else
                        s_noise_floor_ema = s_noise_floor_ema * 0.999f + rms_idle * 0.001f;
                }

                /* Wakeword-Debug: alle ~500ms komplette Diagnosekette loggen (#173) —
                 * rms/peak (kommt überhaupt Audio an, clippt es?), confidence (wie nah
                 * ans Threshold?), feat_size/num_read (liefert das AudioFrontend
                 * vollständige Frames?), mel_preview (rohe Frontend-Werte, 1:1 gegen
                 * pymicro_features/test_inference.py vergleichbar), input_preview
                 * (quantisierte Werte, die tatsächlich ins Modell gingen), output_raw
                 * (unskalierter Modell-Output) und invoke_fail_count (falls Invoke()
                 * selbst scheitert, aktuell sonst lautlos als confidence=0.0 sichtbar).
                 * Bewusst viel auf einmal statt mehrerer Debug-Release-Runden. */
                static int    s_wakeword_debug_ctr      = 0;
                static float  s_wakeword_debug_max      = 0.0f;
                static float  s_wakeword_debug_peak     = 0.0f;
                static size_t s_wakeword_debug_min_feat = SIZE_MAX;
                if (confidence > s_wakeword_debug_max) s_wakeword_debug_max = confidence;
                for (size_t i = 0; i < mono_samples; i++) {
                    float a = fabsf((float)mono[i]) / 32768.0f;
                    if (a > s_wakeword_debug_peak) s_wakeword_debug_peak = a;
                }
                hannah_wakeword_debug_t wwdbg;
                hannah_wakeword_last_debug(&wwdbg);
                if (wwdbg.feat_size < s_wakeword_debug_min_feat) s_wakeword_debug_min_feat = wwdbg.feat_size;
                if (++s_wakeword_debug_ctr >= 50) {
                    s_wakeword_debug_ctr = 0;
                    ESP_LOGI(TAG, "Wakeword-Debug: rms=%.4f peak=%.4f confidence(peak/500ms)=%.4f threshold=%.2f "
                                  "noise_ema=%.4f feat_size(min/500ms)=%u num_read=%u output_raw=%u invoke_fails=%lu",
                             rms_idle, s_wakeword_debug_peak, s_wakeword_debug_max,
                             hannah_config_get()->wakeword_threshold / 100.0f, s_noise_floor_ema,
                             (unsigned)s_wakeword_debug_min_feat, (unsigned)wwdbg.num_read,
                             (unsigned)wwdbg.output_raw, (unsigned long)wwdbg.invoke_fail_count);
                    ESP_LOGI(TAG, "Wakeword-Debug mel[0..9]=%u,%u,%u,%u,%u,%u,%u,%u,%u,%u",
                             (unsigned)wwdbg.mel_preview[0], (unsigned)wwdbg.mel_preview[1],
                             (unsigned)wwdbg.mel_preview[2], (unsigned)wwdbg.mel_preview[3],
                             (unsigned)wwdbg.mel_preview[4], (unsigned)wwdbg.mel_preview[5],
                             (unsigned)wwdbg.mel_preview[6], (unsigned)wwdbg.mel_preview[7],
                             (unsigned)wwdbg.mel_preview[8], (unsigned)wwdbg.mel_preview[9]);
                    ESP_LOGI(TAG, "Wakeword-Debug input[0..9]=%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
                             wwdbg.input_preview[0], wwdbg.input_preview[1], wwdbg.input_preview[2],
                             wwdbg.input_preview[3], wwdbg.input_preview[4], wwdbg.input_preview[5],
                             wwdbg.input_preview[6], wwdbg.input_preview[7], wwdbg.input_preview[8],
                             wwdbg.input_preview[9]);
                    s_wakeword_debug_max      = 0.0f;
                    s_wakeword_debug_peak     = 0.0f;
                    s_wakeword_debug_min_feat = SIZE_MAX;
                }

                /* PTT oder Wake-Word → Streaming starten */
                if ((s_ptt_active && !was_ptt) ||
                    confidence >= hannah_config_get()->wakeword_threshold / 100.0f) {
                    bool  by_wakeword  = !(s_ptt_active && !was_ptt);
                    float min_thr      = CONFIG_HANNAH_VAD_ENERGY_THRESHOLD / 32767.0f;
                    float adaptive_thr = s_noise_floor_ema * 2.0f;
                    if (adaptive_thr < min_thr) adaptive_thr = min_thr;
                    int vad_silence_frames = (int)(hannah_config_get()->vad_silence_ms / 10);
                    mic_led(LED_STATE_WAKE);
                    vTaskDelay(pdMS_TO_TICKS(150));
                    mic_led(LED_STATE_STREAM);
                    if (hannah_webrtc_vad_init(&s_webrtc_vad,
                            CONFIG_HANNAH_VAD_WEBRTC_AGGRESSIVENESS,
                            SAMPLE_RATE, 3, vad_silence_frames) != 0)
                        ESP_LOGE(TAG, "WebRTC VAD init fehlgeschlagen.");
                    if (by_wakeword) s_webrtc_vad.speaking = 1;
                    s_stream_frames = 0;
                    ESP_LOGI(TAG, "%s erkannt → Streaming. (noise_ema=%.4f)",
                             by_wakeword ? "Wake-Word" : "PTT", s_noise_floor_ema);
                    state = AUDIO_STATE_STREAMING;
                }
                break;
            }

            case AUDIO_STATE_STREAMING: {
                static int s_rms_log_ctr = 0;
                hannah_net_send_audio((uint8_t *)mono, mono_samples * 2);
                s_stream_frames++;
                if (++s_rms_log_ctr >= 50) {
                    s_rms_log_ctr = 0;
                    ESP_LOGI(TAG, "VAD RMS=%.4f silence=%d/%d",
                             hannah_rms(mono, (int)mono_samples),
                             s_webrtc_vad.offset_count, s_webrtc_vad.offset_windows);
                }
                bool ptt_end   = (was_ptt && !s_ptt_active);
                bool vad_end   = (!ptt_end &&
                                  s_stream_frames >= 200 &&  /* mind. 2s nach Wakeword bevor VAD abschneiden darf */
                                  hannah_webrtc_vad_feed(&s_webrtc_vad, mono, (int)mono_samples) == HANNAH_VAD_OFFSET);
                bool timed_out = (s_stream_frames >= 1000);  /* 10s Hard-Limit */
                if (ptt_end || vad_end || timed_out) {
                    hannah_net_send_audio_end();
                    hannah_webrtc_vad_free(&s_webrtc_vad);
                    mic_led(LED_STATE_IDLE);
                    state = AUDIO_STATE_IDLE;
                    if (s_virtual_listen_frames > 0) {
                        s_virtual_listen_frames = 0;
                        s_ptt_active = false;
                    }
                    s_rms_log_ctr = 0;
                    if (ptt_end)       ESP_LOGI(TAG, "PTT losgelassen → audio_end.");
                    else if (vad_end)  ESP_LOGI(TAG, "VAD: Stille erkannt → audio_end.");
                    else               ESP_LOGI(TAG, "Stream-Timeout (10s) → audio_end.");
                }
                break;
            }
            }
        }
        was_ptt = s_ptt_active;

/* -- PTT-Modus (Wakeword nicht kompiliert) -------------------------- */
#else
        bool ptt = s_ptt_active;

        if (s_sampling_mode) {
            if (s_sampling_hey_hannah) {
                /* hey_hannah: nur bei PTT streamen, Flush bei PTT-Release */
                if (ptt)
                    hannah_net_send_audio_sampling((uint8_t *)mono, mono_samples * 2);
                if (was_ptt && !ptt)
                    hannah_net_send_audio_end();
            } else {
                /* noise: Dauerstrom, Auto-Flush alle 50s, Pre-Flush bei PTT-Press */
                if (!was_ptt && ptt && s_stream_frames > 0) {
                    hannah_net_send_audio_end();
                    s_stream_frames = 0;
                }
                hannah_net_send_audio_sampling((uint8_t *)mono, mono_samples * 2);
                s_stream_frames++;
                bool ptt_flush  = (was_ptt && !ptt);
                bool auto_flush = (s_stream_frames >= NOISE_AUTOFLUSH_FRAMES);
                if (ptt_flush || auto_flush) {
                    hannah_net_send_audio_end();
                    s_stream_frames = 0;
                }
            }
        } else {
            if (!was_ptt && ptt) {
                mic_led(LED_STATE_STREAM);
                state = AUDIO_STATE_STREAMING;
            }
            if (state == AUDIO_STATE_STREAMING && ptt) {
                hannah_net_send_audio((uint8_t *)mono, mono_samples * 2);
                if (s_virtual_listen_frames > 0 && --s_virtual_listen_frames == 0)
                    s_ptt_active = false;
            }
            if (was_ptt && !ptt && state == AUDIO_STATE_STREAMING) {
                hannah_net_send_audio_end();
                mic_led(LED_STATE_IDLE);
                state = AUDIO_STATE_IDLE;
                s_virtual_listen_frames = 0;
            }
        }

        was_ptt = ptt;
#endif
        vTaskDelay(pdMS_TO_TICKS(1));  /* taskYIELD reicht nicht — IDLE0 sonst Watchdog-Timeout */
    }

    free(raw);
    free(mono);
    vTaskDelete(NULL);
}
#endif /* !CONFIG_HANNAH_MIC_TYPE_NONE */

/* ------------------------------------------------------------------ */
/* Speaker-Task                                                          */

#if CONFIG_HANNAH_SPEAKER_ENABLED
static void speaker_task(void *arg)
{
    bool was_speaking = false;
    char playback_done_topic[96];
    snprintf(playback_done_topic, sizeof(playback_done_topic),
             "hannah/satellite/%s/playback_done", hannah_config_get()->device_id);
    ESP_LOGI(TAG, "Speaker-Task gestartet.");
    while (1) {
        size_t item_size;
        spk_rb_item_t *item = xRingbufferReceive(s_spk_ringbuf, &item_size,
                                                   pdMS_TO_TICKS(1000));
        if (!item) {
            /* Timeout — keine neuen Chunks seit 1s → TTS abgeschlossen */
            if (was_speaking) {
                was_speaking = false;
                s_speaking_active = false;
                if (!s_sampling_mode)
                    hannah_led_set_state(LED_STATE_IDLE);
            }
            continue;
        }
        if (item->len == 0) {
            /* End-Sentinel: DMA-Buffer drainieren */
            static const uint8_t silence[8 * STEP_SAMPLES * 4 * 2] = {0};
            size_t written;
            i2s_channel_write(s_tx_chan, silence, sizeof(silence),
                              &written, portMAX_DELAY);
            vRingbufferReturnItem(s_spk_ringbuf, item);
            was_speaking = false;
            s_speaking_active = false;
            /* Generisches "Wiedergabe fertig"-Signal an Core — DMA ist an dieser
             * Stelle wirklich drained, nicht nur der Ringbuffer geleert. Core kann
             * darauf warten statt eine feste Verzögerung zu raten (z.B. TriggerPlink). */
            hannah_net_mqtt_publish(playback_done_topic, "{}", 1, 0);
            if (!s_sampling_mode)
                hannah_led_set_state(LED_STATE_IDLE);
            if (s_listen_after_tts && !s_sampling_mode) {
                s_listen_after_tts = false;
                s_virtual_listen_frames = 800;  /* 800 × 10ms = 8s */
                s_ptt_active = true;
            }
            continue;
        }
        was_speaking = true;
        s_speaking_active = true;
        /* Lautstärke-Skalierung in-place (Ring-Buffer-Speicher ist schreibbar) */
        int vol = s_volume;
        if (vol < 100) {
            int16_t *samples = (int16_t *)item->data;
            size_t count = item->len / 2;
            for (size_t i = 0; i < count; i++)
                samples[i] = (int16_t)(((int32_t)samples[i] * vol) / 100);
        }
        size_t written;
        i2s_channel_write(s_tx_chan, item->data, item->len,
                          &written, pdMS_TO_TICKS(500));
        vRingbufferReturnItem(s_spk_ringbuf, item);
    }
}
#endif /* CONFIG_HANNAH_SPEAKER_ENABLED */

/* ------------------------------------------------------------------ */
/* hannah_net Callbacks                                                  */

static void on_sampling_mode(bool enabled, const char *sample_type)
{
    s_sampling_mode       = enabled;
    s_sampling_hey_hannah = enabled && sample_type && strcmp(sample_type, "hey_hannah") == 0;
    s_stream_frames = 0;
    if (enabled) {
        hannah_audio_stop();  /* laufende TTS-Queue leeren */
        hannah_led_set_state(LED_STATE_CAPTURE);
        ESP_LOGI(TAG, "Capture-Modus aktiviert — type=%s, LED lila", sample_type ? sample_type : "noise");
    } else {
        hannah_led_set_state(hannah_net_is_muted() ? LED_STATE_MUTE : LED_STATE_IDLE);
        ESP_LOGI(TAG, "Capture-Modus deaktiviert — normaler Betrieb");
    }
}

void hannah_audio_set_sampling_mode(bool enabled)
{
    on_sampling_mode(enabled, "noise");
}

static void on_tts_data(const uint8_t *pcm, size_t len)
{
    hannah_audio_play(pcm, len, SAMPLE_RATE);
}

static void on_tts_end(int sample_rate)
{
    (void)sample_rate;
    hannah_audio_play_end();
}

void hannah_audio_start_listen_after_tts(void)
{
    if (!s_speaking_active && !s_sampling_mode) {
        s_virtual_listen_frames = 800;
        s_ptt_active = true;
    } else {
        s_listen_after_tts = true;
    }
}

static void on_status(const char *state)
{
    if (s_sampling_mode) return;
    ESP_LOGI(TAG, "Server-Status: %s", state);
    if      (strcmp(state, "listening")  == 0) hannah_led_set_state(LED_STATE_STREAM);
    else if (strcmp(state, "processing") == 0) hannah_led_set_state(LED_STATE_WAKE);
    else if (strcmp(state, "speaking")   == 0) hannah_led_set_state(LED_STATE_SPEAK);
    else if (strcmp(state, "idle")       == 0) {
        if (!s_speaking_active)
            hannah_led_set_state(hannah_net_is_muted() ? LED_STATE_MUTE : LED_STATE_IDLE);
    }
}

static void on_playback_cmd(const char *cmd)
{
    ESP_LOGI(TAG, "Playback-Befehl: %s", cmd);
    if      (strcmp(cmd, "stop")   == 0) hannah_audio_stop();
    else if (strcmp(cmd, "pause")  == 0) hannah_audio_pause();
    else if (strcmp(cmd, "resume") == 0) hannah_audio_resume();
}

static void on_virtual_ptt(bool active)
{
    s_ptt_active = active;
}

static void on_hw_mute(bool muted)
{
    gpio_set_level(CONFIG_HANNAH_MUTE_HW_GPIO, muted ? 0 : 1);
    if (!s_sampling_mode)
        hannah_led_set_state(muted ? LED_STATE_MUTE : LED_STATE_IDLE);
}

static void on_volume_set(int vol)
{
    s_volume = vol;
    ESP_LOGI(TAG, "Lautstärke gesetzt: %d%%", s_volume);
}

/* ------------------------------------------------------------------ */
/* Öffentliche API                                                       */

void hannah_audio_init(void)
{
#if CONFIG_HANNAH_SPEAKER_ENABLED
    /* Internes DRAM — I2S-DMA benötigt cache-kohärenten Speicher; PSRAM ist nicht geeignet. */
    s_spk_ringbuf = xRingbufferCreate(SPK_RINGBUF_SIZE, RINGBUF_TYPE_NOSPLIT);
    speaker_init();
#endif
#if !CONFIG_HANNAH_MIC_TYPE_NONE
    mic_init();
#endif

    /* Mute-Button: Input mit Pull-up, Interrupt auf fallende Flanke */
    gpio_config_t io_cfg = {
        .pin_bit_mask = (1ULL << CONFIG_HANNAH_MUTE_GPIO),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_NEGEDGE,
    };
    ESP_ERROR_CHECK(gpio_config(&io_cfg));
    ESP_ERROR_CHECK(gpio_install_isr_service(0));
    ESP_ERROR_CHECK(gpio_isr_handler_add(
        CONFIG_HANNAH_MUTE_GPIO, mute_isr_handler, NULL));

    /* PTT-Button: ANYEDGE — Press und Release erkennen */
    gpio_config_t ptt_cfg = {
        .pin_bit_mask = (1ULL << CONFIG_HANNAH_PTT_GPIO),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_ANYEDGE,
    };
    ESP_ERROR_CHECK(gpio_config(&ptt_cfg));
    ESP_ERROR_CHECK(gpio_isr_handler_add(
        CONFIG_HANNAH_PTT_GPIO, ptt_isr_handler, NULL));

    /* Vol+/Vol-: fallende Flanke */
    gpio_config_t vol_cfg = {
        .pin_bit_mask = (1ULL << CONFIG_HANNAH_VOL_UP_GPIO) |
                        (1ULL << CONFIG_HANNAH_VOL_DOWN_GPIO),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_NEGEDGE,
    };
    ESP_ERROR_CHECK(gpio_config(&vol_cfg));
    ESP_ERROR_CHECK(gpio_isr_handler_add(
        CONFIG_HANNAH_VOL_UP_GPIO, vol_up_isr_handler, NULL));
    ESP_ERROR_CHECK(gpio_isr_handler_add(
        CONFIG_HANNAH_VOL_DOWN_GPIO, vol_down_isr_handler, NULL));

    /* Hardware-Mute Ausgang: Mics standardmäßig aktiv (HIGH) */
    gpio_config_t hw_mute_cfg = {
        .pin_bit_mask = (1ULL << CONFIG_HANNAH_MUTE_HW_GPIO),
        .mode         = GPIO_MODE_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&hw_mute_cfg));
    gpio_set_level(CONFIG_HANNAH_MUTE_HW_GPIO, 1);

#if CONFIG_HANNAH_WAKEWORD_ENABLED
    hannah_wakeword_init();
#endif

#if CONFIG_HANNAH_SPEAKER_ENABLED
    hannah_net_set_tts_callback(on_tts_data);
    hannah_net_set_tts_end_callback(on_tts_end);
    hannah_net_set_playback_callback(on_playback_cmd);
#endif
    hannah_net_set_status_callback(on_status);
    hannah_net_set_hw_mute_callback(on_hw_mute);
    hannah_net_set_volume_callback(on_volume_set);
#if !CONFIG_HANNAH_MIC_TYPE_NONE
    hannah_net_set_sampling_callback(on_sampling_mode);
    hannah_net_set_virtual_ptt_callback(on_virtual_ptt);
    hannah_net_set_start_listening_callback(hannah_audio_start_listen_after_tts);
    xTaskCreatePinnedToCore(mic_task, "mic", 8192, NULL, 5, NULL, 0);
#else
    hannah_led_set_state(LED_STATE_IDLE);
#endif
#if CONFIG_HANNAH_SPEAKER_ENABLED
    xTaskCreatePinnedToCore(speaker_task, "speaker", 4096, NULL, 5, NULL, 1);
#endif

    ESP_LOGI(TAG, "hannah_audio initialisiert (Mic=%s, Speaker=%s).",
#if CONFIG_HANNAH_MIC_TYPE_NONE
             "none",
#elif CONFIG_HANNAH_MIC_TYPE_PDM
             "PDM",
#elif CONFIG_HANNAH_MIC_TYPE_TDM
             "TDM",
#else
             "I2S",
#endif
#if CONFIG_HANNAH_SPEAKER_ENABLED
             "an"
#else
             "aus"
#endif
    );
}

void hannah_audio_play(const uint8_t *pcm, size_t len, int sample_rate)
{
    if (!s_spk_ringbuf || !pcm || len == 0) return;
    if (s_sampling_mode && !s_sampling_hey_hannah) return;
    spk_rb_item_t *item;
    if (xRingbufferSendAcquire(s_spk_ringbuf, (void **)&item,
                                sizeof(spk_rb_item_t) + len,
                                pdMS_TO_TICKS(2000)) != pdTRUE) {
        ESP_LOGW(TAG, "play: Ring-Buffer voll — Chunk verworfen.");
        return;
    }
    item->len = (uint32_t)len;
    memcpy(item->data, pcm, len);
    xRingbufferSendComplete(s_spk_ringbuf, item);
}

void hannah_audio_play_end(void)
{
    if (!s_spk_ringbuf) return;
    spk_rb_item_t *item;
    if (xRingbufferSendAcquire(s_spk_ringbuf, (void **)&item,
                                sizeof(spk_rb_item_t),
                                pdMS_TO_TICKS(50)) != pdTRUE) {
        ESP_LOGW(TAG, "play_end: Ring-Buffer voll — Sentinel verworfen.");
        return;
    }
    item->len = 0;  /* sentinel: len==0 signalisiert End-of-Stream */
    xRingbufferSendComplete(s_spk_ringbuf, item);
}

void hannah_audio_stop(void)
{
    s_streaming_paused = false;
    /* Ring-Buffer drainieren */
    if (s_spk_ringbuf) {
        size_t item_size;
        void *item;
        while ((item = xRingbufferReceive(s_spk_ringbuf, &item_size, 0)) != NULL)
            vRingbufferReturnItem(s_spk_ringbuf, item);
    }
    if (!s_sampling_mode)
        hannah_led_set_state(LED_STATE_IDLE);
}

void hannah_audio_pause(void)
{
    s_streaming_paused = true;
    hannah_led_set_state(LED_STATE_IDLE);
}

void hannah_audio_resume(void)
{
    s_streaming_paused = false;
}

void hannah_audio_pause_wakeword(void)
{
    s_wakeword_paused = true;
    ESP_LOGI(TAG, "Wakeword-Inference pausiert (OTA aktiv).");
}

void hannah_audio_resume_wakeword(void)
{
    s_wakeword_paused = false;
    ESP_LOGI(TAG, "Wakeword-Inference fortgesetzt (OTA fehlgeschlagen).");
}

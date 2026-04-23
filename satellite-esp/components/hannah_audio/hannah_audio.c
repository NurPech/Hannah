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
#include "hannah_net.h"
#include "hannah_led.h"
#include "hannah_wakeword.h"

#include <string.h>
#include <math.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "driver/gpio.h"
#include "driver/i2s_std.h"

static const char *TAG = "hannah_audio";

/* ------------------------------------------------------------------ */
/* Konstanten                                                            */

#define SAMPLE_RATE       CONFIG_HANNAH_AUDIO_SAMPLE_RATE
#define STEP_SAMPLES      WAKEWORD_STEP_SAMPLES          /* 160 (10ms)  */
#define STEP_BYTES_MONO   (STEP_SAMPLES * 2)
#define STEP_BYTES_STEREO (STEP_SAMPLES * 4)

/* VAD: wie viele 10ms-Frames ohne Sprache bis audio_end */
#define VAD_SILENCE_FRAMES \
    (CONFIG_HANNAH_VAD_SILENCE_MS / 10)

/* Speaker-Queue */
#define SPK_QUEUE_DEPTH 32

/* ------------------------------------------------------------------ */
/* Typen                                                                 */

typedef struct {
    uint8_t *data;
    size_t   len;
    bool     is_end;
} spk_chunk_t;

typedef enum {
    AUDIO_STATE_IDLE,
    AUDIO_STATE_STREAMING,
} audio_state_t;

/* ------------------------------------------------------------------ */
/* Zustand                                                               */

static i2s_chan_handle_t s_rx_chan    = NULL;
static i2s_chan_handle_t s_tx_chan    = NULL;
static QueueHandle_t     s_spk_queue = NULL;
static volatile bool     s_ptt_active  = false;
static volatile bool     s_streaming_paused = false;

/* ------------------------------------------------------------------ */
/* PTT GPIO ISR                                                          */

static void IRAM_ATTR ptt_isr_handler(void *arg)
{
    s_ptt_active = (gpio_get_level(CONFIG_HANNAH_MUTE_GPIO) == 0);
}

/* ------------------------------------------------------------------ */
/* I2S Mic initialisieren (I2S0, RX, stereo, INMP441)                   */

static esp_err_t mic_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(
        CONFIG_HANNAH_MIC_I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num  = 8;
    chan_cfg.dma_frame_num = STEP_SAMPLES * 4;  /* Buffer für ~4 Frames */

    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &s_rx_chan));

    i2s_std_config_t std_cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
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
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx_chan));
    ESP_LOGI(TAG, "Mic I2S%d: %dHz stereo", CONFIG_HANNAH_MIC_I2S_PORT, SAMPLE_RATE);
    return ESP_OK;
}

/* ------------------------------------------------------------------ */
/* I2S Speaker initialisieren (I2S1, TX, mono, MAX98357A)               */

static esp_err_t speaker_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(
        CONFIG_HANNAH_SPK_I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num  = 8;
    chan_cfg.dma_frame_num = STEP_SAMPLES * 4;

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

/* ------------------------------------------------------------------ */
/* VAD: einfache Energie-basierte Stille-Erkennung                      */

static bool vad_is_silence(const int16_t *pcm, size_t samples)
{
    int64_t sum = 0;
    for (size_t i = 0; i < samples; i++) {
        sum += (int32_t)pcm[i] * pcm[i];
    }
    float rms = sqrtf((float)sum / samples);
    return rms < (float)CONFIG_HANNAH_VAD_ENERGY_THRESHOLD;
}

/* ------------------------------------------------------------------ */
/* Mic-Task                                                              */

static void mic_task(void *arg)
{
    uint8_t  *stereo = malloc(STEP_BYTES_STEREO);
    int16_t  *mono   = malloc(STEP_BYTES_MONO);
    if (!stereo || !mono) {
        ESP_LOGE(TAG, "mic_task: kein Speicher"); vTaskDelete(NULL); return;
    }

    audio_state_t state          = AUDIO_STATE_IDLE;
    int           silence_frames = 0;
    bool          was_ptt        = false;

#if CONFIG_HANNAH_WAKEWORD_ENABLED
    ESP_LOGI(TAG, "Mic-Task: Wakeword-Modus (Schwelle=%.2f, VAD=%dms).",
             CONFIG_HANNAH_WAKEWORD_THRESHOLD / 100.0f,
             CONFIG_HANNAH_VAD_SILENCE_MS);
#else
    ESP_LOGI(TAG, "Mic-Task: PTT-Modus.");
#endif

    while (1) {
        /* I2S lesen: 10ms stereo */
        size_t bytes_read = 0;
        i2s_channel_read(s_rx_chan, stereo, STEP_BYTES_STEREO,
                         &bytes_read, pdMS_TO_TICKS(200));

        /* Stereo → Mono: linken Kanal (Bytes 0+1 jedes 4-Byte-Frames) */
        size_t frames = bytes_read / 4;
        int16_t *s16  = (int16_t *)stereo;
        for (size_t i = 0; i < frames; i++) {
            mono[i] = s16[i * 2];
        }
        size_t mono_samples = frames;

        if (hannah_net_is_muted()) {
            state = AUDIO_STATE_IDLE;
            hannah_led_set_state(LED_STATE_MUTE);
            was_ptt = false;
            continue;
        }

        if (s_streaming_paused) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

/* -- Wakeword-Modus -------------------------------------------------- */
#if CONFIG_HANNAH_WAKEWORD_ENABLED
        float confidence = hannah_wakeword_process(mono);

        switch (state) {
        case AUDIO_STATE_IDLE:
            /* Wake-Word erkannt? */
            if (confidence >= CONFIG_HANNAH_WAKEWORD_THRESHOLD / 100.0f) {
                ESP_LOGI(TAG, "Wake-Word erkannt (confidence=%.2f)", confidence);
                hannah_led_set_state(LED_STATE_WAKE);
                vTaskDelay(pdMS_TO_TICKS(150));   /* kurze Pause nach Wake */
                hannah_led_set_state(LED_STATE_STREAM);
                silence_frames = 0;
                state = AUDIO_STATE_STREAMING;
            }
            break;

        case AUDIO_STATE_STREAMING:
            /* Audio senden */
            hannah_net_send_audio((uint8_t *)mono, mono_samples * 2);

            /* VAD: Stille zählen */
            if (vad_is_silence(mono, mono_samples)) {
                silence_frames++;
                if (silence_frames >= VAD_SILENCE_FRAMES) {
                    hannah_net_send_audio_end();
                    hannah_led_set_state(LED_STATE_IDLE);
                    silence_frames = 0;
                    state = AUDIO_STATE_IDLE;
                    ESP_LOGD(TAG, "VAD: Stille erkannt → audio_end.");
                }
            } else {
                silence_frames = 0;   /* Sprache → Zähler zurücksetzen */
            }
            break;
        }

/* -- PTT-Modus (Fallback) ------------------------------------------- */
#else
        bool ptt = s_ptt_active;

        if (!was_ptt && ptt) {
            /* Taste gedrückt → Streaming starten */
            hannah_led_set_state(LED_STATE_STREAM);
            state = AUDIO_STATE_STREAMING;
        }

        if (state == AUDIO_STATE_STREAMING && ptt) {
            hannah_net_send_audio((uint8_t *)mono, mono_samples * 2);
        }

        if (was_ptt && !ptt && state == AUDIO_STATE_STREAMING) {
            /* Taste losgelassen → audio_end */
            hannah_net_send_audio_end();
            hannah_led_set_state(LED_STATE_IDLE);
            state = AUDIO_STATE_IDLE;
        }

        was_ptt = ptt;
#endif
    }

    free(stereo);
    free(mono);
    vTaskDelete(NULL);
}

/* ------------------------------------------------------------------ */
/* Speaker-Task                                                          */

static void speaker_task(void *arg)
{
    spk_chunk_t chunk;
    ESP_LOGI(TAG, "Speaker-Task gestartet.");
    while (1) {
        if (xQueueReceive(s_spk_queue, &chunk, portMAX_DELAY) != pdTRUE) continue;
        if (chunk.is_end) {
            /* Kurze Stille nach TTS damit letzter Chunk abklingt */
            uint8_t silence[STEP_BYTES_MONO] = {0};
            size_t written;
            i2s_channel_write(s_tx_chan, silence, sizeof(silence),
                              &written, pdMS_TO_TICKS(100));
            hannah_led_set_state(LED_STATE_IDLE);
            continue;
        }
        if (!chunk.data) continue;
        size_t written;
        i2s_channel_write(s_tx_chan, chunk.data, chunk.len,
                          &written, pdMS_TO_TICKS(500));
        free(chunk.data);
    }
}

/* ------------------------------------------------------------------ */
/* hannah_net Callbacks                                                  */

static void on_tts_data(const uint8_t *pcm, size_t len)
{
    hannah_audio_play(pcm, len, SAMPLE_RATE);
}

static void on_tts_end(int sample_rate)
{
    (void)sample_rate;
    hannah_audio_play_end();
}

static void on_status(const char *state)
{
    ESP_LOGI(TAG, "Server-Status: %s", state);
    if      (strcmp(state, "listening")  == 0) hannah_led_set_state(LED_STATE_STREAM);
    else if (strcmp(state, "processing") == 0) hannah_led_set_state(LED_STATE_WAKE);
    else if (strcmp(state, "speaking")   == 0) hannah_led_set_state(LED_STATE_SPEAK);
    else if (strcmp(state, "idle")       == 0)
        hannah_led_set_state(hannah_net_is_muted() ? LED_STATE_MUTE : LED_STATE_IDLE);
}

static void on_playback_cmd(const char *cmd)
{
    ESP_LOGI(TAG, "Playback-Befehl: %s", cmd);
    if      (strcmp(cmd, "stop")   == 0) hannah_audio_stop();
    else if (strcmp(cmd, "pause")  == 0) hannah_audio_pause();
    else if (strcmp(cmd, "resume") == 0) hannah_audio_resume();
}

/* ------------------------------------------------------------------ */
/* Öffentliche API                                                       */

void hannah_audio_init(void)
{
    s_spk_queue = xQueueCreate(SPK_QUEUE_DEPTH, sizeof(spk_chunk_t));

    mic_init();
    speaker_init();

    /* PTT GPIO (auch im Wakeword-Modus als Notfall-Fallback nutzbar) */
    gpio_config_t io_cfg = {
        .pin_bit_mask = (1ULL << CONFIG_HANNAH_MUTE_GPIO),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_ANYEDGE,
    };
    ESP_ERROR_CHECK(gpio_config(&io_cfg));
    ESP_ERROR_CHECK(gpio_install_isr_service(0));
    ESP_ERROR_CHECK(gpio_isr_handler_add(
        CONFIG_HANNAH_MUTE_GPIO, ptt_isr_handler, NULL));

#if CONFIG_HANNAH_WAKEWORD_ENABLED
    hannah_wakeword_init();
#endif

    hannah_net_set_tts_callback(on_tts_data);
    hannah_net_set_tts_end_callback(on_tts_end);
    hannah_net_set_status_callback(on_status);
    hannah_net_set_playback_callback(on_playback_cmd);

    xTaskCreate(mic_task,     "mic",     8192, NULL, 5, NULL);
    xTaskCreate(speaker_task, "speaker", 4096, NULL, 5, NULL);

    ESP_LOGI(TAG, "hannah_audio initialisiert (%s-Modus).",
#if CONFIG_HANNAH_WAKEWORD_ENABLED
             "Wakeword"
#else
             "PTT"
#endif
    );
}

void hannah_audio_play(const uint8_t *pcm, size_t len, int sample_rate)
{
    if (!s_spk_queue || !pcm || len == 0) return;
    uint8_t *copy = malloc(len);
    if (!copy) { ESP_LOGW(TAG, "play: kein Speicher"); return; }
    memcpy(copy, pcm, len);
    spk_chunk_t chunk = {.data = copy, .len = len, .is_end = false};
    if (xQueueSend(s_spk_queue, &chunk, pdMS_TO_TICKS(50)) != pdTRUE) {
        ESP_LOGW(TAG, "Speaker-Queue voll — Chunk verworfen.");
        free(copy);
    }
}

void hannah_audio_play_end(void)
{
    if (!s_spk_queue) return;
    spk_chunk_t sentinel = {.data = NULL, .len = 0, .is_end = true};
    xQueueSend(s_spk_queue, &sentinel, pdMS_TO_TICKS(50));
}

void hannah_audio_stop(void)
{
    s_streaming_paused = false;
    /* Speaker-Queue leeren */
    if (s_spk_queue) {
        spk_chunk_t chunk;
        while (xQueueReceive(s_spk_queue, &chunk, 0) == pdTRUE) {
            if (chunk.data) free(chunk.data);
        }
    }
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

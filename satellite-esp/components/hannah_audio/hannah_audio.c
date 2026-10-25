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
#include "ref_timeline.h"

#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "esp_log.h"
#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "freertos/semphr.h"
#include "freertos/idf_additions.h"
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
/* Abtastrate des Lautsprecher-I2S: SAMPLE_RATE (TTS direkt) oder das Dreifache
 * (Musik über Sendspin, TTS wird ×3 hochgerechnet), siehe Kconfig (Refs #440). */
#define SPK_RATE          CONFIG_HANNAH_SPK_SAMPLE_RATE
#if SPK_RATE != SAMPLE_RATE && SPK_RATE != 3 * SAMPLE_RATE
#error "CONFIG_HANNAH_SPK_SAMPLE_RATE muss CONFIG_HANNAH_AUDIO_SAMPLE_RATE oder das Dreifache sein"
#endif
#define SPK_UPSAMPLE      (SPK_RATE / SAMPLE_RATE)
#define STEP_SAMPLES      WAKEWORD_STEP_SAMPLES          /* 160 (10ms)  */
#define STEP_BYTES_MONO (STEP_SAMPLES * 2)
#if !CONFIG_HANNAH_MIC_TYPE_NONE
#  if CONFIG_HANNAH_MIC_TYPE_PDM
#  define STEP_BYTES_RAW  (STEP_SAMPLES * 4)   /* 16-bit stereo PDM */
#  elif CONFIG_HANNAH_MIC_TYPE_TDM
/* Beamforming (Refs #222) bräuchte für volle Richtauflösung mehr als die
 * ~3 groben Delay-Stufen, die bei 16kHz zwischen gegenüberliegenden Mics
 * möglich sind — ursprünglich dafür mit 3× Überabtastung (48kHz) erfasst,
 * Delay-and-Sum + Downsample auf SAMPLE_RATE passiert in mic_task().
 *
 * Bewusste Abwägung (Refs #222, 2026-08-11): 48kHz-Rohrate brachte eine
 * deutliche, reproduzierbare Audioqualitäts-Regression ("klingt gedämpft/
 * wie ein alter Kassettenrekorder"). Durchgetestet und ausgeschlossen:
 * Delay-and-Sum-Summierung, AudioLib-Resample-Filter-Charakteristik,
 * ADAU7118-Mikrofon-Performance-Modus (DEC_RATIO), CPU-/Timing-Budget der
 * mic_task-Schleife (per Instrumentierung nachgemessen, komfortabel unter
 * dem 10ms-Sollwert), das bekannte ESP-IDF-TDM-Alternating-Sample-Bug-Muster
 * (espressif/esp-idf#10630), sowie MCLK/BCLK-Ganzzahlteilung (256/64=4,
 * exakt). Verbleibender Verdacht: Signalintegrität auf der TDM-Leitung
 * (BCLK/Data) bei höherer Taktfrequenz — braucht einen Logic-Analyzer zum
 * Verifizieren, den es aktuell nicht gibt. 1× (16kHz, kein Oversampling)
 * klang durchgehend sauber. Bewusste Entscheidung: Rohrate bleibt bei 16kHz,
 * Beamforming-Code bleibt aktiv (nicht entfernt) trotz stark eingeschränkter
 * Richtauflösung — sauberer Klang wiegt schwerer als funktionierende
 * Richtwirkung. Bei Bedarf mit Logic-Analyzer-Zugriff neu bewerten. */
#  define TDM_RAW_OVERSAMPLE 1
#  define TDM_RAW_SAMPLE_RATE (SAMPLE_RATE * TDM_RAW_OVERSAMPLE)
#  define TDM_RAW_STEP_SAMPLES (STEP_SAMPLES * TDM_RAW_OVERSAMPLE)
#  define STEP_BYTES_RAW  (TDM_RAW_STEP_SAMPLES * 8)   /* 4× 16-bit TDM-Slots (ADAU7118), 48kHz */
#  else
#  define STEP_BYTES_RAW  (STEP_SAMPLES * 8)   /* 32-bit slots I2S  */
#  endif
#endif

/* TDM-Downmix-Gain (Refs #222): ADAU7118 hat kein eigenes Gain-/PGA-Register
 * (kompletter Registersatz durchgeprüft, reiner Format-Wandler). Nach Fix des
 * DMA-Bugs + aktiviertem HPF liefert die Rohaufnahme echtes, sprachkorreliertes
 * Signal, aber mit sehr geringer Amplitude (RMS ~33 bei lautem Sprechen aus
 * 15cm Entfernung, Vollausschlag wäre 32767) — braucht digitale Verstärkung,
 * analog zum bestehenden PDM-Pfad (dort bereits ×64 mit Clipping-Schutz).
 * Kconfig-Default via hannah_config_get()->tdm_downmix_gain, per NVS/WebUI
 * ohne Reflash überschreibbar. */

/* VAD_SILENCE_FRAMES wird zur Laufzeit aus hannah_config_get()->vad_silence_ms berechnet */

/* Speaker-Ring-Buffer (internes DRAM, NOSPLIT — kein malloc/free pro Chunk)
 * 32KB ≈ ~22 Chunks × 29ms @ 24kHz = ~640ms Buffer — I2S-DMA braucht cache-kohärenten Speicher */
#define SPK_RINGBUF_SIZE (32 * 1024)

/* Mic-Warmup: erste Frames nach Boot verwerfen (PDM-Transienten, Fehlauslöser) */
#define WARMUP_FRAMES 500  /* 500 × 10ms = 5s */

/* Sampling-Mode "noise": Auto-Flush-Intervall (Dauerstrom-Aufnahme) */
#define NOISE_AUTOFLUSH_FRAMES 5000  /* 5000 × 10ms = 50s */

/* Debug-WAV-Snapshot (#180): Ringpuffer-Länge + Halte-Schwelle für die
 * Vol+/Vol--Tastenkombi. Auslösung per GPIO-Poll im mic_task, ~10ms/Iteration
 * (siehe WARMUP_FRAMES) — bewusst kein Wanduhr-Timer, gleiche Konvention wie
 * die übrigen Frame-Zähler in dieser Datei. */
#define DEBUG_WAV_CAPTURE_SECONDS 4
#define DEBUG_WAV_RING_SAMPLES    (SAMPLE_RATE * DEBUG_WAV_CAPTURE_SECONDS)
#define DEBUG_WAV_HOLD_FRAMES     70  /* 70 × 10ms = 700ms */
#define WAV_HEADER_BYTES          44

#if CONFIG_HANNAH_MIC_TYPE_TDM
/* Zweiter Debug-Ringpuffer (Refs #222): fängt s_tdm_combined mit — das
 * Delay-and-Sum-Ergebnis bei TDM_RAW_SAMPLE_RATE (48kHz), *vor*
 * hannah_resample_ctx()/Gain. Gleicher Trigger wie der bestehende
 * 16kHz-Ringpuffer (debug_wav_snapshot() friert beide im selben Aufruf ein),
 * also garantiert dasselbe Zeitfenster — zum direkten Vergleich "vor/nach
 * Resample" ohne separate Aufnahmen abgleichen zu müssen (2026-08-11:
 * gemutmaßte Kammfilterung durch Verzögerungs-Quantisierung ließ sich nicht
 * bestätigen, echter Pegelverlust im Vergleich zu einer alten
 * Nicht-TDM-Aufnahme aber schon — dieser Puffer soll klären, ob der Verlust
 * schon vor dem Resample-Filter da ist oder erst durch ihn entsteht). */
#define DEBUG_WAV_RING_SAMPLES_RAW (TDM_RAW_SAMPLE_RATE * DEBUG_WAV_CAPTURE_SECONDS)
#endif

/* Remote-Trigger (#194): kein Loslassen-Event wie bei der Tastenkombi, daher
 * fixes Sprechfenster nach Aufruf. 350 Frames = 3.5s, bewusst etwas unter
 * DEBUG_WAV_CAPTURE_SECONDS (4s) — die Aufnahme soll komplett im Ringpuffer
 * liegen, nicht am Anfang schon rausgerotiert sein. */
#define DEBUG_WAV_REMOTE_TRIGGER_FRAMES 350

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
/* Lautsprecher-I2S: ein Schreiber zur Zeit. speaker_task (TTS) und
 * hannah_audio_music_write() (Sendspin-Musik, Refs #440) nehmen s_spk_mutex um
 * jeden i2s_channel_write(). Läuft Musik, schreibt nur noch music_write: die TTS
 * legt ihre Chunks in s_mix_fifo, music_write mischt sie ein und senkt die Musik
 * dabei ab (Ducking). Ohne Musik schreibt speaker_task direkt. */
static SemaphoreHandle_t s_spk_mutex = NULL;

#if CONFIG_HANNAH_SPEAKER_ENABLED
/* Ducking (Refs #440): Musik-Lautstärke während einer Ansage und die Zeiten, in denen
 * sie ab- und wieder aufgeblendet wird. Q16-Faktor, 65536 = volle Lautstärke. */
#define MUSIC_DUCK_PERCENT     25
#define MUSIC_DUCK_ATTACK_MS   30
#define MUSIC_DUCK_RELEASE_MS  500
/* Musik gilt als aktiv, wenn music_write in dieser Zeit aufgerufen wurde. */
#define MUSIC_ACTIVE_WINDOW_US 200000
/* Puffer zwischen TTS und Musik, eine halbe Sekunde bei SPK_RATE (PSRAM). */
#define MIX_FIFO_SAMPLES       (SPK_RATE / 2)

static SemaphoreHandle_t s_mix_mutex      = NULL;   /* schützt s_mix_fifo, -head, -count */
static int16_t          *s_mix_fifo       = NULL;
static size_t            s_mix_head       = 0;
static size_t            s_mix_count      = 0;
static volatile int64_t  s_music_last_us  = 0;
static int32_t           s_duck_q16       = 65536;  /* nur unter s_spk_mutex benutzt */
/* Diagnose für hannah_audio_music_get_stats(), geschrieben in music_write unter s_spk_mutex. */
static volatile uint32_t s_ms_calls       = 0;
static volatile uint32_t s_ms_fails       = 0;
static volatile uint32_t s_ms_frames      = 0;
static volatile int16_t  s_ms_in_peak     = 0;
static volatile int16_t  s_ms_out_peak    = 0;
#endif
/* Vom DMA tatsächlich ausgegebene Frames (ISR) und von uns geschriebene Frames:
 * daraus ergibt sich, wann ein geschriebener Block fertig gespielt ist (braucht
 * Sendspin für den Sync). Mono 16 Bit = 2 Byte pro Frame. */
static volatile uint32_t s_spk_frames_sent    = 0;
static uint32_t          s_spk_frames_written = 0;
static volatile bool     s_music_muted        = false;

#if CONFIG_HANNAH_AEC
/* AEC-Referenz auf einer Zeitachse (Refs #226). Eine FIFO, die der mic_task in der Reihenfolge verbraucht,
 * in der er die Referenz abholt, lässt die Wiedergabe-Verzögerung mit Chunk-Bursts und Ringpuffer-Füllstand um
 * mehrere ms wandern; auch eine Zeitachse, auf der die Chunks beim Schreiben platziert werden, trägt nicht: wo ein
 * geschriebener Chunk im DMA-Ring ausgegeben wird, hängt vom Füllstand der Warteschlange ab (Phasensprünge von
 * ±6..8 Samples, ein 88-ms-Loch nach dem Start). Deshalb wird die Zeitachse im on_sent-Callback des TX-DMA gefüllt,
 * aus dem Puffer, der gerade ausgegeben wurde (i2s_event_data_t.dma_buf, auto_clear löscht erst nach dem Callback):
 * was dort steht, ist exakt das, was ausgegeben wurde, Stille und Musik eingeschlossen. Der mic_task liest zu jedem
 * Frame die Stelle, die der Ausgabezähler (s_spk_frames_sent) zu diesem Zeitpunkt hat. */
#define REF_TL_LEN 4096                                 /* 256 ms bei SAMPLE_RATE, Zweierpotenz; internes RAM, weil der DMA-Callback (ISR) schreibt */
static int16_t           s_ref_tl[REF_TL_LEN];          /* Index = (Ausgabeposition / SPK_UPSAMPLE) mod REF_TL_LEN */
static volatile uint32_t s_ref_tl_end16   = 0;          /* erste noch nicht beschriebene Position (SAMPLE_RATE-Samples) */
static volatile int64_t  s_spk_sent_ts_us = 0;          /* Zeitpunkt des letzten DMA-Deskriptor-Abschlusses */
#endif
static volatile bool     s_ptt_active        = false;
/* PTT durchbricht bewusst den Hardware-Mute (#296): nur mic_task selbst
 * schreibt/liest das, kein ISR-Zugriff nötig. Mic-Stromversorgung (GPIO,
 * siehe on_hw_mute()) wird nur für die Dauer des Tastendrucks wieder
 * zugeschaltet, der Mute-Zustand selbst (hannah_net_is_muted()) bleibt
 * unverändert bestehen. */
static bool              s_ptt_mic_power_override = false;
static volatile bool     s_streaming_paused  = false;
static volatile bool     s_wakeword_paused   = false;
/* Vollständige Hardware-Pause für OTA (#193) — anders als s_wakeword_paused
 * (nur TFLite-Inference übersprungen, I2S-Read läuft unverändert weiter)
 * verhindert das hier jeden i2s_channel_read()/i2s_channel_write()-Aufruf,
 * damit die Kanäle sicher abgebaut werden können (deren DMA-Puffer sind der
 * größte ungenutzte interne-DRAM-Hebel während OTA — s_wakeword_paused gibt
 * nur PSRAM frei). s_{mic,speaker}_parked_sem lassen hannah_audio_deinit_for_ota()
 * warten, bis beide Tasks bestätigt haben, dass sie gerade keinen I2S-Call in
 * Flight haben, bevor die Kanäle gelöscht werden (reine Flag-Prüfung hätte
 * eine Race — ein Task könnte mitten in einem bis zu 500-1000ms blockierenden
 * i2s_channel_write()/xRingbufferReceive() stecken). */
static volatile bool         s_hw_paused          = false;
static SemaphoreHandle_t     s_mic_parked_sem     = NULL;
static SemaphoreHandle_t     s_speaker_parked_sem = NULL;
/* #196: analog zu s_mic_parked_sem, aber für hannah_audio_pause_wakeword() —
 * lässt den OTA-Task warten, bis mic_task sicher keinen hannah_wakeword_process()/
 * Invoke() mehr in Flight hat, bevor hannah_wakeword_deinit() die TFLite-Arena
 * freigibt. Vorher war s_wakeword_paused nur ein Flag ohne Synchronisierung —
 * ein bereits laufender Invoke() lief einfach weiter, während die Arena
 * parallel schon freigegeben wurde (Use-after-free, Absturz in
 * tflite::GetQuantizedConvolutionMultipler beobachtet). */
static SemaphoreHandle_t     s_wakeword_parked_sem = NULL;
static volatile bool     s_vol_up_req        = false;
static volatile bool     s_vol_down_req      = false;
static volatile bool     s_speaking_active        = false;
static volatile bool     s_listen_after_tts       = false;
static volatile int      s_virtual_listen_frames  = 0;
static volatile int      s_volume                 = CONFIG_HANNAH_VOLUME_DEFAULT;
static volatile bool     s_notifications_pending  = false;  /* ungelesene Messages, #234 */
static hannah_webrtc_vad_state_t s_webrtc_vad;
static float              s_noise_floor_ema = 0.020f; /* adaptiver Noise-Floor-Schätzer */

#if CONFIG_HANNAH_WAKEWORD_ENABLED && !CONFIG_HANNAH_MIC_TYPE_NONE
/* Wakeword-Inferenz in eigenem Task (Refs #226). Sie lief inline im mic_task: im Mittel 5,4 ms
 * je 10-ms-Frame (bis 48 ms) neben ~2,8 ms für Beamforming/Resampling/AEC ergaben ~95 % Auslastung
 * ohne Reserve. Jede Pause des Tasks (z. B. die synchrone 115200-Baud-Konsole, 3 Logzeilen alle
 * 500 ms = bis zu ~85 ms) baute einen Rückstau auf, der nie abgebaut wurde, bis die RX-Queue
 * überlief und ganze 10-ms-Frames verloren gingen. Jetzt gibt der mic_task die fertigen Frames
 * über eine kleine Queue an den ww_task (Core 1) und liest nur noch das zuletzt berechnete
 * Maximum der Confidence seit seinem letzten Abholen. Ist die Queue voll, wird der neue Frame
 * verworfen (zählt s_ww_dropped), der mic_task wartet nie. */
#define WW_Q_FRAMES 8
static int16_t           s_ww_q[WW_Q_FRAMES][WAKEWORD_STEP_SAMPLES];
static volatile uint32_t s_ww_q_wr   = 0;       /* nur mic_task schreibt */
static volatile uint32_t s_ww_q_rd   = 0;       /* nur ww_task schreibt (bei Pause auch zurückgesetzt) */
static TaskHandle_t      s_ww_task   = NULL;
static float             s_ww_conf_max = 0.0f;  /* größte Confidence seit dem letzten ww_take_confidence() */
static volatile uint32_t s_ww_dropped = 0;
static portMUX_TYPE      s_ww_mux    = portMUX_INITIALIZER_UNLOCKED;
#endif
/* Lautstärke im NVS merken (Refs #445): jede Änderung startet einen Timer, geschrieben wird erst
 * nach VOLUME_SAVE_DELAY_US Ruhe, also einmal je Änderungsserie statt bei jedem Tastendruck. */
#define VOLUME_NVS_KEY        "volume"
#define MUSIC_VOLUME_NVS_KEY  "music_volume"
#define VOLUME_SAVE_DELAY_US  (5 * 1000 * 1000)
static esp_timer_handle_t s_volume_save_timer = NULL;
static int                s_volume_saved      = -1;   /* zuletzt gespeicherter (oder beim Start geladener) Wert */
/* Musik-Lautstärke (Sendspin, Refs #446), getrennt von s_volume (Hannah: Ansagen, Wecker, Assets). */
static volatile int       s_music_volume       = CONFIG_HANNAH_VOLUME_DEFAULT;
static int                s_music_volume_saved = -1;
static void volume_changed(void);

/* Welche Lautstärke die Tasten bedienen (Refs #446): die der Musik, solange Musik läuft und keine
 * Ansage spricht; sonst (Ansage, Leerlauf) die Hannah-Lautstärke. */
static bool keys_control_music(void)
{
#if CONFIG_HANNAH_SPEAKER_ENABLED
    return !s_speaking_active && (esp_timer_get_time() - s_music_last_us) < MUSIC_ACTIVE_WINDOW_US;
#else
    return false;
#endif
}

/* Vol+/Vol-: ändert je nach Kontext die Musik- oder die Hannah-Lautstärke. */
static void adjust_volume_by_keys(int delta)
{
    if (keys_control_music()) {
        int v = s_music_volume + delta;
        v = v > 100 ? 100 : (v < 0 ? 0 : v);
        s_music_volume = v;
        ESP_LOGI(TAG, "Musik-Lautstärke: %d%%", v);
        hannah_net_publish_music_volume(v);
        hannah_led_show_volume((uint8_t)v);
    } else {
        int v = s_volume + delta;
        v = v > 100 ? 100 : (v < 0 ? 0 : v);
        s_volume = v;
        ESP_LOGI(TAG, "Lautstärke: %d%%", v);
        hannah_net_publish_volume(v);
        hannah_led_show_volume((uint8_t)v);
    }
    volume_changed();
}
static int                s_stream_frames   = 0;

#if CONFIG_HANNAH_MIC_TYPE_TDM
/* Delay-and-Sum-Beamforming (Refs #222). Ringpuffer-Tiefe deutlich über dem
 * physikalisch nötigen Maximum (~9 Samples bei 48kHz für den Kreuz-Array-
 * Radius 32mm, s. tdm_beamform_update_direction()). Kanalreihenfolge = TDM-
 * Slot-Reihenfolge, s. Geometrie-Kommentar bei adau7118_init(): 0=MK1/Ost,
 * 1=MK3/Nord, 2=MK4/Süd, 3=MK2/West. */
#define TDM_BEAM_NUM_CH    4
#define TDM_BEAM_MAX_DELAY 16
static int      s_beam_delay_samples[TDM_BEAM_NUM_CH];
static int16_t  s_beam_history[TDM_BEAM_NUM_CH][TDM_BEAM_MAX_DELAY]; /* letzte Roh-Samples des vorigen 480er-Blocks, für Delays über Blockgrenzen hinweg */
static bool     s_beam_history_valid = false; /* erster Block nach Boot hat keine Historie — dort mit 0 auffüllen */
static uint16_t s_beam_last_direction_deg = 0xFFFF; /* ungültig -> erzwingt Neuberechnung beim ersten Aufruf */
static hannah_resample_ctx_t s_tdm_resample_ctx;
static int16_t  s_tdm_combined[TDM_RAW_STEP_SAMPLES]; /* summiertes 48kHz-Signal vor dem Downsample */

#if CONFIG_HANNAH_WAKEWORD_DEBUG
/* Timing-Instrumentierung (Refs #222, 2026-08-11): Audioqualitäts-Regression
 * korreliert eher mit "Resample-Filter aktiv" als mit einer festen
 * TDM-Taktschwelle (s. Kommentar bei TDM_RAW_OVERSAMPLE) — Verdacht auf
 * Rechenzeit/Timing-Budget in der 10ms-mic_task-Schleife statt auf
 * Frequenzgang. Misst die Iterationsdauer ab i2s_channel_read() (Read
 * selbst + Delay-and-Sum/Rohslot-Kopie + Resample + Gain) sowie separat nur
 * hannah_resample_ctx(), min/max/avg über ein Log-Fenster (s.
 * TDM_TIMING_LOG_FRAMES), um zu sehen ob/wie oft das 10ms-Budget
 * überschritten wird. */
#define TDM_TIMING_LOG_FRAMES 200  /* 200 × 10ms = 2s */
static int64_t s_tdm_timing_loop_sum_us     = 0;
static int64_t s_tdm_timing_loop_max_us     = 0;
static int64_t s_tdm_timing_resample_sum_us = 0;
static int64_t s_tdm_timing_resample_max_us = 0;
static int      s_tdm_timing_over_budget    = 0;  /* Iterationen > 10ms in diesem Fenster */
static int      s_tdm_timing_frames         = 0;
#endif /* CONFIG_HANNAH_WAKEWORD_DEBUG */

/* Berechnet pro Kanal die nötige Verzögerung (in Samples bei TDM_RAW_SAMPLE_RATE),
 * damit alle 4 Kanäle für Schall aus direction_deg (Grad im Uhrzeigersinn ab
 * Norden, s. tdm_beam_direction_deg) phasengleich aufsummiert werden können.
 * Herleitung: Kanal i mit Position p_i (relativ Array-Mitte) empfängt eine
 * ebene Welle aus Richtung û um (p_i·û)/c früher/später als die Mitte
 * (positiv = p_i liegt in Schallquellen-Richtung = empfängt früher). Auf die
 * am spätesten empfangende Kanal normiert (die braucht 0 Verzögerung, alle
 * anderen entsprechend mehr) ergibt nichtnegative, reale Verzögerungen. */
static void tdm_beamform_update_direction(uint16_t direction_deg)
{
    if (direction_deg == s_beam_last_direction_deg) return;

    const double radius_mm            = 32.0;
    const double speed_of_sound_mm_s  = 343000.0;
    const double theta                = direction_deg * M_PI / 180.0;
    const double ux                   =  sin(theta);
    const double uy                   = -cos(theta);

    /* Kanal-Positionen relativ Array-Mitte, mm. Reihenfolge = TDM-Slot 0..3. */
    static const double pos_x[TDM_BEAM_NUM_CH] = {  32.0, 0.0,   0.0,  -32.0 };
    static const double pos_y[TDM_BEAM_NUM_CH] = {   0.0, -32.0, 32.0,  0.0  };

    double raw_delay_s[TDM_BEAM_NUM_CH];
    double min_delay_s = 1e9;
    for (int ch = 0; ch < TDM_BEAM_NUM_CH; ch++) {
        raw_delay_s[ch] = (pos_x[ch] * ux + pos_y[ch] * uy) / speed_of_sound_mm_s;
        if (raw_delay_s[ch] < min_delay_s) min_delay_s = raw_delay_s[ch];
    }
    for (int ch = 0; ch < TDM_BEAM_NUM_CH; ch++) {
        double delay_s = raw_delay_s[ch] - min_delay_s;
        int d = (int)lround(delay_s * TDM_RAW_SAMPLE_RATE);
        if (d < 0) d = 0;
        if (d >= TDM_BEAM_MAX_DELAY) d = TDM_BEAM_MAX_DELAY - 1; /* Sicherheitsnetz, greift bei unseren 45°-Schritten nie */
        s_beam_delay_samples[ch] = d;
    }
    s_beam_last_direction_deg = direction_deg;
    ESP_LOGI(TAG, "Beamforming: Richtung %u° -> Delays [%d,%d,%d,%d] Samples @ %dHz",
             direction_deg, s_beam_delay_samples[0], s_beam_delay_samples[1],
             s_beam_delay_samples[2], s_beam_delay_samples[3], TDM_RAW_SAMPLE_RATE);
}

/* Liest Rohsample für Kanal ch bei Index i (0..TDM_RAW_STEP_SAMPLES-1) im
 * aktuellen 48kHz-Block, verzögert um s_beam_delay_samples[ch]. Negative
 * Indizes (Delay reicht in den vorigen Block zurück) werden aus
 * s_beam_history bedient. raw_s16 ist der interleavte TDM-Rohpuffer
 * (4 Slots/Frame) des aktuellen Blocks. */
static inline int16_t tdm_beam_delayed_sample(const int16_t *raw_s16, int ch, int i)
{
    int idx = i - s_beam_delay_samples[ch];
    if (idx >= 0) return raw_s16[idx * TDM_BEAM_NUM_CH + ch];
    int hist_idx = TDM_BEAM_MAX_DELAY + idx; /* idx ist negativ */
    return s_beam_history_valid ? s_beam_history[ch][hist_idx] : 0;
}

/* Merkt sich die letzten TDM_BEAM_MAX_DELAY Rohsamples je Kanal aus dem
 * gerade verarbeiteten Block, als Historie für den nächsten Aufruf. */
static void tdm_beam_save_history(const int16_t *raw_s16)
{
    for (int ch = 0; ch < TDM_BEAM_NUM_CH; ch++) {
        for (int k = 0; k < TDM_BEAM_MAX_DELAY; k++) {
            int i = TDM_RAW_STEP_SAMPLES - TDM_BEAM_MAX_DELAY + k;
            s_beam_history[ch][k] = raw_s16[i * TDM_BEAM_NUM_CH + ch];
        }
    }
    s_beam_history_valid = true;
}
#endif /* CONFIG_HANNAH_MIC_TYPE_TDM */

/* Debug-WAV-Snapshot (#180) — s_debug_ring wird ausschließlich vom mic_task
 * beschrieben (Schreiber + Trigger-Auswertung laufen im selben Task, keine
 * Synchronisierung nötig). s_debug_wav_snapshot wird vom mic_task bei
 * Trigger neu befüllt und vom Webserver-Handler (anderer Task) gelesen —
 * bewusst ohne Lock: einmaliger, manuell ausgelöster Debug-Snapshot, ein
 * Download während eines Re-Triggers ist ein irrelevantes Randrisiko.
 *
 * #199: die komplette Debug-Infrastruktur (Ringpuffer, periodische
 * Wakeword-Debug-Logzeile, Tastenkombi-/Remote-Snapshot) hinter
 * CONFIG_HANNAH_WAKEWORD_DEBUG. hannah_audio_get_debug_wav()/
 * hannah_audio_trigger_debug_wav_capture() bleiben immer kompiliert und
 * geben bei "n" einfach false zurück — die Webserver-Handler degradieren
 * dadurch von selbst sauber (404/500 "keine Aufnahme"), ohne dass
 * hannah_webserver.c selbst etwas vom Flag wissen muss. */
#if CONFIG_HANNAH_WAKEWORD_DEBUG
static int16_t  *s_debug_ring          = NULL;
static size_t     s_debug_ring_wp       = 0;
/* #197: Zähler, ein Inkrement pro mic_task-Iteration (=10ms), synchron zum
 * Ringpuffer-Schreiben unten — erlaubt eine spätere Debug-WAV-Download-Datei
 * exakt auf eine bestimmte periodische Wakeword-Debug-Zeile zurückzurechnen
 * (Differenz der frame_no-Werte × 160 Samples), statt über Wanduhr-
 * Zeitstempel zu raten (unzuverlässig, siehe Debugging-Session zum
 * Live-vs-Offline-Confidence-Unterschied). */
static uint32_t   s_debug_ring_frame_no = 0;
static bool        s_debug_ring_full     = false;
static uint8_t   *s_debug_wav_snapshot = NULL;
static volatile size_t s_debug_wav_len = 0;

#if CONFIG_HANNAH_MIC_TYPE_TDM
/* Pre-Resample-Gegenstück (Refs #222) — s. DEBUG_WAV_RING_SAMPLES_RAW. Gleiche
 * Zugriffsregeln wie s_debug_ring/s_debug_wav_snapshot oben. */
static int16_t  *s_debug_ring_raw          = NULL;
static size_t     s_debug_ring_raw_wp       = 0;
static bool        s_debug_ring_raw_full     = false;
static uint8_t   *s_debug_wav_raw_snapshot = NULL;
static volatile size_t s_debug_wav_raw_len = 0;
#endif

/* Remote-Trigger (#194) — s_debug_wav_remote_trigger wird vom Webserver-Task
 * gesetzt, ausschließlich vom mic_task wieder auf false gesetzt (einfaches
 * Request/Ack-Flag, kein Lock nötig). s_debug_wav_done_sem lässt den
 * aufrufenden Task (Webserver-Handler) blockieren, bis der mic_task den
 * Snapshot fertiggestellt hat. */
static volatile bool     s_debug_wav_remote_trigger = false;
static SemaphoreHandle_t s_debug_wav_done_sem       = NULL;
#endif /* CONFIG_HANNAH_WAKEWORD_DEBUG */

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

/* Register-Map (ADAU7118 Rev.A Datenblatt, Table 16) */
#define ADAU7118_REG_ENABLES           0x04
#define ADAU7118_REG_DEC_RATIO_CLK_MAP 0x05
#define ADAU7118_REG_HPF_CONTROL       0x06
#define ADAU7118_REG_SPT_CTRL1         0x07
#define ADAU7118_REG_SPT_C(n)          (0x09 + (n))  /* n = 0..7 */
#define ADAU7118_REG_RESETS            0x12

#define ADAU7118_RESETS_SOFT_RESET (1 << 0)

/* HPF_CONTROL (0x06): Bit0=HPF_EN, Bits[7:4]=HPF_FC (Cutoff, Table 15).
 * Datenblatt-Fließtext (Refs #222, bestätigt 2026-08-10): Hochpassfilter ist
 * per Default deaktiviert — durchgängiges Kapitel/Erklärungssatz, nicht nur
 * aus der Tabellen-Bitreihenfolge geraten (Lehre aus dem TRI_STATE-Fehlgriff).
 * Ohne HPF lässt der Wandler einen DC-Offset durch (~-256 in unseren echten
 * Aufnahmen), der das eigentliche Audiosignal überlagert. Reset-Default
 * 0xD0 (HPF_FC bereits sinnvoll gesetzt) bleibt bis auf das Enable-Bit
 * unverändert. */
#define ADAU7118_HPF_CONTROL_VALUE 0xD1

/* ENABLES (0x04): Bit5=PDM_CLK1_EN, Bit4=PDM_CLK0_EN, Bit3=CHAN_67_EN,
 * Bit2=CHAN_45_EN, Bit1=CHAN_23_EN, Bit0=CHAN_01_EN. Nur PDM_DAT0
 * (MK1+MK3, Takt PDM_CLK0) und PDM_DAT1 (MK2+MK4, Takt PDM_CLK1) sind
 * bestückt — DAT2/DAT3 unbeschaltet, deren Kanäle bleiben aus. */
#define ADAU7118_ENABLES_VALUE 0x33  /* CHAN_01+CHAN_23+CLK0+CLK1 an, CHAN_45/67 aus */

/* DEC_RATIO_CLK_MAP (0x05): Bit7=DAT3_CLK_MAP, Bit6=DAT2_CLK_MAP,
 * Bit5=DAT1_CLK_MAP, Bit4=DAT0_CLK_MAP (0=PDM_CLK0, 1=PDM_CLK1), Bit3:2=
 * reserviert, Bit1:0=DEC_RATIO. Datenblatt-Fließtext (Refs #222, bestätigt
 * 2026-08-10): Reset-Default mappt DAT0+DAT1 auf PDM_CLK0, DAT2+DAT3 auf
 * PDM_CLK1 — "The PDM_CLKx assignment must be the actual PDM clock that is
 * driving the PDM microphone." Bei uns hängt DAT1 (MK2+MK4) aber real an
 * PDM_CLK1, nicht CLK0 — ein bisher unentdeckter Mismatch, plausibler
 * Root-Cause für die beobachtete (praktisch) Stille auf Kanal 2/3.
 *
 * DEC_RATIO auf 32× runtergesetzt (Refs #222, 2026-08-11, Audioqualitäts-
 * Regression): der ADAU7118 leitet PDM_CLK automatisch aus FSYNC×DEC_RATIO
 * ab (kein separates Register für den Mic-Takt). Bei 64× (Reset-Default)
 * und der seit v0.72.0 auf 48kHz angehobenen TDM-Rohrate ergibt das
 * 48000×64=3.072MHz — laut SPH0641LU4H-1-Datenblatt exakt die untere
 * Grenze von dessen "Ultrasonic Mode" (3.072-4.8MHz), statt "Standard
 * Performance Mode" (1.024-2.475MHz), in dem die Mics bei der alten 16kHz-
 * Rate liefen (16000×64=1.024MHz, exakt dessen untere Grenze). Das
 * Mikrofon hat für Ultrasonic Mode einen eigenen, separat spezifizierten
 * Frequenzgang ("Typical Ultrasonic Response" vs. "Typical Free Field
 * Response") — plausible Ursache für den gemessenen, bereits im
 * unsummierten Einzelkanal vorhandenen Höhenverlust ("klingt wie ein
 * Kassettenrekorder"). 32× bei 48kHz ergibt 48000×32=1.536MHz, wieder
 * innerhalb Standard Performance Mode, ohne die 48kHz-Rohrate (nötig für
 * die Beamforming-Verzögerungsauflösung) aufzugeben. Der eigene
 * Decimation-Filter des ADAU7118 ist von DEC_RATIO praktisch unberührt
 * (Passband-Spec 0.45×fS gilt unabhängig vom Ratio, viel Marge zu unserem
 * 8kHz-Zielband). Noch nicht auf echter Hardware verifiziert. */
#define ADAU7118_DEC_RATIO_CLK_MAP_VALUE 0xE1  /* DAT1_CLK_MAP=1 (CLK1, unverändert) | DEC_RATIO=32x (01) */

/* SPT_CTRL1 (0x07): Bit6=TRI_STATE, Bit5:4=SLOT_WIDTH, Bit3:1=DATA_FORMAT,
 * Bit0=SAI_MODE. Reset-Default 0x41 hat SAI_MODE bereits auf TDM (Bit0=1) —
 * das war der Grund, warum der bisherige No-Op überhaupt Audio lieferte.
 * SLOT_WIDTH steht im Reset aber auf 32-bit (Bit5:4=00); mic_init() unten
 * konfiguriert den ESP32-seitigen I2S-TDM-RX-Kanal auf 16-bit-Slots — das
 * ist ein echter Mismatch. 0x51 = TRI_STATE(1, unverändert) |
 * SLOT_WIDTH=16bit(01) | DATA_FORMAT=0 (unverändert) | SAI_MODE(1, unverändert). */
#define ADAU7118_SPT_CTRL1_VALUE 0x51

/* Physische Mikrofon-Geometrie (Rev.5-Board, Mittelpunkt 58.5/57.5mm,
 * Radius 32mm, Kreuz-Anordnung). Kanalzuordnung folgt der SEL-Pin-
 * Beschaltung: SEL=GND -> gerader Kanal (0/2), SEL=VDD -> ungerader (1/3).
 *   Kanal 0 (DAT0, SEL=GND) = MK1, Ost  (+32,   0)
 *   Kanal 1 (DAT0, SEL=VDD) = MK3, Nord (  0, -32)
 *   Kanal 2 (DAT1, SEL=GND) = MK4, Süd  (  0, +32)
 *   Kanal 3 (DAT1, SEL=VDD) = MK2, West (-32,   0)
 * Himmelsrichtungen 2026-08-11 am echten Board verifiziert (vorherige Version
 * dieses Kommentars hatte Nord/Süd vertauscht geraten, s. #222) — Süden
 * (MK4) liegt der Stromversorgung gegenüber und ist die Default-Hörrichtung
 * (180°, s. tdm_beam_direction_deg). Jeder Kanal landet per Default-
 * Register-Mapping (SPT_Cx_SLOT = Kanalindex) bereits im gleichnamigen
 * TDM-Slot — keine Slot-Umsortierung nötig. Genutzt vom Delay-and-Sum-
 * Beamforming in mic_task() (Refs #222). */

static esp_err_t adau7118_write(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = { reg, val };
    return i2c_master_transmit(dev, buf, sizeof(buf), 100 /* ms */);
}

static esp_err_t adau7118_read(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *val)
{
    return i2c_master_transmit_receive(dev, &reg, 1, val, 1, 100 /* ms */);
}

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

    err = adau7118_write(dev, ADAU7118_REG_RESETS, ADAU7118_RESETS_SOFT_RESET);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADAU7118: Soft-Reset fehlgeschlagen (%s) — Chip antwortet nicht auf I2C?", esp_err_to_name(err));
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(5));  /* Datenblatt nennt keine Reset-Wartezeit, kurze Sicherheitsmarge */

    err  = adau7118_write(dev, ADAU7118_REG_SPT_CTRL1, ADAU7118_SPT_CTRL1_VALUE);
    err |= adau7118_write(dev, ADAU7118_REG_ENABLES, ADAU7118_ENABLES_VALUE);
    err |= adau7118_write(dev, ADAU7118_REG_DEC_RATIO_CLK_MAP, ADAU7118_DEC_RATIO_CLK_MAP_VALUE);
    err |= adau7118_write(dev, ADAU7118_REG_HPF_CONTROL, ADAU7118_HPF_CONTROL_VALUE);
    /* SPT_C0..SPT_C3 (MK1/MK3/MK4/MK2) bleiben auf Reset-Default (Slot = Kanalindex,
     * Drive an). SPT_C4..SPT_C7 (unbestückte Kanäle) werden vom Bus genommen —
     * SLOT-Feld unverändert, nur das DRV-Bit gelöscht. */
    err |= adau7118_write(dev, ADAU7118_REG_SPT_C(4), 0x40);
    err |= adau7118_write(dev, ADAU7118_REG_SPT_C(5), 0x50);
    err |= adau7118_write(dev, ADAU7118_REG_SPT_C(6), 0x60);
    err |= adau7118_write(dev, ADAU7118_REG_SPT_C(7), 0x70);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADAU7118: Register-Init unvollständig (%s)", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "ADAU7118: TDM-Modus konfiguriert (16-bit Slots, Kanäle 0-3 aktiv, Refs #222)");

    /* Diagnose #222: bestätigt per Rücklesen, dass der Chip die Werte wirklich
     * übernommen hat (statt nur ACK auf den Schreibzugriff) — Verdacht auf
     * "kommt gar keine Audiodaten an" trotz sauberem Init. Temporär, bis geklärt. */
    uint8_t rb_enables = 0, rb_clkmap = 0, rb_hpf = 0, rb_ctrl1 = 0, rb_c0 = 0, rb_c1 = 0, rb_c2 = 0, rb_c3 = 0;
    adau7118_read(dev, ADAU7118_REG_ENABLES, &rb_enables);
    adau7118_read(dev, ADAU7118_REG_DEC_RATIO_CLK_MAP, &rb_clkmap);
    adau7118_read(dev, ADAU7118_REG_HPF_CONTROL, &rb_hpf);
    adau7118_read(dev, ADAU7118_REG_SPT_CTRL1, &rb_ctrl1);
    adau7118_read(dev, ADAU7118_REG_SPT_C(0), &rb_c0);
    adau7118_read(dev, ADAU7118_REG_SPT_C(1), &rb_c1);
    adau7118_read(dev, ADAU7118_REG_SPT_C(2), &rb_c2);
    adau7118_read(dev, ADAU7118_REG_SPT_C(3), &rb_c3);
    ESP_LOGI(TAG, "ADAU7118: Rueckgelesen ENABLES=0x%02X DEC_RATIO_CLK_MAP=0x%02X HPF_CONTROL=0x%02X SPT_CTRL1=0x%02X SPT_C0=0x%02X SPT_C1=0x%02X SPT_C2=0x%02X SPT_C3=0x%02X",
             rb_enables, rb_clkmap, rb_hpf, rb_ctrl1, rb_c0, rb_c1, rb_c2, rb_c3);

    return ESP_OK;
}
#endif /* CONFIG_HANNAH_MIC_TYPE_TDM */

/* ------------------------------------------------------------------ */
/* I2S Mic initialisieren (I2S0, RX, stereo, INMP441)                   */

#if !CONFIG_HANNAH_MIC_TYPE_NONE
static esp_err_t mic_init(void)
{
    /* Experiment zurückgenommen (Refs #222, 2026-08-10): I2S_ROLE_SLAVE für
     * TDM (Annahme: ADAU7118 ist BCLK/FSYNC-Taktquelle) hat es auf echter
     * Hardware nachweislich schlimmer gemacht statt besser — Mic-Warmup
     * brauchte ~108s statt ~9s (wiederholt blockierende/fehlschlagende I2S-
     * Reads), Wakeword-Debug zeigte danach durchgehend rms=0.0000/peak=0.0000
     * (echte Null, kein Signal mehr, nicht mal der vorherige Störwert).
     * Zurück auf MASTER für alle Mic-Typen, bis die eigentliche Taktrichtung
     * zweifelsfrei geklärt ist. */
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(
        CONFIG_HANNAH_MIC_I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num  = 8;
#if CONFIG_HANNAH_MIC_TYPE_TDM
    /* TDM: 8 Byte/Frame (4 Kanäle × 16-bit) statt 4 Byte/Frame bei PDM/I2S —
     * STEP_SAMPLES*4 (640 Frames) überschreitet die ~4092-Byte-DMA-
     * Deskriptor-Grenze und wurde vom Treiber bisher still auf 511 geklemmt
     * ("dma frame num is out of dma buffer size, limited to 511", seit dem
     * allerersten Boot in jedem Log). Bekannte ESP-IDF-Bugs rund um I2S-TDM-
     * DMA-Puffergrößenberechnung (Refs #222: espressif/esp-idf#15126,
     * #10630) legen nahe, dass dieses stille Klemmen zu korrupten/
     * eingefrorenen Werten führen kann. Explizit auf einen Wert deutlich
     * unter der Grenze setzen, damit gar nicht erst geklemmt wird. */
    chan_cfg.dma_frame_num = STEP_SAMPLES * 2;
#else
    chan_cfg.dma_frame_num = STEP_SAMPLES * 4;
#endif

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
    /* Rohe Erfassungsrate ist TDM_RAW_SAMPLE_RATE (48kHz, s. STEP_BYTES_RAW-
     * Kommentar oben) — der ADAU7118 folgt automatisch (kein Register-Write
     * nötig, s. Datenblatt "Clocking": PDM_CLK wird intern aus FSYNC×DEC_RATIO
     * abgeleitet, DEC_RATIO bleibt bei 64× (Register 0x05 unverändert)).
     * mic_task() summiert/verzögert die 4 Kanäle und rechnet per
     * hannah_resample_ctx() auf SAMPLE_RATE (16kHz) runter. */
    i2s_tdm_config_t tdm_cfg = {
        .clk_cfg  = I2S_TDM_CLK_DEFAULT_CONFIG(TDM_RAW_SAMPLE_RATE),
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
    ESP_LOGI(TAG, "Mic TDM I2S%d: %dHz raw (Beamforming -> %dHz), 4 Slots (ADAU7118)",
             CONFIG_HANNAH_MIC_I2S_PORT, TDM_RAW_SAMPLE_RATE, SAMPLE_RATE);
    /* mic_init() läuft auch nach hannah_audio_resume_hw() (OTA-Pause-Zyklus,
     * s. Kommentar dort) erneut — Delay-Historie und Resample-Filterzustand
     * müssen dann neu starten, sonst würde über die reale Erfassungslücke
     * hinweg "kontinuierlich" weitergerechnet. */
    hannah_resample_ctx_init(&s_tdm_resample_ctx);
    s_beam_history_valid = false;
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
/* Läuft im Interrupt: zählt die vom DMA ausgegebenen Frames (mono, 16 Bit). */
static IRAM_ATTR bool spk_on_sent(i2s_chan_handle_t handle, i2s_event_data_t *event, void *user_ctx)
{
    (void)handle;
    (void)user_ctx;
#if !CONFIG_HANNAH_AEC
    s_spk_frames_sent += (uint32_t)(event->size / 2);
    return false;
#else
    const uint32_t s0 = s_spk_frames_sent;
    const uint32_t n  = (uint32_t)(event->size / 2);
    /* Refs #226: das gerade ausgegebene Stück auf SAMPLE_RATE mitteln und an seine Position der
     * Ausgabe-Zeitachse legen (ref_timeline.h, Host-Test test_ref_tl_io.c). Ein Deskriptor ist ein
     * Vielfaches von SPK_UPSAMPLE Frames lang (120), s0 damit auch. */
    if (event->dma_buf)
        s_ref_tl_end16 = ref_tl_store(s_ref_tl, REF_TL_LEN, s0, (const int16_t *)event->dma_buf, n, SPK_UPSAMPLE);
    s_spk_frames_sent = s0 + n;
    s_spk_sent_ts_us  = esp_timer_get_time();   /* Zeitbezug für die AEC-Referenz-Zeitachse */
    return false;
#endif
}

static esp_err_t speaker_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(
        CONFIG_HANNAH_SPK_I2S_PORT, I2S_ROLE_MASTER);
#if SPK_UPSAMPLE > 1
    /* Musik über Sendspin braucht eine feine Zeitauflösung der Ausgabezähler
     * (Refs #440): 32 × 120 Frames = 80 ms bei 48 kHz, 2,5 ms pro Deskriptor.
     * Mit 8 × 640 Frames (13 ms Granularität) meldete der Spike ständig
     * "Lost sync". Bleibt unter dem Drain-Puffer des End-Sentinels (5120 Frames). */
    chan_cfg.dma_desc_num  = 32;
    chan_cfg.dma_frame_num = 120;
#else
    chan_cfg.dma_desc_num  = 8;
    chan_cfg.dma_frame_num = STEP_SAMPLES * 4;
#endif
    chan_cfg.auto_clear    = true;

    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &s_tx_chan, NULL));

    i2s_std_config_t std_cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SPK_RATE),
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
    /* Zählt die vom DMA tatsächlich ausgegebenen Frames (Refs #440). */
    s_spk_frames_sent    = 0;
    s_spk_frames_written = 0;
    i2s_event_callbacks_t cbs = { .on_sent = spk_on_sent };
    ESP_ERROR_CHECK(i2s_channel_register_event_callback(s_tx_chan, &cbs, NULL));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx_chan));
    ESP_LOGI(TAG, "Speaker I2S%d: %dHz mono", CONFIG_HANNAH_SPK_I2S_PORT, SPK_RATE);
    return ESP_OK;
}
#endif /* CONFIG_HANNAH_SPEAKER_ENABLED */

/* ------------------------------------------------------------------ */
/* Debug-WAV-Snapshot (#180)                                             */

#if !CONFIG_HANNAH_MIC_TYPE_NONE
/* Schreibt einen kompletten 44-Byte-RIFF/WAV-Header (16-bit PCM mono) nach *p. */
static void write_wav_header(uint8_t *p, uint32_t pcm_bytes, uint32_t rate)
{
    uint32_t riff_len   = 36 + pcm_bytes;
    uint32_t fmt_len    = 16;
    uint16_t audio_fmt  = 1;   /* PCM */
    uint16_t channels   = 1;
    uint32_t byte_rate  = rate * 2;
    uint16_t block_align = 2;
    uint16_t bits       = 16;

    memcpy(p, "RIFF", 4);        p += 4;
    memcpy(p, &riff_len, 4);     p += 4;
    memcpy(p, "WAVE", 4);        p += 4;
    memcpy(p, "fmt ", 4);        p += 4;
    memcpy(p, &fmt_len, 4);      p += 4;
    memcpy(p, &audio_fmt, 2);    p += 2;
    memcpy(p, &channels, 2);     p += 2;
    memcpy(p, &rate, 4);         p += 4;
    memcpy(p, &byte_rate, 4);    p += 4;
    memcpy(p, &block_align, 2);  p += 2;
    memcpy(p, &bits, 2);         p += 2;
    memcpy(p, "data", 4);        p += 4;
    memcpy(p, &pcm_bytes, 4);
}

#if CONFIG_HANNAH_WAKEWORD_DEBUG
/* Friert den aktuellen Ringpuffer als fertige WAV im Snapshot-Puffer ein.
 * Läuft im mic_task, ausgelöst durch die Vol+/Vol--Kombi. */
static void debug_wav_snapshot(void)
{
    if (!s_debug_ring || !s_debug_wav_snapshot) return;

    size_t samples   = s_debug_ring_full ? DEBUG_WAV_RING_SAMPLES : s_debug_ring_wp;
    size_t pcm_bytes = samples * 2;
    uint8_t *pcm_out = s_debug_wav_snapshot + WAV_HEADER_BYTES;

    if (s_debug_ring_full) {
        /* Älteste Samples liegen ab s_debug_ring_wp (nächster Schreibpunkt) */
        size_t tail = DEBUG_WAV_RING_SAMPLES - s_debug_ring_wp;
        memcpy(pcm_out,               s_debug_ring + s_debug_ring_wp, tail * 2);
        memcpy(pcm_out + tail * 2,    s_debug_ring,                   s_debug_ring_wp * 2);
    } else {
        memcpy(pcm_out, s_debug_ring, pcm_bytes);
    }

    write_wav_header(s_debug_wav_snapshot, (uint32_t)pcm_bytes, SAMPLE_RATE);
    s_debug_wav_len = WAV_HEADER_BYTES + pcm_bytes;

#if CONFIG_HANNAH_MIC_TYPE_TDM
    if (s_debug_ring_raw && s_debug_wav_raw_snapshot) {
        size_t samples_raw   = s_debug_ring_raw_full ? DEBUG_WAV_RING_SAMPLES_RAW : s_debug_ring_raw_wp;
        size_t pcm_bytes_raw = samples_raw * 2;
        uint8_t *pcm_out_raw = s_debug_wav_raw_snapshot + WAV_HEADER_BYTES;

        if (s_debug_ring_raw_full) {
            size_t tail = DEBUG_WAV_RING_SAMPLES_RAW - s_debug_ring_raw_wp;
            memcpy(pcm_out_raw,            s_debug_ring_raw + s_debug_ring_raw_wp, tail * 2);
            memcpy(pcm_out_raw + tail * 2, s_debug_ring_raw,                       s_debug_ring_raw_wp * 2);
        } else {
            memcpy(pcm_out_raw, s_debug_ring_raw, pcm_bytes_raw);
        }

        write_wav_header(s_debug_wav_raw_snapshot, (uint32_t)pcm_bytes_raw, TDM_RAW_SAMPLE_RATE);
        s_debug_wav_raw_len = WAV_HEADER_BYTES + pcm_bytes_raw;
    }
#endif
}
#endif /* CONFIG_HANNAH_WAKEWORD_DEBUG */
#endif /* !CONFIG_HANNAH_MIC_TYPE_NONE */

bool hannah_audio_get_debug_wav(const uint8_t **out_buf, size_t *out_len)
{
#if CONFIG_HANNAH_WAKEWORD_DEBUG
    size_t len = s_debug_wav_len;
    if (!s_debug_wav_snapshot || len == 0) return false;
    *out_buf = s_debug_wav_snapshot;
    *out_len = len;
    return true;
#else
    return false;
#endif
}

bool hannah_audio_get_debug_wav_raw(const uint8_t **out_buf, size_t *out_len)
{
#if CONFIG_HANNAH_WAKEWORD_DEBUG && CONFIG_HANNAH_MIC_TYPE_TDM
    size_t len = s_debug_wav_raw_len;
    if (!s_debug_wav_raw_snapshot || len == 0) return false;
    *out_buf = s_debug_wav_raw_snapshot;
    *out_len = len;
    return true;
#else
    return false;
#endif
}

bool hannah_audio_trigger_debug_wav_capture(void)
{
#if CONFIG_HANNAH_WAKEWORD_DEBUG
    if (!s_debug_wav_done_sem) return false;  /* Mic deaktiviert (CONFIG_HANNAH_MIC_TYPE_NONE) */

    /* Sem vor dem Trigger leeren — falls ein vorheriger Aufruf timeoutete und
     * der mic_task danach doch noch xSemaphoreGive() nachholte, würde sonst
     * dieser Aufruf sofort fälschlich "fertig" zurückmelden. */
    xSemaphoreTake(s_debug_wav_done_sem, 0);

    s_debug_wav_remote_trigger = true;

    /* Sprechfenster + Marge, falls der mic_task gerade pausiert ist (OTA). */
    const TickType_t timeout = pdMS_TO_TICKS(DEBUG_WAV_REMOTE_TRIGGER_FRAMES * 10 + 5000);
    return xSemaphoreTake(s_debug_wav_done_sem, timeout) == pdTRUE;
#else
    return false;
#endif
}

/* ------------------------------------------------------------------ */
/* Mic-Task                                                              */

/* Zeigt IDLE nur wenn weder gemutet noch ungelesene Messages vorhanden sind
 * (Priorität MUTE > NOTIFY-pending > IDLE, #234). Zentraler Helper statt an
 * jeder der ~14 Stellen einzeln dupliziert — unconditional (auch im
 * CONFIG_HANNAH_MIC_TYPE_NONE-Zweig ohne mic_task gebraucht). */
static void update_idle_led(void)
{
    hannah_led_set_state(
        hannah_net_is_muted() ? LED_STATE_MUTE :
        s_notifications_pending ? LED_STATE_NOTIFY : LED_STATE_IDLE);
}

#if !CONFIG_HANNAH_MIC_TYPE_NONE
static inline void mic_led(led_state_t state)
{
    if (!s_sampling_mode) {
        if (state == LED_STATE_IDLE)
            update_idle_led();
        else
            hannah_led_set_state(state);
    }
}

/* Echo-Canceller (Refs #226): Speex-MDF aus AudioLib (hannah_aec_*), 10-ms-Frames mit 16 kHz.
 * Der Filterzustand bleibt zwischen Ansagen erhalten (kalt braucht Speex ~3 s zum Einschwingen). */
#if CONFIG_HANNAH_AEC
static hannah_aec_state_t s_aec;
static bool               s_aec_ready = false;

/* Liest zu diesem Mikrofon-Frame das Stück der Referenz-Zeitachse, das zu diesem
 * Zeitpunkt am I2S-Ausgang liegt (Refs #226). Der Ausgabezähler s_spk_frames_sent
 * springt nur alle Deskriptoren (120 Frames = 2,5 ms): mit dem Zeitstempel des
 * letzten Sprungs wird dazwischen interpoliert. Die Abweichung zwischen Mikrofon-
 * und Lautsprecher-Zeitachse (je an Zählern getaktet) hält ref_timeline.c fest, damit
 * das Aufwachen des mic_task (±1-2 ms) nicht ins Ergebnis geht. Der Rest — Wartetiefe
 * der TX-DMA-Warteschlange (~80 ms), Weg zum Mikrofon, Verarbeitung — ist konstant;
 * das Filter des Echo-Cancellers deckt ihn ab. */
static void ref_from_timeline(int16_t *ref_frame, size_t n)
{
    memset(ref_frame, 0, n * sizeof(int16_t));
#if CONFIG_HANNAH_SPEAKER_ENABLED
    static bool     s_init       = false;
    static uint32_t s_last_sent  = 0;
    static int64_t  s_unwrapped  = 0;   /* Ausgabezähler ohne 32-Bit-Überlauf, startet beim absoluten Zählerstand */
    static int64_t  s_pll_off    = 0;   /* geglätteter Versatz zur Mikrofon-Zeitachse */
    static int64_t  s_frame_idx  = 0;   /* Mikrofon-Frames seit Start, in Ausgabe-Frames: * n * SPK_UPSAMPLE */

    const uint32_t sent = s_spk_frames_sent;
    const int64_t  ts   = s_spk_sent_ts_us;
    const int64_t  now  = esp_timer_get_time();
    if (!s_init) {
        s_last_sent = sent;
        s_unwrapped = sent;   /* absolut starten: spk_on_sent() legt die Daten an absoluten Zählerpositionen ab */
        s_init = true;
    }
    s_unwrapped += (int32_t)(sent - s_last_sent);
    s_last_sent  = sent;

    int64_t dt = now - ts;
    if (dt < 0) dt = 0;
    int64_t frac = dt * SPK_RATE / 1000000LL;
    if (frac > 120) frac = 120;     /* länger als ein Deskriptor = ISR hängt, nicht weiter hochzählen */
    const int64_t est48  = s_unwrapped + frac;
    const int64_t frame_len48 = (int64_t)n * SPK_UPSAMPLE;
    const int64_t meas_off = est48 - s_frame_idx * frame_len48;

    /* Der Versatz zwischen den beiden Zeitachsen ist konstant: Mikrofon-RX und Lautsprecher-TX laufen mit
     * 48 kHz aus derselben Taktquelle mit demselben Teiler, es gibt keine Drift. Jede Einzelmessung rauscht
     * aber nach oben (der mic_task wacht spät auf, z. B. beim Start eines Assets, wenn das SPIFFS-Lesen ihn
     * verdrängt). Ein Regler, der solchen Verspätungen nachgibt, verschiebt die Referenz um bis zu 15 Samples.
     * ref_timeline.c nimmt stattdessen das Minimum über ein Fenster von 100 Frames und hält den Versatz fest
     * (Details und Host-Test dort). */
    static ref_tl_t s_tl;
    static bool     s_tl_init = false;
    if (!s_tl_init) {
        ref_tl_init(&s_tl, 5 * (SPK_RATE / 1000));   /* 5 ms: ab hier gilt ein Fenster als Sprung */
        s_tl_init = true;
    }
    s_pll_off = ref_tl_update(&s_tl, meas_off);
    /* geglättete Ausgabeposition "jetzt" = Ende dieses Frames, einen Deskriptor (120 Frames,
     * 2,5 ms) zurückversetzt: die Zeitachse enthält nur schon vollständig ausgegebene
     * Deskriptoren, ein Lesen bis "jetzt" würde in den noch laufenden greifen (Stille). */
    const int64_t end48 = s_pll_off + s_frame_idx * frame_len48 - 120;
    s_frame_idx++;

    /* Lesen mit Catmull-Rom zwischen zwei Samples, Stille vor dem beschriebenen Ende und hinter dem
     * überschriebenen Teil: ref_timeline.c, Host-Test test_ref_tl_io.c. */
    ref_tl_read(s_ref_tl, REF_TL_LEN, s_ref_tl_end16, end48, SPK_UPSAMPLE, ref_frame, n);
#endif
}
#endif /* CONFIG_HANNAH_AEC */

/* Holt das zum Frame passende Stück Referenz und führt die AEC aus. Läuft
 * unconditional mit, auch außerhalb von STREAMING/Warmup, damit die Zeitachse
 * durchgehend mitläuft. Ohne CONFIG_HANNAH_AEC bleibt mic_frame unverändert. */
static void aec_run(int16_t *mic_frame, size_t n)
{
#if CONFIG_HANNAH_AEC
    if (!s_aec_ready) return;
    int16_t ref_frame[STEP_SAMPLES];
    ref_from_timeline(ref_frame, n);
    hannah_aec_process(&s_aec, mic_frame, ref_frame, (int)n);
#else
    (void)mic_frame;
    (void)n;
#endif
}

#if CONFIG_HANNAH_WAKEWORD_ENABLED
/* Reicht einen fertigen 10-ms-Frame an den ww_task weiter. Wartet nie: bei voller Queue wird der
 * Frame verworfen (das Wakeword-Frontend verträgt eine 10-ms-Lücke), die Mikrofon-Kette läuft weiter. */
static void ww_push(const int16_t *pcm)
{
    const uint32_t wr = s_ww_q_wr;
    if (wr - s_ww_q_rd >= WW_Q_FRAMES) {
        s_ww_dropped++;
        return;
    }
    memcpy(s_ww_q[wr % WW_Q_FRAMES], pcm, sizeof(s_ww_q[0]));
    __sync_synchronize();
    s_ww_q_wr = wr + 1;
    if (s_ww_task) xTaskNotifyGive(s_ww_task);
}

/* Größte Confidence, die der ww_task seit dem letzten Aufruf berechnet hat (und setzt sie zurück). */
static float ww_take_confidence(void)
{
    portENTER_CRITICAL(&s_ww_mux);
    const float c = s_ww_conf_max;
    s_ww_conf_max = 0.0f;
    portEXIT_CRITICAL(&s_ww_mux);
    return c;
}

/* Läuft auf Core 1 (Priorität unter dem Speaker-Task), Stack im PSRAM. Parkt bei OTA wie früher der
 * mic_task: das Give auf s_wakeword_parked_sem bestätigt, dass hier kein hannah_wakeword_process() /
 * Invoke() mehr in Flight ist (#196), erst danach gibt der OTA-Task die TFLite-Arena frei. */
static void ww_task(void *arg)
{
    (void)arg;
#if CONFIG_HANNAH_WAKEWORD_DEBUG
    uint32_t dbg_ctr      = 0;
    float    dbg_max      = 0.0f;
    float    dbg_peak     = 0.0f;
    size_t   dbg_min_feat = SIZE_MAX;
#endif
    while (1) {
        if (s_wakeword_paused) {
            s_ww_q_rd = s_ww_q_wr;   /* Rückstau verwerfen, nach der Pause beginnt der Strom neu */
            xSemaphoreGive(s_wakeword_parked_sem);
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(100));
        while (s_ww_q_rd != s_ww_q_wr && !s_wakeword_paused) {
            const int16_t *frame = s_ww_q[s_ww_q_rd % WW_Q_FRAMES];
            const float confidence = hannah_wakeword_process(frame);

            portENTER_CRITICAL(&s_ww_mux);
            if (confidence > s_ww_conf_max) s_ww_conf_max = confidence;
            portEXIT_CRITICAL(&s_ww_mux);

#if CONFIG_HANNAH_WAKEWORD_DEBUG
            /* Wakeword-Debug (#173): die Logzeilen kamen früher aus dem mic_task und haben ihn über die
             * synchrone Konsole bis zu ~85 ms blockiert. Hier blockieren sie nur den ww_task, und nur alle 5 s. */
            if (confidence > dbg_max) dbg_max = confidence;
            for (size_t i = 0; i < WAKEWORD_STEP_SAMPLES; i++) {
                const float a = fabsf((float)frame[i]) / 32768.0f;
                if (a > dbg_peak) dbg_peak = a;
            }
            hannah_wakeword_debug_t wwdbg;
            hannah_wakeword_last_debug(&wwdbg);
            if (wwdbg.feat_size < dbg_min_feat) dbg_min_feat = wwdbg.feat_size;
            if (++dbg_ctr >= 500) {
                dbg_ctr = 0;
                ESP_LOGI(TAG, "Wakeword-Debug: rms=%.4f peak=%.4f confidence(peak/5s)=%.4f threshold=%.2f "
                              "noise_ema=%.4f feat_size(min/5s)=%u num_read=%u output_raw=%u invoke_fails=%lu frame_no=%lu dropped=%lu model=%uB/%uF",
                         hannah_rms(frame, WAKEWORD_STEP_SAMPLES), dbg_peak, dbg_max,
                         hannah_config_get()->wakeword_threshold / 100.0f, s_noise_floor_ema,
                         (unsigned)dbg_min_feat, (unsigned)wwdbg.num_read,
                         (unsigned)wwdbg.output_raw, (unsigned long)wwdbg.invoke_fail_count,
                         (unsigned long)s_debug_ring_frame_no, (unsigned long)s_ww_dropped,
                         (unsigned)wwdbg.model_bytes, (unsigned)wwdbg.input_frames);
                dbg_max      = 0.0f;
                dbg_peak     = 0.0f;
                dbg_min_feat = SIZE_MAX;
            }
#endif
            s_ww_q_rd = s_ww_q_rd + 1;
        }
    }
}
#endif /* CONFIG_HANNAH_WAKEWORD_ENABLED */

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
            update_idle_led();
        }
        if (s_vol_up_req) {
            s_vol_up_req = false;
            adjust_volume_by_keys(CONFIG_HANNAH_VOLUME_STEP);
        }
        if (s_vol_down_req) {
            s_vol_down_req = false;
            adjust_volume_by_keys(-CONFIG_HANNAH_VOLUME_STEP);
        }

#if CONFIG_HANNAH_WAKEWORD_DEBUG
        /* Debug-WAV-Trigger (#180, #182): Vol+ und Vol- gleichzeitig gehalten.
         * Bewusst per GPIO-Poll statt eigener ISR — läuft unabhängig vom
         * Sampling-/Capture-Modus im normalen Wakeword-Betrieb mit, ohne
         * dessen Zustand anzufassen. Snapshot löst erst beim LOSLASSEN aus
         * (700ms Halten dient nur als Debounce/"scharf machen") — sonst
         * schneidet der Snapshot mitten in die Testphrase, wenn Tasten und
         * Sprechen gleichzeitig beginnen (#182: "Okay Nabu" → nur "Okay" in
         * der WAV, weil vorher exakt beim Erreichen der Halteschwelle
         * ausgelöst wurde statt beim Loslassen). */
        {
            static int  s_debug_hold_frames = 0;
            static bool s_debug_ready       = false;  /* Halteschwelle erreicht, wartet auf Loslassen */
            bool both_down = (gpio_get_level(CONFIG_HANNAH_VOL_UP_GPIO) == 0) &&
                             (gpio_get_level(CONFIG_HANNAH_VOL_DOWN_GPIO) == 0);
            if (both_down) {
                if (!s_debug_ready && ++s_debug_hold_frames >= DEBUG_WAV_HOLD_FRAMES) {
                    s_debug_ready = true;
                    ESP_LOGI(TAG, "Debug-WAV-Trigger scharf — jetzt sprechen, Tasten danach loslassen.");
                }
            } else {
                if (s_debug_ready) {
                    debug_wav_snapshot();
                    ESP_LOGI(TAG, "Debug-WAV-Snapshot ausgelöst (%u B, frame_no=%lu) — abrufbar unter /debug/wav.",
                             (unsigned)s_debug_wav_len, (unsigned long)s_debug_ring_frame_no);
                }
                s_debug_hold_frames = 0;
                s_debug_ready       = false;
            }
        }

        /* Debug-WAV-Remote-Trigger (#194): Gegenstück zur Tastenkombi oben,
         * ausgelöst per Webserver-Endpoint statt physischem Tastendruck — für
         * Tests aus normalem Nutzungsabstand. Kein Loslassen-Event verfügbar,
         * daher festes Sprechfenster (DEBUG_WAV_REMOTE_TRIGGER_FRAMES) statt
         * Halten-bis-fertig. */
        {
            static int  s_remote_countdown = -1;  /* -1 = kein Capture aktiv */
            if (s_debug_wav_remote_trigger && s_remote_countdown < 0) {
                s_debug_wav_remote_trigger = false;
                s_remote_countdown = DEBUG_WAV_REMOTE_TRIGGER_FRAMES;
                hannah_led_set_state(LED_STATE_CAPTURE);
                ESP_LOGI(TAG, "Debug-WAV-Remote-Trigger — jetzt sprechen (%.1fs Fenster).",
                         DEBUG_WAV_REMOTE_TRIGGER_FRAMES / 100.0f);
            } else if (s_remote_countdown == 0) {
                debug_wav_snapshot();
                ESP_LOGI(TAG, "Debug-WAV-Snapshot (remote) ausgelöst (%u B, frame_no=%lu) — abrufbar unter /debug/wav.",
                         (unsigned)s_debug_wav_len, (unsigned long)s_debug_ring_frame_no);
                update_idle_led();
                xSemaphoreGive(s_debug_wav_done_sem);
                s_remote_countdown = -1;
            } else if (s_remote_countdown > 0) {
                s_remote_countdown--;
            }
        }
#endif /* CONFIG_HANNAH_WAKEWORD_DEBUG */

        if (s_hw_paused) {
            /* OTA baut die I2S-Kanäle gerade ab/wieder auf — s_rx_chan nicht anfassen. */
            xSemaphoreGive(s_mic_parked_sem);
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        size_t bytes_read = 0;
        i2s_channel_read(s_rx_chan, raw, STEP_BYTES_RAW,
                         &bytes_read, pdMS_TO_TICKS(200));

#if CONFIG_HANNAH_MIC_TYPE_TDM && CONFIG_HANNAH_WAKEWORD_DEBUG
        /* Timing-Instrumentierung (Refs #222) — Startpunkt bewusst NACH dem
         * i2s_channel_read()-Block, der misst nur die reine Rechenzeit
         * (Delay-and-Sum/Rohslot + Resample + Gain) gegen das 10ms-Budget
         * der Schleife, nicht die (normale) Wartezeit auf neue DMA-Daten. */
        int64_t tdm_timing_t0 = esp_timer_get_time();
#endif

#if CONFIG_HANNAH_MIC_TYPE_PDM
        /* PDM: 16-bit stereo → linker Kanal (SPH0641: SEL=GND → L, Index 0) */
        size_t frames    = bytes_read / 4;
        int16_t *s16     = (int16_t *)raw;
        for (size_t i = 0; i < frames; i++) {
            mono[i] = (int16_t)((int32_t)s16[i * 2] * 64 > 32767 ? 32767 : (int32_t)s16[i * 2] * 64 < -32768 ? -32768 : (int32_t)s16[i * 2] * 64);
        }
#elif CONFIG_HANNAH_MIC_TYPE_TDM
        /* TDM: 4× 16-bit Slots (ADAU7118), erfasst mit 3× Überabtastung
         * (TDM_RAW_SAMPLE_RATE) für Delay-and-Sum-Beamforming — s. Issue
         * #222. Reihenfolge: pro Kanal verzögern + summieren (bei 48kHz,
         * volle Richtauflösung) -> Downsample auf SAMPLE_RATE
         * (hannah_resample_ctx, AudioLib >=0.3.0, mit Anti-Aliasing-Filter)
         * -> erst danach Gain mit Clipping-Schutz (s. Kommentar bei
         * hannah_config_t.tdm_downmix_gain) — vor dem Downsample würde ein
         * geclipptes Signal den Anti-Aliasing-Filter verzerren. */
        size_t raw_frames = bytes_read / 8;
        int16_t *s16      = (int16_t *)raw;

        tdm_beamform_update_direction(hannah_config_get()->tdm_beam_direction_deg);

        int8_t debug_raw_slot = hannah_config_get()->tdm_debug_raw_slot;
        if (debug_raw_slot >= 0 && debug_raw_slot < TDM_BEAM_NUM_CH) {
            /* Diagnose (Refs #222): einzelnen Rohslot unverändert durchreichen,
             * kein Delay-and-Sum — zum empirischen Verifizieren der
             * Slot->Mikrofon-Zuordnung gegen eine bekannte Sprechrichtung. */
            for (size_t i = 0; i < raw_frames; i++) {
                s_tdm_combined[i] = s16[i * TDM_BEAM_NUM_CH + debug_raw_slot];
            }
        } else {
            for (size_t i = 0; i < raw_frames; i++) {
                int32_t sum = 0;
                for (int ch = 0; ch < TDM_BEAM_NUM_CH; ch++) {
                    sum += tdm_beam_delayed_sample(s16, ch, (int)i);
                }
                s_tdm_combined[i] = (int16_t)(sum / TDM_BEAM_NUM_CH);
            }
        }
        tdm_beam_save_history(s16);

#if CONFIG_HANNAH_WAKEWORD_DEBUG
        /* Pre-Resample-Debug-Ringpuffer (Refs #222, s. DEBUG_WAV_RING_SAMPLES_RAW):
         * exakt das Signal, das gleich in hannah_resample_ctx() reingeht. */
        if (s_debug_ring_raw) {
            for (size_t i = 0; i < raw_frames; i++) {
                s_debug_ring_raw[s_debug_ring_raw_wp] = s_tdm_combined[i];
                if (++s_debug_ring_raw_wp >= DEBUG_WAV_RING_SAMPLES_RAW) {
                    s_debug_ring_raw_wp   = 0;
                    s_debug_ring_raw_full = true;
                }
            }
        }
#endif /* CONFIG_HANNAH_WAKEWORD_DEBUG */

#if CONFIG_HANNAH_WAKEWORD_DEBUG
        int64_t tdm_timing_t_resample0 = esp_timer_get_time();
#endif
        hannah_resample_ctx(&s_tdm_resample_ctx, s_tdm_combined, (int)raw_frames,
                             TDM_RAW_SAMPLE_RATE, mono, STEP_SAMPLES, SAMPLE_RATE);
#if CONFIG_HANNAH_WAKEWORD_DEBUG
        int64_t tdm_timing_resample_us = esp_timer_get_time() - tdm_timing_t_resample0;
#endif

        /* Echo-Canceller vor dem Gain (Refs #226): Der Gain (Faktor 16) schneidet das Mikrofonsignal bei
         * lautem eigenem Lautsprecher hart ab (gemessen: ~21000 von 32767 bei Lautstärke 70), ein lineares
         * Filter kann das nicht mehr auslöschen. Die AEC ist linear und läuft deshalb davor. */
        aec_run(mono, STEP_SAMPLES);

        int32_t tdm_gain = hannah_config_get()->tdm_downmix_gain;
        for (size_t i = 0; i < STEP_SAMPLES; i++) {
            int32_t amplified = (int32_t)mono[i] * tdm_gain;
            mono[i] = (int16_t)(amplified > 32767 ? 32767 : amplified < -32768 ? -32768 : amplified);
        }
        size_t frames = STEP_SAMPLES;

#if CONFIG_HANNAH_WAKEWORD_DEBUG
        /* Timing-Instrumentierung (Refs #222) — Auswertung/Log-Fenster, s.
         * TDM_TIMING_LOG_FRAMES und Kommentar bei s_tdm_timing_loop_sum_us. */
        {
            int64_t loop_us = esp_timer_get_time() - tdm_timing_t0;
            s_tdm_timing_loop_sum_us     += loop_us;
            s_tdm_timing_resample_sum_us += tdm_timing_resample_us;
            if (loop_us > s_tdm_timing_loop_max_us)         s_tdm_timing_loop_max_us     = loop_us;
            if (tdm_timing_resample_us > s_tdm_timing_resample_max_us)
                s_tdm_timing_resample_max_us = tdm_timing_resample_us;
            if (loop_us > 10000) s_tdm_timing_over_budget++;  /* 10ms Sollzeit pro Iteration */
            if (++s_tdm_timing_frames >= TDM_TIMING_LOG_FRAMES) {
                ESP_LOGI(TAG, "TDM-Timing: loop avg=%lldus max=%lldus | resample avg=%lldus max=%lldus | %d/%d Iterationen > 10ms",
                         (long long)(s_tdm_timing_loop_sum_us / s_tdm_timing_frames),
                         (long long)s_tdm_timing_loop_max_us,
                         (long long)(s_tdm_timing_resample_sum_us / s_tdm_timing_frames),
                         (long long)s_tdm_timing_resample_max_us,
                         s_tdm_timing_over_budget, s_tdm_timing_frames);
                s_tdm_timing_loop_sum_us     = 0;
                s_tdm_timing_loop_max_us     = 0;
                s_tdm_timing_resample_sum_us = 0;
                s_tdm_timing_resample_max_us = 0;
                s_tdm_timing_over_budget     = 0;
                s_tdm_timing_frames          = 0;
            }
        }
#endif /* CONFIG_HANNAH_WAKEWORD_DEBUG */
#else
        /* I2S: 32-bit slots → linker Kanal (INMP441: MSB in bits[31:8]) */
        size_t frames    = bytes_read / 8;
        int32_t *s32     = (int32_t *)raw;
        for (size_t i = 0; i < frames; i++) {
            mono[i] = (int16_t)(s32[i * 2] >> 14);
        }
#endif
        size_t mono_samples = frames;

#if !CONFIG_HANNAH_MIC_TYPE_TDM
        aec_run(mono, mono_samples);   /* TDM: schon vor dem Gain gelaufen (s.o.) */
#endif

#if CONFIG_HANNAH_WAKEWORD_DEBUG
        /* Debug-Ringpuffer (#180): läuft immer mit, unabhängig vom State —
         * erfasst exakt das PCM, das auch durch die Wakeword-Pipeline läuft. */
        if (s_debug_ring) {
            for (size_t i = 0; i < mono_samples; i++) {
                s_debug_ring[s_debug_ring_wp] = mono[i];
                if (++s_debug_ring_wp >= DEBUG_WAV_RING_SAMPLES) {
                    s_debug_ring_wp   = 0;
                    s_debug_ring_full = true;
                }
            }
            s_debug_ring_frame_no++;
        }
#endif /* CONFIG_HANNAH_WAKEWORD_DEBUG */

        /* Warmup: Frontend füttern aber nicht triggern */
        if (warmup_remaining > 0) {
            --warmup_remaining;
#if CONFIG_HANNAH_WAKEWORD_ENABLED
            ww_push(mono);
#endif
            /* Noise-Floor während Warmup kalibrieren (nicht während TTS) */
            if (!s_speaking_active) {
                float rms_warmup = hannah_rms(mono, (int)mono_samples);
                s_noise_floor_ema = s_noise_floor_ema * 0.99f + rms_warmup * 0.01f;
            }
            if (warmup_remaining == 0) {
                update_idle_led();
                ESP_LOGI(TAG, "Mic-Warmup abgeschlossen. noise_ema=%.4f", s_noise_floor_ema);
            }
            vTaskDelay(pdMS_TO_TICKS(1));  /* taskYIELD reicht nicht — IDLE hat prio 0 und kommt bei vielen laufenden Boot-Tasks nie dran */
            continue;
        }

        if (s_wakeword_paused) {
            /* Muss vor den Mute-/Streaming-Paused-Checks unten geprüft werden (#240):
             * beide `continue`n vorher aus der Schleife, sodass dieser Block bei
             * einem gemuteten oder streaming-pausierten Satelliten nie erreicht
             * würde — hannah_audio_pause_wakeword() (OTA-Task) blockiert dann für
             * immer auf das hier fällige xSemaphoreGive() und das OTA hängt sich
             * beim allerersten Teardown-Schritt auf.
             * OTA läuft — Inference pausieren damit IDLE0 den WDT zurücksetzen kann.
             * xSemaphoreGive() bestätigt hannah_audio_pause_wakeword(), dass hier
             * gerade sicher kein hannah_wakeword_process()/Invoke() mehr läuft —
             * erst danach darf der OTA-Task die TFLite-Arena freigeben (#196).
             * Mit eigenem ww_task (Refs #226) bestätigt dieser das Parken, nicht mehr der mic_task. */
#if !CONFIG_HANNAH_WAKEWORD_ENABLED
            xSemaphoreGive(s_wakeword_parked_sem);
#endif
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        bool muted = hannah_net_is_muted();

        /* PTT durchbricht bewusst den Mute (#296) — physische Anwesenheit +
         * aktive Aktion direkt am Gerät. Mic-Stromversorgung wird nur für die
         * Dauer des Tastendrucks wieder zugeschaltet, der Mute-Zustand selbst
         * (hannah_net_is_muted(), MQTT-Retained-State, LED) bleibt unberührt. */
        if (muted && s_ptt_active) {
            if (!s_ptt_mic_power_override) {
                gpio_set_level(CONFIG_HANNAH_MUTE_HW_GPIO, 1);
                s_ptt_mic_power_override = true;
            }
        } else if (s_ptt_mic_power_override) {
            /* Override endet entweder weil PTT losgelassen wurde (weiterhin
             * muted → Strom aktiv wieder kappen) oder weil zwischenzeitlich
             * komplett entmutet wurde (Strom bleibt an, das übernimmt bereits
             * on_hw_mute() über den normalen Mute-Toggle-Pfad). */
            if (muted)
                gpio_set_level(CONFIG_HANNAH_MUTE_HW_GPIO, 0);
            s_ptt_mic_power_override = false;
        }

        if (muted && !s_ptt_active && state != AUDIO_STATE_STREAMING) {
            state = AUDIO_STATE_IDLE;
            /* Nicht während einer Ansage (#444): dieser Zweig läuft in jedem 10-ms-Frame und
             * hat SPEAK sonst sofort wieder durch MUTE ersetzt. */
            if (!s_sampling_mode && !s_speaking_active)
                update_idle_led();
            was_ptt = false;
            if (!s_sampling_mode) {
                vTaskDelay(pdMS_TO_TICKS(1));
                continue;  /* Im Sampling-Mode: Audio trotz Mute streamen; PTT: siehe oben */
            }
        }

        if (s_streaming_paused) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

/* -- Wakeword-Modus -------------------------------------------------- */
#if CONFIG_HANNAH_WAKEWORD_ENABLED
        {
            /* Inferenz läuft im ww_task (Refs #226): hier nur Frame abgeben und die größte Confidence
             * seit dem letzten Frame abholen (eine Frame-Latenz, ändert die Erkennung nicht). */
            ww_push(mono);
            float confidence = ww_take_confidence();

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


                /* PTT oder Wake-Word → Streaming starten */
                if ((s_ptt_active && !was_ptt) ||
                    confidence >= hannah_config_get()->wakeword_threshold / 100.0f) {
                    bool  by_wakeword  = !(s_ptt_active && !was_ptt);
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
/* Schreibt unter s_spk_mutex auf den Lautsprecher-I2S und führt Buch über die
 * geschriebenen Frames (Refs #440). Bei OTA-Pause (Kanal abgebaut) passiert
 * nichts, *written bleibt 0. */
static void spk_write_locked(const void *buf, size_t bytes, TickType_t timeout, size_t *written)
{
    *written = 0;
    xSemaphoreTake(s_spk_mutex, portMAX_DELAY);
    if (s_tx_chan && !s_hw_paused) {
        i2s_channel_write(s_tx_chan, buf, bytes, written, timeout);
        s_spk_frames_written += (uint32_t)(*written / 2);
    }
    xSemaphoreGive(s_spk_mutex);
}

static bool music_active(void)
{
    return s_mix_fifo && (esp_timer_get_time() - s_music_last_us) < MUSIC_ACTIVE_WINDOW_US;
}

/* Hängt bis zu n Samples hinten an die Mix-Warteschlange, liefert die Anzahl der
 * angenommenen Samples (kleiner n, wenn sie voll ist). */
static size_t mix_push(const int16_t *src, size_t n)
{
    xSemaphoreTake(s_mix_mutex, portMAX_DELAY);
    const size_t room = MIX_FIFO_SAMPLES - s_mix_count;
    if (n > room) n = room;
    size_t tail = (s_mix_head + s_mix_count) % MIX_FIFO_SAMPLES;
    for (size_t i = 0; i < n; i++) {
        s_mix_fifo[tail] = src[i];
        if (++tail == MIX_FIFO_SAMPLES) tail = 0;
    }
    s_mix_count += n;
    xSemaphoreGive(s_mix_mutex);
    return n;
}

static size_t mix_count(void)
{
    if (!s_mix_fifo) return 0;
    xSemaphoreTake(s_mix_mutex, portMAX_DELAY);
    const size_t c = s_mix_count;
    xSemaphoreGive(s_mix_mutex);
    return c;
}

static void mix_clear(void)
{
    if (!s_mix_fifo) return;
    xSemaphoreTake(s_mix_mutex, portMAX_DELAY);
    s_mix_head  = 0;
    s_mix_count = 0;
    xSemaphoreGive(s_mix_mutex);
}

/* Spielt Reste der Mix-Warteschlange direkt ab (Musik ist inzwischen gestoppt), damit
 * die Reihenfolge der TTS stimmt. */
static void mix_flush_direct(void)
{
    int16_t tmp[128];
    while (s_mix_fifo) {
        xSemaphoreTake(s_mix_mutex, portMAX_DELAY);
        size_t n = s_mix_count < 128 ? s_mix_count : 128;
        for (size_t i = 0; i < n; i++) {
            tmp[i] = s_mix_fifo[s_mix_head];
            if (++s_mix_head == MIX_FIFO_SAMPLES) s_mix_head = 0;
        }
        s_mix_count -= n;
        xSemaphoreGive(s_mix_mutex);
        if (n == 0) break;
        size_t written;
        spk_write_locked(tmp, n * sizeof(int16_t), pdMS_TO_TICKS(500), &written);
    }
}

/* Gibt einen TTS-Chunk (schon auf SPK_RATE, Lautstärke angewendet) aus. Läuft Musik,
 * geht er in die Mix-Warteschlange und music_write mischt ihn im Takt der Musik ein;
 * sonst direkt auf den Lautsprecher. */
static void tts_emit(const int16_t *buf, size_t n)
{
    size_t off = 0;
    while (off < n && !s_hw_paused) {
        if (music_active()) {
            off += mix_push(buf + off, n - off);
            if (off < n) vTaskDelay(pdMS_TO_TICKS(5));   /* voll: die Musik leert sie im Echtzeit-Takt */
        } else {
            mix_flush_direct();
            size_t written;
            spk_write_locked(buf + off, (n - off) * sizeof(int16_t), pdMS_TO_TICKS(500), &written);
            break;
        }
    }
}

#if SPK_UPSAMPLE > 1
/* TTS kommt mit SAMPLE_RATE, der Lautsprecher läuft mit dem SPK_UPSAMPLE-fachen:
 * lineare Interpolation, der letzte Wert wird über Chunk-Grenzen mitgeführt. */
static int16_t  s_up_prev = 0;
static int16_t *s_up_buf  = NULL;
static size_t   s_up_cap  = 0;   /* in Samples (nach dem Hochrechnen) */

static void upsample_linear(const int16_t *in, size_t n, int16_t *out)
{
    int32_t prev = s_up_prev;
    for (size_t i = 0; i < n; i++) {
        int32_t cur = in[i];
        for (int k = 1; k <= SPK_UPSAMPLE; k++)
            *out++ = (int16_t)(prev + ((cur - prev) * k) / SPK_UPSAMPLE);
        prev = cur;
    }
    s_up_prev = (int16_t)prev;
}
#endif

static void speaker_task(void *arg)
{
    bool was_speaking = false;
    char playback_done_topic[96];
    snprintf(playback_done_topic, sizeof(playback_done_topic),
             "hannah/satellite/%s/playback_done", hannah_config_get()->device_id);
    ESP_LOGI(TAG, "Speaker-Task gestartet.");
    while (1) {
        /* #200: Check muss am Loop-Anfang stehen, unabhängig vom Receive-
         * Ergebnis (siehe mic_task) — stand er hinter dem "kein Item"-Zweig,
         * wurde er nie erreicht, solange der Speaker idle ist (Normalfall
         * beim Start eines automatischen OTA-Checks): xRingbufferReceive
         * timeout't dann alle 1s und macht `continue`, ohne s_hw_paused je
         * zu sehen — s_speaker_parked_sem wurde nie gegeben, wodurch
         * hannah_audio_deinit_for_ota() für immer blockierte. */
        if (s_hw_paused) {
            /* OTA baut die I2S-Kanäle gerade ab/wieder auf — s_tx_chan nicht
             * anfassen. */
            xSemaphoreGive(s_speaker_parked_sem);
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        size_t item_size;
        spk_rb_item_t *item = xRingbufferReceive(s_spk_ringbuf, &item_size,
                                                   pdMS_TO_TICKS(1000));
        if (!item) {
            /* Timeout — keine neuen Chunks seit 1s → TTS abgeschlossen */
            if (mix_count() > 0 && !music_active())
                mix_flush_direct();   /* Musik ist gestoppt, Rest der Ansage noch in der Warteschlange */
            if (was_speaking) {
                was_speaking = false;
                s_speaking_active = false;
                if (!s_sampling_mode)
                    update_idle_led();
            }
            continue;
        }

        if (item->len == 0) {
            /* End-Sentinel: DMA-Buffer drainieren. Läuft Musik, mischt music_write den Rest
             * der Ansage noch ein: abwarten, bis die Mix-Warteschlange leer ist, dann
             * bleibt nur die Wartezeit der DMA-Puffer, Stille braucht es nicht. */
            for (int waited = 0; waited < 3000 && mix_count() > 0 && music_active(); waited += 10)
                vTaskDelay(pdMS_TO_TICKS(10));
            mix_flush_direct();
            if (music_active()) {
                vTaskDelay(pdMS_TO_TICKS(100));
            } else {
                static const uint8_t silence[8 * STEP_SAMPLES * 4 * 2] = {0};
                size_t written;
                spk_write_locked(silence, sizeof(silence), portMAX_DELAY, &written);
            }
#if SPK_UPSAMPLE > 1
            s_up_prev = 0;
#endif
            vRingbufferReturnItem(s_spk_ringbuf, item);
            was_speaking = false;
            s_speaking_active = false;
            /* Generisches "Wiedergabe fertig"-Signal an Core — DMA ist an dieser
             * Stelle wirklich drained, nicht nur der Ringbuffer geleert. Core kann
             * darauf warten statt eine feste Verzögerung zu raten (z.B. TriggerPlink). */
            hannah_net_mqtt_publish(playback_done_topic, "{}", 1, 0);
            if (!s_sampling_mode)
                update_idle_led();
            if (s_listen_after_tts && !s_sampling_mode) {
                s_listen_after_tts = false;
                s_virtual_listen_frames = 800;  /* 800 × 10ms = 8s */
                s_ptt_active = true;
            }
            continue;
        }
        if (!was_speaking && !s_sampling_mode) {
            /* Die Ansage zeigt SPEAK, sobald sie wirklich abgespielt wird, nicht erst/nur wenn
             * eine Server-Status-Meldung "speaking" ankommt (#444). */
            hannah_led_set_state(LED_STATE_SPEAK);
        }
        was_speaking = true;
        s_speaking_active = true;
        /* Lautstärke-Skalierung in-place (Ring-Buffer-Speicher ist schreibbar).
         * spk_gain ist die Rev5-Kopfraum-Reduktion (Refs #232, 100 = keine
         * Reduktion auf Rev4) — ein Multiply statt zweier Skalierungsschritte
         * gegen doppelten Rundungsfehler. */
        int vol = s_volume;
        int spk_gain = hannah_config_get()->spk_output_gain_percent;
        if (vol < 100 || spk_gain < 100) {
            int16_t *samples = (int16_t *)item->data;
            size_t count = item->len / 2;
            for (size_t i = 0; i < count; i++)
                samples[i] = (int16_t)(((int32_t)samples[i] * vol * spk_gain) / 10000);
        }
#if SPK_UPSAMPLE > 1
        const size_t n_in  = item->len / 2;
        const size_t n_out = n_in * SPK_UPSAMPLE;
        if (n_out > s_up_cap) {
            int16_t *nb = realloc(s_up_buf, n_out * sizeof(int16_t));
            if (nb) {
                s_up_buf = nb;
                s_up_cap = n_out;
            }
        }
        if (n_out <= s_up_cap && s_up_buf) {
            upsample_linear((const int16_t *)item->data, n_in, s_up_buf);
            tts_emit(s_up_buf, n_out);
        } else {
            ESP_LOGW(TAG, "TTS-Chunk verworfen: kein Speicher für die Hochrechnung (%u Samples)", (unsigned)n_out);
        }
#else
        tts_emit((const int16_t *)item->data, item->len / 2);
#endif
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
        update_idle_led();
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
            update_idle_led();
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
        update_idle_led();
}

static void on_notifications_pending(bool pending)
{
    s_notifications_pending = pending;
    if (!s_sampling_mode)
        update_idle_led();
}

static void volume_save_cb(void *arg)
{
    (void)arg;
    const int v = s_volume;
    if (v != s_volume_saved) {
        hannah_config_runtime_set_u8(VOLUME_NVS_KEY, (uint8_t)v);
        s_volume_saved = v;
        ESP_LOGI(TAG, "Lautstärke gespeichert: %d%%", v);
    }
    const int mv = s_music_volume;
    if (mv != s_music_volume_saved) {
        hannah_config_runtime_set_u8(MUSIC_VOLUME_NVS_KEY, (uint8_t)mv);
        s_music_volume_saved = mv;
        ESP_LOGI(TAG, "Musik-Lautstärke gespeichert: %d%%", mv);
    }
}

/* Merkt eine Lautstärkeänderung zum Speichern vor (Timer neu starten, damit mehrere schnelle
 * Änderungen nur einen Schreibvorgang ergeben). */
static void volume_changed(void)
{
    if (!s_volume_save_timer) return;
    esp_timer_stop(s_volume_save_timer);   /* ESP_ERR_INVALID_STATE, wenn er nicht lief: ok */
    esp_timer_start_once(s_volume_save_timer, VOLUME_SAVE_DELAY_US);
}

static void on_volume_set(int vol)
{
    s_volume = vol;
    ESP_LOGI(TAG, "Lautstärke gesetzt: %d%%", s_volume);
    hannah_net_publish_volume(vol);
    volume_changed();
}

static void on_music_volume_set(int vol)
{
    s_music_volume = vol;
    ESP_LOGI(TAG, "Musik-Lautstärke gesetzt (MQTT): %d%%", vol);
    hannah_net_publish_music_volume(vol);
    volume_changed();
}

/* ------------------------------------------------------------------ */
/* Öffentliche API                                                       */

void hannah_audio_init(void)
{
    /* Zuletzt eingestellte Lautstärke aus dem NVS (Refs #445); ohne gespeicherten Wert bleibt
     * CONFIG_HANNAH_VOLUME_DEFAULT. hannah_config_init() läuft vorher (main.c). */
    uint8_t saved_volume;
    if (hannah_config_runtime_get_u8(VOLUME_NVS_KEY, &saved_volume) && saved_volume <= 100) {
        s_volume = saved_volume;
        ESP_LOGI(TAG, "Lautstärke aus NVS: %d%%", s_volume);
    }
    s_volume_saved = s_volume;
    /* Musik-Lautstärke (Sendspin, Refs #446) getrennt davon; ohne Wert gilt der gleiche Standard. */
    if (hannah_config_runtime_get_u8(MUSIC_VOLUME_NVS_KEY, &saved_volume) && saved_volume <= 100) {
        s_music_volume = saved_volume;
        ESP_LOGI(TAG, "Musik-Lautstärke aus NVS: %d%%", s_music_volume);
    }
    s_music_volume_saved = s_music_volume;
    const esp_timer_create_args_t volume_timer_args = {
        .callback = volume_save_cb,
        .name     = "vol_save",
    };
    if (esp_timer_create(&volume_timer_args, &s_volume_save_timer) != ESP_OK)
        ESP_LOGW(TAG, "Timer zum Speichern der Lautstärke nicht angelegt — Lautstärke bleibt nicht erhalten.");

#if CONFIG_HANNAH_SPEAKER_ENABLED
    /* Internes DRAM — I2S-DMA benötigt cache-kohärenten Speicher; PSRAM ist nicht geeignet. */
    s_spk_ringbuf = xRingbufferCreate(SPK_RINGBUF_SIZE, RINGBUF_TYPE_NOSPLIT);
    s_spk_mutex   = xSemaphoreCreateMutex();
    /* Mix-Warteschlange für das Ducking der Sendspin-Musik (Refs #440), PSRAM: wird nur
     * per memcpy gelesen, der DMA sieht sie nie. Ohne sie fällt die Musik auf Verwerfen
     * während der Ansage zurück. */
    s_mix_mutex = xSemaphoreCreateMutex();
    s_mix_fifo  = (int16_t *)heap_caps_malloc(MIX_FIFO_SAMPLES * sizeof(int16_t),
                                              MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_mix_fifo)
        ESP_LOGW(TAG, "Mix-Warteschlange: PSRAM-Allokation fehlgeschlagen — kein Ducking.");
    speaker_init();
#endif
#if CONFIG_HANNAH_AEC
    /* Echo-Canceller (Refs #226) — nur sinnvoll, wenn Lautsprecher und Mikrofon existieren (Kconfig). Lebt
     * wie s_spk_ringbuf über den ganzen Boot-Zyklus und bleibt zwischen Ansagen eingeschwungen; bei der
     * OTA-Pause (hannah_audio_deinit_for_ota()) wird er nicht angefasst. */
    s_aec_ready = hannah_aec_init(&s_aec, STEP_SAMPLES, CONFIG_HANNAH_AEC_FILTER_LEN, SAMPLE_RATE) == 0;
    if (s_aec_ready)
        ESP_LOGI(TAG, "Echo-Canceller: Frame %d, Filter %d Taps (%d ms).", STEP_SAMPLES,
                 CONFIG_HANNAH_AEC_FILTER_LEN, CONFIG_HANNAH_AEC_FILTER_LEN * 1000 / SAMPLE_RATE);
    else
        ESP_LOGE(TAG, "Echo-Canceller nicht allokierbar — AEC aus.");
#endif
#if !CONFIG_HANNAH_MIC_TYPE_NONE
    mic_init();

#if CONFIG_HANNAH_WAKEWORD_DEBUG
    /* Debug-WAV-Snapshot (#180): Ringpuffer + Ausgabepuffer auf PSRAM. */
    s_debug_ring = (int16_t *)heap_caps_malloc(
        DEBUG_WAV_RING_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_debug_wav_snapshot = (uint8_t *)heap_caps_malloc(
        WAV_HEADER_BYTES + DEBUG_WAV_RING_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_debug_ring || !s_debug_wav_snapshot)
        ESP_LOGE(TAG, "Debug-WAV-Puffer: PSRAM-Allokation fehlgeschlagen — /debug/wav bleibt leer.");

#if CONFIG_HANNAH_MIC_TYPE_TDM
    /* Pre-Resample-Gegenstück (Refs #222, s. DEBUG_WAV_RING_SAMPLES_RAW). */
    s_debug_ring_raw = (int16_t *)heap_caps_malloc(
        DEBUG_WAV_RING_SAMPLES_RAW * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_debug_wav_raw_snapshot = (uint8_t *)heap_caps_malloc(
        WAV_HEADER_BYTES + DEBUG_WAV_RING_SAMPLES_RAW * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_debug_ring_raw || !s_debug_wav_raw_snapshot)
        ESP_LOGE(TAG, "Debug-WAV-Rohpuffer: PSRAM-Allokation fehlgeschlagen — /debug/wav/raw bleibt leer.");
#endif
#endif /* CONFIG_HANNAH_WAKEWORD_DEBUG */
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
    hannah_net_set_notifications_pending_callback(on_notifications_pending);
    hannah_net_set_volume_callback(on_volume_set);
    hannah_net_set_music_volume_callback(on_music_volume_set);
#if !CONFIG_HANNAH_MIC_TYPE_NONE
    hannah_net_set_sampling_callback(on_sampling_mode);
    hannah_net_set_virtual_ptt_callback(on_virtual_ptt);
    hannah_net_set_start_listening_callback(hannah_audio_start_listen_after_tts);
    s_mic_parked_sem = xSemaphoreCreateBinary();
    s_wakeword_parked_sem = xSemaphoreCreateBinary();
#if CONFIG_HANNAH_WAKEWORD_DEBUG
    s_debug_wav_done_sem = xSemaphoreCreateBinary();
#endif
#if CONFIG_HANNAH_WAKEWORD_ENABLED
    /* Vor dem mic_task, damit s_ww_task beim ersten ww_push() gesetzt ist. Stack im PSRAM: interner
     * Heap ist knapp (Heap-Watchdog, #266). Core 1 teilt sich nur den Speaker-Task, der fast nur wartet. */
    if (xTaskCreatePinnedToCoreWithCaps(ww_task, "ww", 8192, NULL, 4, &s_ww_task, 1, MALLOC_CAP_SPIRAM) != pdPASS)
        ESP_LOGE(TAG, "ww_task konnte nicht angelegt werden — keine Wake-Word-Erkennung.");
#endif
    xTaskCreatePinnedToCore(mic_task, "mic", 8192, NULL, 5, NULL, 0);
#else
    update_idle_led();
#endif
#if CONFIG_HANNAH_SPEAKER_ENABLED
    s_speaker_parked_sem = xSemaphoreCreateBinary();
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

#if CONFIG_HANNAH_SPEAKER_ENABLED
/* Skalierungspuffer für Musik: der Eingabepuffer gehört der Bibliothek und kann
 * bei teilweisem Schreiben erneut angeboten werden, darf also nicht in place
 * skaliert werden. Nur unter s_spk_mutex benutzt. */
static int16_t *s_music_buf = NULL;
static size_t   s_music_cap = 0;   /* in Samples */
#endif

size_t hannah_audio_music_write(const uint8_t *pcm, size_t len, uint32_t timeout_ms,
                                uint32_t *frames, int64_t *finish_us)
{
    if (frames)    *frames    = 0;
    if (finish_us) *finish_us = 0;
#if CONFIG_HANNAH_SPEAKER_ENABLED
    if (!pcm || len < 2 || !s_spk_mutex || s_hw_paused) return 0;

    s_music_last_us = esp_timer_get_time();
    s_ms_calls++;

    /* Ohne Mix-Warteschlange (PSRAM-Allokation fehlgeschlagen) hat die TTS Vorrang: der
     * Mutex allein lässt beide blockweise abwechselnd auf den I2S schreiben, dann ist weder
     * Musik noch Sprache zu verstehen (Refs #440). Die Musik wird dann nicht angehalten,
     * sondern angenommen und im Echtzeit-Takt verworfen, wie ein stummer Lautsprecher:
     * Music Assistant schickt weiter im Zeitplan der laufenden Musik, hielten wir die Daten
     * an, liefe der Eingangspuffer der Bibliothek voll und sie trennte die Verbindung. */
    if (s_speaking_active && !s_mix_fifo) {
        const uint32_t drop_frames = (uint32_t)(len / 2);
        uint32_t wait_ms = (uint32_t)((uint64_t)drop_frames * 1000 / SPK_RATE);
        if (wait_ms > timeout_ms) wait_ms = timeout_ms;
        vTaskDelay(pdMS_TO_TICKS(wait_ms));
        if (frames)    *frames    = drop_frames;
        if (finish_us) *finish_us = esp_timer_get_time();
        return (size_t)drop_frames * 2;
    }
    if (xSemaphoreTake(s_spk_mutex, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
        s_ms_fails++;
        return 0;
    }
    if (!s_tx_chan || s_hw_paused) {
        xSemaphoreGive(s_spk_mutex);
        s_ms_fails++;
        return 0;
    }

    const size_t n = len / 2;
    if (n > s_music_cap) {
        int16_t *nb = realloc(s_music_buf, n * sizeof(int16_t));
        if (!nb) {
            xSemaphoreGive(s_spk_mutex);
            s_ms_fails++;
            return 0;
        }
        s_music_buf = nb;
        s_music_cap = n;
    }
    /* Lautstärke und Ausgangsgain (Rev5-Kopfraum) wie bei TTS, als Q8-Faktor. */
    const int32_t gain_q8 = s_music_muted
        ? 0
        : (int32_t)(((int64_t)s_music_volume * hannah_config_get()->spk_output_gain_percent * 256) / 10000);
    const int16_t *in = (const int16_t *)pcm;

    /* Ducking: während einer Ansage (oder solange noch TTS-Samples in der Warteschlange
     * liegen) wird die Musik auf MUSIC_DUCK_PERCENT abgesenkt, schnell hinunter, langsam
     * wieder hinauf. Die Ansage wird im selben Takt aus der Mix-Warteschlange addiert. */
    const int32_t duck_min     = (int32_t)(65536 * MUSIC_DUCK_PERCENT / 100);
    const int32_t attack_step  = (65536 - duck_min) / (int32_t)(SPK_RATE * MUSIC_DUCK_ATTACK_MS / 1000);
    int32_t release_step       = (65536 - duck_min) / (int32_t)(SPK_RATE * MUSIC_DUCK_RELEASE_MS / 1000);
    if (release_step < 1) release_step = 1;

    size_t mix_have = 0;
    size_t mix_idx  = 0;
    if (s_mix_fifo) {
        xSemaphoreTake(s_mix_mutex, portMAX_DELAY);
        mix_have = s_mix_count < n ? s_mix_count : n;
        mix_idx  = s_mix_head;
    }
    const int32_t duck_target = (s_speaking_active || mix_have > 0) ? duck_min : 65536;
    int32_t in_pk = 0, out_pk = 0;
    for (size_t i = 0; i < n; i++) {
        if (s_duck_q16 > duck_target) {
            s_duck_q16 -= attack_step;
            if (s_duck_q16 < duck_target) s_duck_q16 = duck_target;
        } else if (s_duck_q16 < duck_target) {
            s_duck_q16 += release_step;
            if (s_duck_q16 > duck_target) s_duck_q16 = duck_target;
        }
        int32_t m = (int32_t)(((int64_t)(((int32_t)in[i] * gain_q8) >> 8) * s_duck_q16) >> 16);
        if (i < mix_have) {
            m += s_mix_fifo[mix_idx];
            if (++mix_idx == MIX_FIFO_SAMPLES) mix_idx = 0;
        }
        if (m > INT16_MAX) m = INT16_MAX;
        if (m < INT16_MIN) m = INT16_MIN;
        s_music_buf[i] = (int16_t)m;
        const int32_t a_in  = in[i] < 0 ? -(int32_t)in[i] : in[i];
        const int32_t a_out = m < 0 ? -m : m;
        if (a_in  > in_pk)  in_pk  = a_in;
        if (a_out > out_pk) out_pk = a_out;
    }
    if (in_pk  > s_ms_in_peak)  s_ms_in_peak  = (int16_t)(in_pk  > INT16_MAX ? INT16_MAX : in_pk);
    if (out_pk > s_ms_out_peak) s_ms_out_peak = (int16_t)(out_pk > INT16_MAX ? INT16_MAX : out_pk);
    if (s_mix_fifo) {
        s_mix_head   = mix_idx;
        s_mix_count -= mix_have;
        xSemaphoreGive(s_mix_mutex);
    }

    size_t written = 0;
    i2s_channel_write(s_tx_chan, s_music_buf, n * sizeof(int16_t), &written, pdMS_TO_TICKS(timeout_ms));
    written -= written % 2;
    const uint32_t wrote_frames = (uint32_t)(written / 2);
    s_spk_frames_written += wrote_frames;
    s_ms_frames += wrote_frames;
    if (wrote_frames == 0) s_ms_fails++;

    /* Ist der DMA leergelaufen (Stille wurde ausgegeben), zählt "ausgegeben" mehr
     * als "geschrieben": dann beginnt die Warteschlange neu bei 0. */
    const uint32_t sent = s_spk_frames_sent;
    if ((int32_t)(sent - s_spk_frames_written) > 0) s_spk_frames_written = sent;
    const int32_t queued = (int32_t)(s_spk_frames_written - sent);
    if (frames)    *frames    = wrote_frames;
    if (finish_us) *finish_us = esp_timer_get_time() + (int64_t)queued * 1000000LL / SPK_RATE;
    s_music_last_us = esp_timer_get_time();
    xSemaphoreGive(s_spk_mutex);
    return written;
#else
    (void)timeout_ms;
    return 0;
#endif
}

void hannah_audio_music_set_muted(bool muted)
{
    s_music_muted = muted;
}

void hannah_audio_music_get_stats(hannah_audio_music_stats_t *out)
{
    if (!out) return;
    memset(out, 0, sizeof(*out));
    out->volume       = s_volume;
    out->music_volume = s_music_volume;
    out->muted        = s_music_muted;
    out->duck_percent = 100;
#if CONFIG_HANNAH_SPEAKER_ENABLED
    out->calls        = s_ms_calls;
    out->fails        = s_ms_fails;
    out->frames       = s_ms_frames;
    out->frames_sent  = s_spk_frames_sent;
    out->in_peak      = s_ms_in_peak;
    out->out_peak     = s_ms_out_peak;
    out->duck_percent = (int)((int64_t)s_duck_q16 * 100 / 65536);
    s_ms_in_peak  = 0;
    s_ms_out_peak = 0;
#endif
}

int hannah_audio_get_volume(void)
{
    return s_volume;
}

void hannah_audio_set_volume(int percent)
{
    if (percent < 0)   percent = 0;
    if (percent > 100) percent = 100;
    s_volume = percent;
    ESP_LOGI(TAG, "Lautstärke gesetzt: %d%%", percent);
    hannah_net_publish_volume(percent);
    hannah_led_show_volume((uint8_t)percent);
    volume_changed();
}

int hannah_audio_get_music_volume(void)
{
    return s_music_volume;
}

void hannah_audio_set_music_volume(int percent)
{
    if (percent < 0)   percent = 0;
    if (percent > 100) percent = 100;
    s_music_volume = percent;
    ESP_LOGI(TAG, "Musik-Lautstärke gesetzt: %d%%", percent);
    hannah_net_publish_music_volume(percent);
    hannah_led_show_volume((uint8_t)percent);
    volume_changed();
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
#if CONFIG_HANNAH_SPEAKER_ENABLED
    mix_clear();
#endif
    if (!s_sampling_mode)
        update_idle_led();
}

void hannah_audio_pause(void)
{
    s_streaming_paused = true;
    update_idle_led();
}

void hannah_audio_resume(void)
{
    s_streaming_paused = false;
}

void hannah_audio_pause_wakeword(void)
{
    /* Kein mic_task (CONFIG_HANNAH_MIC_TYPE_NONE) — Semaphore existiert nicht,
     * es läuft ohnehin nie ein Invoke(), das auf die Arena zugreifen könnte. */
    if (s_wakeword_parked_sem) {
        /* Stale Give aus einem früheren Pause/Resume-Zyklus verwerfen, BEVOR
         * das Flag gesetzt wird — der anschließende blockierende Take darf nur
         * einen Give akzeptieren, der mic_task nachweislich NACH dem Setzen
         * des Flags erreicht hat (#196). */
        xSemaphoreTake(s_wakeword_parked_sem, 0);
        s_wakeword_paused = true;
        xSemaphoreTake(s_wakeword_parked_sem, portMAX_DELAY);
    } else {
        s_wakeword_paused = true;
    }
    ESP_LOGI(TAG, "Wakeword-Inference pausiert (OTA aktiv).");
}

void hannah_audio_resume_wakeword(void)
{
    s_wakeword_paused = false;
    ESP_LOGI(TAG, "Wakeword-Inference fortgesetzt (OTA fehlgeschlagen).");
}

/* Baut die I2S-Kanäle (Mic-RX, Speaker-TX) komplett ab und gibt deren
 * DMA-Puffer frei — anders als hannah_audio_pause_wakeword() (nur PSRAM)
 * betrifft das echtes internes DRAM (#193). Blockiert bis mic_task/
 * speaker_task bestätigt haben, dass sie gerade keinen I2S-Call in Flight
 * haben (s_hw_paused wird von beiden Tasks vor jedem i2s_channel_read()/
 * -write() geprüft), damit kein Kanal während eines laufenden Calls
 * gelöscht wird. Wird von hannah_ota.c vor esp_https_ota() aufgerufen. */
void hannah_audio_deinit_for_ota(void)
{
    s_hw_paused = true;

#if !CONFIG_HANNAH_MIC_TYPE_NONE
    xSemaphoreTake(s_mic_parked_sem, portMAX_DELAY);
    if (s_rx_chan) {
        esp_err_t err = i2s_channel_disable(s_rx_chan);
        if (err != ESP_OK)
            ESP_LOGW(TAG, "i2s_channel_disable(rx) fehlgeschlagen: %s", esp_err_to_name(err));
        err = i2s_del_channel(s_rx_chan);
        if (err != ESP_OK)
            ESP_LOGW(TAG, "i2s_del_channel(rx) fehlgeschlagen: %s", esp_err_to_name(err));
        s_rx_chan = NULL;
    }
#endif
#if CONFIG_HANNAH_SPEAKER_ENABLED
    xSemaphoreTake(s_speaker_parked_sem, portMAX_DELAY);
    /* Musik-Schreiber (Sendspin, Refs #440) können außerhalb von speaker_task
     * mitten in einem i2s_channel_write() stecken: Mutex abwarten, dann abbauen. */
    xSemaphoreTake(s_spk_mutex, portMAX_DELAY);
    if (s_tx_chan) {
        esp_err_t err = i2s_channel_disable(s_tx_chan);
        if (err != ESP_OK)
            ESP_LOGW(TAG, "i2s_channel_disable(tx) fehlgeschlagen: %s", esp_err_to_name(err));
        err = i2s_del_channel(s_tx_chan);
        if (err != ESP_OK)
            ESP_LOGW(TAG, "i2s_del_channel(tx) fehlgeschlagen: %s", esp_err_to_name(err));
        s_tx_chan = NULL;
    }
    xSemaphoreGive(s_spk_mutex);
#endif
    ESP_LOGI(TAG, "Audio-Hardware deinitialisiert (OTA aktiv) — I2S-DMA-Puffer freigegeben.");
}

/* Gegenstück zu hannah_audio_deinit_for_ota() — für den Fall, dass OTA
 * fehlschlägt und kein Neustart folgt. Baut die I2S-Kanäle über dieselben
 * (bisher nur beim Boot genutzten) mic_init()/speaker_init() wieder auf und
 * gibt mic_task/speaker_task frei. */
void hannah_audio_reinit_after_ota_failure(void)
{
#if CONFIG_HANNAH_SPEAKER_ENABLED
    speaker_init();
#endif
#if !CONFIG_HANNAH_MIC_TYPE_NONE
    mic_init();
#endif
    s_hw_paused = false;
    ESP_LOGI(TAG, "Audio-Hardware re-initialisiert (OTA fehlgeschlagen).");
}

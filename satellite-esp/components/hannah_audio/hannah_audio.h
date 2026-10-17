#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

/**
 * hannah_audio — PDM Mic-Array, Speaker, Tasten
 *
 * Hardware (PCB Rev.3):
 *   Mic:     2× SPH0641LU4H-1 (PDM, CLK=GPIO39, DATA=GPIO40, SEL trennt L/R)
 *   Speaker: MAX98357A (I2S, BCLK=GPIO47, LRC=GPIO38, DATA=GPIO21)
 *   Tasten:  PTT (GPIO12), Mute (GPIO11), Vol+ (GPIO13), Vol- (GPIO14)
 *            alle active-low, interner Pull-up aktiviert
 *   HW-Mute: NPN-Transistor via GPIO10 (HIGH = Mics aktiv)
 *
 * Pipeline Phase 1:
 *   PDM-Read → bei PTT gedrückt: hannah_net_send_audio()
 *              bei PTT losgelassen: hannah_net_send_audio_end()
 *
 * Pipeline Phase 2:
 *   PDM-Read → ESP-SR AFE (Beamforming + AEC + VAD) →
 *   Wake-Word → Stream-Start → audio_end bei Stille
 *
 * TTS-Wiedergabe:
 *   hannah_audio_play() nimmt PCM-Chunks entgegen und schreibt sie
 *   asynchron über den Speaker-Task auf I2S.
 */

void hannah_audio_init(void);

/* TTS-PCM-Chunk zur Wiedergabe einreihen (thread-safe). */
void hannah_audio_play(const uint8_t *pcm, size_t len, int sample_rate);

/* TTS-Stream abgeschlossen — Speaker-Task spielt verbleibende Chunks ab. */
void hannah_audio_play_end(void);

/* Musik (Sendspin, Refs #440): mono-16-Bit-PCM mit CONFIG_HANNAH_SPK_SAMPLE_RATE
 * auf den Lautsprecher schreiben. Skaliert mit Lautstärke und Ausgangsgain in
 * einem Zwischenpuffer (der Eingabepuffer bleibt unverändert). Läuft eine Ansage,
 * mischt diese Funktion sie ein und senkt die Musik dabei ab (Ducking).
 * Rückgabe: verbrauchte Bytes (0 bei OTA-Pause oder Timeout). *frames: tatsächlich
 * geschriebene Frames, *finish_us: geschätzte Zeit (esp_timer), zu der sie fertig
 * gespielt sind. */
size_t hannah_audio_music_write(const uint8_t *pcm, size_t len, uint32_t timeout_ms,
                                uint32_t *frames, int64_t *finish_us);

/* Diagnose der Musikwiedergabe (Refs #440), um Stille einzugrenzen: Zähler sind
 * kumulativ, die Spitzenwerte gelten seit dem letzten Abruf und werden dabei
 * zurückgesetzt (ein Aufrufer). */
typedef struct {
    uint32_t calls;        /* Aufrufe von hannah_audio_music_write() */
    uint32_t fails;        /* davon ohne geschriebene Frames */
    uint32_t frames;       /* geschriebene Musik-Frames */
    uint32_t frames_sent;  /* vom DMA ausgegebene Frames (Musik, TTS und Stille) */
    int16_t  in_peak;      /* größter Betrag der gelieferten Musik */
    int16_t  out_peak;     /* größter Betrag auf dem Lautsprecher (nach Lautstärke, Ducking, Mix) */
    int      volume;       /* Hannah-Lautstärke (Ansagen, Wecker) in Prozent */
    int      music_volume; /* Musik-Lautstärke in Prozent */
    bool     muted;        /* Musik stumm */
    int      duck_percent; /* aktueller Ducking-Faktor in Prozent, 100 = unabgesenkt */
} hannah_audio_music_stats_t;
void hannah_audio_music_get_stats(hannah_audio_music_stats_t *out);

/* Musik stumm schalten (nur die Sendspin-Wiedergabe, nicht TTS und nicht die Mikrofone). */
void hannah_audio_music_set_muted(bool muted);

/* Hannah-Lautstärke (Ansagen, Wecker, Sound-Assets) 0-100 lesen/setzen. set meldet den Wert
 * per MQTT (.../volume/state) und zeigt ihn am LED-Ring. */
int  hannah_audio_get_volume(void);
void hannah_audio_set_volume(int percent);

/* Musik-Lautstärke (Sendspin, Refs #446) 0-100, getrennt von der Hannah-Lautstärke. Die Tasten
 * am Satelliten bedienen sie, solange Musik läuft und keine Ansage spricht. set meldet den Wert
 * per MQTT (.../music_volume/state) und zeigt ihn am LED-Ring. */
int  hannah_audio_get_music_volume(void);
void hannah_audio_set_music_volume(int percent);

/* Playback-Steuerung (stop/pause/resume via UDP-Control). */
void hannah_audio_stop(void);    /* Speaker-Queue leeren, Streaming stoppen. */
void hannah_audio_pause(void);   /* Mic-Streaming pausieren. */
void hannah_audio_resume(void);  /* Mic-Streaming fortsetzen. */

/* Wakeword-Capture-Modus: Speaker-Ausgabe blockieren, LED auf CAPTURE setzen.
 * Wird von hannah_net bei hannah/satellite/<device>/sampling aufgerufen. */
void hannah_audio_set_sampling_mode(bool enabled);

/* Nach TTS-Wiedergabe automatisch in Listening-Mode wechseln (für ask-Befehl).
 * Wird vom nächsten tts_end-Sentinel ausgelöst; läuft 8s oder bis PTT-Release. */
void hannah_audio_start_listen_after_tts(void);

/* Wakeword-Inference pausieren — mic_task schläft statt TFLite auszuführen.
 * Wird von OTA aufgerufen um IDLE0-Starvation während des Downloads zu vermeiden.
 * Bei Erfolg reboot't OTA danach ohnehin; bei Fehlschlag siehe
 * hannah_audio_resume_wakeword(). */
void hannah_audio_pause_wakeword(void);

/* Gegenstück zu hannah_audio_pause_wakeword() — für den Fall, dass OTA
 * fehlschlägt und kein Neustart folgt (kein manueller Eingriff nötig). */
void hannah_audio_resume_wakeword(void);

/* Baut die I2S-Kanäle (Mic-RX, Speaker-TX) komplett ab und gibt deren
 * DMA-Puffer (internes DRAM) frei — für mehr Headroom während OTA (#193).
 * Blockiert bis mic_task/speaker_task sicher geparkt sind. Bei Erfolg
 * reboot't OTA danach ohnehin; bei Fehlschlag siehe
 * hannah_audio_reinit_after_ota_failure(). */
void hannah_audio_deinit_for_ota(void);

/* Gegenstück zu hannah_audio_deinit_for_ota() — für den Fall, dass OTA
 * fehlschlägt und kein Neustart folgt. */
void hannah_audio_reinit_after_ota_failure(void);

/* Debug (#180): letzter per Tastenkombi (Vol+ und Vol- gleichzeitig ~700ms
 * gehalten) eingefrorener Roh-PCM-Snapshot als fertige WAV (inkl. Header).
 * Läuft unabhängig vom Sampling-/Capture-Modus im normalen Wakeword-Betrieb
 * mit. Liefert false, wenn seit Boot noch keine Aufnahme ausgelöst wurde. */
bool hannah_audio_get_debug_wav(const uint8_t **out_buf, size_t *out_len);

/* Debug (Refs #222): Gegenstück zu hannah_audio_get_debug_wav(), aber das
 * Signal *vor* hannah_resample_ctx()/Gain (TDM_RAW_SAMPLE_RATE, also bei
 * TDM 48kHz statt 16kHz) — vom selben Trigger im selben Moment eingefroren,
 * zum direkten Vergleich vor/nach dem Downsample-Filter. Nur unter
 * CONFIG_HANNAH_MIC_TYPE_TDM verfügbar (PDM/I2S haben keinen separaten
 * Pre-Resample-Schritt), liefert sonst immer false. */
bool hannah_audio_get_debug_wav_raw(const uint8_t **out_buf, size_t *out_len);

/* Debug (#194): löst denselben Snapshot wie die Vol+/Vol--Tastenkombi aus,
 * aber remote (z.B. per Webserver-Endpoint) statt per physischem Tastendruck
 * — für Tests aus normalem Nutzungsabstand, wo man nicht gleichzeitig die
 * Tasten halten und auf Abstand gehen kann. Blockiert für das Sprechfenster
 * (siehe DEBUG_WAV_REMOTE_TRIGGER_FRAMES) und liefert danach true, sobald der
 * Snapshot fertig ist (per hannah_audio_get_debug_wav() abrufbar). Liefert
 * false bei Timeout (mic_task nicht erreichbar, z.B. während OTA-Pause). */
bool hannah_audio_trigger_debug_wav_capture(void);

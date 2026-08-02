#pragma once
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

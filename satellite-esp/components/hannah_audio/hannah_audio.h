#pragma once
#include <stdint.h>
#include <stddef.h>

/**
 * hannah_audio — I2S Mic-Array, Speaker, PTT-Button
 *
 * Hardware:
 *   Mic:    2× INMP441 (I2S0, stereo L/R, gleiche BCK/WS/DATA-Leitung)
 *   Speaker: MAX98357A  (I2S1, mono)
 *   PTT:    GPIO-Taster (active-low, interner Pull-up)
 *             Phase 1: Halten = Aufnahme streamen, Loslassen = audio_end
 *
 * Pipeline Phase 1:
 *   I2S-Read (stereo) → Links-Kanal extrahieren →
 *   bei PTT gedrückt: hannah_net_send_audio()
 *   bei PTT losgelassen: hannah_net_send_audio_end()
 *
 * Pipeline Phase 2:
 *   I2S-Read → ESP-SR AFE (Beamforming + AEC + VAD) →
 *   Wake-Word → Stream-Start → audio_end bei Stille
 *
 * TTS-Wiedergabe:
 *   hannah_audio_play() nimmt PCM-Chunks entgegen und schreibt sie
 *   asynchron über den Speaker-Task auf I2S1.
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

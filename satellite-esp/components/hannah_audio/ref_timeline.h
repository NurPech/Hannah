#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

/**
 * ref_timeline — Versatzschätzer für die AEC-Referenz-Zeitachse (Refs #226)
 *
 * Mikrofon-RX und Lautsprecher-TX laufen mit derselben Rate (48 kHz), derselben Taktquelle und demselben
 * mclk-Teiler, der Versatz zwischen beiden Zeitachsen ist also konstant. Jede Einzelmessung (Ausgabezähler zum Zeitpunkt,
 * an dem der mic_task einen Frame verarbeitet) rauscht aber nur nach oben: wacht der mic_task spät auf (z. B. beim Start eines
 * Assets, wenn das SPIFFS-Lesen ihn verdrängt), ist die Messung um die Verspätung zu hoch.
 *
 * Einzelne verspätete Messungen dürfen den Versatz deshalb nicht verschieben: ein Regler, der ihnen nach oben folgt,
 * verschiebt die Referenz um bis zu +15 Samples (in den ersten Sekunden einer Wiedergabe) und springt nach unten
 * sofort zurück (Sägezahn).
 *
 * Hier: Fenster-Minimum (ein Fenster = REF_TL_WIN_FRAMES Mikrofon-Frames) statt Einzelmessung, danach fester Versatz
 * mit sehr kleiner Schrittweite. Sprünge werden nur übernommen, wenn sie über mehrere Fenster bestehen: nach oben
 * erst nach REF_TL_PENDING_UP Fenstern (eine Verspätung von Sekunden ist keine echte Verschiebung), nach unten nach
 * REF_TL_PENDING_DOWN (Rauschen geht nie nach unten, sonst würde die Referenz vor das beschriebene Ende gelesen).
 *
 * Reine Rechenlogik ohne ESP-Abhängigkeit, damit sie auf dem Host getestet werden kann
 * (test_host/test_ref_timeline.c).
 */

#define REF_TL_WIN_FRAMES   100   /* Mikrofon-Frames je Fenster (10 ms je Frame = 1 s) */
#define REF_TL_SLEW_PER_WIN 1     /* Nachführung in Ausgabe-Frames je Fenster (bei 48 kHz ~ 7 ppm) */
#define REF_TL_PENDING_UP   5     /* Fenster, die ein Sprung nach oben bestehen muss */
#define REF_TL_PENDING_DOWN 2     /* ... nach unten */

typedef struct {
    bool    started;
    bool    locked;        /* erstes Fenster abgeschlossen */
    int64_t off;           /* aktueller Versatz in Ausgabe-Frames */
    int64_t win_min;       /* kleinste Messung im laufenden Fenster */
    int32_t win_n;
    int32_t pending_up;
    int32_t pending_down;
    int64_t jump_frames;   /* ab dieser Abweichung gilt ein Fenster als "Sprung" (z. B. 5 ms in Ausgabe-Frames) */
    uint32_t jumps;        /* Diagnose: übernommene Sprünge */
} ref_tl_t;

void ref_tl_init(ref_tl_t *s, int64_t jump_frames);

/* Pro Mikrofon-Frame mit der Einzelmessung aufrufen; liefert den Versatz, der für diesen Frame gilt. */
int64_t ref_tl_update(ref_tl_t *s, int64_t meas);

/* Referenz-Zeitachse: Ring aus tl_len Samples bei SAMPLE_RATE (Zweierpotenz). Position p16 liegt an Index
 * p16 & (tl_len - 1). Die Ausgabe läuft mit upsample * SAMPLE_RATE (bei 48 kHz: 3). */

/* Legt ein vom TX-DMA ausgegebenes Stück ab (dma: n Frames bei Ausgabe-Rate, gemittelt über je upsample Frames).
 * s0: Ausgabezähler (Ausgabe-Frames) vor dem Stück, ein Vielfaches von upsample. Liefert das neue Ende, die erste
 * noch nicht beschriebene Position. static inline, weil es im I2S-Interrupt läuft und nicht im Flash liegen darf. */
static inline uint32_t ref_tl_store(int16_t *tl, uint32_t tl_len, uint32_t s0, const int16_t *dma, uint32_t n,
                                    int upsample)
{
    const uint32_t up  = (uint32_t)upsample;
    uint32_t       p16 = s0 / up;
    for (uint32_t i = 0; i + up <= n; i += up) {
        int32_t acc = 0;
        for (uint32_t k = 0; k < up; k++) acc += dma[i + k];
        tl[p16 & (tl_len - 1)] = (int16_t)(acc / (int32_t)up);
        p16++;
    }
    return p16;
}

/* Liest n Samples, die bei end48 (Ausgabe-Frames) enden, in out. tl_end16: erste noch nicht beschriebene Position.
 * Was noch nicht beschrieben oder schon überschrieben ist, bleibt 0 (Stille). Liegt die Position zwischen zwei
 * SAMPLE_RATE-Samples, wird mit Catmull-Rom über vier Nachbarn interpoliert (Abrunden schadet den hohen
 * Frequenzen); fehlen rechts die Nachbarn, bleibt es beim abgerundeten Sample. */
void ref_tl_read(const int16_t *tl, uint32_t tl_len, uint32_t tl_end16, int64_t end48, int upsample,
                 int16_t *out, size_t n);

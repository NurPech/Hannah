/* Host-Test für ref_timeline.c (Refs #226): Versatzschätzer der AEC-Referenz-Zeitachse bei Messrauschen.
 *
 * Bauen und laufen lassen (Host, ohne ESP-IDF):
 *   gcc -O2 -Wall -Wextra -Werror -I.. ../ref_timeline.c test_ref_timeline.c -lm -o test_ref_timeline && ./test_ref_timeline
 *
 * Messmodell (Ausgabe-Frames bei 48 kHz, ein Mikrofon-Frame = 480): meas = Versatz + Verspätung des mic_task (nur >= 0):
 *   - Grundrauschen: exponentiell, Mittel 24 Frames (0,5 ms)
 *   - 3 % der Frames zusätzlich 5..50 ms spät (Aufwachen des mic_task)
 *   - Frame 0: bis ~98 ms spät (Start)
 *   - Wiedergabestart bei Frame 300: 1,8 s lang konstant ~21 ms spät (mic_task wird verdrängt, holt dann auf)
 *   - Frame 2000: echte Verschiebung des Versatzes um +480 Frames (ein verlorener Mikrofon-Frame), bleibt bestehen
 * Prüfung: Abweichung des geschätzten Versatzes vom wahren, in 16-kHz-Samples (= Frames / 3).
 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "ref_timeline.h"

#define SPK_RATE 48000

static uint64_t rng = 88172645463325252ULL;
static double urand(void)
{
    rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
    return (double)(rng >> 11) / 9007199254740992.0;
}

static int64_t lateness(int k)
{
    double l = -24.0 * log(1.0 - urand());
    if (urand() < 0.03) l += 240 + urand() * 2160;
    if (k == 0) l += 4700;
    if (k >= 300 && k < 480) l += 1000 + urand() * 40;
    return (int64_t)l;
}

typedef struct { double max, sum2; int n; } seg_t;
static void seg_add(seg_t *s, double e) { double a = fabs(e); if (a > s->max) s->max = a; s->sum2 += e * e; s->n++; }

int main(void)
{
    const int64_t O = 1000000;
    const int total = 4000;
    ref_tl_t tl;
    ref_tl_init(&tl, 5 * (SPK_RATE / 1000));
    seg_t seg[5] = {{0}};
    const char *names[5] = {"Start (Frame 0..99)", "vor Wiedergabe (100..299)", "Verspaetung 1,8 s (300..479)",
                            "danach (480..1999)", "nach Verschiebung (2300..3999)"};
    int conv = -1;
    for (int k = 0; k < total; k++) {
        const int64_t truth = k < 2000 ? O : O + 480;
        const int64_t est = ref_tl_update(&tl, truth + lateness(k));
        const double e = (double)(est - truth) / 3.0;
        int s = k < 100 ? 0 : k < 300 ? 1 : k < 480 ? 2 : k < 2000 ? 3 : (k >= 2300 ? 4 : -1);
        if (s >= 0) seg_add(&seg[s], e);
        if (k >= 2000 && conv < 0 && fabs(e) < 1.0) conv = k - 2000;
    }
    printf("Fehler des geschaetzten Versatzes gegen den wahren Versatz (Samples @16 kHz)\n");
    for (int i = 0; i < 5; i++)
        printf("  %-34s max %6.1f rms %6.2f\n", names[i], seg[i].max, seg[i].n ? sqrt(seg[i].sum2 / seg[i].n) : 0);
    printf("Nach der echten Verschiebung um +480 Frames (= 160 Samples): unter 1 Sample nach %d Frames (je 10 ms)\n", conv);
    printf("Uebernommene Spruenge: %u\n", tl.jumps);

    /* Abnahme: die Verspaetung wirkt nicht auf den Schaetzer, nach ihr bleibt der Fehler klein, die echte
     * Verschiebung wird rechtzeitig uebernommen. */
    int ok = 1;
    if (seg[2].max > 3.0) { printf("FEHLER: Verspaetung wirkt auf den Schaetzer (max %.1f)\n", seg[2].max); ok = 0; }
    if (seg[3].max > 3.0) { printf("FEHLER: nach der Verspaetung zu gross (max %.1f)\n", seg[3].max); ok = 0; }
    if (conv < 0 || conv > 800) { printf("FEHLER: echte Verschiebung nicht rechtzeitig uebernommen (%d)\n", conv); ok = 0; }
    printf(ok ? "OK\n" : "FEHLGESCHLAGEN\n");
    return ok ? 0 : 1;
}

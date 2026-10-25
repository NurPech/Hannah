/* Host-Test für ref_tl_store() und ref_tl_read() (Refs #226): Ablegen des vom TX-DMA ausgegebenen Stücks auf der
 * Referenz-Zeitachse und Lesen mit Interpolation.
 *
 * Bauen und laufen lassen (Host, ohne ESP-IDF):
 *   gcc -O2 -Wall -Wextra -Werror -I.. ../ref_timeline.c test_ref_tl_io.c -lm -o test_ref_tl_io && ./test_ref_tl_io
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ref_timeline.h"

#define TL_LEN 64
#define UP     3

static int fails = 0;
#define CHECK(cond, ...) do { if (!(cond)) { printf("FEHLER: "); printf(__VA_ARGS__); printf("\n"); fails++; } } while (0)

/* Zeitachse mit Rampe füllen: Position p hat den Wert 100 * p. */
static uint32_t fill_ramp(int16_t *tl, uint32_t count)
{
    int16_t dma[UP * 8];
    uint32_t end = 0;
    for (uint32_t p = 0; p < count; p += 8) {
        for (uint32_t k = 0; k < 8; k++)
            for (int u = 0; u < UP; u++) dma[k * UP + u] = (int16_t)(100 * (p + k));
        end = ref_tl_store(tl, TL_LEN, p * UP, dma, 8 * UP, UP);
    }
    return end;
}

static void test_store_averages(void)
{
    int16_t tl[TL_LEN] = {0};
    const int16_t dma[6] = {3, 3, 6, 9, 12, 12};   /* Mittel je 3 Frames: 4, 11 */
    uint32_t end = ref_tl_store(tl, TL_LEN, 0, dma, 6, UP);
    CHECK(end == 2, "Ende nach dem ersten Stueck: %u statt 2", end);
    CHECK(tl[0] == 4 && tl[1] == 11, "Mittelwerte %d, %d statt 4, 11", tl[0], tl[1]);

    /* zweites Stueck schliesst an */
    end = ref_tl_store(tl, TL_LEN, 6, dma, 6, UP);
    CHECK(end == 4 && tl[2] == 4 && tl[3] == 11, "zweites Stueck liegt an Position 2..3");
}

static void test_store_wraps(void)
{
    int16_t tl[TL_LEN] = {0};
    const int16_t dma[3] = {30, 30, 30};
    /* Position TL_LEN - 1, danach TL_LEN: der Ring laeuft ueber */
    uint32_t end = ref_tl_store(tl, TL_LEN, (TL_LEN - 1) * UP, dma, 3, UP);
    CHECK(end == TL_LEN && tl[TL_LEN - 1] == 30, "Ablage an der letzten Position");
    end = ref_tl_store(tl, TL_LEN, TL_LEN * UP, dma, 3, UP);
    CHECK(end == TL_LEN + 1 && tl[0] == 30, "Ablage nach dem Ueberlauf an Index 0 (end %u)", end);
}

static void test_store_no_upsample(void)
{
    int16_t tl[TL_LEN] = {0};
    const int16_t dma[4] = {1, -2, 3, -4};
    uint32_t end = ref_tl_store(tl, TL_LEN, 10, dma, 4, 1);
    CHECK(end == 14 && tl[10] == 1 && tl[11] == -2 && tl[12] == 3 && tl[13] == -4, "ohne Hochrechnung 1:1");
}

static void test_read_integer_positions(void)
{
    int16_t tl[TL_LEN] = {0};
    const uint32_t end = fill_ramp(tl, 32);
    int16_t out[4];
    /* Ende bei Position 20 (end48 = 20 * UP), 4 Samples: Positionen 17..20 */
    ref_tl_read(tl, TL_LEN, end, 20 * UP, UP, out, 4);
    CHECK(out[0] == 1700 && out[1] == 1800 && out[2] == 1900 && out[3] == 2000,
          "ganze Positionen: %d %d %d %d", out[0], out[1], out[2], out[3]);
}

static void test_read_interpolates_linear_ramp(void)
{
    int16_t tl[TL_LEN] = {0};
    const uint32_t end = fill_ramp(tl, 32);
    int16_t out[4];
    /* ein Ausgabe-Frame (1/3 Sample) hinter Position 20: Catmull-Rom reproduziert eine Gerade */
    ref_tl_read(tl, TL_LEN, end, 20 * UP + 1, UP, out, 4);
    const int expect[4] = {1733, 1833, 1933, 2033};
    for (int i = 0; i < 4; i++)
        CHECK(abs(out[i] - expect[i]) <= 1, "Interpolation %d: %d statt %d", i, out[i], expect[i]);
    ref_tl_read(tl, TL_LEN, end, 20 * UP + 2, UP, out, 4);
    CHECK(abs(out[3] - 2067) <= 1, "Interpolation bei 2/3: %d statt 2067", out[3]);
}

static void test_read_silence_outside(void)
{
    int16_t tl[TL_LEN] = {0};
    const uint32_t end = fill_ramp(tl, 32);
    int16_t out[4];

    /* noch nicht beschrieben: Positionen 30..33 (Ende der Zeitachse ist 32) */
    ref_tl_read(tl, TL_LEN, end, 33 * UP, UP, out, 4);
    CHECK(out[2] == 0 && out[3] == 0, "noch nicht beschrieben muss Stille sein (%d %d)", out[2], out[3]);
    CHECK(out[0] == 3000 && out[1] == 3100, "beschriebener Teil bleibt erhalten (%d %d)", out[0], out[1]);

    /* schon ueberschrieben: Zeitachse wurde 3 * TL_LEN weiter beschrieben, Position 5 ist laengst weg */
    int16_t big[TL_LEN] = {0};
    const uint32_t end2 = fill_ramp(big, 3 * TL_LEN);
    ref_tl_read(big, TL_LEN, end2, 5 * UP, UP, out, 4);
    CHECK(out[0] == 0 && out[1] == 0 && out[2] == 0 && out[3] == 0, "ueberschrieben muss Stille sein");

    /* zu nah am Ende: rechts fehlen Nachbarn, dann abgerundetes Sample statt Interpolation */
    ref_tl_read(tl, TL_LEN, end, 31 * UP + 1, UP, out, 1);
    CHECK(out[0] == 3100, "ohne rechte Nachbarn abgerundet (%d)", out[0]);
}

static void test_read_wraps(void)
{
    int16_t tl[TL_LEN] = {0};
    const uint32_t end = fill_ramp(tl, TL_LEN + 10);   /* Ring ist einmal ueber den Rand gelaufen */
    int16_t out[3];
    ref_tl_read(tl, TL_LEN, end, (TL_LEN + 5) * UP, UP, out, 3);
    CHECK(out[0] == 100 * (TL_LEN + 3) && out[2] == 100 * (TL_LEN + 5), "Lesen ueber den Rand (%d %d)", out[0], out[2]);
}

int main(void)
{
    test_store_averages();
    test_store_wraps();
    test_store_no_upsample();
    test_read_integer_positions();
    test_read_interpolates_linear_ramp();
    test_read_silence_outside();
    test_read_wraps();
    printf(fails ? "FEHLGESCHLAGEN (%d)\n" : "OK\n", fails);
    return fails ? 1 : 0;
}

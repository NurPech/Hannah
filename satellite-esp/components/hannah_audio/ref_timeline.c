#include "ref_timeline.h"

#include <math.h>

void ref_tl_init(ref_tl_t *s, int64_t jump_frames)
{
    s->started      = false;
    s->locked       = false;
    s->off          = 0;
    s->win_min      = INT64_MAX;
    s->win_n        = 0;
    s->pending_up   = 0;
    s->pending_down = 0;
    s->jump_frames  = jump_frames;
    s->jumps        = 0;
}

int64_t ref_tl_update(ref_tl_t *s, int64_t meas)
{
    if (!s->started) {
        s->started = true;
        s->off     = meas;
    }
    if (meas < s->win_min) s->win_min = meas;
    s->win_n++;

    if (!s->locked) {
        /* Einschwingen im ersten Fenster: eine kleinere Messung wird sofort übernommen, nach oben nie. */
        if (meas < s->off) s->off = meas;
        if (s->win_n >= REF_TL_WIN_FRAMES) {
            s->off     = s->win_min;
            s->locked  = true;
            s->win_n   = 0;
            s->win_min = INT64_MAX;
        }
        return s->off;
    }

    if (s->win_n >= REF_TL_WIN_FRAMES) {
        const int64_t dev = s->win_min - s->off;   /* > 0: Messungen höher, < 0: tiefer als der Versatz */
        s->win_n   = 0;
        s->win_min = INT64_MAX;
        const int64_t adev = dev < 0 ? -dev : dev;
        if (adev > s->jump_frames) {
            if (dev > 0) {
                s->pending_down = 0;
                if (++s->pending_up >= REF_TL_PENDING_UP) {
                    s->off = s->off + dev;
                    s->pending_up = 0;
                    s->jumps++;
                }
            } else {
                s->pending_up = 0;
                if (++s->pending_down >= REF_TL_PENDING_DOWN) {
                    s->off = s->off + dev;
                    s->pending_down = 0;
                    s->jumps++;
                }
            }
        } else {
            s->pending_up = s->pending_down = 0;
            if (dev > 0)      s->off += dev < REF_TL_SLEW_PER_WIN ? dev : REF_TL_SLEW_PER_WIN;
            else if (dev < 0) s->off -= adev < REF_TL_SLEW_PER_WIN ? adev : REF_TL_SLEW_PER_WIN;
        }
    }
    return s->off;
}

void ref_tl_read(const int16_t *tl, uint32_t tl_len, uint32_t tl_end16, int64_t end48, int upsample,
                 int16_t *out, size_t n)
{
    const uint32_t mask = tl_len - 1;
    const uint32_t up   = (uint32_t)upsample;
    for (size_t i = 0; i < n; i++) {
        out[i] = 0;
        const int64_t  q48    = end48 - (int64_t)(n - 1 - i) * upsample;
        const uint32_t p16    = (uint32_t)q48 / up;
        const int32_t  ahead  = (int32_t)(p16 - tl_end16);       /* >= 0: noch nicht beschrieben = Stille */
        const int32_t  behind = (int32_t)(tl_end16 - p16);       /* > tl_len: überschrieben */
        if (ahead >= 0 || behind > (int32_t)tl_len) continue;

        const uint32_t frac = (uint32_t)q48 % up;
        if (frac && (int32_t)(p16 + 2 - tl_end16) < 0 && behind < (int32_t)tl_len - 1) {
            const float f  = (float)frac / (float)up;
            const float p0 = tl[(p16 - 1) & mask];
            const float p1 = tl[p16 & mask];
            const float p2 = tl[(p16 + 1) & mask];
            const float p3 = tl[(p16 + 2) & mask];
            float v = p1 + 0.5f * f * ((p2 - p0) + f * ((2.0f * p0 - 5.0f * p1 + 4.0f * p2 - p3) + f * (3.0f * (p1 - p2) + p3 - p0)));
            if (v > 32767.0f)  v = 32767.0f;
            if (v < -32768.0f) v = -32768.0f;
            out[i] = (int16_t)lrintf(v);
            continue;
        }
        out[i] = tl[p16 & mask];
    }
}

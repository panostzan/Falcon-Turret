/* Color tracker in portable C, for running natively on QNX on the Raspberry Pi.
 *
 * It is a port of laptop/tracker.py and is meant to give the same answers: same half-size working picture, same
 * HSV thresholds (OpenCV convention: hue 0..180), same blob grouping, selection, smoothing and confidence rules.
 * Differences, on purpose: the "join nearby fragments" step uses a square window instead of an ellipse, and the
 * borders of the blur are handled by replication. laptop/tests/test_qnx_tracker.py checks the two against each other.
 *
 * No dynamic allocation after tracker_new(), no threads, no OS calls: it builds with any C99 compiler (qcc for QNX,
 * gcc, clang, zig cc, MSVC), so the same file is tested on the laptop and runs on QNX.
 */
#ifndef TURRET_TRACKER_H
#define TURRET_TRACKER_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int    found;                       /* 1 if the target was found in this picture */
    double cx, cy, w, h;                /* its box, in full-picture pixels */
    double confidence;                  /* 0..1, see the five factors below (same definition as tracker.py) */
    double fill, size, unique, steady, seen;
} TrackResult;

typedef struct Tracker Tracker;

/* width and height of the full picture (both even). Returns NULL if memory is short. */
Tracker *tracker_new(int width, int height);
void     tracker_free(Tracker *t);

/* "teal" or "blue". Returns 0, or -1 for an unknown name. */
int  tracker_use_preset(Tracker *t, const char *name);
void tracker_clear(Tracker *t);
int  tracker_armed(const Tracker *t);

/* Find the target. Returns 1 if found (and fills *out), else 0 (out->found = 0, out->confidence = 0). */
int tracker_find_bgr(Tracker *t, const uint8_t *bgr, TrackResult *out);               /* 8-bit B,G,R, row-major */
int tracker_find_yuy2(Tracker *t, const uint8_t *yuy2, int stride, TrackResult *out); /* camera format: Y0 U Y1 V */

#ifdef __cplusplus
}
#endif
#endif

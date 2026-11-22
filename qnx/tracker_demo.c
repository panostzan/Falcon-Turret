/* Runs the C tracker over a file of raw 8-bit BGR frames and prints one line per frame, plus timing.
 *   tracker_demo <preset> <width> <height> <frames.bgr>
 * Used to check the C tracker against laptop/tracker.py on identical pictures, and to time it on the Pi. */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#include "tracker.h"

static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

int main(int argc, char **argv)
{
    if (argc != 5) { fprintf(stderr, "usage: %s preset width height frames.bgr\n", argv[0]); return 2; }
    int w = atoi(argv[2]), h = atoi(argv[3]);
    FILE *f = fopen(argv[4], "rb");
    if (!f) { perror("open frames"); return 1; }
    Tracker *t = tracker_new(w, h);
    if (!t || tracker_use_preset(t, argv[1]) != 0) { fprintf(stderr, "bad tracker or preset\n"); return 1; }
    size_t size = (size_t)w * h * 3;
    unsigned char *img = malloc(size);
    int n = 0;
    double total = 0.0;
    while (fread(img, 1, size, f) == size) {
        TrackResult r;
        double t0 = now_ms();
        tracker_find_bgr(t, img, &r);
        total += now_ms() - t0;
        printf("%d %d %.2f %.2f %.2f %.2f %.3f\n", n++, r.found, r.cx, r.cy, r.w, r.h, r.confidence);
    }
    fprintf(stderr, "%d frames, %.2f ms per frame\n", n, n ? total / n : 0.0);
    free(img);
    fclose(f);
    tracker_free(t);
    return 0;
}

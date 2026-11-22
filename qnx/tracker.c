/* See tracker.h. Port of laptop/tracker.py. */
#include "tracker.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define SCALE_DIV 2          /* work at half size (SCALE = 0.5 in tracker.py) */
#define MIN_AREA 40          /* mask pixels at half size */
#define MAX_FRAC 0.30
#define SIZE_LO 0.25
#define SIZE_HI 4.0
#define MIN_FILL 0.15
#define JOIN_R 7             /* join radius (JOIN = 15 in tracker.py) */
#define SMOOTH 0.8
#define LOST_AFTER 10
#define SURE_FRAMES 10.0

typedef struct { int area, x0, y0, x1, y1; } Group;

struct Tracker {
    int W, H, w, h;                 /* full size and working (half) size */
    int armed;
    int lo[3], hi[3];               /* HSV bounds, OpenCV convention (H 0..180) */
    double ref_area;                /* learned size, 0 = unknown */
    int have_last;
    double last_x, last_y;          /* previous (smoothed) centre, working pixels */
    int missed, streak;
    uint8_t *bgr, *blur, *mask, *tmp, *joined;   /* working buffers */
    uint16_t *prefix;               /* running sums for box filters */
    int32_t *labels;
    int32_t *parent;
    int32_t *remap;
    int max_labels;
    Group *groups;                  /* one per label, allocated once */
};

/* ---------------------------------------------------------------- helpers */

static int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }
static double clampd(double v, double lo, double hi) { return v < lo ? lo : (v > hi ? hi : v); }

/* ---------------------------------------------------------------- lifecycle */

Tracker *tracker_new(int width, int height)
{
    if (width < 8 || height < 8 || (width & 1) || (height & 1)) return NULL;
    Tracker *t = calloc(1, sizeof *t);
    if (!t) return NULL;
    t->W = width; t->H = height; t->w = width / SCALE_DIV; t->h = height / SCALE_DIV;
    size_t n = (size_t)t->w * t->h;
    t->max_labels = (int)(n / 4 + 2);
    t->bgr = malloc(n * 3); t->blur = malloc(n * 3);
    t->mask = malloc(n); t->tmp = malloc(n); t->joined = malloc(n);
    t->prefix = malloc((size_t)(t->w > t->h ? t->w : t->h) * 2 + 8);
    t->labels = malloc(n * sizeof(int32_t));
    t->parent = malloc((size_t)t->max_labels * sizeof(int32_t));
    t->remap = malloc((size_t)t->max_labels * sizeof(int32_t));
    t->groups = malloc((size_t)t->max_labels * sizeof(Group));
    if (!t->bgr || !t->blur || !t->mask || !t->tmp || !t->joined || !t->prefix || !t->labels || !t->parent || !t->remap || !t->groups) {
        tracker_free(t);
        return NULL;
    }
    return t;
}

void tracker_free(Tracker *t)
{
    if (!t) return;
    free(t->bgr); free(t->blur); free(t->mask); free(t->tmp); free(t->joined);
    free(t->prefix); free(t->labels); free(t->parent); free(t->remap); free(t->groups);
    free(t);
}

int tracker_use_preset(Tracker *t, const char *name)
{
    static const struct { const char *name; int lo[3], hi[3]; } P[] = {
        {"teal", {75, 70, 40},   {105, 255, 255}},
        {"blue", {108, 85, 85}, {132, 255, 255}},     /* same limits as laptop/tracker.py: see the note there */
    };
    for (size_t i = 0; i < sizeof P / sizeof P[0]; i++) {
        if (strcmp(P[i].name, name) == 0) {
            /* Choosing the target that is ALREADY chosen changes nothing. The laptop repeats "preset blue" about once a second to
             * keep the Pi in step; treating each repeat as a fresh start wiped the confidence build-up (it needs about 10
             * pictures) just as it got high. */
            if (t->armed && memcmp(t->lo, P[i].lo, sizeof t->lo) == 0 && memcmp(t->hi, P[i].hi, sizeof t->hi) == 0)
                return 0;
            memcpy(t->lo, P[i].lo, sizeof t->lo);
            memcpy(t->hi, P[i].hi, sizeof t->hi);
            t->armed = 1; t->ref_area = 0; t->have_last = 0; t->missed = 0; t->streak = 0;
            return 0;
        }
    }
    return -1;
}

void tracker_clear(Tracker *t) { t->armed = 0; t->ref_area = 0; t->have_last = 0; t->missed = 0; t->streak = 0; }
int  tracker_armed(const Tracker *t) { return t->armed; }

/* ---------------------------------------------------------------- picture preparation */

/* half-size BGR from full-size BGR: the average of each 2x2 block (what a half-size bilinear resize gives) */
static void half_from_bgr(Tracker *t, const uint8_t *src)
{
    for (int y = 0; y < t->h; y++)
        for (int x = 0; x < t->w; x++)
            for (int c = 0; c < 3; c++) {
                const uint8_t *p = src + ((size_t)(2 * y) * t->W + 2 * x) * 3 + c;
                int s = p[0] + p[3] + p[t->W * 3] + p[t->W * 3 + 3];
                t->bgr[((size_t)y * t->w + x) * 3 + c] = (uint8_t)((s + 2) >> 2);
            }
}

static uint8_t to_u8(double v) { return (uint8_t)(v < 0 ? 0 : (v > 255 ? 255 : v + 0.5)); }

/* half-size BGR from YUY2 (full-range YCbCr, the same conversion the JPEG the laptop sees uses) */
static void half_from_yuy2(Tracker *t, const uint8_t *src, int stride)
{
    for (int y = 0; y < t->h; y++) {
        const uint8_t *r0 = src + (size_t)(2 * y) * stride, *r1 = r0 + stride;
        for (int x = 0; x < t->w; x++) {
            const uint8_t *a = r0 + 4 * x, *b = r1 + 4 * x;            /* macropixel x covers picture pixels 2x and 2x+1 */
            double Y = (a[0] + a[2] + b[0] + b[2]) / 4.0;
            double U = ((a[1] + b[1]) / 2.0) - 128.0, V = ((a[3] + b[3]) / 2.0) - 128.0;
            uint8_t *o = t->bgr + ((size_t)y * t->w + x) * 3;
            o[0] = to_u8(Y + 1.772 * U);
            o[1] = to_u8(Y - 0.344136 * U - 0.714136 * V);
            o[2] = to_u8(Y + 1.402 * V);
        }
    }
}

/* 5x5 Gaussian blur ([1 4 6 4 1]/16, separable), edges replicated */
static void blur5(const uint8_t *src, uint8_t *dst, uint8_t *scratch, int w, int h)
{
    static const int K[5] = {1, 4, 6, 4, 1};
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++)
            for (int c = 0; c < 3; c++) {
                int s = 0;
                for (int k = -2; k <= 2; k++) s += K[k + 2] * src[((size_t)y * w + clampi(x + k, 0, w - 1)) * 3 + c];
                scratch[((size_t)y * w + x) * 3 + c] = (uint8_t)((s + 8) >> 4);
            }
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++)
            for (int c = 0; c < 3; c++) {
                int s = 0;
                for (int k = -2; k <= 2; k++) s += K[k + 2] * scratch[((size_t)clampi(y + k, 0, h - 1) * w + x) * 3 + c];
                dst[((size_t)y * w + x) * 3 + c] = (uint8_t)((s + 8) >> 4);
            }
}

/* OpenCV-style 8-bit HSV of one pixel: H 0..180, S and V 0..255 */
static void to_hsv(int b, int g, int r, int *H, int *S, int *V)
{
    int v = b > g ? (b > r ? b : r) : (g > r ? g : r);
    int mn = b < g ? (b < r ? b : r) : (g < r ? g : r);
    int d = v - mn;
    double h = 0;
    if (d) {
        if (v == r)      h = 60.0 * (g - b) / d;
        else if (v == g) h = 120.0 + 60.0 * (b - r) / d;
        else             h = 240.0 + 60.0 * (r - g) / d;
        if (h < 0) h += 360.0;
    }
    *H = (int)(h / 2.0 + 0.5); if (*H > 179) *H = (*H >= 180 ? 0 : *H);
    *S = v ? (int)(255.0 * d / v + 0.5) : 0;
    *V = v;
}

static int in_band(const Tracker *t, int h, int s, int v)
{
    if (s < t->lo[1] || s > t->hi[1] || v < t->lo[2] || v > t->hi[2]) return 0;
    if (t->lo[0] < 0)   /* hue wraps around red, low side */
        return (h >= 0 && h <= t->hi[0]) || (h >= 180 + t->lo[0] && h <= 180);
    if (t->hi[0] > 180) /* wraps, high side */
        return (h >= t->lo[0] && h <= 180) || (h >= 0 && h <= t->hi[0] - 180);
    return h >= t->lo[0] && h <= t->hi[0];
}

/* ---------------------------------------------------------------- binary morphology (square windows, O(pixels)) */

/* dst = 1 where any pixel of src within r (square) is nonzero. Outside the picture counts as 0. */
static void dilate_sq(const uint8_t *src, uint8_t *dst, uint8_t *scratch, uint16_t *prefix, int w, int h, int r)
{
    for (int y = 0; y < h; y++) {                       /* rows */
        prefix[0] = 0;
        for (int x = 0; x < w; x++) prefix[x + 1] = (uint16_t)(prefix[x] + (src[(size_t)y * w + x] != 0));
        for (int x = 0; x < w; x++) {
            int a = clampi(x - r, 0, w), b = clampi(x + r + 1, 0, w);
            scratch[(size_t)y * w + x] = prefix[b] - prefix[a] > 0;
        }
    }
    for (int x = 0; x < w; x++) {                       /* columns */
        prefix[0] = 0;
        for (int y = 0; y < h; y++) prefix[y + 1] = (uint16_t)(prefix[y] + scratch[(size_t)y * w + x]);
        for (int y = 0; y < h; y++) {
            int a = clampi(y - r, 0, h), b = clampi(y + r + 1, 0, h);
            dst[(size_t)y * w + x] = prefix[b] - prefix[a] > 0;
        }
    }
}

/* ---------------------------------------------------------------- connected components (8-connected, two-pass) */

static int32_t find_root(int32_t *parent, int32_t i)
{
    while (parent[i] != i) { parent[i] = parent[parent[i]]; i = parent[i]; }
    return i;
}

static void unite(int32_t *parent, int32_t a, int32_t b)
{
    a = find_root(parent, a); b = find_root(parent, b);
    if (a < b) parent[b] = a; else if (b < a) parent[a] = b;
}

/* labels `joined`; returns the number of compact labels (1..n) and leaves them in t->labels (0 = background) */
static int label_components(Tracker *t)
{
    int w = t->w, h = t->h, next = 1;
    t->parent[0] = 0;
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++) {
            size_t i = (size_t)y * w + x;
            if (!t->joined[i]) { t->labels[i] = 0; continue; }
            int32_t nb[4]; int n = 0;
            if (x > 0 && t->labels[i - 1]) nb[n++] = t->labels[i - 1];
            if (y > 0) {
                if (x > 0 && t->labels[i - w - 1]) nb[n++] = t->labels[i - w - 1];
                if (t->labels[i - w]) nb[n++] = t->labels[i - w];
                if (x < w - 1 && t->labels[i - w + 1]) nb[n++] = t->labels[i - w + 1];
            }
            if (n == 0) {
                if (next >= t->max_labels) { t->labels[i] = 0; continue; }   /* cannot happen for sane pictures */
                t->parent[next] = next; t->labels[i] = next++;
            } else {
                int32_t m = nb[0];
                for (int k = 1; k < n; k++) if (nb[k] < m) m = nb[k];
                t->labels[i] = m;
                for (int k = 0; k < n; k++) unite(t->parent, m, nb[k]);
            }
        }
    int count = 0;
    memset(t->remap, 0, (size_t)next * sizeof(int32_t));
    for (int32_t l = 1; l < next; l++) {
        int32_t r = find_root(t->parent, l);
        if (!t->remap[r]) t->remap[r] = ++count;
    }
    for (size_t i = 0; i < (size_t)w * h; i++)
        if (t->labels[i]) t->labels[i] = t->remap[find_root(t->parent, t->labels[i])];
    return count;
}

/* ---------------------------------------------------------------- the search */

static int find_common(Tracker *t, TrackResult *out)
{
    memset(out, 0, sizeof *out);
    if (!t->armed) return 0;
    int w = t->w, h = t->h;
    size_t n = (size_t)w * h;

    blur5(t->bgr, t->blur, (uint8_t *)t->labels, w, h);       /* the labels buffer doubles as scratch space here */
    for (size_t i = 0; i < n; i++) {
        int H, S, V;
        const uint8_t *p = t->blur + i * 3;
        to_hsv(p[0], p[1], p[2], &H, &S, &V);
        t->mask[i] = in_band(t, H, S, V);
    }
    /* open (erode then dilate, 3x3) to drop speckle: erode(m) = not dilate(not m) with outside treated as set */
    for (size_t i = 0; i < n; i++) t->tmp[i] = !t->mask[i];
    dilate_sq(t->tmp, t->joined, (uint8_t *)t->labels, t->prefix, w, h, 1);
    for (size_t i = 0; i < n; i++) t->tmp[i] = !t->joined[i];
    dilate_sq(t->tmp, t->mask, (uint8_t *)t->labels, t->prefix, w, h, 1);
    /* join nearby fragments */
    dilate_sq(t->mask, t->joined, (uint8_t *)t->labels, t->prefix, w, h, JOIN_R);

    int count = label_components(t);
    if (count == 0) goto none;

    Group *g = t->groups;
    for (int l = 1; l <= count; l++) { g[l].area = 0; g[l].x0 = w; g[l].y0 = h; g[l].x1 = -1; g[l].y1 = -1; }
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++) {
            size_t i = (size_t)y * w + x;
            if (!t->mask[i] || !t->labels[i]) continue;
            Group *q = &g[t->labels[i]];
            q->area++;
            if (x < q->x0) q->x0 = x;
            if (y < q->y0) q->y0 = y;
            if (x > q->x1) q->x1 = x;
            if (y > q->y1) q->y1 = y;
        }

    double max_area = MAX_FRAC * (double)n;
    int best = -1; double best_score = -1.0;
    int ok_count = 0, ok_idx[64];                       /* the candidates that pass the filters (capped: rivals matter little) */
    for (int l = 1; l <= count; l++) {
        Group *q = &g[l];
        if (q->area < MIN_AREA) continue;
        int bw = q->x1 - q->x0 + 1, bh = q->y1 - q->y0 + 1;
        if (q->area > max_area) continue;
        if (t->ref_area > 0 && (q->area < SIZE_LO * t->ref_area || q->area > SIZE_HI * t->ref_area)) continue;
        if ((double)q->area / (double)(bw * bh) < MIN_FILL) continue;
        double score = q->area;
        if (t->have_last) {
            double d = hypot(q->x0 + bw / 2.0 - t->last_x, q->y0 + bh / 2.0 - t->last_y);
            score = q->area / (1.0 + (d / 40.0) * (d / 40.0));
        }
        if (ok_count < 64) ok_idx[ok_count++] = l;
        if (score > best_score) { best_score = score; best = l; }
    }
    if (best < 0) goto none;

    Group *q = &g[best];
    int bw = q->x1 - q->x0 + 1, bh = q->y1 - q->y0 + 1;
    double cx = q->x0 + bw / 2.0, cy = q->y0 + bh / 2.0;

    /* confidence: the same five factors as LastSeen/_rate in tracker.py, computed before smoothing and learning */
    double fill = clampd(((double)q->area / (bw * bh) - MIN_FILL) / (0.6 - MIN_FILL), 0.0, 1.0);
    double size = t->ref_area > 0 ? clampd(1.0 - fabs(q->area / t->ref_area - 1.0) / 0.6, 0.0, 1.0) : 1.0;
    int rival = 0;
    for (int k = 0; k < ok_count; k++) if (ok_idx[k] != best && g[ok_idx[k]].area > rival) rival = g[ok_idx[k]].area;
    double unique = 1.0 - (double)rival / (q->area > 1 ? q->area : 1);
    double steady = 1.0;
    if (t->have_last) {
        double d = hypot(cx - t->last_x, cy - t->last_y);
        steady = 1.0 / (1.0 + (d / 60.0) * (d / 60.0));
    }
    t->streak++;
    double seen = t->streak / SURE_FRAMES; if (seen > 1.0) seen = 1.0;

    double sx = cx, sy = cy;                           /* light smoothing of the centre */
    if (t->have_last) { sx = SMOOTH * cx + (1 - SMOOTH) * t->last_x; sy = SMOOTH * cy + (1 - SMOOTH) * t->last_y; }
    t->last_x = sx; t->last_y = sy; t->have_last = 1; t->missed = 0;
    t->ref_area = t->ref_area > 0 ? 0.9 * t->ref_area + 0.1 * q->area : q->area;

    out->found = 1;
    out->cx = sx * SCALE_DIV; out->cy = sy * SCALE_DIV; out->w = bw * SCALE_DIV; out->h = bh * SCALE_DIV;
    out->fill = fill; out->size = size; out->unique = unique; out->steady = steady; out->seen = seen;
    out->confidence = seen * (0.3 * fill + 0.25 * size + 0.25 * unique + 0.2 * steady);
    return 1;

none:
    t->streak = 0;
    if (++t->missed > LOST_AFTER) t->have_last = 0;
    return 0;
}

int tracker_find_bgr(Tracker *t, const uint8_t *bgr, TrackResult *out)
{
    if (!t->armed) { memset(out, 0, sizeof *out); return 0; }
    half_from_bgr(t, bgr);
    return find_common(t, out);
}

int tracker_find_yuy2(Tracker *t, const uint8_t *yuy2, int stride, TrackResult *out)
{
    if (!t->armed) { memset(out, 0, sizeof *out); return 0; }
    half_from_yuy2(t, yuy2, stride);
    return find_common(t, out);
}

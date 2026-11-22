// facewatch: is there a human face in the camera picture? Runs on the Pi under QNX, using QNX's TensorFlow Lite port and the open-source
// MediaPipe BlazeFace (short range) model.
//
//   facewatch model.tflite                 reads raw YUY2 640x480 frames (614400 bytes each) from stdin, one per inference
//   facewatch model.tflite --once frame.yuy2    runs on one saved frame, prints every candidate, exits (for testing)
//
// For every frame it tells turretd (the tracker and fire interlock, UDP 127.0.0.1:5005):
//     human 0|1       1 while a face has been seen within the last HOLD_S seconds (a short hold stops flicker)
//     faceinfo <ms>   how long the model took
// and prints one line to stdout for the streamer to forward to the dashboard:
//     FACE <count> <ms> [x y w h score]...      boxes as fractions of the 640x480 picture (centre x, centre y, width, height)
//
// It runs at a low priority, in its own process: if it stalls or crashes, turretd stops hearing from it and the human gate (when on)
// refuses to fire. It can never delay the tracker.
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <vector>

#include "tensorflow/lite/interpreter.h"
#include "tensorflow/lite/kernels/register.h"
#include "tensorflow/lite/model.h"

static const int SRC_W = 640, SRC_H = 480;       // the camera picture, YUY2 (2 bytes per pixel)
static const int IN = 128;                       // the model's input is 128x128
static const int FIT_W = 128, FIT_H = 96;        // the picture shrunk 5x keeps its shape: 128x96, padded top and bottom to 128x128
static const int PAD_Y = (IN - FIT_H) / 2;
static const float SCORE_MIN = 0.5f;             // a candidate must be at least this sure
static const float NMS_IOU = 0.3f;               // candidates overlapping more than this are the same face
static const double HOLD_S = 1.0;                // keep reporting "human" this long after the last sighting
static const int N_ANCHORS = 896, N_COORDS = 16; // BlazeFace short range: 896 candidate boxes, 16 numbers each (4 box + 6 keypoints x 2)

struct Anchor { float x, y; };
struct Face { float x, y, w, h, score; };        // centre and size, as fractions of the camera picture

static double now_s()
{
    timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

// The model's candidate boxes sit on a fixed grid: 2 per cell on a 16x16 grid, then 6 per cell on an 8x8 grid = 896.
static std::vector<Anchor> make_anchors()
{
    std::vector<Anchor> a;
    const int strides[2] = {8, 16}, per[2] = {2, 6};
    for (int k = 0; k < 2; k++) {
        int fm = IN / strides[k];
        for (int y = 0; y < fm; y++)
            for (int x = 0; x < fm; x++)
                for (int r = 0; r < per[k]; r++) a.push_back({(x + 0.5f) / fm, (y + 0.5f) / fm});
    }
    return a;
}

// One camera pixel (YUY2: Y0 U Y1 V per pair of pixels) as RGB 0..255.
static inline void pixel_rgb(const uint8_t *p, int c, int r, float *rgb)
{
    const uint8_t *row = p + (size_t)r * SRC_W * 2;
    int pair = c & ~1;
    float y = row[c * 2], u = row[pair * 2 + 1] - 128.0f, v = row[pair * 2 + 3] - 128.0f;
    rgb[0] = y + 1.402f * v;
    rgb[1] = y - 0.344f * u - 0.714f * v;
    rgb[2] = y + 1.772f * u;
}

// Camera frame -> the model's input: 128x128x3 floats in [-1, 1], the picture shrunk 5x (each output pixel averages a 5x5 block) and
// padded with black above and below.
static void prepare(const uint8_t *yuy2, float *out)
{
    for (int i = 0; i < IN * IN * 3; i++) out[i] = -1.0f;
    for (int oy = 0; oy < FIT_H; oy++) {
        for (int ox = 0; ox < FIT_W; ox++) {
            float sum[3] = {0, 0, 0}, rgb[3];
            for (int dy = 0; dy < 5; dy++)
                for (int dx = 0; dx < 5; dx++) {
                    pixel_rgb(yuy2, ox * 5 + dx, oy * 5 + dy, rgb);
                    sum[0] += rgb[0]; sum[1] += rgb[1]; sum[2] += rgb[2];
                }
            float *o = out + ((size_t)(oy + PAD_Y) * IN + ox) * 3;
            for (int c = 0; c < 3; c++) o[c] = std::min(255.0f, std::max(0.0f, sum[c] / 25.0f)) / 127.5f - 1.0f;
        }
    }
}

static float iou(const Face &a, const Face &b)
{
    float ax0 = a.x - a.w / 2, ax1 = a.x + a.w / 2, ay0 = a.y - a.h / 2, ay1 = a.y + a.h / 2;
    float bx0 = b.x - b.w / 2, bx1 = b.x + b.w / 2, by0 = b.y - b.h / 2, by1 = b.y + b.h / 2;
    float iw = std::max(0.0f, std::min(ax1, bx1) - std::max(ax0, bx0)), ih = std::max(0.0f, std::min(ay1, by1) - std::max(ay0, by0));
    float inter = iw * ih, uni = a.w * a.h + b.w * b.h - inter;
    return uni > 0 ? inter / uni : 0.0f;
}

// The model's two outputs -> faces. regress: 896 x 16 numbers; logits: 896 scores before the sigmoid.
static std::vector<Face> decode(const float *regress, const float *logits, const std::vector<Anchor> &anchors, float score_min)
{
    std::vector<Face> found;
    for (int i = 0; i < N_ANCHORS; i++) {
        float l = std::min(100.0f, std::max(-100.0f, logits[i]));
        float score = 1.0f / (1.0f + std::exp(-l));
        if (score < score_min) continue;
        const float *r = regress + (size_t)i * N_COORDS;
        float cx = anchors[i].x + r[0] / IN, cy = anchors[i].y + r[1] / IN, w = r[2] / IN, h = r[3] / IN;
        // back from the padded square to the real picture (the padding is above and below)
        Face f;
        f.x = cx; f.w = w;
        f.y = (cy * IN - PAD_Y) / FIT_H; f.h = h * IN / FIT_H;
        f.score = score;
        found.push_back(f);
    }
    std::sort(found.begin(), found.end(), [](const Face &a, const Face &b) { return a.score > b.score; });
    std::vector<Face> kept;
    for (const Face &f : found) {
        bool dup = false;
        for (const Face &k : kept)
            if (iou(f, k) > NMS_IOU) { dup = true; break; }
        if (!dup) kept.push_back(f);
    }
    return kept;
}

static bool read_full(int fd, uint8_t *buf, size_t n)
{
    size_t got = 0;
    while (got < n) {
        ssize_t r = read(fd, buf + got, n - got);
        if (r <= 0) return false;
        got += (size_t)r;
    }
    return true;
}

int main(int argc, char **argv)
{
    if (argc < 2) { std::fprintf(stderr, "usage: facewatch model.tflite [--once frame.yuy2]\n"); return 2; }
    const char *once = (argc >= 4 && std::strcmp(argv[2], "--once") == 0) ? argv[3] : nullptr;

    auto model = tflite::FlatBufferModel::BuildFromFile(argv[1]);
    if (!model) { std::fprintf(stderr, "cannot load %s\n", argv[1]); return 1; }
    tflite::ops::builtin::BuiltinOpResolver resolver;
    std::unique_ptr<tflite::Interpreter> interp;
    tflite::InterpreterBuilder(*model, resolver)(&interp);
    if (!interp) { std::fprintf(stderr, "cannot build the interpreter\n"); return 1; }
    interp->SetNumThreads(1);                       // one core: the tracker and the video need the others
    if (interp->AllocateTensors() != kTfLiteOk) { std::fprintf(stderr, "cannot allocate tensors\n"); return 1; }

    float *input = interp->typed_input_tensor<float>(0);
    if (!input) { std::fprintf(stderr, "the model input is not float32\n"); return 1; }
    // which output is which: 896 x 16 numbers are the boxes, 896 x 1 are the scores
    int reg_idx = -1, cls_idx = -1;
    for (int i : interp->outputs()) {
        const TfLiteTensor *t = interp->tensor(i);
        int last = t->dims->data[t->dims->size - 1];
        if (last == N_COORDS) reg_idx = i;
        else if (last == 1) cls_idx = i;
    }
    if (reg_idx < 0 || cls_idx < 0) { std::fprintf(stderr, "unexpected model outputs\n"); return 1; }
    const std::vector<Anchor> anchors = make_anchors();
    if ((int)anchors.size() != N_ANCHORS) { std::fprintf(stderr, "anchor count %zu\n", anchors.size()); return 1; }

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    sockaddr_in turretd{};
    turretd.sin_family = AF_INET;
    turretd.sin_port = htons(5005);
    inet_pton(AF_INET, "127.0.0.1", &turretd.sin_addr);
    auto tell = [&](const char *text) { sendto(sock, text, std::strlen(text), 0, (sockaddr *)&turretd, sizeof turretd); };

    std::vector<uint8_t> frame((size_t)SRC_W * SRC_H * 2);
    double last_face = -1e9;
    for (;;) {
        if (once) {
            FILE *f = std::fopen(once, "rb");
            if (!f || std::fread(frame.data(), 1, frame.size(), f) != frame.size()) { std::fprintf(stderr, "cannot read a full frame from %s\n", once); return 1; }
            std::fclose(f);
        } else if (!read_full(0, frame.data(), frame.size())) {
            break;                                    // the streamer went away
        }
        double t0 = now_s();
        prepare(frame.data(), input);
        if (interp->Invoke() != kTfLiteOk) { std::fprintf(stderr, "inference failed\n"); return 1; }
        const float *regress = interp->typed_tensor<float>(reg_idx), *logits = interp->typed_tensor<float>(cls_idx);
        std::vector<Face> faces = decode(regress, logits, anchors, once ? 0.3f : SCORE_MIN);
        double ms = (now_s() - t0) * 1000.0;

        if (once) {
            std::printf("inference %.1f ms, %zu candidate(s) at score >= 0.3 (a face needs >= %.2f):\n", ms, faces.size(), SCORE_MIN);
            for (const Face &f : faces) std::printf("  centre (%.2f, %.2f)  size %.2f x %.2f  score %.2f\n", f.x, f.y, f.w, f.h, f.score);
            return 0;
        }
        double now = now_s();
        int count = 0;
        for (const Face &f : faces) if (f.score >= SCORE_MIN) count++;
        if (count > 0) last_face = now;
        char msg[96];
        std::snprintf(msg, sizeof msg, "human %d", (now - last_face) <= HOLD_S ? 1 : 0);
        tell(msg);
        std::snprintf(msg, sizeof msg, "faceinfo %.1f", ms);
        tell(msg);
        std::printf("FACE %d %.1f", count, ms);
        for (const Face &f : faces) if (f.score >= SCORE_MIN) std::printf(" %.3f %.3f %.3f %.3f %.2f", f.x, f.y, f.w, f.h, f.score);
        std::printf("\n");
        std::fflush(stdout);
    }
    return 0;
}

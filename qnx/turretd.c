/* turretd: the turret's brain, running natively on QNX.
 *
 * Reads raw camera pictures (YUY2, the camera's own format) from stdin, finds the target with the C tracker, runs the
 * mission state machine and fire interlock, and writes one text line per picture on stdout. It never touches the
 * servos. The laptop turns its answers into aim commands (for now), and sends control lines to this program over UDP.
 *
 *   turretd <width> <height> [control_port]        control_port defaults to 5005 (UDP, localhost only)
 *
 * Output lines (stdout):
 *   T seq t_ms found cx cy w h confidence state armed shots link proc_us scan_pan
 *   E t_ms text                                     (an event: state change, ARMED, FIRE #n, WATCHDOG ...)
 * Control lines (UDP):
 *   preset teal|blue    clear    track 0|1    arm 0|1    min_conf 50..100    auto_scan 0|1
 *   aim <settled 0|1> <speed>       what the aiming loop says about the gimbal and the target's speed
 *   hb                              heartbeat from the laptop
 *
 * Safety: if heartbeats have been seen and then stop for WATCHDOG_S seconds, the turret is DISARMED here, on QNX,
 * whatever the laptop or the network is doing. It stays disarmed until the operator arms it again.
 */
#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "engage.h"
#include "tracker.h"

#define WATCHDOG_S 1.5

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

typedef struct {
    Tracker *tracker;
    Engage engage;
    int tracking, settled;
    double speed;
    int hb_seen, link_ok;
    double hb_last;
    int events_printed;
} Brain;

static void print_new_events(Brain *b)
{
    int kept = b->engage.n_events < ENGAGE_EVENTS ? b->engage.n_events : ENGAGE_EVENTS;
    int first = b->engage.n_events - kept;
    for (int i = 0; i < kept; i++) {
        if (first + i < b->events_printed) continue;
        double ts; const char *text;
        if (engage_event(&b->engage, i, &ts, &text)) printf("E %.0f %s\n", ts * 1000.0, text);
    }
    b->events_printed = b->engage.n_events;
}

static void handle_control(Brain *b, char *line)
{
    char a[32] = "";
    double speed = 0;
    int n;
    line[strcspn(line, "\r\n")] = 0;
    if (sscanf(line, "preset %31s", a) == 1) tracker_use_preset(b->tracker, a);
    else if (strcmp(line, "clear") == 0) tracker_clear(b->tracker);
    else if (sscanf(line, "track %d", &n) == 1) b->tracking = n != 0;
    else if (sscanf(line, "arm %d", &n) == 1) engage_arm(&b->engage, n, now_s());
    else if (sscanf(line, "auto_scan %d", &n) == 1) b->engage.auto_scan = n != 0;
    else if (sscanf(line, "autofire %d", &n) == 1) b->engage.auto_fire = n != 0;
    else if (strcmp(line, "fire") == 0) { engage_fire(&b->engage, now_s()); print_new_events(b); }
    else if (sscanf(line, "min_conf %d", &n) == 1) { if (n >= 50 && n <= 100) b->engage.cfg.min_confidence = n / 100.0; }
    else if (sscanf(line, "aim %d %lf", &n, &speed) == 2) { b->settled = n != 0; b->speed = speed; }
    else if (strcmp(line, "hb") == 0) {
        if (b->hb_seen && !b->link_ok) printf("E %.0f laptop link restored (still disarmed)\n", now_s() * 1000.0);
        b->hb_seen = 1; b->link_ok = 1; b->hb_last = now_s();
    }
}

int main(int argc, char **argv)
{
    if (argc < 3) { fprintf(stderr, "usage: %s width height [control_port]\n", argv[0]); return 2; }
    int w = atoi(argv[1]), h = atoi(argv[2]), port = argc > 3 ? atoi(argv[3]) : 5005;

    Brain b;
    memset(&b, 0, sizeof b);
    b.tracker = tracker_new(w, h);
    if (!b.tracker) { fprintf(stderr, "cannot allocate the tracker\n"); return 1; }
    engage_init(&b.engage, NULL);
    b.link_ok = 1;

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);      /* local programs only: nothing off this machine can send commands */
    addr.sin_port = htons((unsigned short)port);
    if (sock < 0 || bind(sock, (struct sockaddr *)&addr, sizeof addr) != 0) { perror("control socket"); return 1; }

    size_t size = (size_t)w * h * 2;
    unsigned char *img = malloc(size);
    if (!img) return 1;
    setvbuf(stdout, NULL, _IOLBF, 0);

    for (long seq = 0;; seq++) {
        size_t got = 0;
        while (got < size) {                              /* one whole picture from stdin */
            struct pollfd p[2] = {{0, POLLIN, 0}, {sock, POLLIN, 0}};
            if (poll(p, 2, 200) < 0) continue;
            if (p[1].revents & POLLIN) {                  /* control messages are handled as they arrive */
                char buf[160];
                ssize_t n;
                while ((n = recv(sock, buf, sizeof buf - 1, MSG_DONTWAIT)) > 0) { buf[n] = 0; handle_control(&b, buf); }
            }
            if (p[0].revents & (POLLIN | POLLHUP)) {
                ssize_t n = read(0, img + got, size - got);
                if (n <= 0) goto done;
                got += (size_t)n;
            }
            /* the watchdog runs even when no pictures arrive */
            double t = now_s();
            if (b.hb_seen && b.link_ok && t - b.hb_last > WATCHDOG_S) {
                b.link_ok = 0;
                if (b.engage.armed) engage_arm(&b.engage, 0, t);
                printf("E %.0f WATCHDOG: no heartbeat from the laptop, disarmed\n", t * 1000.0);
            }
        }

        double t0 = now_s();
        TrackResult r;
        tracker_find_yuy2(b.tracker, img, w * 2, &r);
        ScanGoal goal;
        engage_update(&b.engage, t0, b.tracking, r.found, b.settled, b.speed, r.found ? r.confidence : 0.0, &goal);
        double t1 = now_s();
        print_new_events(&b);
        printf("T %ld %.0f %d %.1f %.1f %.1f %.1f %.3f %s %d %d %d %.0f %.1f\n", seq, t1 * 1000.0, r.found, r.cx, r.cy, r.w, r.h,
               r.found ? r.confidence : 0.0, engage_state_name(b.engage.state), b.engage.armed, b.engage.shots, b.link_ok,
               (t1 - t0) * 1e6, goal.has_goal ? goal.pan : -1.0);
    }
done:
    free(img);
    tracker_free(b.tracker);
    return 0;
}

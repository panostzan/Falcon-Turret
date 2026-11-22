/* See engage.h. Port of laptop/engage.py. */
#include "engage.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

static const char *NAMES[] = {"IDLE", "SEARCH", "ACQUIRE", "TRACK", "LOCK", "COOLDOWN"};

const char *engage_state_name(EngageState s) { return NAMES[s]; }

void engage_defaults(EngageConfig *c)
{
    c->acquire_frames = 3; c->lock_frames = 8; c->max_shots = 30;
    c->dwell_s = 0.5; c->cooldown_s = 1.5; c->lost_s = 1.0; c->max_speed = 300.0; c->min_confidence = 0.8;
    c->scan_lo = 50.0; c->scan_hi = 130.0; c->scan_speed = 25.0; c->scan_tilt = 135.0;
}

static void note(Engage *e, double ts, const char *text)
{
    EngageEvent *ev = &e->events[e->n_events % ENGAGE_EVENTS];
    ev->ts = ts;
    snprintf(ev->text, sizeof ev->text, "%s", text);
    e->n_events++;
}

static void reset(Engage *e)
{
    e->seen = e->settled = 0;
    e->have_last_seen = e->have_lock_since = e->have_scan_t0 = 0;
    e->cooldown_until = 0.0;
}

void engage_init(Engage *e, const EngageConfig *cfg)
{
    memset(e, 0, sizeof *e);
    if (cfg) e->cfg = *cfg; else engage_defaults(&e->cfg);
    e->state = ENGAGE_IDLE;
    reset(e);
}

static void to_state(Engage *e, EngageState s, double ts, const char *why)
{
    char text[64];
    if (s == e->state) return;
    if (why) snprintf(text, sizeof text, "%s -> %s (%s)", NAMES[e->state], NAMES[s], why);
    else snprintf(text, sizeof text, "%s -> %s", NAMES[e->state], NAMES[s]);
    note(e, ts, text);
    e->state = s;
}

void engage_arm(Engage *e, int on, double ts)
{
    on = on != 0;
    if (on == e->armed) return;
    e->armed = on;
    note(e, ts, on ? "ARMED" : "DISARMED");
    if (on) e->shots = 0;
}

static void scan_goal(Engage *e, double ts, ScanGoal *g)
{
    g->has_goal = 0; g->pan = g->tilt = 0.0;
    if (!e->auto_scan || e->state != ENGAGE_SEARCH) { e->have_scan_t0 = 0; return; }
    if (!e->have_scan_t0) { e->scan_t0 = ts; e->have_scan_t0 = 1; }
    double span = e->cfg.scan_hi - e->cfg.scan_lo;
    double phase = fmod((ts - e->scan_t0) * e->cfg.scan_speed, 2.0 * span);
    g->has_goal = 1;
    g->pan = e->cfg.scan_lo + (phase <= span ? phase : 2.0 * span - phase);
    g->tilt = e->cfg.scan_tilt;
}

int engage_update(Engage *e, double ts, int tracking, int found, int settled, double speed, double confidence,
                  ScanGoal *goal)
{
    ScanGoal local;
    if (!goal) goal = &local;
    const EngageConfig *c = &e->cfg;

    if (!tracking) {
        to_state(e, ENGAGE_IDLE, ts, NULL);
        reset(e);
        goal->has_goal = 0; goal->pan = goal->tilt = 0.0;
        return 0;
    }
    if (e->state == ENGAGE_IDLE) to_state(e, ENGAGE_SEARCH, ts, NULL);
    if (found) {
        e->seen++;
        e->last_seen = ts; e->have_last_seen = 1;
    } else {
        e->seen = e->settled = 0;
        if (!e->have_last_seen || ts - e->last_seen > c->lost_s) {
            if (e->state != ENGAGE_SEARCH) to_state(e, ENGAGE_SEARCH, ts, "target lost");
            e->have_lock_since = 0;
        }
    }
    int fired = 0;

    if (e->state == ENGAGE_SEARCH) {
        if (found) to_state(e, ENGAGE_ACQUIRE, ts, NULL);
    } else if (e->state == ENGAGE_ACQUIRE) {
        if (e->seen >= c->acquire_frames) to_state(e, ENGAGE_TRACK, ts, NULL);
    } else if (e->state == ENGAGE_TRACK || e->state == ENGAGE_LOCK || e->state == ENGAGE_COOLDOWN) {
        int sure = confidence >= c->min_confidence;
        e->settled = (found && settled && sure) ? e->settled + 1 : 0;
        if (e->state == ENGAGE_TRACK) {
            if (e->settled >= c->lock_frames && speed <= c->max_speed) {
                e->lock_since = ts; e->have_lock_since = 1;
                to_state(e, ENGAGE_LOCK, ts, NULL);
            }
        } else if (e->state == ENGAGE_LOCK) {
            if (e->settled == 0 || speed > c->max_speed) {
                to_state(e, ENGAGE_TRACK, ts, "lock broken");
                e->have_lock_since = 0;
            } else if (e->armed && e->shots < c->max_shots && e->have_lock_since && ts - e->lock_since >= c->dwell_s) {
                e->shots++;
                fired = 1;
                {
                    char text[32];
                    snprintf(text, sizeof text, "FIRE #%d", e->shots);
                    note(e, ts, text);
                }
                e->cooldown_until = ts + c->cooldown_s;
                to_state(e, ENGAGE_COOLDOWN, ts, NULL);
            }
        } else if (ts >= e->cooldown_until) {            /* COOLDOWN over */
            int locked = e->settled >= c->lock_frames && speed <= c->max_speed;
            e->have_lock_since = locked;
            if (locked) e->lock_since = ts;
            to_state(e, locked ? ENGAGE_LOCK : ENGAGE_TRACK, ts, NULL);
        }
    }
    scan_goal(e, ts, goal);
    return fired;
}

int engage_event(const Engage *e, int i, double *ts, const char **text)
{
    int kept = e->n_events < ENGAGE_EVENTS ? e->n_events : ENGAGE_EVENTS;
    if (i < 0 || i >= kept) return 0;
    int first = e->n_events - kept;
    const EngageEvent *ev = &e->events[(first + i) % ENGAGE_EVENTS];
    *ts = ev->ts; *text = ev->text;
    return 1;
}

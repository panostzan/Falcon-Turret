/* Mission state machine and fire interlock in portable C, for running natively on QNX.
 * A port of laptop/engage.py: SEARCH -> ACQUIRE -> TRACK -> LOCK -> (fire) -> COOLDOWN, with safety interlocks.
 *
 * It decides only WHEN to fire. It never fires on its own authority: a shot needs ALL of these at once
 *   - the master ARM switch is on (it starts OFF, and arming resets the shot counter)
 *   - tracking is on and the target has been seen continuously
 *   - the aim has settled on the target for `lock_frames` frames, the target is slow enough, and the tracker's confidence
 *     stayed at or above `min_confidence` for all of them
 *   - the lock has been held for `dwell_s` seconds
 *   - the cooldown since the last shot has passed, and fewer than `max_shots` have been fired since arming
 * No dynamic allocation, no OS calls: time is passed in by the caller (seconds, any steady clock).
 */
#ifndef TURRET_ENGAGE_H
#define TURRET_ENGAGE_H

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { ENGAGE_IDLE = 0, ENGAGE_SEARCH, ENGAGE_ACQUIRE, ENGAGE_TRACK, ENGAGE_LOCK, ENGAGE_COOLDOWN } EngageState;

typedef struct {
    int    acquire_frames, lock_frames, max_shots;
    double dwell_s, cooldown_s, lost_s, max_speed, min_confidence;
    double scan_lo, scan_hi, scan_speed, scan_tilt;
} EngageConfig;

typedef struct {
    int    has_goal;                /* 1 while auto-scan wants the gimbal to sweep */
    double pan, tilt;
} ScanGoal;

#define ENGAGE_EVENTS 32
typedef struct { double ts; char text[64]; } EngageEvent;

typedef struct {
    EngageConfig cfg;
    EngageState state;
    int armed, auto_scan, auto_fire, shots;     /* auto_fire: shoot by itself on LOCK (default 1); 0 = only on a manual engage_fire() */
    int seen, settled, have_last_seen, have_lock_since, have_scan_t0;
    double last_seen, lock_since, cooldown_until, scan_t0;
    int n_events;                   /* total ever recorded; the newest ENGAGE_EVENTS are kept */
    EngageEvent events[ENGAGE_EVENTS];
} Engage;

void engage_defaults(EngageConfig *cfg);
void engage_init(Engage *e, const EngageConfig *cfg);               /* cfg may be NULL for the defaults */
void engage_arm(Engage *e, int on, double ts);                      /* the master switch; arming resets the shot counter */

/* A manual shot (the operator's click). Allowed only when ARMED and tracking is on, outside the cooldown and under max_shots;
 * it does not need a lock, the operator decides. Returns FIRE_OK or why it was refused (also logged as an event). */
typedef enum { FIRE_OK = 0, FIRE_NOT_ARMED, FIRE_NOT_TRACKING, FIRE_COOLDOWN, FIRE_LIMIT } FireResult;
FireResult engage_fire(Engage *e, double ts);

/* Call once per picture. Returns 1 on the picture where a shot is taken. *goal (may be NULL) is filled in. */
int engage_update(Engage *e, double ts, int tracking, int found, int settled, double speed, double confidence,
                  ScanGoal *goal);

const char *engage_state_name(EngageState s);
/* Event number i (0 = oldest kept). Returns 0 if out of range. */
int engage_event(const Engage *e, int i, double *ts, const char **text);

#ifdef __cplusplus
}
#endif
#endif

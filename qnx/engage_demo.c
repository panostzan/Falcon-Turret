/* Feeds a script of inputs to the C state machine and prints the result of each step. Reads lines from stdin:
 *   ts tracking found settled speed confidence arm auto_scan      (arm and auto_scan: 1, 0, or -1 for "no change")
 * Prints: state fired shots scan_pan   (scan_pan is -1 when there is no scan goal). Used to check against engage.py. */
#include <stdio.h>

#include "engage.h"

int main(void)
{
    Engage e;
    engage_init(&e, NULL);
    double ts, speed, conf;
    int tracking, found, settled, arm, scan;
    while (scanf("%lf %d %d %d %lf %lf %d %d", &ts, &tracking, &found, &settled, &speed, &conf, &arm, &scan) == 8) {
        ScanGoal g;
        if (arm >= 0) engage_arm(&e, arm, ts);
        if (scan >= 0) e.auto_scan = scan;
        int fired = engage_update(&e, ts, tracking, found, settled, speed, conf, &g);
        printf("%s %d %d %.3f\n", engage_state_name(e.state), fired, e.shots, g.has_goal ? g.pan : -1.0);
    }
    return 0;
}

"""Tests the last-seen memory: perception status, ghost data, and the position estimate. Run: python tests/test_lastseen.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
from tracker import LastSeen   # noqa: E402

WH = (640, 480)


def test():
    ls = LastSeen()
    assert ls.lost(0, WH) is None                              # never seen: nothing to report

    for i in range(10):                                        # moving right at 200 px/s
        ls.seen(i * 0.066, 100 + 13.2 * i, 240, 50, 50, 0.9)
    t_last = 9 * 0.066
    L = ls.lost(t_last + 0.5, WH)
    assert L["status"] == "OCCLUDED" and L["ago"] == 0.5
    assert L["pred"] is not None and L["pred"][0] > L["last"][0] + 40, L      # estimate is ahead of the last confirmed spot
    assert L["last"][:2] == [219, 240] and L["heading"] == "right" and "middle" not in L["where"], L
    assert 0.6 < L["pos_conf"] < 0.9, L

    L = ls.lost(t_last + 3.0, WH)
    assert L["status"] == "REACQUIRING" and L["pred"] is None, L             # no estimate once it is too old to trust
    assert 0 < L["pos_conf"] < 0.4, L
    L = ls.lost(t_last + 6.0, WH)
    assert L["status"] == "LOST" and L["pos_conf"] == 0.0, L

    ls.seen(10.0, 300, 300, 50, 50, 0.9)                      # seen again: motion estimate restarts, no stale velocity
    assert ls.v == (0.0, 0.0)
    ls.reset()
    assert ls.lost(11, WH) is None

    still = LastSeen()                                         # a target that was not moving gets no estimate, only the ghost
    for i in range(6):
        still.seen(i * 0.066, 320, 240, 50, 50, 0.9)
    L = still.lost(2.0, WH)
    assert L["pred"] is None and L["where"] == "centre", L

    edge = LastSeen()                                          # the estimate never leaves the picture
    for i in range(6):
        edge.seen(i * 0.066, 600 + 20 * i, 240, 50, 50, 0.9)
    assert edge.lost(0.8, WH)["pred"][0] <= 640
    print("last-seen tests passed")


if __name__ == "__main__":
    test()

"""The 'now listening' ding must sound ONCE per turn, never every cycle.

This is the ting-ting-ting regression. The daemon's capture loop re-records
continuously, so a cue played per record cycle spams. These tests replay the
loop's cue decisions over scripted iterations and assert the exact ding count.

Run: python3 tests/test_cue_behavior.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import talkd  # noqa: E402


def simulate(events, mode="all", in_follow=False):
    """Replay run_daemon's cue logic. Each event is one loop iteration:
      "idle"  -> a listen cycle with no speech (the common re-record)
      "reply" -> section 1 spoke a reply, then `continue` (no listen this iter)
    Mirrors the real code: cue_armed=True at boot; a reply sets cue_armed=True
    and continues; a listen iteration dings iff should_ding, then disarms.
    Returns the number of dings."""
    armed = True
    dings = 0
    for ev in events:
        if ev == "reply":
            armed = True        # spoke -> re-arm; loop continues, no listen
            continue
        if talkd.should_ding(mode, in_follow, armed):
            dings += 1
            armed = False       # disarm after the single ding
    return dings


def test_single_ding_at_start_then_silent_forever_when_idle():
    # 100 idle re-records must produce exactly ONE ding (the ting-spam bug).
    assert simulate(["idle"] * 100) == 1


def test_one_ding_after_each_reply():
    # start ding + one after each of two replies = 3 total.
    seq = ["idle", "idle", "reply", "idle", "idle", "reply", "idle", "idle"]
    assert simulate(seq) == 3


def test_reply_with_no_following_listen_does_not_ding_twice():
    # Back-to-back replies (queued) then idle: still one ding when we resume.
    assert simulate(["idle", "reply", "reply", "idle", "idle"]) == 2


def test_wake_mode_ambient_never_dings():
    assert simulate(["idle"] * 50, mode="wake") == 0


def test_wake_follow_window_dings_once():
    # After "hey Claude", in_follow is True -> one ding, then silent.
    assert simulate(["idle"] * 10, mode="wake", in_follow=True) == 1


def test_should_ding_matrix():
    assert talkd.should_ding("all", False, True) is True
    assert talkd.should_ding("all", False, False) is False   # disarmed -> silent
    assert talkd.should_ding("wake", False, True) is False   # ambient -> silent
    assert talkd.should_ding("wake", True, True) is True     # post-wake follow
    assert talkd.should_ding("speak", False, True) is False  # speak-only, mic off


if __name__ == "__main__":
    test_single_ding_at_start_then_silent_forever_when_idle()
    test_one_ding_after_each_reply()
    test_reply_with_no_following_listen_does_not_ding_twice()
    test_wake_mode_ambient_never_dings()
    test_wake_follow_window_dings_once()
    test_should_ding_matrix()
    print("ok  cue dings once per turn, never per cycle (all cases)")

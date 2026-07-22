"""The status line: ONE state + ONE rotating, behaviour-aware recommendation.

Run: python3 tests/test_statusline.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402


def test_line_is_state_plus_one_suggestion_only():
    out = core.render_statusline({"phase": "listening"}, n=0)
    assert out.startswith("vb ")
    # exactly one " · " separator: "vb <state> · <one tip>"
    assert out.count(" · ") == 1, out


def test_update_is_not_a_permanent_prefix():
    # With an update pending, most refreshes still show a normal tip; the
    # update is just one of the rotating suggestions, not always shown.
    outs = [core.render_statusline({"phase": "listening"}, n=i,
                                   update_cmd="update: run vb update")
            for i in range(6)]
    with_update = [o for o in outs if "update" in o]
    assert 0 < len(with_update) < len(outs), "update should rotate, not stick"


def test_recommendations_rotate_one_at_a_time():
    seen = {core.recommend("listening", n=i, speed_changed=True)
            for i in range(4)}
    assert len(seen) > 1, "should show different suggestions across refreshes"


def test_stray_audio_surfaces_wake_suggestion():
    # A burst of dropped captures in agent mode -> recommend wake mode.
    tip_seen = any(
        "wake word mode" in core.recommend("listening", n=i,
                                            stats={"drops": 5, "prompts": 0},
                                            speed_changed=True)
        for i in range(6))
    assert tip_seen, "high drops should surface the wake-mode recommendation"


def test_no_wake_suggestion_when_capture_is_clean():
    for i in range(6):
        assert "wake word mode" not in core.recommend(
            "listening", n=i, stats={"drops": 0, "prompts": 4},
            speed_changed=True)


def test_speed_tip_gone_once_user_used_speed():
    for i in range(6):
        assert "faster" not in core.recommend("listening", n=i,
                                               speed_changed=True)
    assert any("faster" in core.recommend("listening", n=i, speed_changed=False)
               for i in range(6))


def test_speaking_always_shows_cut_in():
    for i in range(4):
        assert "cut in" in core.recommend("speaking", n=i)


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print("ok  status line: single behaviour-aware recommendation, rotating")

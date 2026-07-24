"""The status line SEGMENT: one state, appended to a line we do not own.

Run: python3 tests/test_statusline.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402


def test_segment_is_state_only():
    out = core.render_statusline({"phase": "listening"})
    assert out == "vb 🎙 voice on", out


def test_silent_when_voice_is_off():
    # The common case. Someone who installed us months ago and isn't talking
    # right now must see their own status line, unchanged, with nothing of
    # ours in it.
    assert core.render_statusline({"phase": "off"}) == ""
    assert core.render_statusline({}) == ""
    assert core.render_statusline({"phase": "nonsense"}) == ""


def test_no_tips_in_any_state():
    # We used to rotate suggestions here. On a shared line they are noise:
    # you read them once, then they compete with git/model/cost forever.
    for phase in core.HUD_PHASES:
        out = core.render_statusline({"phase": phase})
        for noise in ("faster", "slower", "F9", "wake word mode",
                      "stop listening", "cut in"):
            assert noise not in out, f"{phase} leaked a tip: {out}"


def test_every_live_phase_has_a_label():
    for phase in core.HUD_PHASES:
        out = core.render_statusline({"phase": phase})
        assert out.startswith("vb "), phase
        assert len(out) > 4, phase


def test_update_alert_is_the_one_exception():
    out = core.render_statusline({"phase": "listening"},
                                 update_cmd="update: run  vb update")
    assert "update: run  vb update" in out
    assert out.startswith("vb 🎙 voice on"), out
    # ...but never when voice is off: an idle install stays silent.
    assert core.render_statusline({"phase": "off"},
                                  update_cmd="update: run  vb update") == ""


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print("ok  status line: state-only segment, silent when off")

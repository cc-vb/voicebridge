"""Control-side target adapter: the seam for delivering to an agent.

voicebridge used to call inject.paste_text directly at every send site, hard-
wiring "the target is Claude Code in a terminal." These lock in that the send
now goes through an adapter (so Codex / Antigravity slot in later) and that the
Claude Code adapter still delivers exactly as before.

Run: python3 tests/test_adapters_control.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import adapters, inject, talkd  # noqa: E402


def test_for_sid_returns_claude_adapter_today():
    a = adapters.for_sid("any-sid")
    assert isinstance(a, adapters.ClaudeCodeAdapter)
    assert a.kind == "claude-code"
    # unknown kinds fall back to the default rather than crash
    assert adapters.for_sid("") is not None


def test_claude_adapter_send_delivers_like_before():
    calls = {}
    orig_paste, orig_tty, orig_bound = (
        inject.paste_text, talkd.tty_for_sid, talkd.bound_app)
    try:
        talkd.bound_app = lambda: "Terminal"
        talkd.tty_for_sid = lambda sid: "/dev/ttys009" if sid == "S1" else ""

        def fake_paste(text, send=False, expect_app="", target_tty=""):
            calls.update(text=text, send=send, expect_app=expect_app,
                         target_tty=target_tty)
            return True

        inject.paste_text = fake_paste
        ok = adapters.ClaudeCodeAdapter().send("S1", "run the tests")
        assert ok is True
        # same delivery contract as the old direct call: submit, bound app,
        # and the SELECTED session's tty (not the frontmost tab)
        assert calls == {"text": "run the tests", "send": True,
                         "expect_app": "Terminal", "target_tty": "/dev/ttys009"}
    finally:
        inject.paste_text, talkd.tty_for_sid, talkd.bound_app = (
            orig_paste, orig_tty, orig_bound)


def test_claude_adapter_send_reports_failure():
    orig_paste, orig_tty, orig_bound = (
        inject.paste_text, talkd.tty_for_sid, talkd.bound_app)
    try:
        talkd.bound_app = lambda: "Terminal"
        talkd.tty_for_sid = lambda sid: ""
        inject.paste_text = lambda *a, **k: False   # couldn't deliver
        assert adapters.ClaudeCodeAdapter().send("S1", "hi") is False
    finally:
        inject.paste_text, talkd.tty_for_sid, talkd.bound_app = (
            orig_paste, orig_tty, orig_bound)


def test_claude_adapter_cycle_mode_press_count():
    presses = {"n": 0}
    orig_stab, orig_bound = inject.press_shift_tab, talkd.bound_app
    try:
        talkd.bound_app = lambda: "Terminal"
        inject.press_shift_tab = lambda expect_app="": (
            presses.__setitem__("n", presses["n"] + 1) or True)
        a = adapters.ClaudeCodeAdapter()
        # n == 0 is a no-op (target mode already current), must NOT press
        assert a.cycle_mode("S1", 0) is True
        assert presses["n"] == 0
        # n steps -> n presses
        assert a.cycle_mode("S1", 2) is True
        assert presses["n"] == 2
    finally:
        inject.press_shift_tab, talkd.bound_app = orig_stab, orig_bound


if __name__ == "__main__":
    test_for_sid_returns_claude_adapter_today()
    test_claude_adapter_send_delivers_like_before()
    test_claude_adapter_send_reports_failure()
    test_claude_adapter_cycle_mode_press_count()
    print("ok  control adapter: for_sid + send contract + cycle_mode count")

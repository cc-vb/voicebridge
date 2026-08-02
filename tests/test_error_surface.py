"""Error surfacing: failures must become a phone toast + ledger (+ Mac cue),
never a silent degrade. Locks the mailbox round-trip, dedupe, and that a caught
exception with a known remedy surfaces an actionable message.

Run: python3 tests/test_error_surface.py   (no pytest needed)
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core, recover  # noqa: E402


def _tmp():
    return Path(tempfile.mkdtemp())


def test_mailbox_roundtrip_and_cursor():
    core.ERROR_MAILBOX = _tmp() / "err.jsonl"
    t0 = time.time()
    core.push_error("voice", "one")
    core.push_error("relay", "two")
    msgs = [g["msg"] for g in core.errors_since(t0 - 1)]
    assert "one" in msgs and "two" in msgs
    assert core.errors_since(time.time() + 1) == []      # cursor excludes old


def test_surface_dedupes_toasts_and_speaks_a_cue():
    d = _tmp()
    core.ERROR_MAILBOX = d / "err.jsonl"
    recover.ERR_LOG = d / "errors.jsonl"
    core._last_surfaced.clear()
    spoke = []
    orig = (core.speak, core.call_live, core._voice_active)
    try:
        core.call_live = lambda *a, **k: False
        core._voice_active = lambda: True
        core.speak = lambda *a, **k: spoke.append(a[0] if a else "")
        core.surface_error("voice", "the voice engine failed", speak=True)
        core.surface_error("voice", "the voice engine failed", speak=True)  # dup <30s
        lines = (d / "err.jsonl").read_text().splitlines()
        assert len(lines) == 1                            # deduped -> one toast
        assert spoke and "Heads up" in spoke[0]           # spoken cue once
        ledger = (d / "errors.jsonl").read_text()
        assert "voice engine failed" in ledger            # also in `vb errors`
    finally:
        core.speak, core.call_live, core._voice_active = orig


def test_recorded_exception_surfaces_actionable_remedy():
    d = _tmp()
    core.ERROR_MAILBOX = d / "err.jsonl"
    recover.ERR_LOG = d / "errors.jsonl"
    core._last_surfaced.clear()
    recover._last_seen.clear()
    orig = (core.call_live, core._voice_active,
            recover.selfheal.heal, recover.telemetry.send)
    try:
        core.call_live = lambda *a, **k: False
        core._voice_active = lambda: False                # don't attempt to speak
        recover.selfheal.heal = lambda **k: None          # no real heal in a test
        recover.telemetry.send = lambda *a, **k: None
        recover.record("voice", RuntimeError("kokoro server connection refused"))
        box = (d / "err.jsonl").read_text().lower()
        assert "voice server" in box                      # plain remedy, not a stack trace
    finally:
        (core.call_live, core._voice_active,
         recover.selfheal.heal, recover.telemetry.send) = orig


if __name__ == "__main__":
    test_mailbox_roundtrip_and_cursor()
    test_surface_dedupes_toasts_and_speaks_a_cue()
    test_recorded_exception_surfaces_actionable_remedy()
    print("ok  error surface: mailbox round-trip, dedupe + cue, remedy surfaced")

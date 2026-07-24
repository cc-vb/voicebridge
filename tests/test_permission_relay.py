"""Phone permission relay: spoken yes/no answers a blocking decision.

Run: python3 tests/test_permission_relay.py   (no pytest needed)
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core, call, inject  # noqa: E402


def _fresh(tmp):
    core.STATE_DIR = tmp
    core.PENDING_NOTICE = tmp / "pending_notice"


def test_notice_roundtrip_ttl_and_sid_scoping():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    core.set_pending_notice("S1", "Claude needs permission to run npm install")
    assert "npm install" in core.get_pending_notice("S1")
    assert core.get_pending_notice("OTHER") == ""     # scoped to its session
    assert "npm install" in core.get_pending_notice("")  # unscoped read ok
    # stale notices expire
    import json
    core.PENDING_NOTICE.write_text(json.dumps(
        {"sid": "S1", "message": "old", "ts": time.time() - 999}))
    assert core.get_pending_notice("S1") == ""
    core.set_pending_notice("S1", "x")
    core.clear_pending_notice()
    assert core.get_pending_notice("S1") == ""


def test_yes_no_matching():
    for t in ("yes", "Yeah!", "ok", "okay", "go ahead", "approve", "do it"):
        assert call.YES_RE.match(t), t
    for t in ("no", "Nope.", "deny", "don't", "cancel", "stop"):
        assert call.NO_RE.match(t), t
    for t in ("yes and also fix the tests", "not sure", "why?"):
        assert not call.YES_RE.match(t) and not call.NO_RE.match(t), t


def test_handle_pending_paths():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    keys = []
    orig_enter, orig_esc = inject.press_enter, inject.press_escape
    inject.press_enter = lambda: keys.append("enter")
    inject.press_escape = lambda: keys.append("esc")
    try:
        core.set_pending_notice("S1", "Permission to edit main.py?")
        # yes -> Enter, notice cleared, empty return (falls through to wait)
        assert call._handle_pending("yes", "Permission to edit main.py?") == ""
        assert keys == ["enter"] and core.get_pending_notice("") == ""
        # no -> Escape + a spoken acknowledgement
        core.set_pending_notice("S1", "Permission to edit main.py?")
        out = call._handle_pending("no", "Permission to edit main.py?")
        assert keys == ["enter", "esc"] and "declined" in out
        # anything else -> told what Claude is asking, no keystroke
        core.set_pending_notice("S1", "Permission to edit main.py?")
        out = call._handle_pending("what files changed",
                                   "Permission to edit main.py?")
        assert "waiting on you" in out and "main.py" in out
        assert keys == ["enter", "esc"]   # unchanged
    finally:
        inject.press_enter, inject.press_escape = orig_enter, orig_esc


if __name__ == "__main__":
    test_notice_roundtrip_ttl_and_sid_scoping()
    test_yes_no_matching()
    test_handle_pending_paths()
    print("ok  permission relay: notice ttl/scope, yes/no, keystroke paths")

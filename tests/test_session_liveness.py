"""Active vs inactive sessions: only live Claude processes are talkable.

Run: python3 tests/test_session_liveness.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import sessions  # noqa: E402


def _row(sid, proj, mtime):
    return {"sid": sid, "label": sid, "mtime": mtime,
            "path": f"/x/.claude/projects/{proj}/{sid}.jsonl"}


def test_live_cwd_marks_newest_in_dir_active():
    rows = [_row("new", "-Users-k-app", 200), _row("old", "-Users-k-app", 100),
            _row("other", "-Users-k-web", 150)]
    live = {"/Users/k/app": 1}   # ONE live claude in ~/k/app
    out = {r["sid"]: r["active"] for r in sessions.mark_active(rows, live)}
    assert out == {"new": True, "old": False, "other": False}


def test_two_processes_in_one_dir_mark_two_sessions():
    rows = [_row("a", "-Users-k-app", 300), _row("b", "-Users-k-app", 200),
            _row("c", "-Users-k-app", 100)]
    out = {r["sid"]: r["active"]
           for r in sessions.mark_active(rows, {"/Users/k/app": 2})}
    assert out == {"a": True, "b": True, "c": False}


def test_no_processes_means_nothing_active():
    rows = [_row("a", "-Users-k-app", 300)]
    assert sessions.mark_active(rows, {})[0]["active"] is False


def test_switch_refuses_closed_sessions(monkeypatch):
    monkeypatch.setattr(sessions, "roster", lambda: [
        {"sid": "S1", "label": "jobhunt", "path": "/x", "active": False}])
    msg = sessions.switch_sid("S1")
    assert "closed" in msg and "Voice moved" not in msg
    monkeypatch.setattr(sessions, "find", lambda q: {
        "sid": "S1", "label": "jobhunt", "path": "/x", "active": False})
    msg = sessions.switch("jobhunt")
    assert "closed" in msg and "Voice moved" not in msg


if __name__ == "__main__":
    test_live_cwd_marks_newest_in_dir_active()
    test_two_processes_in_one_dir_mark_two_sessions()
    test_no_processes_means_nothing_active()
    print("ok  liveness: newest-per-live-proc active, switch refuses closed "
          "(run under pytest for the monkeypatch case)")

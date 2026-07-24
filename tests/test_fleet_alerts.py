"""Fleet idle-alerts must never announce the session you're voiced in.

The bug: "Heads up: <this session> is ready" fired after every reply, because
newly_idle() reported the active session's own working->idle transition.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import sessions  # noqa: E402


def _roster(rows):
    return lambda: rows


def test_active_session_is_never_announced(monkeypatch):
    rows = [
        {"sid": "me", "label": "this-one", "state": "idle", "path": "/x"},
        {"sid": "other", "label": "jobhunt", "state": "idle", "path": "/y"},
    ]
    monkeypatch.setattr(sessions, "roster", _roster(rows))
    prev = {"me": "working", "other": "working"}   # both just finished
    fresh, now = sessions.newly_idle(prev, exclude_sid="me")
    assert "this-one" not in fresh          # my own session: silent
    assert "jobhunt" in fresh               # the other one: announced
    assert now == {"me": "idle", "other": "idle"}


def test_no_alert_when_state_unchanged(monkeypatch):
    rows = [{"sid": "other", "label": "jobhunt", "state": "idle", "path": "/y"}]
    monkeypatch.setattr(sessions, "roster", _roster(rows))
    # already idle last scan -> not a fresh transition -> no repeat
    fresh, _ = sessions.newly_idle({"other": "idle"}, exclude_sid="me")
    assert fresh == []

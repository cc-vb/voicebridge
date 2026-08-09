"""Fleet sensing: the event diff must be exact.

These lock the pure detect_events() logic, which is what the attention engine
will be tuned against. Every false event here becomes an unwanted interruption
in the product, so the bar is: no event unless something really changed.

Run: python3 tests/test_fleet_sensing.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import fleet  # noqa: E402


def _row(sid="a", label="jobhunt", status="idle", mtime=1000.0, **kw):
    r = {"sid": sid, "label": label, "status": status, "mtime": mtime,
         "question": "", "permission": "", "source": "agents_json", "path": "/x"}
    r.update(kw)
    return r


def _kinds(evs):
    return [e["kind"] for e in evs]


def test_first_sight_of_a_session_is_not_an_event():
    # every agent already running when we start must not fire anything
    cur = {"a": _row(status="idle"), "b": _row(sid="b", status="working")}
    assert fleet.detect_events({}, cur, now=2000.0) == []


def test_working_to_idle_is_a_finish():
    prev = {"a": _row(status="working")}
    cur = {"a": _row(status="idle")}
    evs = fleet.detect_events(prev, cur, now=2000.0)
    assert _kinds(evs) == [fleet.FINISHED]
    assert evs[0]["label"] == "jobhunt"


def test_no_event_when_nothing_changed():
    # now is only 200s past the last transcript write, well under the stuck
    # threshold, so a busy agent that is simply working must stay silent.
    prev = {"a": _row(status="working")}
    cur = {"a": _row(status="working")}
    assert fleet.detect_events(prev, cur, now=1200.0) == []
    prev = {"a": _row(status="idle")}
    cur = {"a": _row(status="idle")}
    assert fleet.detect_events(prev, cur, now=1200.0) == []


def test_new_question_fires_once_not_every_tick():
    prev = {"a": _row(status="working")}
    cur = {"a": _row(status="working", question="Which token store?")}
    evs = fleet.detect_events(prev, cur, now=2000.0)
    assert _kinds(evs) == [fleet.BLOCKED_QUESTION]
    assert "token store" in evs[0]["detail"]
    # still open on the next tick -> silent
    assert fleet.detect_events(cur, dict(cur), now=2001.0) == []


def test_permission_fires_once():
    prev = {"a": _row()}
    cur = {"a": _row(permission="Allow Bash(rm)?")}
    assert _kinds(fleet.detect_events(prev, cur, now=2000.0)) == [fleet.PERMISSION]
    assert fleet.detect_events(cur, dict(cur), now=2001.0) == []


def test_stuck_only_when_busy_and_quiet_and_only_once():
    # busy, transcript untouched for 20 minutes
    prev = {"a": _row(status="working", mtime=1000.0)}
    cur = {"a": _row(status="working", mtime=1000.0)}
    evs = fleet.detect_events(prev, cur, now=1000.0 + 1200, stuck_after=600)
    assert _kinds(evs) == [fleet.STUCK]
    # next tick, still stuck: silent (no nagging)
    evs2 = fleet.detect_events(cur, dict(cur), now=1000.0 + 1260, stuck_after=600)
    assert evs2 == []


def test_progress_resets_stuck():
    prev = {"a": _row(status="working", mtime=1000.0)}
    cur = {"a": _row(status="working", mtime=1000.0)}
    fleet.detect_events(prev, cur, now=1000.0 + 1200, stuck_after=600)   # fires
    moved = _row(status="working", mtime=2500.0)                          # grew
    cur2 = {"a": moved}
    assert fleet.detect_events(cur, cur2, now=2500.0, stuck_after=600) == []
    # and it can go stuck again later, since progress reset it
    evs = fleet.detect_events(cur2, {"a": _row(status="working", mtime=2500.0)},
                              now=2500.0 + 1200, stuck_after=600)
    assert _kinds(evs) == [fleet.STUCK]


def test_idle_agent_is_never_stuck():
    prev = {"a": _row(status="idle", mtime=1000.0)}
    cur = {"a": _row(status="idle", mtime=1000.0)}
    assert fleet.detect_events(prev, cur, now=1000.0 + 9999, stuck_after=600) == []


def test_multiple_agents_report_independently():
    prev = {"a": _row(sid="a", label="one", status="working"),
            "b": _row(sid="b", label="two", status="working")}
    cur = {"a": _row(sid="a", label="one", status="idle"),
           "b": _row(sid="b", label="two", status="working",
                     question="Merge now?")}
    evs = fleet.detect_events(prev, cur, now=2000.0)
    got = {(e["kind"], e["label"]) for e in evs}
    assert got == {(fleet.FINISHED, "one"), (fleet.BLOCKED_QUESTION, "two")}


def test_status_vocabulary_is_normalized():
    assert fleet._norm_status("busy") == "working"
    assert fleet._norm_status("running") == "working"
    assert fleet._norm_status("idle") == "idle"
    assert fleet._norm_status("") == "idle"
    assert fleet._norm_status(None) == "idle"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  fleet sensing: diff is exact, no phantom events, stuck fires once")

"""The supervisor loop: sensing plus judgment, wired together.

Uses fake events and a fake context so a whole "day" runs in milliseconds and
no audio is produced. What matters here is the end-to-end behaviour a user
would feel: it batches, it respects the budget, it never repeats itself.

Run: python3 tests/test_supervisor.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import attention as at, fleet, signals, supervisor  # noqa: E402

NOW = 50_000.0


def _fake_world(monkey_events, ctx_kw=None):
    """Patch sensing + signals so tick() runs offline and deterministically."""
    calls = {"n": 0}

    def fake_poll(state):
        calls["n"] += 1
        evs = monkey_events[calls["n"] - 1] if calls["n"] <= len(monkey_events) else []
        return list(evs), {}

    def fake_gather(sids, scores=None, spoken_at=()):
        kw = dict(ctx_kw or {})
        kw.setdefault("now", NOW)
        return at.context(scores=scores, spoken_at=spoken_at, **kw)

    fleet.poll, signals.gather = fake_poll, fake_gather
    return calls


def _ev(kind, label="jobhunt", sid="a", ts=NOW - 60, detail=""):
    return {"kind": kind, "label": label, "sid": sid, "ts": ts, "detail": detail}


def setup_function(_=None):
    pass


def test_a_finished_agent_gets_announced_once_shortly_after():
    _fake_world([[_ev("finished")], [], [], []])
    st = supervisor.new_state()
    # held briefly, so anything else finishing at the same moment can join it
    assert supervisor.tick(st, now=NOW) == []
    said = supervisor.tick(st, now=NOW + at.BATCH_WINDOW + 1)
    assert said and "jobhunt finished" in said[0]
    # and then silence, not repetition
    assert supervisor.tick(st, now=NOW + 120) == []
    assert supervisor.tick(st, now=NOW + 300) == []


def test_two_agents_needing_you_become_one_sentence():
    _fake_world([[_ev("finished", label="migration", sid="a"),
                  _ev("error", label="api", sid="b", detail="build failed")]])
    st = supervisor.new_state()
    said = supervisor.tick(st, now=NOW)
    assert len(said) == 1                       # ONE utterance, not two
    assert "api" in said[0] and "migration" in said[0]
    assert said[0].startswith("Two things")


def test_it_says_nothing_about_the_agent_you_are_watching():
    _fake_world([[_ev("finished", sid="a")]], ctx_kw={"focused_sid": "a"})
    st = supervisor.new_state()
    assert supervisor.tick(st, now=NOW) == []


def test_it_stays_silent_while_you_are_talking_then_delivers():
    # you are speaking: it holds
    _fake_world([[_ev("error", detail="crashed")]], ctx_kw={"user_speaking": True})
    st = supervisor.new_state()
    assert supervisor.tick(st, now=NOW) == []
    assert st["pending"], "the news must be held, not dropped"
    # you stop talking: it delivers what it was holding
    _fake_world([[]], ctx_kw={})
    said = supervisor.tick(st, now=NOW + 30)
    assert said and "crashed" in said[0]


def test_minor_news_waits_and_rides_along_with_something_real():
    _fake_world([[_ev("stuck", ts=NOW - 60, detail="no progress")],
                 [_ev("error", label="api", detail="failed")]])
    st = supervisor.new_state()
    assert supervisor.tick(st, now=NOW) == []            # stuck alone: silent
    said = supervisor.tick(st, now=NOW + 5)              # now something real
    assert said and "smaller update" in said[0]          # mentioned, not listed


def test_the_hourly_budget_is_respected_across_ticks():
    evs = [[_ev("finished", label=f"a{i}", sid=f"s{i}")] for i in range(6)]
    _fake_world(evs)
    st = supervisor.new_state()
    spoken = 0
    for i in range(6):
        if supervisor.tick(st, now=NOW + i):
            spoken += 1
    assert spoken <= at.HOURLY_BUDGET, f"interrupted {spoken} times in an hour"


def test_dangerous_sounding_requests_are_treated_as_high_stakes():
    assert supervisor._stakes({"detail": "Allow rm -rf build?"}) == "high"
    assert supervisor._stakes({"detail": "drop table users"}) == "high"
    assert supervisor._stakes({"detail": "deploy to production"}) == "high"
    assert supervisor._stakes({"detail": "rename this variable"}) == "low"
    assert supervisor._stakes({"detail": ""}) == "low"


def test_the_session_you_are_voiced_in_is_never_announced_to_you():
    # its replies are already spoken to you there; announcing it repeats yourself
    _fake_world([[_ev("finished", sid="me", label="this-one")]])
    st = supervisor.new_state()
    st["_voiced_sid"] = "me"
    assert supervisor.tick(st, now=NOW) == []


def test_the_same_news_is_never_queued_twice():
    same = _ev("finished", sid="a", label="jobhunt")
    _fake_world([[same], [same], []])       # the same event arrives repeatedly
    st = supervisor.new_state()
    supervisor.tick(st, now=NOW)
    supervisor.tick(st, now=NOW + 1)
    said = supervisor.tick(st, now=NOW + at.BATCH_WINDOW + 2)
    assert said == ["jobhunt finished."]     # not "... And jobhunt finished."


if __name__ == "__main__":
    import types
    orig = (fleet.poll, signals.gather)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and isinstance(fn, types.FunctionType):
            fleet.poll, signals.gather = orig      # reset between tests
            fn()
    fleet.poll, signals.gather = orig
    print("ok  supervisor: batches, holds, respects focus + budget, no repeats")

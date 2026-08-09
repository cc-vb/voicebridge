"""The attention engine: when to speak, and (mostly) when not to.

Every test here is a real annoyance we are promising not to commit. If one of
these regresses, the product gets muted on day one, which is exactly what the
kill test measures.

Run: python3 tests/test_attention.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import attention as at  # noqa: E402

NOW = 10_000.0


def ev(kind="finished", sid="a", label="jobhunt", ts=None, detail=""):
    return {"kind": kind, "sid": sid, "label": label,
            "ts": NOW - 60 if ts is None else ts, "detail": detail}


def ctx(**kw):
    kw.setdefault("now", NOW)
    return at.context(**kw)


# ---- tiers ---------------------------------------------------------------
def test_errors_are_always_urgent():
    assert at.tier_for(ev("error"), ctx()) == at.P0


def test_permission_is_urgent_when_high_stakes_immediately():
    fresh = ev("permission", ts=NOW - 1)
    assert at.tier_for(fresh, ctx(), stakes="high") == at.P0
    assert at.tier_for(fresh, ctx(), stakes="low") == at.P1


def test_a_question_left_hanging_becomes_urgent():
    young = ev("blocked", ts=NOW - 5)
    old = ev("blocked", ts=NOW - 120)
    assert at.tier_for(young, ctx(), stakes="high") == at.P1
    assert at.tier_for(old, ctx(), stakes="high") == at.P0


def test_finishing_is_never_urgent():
    assert at.tier_for(ev("finished"), ctx()) == at.P1


# ---- the gates, in order -------------------------------------------------
def test_never_speaks_about_the_agent_you_are_watching():
    d = at.decide(ev("finished", sid="a"), ctx(focused_sid="a"))
    assert d["action"] == at.DISCARD and "looking at" in d["reason"]


def test_never_speaks_about_an_agent_you_just_typed_into():
    d = at.decide(ev("blocked", sid="a"), ctx(typed_at={"a": NOW - 5}))
    assert d["action"] == at.DISCARD
    # but a while later it is fair game again
    d2 = at.decide(ev("blocked", sid="a"), ctx(typed_at={"a": NOW - 3600}))
    assert d2["action"] != at.DISCARD


def test_fresh_events_wait_in_case_they_resolve_themselves():
    d = at.decide(ev("blocked", ts=NOW - 2), ctx())
    assert d["action"] == at.QUEUE and "resolve" in d["reason"]


def test_but_an_error_is_never_debounced():
    d = at.decide(ev("error", ts=NOW - 1), ctx())
    assert d["action"] == at.SPEAK_NOW


def test_never_talks_over_you():
    for busy in ({"user_speaking": True}, {"mic_busy": True}):
        d = at.decide(ev("error"), ctx(**busy))
        assert d["action"] == at.QUEUE, busy
    # even an urgent error waits for the channel, it does not shout over a call
    d = at.decide(ev("error"), ctx(mic_busy=True))
    assert d["tier"] == at.P0 and d["action"] == at.QUEUE


def test_do_not_disturb_is_respected_but_urgent_news_still_lands_eventually():
    fresh_urgent = ev("error", ts=NOW - 60)
    assert at.decide(fresh_urgent, ctx(dnd=True))["action"] == at.QUEUE
    waited = ev("error", ts=NOW - 300)          # past the P0 deadline
    assert at.decide(waited, ctx(dnd=True))["action"] == at.SPEAK_NOW
    # a non-urgent item never pierces DND
    assert at.decide(ev("finished", ts=NOW - 9999),
                     ctx(dnd=True))["action"] == at.QUEUE


def test_hourly_budget_stops_nagging_but_urgent_always_gets_through():
    spoken = [NOW - 100, NOW - 200, NOW - 300, NOW - 400]   # 4 this hour
    assert at.decide(ev("finished"), ctx(spoken_at=spoken))["action"] == at.DIGEST
    assert at.decide(ev("error"), ctx(spoken_at=spoken))["action"] == at.SPEAK_NOW
    # yesterday's interruptions do not count against today
    old = [NOW - 7200] * 9
    assert at.decide(ev("finished"), ctx(spoken_at=old))["action"] != at.DIGEST


def test_minor_news_never_speaks_alone():
    assert at.decide(ev("stuck", ts=NOW - 60), ctx())["action"] == at.DIGEST


def test_an_ignored_alert_type_demotes_itself():
    scores = {"finished": 0.2}
    d = at.decide(ev("finished"), ctx(scores=scores))
    assert d["action"] == at.DIGEST and "ignore" in d["reason"]
    # but a demoted type can still be urgent: errors always get through
    d2 = at.decide(ev("error"), ctx(scores={"error": 0.0}))
    assert d2["action"] == at.SPEAK_NOW


def test_every_decision_explains_itself():
    for kind in ("finished", "blocked", "permission", "error", "stuck"):
        d = at.decide(ev(kind), ctx())
        assert d["reason"], f"{kind} decided without a reason"


# ---- deferral ------------------------------------------------------------
def test_nothing_is_lost_it_becomes_due():
    pending = [{"tier": at.P1, "deadline": NOW - 1, "kind": "finished",
                "label": "one"}]
    assert at.due(pending, ctx()) == pending


def test_queued_items_stay_queued_while_you_are_talking():
    pending = [{"tier": at.P0, "deadline": NOW - 1, "kind": "error"}]
    assert at.due(pending, ctx(user_speaking=True)) == []


def test_urgent_goes_as_soon_as_the_channel_is_free():
    pending = [{"tier": at.P0, "deadline": NOW + 999, "kind": "error"}]
    assert at.due(pending, ctx()) == pending          # free channel
    assert at.due(pending, ctx(dnd=True)) == []       # but not through DND


# ---- batching ------------------------------------------------------------
def test_several_items_become_one_utterance_worst_first():
    items = [{"tier": at.P1, "kind": "finished", "label": "migration"},
             {"tier": at.P0, "kind": "error", "label": "api", "detail": "build failed"}]
    said = at.compose(items)
    assert said.count(".") >= 2
    assert said.index("api") < said.index("migration")   # worst first
    assert said.startswith("Two things")


def test_one_item_is_just_said_plainly():
    said = at.compose([{"tier": at.P1, "kind": "finished", "label": "jobhunt"}])
    assert said == "jobhunt finished."


def test_digest_items_are_mentioned_not_enumerated():
    said = at.compose([{"tier": at.P1, "kind": "finished", "label": "one"}],
                      digest=[{}, {}])
    assert "2 smaller updates waiting" in said


def test_nothing_pending_says_nothing():
    assert at.compose([]) == ""


# ---- feedback ------------------------------------------------------------
def test_ignoring_alerts_quiets_them_and_acting_on_them_restores_trust():
    s = at._default_scores()
    for _ in range(5):
        s = at.feedback(s, "finished", at.IGNORED)
    assert s["finished"] < 0.35                     # now demoted
    for _ in range(20):
        s = at.feedback(s, "finished", at.ANSWERED_FAST)
    assert s["finished"] == 1.0                     # clamped, fully restored


def test_saying_less_of_this_is_stronger_than_ignoring():
    s = at._default_scores()
    a = at.feedback(s, "stuck", at.IGNORED)["stuck"]
    b = at.feedback(s, "stuck", at.LESS_OF_THIS)["stuck"]
    assert b < a


def test_scores_recover_over_time_so_one_bad_day_is_not_permanent():
    s = {"finished": 0.2}
    assert at.decay(s)["finished"] > 0.2
    assert at.decay({"finished": 0.99})["finished"] == 1.0   # never exceeds 1


def test_offers_to_mute_after_repeated_ignores():
    hist = [{"kind": "finished", "ignored": True},
            {"kind": "finished", "ignored": True}]
    assert at.should_offer_mute(hist, "finished") is True
    mixed = [{"kind": "finished", "ignored": True},
             {"kind": "finished", "ignored": False}]
    assert at.should_offer_mute(mixed, "finished") is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  attention: tiers, every gate, deferral, batching, self-tuning")

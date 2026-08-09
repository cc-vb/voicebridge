"""Answer routing: your reply must reach the agent that asked.

The dangerous failure here is silent: an answer delivered to the WRONG agent is
a wrong instruction to a machine that acts on it. So the rule these tests
enforce is, when it is not obvious, ask rather than guess.

Run: python3 tests/test_routing.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import routing  # noqa: E402

NOW = 100_000.0
SESSIONS = [{"sid": "s1", "label": "jobhunt"},
            {"sid": "s2", "label": "api"},
            {"sid": "s3", "label": "ccdash"}]


def find(q):
    q = (q or "").lower()
    for s in SESSIONS:
        if s["label"].lower() == q:
            return s
    for s in SESSIONS:
        if q and (q in s["label"].lower() or s["label"].lower() in q):
            return s
    return None


def _sid(label):
    return next(s["sid"] for s in SESSIONS if s["label"] == label)


def _focus(*labels, kind="blocked", ts=NOW):
    items = [{"sid": _sid(l), "label": l, "kind": kind} for l in labels]
    return routing.note_spoken(routing.new_focus(), items, now=ts)


def test_naming_an_agent_always_wins():
    f = _focus("api")
    r = routing.route("tell jobhunt to run the tests", f, active_sid="s9",
                      find=find, now=NOW)
    assert r["sid"] == "s1" and "named" in r["why"]


def test_a_reply_goes_to_the_single_agent_that_asked():
    f = _focus("api")
    for phrase in ("tell it to use the existing Redis store",
                   "use the redis one", "yes", "go with the second option"):
        r = routing.route(phrase, f, active_sid="s9", find=find, now=NOW + 10)
        assert r.get("sid") == "s2", phrase


def test_it_asks_instead_of_guessing_when_two_agents_wait():
    f = _focus("api", "ccdash")
    r = routing.route("tell it to use Redis", f, active_sid="s9",
                      find=find, now=NOW + 10)
    assert "ask" in r and "api" in r["ask"] and "ccdash" in r["ask"]
    assert "sid" not in r          # nothing delivered on a guess


def test_a_fresh_instruction_goes_to_your_current_session():
    f = _focus("api")
    r = routing.route("add a health check endpoint and run the tests", f,
                      active_sid="s9", find=find, now=NOW + 10)
    assert r["sid"] == "s9" and "current session" in r["why"]


def test_an_old_mention_no_longer_captures_your_reply():
    f = _focus("api", ts=NOW)
    r = routing.route("yes", f, active_sid="s9", find=find,
                      now=NOW + routing.REPLY_WINDOW + 1)
    assert r["sid"] == "s9"        # too long ago: back to the current session


def test_only_agents_that_asked_can_receive_a_reply():
    # an agent that merely FINISHED is not waiting for an answer
    f = routing.note_spoken(routing.new_focus(),
                            [{"sid": "s2", "label": "api", "kind": "finished"}],
                            now=NOW)
    r = routing.route("yes", f, active_sid="s9", find=find, now=NOW + 5)
    assert r["sid"] == "s9"


def test_permission_prompts_can_receive_a_spoken_yes():
    f = _focus("api", kind="permission")
    r = routing.route("yes", f, active_sid="s9", find=find, now=NOW + 5)
    assert r["sid"] == "s2"


def test_reply_cues_are_recognised_but_ordinary_sentences_are_not():
    assert routing.looks_like_reply("tell it to stop")
    assert routing.looks_like_reply("yes")
    assert routing.looks_like_reply("use the redis one")
    assert not routing.looks_like_reply("what is the status of everything")
    assert not routing.looks_like_reply("add input validation to signup")


def test_the_question_lists_every_waiting_agent():
    f = _focus("api", "ccdash", "jobhunt")
    r = routing.route("yes", f, active_sid="s9", find=find, now=NOW + 5)
    for lbl in ("api", "ccdash", "jobhunt"):
        assert lbl in r["ask"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  routing: named wins, replies reach the asker, ambiguity asks")

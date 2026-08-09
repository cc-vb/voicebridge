"""The local brain: advisory only, and it must fail soft.

The whole point of these tests is that Friday keeps working when the brain is
missing, slow, or wrong. A voice product that breaks because a model did not
load is worse than one that speaks in plainer sentences.

Run: python3 tests/test_brain.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import brain, supervisor  # noqa: E402


def _fake_chat(reply):
    return lambda messages, timeout, schema=None, max_tokens=200: reply


def test_no_brain_means_no_opinion_never_a_crash():
    orig = brain._chat
    try:
        brain._chat = _fake_chat("")          # server down / timed out
        assert brain.stakes("drop the table?") == ""
        assert brain.brief("x" * 900) == ""
        assert brain.phrase("api finished.") == ""
    finally:
        brain._chat = orig


def test_stakes_reads_the_constrained_answer():
    orig = brain._chat
    try:
        brain._chat = _fake_chat('{"stakes": "high"}')
        assert brain.stakes("delete the users table?") == "high"
        brain._chat = _fake_chat('{"stakes": "low"}')
        assert brain.stakes("rename this variable?") == "low"
    finally:
        brain._chat = orig


def test_garbage_from_the_model_is_ignored_not_trusted():
    orig = brain._chat
    try:
        for junk in ("not json", '{"stakes": "maybe"}', "{}", '{"stakes": 7}'):
            brain._chat = _fake_chat(junk)
            assert brain.stakes("anything?") == "", junk
    finally:
        brain._chat = orig


def test_an_empty_question_is_not_sent_to_the_model():
    calls = {"n": 0}
    orig = brain._chat

    def counting(*a, **k):
        calls["n"] += 1
        return '{"stakes": "high"}'
    try:
        brain._chat = counting
        assert brain.stakes("") == ""
        assert brain.stakes("   ") == ""
        assert calls["n"] == 0            # no wasted model call
    finally:
        brain._chat = orig


def test_a_briefing_must_actually_be_shorter():
    long = "The reply. " * 60              # 660 chars
    orig = brain._chat
    try:
        brain._chat = _fake_chat("Short and useful.")
        assert brain.brief(long) == "Short and useful."
        brain._chat = _fake_chat("x" * 900)      # padded, not summarized
        assert brain.brief(long) == ""           # rejected
    finally:
        brain._chat = orig


def test_short_text_is_never_summarized():
    orig = brain._chat
    try:
        brain._chat = _fake_chat("a summary")
        assert brain.brief("just a line") == ""
    finally:
        brain._chat = orig


def test_a_rambling_rewrite_is_rejected():
    orig = brain._chat
    try:
        brain._chat = _fake_chat("api finished, and here is a long essay " * 20)
        assert brain.phrase("api finished.") == ""
    finally:
        brain._chat = orig


def test_a_partly_downloaded_model_is_never_treated_as_ready():
    # the marker is what makes a model usable, not the file existing
    assert brain.model_ready() in (True, False)     # never raises
    orig = brain.OK_MARKER
    try:
        brain.OK_MARKER = Path("/nonexistent/marker")
        assert brain.model_ready() is False
    finally:
        brain.OK_MARKER = orig


def test_keywords_override_a_relaxed_model():
    """If the text plainly says something destructive, we do not defer to a 4B
    model's calm opinion."""
    orig = brain.stakes
    try:
        brain.stakes = lambda q: "low"           # model says relax
        assert supervisor._stakes({"kind": "permission",
                                   "detail": "rm -rf the build dir"}) == "high"
        assert supervisor._stakes({"kind": "permission",
                                   "detail": "tidy the imports"}) == "low"
    finally:
        brain.stakes = orig


def test_the_model_is_only_consulted_for_questions():
    asked = {"n": 0}
    orig = brain.stakes
    try:
        def counting(q):
            asked["n"] += 1
            return "high"
        brain.stakes = counting
        supervisor._stakes({"kind": "finished", "detail": "all done"})
        assert asked["n"] == 0        # a finish has no stakes to judge
        supervisor._stakes({"kind": "blocked", "detail": "which store?"})
        assert asked["n"] == 1
    finally:
        brain.stakes = orig


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  brain: advisory, fails soft, rejects junk, keywords still win")

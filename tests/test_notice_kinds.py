"""Phone notices, questions, and session-state truth.

Three phone bugs, three guards:
  1. an idle / completion notice must NEVER surface as a yes/no decision
     (that was "click Allow -> 'yes' typed into the prompt");
  2. an OPEN AskUserQuestion is detected so the phone can render option cards
     in the chat, and disappears the moment it is answered;
  3. the real session state (working / idle) is reported so the phone's orb
     can reconcile instead of sitting stuck on "working".

Run: python3 -m pytest tests/test_notice_kinds.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402


def _fresh(tmp):
    core.STATE_DIR = tmp
    core.PENDING_NOTICE = tmp / "pending_notice"


def test_classify_notice():
    assert core.classify_notice("Claude needs your permission to use Bash") == "permission"
    assert core.classify_notice("Claude is waiting for your input") == "idle"
    assert core.classify_notice("Task completed") == "idle"       # unknown -> idle
    assert core.classify_notice("") == "idle"


def test_idle_notice_is_not_a_yes_no_decision():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    # An idle / completion notice: recorded, surfaced as a message + kind, but
    # NEVER returned as a permission decision (no yes/no, no injected "yes").
    core.set_pending_notice("S1", "Claude is waiting for your input")
    assert core.get_pending_notice("S1") == ""          # not a decision
    assert core.get_pending_kind("S1") == "idle"
    assert "waiting" in core.get_pending_message("S1")

    # A real permission prompt still IS a decision.
    core.set_pending_notice("S1", "Claude needs your permission to run npm")
    assert "npm" in core.get_pending_notice("S1")
    assert core.get_pending_kind("S1") == "permission"


def _write(recs):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for r in recs:
        f.write(json.dumps(r) + "\n")
    f.close()
    return f.name


def test_pending_question_open_then_answered():
    ask = {"type": "assistant", "uuid": "a1", "message": {"content": [
        {"type": "text", "text": "Let me ask."},
        {"type": "tool_use", "id": "toolu_ABC", "name": "AskUserQuestion",
         "input": {"questions": [{
             "question": "Which target?", "header": "Target",
             "multiSelect": False,
             "options": [{"label": "A", "description": "first"},
                         {"label": "B", "description": "second"}]}]}}]}}
    path = _write([{"type": "user", "message": {"content": "go"}}, ask])
    q = core.pending_question(path)
    assert q and q["id"] == "toolu_ABC"
    assert q["questions"][0]["header"] == "Target"
    assert [o["label"] for o in q["questions"][0]["options"]] == ["A", "B"]
    # working while the tool_use is unanswered
    assert core.active_session_state(path) == "working"

    # answer it: a tool_result for that id + a following text reply
    with open(path, "a") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_ABC",
             "content": "answered A"}]}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "uuid": "a2",
                             "message": {"content": [
                                 {"type": "text", "text": "Doing A."}]}}) + "\n")
    assert core.pending_question(path) == {}
    assert core.active_session_state(path) == "idle"


def test_active_session_state():
    idle = _write([{"type": "assistant", "uuid": "a1",
                    "message": {"content": [{"type": "text", "text": "done"}]}}])
    assert core.active_session_state(idle) == "idle"
    working = _write([
        {"type": "assistant", "uuid": "a1",
         "message": {"content": [{"type": "text", "text": "ok"}]}},
        {"type": "user", "message": {"content": "next thing"}}])
    assert core.active_session_state(working) == "working"


if __name__ == "__main__":
    test_classify_notice()
    test_idle_notice_is_not_a_yes_no_decision()
    test_pending_question_open_then_answered()
    test_active_session_state()
    print("ok")

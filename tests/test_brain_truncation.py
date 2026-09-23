"""A model call cut off mid-thought must not look like a decision.

A reasoning-tuned model emits its thinking BEFORE the schema-constrained
answer, and the schema binds the answer, not how long it thinks first. So a
small max_tokens can be spent entirely on reasoning and return finish_reason
"length" with empty content. That arrived here as "" and was read downstream as
"that was just ordinary conversation", so the sentence fell through silently
and nothing anywhere recorded that a call had been cut off.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vb import brain


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _call(monkeypatch, payload, logged):
    monkeypatch.setattr(brain, "up", lambda: True)
    monkeypatch.setattr(brain.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(payload))
    monkeypatch.setattr(brain.core, "log", lambda m: logged.append(m))
    return brain._chat([{"role": "user", "content": "hi"}], timeout=5)


def test_a_truncation_is_recorded_not_swallowed(monkeypatch):
    logged = []
    out = _call(monkeypatch, {"choices": [{
        "finish_reason": "length",
        "message": {"content": "", "reasoning_content": "x" * 300}}]}, logged)
    assert out == ""                       # callers still get a string
    assert brain.last_stop == "length"     # but the reason survives
    assert any("TRUNCATED" in m for m in logged)
    assert any("300 chars of reasoning" in m for m in logged)


def test_an_ordinary_empty_answer_is_not_called_a_truncation(monkeypatch):
    """The model genuinely had nothing to say. That is a decision, not a fault,
    and it must not be logged as one or the log becomes noise."""
    logged = []
    out = _call(monkeypatch, {"choices": [{
        "finish_reason": "stop", "message": {"content": ""}}]}, logged)
    assert out == ""
    assert brain.last_stop == "stop"
    assert not any("TRUNCATED" in m for m in logged)


def test_a_normal_answer_is_unchanged(monkeypatch):
    out = _call(monkeypatch, {"choices": [{
        "finish_reason": "stop", "message": {"content": "  yes  "}}]}, [])
    assert out == "yes"
    assert brain.last_stop == "stop"


def test_a_failed_call_says_error_not_nothing(monkeypatch):
    monkeypatch.setattr(brain, "up", lambda: True)
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(brain.urllib.request, "urlopen", boom)
    monkeypatch.setattr(brain.core, "log", lambda m: None)
    assert brain._chat([], timeout=1) == ""
    assert brain.last_stop == "error"

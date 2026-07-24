"""While a phone call is live, the phone owns the audio: the Mac is silent.

Run: python3 tests/test_call_audio_ownership.py   (no pytest needed)
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
    core.CALL_HEARTBEAT = tmp / "call_heartbeat"


def test_call_live_heartbeat_and_expiry():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    assert core.call_live() is False          # no heartbeat -> not live
    core.mark_call_live()
    assert core.call_live() is True           # fresh -> live
    core.CALL_HEARTBEAT.write_text(str(time.time() - 60))
    assert core.call_live() is False          # stale -> call ended


def test_mac_speech_suppressed_during_live_call():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    core.mark_call_live()
    assert core.start_speech("hello there") is None   # silent on the Mac
    core.CALL_HEARTBEAT.write_text(str(time.time() - 60))
    # (not asserting a real speak after expiry, that would need audio; the
    # gate being the FIRST check in start_speech is covered by the None path)


def test_recent_turns_filters_machinery():
    tmp = Path(tempfile.mkdtemp())
    tp = tmp / "t.jsonl"
    recs = [
        {"type": "user", "message": {"role": "user", "content": "fix the bug"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Done, pushed."}]}},
        {"type": "user", "message": {"role": "user",
                                     "content": "<command-message>x</command-message>"}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "stuff"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash"}]}},
        {"type": "user", "message": {"role": "user", "content": "thanks"}},
    ]
    tp.write_text("\n".join(json.dumps(r) for r in recs))
    turns = core.recent_turns(str(tp))
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[0]["text"] == "fix the bug"
    assert turns[1]["text"] == "Done, pushed."
    assert turns[2]["text"] == "thanks"


if __name__ == "__main__":
    test_call_live_heartbeat_and_expiry()
    test_mac_speech_suppressed_during_live_call()
    test_recent_turns_filters_machinery()
    print("ok  call audio ownership + chat turns")

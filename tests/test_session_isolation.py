"""Phone session isolation: default-deny, only opted-in sessions are reachable.

Run: python3 tests/test_session_isolation.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core, talkd  # noqa: E402


def _fresh():
    tmp = Path(tempfile.mkdtemp())
    core.STATE_DIR = tmp
    talkd.STATE = tmp / "talk"
    talkd.PHONE = talkd.STATE / "phone"
    talkd.STATE.mkdir(parents=True, exist_ok=True)
    return tmp


def test_default_deny():
    _fresh()
    assert talkd.phone_enabled() == set()          # nothing enabled by default
    assert talkd.phone_is_enabled("S1") is False


def test_enable_disable_roundtrip():
    _fresh()
    talkd.phone_enable("S1", "/tmp/s1.jsonl")
    assert talkd.phone_is_enabled("S1")
    assert talkd.phone_enabled() == {"S1"}
    assert talkd.phone_path("S1") == "/tmp/s1.jsonl"
    talkd.phone_disable("S1")
    assert not talkd.phone_is_enabled("S1")
    assert talkd.phone_enabled() == set()


def test_only_enabled_are_listed():
    _fresh()
    talkd.phone_enable("S1", "/tmp/s1.jsonl")
    # S2 exists in the world but was never enabled: it must not be reachable
    assert "S2" not in talkd.phone_enabled()
    assert talkd.phone_is_enabled("S2") is False


def test_close_drops_enablement():
    _fresh()
    talkd.phone_enable("S1", "/tmp/s1.jsonl")
    talkd.phone_disable("S1")                       # session_closed calls this
    assert talkd.phone_enabled() == set()


if __name__ == "__main__":
    test_default_deny()
    test_enable_disable_roundtrip()
    test_only_enabled_are_listed()
    test_close_drops_enablement()
    print("ok  session isolation: default-deny, enable/disable, teardown")

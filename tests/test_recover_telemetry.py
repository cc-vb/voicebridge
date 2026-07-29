"""Layer 2 (recover) + Layer 3 (telemetry): recover safely, never leak data.

Run: python3 tests/test_recover_telemetry.py   (no pytest needed)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core, recover, telemetry  # noqa: E402


def test_telemetry_report_carries_no_user_content():
    try:
        raise ValueError("send to #secret-channel: /Users/alice/prompt.txt")
    except ValueError as e:
        rep = telemetry.build_report("daemon-loop", e)
    assert rep["err_type"] == "ValueError"
    assert rep["where"] == "daemon-loop"
    assert "v" in rep and "os" in rep
    # The whole point: no message, no path, nothing derived from user data.
    blob = json.dumps(rep)
    assert "secret-channel" not in blob
    assert "/Users/alice" not in blob
    assert "msg" not in rep and "message" not in rep


def test_telemetry_opt_in_default_off_and_send_is_noop_when_off():
    tmp = Path(tempfile.mkdtemp())
    orig = telemetry.OPT_IN
    try:
        telemetry.OPT_IN = tmp / "opt"
        assert telemetry.opted_in() is False        # OFF by default
        telemetry.send({"x": 1})                     # off -> no-op, no raise
        telemetry.set_opt_in(True)
        assert telemetry.opted_in() is True
        telemetry.send({"x": 1})                     # on but no endpoint -> no-op
        telemetry.set_opt_in(False)
        assert telemetry.opted_in() is False
    finally:
        telemetry.OPT_IN = orig


def test_remedy_maps_known_signatures():
    assert "Accessibility" in recover._remedy(
        Exception("osascript: not authorized to send keystrokes"))
    assert "voice server" in recover._remedy(
        Exception("kokoro server unavailable")).lower()
    assert recover._remedy(Exception("something totally novel")) == ""


def test_record_is_safe_and_logs_locally():
    tmp = Path(tempfile.mkdtemp())
    orig_state, orig_log = core.STATE_DIR, recover.ERR_LOG
    try:
        core.STATE_DIR = tmp
        recover.ERR_LOG = tmp / "errors.jsonl"
        hint = recover.record("unit-test", Exception("kokoro server unavailable"))
        assert "voice server" in hint.lower()
        assert recover.ERR_LOG.exists()
        assert recover.recent_errors(5)   # readable back
    finally:
        core.STATE_DIR, recover.ERR_LOG = orig_state, orig_log


if __name__ == "__main__":
    test_telemetry_report_carries_no_user_content()
    test_telemetry_opt_in_default_off_and_send_is_noop_when_off()
    test_remedy_maps_known_signatures()
    test_record_is_safe_and_logs_locally()
    print("ok  recover+telemetry: scrubbed reports, opt-in off by default, "
          "safe record")

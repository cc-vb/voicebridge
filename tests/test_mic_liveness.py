"""A stale 'the mic is busy' file must never silence voicebridge forever.

This one is worth a dedicated test because the failure is invisible: a leftover
active.json (crashed daemon, killed process, laptop slept) made every hook
believe the mic was owned, so replies stopped being spoken and the product
looked dead with nothing in the logs.

Run: python3 tests/test_mic_liveness.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core, talkd  # noqa: E402


def _with_active_json():
    d = Path(tempfile.mkdtemp())
    (d / "talk").mkdir()
    (d / "talk" / "active.json").write_text('{"session_id": "x"}')
    return d


def test_stale_active_json_without_a_daemon_does_not_claim_the_mic():
    orig_dir, orig_alive = core.STATE_DIR, talkd.daemon_alive
    try:
        core.STATE_DIR = _with_active_json()
        talkd.daemon_alive = lambda: False        # the daemon is gone
        assert core.mic_active() is False         # so the file means nothing
    finally:
        core.STATE_DIR, talkd.daemon_alive = orig_dir, orig_alive


def test_a_live_daemon_does_claim_the_mic():
    orig_dir, orig_alive = core.STATE_DIR, talkd.daemon_alive
    try:
        core.STATE_DIR = _with_active_json()
        talkd.daemon_alive = lambda: True
        assert core.mic_active() is True
    finally:
        core.STATE_DIR, talkd.daemon_alive = orig_dir, orig_alive


def test_when_liveness_cannot_be_determined_we_assume_the_mic_is_owned():
    # failing safe here means "stay quiet", which is better than talking over
    # a session we could not verify
    orig_dir, orig_alive = core.STATE_DIR, talkd.daemon_alive
    try:
        core.STATE_DIR = _with_active_json()

        def boom():
            raise RuntimeError("cannot check")
        talkd.daemon_alive = boom
        assert core.mic_active() is True
    finally:
        core.STATE_DIR, talkd.daemon_alive = orig_dir, orig_alive


if __name__ == "__main__":
    test_stale_active_json_without_a_daemon_does_not_claim_the_mic()
    test_a_live_daemon_does_claim_the_mic()
    test_when_liveness_cannot_be_determined_we_assume_the_mic_is_owned()
    print("ok  mic liveness: a stale file can no longer mute voicebridge")

"""Self-heal: deterministic, idempotent, only touches what's actually broken.

Run: python3 tests/test_selfheal.py   (no pytest needed)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core, selfheal  # noqa: E402


def test_pid_dead():
    assert selfheal._pid_dead(os.getpid()) is False   # us: alive
    assert selfheal._pid_dead(0) is True
    assert selfheal._pid_dead(2_147_480_000) is True   # nonexistent


def test_reap_stale_pids_only_dead_and_idempotent():
    tmp = Path(tempfile.mkdtemp())
    orig = core.STATE_DIR
    try:
        core.STATE_DIR = tmp
        dead = tmp / "talkd.pid"
        dead.write_text("2147480000")            # not running -> reap
        live = tmp / "call.pid"
        live.write_text(str(os.getpid()))        # alive -> keep

        lines = selfheal._reap_stale_pids(apply=True)
        assert not dead.exists()                 # stale marker removed
        assert live.exists()                     # live marker untouched
        assert any("talkd.pid" in ln for ln in lines)

        # idempotent: nothing left to reap on a second pass
        assert selfheal._reap_stale_pids(apply=True) == []
    finally:
        core.STATE_DIR = orig


def test_report_does_not_mutate():
    tmp = Path(tempfile.mkdtemp())
    orig = core.STATE_DIR
    try:
        core.STATE_DIR = tmp
        dead = tmp / "orb.pid"
        dead.write_text("2147480000")
        lines = selfheal.report()                # describe only
        assert dead.exists()                     # report() must NOT delete it
        assert any("orb.pid" in ln for ln in lines)
    finally:
        core.STATE_DIR = orig


if __name__ == "__main__":
    test_pid_dead()
    test_reap_stale_pids_only_dead_and_idempotent()
    test_report_does_not_mutate()
    print("ok  selfheal: dead-pid detection, reap-only-dead, report is read-only")

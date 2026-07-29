"""owner_sid(): resolve the CALLING session from the process tree.

`vb phone` launched by the Claude Code tool runs in a subprocess where the
LAST file is stale/empty, so it used to opt in no session and the phone showed
"no open sessions". owner_sid() maps our owner pid (the Claude process we run
under) back to a sid via the OWNERS registry. These lock that in.

Run: python3 tests/test_phone_owner.py   (no pytest needed)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import talkd  # noqa: E402


def test_owner_sid_matches_pid_to_registry():
    tmp = Path(tempfile.mkdtemp())
    orig_owners, orig_owner_pid = talkd.OWNERS, talkd.owner_pid
    try:
        talkd.OWNERS = tmp / "owners"
        talkd.OWNERS.mkdir(parents=True, exist_ok=True)
        (talkd.OWNERS / "SID-A").write_text("4242")
        (talkd.OWNERS / "SID-B").write_text("5353")

        # We are running under the Claude process pid 5353 -> its session wins.
        talkd.owner_pid = lambda: 5353
        assert talkd.owner_sid() == "SID-B"

        # A pid that isn't a recorded owner -> no false match.
        talkd.owner_pid = lambda: 9999
        assert talkd.owner_sid() == ""

        # No Claude ancestor found at all -> empty, never a wrong session.
        talkd.owner_pid = lambda: 0
        assert talkd.owner_sid() == ""
    finally:
        talkd.OWNERS, talkd.owner_pid = orig_owners, orig_owner_pid


def test_owner_live_rejects_recycled_pid():
    tmp = Path(tempfile.mkdtemp())
    orig = (talkd.OWNERS, talkd._pid_start, talkd._is_claude_pid)
    try:
        talkd.OWNERS = tmp / "owners"
        talkd.OWNERS.mkdir(parents=True, exist_ok=True)
        (talkd.OWNERS / "S1").write_text("4242:Mon Jul 28 21:39:00 2026")
        talkd._is_claude_pid = lambda pid: True

        # same start marker -> still the same live claude session
        talkd._pid_start = lambda pid: "Mon Jul 28 21:39:00 2026"
        assert talkd.owner_live("S1") is True

        # pid recycled by a different process -> start differs -> rejected
        talkd._pid_start = lambda pid: "Tue Jul 29 09:00:00 2026"
        assert talkd.owner_live("S1") is False

        # legacy pid-only record (no start marker) -> best-effort accept
        (talkd.OWNERS / "S2").write_text("4242")
        talkd._pid_start = lambda pid: "irrelevant"
        assert talkd.owner_live("S2") is True

        # pid not a Claude process -> rejected regardless of start
        talkd._is_claude_pid = lambda pid: False
        assert talkd.owner_live("S1") is False
    finally:
        talkd.OWNERS, talkd._pid_start, talkd._is_claude_pid = orig


def test_tty_for_sid_gated_on_owner_live():
    orig = talkd.owner_live
    try:
        talkd.owner_live = lambda sid, **k: False   # dead / reused owner
        assert talkd.tty_for_sid("ANY") == ""       # must not resolve a tty
    finally:
        talkd.owner_live = orig


if __name__ == "__main__":
    test_owner_sid_matches_pid_to_registry()
    test_owner_live_rejects_recycled_pid()
    test_tty_for_sid_gated_on_owner_live()
    print("ok  phone owner: sid resolution + pid-reuse-safe ownership")

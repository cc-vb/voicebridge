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


if __name__ == "__main__":
    test_owner_sid_matches_pid_to_registry()
    print("ok  phone owner: process-tree sid resolution")

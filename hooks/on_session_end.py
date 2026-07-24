#!/usr/bin/env python3
"""SessionEnd hook: voice stops when the session it was bound to stops.

Leaving a session by any deliberate route (/exit, Ctrl+C, closing the tab)
used to leave voice running: the marker file stayed, so the daemon kept the
microphone open and kept reading replies aloud out of a transcript nobody was
watching. Nothing could clear it either, because the window that would have
run /voice-off was gone.

This is the clean path: the moment Claude Code ends a session it tells us, and
we silence speech, release the mic and close any phone link that session
opened. The daemon's own owner watchdog (talkd.session_alive) is the backstop
for the endings that never reach a hook, a kill -9 or a crash.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core, talkd  # noqa: E402


def main() -> int:
    try:
        data = core.read_hook_input()
        sid = data.get("session_id", "")
        if sid:
            talkd.session_closed(sid, data.get("reason") or "session ended")
    except Exception as e:
        core.log(f"on_session_end failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

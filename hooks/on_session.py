#!/usr/bin/env python3
"""SessionStart hook: a one-time getting-started hint.

New users didn't know which command to run first. This prints the starter
commands once per machine (a flag file suppresses it forever after), so the
very first Claude Code session after install shows the way in.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402

HINT = """\
🎙  voicebridge is ready. Talk to Claude hands-free.

  START      /voice-on        talk to this session, hear replies
  HEY-CLAUDE /voice-wake      only reacts to "hey Claude ..."
  STOP       /voice-off       (or say "stop listening")
  INTERRUPT  just start typing your next message, the voice stops instantly
  PHONE      vb phone         use it from your phone (shows a QR to scan)
  VOICE      vb voice <name>  change the voice (vb voice = list all)
"""


def main() -> int:
    flag = core.STATE_DIR / "hint_shown"
    if flag.exists():
        return 0
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        flag.touch()
        # SessionStart hook stdout is surfaced to the user as context.
        print(HINT)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

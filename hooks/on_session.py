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
🎙  voicebridge is ready. Talk to Claude, hands-free.

  Not sure where to start? Pick by how you want to work:

    /voice-on     Heads-down pairing. Everything you say goes to Claude,
                  replies spoken back. Headphones recommended.
    /voice-wake   Multitasking or others around. Stays quiet until you say
                  "hey Claude ...", ignores everything else.

  New here? Just run /voice-on and start talking.

  To stop, least to most:
    Cmd+Alt+Ctrl+X   silence the voice now (or just start typing)
    Cmd+Alt+Ctrl+Z   silence AND stop Claude mid-answer
    /voice-off       stop listening in this session
    vb off           stop listening everywhere

  Later: "vb phone" to talk from your phone, "vb voice" to change the voice.
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

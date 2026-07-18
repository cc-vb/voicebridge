#!/usr/bin/env python3
"""Stop hook: speak Claude's final reply for the turn.

Claude Code fires this when a turn finishes. The payload gives us the
transcript path; we read the last assistant message and say it aloud.
Exits 0 fast and never blocks the session.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402


def main() -> int:
    if not core.is_enabled():
        return 0
    data = core.read_hook_input()
    transcript = data.get("transcript_path", "")
    if not transcript:
        return 0
    text = core.last_assistant_text(transcript)
    if text:
        core.speak(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

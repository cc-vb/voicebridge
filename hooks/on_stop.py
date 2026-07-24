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
    data = core.read_hook_input()
    sid = data.get("session_id", "")
    transcript = data.get("transcript_path", "")
    # The turn finished, so any recorded "Claude is waiting on you" decision
    # moment is resolved; clear it so the phone stops offering a stale yes/no.
    core.clear_pending_notice()
    # Strictly per-session: only speak if THIS session was voiced. Not a
    # global flag, so a session you never turned voice on for stays silent.
    if not core.is_voiced(sid):
        return 0
    if core.mic_active():   # voicemode channel speaks replies itself
        return 0
    if not transcript:
        return 0
    text = core.last_assistant_text(transcript)
    if text:
        core.speak(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

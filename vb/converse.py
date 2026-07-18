"""voicebridge converse: hands-free continuous 'agent mode'.

Tap once to start, then just talk. It listens, you speak, it injects your
words into the focused Claude window, waits for the reply, speaks it, and
automatically listens again. No buttons between turns. Say "stop" to end.

Design:
- We turn the transcript watcher OFF for the duration so it doesn't also
  speak (converse speaks replies itself, blocking, so it never records over
  its own voice). The watcher's enabled state is restored on exit.
- Replies are detected by watching the session transcript for a new
  assistant message. Best for conversational back-and-forth; on long
  multi-tool turns it may resume a little early.
- Use HEADPHONES: on speakers the mic hears the reply and the loop derails.
"""

import subprocess
import time

from . import core, inject, stt

EXIT_WORDS = {
    "stop", "stop it", "exit", "quit", "bye", "goodbye", "good bye",
    "that's all", "thats all", "that's it", "thats it", "end", "we're done",
    "were done", "done for now",
}

START_TINK = "/System/Library/Sounds/Tink.aiff"
STOP_POP = "/System/Library/Sounds/Pop.aiff"
NADA = "/System/Library/Sounds/Basso.aiff"


def _beep(sound: str) -> None:
    try:
        subprocess.Popen(["afplay", sound], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _wait_for_reply(transcript: str, prev: str, timeout: float = 120.0) -> str:
    """Poll the transcript until a new assistant message appears."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        cur = core.last_assistant_text(transcript)
        if cur and cur != prev:
            return cur
        time.sleep(0.5)
    return ""


def run(transcript: str = "") -> int:
    if not stt.whisper_bin() or not stt.MODEL.exists():
        print("STT not ready. Run: vb stt")
        return 1
    transcript = transcript or core.newest_transcript()
    if not transcript:
        print("No Claude session transcript found. Start Claude Code first.")
        return 1

    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    was_enabled = core.is_enabled()
    # Silence the watcher while converse runs (converse speaks replies itself).
    try:
        core.ENABLED_FLAG.unlink()
    except FileNotFoundError:
        pass

    print("agent mode: listening. Focus your Claude window. Say 'stop' to end.")
    core.speak("Agent mode on. Talk to me. Say stop when you're done.",
               blocking=True)

    wav = str(core.STATE_DIR / "rec.wav")
    empties = 0
    try:
        while True:
            _beep(START_TINK)
            ok = stt.record(wav, silence_stop=1.5)
            _beep(STOP_POP)
            if not ok:
                continue
            text = stt.transcribe(wav)
            if not text:
                empties += 1
                if empties >= 3:
                    core.speak("I can't hear you, ending agent mode.",
                               blocking=True)
                    break
                continue
            empties = 0

            if text.lower().strip(" .,!?") in EXIT_WORDS:
                core.speak("Okay, ending agent mode.", blocking=True)
                break

            print(f"you: {text}")
            prev = core.last_assistant_text(transcript)
            inject.paste_text(text, send=True)   # into the focused Claude
            reply = _wait_for_reply(transcript, prev)
            if reply:
                core.speak(reply, blocking=True)  # blocking so we don't record over it
                time.sleep(0.4)  # let speaker audio settle before listening (no-headphones safety)
            else:
                _beep(NADA)
                core.speak("I didn't get a reply, still listening.",
                           blocking=True)
    except KeyboardInterrupt:
        core.speak("Agent mode off.", blocking=True)
    finally:
        if was_enabled:
            core.ENABLED_FLAG.touch()   # restore the watcher
    print("agent mode: ended")
    return 0

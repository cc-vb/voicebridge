"""voicebridge converse: hands-free continuous 'agent mode'.

Tap once, then just talk. It listens, you speak, it asks Claude (headless,
in the current project dir), speaks the answer, and listens again. No
buttons, no window focus, no typing. Say "stop" to end.

Design:
- Talks to Claude via `claude -p ... --continue`, reading the reply straight
  off stdout. Far more robust than injecting into a TUI and scraping its
  transcript: no focus needed, and it can never hang waiting on a reply that
  isn't coming, so "stop" always works between turns.
- Replies inherit your `vb lang` tone/language (the prompt hook applies).
- Runs Claude in the directory you launch from, so `cd` to your project
  first (or pass it: `vb converse ~/my/project`).
- We silence the transcript watcher while converse runs (converse speaks
  replies itself, blocking, so it never records over its own voice).
- Use headphones if you can; on speakers keep the volume moderate.
"""

import os
import shutil
import subprocess
import time

from . import core, stt

EXIT_WORDS = {
    "stop", "stop it", "exit", "quit", "bye", "goodbye", "good bye",
    "that's all", "thats all", "that's it", "thats it", "end", "we're done",
    "were done", "done for now",
}

START_TINK = "/System/Library/Sounds/Tink.aiff"
STOP_POP = "/System/Library/Sounds/Pop.aiff"
THINK = "/System/Library/Sounds/Morse.aiff"
NADA = "/System/Library/Sounds/Basso.aiff"


def _beep(sound: str) -> None:
    try:
        subprocess.Popen(["afplay", sound], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _claude_bin() -> str:
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _ask(text: str, first: bool, cwd: str) -> str:
    """Ask Claude headless and return the reply text. --continue keeps context."""
    args = [_claude_bin(), "-p", text]
    if not first:
        args.append("--continue")
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=180, cwd=cwd)
        return (r.stdout or "").strip()
    except Exception as e:
        core.log(f"converse: claude call failed: {e}")
        return ""


def run(project_dir: str = "") -> int:
    if not stt.whisper_bin() or not stt.MODEL.exists():
        print("STT not ready. Run: vb stt")
        return 1
    cwd = os.path.abspath(os.path.expanduser(project_dir)) if project_dir \
        else os.getcwd()

    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    was_enabled = core.is_enabled()
    try:
        core.ENABLED_FLAG.unlink()   # silence the watcher during converse
    except FileNotFoundError:
        pass

    print(f"agent mode: talking to Claude in {cwd}. Say 'stop' to end.")
    core.speak("Agent mode on. Talk to me. Say stop when you're done.",
               blocking=True)

    wav = str(core.STATE_DIR / "rec.wav")
    first = True
    empties = 0
    try:
        while True:
            _beep(START_TINK)
            time.sleep(0.35)  # let the Tink finish so it's not your first word
            ok = stt.record(wav, silence_stop=2.0)
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
            _beep(THINK)  # a cue that it's thinking (reply takes a few seconds)
            reply = _ask(text, first, cwd)
            first = False
            if reply:
                core.speak(reply, blocking=True)  # blocking so we don't record over it
                time.sleep(0.4)
            else:
                _beep(NADA)
                core.speak("Sorry, I didn't get an answer. Still listening.",
                           blocking=True)
    except KeyboardInterrupt:
        core.speak("Agent mode off.", blocking=True)
    finally:
        if was_enabled:
            core.ENABLED_FLAG.touch()   # restore the watcher
    print("agent mode: ended")
    return 0

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

# Filler that can surround an exit word without changing intent
_EXIT_VOCAB = {
    "stop", "exit", "quit", "bye", "goodbye", "end", "done", "please",
    "it", "now", "ok", "okay", "thanks", "thank", "you", "agent", "mode",
    "the", "that's", "thats", "all",
}


def _is_exit(text: str) -> bool:
    """True for 'stop', 'stop stop stop', 'please stop now', etc.

    A real instruction like 'stop the research and fix the bug' keeps going,
    because it contains words outside the exit vocabulary.
    """
    t = text.lower().strip(" .,!?")
    if t in EXIT_WORDS:
        return True
    toks = [w.strip(" .,!?") for w in t.split()]
    toks = [w for w in toks if w]
    if not toks or not any(w in ("stop", "exit", "quit", "goodbye", "bye")
                           for w in toks):
        return False
    return all(w in _EXIT_VOCAB for w in toks)

START_TINK = "/System/Library/Sounds/Tink.aiff"
STOP_POP = "/System/Library/Sounds/Pop.aiff"
THINK = "/System/Library/Sounds/Morse.aiff"
NADA = "/System/Library/Sounds/Basso.aiff"


def _beep(sound: str) -> None:
    from . import core
    if core.call_live():
        return   # a phone call owns the audio; no cues to an empty room
    try:
        subprocess.Popen(["afplay", sound], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _claude_bin() -> str:
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _ask(text: str, first: bool, cwd: str, max_wait: float = 600.0) -> str:
    """Ask Claude headless and return the reply. --continue keeps context.

    Long turns (research, multi-tool work) are normal, so we wait up to
    max_wait but keep the user in the loop: a soft tick every ~10s and a
    spoken heads-up at 30s / 2min so silence never reads as 'it died'.
    """
    args = [_claude_bin(), "-p", text]
    if not first:
        args.append("--continue")
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, cwd=cwd)
    except Exception as e:
        core.log(f"converse: claude spawn failed: {e}")
        return ""
    waited = 0.0
    while True:
        try:
            out, _ = p.communicate(timeout=10)
            return (out or "").strip()
        except subprocess.TimeoutExpired:
            waited += 10
            _beep(THINK)
            if waited == 30:
                core.speak("Still working on it, hang tight.", blocking=True)
            elif waited == 120:
                core.speak("This one's taking a while. I'll speak up the "
                           "moment it's done.", blocking=True)
            elif waited >= max_wait:
                p.kill()
                core.log("converse: turn exceeded max_wait")
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

            if _is_exit(text):
                core.speak("Okay, ending agent mode.", blocking=True)
                break

            print(f"you: {text}")
            _beep(THINK)  # a cue that it's thinking (reply takes a few seconds)
            reply = _ask(text, first, cwd)
            first = False
            if reply:
                print(f"claude: {reply}")
                core.speak(reply, blocking=True)  # blocking so we don't record over it
                time.sleep(0.4)
            else:
                _beep(NADA)
                core.speak("Sorry, I didn't get an answer. Still listening.",
                           blocking=True)
    except KeyboardInterrupt:
        print("\n(interrupted)")
    finally:
        if was_enabled:
            core.ENABLED_FLAG.touch()   # restore the watcher
    print("agent mode: ended")
    return 0

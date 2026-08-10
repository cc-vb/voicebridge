"""voicebridge talk: hands-free voice link to your OPEN Claude session.

Channel-free (works on org accounts where Channels is disabled by policy).
You keep a normal interactive `claude` running in its own window; `vb talk`
runs beside it:

  mic -> local whisper -> typed into the focused Claude window (paste+Enter)
  session transcript -> new replies spoken aloud (Tara etc.)

Fire-and-forget by design: injection never waits on a reply, so 'stop'
always works. While waiting silently, a recording is cut short the moment a
reply lands, so answers are spoken promptly.

Requirements: keep the Claude window FOCUSED (the paste lands wherever your
cursor is), and start `vb talk` right after touching the session you want
voiced (it binds to the most recently active transcript, and re-binds after
your first spoken message).
"""

import os
import re
import time

from . import core, inject, stt
from .converse import _is_exit, _beep, START_TINK, STOP_POP, THINK

MUTE_RE = re.compile(
    r"^\s*(stop listening|mic off|mute|go to sleep|stop the mic)[.!\s]*$",
    re.IGNORECASE)


def _new_reply(transcript: str, prev: str) -> str:
    cur = core.last_assistant_text(transcript)
    return cur if cur and cur != prev else ""


def run(transcript: str = "") -> int:
    if not stt.whisper_bin() or not stt.MODEL.exists():
        print("STT not ready. Run: vb stt")
        return 1
    transcript = transcript or core.newest_transcript()
    if not transcript:
        print("No Claude session found. Open `claude` first, say something "
              "to it (or just run it), then start vb talk.")
        return 1

    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    was_enabled = core.is_enabled()
    try:
        core.ENABLED_FLAG.unlink()   # we speak replies ourselves
    except FileNotFoundError:
        pass

    print(f"voice link: {transcript}")
    print("keep your Claude window focused; say 'stop listening' to end")
    core.speak("Voice link on. Keep your Claude window focused and talk to "
               "me.", blocking=True)

    wav = str(core.STATE_DIR / "talk.wav")
    prev = core.last_assistant_text(transcript)
    rebound = False
    try:
        while True:
            # 1) Speak any reply that landed while we weren't recording.
            reply = _new_reply(transcript, prev)
            if reply:
                prev = reply
                print(f"claude: {reply[:120]}...")
                core.speak(reply, blocking=True)
                time.sleep(0.4)
                continue

            # 2) Listen; cut the wait short if a reply arrives mid-silence.
            _beep(START_TINK)
            time.sleep(0.35)
            try:
                os.remove(wav)
            except FileNotFoundError:
                pass
            p = stt.record_start(wav)
            if p is None:
                time.sleep(1)
                continue
            interrupted = False
            while p.poll() is None:
                time.sleep(0.3)
                if _new_reply(transcript, prev):
                    # Reply landed; if you haven't spoken yet, stop waiting.
                    try:
                        size = os.path.getsize(wav)
                    except OSError:
                        size = 0
                    if size < 5000:   # essentially no speech captured yet
                        p.terminate()
                        p.wait()
                        interrupted = True
                        break
            _beep(STOP_POP)
            if interrupted:
                continue   # top of loop speaks the reply

            # 3) Handle what you said.
            try:
                if os.path.getsize(wav) < 2000:
                    continue
            except OSError:
                continue
            text = stt.transcribe(wav)
            if not text:
                continue
            if _is_exit(text) or MUTE_RE.match(text):
                core.speak("Okay, voice link off.", blocking=True)
                break

            print(f"you: {text}")
            # Named target: unqualified this typed into the frontmost window,
            # whatever it was, and pressed Return.
            _app = ""
            try:
                from .talkd import bound_app as _ba
                _app = _ba()
            except Exception:
                _app = ""
            if not inject.paste_text(text, send=True, expect_app=_app):
                print("  (couldn't reach the session; nothing was sent)")
                continue
            _beep(THINK)
            time.sleep(1.5)
            if not rebound:
                # Your message just made the target session the most recently
                # modified transcript; re-bind in case we guessed wrong.
                nt = core.newest_transcript()
                if nt and nt != transcript:
                    print(f"voice link re-bound: {nt}")
                    transcript = nt
                prev = core.last_assistant_text(transcript)
                rebound = True
    except KeyboardInterrupt:
        print("\n(interrupted)")
    finally:
        if was_enabled:
            core.ENABLED_FLAG.touch()
    print("voice link: ended")
    return 0

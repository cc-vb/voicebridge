"""voicebridge talkd: per-session voice mode, toggled from inside a session.

The UX mirrors /remote-control: type /voice-on inside any Claude Code
session and voice binds to THAT session; /voice-off leaves it. Multiple
sessions can each be voiced; the single daemon follows whichever voiced
session you interacted with most recently (there's only one mic).

How binding works (the fix for "it picked the wrong session"): the
UserPromptSubmit hook receives session_id + transcript_path on every prompt
and records them. When /voice-on runs `vb talkd on`, it binds to the session
that just submitted that prompt, never a guess.

State (~/.voicebridge/talk/):
  last_prompt.json   session_id + transcript of the latest prompt anywhere
  voiced/<sid>       file per voiced session (content = transcript path)
  active.json        which voiced session the mic follows right now
  talkd.pid          the daemon
"""

import json
import os
import re
import subprocess
import sys
import time

from . import core, inject, stt
from .converse import _is_exit, _beep, START_TINK, STOP_POP, THINK

STATE = core.STATE_DIR / "talk"
VOICED = STATE / "voiced"
LAST = STATE / "last_prompt.json"
ACTIVE = STATE / "active.json"
PID = STATE / "talkd.pid"

MUTE_RE = re.compile(
    r"^\s*(stop listening|mic off|mute|go to sleep|stop the mic)[.!\s]*$",
    re.IGNORECASE)

# Whisper renders non-speech as bracketed/parenthesized tags: "(air
# whooshing)", "[BLANK_AUDIO]", "(wind blowing)". Never treat those as words.
NOISE_RE = re.compile(r"^[\s\(\[][^\)\]]*[\)\]][.!\s]*$|^[\s.,!?]*$")


def is_noise(text: str) -> bool:
    return bool(NOISE_RE.match(text.strip()))


# ---------- state helpers (called from the hook and the CLI) -----------------

def record_prompt(session_id: str, transcript_path: str) -> None:
    """Called by the UserPromptSubmit hook on every prompt, any session."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": session_id,
                   "transcript_path": transcript_path, "ts": time.time()}
        LAST.write_text(json.dumps(payload))
        # If this session is voiced, the mic follows it (most recent wins).
        if (VOICED / session_id).exists():
            ACTIVE.write_text(json.dumps(payload))
    except Exception as e:
        core.log(f"talkd.record_prompt failed: {e}")


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def voice_on(ensure: bool = True) -> str:
    last = _read_json(LAST)
    if not last:
        return ("ERROR: no prompt recorded yet. Type any message in this "
                "session first, then run /voice-on again.")
    sid, tp = last["session_id"], last["transcript_path"]
    VOICED.mkdir(parents=True, exist_ok=True)
    (VOICED / sid).write_text(tp)
    ACTIVE.write_text(json.dumps(last))
    if ensure:
        ensure_daemon()
    n = len(list(VOICED.iterdir()))
    return (f"voice mode ON for session {sid[:8]} ({n} voiced session"
            f"{'s' if n != 1 else ''}). Mic follows this session now.")


def voice_off(ensure: bool = True) -> str:
    last = _read_json(LAST)
    if not last:
        return "ERROR: no prompt recorded; nothing to turn off."
    sid = last["session_id"]
    try:
        (VOICED / sid).unlink()
    except FileNotFoundError:
        return f"voice mode was not on for session {sid[:8]}."
    active = _read_json(ACTIVE)
    if active and active.get("session_id") == sid:
        try:
            ACTIVE.unlink()
        except FileNotFoundError:
            pass
    remaining = list(VOICED.iterdir()) if VOICED.exists() else []
    if not remaining and ensure:
        stop_daemon()
        return f"voice mode OFF for session {sid[:8]}. No voiced sessions left; mic stopped."
    return (f"voice mode OFF for session {sid[:8]}. "
            f"{len(remaining)} voiced session(s) remain.")


def daemon_alive() -> bool:
    try:
        pid = int(PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def ensure_daemon() -> None:
    if daemon_alive():
        return
    STATE.mkdir(parents=True, exist_ok=True)
    vb = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin", "vb")
    p = subprocess.Popen([sys.executable, vb, "talkd", "__run__"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    PID.write_text(str(p.pid))


def stop_daemon() -> None:
    try:
        os.kill(int(PID.read_text().strip()), 15)
    except Exception:
        pass
    try:
        PID.unlink()
    except FileNotFoundError:
        pass
    subprocess.run(["pkill", "-x", "say"], capture_output=True)


def status() -> str:
    lines = [f"daemon : {'running' if daemon_alive() else 'stopped'}"]
    active = _read_json(ACTIVE)
    lines.append(f"active : {active['session_id'][:8] if active else '(none)'}")
    if VOICED.exists():
        for f in VOICED.iterdir():
            lines.append(f"voiced : {f.name[:8]} -> {f.read_text().strip()}")
    return "\n".join(lines)


# ---------- the daemon --------------------------------------------------------

def _any_speech_playing() -> bool:
    """True while ANY `say` is talking (ours, a hook's, a stale watcher's)."""
    return subprocess.run(["pgrep", "-x", "say"],
                          capture_output=True).returncode == 0


def _wait_for_silence(max_wait: float = 60.0) -> None:
    t0 = time.time()
    while _any_speech_playing() and time.time() - t0 < max_wait:
        time.sleep(0.25)
    time.sleep(0.3)   # let speaker audio settle before the mic opens


def run_daemon() -> int:
    core.log("talkd: started")
    wav = str(STATE / "talkd.wav")
    prev: dict = {}
    announced: set = set()
    while True:
        active = _read_json(ACTIVE)
        if not active or not (VOICED / active["session_id"]).exists():
            time.sleep(0.5)
            continue
        sid, tp = active["session_id"], active["transcript_path"]
        if sid not in announced:
            # No spoken announce: the assistant's own short confirmation is
            # spoken via the reply path; a second announcement is noise.
            prev[tp] = core.last_assistant_text(tp)
            announced.add(sid)

        # 1) Speak any new reply in the active session.
        cur = core.last_assistant_text(tp)
        if cur and cur != prev.get(tp):
            prev[tp] = cur
            core.speak(cur, blocking=True)
            time.sleep(0.4)
            continue

        # 2) Listen; abandon the wait early if a reply lands or focus moves.
        _wait_for_silence()   # never record while ANY speech is playing
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
        cut = False
        while p.poll() is None:
            time.sleep(0.3)
            now_active = _read_json(ACTIVE)
            switched = (not now_active
                        or now_active.get("session_id") != sid)
            newreply = core.last_assistant_text(tp) != prev.get(tp)
            if switched or newreply:
                try:
                    size = os.path.getsize(wav)
                except OSError:
                    size = 0
                if size < 5000:
                    p.terminate()
                    p.wait()
                    cut = True
                    break
        _beep(STOP_POP)
        if cut:
            continue

        # 3) Handle speech.
        try:
            if os.path.getsize(wav) < 2000:
                continue
        except OSError:
            continue
        text = stt.transcribe(wav)
        if not text or is_noise(text):
            continue
        if _is_exit(text) or MUTE_RE.match(text):
            try:
                (VOICED / sid).unlink()
            except FileNotFoundError:
                pass
            try:
                ACTIVE.unlink()
            except FileNotFoundError:
                pass
            announced.discard(sid)
            remaining = list(VOICED.iterdir()) if VOICED.exists() else []
            core.speak("Okay, voice mode off for this session.",
                       blocking=True)
            if not remaining:
                core.log("talkd: no voiced sessions left, exiting")
                try:
                    PID.unlink()
                except FileNotFoundError:
                    pass
                return 0
            continue

        core.log(f"talkd you: {text}")
        inject.paste_text(text, send=True)
        _beep(THINK)
        time.sleep(1.0)
        prev[tp] = core.last_assistant_text(tp)

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

MODE = STATE / "mode"   # "all" (default): every utterance goes in
                        # "wake": only utterances addressed to the wake word

# Whisper renders "Claude" many ways. A bare strict name can wake it;
# loose homophones (cloud/clod/...) need a greeting so ordinary sentences
# like "cloud computing is..." never trigger.
_GREET = r"(?:hey|ok|okay|yo|hi|hai|he)"
_STRICT = r"(?:claude|claud|klaude?|clyde)"
_LOOSE = r"(?:cloud|clod|clawed|clot|claw|glod|glaud|clown|clued|klaud)"
WAKE_RE = re.compile(
    rf"^\s*(?:{_GREET}[,!\s]+(?:{_STRICT}|{_LOOSE})|{_STRICT})\b[,!.\s]*(.*)$",
    re.IGNORECASE | re.DOTALL)

# Voice toggles are heard imperfectly ("weak word mode", "wait word mode"),
# so accept the homophones. Typed /voice-wake and /voice-agent are the
# deterministic way to switch.
TO_WAKE_RE = re.compile(
    r"^\s*(switch to )?(wake|weak|wait|week|work)([- ]?word)? ?mode[.!\s]*$",
    re.IGNORECASE)
TO_ALL_RE = re.compile(
    r"^\s*(switch to )?(agent|agentic|asian|urgent|continuous|normal)"
    r" ?mode[.!\s]*$",
    re.IGNORECASE)


def get_mode() -> str:
    try:
        m = MODE.read_text().strip()
        return m if m in ("all", "wake") else "all"
    except Exception:
        return "all"


def set_mode(mode: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    MODE.write_text(mode)


def wake_match(text: str) -> "tuple[bool, str]":
    """(addressed?, prompt-with-wake-word-stripped)."""
    m = WAKE_RE.match(text)
    if not m:
        return False, ""
    return True, m.group(1).strip()

# Whisper renders non-speech as bracketed/parenthesized tags: "(air
# whooshing)", "[BLANK_AUDIO]", "(wind blowing)". Never treat those as words.
NOISE_RE = re.compile(r"^[\s\(\[][^\)\]]*[\)\]][.!\s]*$|^[\s.,!?]*$")


def is_noise(text: str) -> bool:
    return bool(NOISE_RE.match(text.strip())) or _foreign_script(text)


# voicebridge speakers use Latin (English/Hinglish) or Devanagari (Hindi).
# A capture dominated by other scripts (Korean/CJK/Cyrillic/...) is almost
# always background media (e.g. a TV), not the user, so drop it.
_SUPPORTED = re.compile(r"[A-Za-zऀ-ॿ]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _foreign_script(text: str) -> bool:
    letters = _LETTER.findall(text)
    if len(letters) < 3:
        return False
    supported = len(_SUPPORTED.findall(text))
    return supported / len(letters) < 0.5


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

PAUSE = STATE / "pause"


CUES = STATE / "cues"
BARGE = STATE / "barge"


def get_barge() -> str:
    """Voice barge-in (talking over speech to interrupt). Default OFF:
    voice-interrupt can't always tell the user from the speaker echo, so
    the reliable interrupts are typing a new prompt or the hush hotkey."""
    try:
        v = BARGE.read_text().strip()
        return v if v in ("on", "off") else "off"
    except Exception:
        return "off"


def get_cues() -> str:
    """Cue sounds: 'on' = every listen cycle, 'once' = a single ding when
    voice mode activates (default), 'off' = fully silent."""
    try:
        v = CUES.read_text().strip()
        return v if v in ("on", "once", "off") else "once"
    except Exception:
        return "once"


def cues_on() -> bool:
    return get_cues() != "off"


def _cue(sound: str) -> None:
    """Per-cycle cue: only in 'on' mode."""
    if get_cues() == "on":
        _beep(sound)


def _cue_event(sound: str) -> None:
    """One-time event cue (mode activation): 'on' and 'once' modes."""
    if get_cues() != "off":
        _beep(sound)


SENS = STATE / "sens"

# (min whisper confidence, min RMS loudness) per sensitivity level.
# strict = only clear, close, confident speech gets through.
# Calibrated against a real MacBook mic: directed speech lands ~0.009-0.03
# RMS, background TV/chatter ~0.005-0.008. (Synthetic test audio is ~10x
# hotter; don't calibrate against it.)
_SENS_TH = {
    "relaxed": (0.30, 0.003),
    "normal": (0.42, 0.007),
    "strict": (0.55, 0.015),
}


def get_sens() -> str:
    try:
        v = SENS.read_text().strip()
        return v if v in _SENS_TH else "normal"
    except Exception:
        return "normal"


def accept_capture(text: str, conf: float, loud: float) -> "tuple[bool, str]":
    """Is this capture really the user talking to Claude, or noise/chatter?"""
    min_conf, min_rms = _SENS_TH[get_sens()]
    if conf and conf < min_conf:
        return False, f"low-confidence ({conf:.2f})"
    if 0 <= loud < min_rms:
        return False, f"too-quiet ({loud:.3f})"
    return True, ""


_DIRECTED_STARTS = {
    "can", "could", "would", "please", "run", "show", "fix", "what", "how",
    "why", "when", "where", "which", "who", "list", "open", "close", "stop",
    "yes", "no", "okay", "ok", "yeah", "nope", "do", "don't", "make", "add",
    "create", "tell", "explain", "give", "check", "try", "go", "continue",
    "wait", "let's", "lets", "hey", "search", "find", "read", "write",
    "delete", "commit", "push", "test", "build", "install", "help",
}


def looks_directed(text: str) -> bool:
    """For SHORT utterances in agent mode: is this aimed at Claude, or a
    stray remark? Long utterances pass automatically; short ones need a
    command/question shape, or to be an answer to a question we just asked."""
    words = [w.strip(".,!?'\"") for w in text.lower().split()]
    words = [w for w in words if w]
    if len(words) >= 4:
        return True
    if not words:
        return False
    if text.rstrip().endswith("?") or words[0] in _DIRECTED_STARTS:
        return True
    try:
        last = json.loads(
            (core.STATE_DIR / "last_spoken_text").read_text())["text"]
        if last.rstrip().endswith("?"):
            return True   # we asked; a short answer is expected
    except Exception:
        pass
    return False


_TRAILING_INCOMPLETE = {
    "and", "or", "but", "so", "like", "because", "then", "also", "plus",
    "with", "to", "the", "a", "an", "of", "for", "in", "on", "at", "is",
    "are", "was", "i", "we", "you", "it", "that", "this", "my", "your",
}


def followup_window(text: str) -> float:
    """How long to keep the mic open for a continuation, judged from how
    finished the words sound. Trailing conjunctions mean they're mid-thought."""
    t = text.rstrip()
    if not t:
        return 3.5
    if t.endswith((",", ";", ":", "-")):
        return 6.0
    lastword = t.strip(".!?, ").split()[-1].lower() if t.strip(".!?, ") else ""
    if lastword in _TRAILING_INCOMPLETE:
        return 6.0
    if t.endswith(("?", "!", ".")):
        return 2.0
    return 3.5


def get_pause() -> float:
    """Seconds of silence that end an utterance (default 2.5)."""
    try:
        return max(1.0, min(6.0, float(PAUSE.read_text().strip())))
    except Exception:
        return 2.5


def _stitch_more(wav: str, pause: float, so_far: str = "") -> str:
    """After a capture, briefly keep listening: if the speaker resumes
    (they paused to think), capture the continuation(s) and return them.

    The follow-up window adapts to how finished the words sound: a trailing
    'and...' holds the mic ~6s, a finished question only ~2s."""
    parts = []
    text_so_far = so_far
    while True:
        window = followup_window(text_so_far)
        try:
            os.remove(wav)
        except FileNotFoundError:
            pass
        p = stt.record_start(wav, silence_stop=pause)
        if p is None:
            break
        t0 = time.time()
        started = False
        while p.poll() is None:
            time.sleep(0.25)
            try:
                started = started or os.path.getsize(wav) > 4000
            except OSError:
                pass
            if not started and time.time() - t0 > window:
                p.terminate()
                p.wait()
                break
        if not started:
            break
        more, conf = stt.transcribe_ex(wav)
        if not more or is_noise(more):
            break
        min_conf, _ = _SENS_TH[get_sens()]
        if conf and conf < min_conf:
            break
        parts.append(more)
        text_so_far = f"{text_so_far} {more}"
    return " ".join(parts)


_WORD_RE = re.compile(r"[a-z0-9']+")


def _spoken_words(window_s: float) -> set:
    """Every word we've spoken recently (rolling history, not just the last
    utterance, older speech can still be echoing when new speech starts)."""
    words: set = set()
    now = time.time()
    try:
        for ln in (core.STATE_DIR / "spoken_history").read_text().splitlines():
            try:
                rec = json.loads(ln)
                if now - float(rec.get("ts", 0)) <= window_s:
                    words |= set(_WORD_RE.findall(rec.get("text", "").lower()))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    if not words:   # fallback: the single last utterance
        try:
            rec = json.loads(
                (core.STATE_DIR / "last_spoken_text").read_text())
            if now - float(rec.get("ts", 0)) <= window_s:
                words = set(_WORD_RE.findall(rec.get("text", "").lower()))
        except Exception:
            pass
    return words


def _is_echo(text: str, window_s: float = 90.0) -> bool:
    """True if `text` is (mostly) the system's own recent speech."""
    spoken = _spoken_words(window_s)
    heard = _WORD_RE.findall(text.lower())
    if len(heard) < 3 or not spoken:
        return False
    overlap = sum(1 for w in heard if w in spoken) / len(heard)
    return overlap >= 0.6


def _is_assistant_echo(text: str) -> bool:
    """True if the capture is really a recent ASSISTANT REPLY being read
    aloud, by anyone. Another app (e.g. the Claude desktop app's voice
    mode) can speak a reply we never voiced; our history won't know it,
    but the session transcripts do, so compare against the latest reply of
    every voiced session and drop matches instead of injecting them."""
    heard = _WORD_RE.findall(text.lower())
    if len(heard) < 6:
        return False
    try:
        files = list(VOICED.iterdir())
    except FileNotFoundError:
        return False
    for f in files:
        try:
            reply = core.last_assistant_text(f.read_text().strip())
        except Exception:
            continue
        if not reply:
            continue
        spoken = set(_WORD_RE.findall(reply.lower()))
        if not spoken:
            continue
        overlap = sum(1 for w in heard if w in spoken) / len(heard)
        if overlap >= 0.6:
            return True
    return False


ATTENTION_RE = re.compile(
    r"\b(wait|stop|listen|hold on|hey|claude|glory|cloud|clod|pause|"
    r"excuse me|one second|hang on|escape|shut up|quiet|chup)\b",
    re.IGNORECASE)


def echo_residue(text: str, window_s: float = 90.0) -> "tuple[str, float]":
    """Split a capture into (what the USER said, echo overlap 0..1).

    While we talk on speakers, the mic hears our TTS mixed with the user.
    Subtracting our recently spoken words leaves the user's words, so a
    barge-in is recognized on the FIRST try instead of being dropped as
    echo until they repeat themselves."""
    heard = _WORD_RE.findall(text.lower())
    if not heard:
        return "", 1.0
    spoken = _spoken_words(window_s)
    if not spoken:
        return text, 0.0
    residue = [w for w in heard if w not in spoken]
    overlap = 1.0 - len(residue) / len(heard)
    return " ".join(residue), overlap


def _speak_interruptible(text: str) -> str:
    """Speak a reply, but listen WHILE speaking: if the user talks over it
    out loud, cut the speech and return their words as the next prompt.

    The mic hears our own TTS on speakers, so a capture only counts as a
    barge-in if it survives the echo guard (their words, not ours). Returns
    "" when the reply played out uninterrupted."""
    t = core.clean_for_speech(text, max_chars=6000)
    if not t or core._recently_spoken(t):
        return ""
    say = core.start_speech(t)   # engine-aware (kokoro or say) + echo record
    if say is None:
        return ""
    if get_barge() != "on":
        # Voice barge-in disabled (default): play the reply out; it stays
        # interruptible by a typed prompt (hook hush) or the hotkey.
        try:
            say.wait()
        except Exception:
            pass
        return ""
    wav = str(STATE / "barge.wav")
    try:
        while say.poll() is None:
            try:
                os.remove(wav)
            except FileNotFoundError:
                pass
            # Short capture windows so barge-ins are judged quickly even
            # while our own audio keeps the room from ever going silent.
            rec = stt.record_start(wav, max_secs=6, silence_stop=0.7)
            if rec is None:
                say.wait()
                break
            while rec.poll() is None and say.poll() is None:
                time.sleep(0.2)
            if rec.poll() is None:      # speech finished first
                rec.terminate()
                rec.wait()
                break
            try:
                if os.path.getsize(wav) < 4000:
                    continue
            except OSError:
                continue
            heard = stt.transcribe(wav)
            if not heard or is_noise(heard) or _is_assistant_echo(heard):
                continue
            residue, overlap = echo_residue(heard)
            if overlap < 0.55:
                barge = heard           # clearly the user, take it whole
            elif ATTENTION_RE.search(residue) or len(residue.split()) >= 3:
                barge = residue         # user talking OVER us; keep their words
            else:
                continue                # just our own voice; keep talking
            say.terminate()
            core.hush()
            say.wait()
            core.log(f"talkd barge-in ({overlap:.2f} echo): {barge[:80]}")
            return barge
    except Exception as e:
        core.log(f"speak_interruptible: {e}")
    return ""


def _any_speech_playing() -> bool:
    """True while ANY speech is playing: `say` from anywhere, or the current
    engine player tracked in speech.pid (kokoro/afplay)."""
    if subprocess.run(["pgrep", "-x", "say"],
                      capture_output=True).returncode == 0:
        return True
    try:
        os.kill(int(core.SPEECH_PID.read_text().strip()), 0)
        return True
    except Exception:
        return False


def _wait_for_silence(max_wait: float = 60.0) -> None:
    t0 = time.time()
    while _any_speech_playing() and time.time() - t0 < max_wait:
        time.sleep(0.25)
    time.sleep(0.3)   # let speaker audio settle before the mic opens


def run_daemon() -> int:
    core.log(f"talkd: started (pid {os.getpid()})")
    try:
        PID.write_text(str(os.getpid()))   # claim singleton ownership
    except Exception:
        pass
    wav = str(STATE / "talkd.wav")
    prev: dict = {}
    announced: set = set()
    follow_until = 0.0   # wake mode: window after "hey Claude" alone
    queued = ""          # barge-in captured while a reply was speaking
    while True:
        # Singleton: if another daemon claimed the pid file, this one exits.
        try:
            if PID.read_text().strip() != str(os.getpid()):
                core.log(f"talkd: superseded, exiting (pid {os.getpid()})")
                return 0
        except Exception:
            pass
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
            _cue_event(START_TINK)   # single "voice mode is live" ding

        # 1) Speak any new reply, listening for a barge-in while talking.
        cur = core.last_assistant_text(tp)
        if cur and cur != prev.get(tp):
            prev[tp] = cur
            barge = _speak_interruptible(cur)
            if barge:
                queued = barge   # user talked over the reply; that's the prompt
            time.sleep(0.3)
            if not barge:
                continue

        # 2) Listen (or use a barge-in already captured during speech).
        mode = get_mode()
        pause = get_pause()
        in_follow = time.time() < follow_until
        if queued:
            text = queued
            queued = ""
        else:
            _wait_for_silence()   # never record while ANY speech is playing
            if mode == "all" or in_follow:
                _cue(START_TINK)   # wake mode listens silently (ambient)
                time.sleep(0.35)
            try:
                os.remove(wav)
            except FileNotFoundError:
                pass
            p = stt.record_start(wav, silence_stop=pause)
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
            if mode == "all" or in_follow:
                _cue(STOP_POP)
            if cut:
                continue

            # 3) Handle speech: is this really the user talking to Claude?
            try:
                if os.path.getsize(wav) < 2000:
                    continue
            except OSError:
                continue
            text, conf = stt.transcribe_ex(wav)
            if not text or is_noise(text):
                continue
            if _is_echo(text):
                core.log(f"talkd echo dropped: {text[:80]}")
                continue
            if _is_assistant_echo(text):
                core.log(f"talkd assistant-echo dropped: {text[:80]}")
                continue
            ok, why = accept_capture(text, conf, stt.loudness(wav))
            if not ok:
                core.log(f"talkd dropped ({why}): {text[:80]}")
                continue
            if mode == "all" and not looks_directed(text):
                core.log(f"talkd dropped (not directed): {text[:80]}")
                continue

        # Mode switching by voice, from either mode.
        if TO_WAKE_RE.match(text):
            set_mode("wake")
            core.speak("Wake word mode. Say hey Claude when you need me.",
                       blocking=True)
            continue
        if TO_ALL_RE.match(text):
            set_mode("all")
            core.speak("Agent mode. I'm taking everything now.",
                       blocking=True)
            continue

        # Wake mode: ignore everything not addressed to the wake word.
        if mode == "wake" and not in_follow:
            addressed, prompt = wake_match(text)
            if not addressed:
                continue   # ambient chatter, drop silently
            if not prompt:
                core.speak("Yes?", blocking=True)
                follow_until = time.time() + 12   # next utterance is the prompt
                continue
            text = prompt
        follow_until = 0.0

        # The speaker may have paused to think; stitch continuations.
        more = _stitch_more(wav, pause, text)
        if more:
            text = f"{text} {more}"

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
        _cue(THINK)
        time.sleep(1.0)
        prev[tp] = core.last_assistant_text(tp)

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
import shutil
import subprocess
import sys
import threading
import time

from . import core, inject, stt
from .converse import _is_exit, _beep, START_TINK, STOP_POP, THINK

STATE = core.STATE_DIR / "talk"
VOICED = STATE / "voiced"
LAST = STATE / "last_prompt.json"
ACTIVE = STATE / "active.json"
PID = STATE / "talkd.pid"

APP = STATE / "app"    # the app /voice-on was run in; the ONLY paste target


def bind_app(name: str = "") -> str:
    """Remember which app owns voice. Injection targets it and nothing else.

    Without this, a paste lands in whatever happens to be frontmost, which
    during a meeting means typing into the meeting and pressing Return."""
    name = (name or inject.frontmost_app()).strip()
    if name:
        STATE.mkdir(parents=True, exist_ok=True)
        APP.write_text(name)
    return name


def bound_app() -> str:
    try:
        return APP.read_text().strip()
    except Exception:
        return ""


def app_focused(frontmost: str, bound: str) -> bool:
    """May we record and inject right now?

    Voice belongs to the window you turned it on in. If we're bound to an
    app and it isn't the frontmost one, stop. If we're bound but CAN'T see
    the frontmost app (screen locked, switched to another macOS user), also
    stop: fail closed, so voice never carries over to a switched user.
    Only when unbound (old version) do we fall back to always-on."""
    if not bound:
        return True
    if not frontmost:
        return False   # bound but can't confirm our app is front -> stop
    return frontmost.strip().casefold() == bound.casefold()


MUTE_RE = re.compile(
    r"^\s*(stop listening|mic off|mute|go to sleep|stop the mic)[.!\s]*$",
    re.IGNORECASE)

MODE = STATE / "mode"   # "all" (default): every utterance goes in
                        # "wake": only utterances addressed to the wake word

# Whisper hears "Claude" a dozen ways. Be generous: an optional greeting
# then Claude OR any close homophone, at the START of the utterance, wakes
# it. Users asked for leniency ("hey cloud", "you cloud", "glory" should all
# work), so bare homophones at the start trigger too. False fires on
# "cloud computing..." are acceptable in wake mode (it's opt-in) and the
# real prompt still follows.
_GREET = r"(?:hey|ok|okay|yo|hi|hai|he|hello|yes|you|a|ay|oi)"
_STRICT = r"(?:claude|claud|klaude?|clyde|cloudy)"
_LOOSE = (r"(?:cloud|clod|clawed|clot|claw|glod|glaud|glory|gloria|clown|"
          r"clued|klaud|crowd|loud|clode|chlo|flow|lord)")
WAKE_RE = re.compile(
    rf"^\s*(?:{_GREET}[,!\s]+)?(?:{_STRICT}|{_LOOSE})\b[,!.\s]*(.*)$",
    re.IGNORECASE | re.DOTALL)

# Voice toggles are heard imperfectly ("weak word mode", "wait word mode"),
# so accept the homophones. Typed /voice-wake and /voice-agent are the
# deterministic way to switch.
# Fleet voice commands (multi-session control).
ROSTER_RE = re.compile(
    r"^\s*(which (agents?|sessions?)( need me)?|list (my )?sessions|"
    r"(my )?sessions|what('?s| is) running|status of (all )?sessions)"
    r"[.!?\s]*$", re.IGNORECASE)
SWITCH_RE = re.compile(
    r"^\s*(?:switch|go|move|jump) (?:to|over to|into) (?:the )?"
    r"([a-z0-9 _-]+?)(?: session| project)?[.!?\s]*$", re.IGNORECASE)
READLAST_RE = re.compile(
    r"^\s*(?:read|what did|tell me what) (?:me )?(?:out )?(?:the )?"
    r"([a-z0-9 _-]+?)(?:'s)?(?: session| project)?(?: last| latest)?"
    r"(?: reply| say| said| output)[.!?\s]*$", re.IGNORECASE)
ALERTS = STATE / "alerts"   # "on" (default): announce agents that go idle

FASTER_RE = re.compile(r"^\s*(speak |talk |go )?(faster|speed up|quicker)"
                       r"[.!\s]*$", re.IGNORECASE)
SLOWER_RE = re.compile(r"^\s*(speak |talk |go )?(slower|slow down|slow it "
                       r"down)[.!\s]*$", re.IGNORECASE)
NORMAL_SPEED_RE = re.compile(r"^\s*(normal|regular|default) speed[.!\s]*$",
                             re.IGNORECASE)


def _adjust_speed(delta: float = 0.0, absolute: float = 0.0) -> str:
    cur = int(core.get_rate()) / 175.0
    x = absolute if absolute else max(0.5, min(2.5, cur + delta))
    core.RATE_FILE.write_text(str(int(round(175.0 * x))))
    return f"{x:g} times speed"


def alerts_on() -> bool:
    try:
        return ALERTS.read_text().strip() != "off"
    except Exception:
        return True

TO_WAKE_RE = re.compile(
    r"^\s*(switch to )?(wake|weak|wait|week|work)([- ]?word)? ?mode[.!\s]*$",
    re.IGNORECASE)
TO_ALL_RE = re.compile(
    r"^\s*(switch to )?(agent|agentic|asian|urgent|continuous|normal)"
    r" ?mode[.!\s]*$",
    re.IGNORECASE)

# "continue" resumes a reply the character cap cut short. Kept to bare
# resume phrasings so an ordinary sentence starting "continue with..."
# still reaches Claude as a prompt.
CONTINUE_RE = re.compile(
    r"^\s*(please |ok |okay )?(continue|carry on|go on|keep going|"
    r"(read |say )?the rest)( reading| from there| please)?[.!\s]*$",
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
# Any capture containing foreign-script letters (Korean/CJK/Cyrillic/Arabic/
# ...) is background media (a TV, a video), never the user, so drop it. Even
# two such characters is a giveaway ("MBC 뉴스"): real speech has none.
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _foreign_script(text: str) -> bool:
    other = [c for c in _LETTER.findall(text)
             if not ("a" <= c.lower() <= "z" or "ऀ" <= c <= "ॿ")]
    return len(other) >= 2


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


# ---------- the silence hotkey ------------------------------------------------
#
# Cmd+Alt+Ctrl+X is delivered by skhd, which reads ~/.skhdrc and needs its own
# Accessibility grant. Installing it as a launchd service means a keyboard
# hook running at login forever, for a key that only means anything while we
# are speaking. So voice owns its lifetime instead: up when the first session
# is voiced, down when the last one leaves.

HOTKEY_PID = STATE / "skhd.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def hotkey_up() -> None:
    """Start skhd for the duration of voice mode, if it isn't already up.

    An skhd the user runs themselves (for their own bindings) is left alone:
    we only ever stop one we started, tracked by HOTKEY_PID."""
    skhd = shutil.which("skhd")
    if not skhd:
        return
    if subprocess.run(["pgrep", "-x", "skhd"],
                      capture_output=True).returncode == 0:
        return
    try:
        p = subprocess.Popen([skhd], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        HOTKEY_PID.write_text(str(p.pid))
    except Exception as e:
        core.log(f"hotkey_up failed: {e}")


def hotkey_down() -> None:
    """Stop the skhd we started. Never touches one that was already running."""
    try:
        pid = int(HOTKEY_PID.read_text().strip())
    except Exception:
        return
    if _pid_alive(pid):
        try:
            os.kill(pid, 15)
        except Exception as e:
            core.log(f"hotkey_down failed: {e}")
    try:
        HOTKEY_PID.unlink()
    except FileNotFoundError:
        pass


def voice_on(ensure: bool = True) -> str:
    last = _read_json(LAST)
    if not last:
        return ("ERROR: no prompt recorded yet. Type any message in this "
                "session first, then run /voice-on again.")
    sid, tp = last["session_id"], last["transcript_path"]
    VOICED.mkdir(parents=True, exist_ok=True)
    # Exclusive: only ONE session is voiced at a time. With a single mic,
    # multiple voiced sessions hear each other's spoken replies and inject
    # them as prompts, drop the others so voice always belongs to here.
    for f in VOICED.iterdir():
        if f.name != sid:
            try:
                f.unlink()
            except OSError:
                pass
    (VOICED / sid).write_text(tp)
    ACTIVE.write_text(json.dumps(last))
    app = bind_app()
    if ensure:
        ensure_daemon()
        hotkey_up()
    where = f" Bound to {app}; ignored elsewhere." if app else ""
    return (f"voice mode ON for session {sid[:8]}. This session only; any "
            f"other session's voice is now off.{where} Mic is yours here.")


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


def voice_off_all() -> str:
    """Leave voice mode everywhere and release the microphone.

    /voice-off only knows about the session it runs in, and nothing removes a
    marker for a session that was closed rather than turned off. Those
    leftovers keep the daemon alive holding the input device, and they cannot
    be cleared from the window that created them because that window is gone.
    Deleting the markers by hand does not help either: the daemon only
    re-reads the directory when it hears a spoken exit phrase. So this is the
    one command that ends voice for the whole machine, from any terminal."""
    sids = sorted(f.name for f in VOICED.iterdir()) if VOICED.exists() else []
    for sid in sids:
        try:
            (VOICED / sid).unlink()
        except FileNotFoundError:
            pass
    try:
        ACTIVE.unlink()
    except FileNotFoundError:
        pass
    if daemon_alive():
        stop_daemon()
        freed = "mic released."
    else:
        freed = "daemon was already stopped."
    if not sids:
        head = "no voiced sessions."
    else:
        head = "voice mode OFF for %d session(s): %s." % (
            len(sids), ", ".join(s[:8] for s in sids))
    return f"{head} {freed}\n{status()}"


def daemon_alive() -> bool:
    try:
        pid = int(PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _daemon_pids() -> list:
    # Only THIS user's daemons: another account's voicebridge (separate home
    # + state) is a different install we can't and shouldn't touch.
    r = subprocess.run(["pgrep", "-U", str(os.getuid()),
                        "-f", "vb talkd __run__"],
                       capture_output=True, text=True)
    return [int(x) for x in r.stdout.split() if x.strip().isdigit()]


def _kill_all_daemons(keep: int = 0) -> None:
    """Kill every talkd daemon (optionally keeping one pid). Orphans from
    earlier runs otherwise survive restarts and cause double-voice. Escalate
    to SIGKILL: a daemon blocked in a subprocess (recording/speaking) soaks
    SIGTERM, so plain terminate leaves zombies that keep listening."""
    import signal
    for pid in _daemon_pids():
        if pid == keep or pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    time.sleep(0.3)
    for pid in _daemon_pids():
        if pid == keep or pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGKILL)   # force , it ignored SIGTERM
        except Exception:
            pass


def ensure_daemon() -> None:
    if daemon_alive():
        # A valid daemon owns the pid file; kill any OTHER (orphan) daemons.
        _kill_all_daemons(keep=int(PID.read_text().strip()))
        return
    _kill_all_daemons()   # clear any orphan before starting a fresh one
    STATE.mkdir(parents=True, exist_ok=True)
    vb = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin", "vb")
    p = subprocess.Popen([sys.executable, vb, "talkd", "__run__"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    PID.write_text(str(p.pid))


def stop_daemon() -> None:
    _kill_all_daemons()
    try:
        PID.unlink()
    except FileNotFoundError:
        pass
    subprocess.run(["pkill", "-x", "say"], capture_output=True)
    # The recorder is a plain child of the daemon, so killing the daemon
    # orphans it rather than stopping it: it keeps the input device until it
    # hits its own trim limit, up to 30 seconds. We just told the user the mic
    # was released. Match on our own wav so an unrelated sox recording of the
    # user's survives.
    subprocess.run(["pkill", "-f", str(STATE / "talkd.wav")],
                   capture_output=True)
    hotkey_down()


def status() -> str:
    lines = [f"daemon : {'running' if daemon_alive() else 'stopped'}"]
    active = _read_json(ACTIVE)
    lines.append(f"active : {active['session_id'][:8] if active else '(none)'}")
    app = bound_app()
    lines.append(f"app    : {app or '(unbound - any app, legacy behaviour)'}")
    if app:
        front = inject.frontmost_app()
        state = "listening" if app_focused(front, app) else f"dormant ({front})"
        lines.append(f"focus  : {state}")
    if VOICED.exists():
        for f in VOICED.iterdir():
            lines.append(f"voiced : {f.name[:8]} -> {f.read_text().strip()}")
    return "\n".join(lines)


# ---------- the daemon --------------------------------------------------------

PAUSE = STATE / "pause"


CUES = STATE / "cues"
BARGE = STATE / "barge"


def get_barge() -> str:
    """Voice barge-in (talking over speech to interrupt). Default ON, gated
    by barge_decision so speaker echo can't trigger it. Typing a new prompt
    and the hush hotkey remain the always-reliable interrupts."""
    try:
        v = BARGE.read_text().strip()
        return v if v in ("on", "off") else "on"
    except Exception:
        return "on"


def get_cues() -> str:
    """Cue sounds: 'on' = a soft tick each listen cycle so you always know
    it's hearing you (default), 'once' = only a ding when voice activates,
    'off' = fully silent."""
    try:
        v = CUES.read_text().strip()
        return v if v in ("on", "once", "off") else "on"
    except Exception:
        return "on"


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
    finished the words sound. Trailing conjunctions mean they're mid-thought.
    Tightened per issue #2: the old 3.5s default was the common case (whisper
    often drops terminal punctuation), so short commands paid the slow path."""
    t = text.rstrip()
    if not t:
        return 0.8
    # Finished punctuation wins first: "do it." is complete even though "it"
    # is a trailing-incomplete word, so check terminal punctuation before the
    # word list (that ordering bug made finished commands wait the long path).
    if t.endswith(("?", "!", ".")):
        return 0.5
    if t.endswith((",", ";", ":", "-")):
        return 2.0
    lastword = t.split()[-1].lower().strip(".!?,")
    if lastword in _TRAILING_INCOMPLETE:
        return 2.0
    return 0.8


def get_pause() -> float:
    """Seconds of silence that end an utterance (default 1.0). Stitching
    repairs a mid-sentence breath, so a short pause stays coherent; slower
    speakers can raise it with `vb pause`."""
    try:
        return max(0.6, min(6.0, float(PAUSE.read_text().strip())))
    except Exception:
        return 1.0


def _transcribe_async(wav: str):
    """Transcribe `wav` on a background thread and return a callable that
    blocks for the (text, conf) result. Lets the caller overlap the ~1.3s
    whisper pass with the idle end-of-speech grace window (issue #2):
    transcribe_ex only reads the wav file, so nothing else in the loop
    touches its state while it runs."""
    holder = {}

    def run():
        try:
            holder["r"] = stt.transcribe_ex(wav)
        except Exception as e:  # never let a thread crash take the daemon
            holder["r"] = ("", 0.0)
            core.log(f"talkd async transcribe failed: {e}")

    th = threading.Thread(target=run, daemon=True)
    th.start()

    def result():
        th.join()
        return holder.get("r", ("", 0.0))

    return result


def _listen_continuation(wav: str, pause: float, window: float) -> str:
    """One follow-up listen: hold the mic up to `window` seconds for speech
    to resume; if it does, capture until `pause` of silence and transcribe.
    Returns the continuation text ('' if the speaker didn't resume, or it was
    noise / too quiet). Shared by the overlapped first listen and the
    _stitch_more loop so the accept/reject gating lives in one place."""
    try:
        os.remove(wav)
    except FileNotFoundError:
        pass
    p = stt.record_start(wav, silence_stop=pause)
    if p is None:
        return ""
    t0 = time.time()
    started = False
    while p.poll() is None:
        time.sleep(0.15)
        try:
            started = started or os.path.getsize(wav) > 4000
        except OSError:
            pass
        if not started and time.time() - t0 > window:
            p.terminate()
            p.wait()
            break
    if not started:
        return ""
    more, conf = stt.transcribe_ex(wav)
    if not more or is_noise(more):
        return ""
    min_conf, _ = _SENS_TH[get_sens()]
    if conf and conf < min_conf:
        return ""
    return more


def _stitch_more(wav: str, pause: float, so_far: str = "") -> str:
    """After a capture, briefly keep listening: if the speaker resumes
    (they paused to think), capture the continuation(s) and return them.

    The follow-up window adapts to how finished the words sound: a trailing
    'and...' holds the mic longer, a finished question only briefly."""
    parts = []
    text_so_far = so_far
    while True:
        more = _listen_continuation(wav, pause, followup_window(text_so_far))
        if not more:
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


# A barge-in cancels a reply mid-sentence, so the bar is higher than for an
# ordinary capture: one stray word (a cough transcribed as a word, someone
# else in the room, a clipped "adios") must never cut Claude off. Attention
# words are the deliberate exception, since that is how you interrupt.
BARGE_MIN_WORDS = 3


def barge_decision(heard: str, conf: float, loud: float) -> str:
    """What the user said over the top of our speech, or "" to keep talking.

    Applies the same noise/confidence/loudness gate as an ordinary capture,
    then requires the leftover words to be substantive. Echo, stray
    syllables and background chatter all lose to the reply already playing."""
    if not heard or is_noise(heard) or _is_assistant_echo(heard):
        return ""
    ok, _ = accept_capture(heard, conf, loud)
    if not ok:
        return ""
    residue, overlap = echo_residue(heard)
    if ATTENTION_RE.search(residue):
        return residue          # "stop"/"wait" cuts through at any length
    # Only CLEARLY-new speech interrupts otherwise. The old "overlap < 0.6"
    # path false-cut our OWN long replies: over a long utterance the mic
    # hears our voice, echo-subtraction is imperfect, and leftover words
    # looked like a barge-in and killed playback mid-reply. Require very low
    # overlap (genuinely different words) AND enough of them.
    if overlap < 0.25 and len(residue.split()) >= BARGE_MIN_WORDS:
        return heard
    return ""                   # mostly our own voice; keep talking


def screen_capture(text: str, conf: float, loud: float, mode: str) -> str:
    """Why this capture should be dropped, or "" to accept it.

    Every path that can reach inject.paste_text goes through here, including
    barge-ins: text captured while we were speaking is the MOST likely to be
    our own echo, so it needs the guard more than a quiet-room capture does."""
    if not text or is_noise(text):
        return "noise"
    if _is_echo(text):
        return "echo"
    if _is_assistant_echo(text):
        return "assistant-echo"
    ok, why = accept_capture(text, conf, loud)
    if not ok:
        return why
    if mode == "all" and not looks_directed(text):
        return "not directed"
    return ""


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
        # Voice barge-in disabled (default): play the reply out, but stop the
        # instant you switch away from the bound window (don't keep talking
        # into another app/user), and stay interruptible by typing/the hotkey.
        bound = bound_app()
        try:
            while say.poll() is None:
                time.sleep(0.25)
                if bound and not app_focused(inject.frontmost_app(), bound):
                    core.hush()
                    break
            say.wait()
        except Exception:
            pass
        return ""
    wav = str(STATE / "barge.wav")
    barge = ""
    bound = bound_app()
    try:
        while say.poll() is None:
            if bound and not app_focused(inject.frontmost_app(), bound):
                core.hush()   # switched away mid-reply -> stop speaking
                core.set_hud("away")
                break
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
            heard, conf = stt.transcribe_ex(wav)
            barge = barge_decision(heard, conf, stt.loudness(wav))
            if not barge:
                continue
            say.terminate()
            core.hush()
            say.wait()
            core.log(f"talkd barge-in: {barge[:80]}")
            return barge
    except Exception as e:
        # Never let a bug in here eat the reply silently. If we already cut
        # the speech we must still hand back what the user said, otherwise
        # the reply dies mid-sentence and their words vanish with it.
        core.log(f"speak_interruptible: {e!r}")
        try:
            if say.poll() is not None:
                return barge
        except Exception:
            pass
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


def _cleanup_temp() -> None:
    """Delete audio scratch files older than an hour so they don't pile up."""
    import glob
    now = time.time()
    roots = [str(core.STATE_DIR), str(core.STATE_DIR / "vm-tmp"),
             str(core.STATE_DIR / "remote-tmp"),
             str(core.STATE_DIR / "telegram" / "tmp")]
    for r in roots:
        for ext in ("*.wav", "*.aiff", "*.ogg", "*.oga"):
            for f in glob.glob(os.path.join(r, ext)):
                try:
                    if now - os.path.getmtime(f) > 3600:
                        os.remove(f)
                except OSError:
                    pass


def run_daemon() -> int:
    core.log(f"talkd: started (pid {os.getpid()})")
    _cleanup_temp()
    try:
        PID.write_text(str(os.getpid()))   # claim singleton ownership
    except Exception:
        pass
    # Warm the Kokoro server in the background so the FIRST reply doesn't pay
    # the model-load wait (and never silently drops to the robotic voice).
    if core.get_engine() == "kokoro":
        threading.Thread(target=core.ensure_kokoro_server,
                         daemon=True).start()
    # Warm the STT server too, so the first utterance doesn't pay a ~2s
    # cold model+backend load (issue #2). Falls back to the CLI if absent.
    threading.Thread(target=stt.ensure_whisper_server, daemon=True).start()
    wav = str(STATE / "talkd.wav")
    wav2 = str(STATE / "talkd_cont.wav")   # continuation, recorded while
    #                                        the main capture transcribes
    prev: dict = {}
    announced: set = set()
    follow_until = 0.0   # wake mode: window after "hey Claude" alone
    queued = ""          # barge-in captured while a reply was speaking
    fleet_states = {}    # sid -> state, for idle alerts
    fleet_next = 0.0     # next fleet-check time
    first_fleet = True   # skip alerts on the very first scan (no baseline)
    unfocused_since = 0.0   # when the bound app lost focus (0.0 = focused)
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

        # Focus gate. While you're in another app we hold no mic at all, so
        # a meeting or any other recorder gets a free input device, and we
        # cannot type into whatever has your cursor. We also stay quiet,
        # since a reply read aloud into a live meeting is its own problem;
        # `prev` is left untouched, so a reply that lands while you're away
        # is spoken when you come back rather than lost.
        bound = bound_app()
        focused = app_focused(inject.frontmost_app(), bound)
        if not focused:
            if not unfocused_since:
                unfocused_since = time.time()
                core.hush()   # switched away -> stop speaking, not just listening
                core.log(f"talkd: {bound} not frontmost, voice paused "
                         "(silence + mic released)")
            time.sleep(1.0)
            continue
        if unfocused_since:
            core.log(f"talkd: {bound} refocused after "
                     f"{time.time() - unfocused_since:.0f}s, mic live")
            unfocused_since = 0.0
            queued = ""   # anything overheard on the way back isn't a prompt

        if sid not in announced:
            # No spoken announce: the assistant's own short confirmation is
            # spoken via the reply path; a second announcement is noise.
            prev[tp] = core.latest_assistant_uuid(tp)
            announced.add(sid)
            _cue_event(START_TINK)   # single "voice mode is live" ding

        # 0) Fleet alerts: every ~12s, announce any OTHER agent that just
        # finished (working -> idle), so you hear "jobhunt is ready" without
        # watching terminals. Skipped while speaking.
        if alerts_on() and time.time() >= fleet_next and not _any_speech_playing():
            from . import sessions as _sess
            fresh, fleet_states = _sess.newly_idle(fleet_states)
            fleet_next = time.time() + 12
            others = [n for n in fresh if n]  # labels only
            if fleet_states and others and not first_fleet:
                names = ", ".join(others[:4])
                core.speak(f"Heads up: {names} "
                           f"{'is' if len(others) == 1 else 'are'} ready for "
                           f"you.", blocking=True)
            first_fleet = False

        # 1) Speak every reply we have not spoken yet, oldest first.
        #
        # We can only look here, between recordings, and in agent mode the mic
        # is open nearly all the time. Asking "is the newest reply different
        # from the last one I said" loses any reply that landed while we were
        # listening: the newest moves on and the one in between is never
        # spoken. The Stop hook cannot cover for us either, it stands down
        # whenever a session is voiced (core.mic_active). So track what we
        # have said by uuid and drain the backlog in order.
        pending = core.assistant_replies_after(tp, prev.get(tp, ""))
        if pending:
            if len(pending) > 1:
                core.log(f"talkd: {len(pending)} replies queued while listening")
            barge = ""
            for uid, text in pending:
                prev[tp] = uid      # marked read before speaking: a crash mid
                barge = _speak_interruptible(text)   # reply must not loop it
                if barge:
                    queued = barge  # talked over the reply; that's the prompt
                    break
            time.sleep(0.3)
            if not barge:
                continue

        # 2) Listen (or use a barge-in already captured during speech).
        mode = get_mode()
        pause = get_pause()
        in_follow = time.time() < follow_until
        first_more = ""   # continuation captured during the overlapped listen
        if queued:
            # A barge-in was captured while we spoke. It cleared the barge
            # bar, but it still has to clear the injection bar: conf/loudness
            # were already judged against the wav, so pass the skip sentinels.
            text = queued
            queued = ""
            why = screen_capture(text, 0.0, -1.0, mode)
            if why:
                core.log(f"talkd barge dropped ({why}): {text[:80]}")
                continue
        else:
            _wait_for_silence()   # never record while ANY speech is playing
            if mode == "all" or in_follow:
                _cue(START_TINK)   # wake mode listens silently (ambient)
                time.sleep(0.15)   # brief, just so the cue isn't recorded
            try:
                os.remove(wav)
            except FileNotFoundError:
                pass
            t_rec = time.time()
            core.set_hud("wake" if (mode == "wake" and not in_follow)
                         else "listening")
            p = stt.record_start(wav, silence_stop=pause)
            if p is None:
                time.sleep(1)
                continue
            cut = False
            while p.poll() is None:
                time.sleep(0.15)
                # Live mic meter: once your voice registers, the indicator
                # flips to "hearing" and pulses with the level, so you can see
                # it's catching you (and know when it isn't).
                lvl = stt.live_level(wav)
                core.set_hud("hearing" if lvl > 0.12 else
                             ("wake" if (mode == "wake" and not in_follow)
                              else "listening"), level=lvl)
                now_active = _read_json(ACTIVE)
                switched = (not now_active
                            or now_active.get("session_id") != sid)
                newreply = bool(core.assistant_replies_after(tp,
                                                            prev.get(tp, "")))
                if switched or newreply:
                    try:
                        size = os.path.getsize(wav)
                    except OSError:
                        size = 0
                    if size < 5000:
                        p.terminate()
                        p.wait()
                        cut = True
                        if switched:
                            core.set_hud("away")
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
            # Overlap (issue #2): transcribe what we just heard on a
            # background thread WHILE we hold the mic open for a possible
            # continuation. The mic is idle during that grace window anyway,
            # so the ~1.3s whisper pass hides behind it instead of adding to
            # it. We don't yet know how finished the words sound, so this
            # first listen uses the neutral window; if the real text turns
            # out mid-thought, the stitch loop below extends it with the
            # true (longer) window. If this turns out to be a command or gets
            # dropped, the speculative continuation is simply discarded.
            _t0 = time.time()
            core.set_hud("thinking")   # transcribing now
            get_text = _transcribe_async(wav)
            first_more = _listen_continuation(wav2, pause, followup_window(""))
            text, conf = get_text()
            core.log(f"latency: capture+silence {_t0 - t_rec:.2f}s, "
                     f"transcribe+listen {time.time() - _t0:.2f}s -> "
                     f"{text[:40]!r}")
            why = screen_capture(text, conf, stt.loudness(wav), mode)
            if why:
                if text:
                    core.log(f"talkd dropped ({why}): {text[:80]}")
                continue

        # Fleet control by voice: roster + switch across all sessions.
        if ROSTER_RE.match(text):
            from . import sessions as _sess
            core.speak(_sess.speak_roster(), blocking=True)
            continue
        m_rl = READLAST_RE.match(text)
        if m_rl:
            from . import sessions as _sess
            core.speak(_sess.read_last(m_rl.group(1)), blocking=True)
            continue
        m_sw = SWITCH_RE.match(text)
        if m_sw and not m_sw.group(1).rstrip().endswith("mode"):
            from . import sessions as _sess
            core.speak(_sess.switch(m_sw.group(1)), blocking=True)
            continue

        # Playback speed by voice.
        if FASTER_RE.match(text):
            core.speak(_adjust_speed(delta=0.25), blocking=True)
            continue
        if SLOWER_RE.match(text):
            core.speak(_adjust_speed(delta=-0.25), blocking=True)
            continue
        if NORMAL_SPEED_RE.match(text):
            core.speak(_adjust_speed(absolute=1.0), blocking=True)
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

        # "continue" picks up a reply the cap cut short, without
        # round-tripping through Claude.
        if CONTINUE_RE.match(text):
            if not core.speak_remainder():
                core.speak("Nothing pending. That was the whole reply.",
                           blocking=True)
            continue

        # Wake mode: ignore everything not addressed to the wake word.
        if mode == "wake" and not in_follow:
            addressed, prompt = wake_match(text)
            if not addressed:
                continue   # ambient chatter, drop silently
            _cue_event(START_TINK)   # heard the wake word, you know it's live
            if not prompt:
                core.speak("Yes?", blocking=True)
                follow_until = time.time() + 12   # next utterance is the prompt
                continue
            text = prompt
        follow_until = 0.0

        # The speaker may have paused to think; stitch continuations. The
        # first follow-up listen already ran during transcription (the
        # overlap), using the neutral window. Only listen AGAIN if either the
        # speaker actually resumed (first_more) or the now-known text is
        # mid-thought and deserves a window longer than the neutral one we
        # already waited. For a finished/neutral short command that got no
        # continuation, we're done here -> no extra probe, no wasted seconds.
        if first_more:
            text = f"{text} {first_more}"
            more = _stitch_more(wav2, pause, text)
            if more:
                text = f"{text} {more}"
        elif followup_window(text) > followup_window(""):
            more = _stitch_more(wav2, pause, text)
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
        # Re-checked at the moment of the paste: focus can change during the
        # seconds we spent recording and transcribing.
        if not inject.paste_text(text, send=True, expect_app=bound):
            continue
        _cue(THINK)
        time.sleep(1.0)
        # Nothing is marked read here. This used to re-baseline on the newest
        # reply after every prompt, which threw away any reply that landed in
        # the second we just slept. The queue is drained at the top of the
        # loop, so by now everything spoken is already marked and everything
        # unmarked still deserves to be said.

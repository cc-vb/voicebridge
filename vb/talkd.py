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
# PHONE opt-in registry (mirror of VOICED, default-DENY): the phone relay may
# ONLY list, switch to, and inject into sessions that appear here. A public
# tunnel guarded by a shared key must not be able to reach a session the user
# never chose to share. One file per enabled sid, content = transcript path.
PHONE = STATE / "phone"
LAST = STATE / "last_prompt.json"
ACTIVE = STATE / "active.json"
PID = STATE / "talkd.pid"
VER = STATE / "talkd.ver"   # code fingerprint of the RUNNING daemon

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
    x = max(core.MIN_SPEED,
            min(core.MAX_SPEED, absolute if absolute else cur + delta))
    core.RATE_FILE.write_text(str(int(round(175.0 * x))))
    # Read back what was stored, so the spoken confirmation matches the
    # speed actually in effect rather than the requested one.
    return f"{core.spoken_speed(int(core.get_rate()) / 175.0)} speed"


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
        return m if m in ("all", "wake", "speak") else "all"
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


def _first_sentence(text: str, cap: int = 200) -> str:
    """The first spoken sentence of a reply, for wake mode's short answers.
    Strips markdown first, then returns up to the first ., !, or ?; falls back
    to a hard cap so a run-on still stays about one line."""
    clean = core.clean_for_speech(text, max_chars=max(cap, 400))
    first = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)[0]
    return first[:cap].strip()


def _snapshot_wav(src: str, dst: str, min_s: float = 0.4) -> bool:
    """Write a valid WAV of the PCM captured in `src` so far (a growing 16k
    mono s16le sox recording), so we can transcribe WHILE recording, for early
    wake-word detection. Returns False if there isn't enough audio yet."""
    import wave
    try:
        with open(src, "rb") as f:
            f.seek(44)   # past the standard sox WAV header
            data = f.read()
        data = data[:len(data) - (len(data) % 2)]
        if len(data) < int(16000 * 2 * min_s):
            return False
        with wave.open(dst, "wb") as o:
            o.setnchannels(1)
            o.setsampwidth(2)
            o.setframerate(16000)
            o.writeframes(data)
        return True
    except Exception:
        return False


def wake_accept(text: str, why: str, winked: bool) -> bool:
    """In wake mode, whether to accept a capture the normal gate wanted to
    drop. Short wake phrases often score low on confidence/loudness, so if the
    capture is NOT noise/echo and either the wake word is present or we already
    dinged on it mid-recording (`winked`), take it. Genuine noise/echo (dropped
    upstream) never reaches here."""
    if why in ("noise", "echo", "assistant-echo", ""):
        return why == ""   # "" means it already passed; noise/echo stays dropped
    return winked or wake_match(text)[0]

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
    # NFKD first so accented Latin (café, résumé, naïve) decomposes to ASCII
    # base letters + combining marks; the marks aren't letters, so they don't
    # count. Without this, é/ï read as "foreign" and a real English prompt with
    # loanwords was silently dropped as background media.
    import unicodedata
    norm = unicodedata.normalize("NFKD", text)
    other = [c for c in _LETTER.findall(norm)
             if not ("a" <= c.lower() <= "z" or "ऀ" <= c <= "ॿ")]
    return len(other) >= 2


# ---------- state helpers (called from the hook and the CLI) -----------------

# ---------- session ownership -------------------------------------------------
#
# Voice must never outlive the session it was bound to. Closing a session with
# Ctrl+C, quitting the terminal or a crash leaves nobody to run /voice-off, and
# the marker files stay behind: the daemon kept the microphone open and kept
# reading replies aloud out of a transcript no one was watching any more.
#
# The fix is to know WHO owns the voiced session. Hooks run as descendants of
# the Claude Code process, so walking up the parent chain from the hook finds
# that process exactly, no guessing from a process list. Its pid is recorded
# with every prompt, which gives the daemon a one-syscall liveness test and a
# definite moment to shut everything down.

CALL_OWNER = core.STATE_DIR / "call_owner"   # session `vb phone` was run in
OWNERS = STATE / "owners"    # <sid> -> pid of the Claude Code process running it


def phone_enable(sid: str, path: str = "") -> None:
    """Opt a session into phone control. Stores its transcript path so the
    relay can resolve a target without trusting an arbitrary active.json."""
    if not sid:
        return
    try:
        PHONE.mkdir(parents=True, exist_ok=True)
        (PHONE / sid).write_text(path or core.newest_transcript() or "")
    except Exception:
        pass


def phone_disable(sid: str) -> None:
    try:
        (PHONE / sid).unlink()
    except OSError:
        pass


def phone_enabled() -> set:
    """The set of sids the phone is allowed to touch (default: empty = deny)."""
    try:
        return {f.name for f in PHONE.iterdir()}
    except Exception:
        return set()


def phone_is_enabled(sid: str) -> bool:
    return bool(sid) and (PHONE / sid).exists()


def phone_path(sid: str) -> str:
    """The stored transcript path for an enabled session, '' if none/stale."""
    try:
        return (PHONE / sid).read_text().strip()
    except Exception:
        return ""


def owner_pid() -> int:
    """The pid of the Claude Code process this code is running under, or 0.
    Only meaningful from inside a hook (a descendant of that process)."""
    pid = os.getpid()
    for _ in range(12):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True,
                                 timeout=3).stdout.strip()
        except Exception:
            return 0
        if not out:
            return 0
        parts = out.split(None, 1)
        comm = parts[1].strip() if len(parts) > 1 else ""
        if os.path.basename(comm) == "claude":
            return pid
        try:
            pid = int(parts[0])
        except ValueError:
            return 0
        if pid <= 1:
            return 0
    return 0


def _is_claude_pid(pid: int) -> bool:
    """Is this pid still a live Claude Code session? The name check is what
    makes it safe against pid reuse: an unrelated program that inherits the
    number must not read as 'your session is still open'."""
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return True   # can't tell -> assume alive; never tear down on a doubt
    return os.path.basename(out.strip()) == "claude"


def _pid_start(pid: int) -> str:
    """A stable per-process start marker (ps lstart), used to tell a recycled
    pid apart from the process that originally held it. '' if unknown."""
    if pid <= 0:
        return ""
    try:
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                              capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except Exception:
        return ""


def _owner_of(sid: str):
    """Parse OWNERS/sid into (pid, start_marker). Legacy records are pid-only
    (start=''), which callers treat as best-effort (no start comparison)."""
    try:
        raw = (OWNERS / sid).read_text().strip()
    except Exception:
        return (0, "")
    pid_s, _, start = raw.partition(":")
    try:
        return (int(pid_s), start)
    except ValueError:
        return (0, "")


def owner_live(sid: str, require_claude: bool = True) -> bool:
    """Is the process recorded for `sid` STILL that exact session? Requires the
    pid to be alive, its start marker to match what we recorded (this is what
    defeats pid reuse), and, unless disabled, to be a Claude process. Legacy
    pid-only records skip the start check. This is what makes phone targeting
    and the owner watchdog safe against a recycled pid owned by a DIFFERENT
    session, the cross-session isolation break the registries must prevent."""
    pid, start = _owner_of(sid)
    if pid <= 0:
        return False
    if require_claude:
        if not _is_claude_pid(pid):
            return False
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
    if start and _pid_start(pid) != start:
        return False
    return True


def live_claude_pids() -> set:
    """Every running Claude Code process, in one `ps`. Callers asking about a
    dozen sessions at a time (the phone roster, polled every few seconds) get
    one process listing instead of one per session."""
    pids = set()
    try:
        out = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return pids
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1].strip()) == "claude":
            try:
                pids.add(int(parts[0]))
            except ValueError:
                pass
    return pids


def _any_claude_running() -> bool:
    """Is ANY Claude Code session open on this machine? The fallback for
    sessions voiced before their owner was recorded: if there is no Claude at
    all, an open mic cannot belong to anything."""
    try:
        out = subprocess.run(["ps", "-axo", "comm="], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return True
    return any(os.path.basename(ln.strip()) == "claude"
               for ln in out.splitlines())


def _record_owner(sid: str, pid: int) -> None:
    """Remember which process runs a session. Written for EVERY session that
    submits a prompt, not just the voiced one, so the roster can say which
    sessions are still open instead of inferring it from process working
    directories (which cannot tell two sessions in one project apart)."""
    if not sid or pid <= 0:
        return
    try:
        OWNERS.mkdir(parents=True, exist_ok=True)
        # Store pid AND its start marker so a later reader can reject a recycled
        # pid that now belongs to a different process (pid-reuse safety).
        (OWNERS / sid).write_text(f"{pid}:{_pid_start(pid)}")
    except OSError as e:
        core.log(f"talkd._record_owner failed: {e}")


def tty_for_sid(sid: str) -> str:
    """The /dev tty of the terminal running session `sid`, via the OWNERS
    (sid -> Claude Code pid) registry, so a phone prompt targets THAT
    session's tab even when several sessions are open. '' if unknown/dead."""
    if not sid:
        return ""
    # Reuse-safe: only resolve a tty if the recorded pid is STILL this exact
    # Claude session. A stale OWNERS entry whose pid was recycled by a different
    # session returns "" here, so a phone prompt can never land in a session
    # that did not opt in.
    if not owner_live(sid):
        return ""
    pid, _ = _owner_of(sid)
    try:
        t = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=3).stdout.strip()
        return ("/dev/" + t) if t and t not in ("??", "?") else ""
    except Exception:
        return ""


def known_owners() -> dict:
    """{sid: pid} for every session we've seen, dropping records old enough to
    be noise (the roster only reaches back hours anyway)."""
    out = {}
    try:
        entries = list(OWNERS.iterdir())
    except OSError:
        return out
    cutoff = time.time() - 7 * 86400
    for f in entries:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                continue
            pid_s, _, _rest = f.read_text().strip().partition(":")
            out[f.name] = int(pid_s)
        except (OSError, ValueError):
            pass
    return out


def owner_sid() -> str:
    """The session id of the Claude Code process THIS code is running under,
    found by matching our owner pid against the OWNERS registry.

    A hook or a tool-launched `vb phone` runs as a DESCENDANT of the Claude
    process, so owner_pid() walks up to it and known_owners() maps it back to a
    sid. This is why it works where the LAST file does not: `vb phone` fired
    from the slash command runs in a subprocess with a stale/empty LAST, so it
    used to opt in nothing and the phone saw "no sessions". '' if unresolved.
    """
    pid = owner_pid()
    if pid <= 0:
        return ""
    for sid, opid in known_owners().items():
        if opid == pid:
            return sid
    return ""


def sid_alive(sid: str, live: set = None) -> bool:
    """Is this session still open? None when we've never seen it, so callers
    can fall back instead of declaring a session dead on no evidence."""
    pid, start = _owner_of(sid) if sid else (0, "")
    if not pid:
        return None
    livepids = live if live is not None else live_claude_pids()
    if pid not in livepids:
        return False
    # pid is live, but confirm it is still the SAME process (not a reused pid).
    if start and _pid_start(pid) != start:
        return False
    return True


def session_alive(payload: dict) -> bool:
    """Is the session described by an active.json/last_prompt.json payload
    still open?"""
    if not payload:
        return False
    try:
        pid = int(payload.get("owner_pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid:
        return _is_claude_pid(pid)
    return _any_claude_running()


def call_owner() -> str:
    """Which session started the phone link, if any."""
    try:
        return CALL_OWNER.read_text().strip()
    except Exception:
        return ""


def session_closed(sid: str = "", why: str = "session closed") -> str:
    """Tear voice down for a session that ENDED instead of turning voice off.

    Silences speech mid-sentence, drops the session's marker and, once no
    voiced session is left, stops the daemon so the microphone is released and
    the hotkeys come down. A phone link opened from that session goes with it:
    a public tunnel into this Mac must not survive the window that opened it.
    """
    if not sid:
        sid = (_read_json(ACTIVE) or {}).get("session_id", "")
    if sid:
        phone_disable(sid)   # a closed session can never be phone-controllable
        try:
            (OWNERS / sid).unlink()   # reap the ownership record (reuse-safe)
        except OSError:
            pass
    marker = (VOICED / sid) if sid else None
    was_voiced = bool(marker and marker.exists())
    done = []
    if was_voiced:
        core.hush()   # stop mid-sentence, before anything slower runs
        try:
            marker.unlink()
        except OSError:
            pass
        active = _read_json(ACTIVE)
        if active and active.get("session_id") == sid:
            try:
                ACTIVE.unlink()
            except FileNotFoundError:
                pass
        done.append("voice off")
    if sid and call_owner() == sid:
        try:
            from . import call
            call.off()
            CALL_OWNER.unlink()
        except Exception as e:
            core.log(f"session_closed: phone teardown failed: {e}")
        else:
            done.append("phone link closed")
    remaining = list(VOICED.iterdir()) if VOICED.exists() else []
    if was_voiced and not remaining:
        stop_daemon()   # daemon + recorder + skhd: the mic is actually freed
        done.append("mic released")
    if not done:
        return ""
    core.log(f"talkd: {sid[:8]} {why} -> {', '.join(done)}")
    return f"{sid[:8]} {why}: {', '.join(done)}."


def record_prompt(session_id: str, transcript_path: str) -> None:
    """Called by the UserPromptSubmit hook on every prompt, any session."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": session_id,
                   "transcript_path": transcript_path, "ts": time.time(),
                   # Recorded from the hook, where the parent chain is still
                   # reachable: this is what lets the daemon notice the
                   # session going away.
                   "owner_pid": owner_pid()}
        _record_owner(session_id, payload["owner_pid"])
        _atomic_write(LAST, json.dumps(payload))
        # If this session is voiced, the mic follows it (most recent wins).
        if (VOICED / session_id).exists():
            _atomic_write(ACTIVE, json.dumps(payload))
    except Exception as e:
        core.log(f"talkd.record_prompt failed: {e}")


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _atomic_write(path, text: str) -> None:
    """Write via a temp file + os.replace (atomic) so a concurrent reader never
    sees a half-written file. The daemon polls ACTIVE continuously; a torn read
    made json.loads fail and the daemon drop to 'no active session' for a tick."""
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


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
    # OUR user's skhd only: on a shared Mac, another account's skhd made this
    # check pass, so ours never started and every hotkey silently did nothing
    # (their instance reads THEIR config, not ours).
    if subprocess.run(["pgrep", "-U", str(os.getuid()), "-x", "skhd"],
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
    _atomic_write(ACTIVE, json.dumps(last))
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
    # A recorded PID can be recycled by an unrelated process; a bare kill(pid,0)
    # would then read "alive", and ensure_daemon skips starting a real daemon so
    # the mic never opens. Confirm the pid is actually one of OUR talkd runners.
    try:
        pid = int(PID.read_text().strip())
    except Exception:
        return False
    return pid in _daemon_pids()


def _code_version() -> str:
    """A cheap fingerprint (newest source mtime) of the daemon's own code.
    A long-lived daemon keeps running the code it loaded at start, so after a
    git pull / plugin update it still executes the OLD logic, that is how a
    fixed bug 'comes back'. Comparing this against the running daemon's stamp
    lets ensure_daemon restart a stale one so fixes actually take effect."""
    pkg = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(pkg)
    latest = 0.0
    for base, _dirs, files in os.walk(pkg):
        for f in files:
            if f.endswith(".py"):
                try:
                    latest = max(latest, os.path.getmtime(os.path.join(base, f)))
                except OSError:
                    pass
    try:
        latest = max(latest, os.path.getmtime(os.path.join(root, "bin", "vb")))
    except OSError:
        pass
    return f"{latest:.0f}"


def _kill_recorder() -> None:
    """Kill any sox recorder the daemon left behind. It's a plain child, so
    killing the daemon orphans it and it keeps HOLDING THE MIC until its own
    trim limit (up to 30s). Every stop path must call this or 'voice off' can
    leave the mic live."""
    for wav in (STATE / "talkd.wav", STATE / "talkd_cont.wav",
                STATE / "barge.wav"):
        subprocess.run(["pkill", "-U", str(os.getuid()), "-f", str(wav)],
                       capture_output=True)


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
    _kill_recorder()   # never leave an orphaned recorder holding the mic


def _start_daemon() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    vb = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin", "vb")
    p = subprocess.Popen([sys.executable, vb, "talkd", "__run__"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    PID.write_text(str(p.pid))
    VER.write_text(_code_version())   # stamp the code this daemon is running


def ensure_daemon() -> None:
    cur = _code_version()
    if daemon_alive():
        try:
            running = VER.read_text().strip()
        except Exception:
            running = ""
        if running == cur:
            # Current daemon runs current code; just clear any orphans.
            _kill_all_daemons(keep=int(PID.read_text().strip()))
            return
        # Stale daemon (started before a code update): restart so today's
        # fixes actually run instead of the code it loaded hours ago.
        core.log(f"talkd: code changed ({running} -> {cur}), restarting daemon")
    _kill_all_daemons()   # clear stale/orphan daemons AND the mic
    _start_daemon()


def _wipe_recordings() -> None:
    """Delete the scratch audio the moment listening stops.

    These files are recordings of the room. They were only swept at the next
    daemon start, and only once an hour old, so 'the mic is off' still left
    your voice sitting on disk. Nothing here is needed after a turn ends."""
    for name in ("talkd.wav", "talkd_cont.wav", "talkd_probe.wav",
                 "barge.wav"):
        try:
            (STATE / name).unlink()
        except OSError:
            pass


def stop_daemon() -> None:
    _kill_all_daemons()
    core.hush()   # a Kokoro reply plays via a detached worker; killing the
    #               daemon orphans it, so silence it or "mic released" lies.
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
    _wipe_recordings()   # the mic is off; the recordings of the room go too
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
    # The states people get silently stuck in ("it stopped talking and I
    # don't know why"): muted replies and a held (paused) reply.
    if core.replies_muted():
        lines.append("replies: MUTED (Ctrl+Option+M or `vb replies on` to unmute)")
    if core.speech_held():
        lines.append("reply  : PAUSED (Fn+F8 or `vb hold` to resume)")
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


def should_ding(mode: str, in_follow: bool, armed: bool) -> bool:
    """Whether to play the 'now listening' ding as we open the mic.

    ONE ding per turn, never a ding-per-cycle. The caller keeps an `armed`
    flag: it is True at startup and set True again ONLY after speech finishes
    (a reply or spoken response). We ding once when armed and actually
    listening, then the caller disarms. Idle re-records (no speech in between)
    stay armed=False, so they are silent, that is the fix for the
    ting-ting-ting. Wake mode's ambient listen never dings (it isn't 'all' and
    isn't in the post-wake follow window); the wake-word match dings instead.
    """
    return armed and (mode == "all" or in_follow)


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
#
# The bar scales with how much of the capture was our own voice, because the
# two failure modes pull in opposite directions. A flat low ceiling drops a
# genuine interruption that happens to reuse the reply's words ("no, use the
# OTHER branch"); a flat high one lets imperfect echo subtraction cut our own
# long replies off mid-sentence. So: the more it sounds like us, the more
# clearly-new words we demand before believing anyone is talking.
BARGE_MIN_WORDS = 3          # at low overlap, three new words is a person
BARGE_PARTIAL_OVERLAP = 0.60  # above this it is our reply coming back
BARGE_PARTIAL_WORDS = 5      # ...below it, five new words still count


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
    new_words = len(residue.split())
    # Barely any shared words: they are talking about something else entirely.
    if overlap < 0.25 and new_words >= BARGE_MIN_WORDS:
        return residue
    # Half our words came back, but there is a sentence of new ones underneath
    # -- someone interrupting with the reply's own vocabulary ("no, use the
    # OTHER branch"). Echo alone does not invent five substantive new words.
    if overlap < BARGE_PARTIAL_OVERLAP and new_words >= BARGE_PARTIAL_WORDS:
        return residue
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
        VER.write_text(_code_version())    # record the code this daemon runs
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
    wav_probe = str(STATE / "talkd_probe.wav")   # partial, for early wake ding
    prev: dict = {}
    announced: set = set()
    follow_until = 0.0   # wake mode: window after "hey Claude" alone
    queued = ""          # barge-in captured while a reply was speaking
    fleet_states = {}    # sid -> state, for idle alerts
    fleet_next = 0.0     # next fleet-check time
    first_fleet = True   # skip alerts on the very first scan (no baseline)
    unfocused_since = 0.0   # when the bound app lost focus (0.0 = focused)
    owner_next = 0.0        # next owner-liveness check (see the watchdog below)
    cue_armed = True   # ding once when listening (re)starts for a turn; armed
    #                    at boot and after each reply, never on idle re-records
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

        # Owner watchdog. The session voice belongs to has to still be open.
        # Ctrl+C, a closed terminal or a crash leaves nobody to run /voice-off,
        # and this loop would otherwise keep the mic and keep speaking replies
        # into an empty room. Checked every few seconds (one `ps`), and BEFORE
        # the focus gate below, since a closed terminal is never frontmost and
        # would park us in the dormant branch forever.
        if time.time() >= owner_next:
            owner_next = time.time() + 4.0
            if not session_alive(active):
                session_closed(sid, "session closed")
                core.log(f"talkd: exiting, {sid[:8]} is gone (pid {os.getpid()})")
                return 0

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
            # Never announce the session you're voiced in (sid): its reply is
            # already spoken, and announcing it fired "X is ready" every turn.
            fresh, fleet_states = _sess.newly_idle(fleet_states, exclude_sid=sid)
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
        # Mode drives everything below. "speak" = read replies aloud with the
        # mic OFF (you give prompts with Claude's own space-to-talk, or type);
        # "all"/"wake" open our mic. Fetched here so reply-speaking knows
        # whether to listen for a barge-in.
        mode = get_mode()

        pending = core.assistant_replies_after(tp, prev.get(tp, ""))
        if pending:
            if len(pending) > 1:
                core.log(f"talkd: {len(pending)} replies queued while listening")
            barge = ""
            muted = core.replies_muted()   # "talk to me, don't talk back"
            for uid, text in pending:
                prev[tp] = uid      # marked read before speaking: a crash mid
                if muted:
                    continue        # keep listening/injecting, just stay silent
                if mode == "speak":
                    core.speak(text, blocking=True)   # no mic, no barge-in
                else:
                    # Wake mode is quick Q&A: speak just the first sentence
                    # (the full reply stays on screen). Agent mode reads it all.
                    say_text = _first_sentence(text) if mode == "wake" else text
                    barge = _speak_interruptible(say_text)  # must not loop it
                    if barge:
                        queued = barge  # talked over the reply; that's the prompt
                        break
            time.sleep(0.3)
            cue_armed = True   # spoke a reply -> ding once when we next listen
            if not barge:
                continue

        # Speak-only mode stops here: nothing to listen for. Keep the daemon
        # alive (fleet alerts, HUD) and idle. The orb/status line shows
        # "reads replies" so you know it's on but not listening.
        if mode == "speak":
            core.set_hud("speakonly")
            time.sleep(0.4)
            continue

        # 2) Listen (or use a barge-in already captured during speech).
        pause = get_pause()
        in_follow = time.time() < follow_until
        first_more = ""   # continuation captured during the overlapped listen
        winked = False    # dinged early on the wake word (wake mode)
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
            # ONE ding as listening (re)starts, never per idle cycle. `armed`
            # is set at boot and after each reply (below), and cleared here.
            if should_ding(mode, in_follow, cue_armed):
                _cue(START_TINK)   # wake mode listens silently (ambient)
                time.sleep(0.15)   # brief, just so the cue isn't recorded
                cue_armed = False
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
            last_probe = 0.0     # throttle for the mid-recording wake probe
            try:
                last_tsize = os.path.getsize(tp)   # transcript size, to detect
            except OSError:                         # a new reply without a full
                last_tsize = -1                     # re-parse every poll

            # Only probe mid-recording when the warm STT server is up: the CLI
            # fallback takes ~2s and would block this loop (stalling the meter
            # and end-of-speech detection). Checked once per capture, not per
            # probe. Without the server, the wake ding just fires at end-of-turn.
            probe_wake = (mode == "wake" and not in_follow and stt.whisper_up())
            while p.poll() is None:
                time.sleep(0.15)
                # Live mic meter: once your voice registers, the indicator
                # flips to "hearing" and pulses with the level, so you can see
                # it's catching you (and know when it isn't).
                lvl = stt.live_level(wav)
                core.set_hud("hearing" if lvl > 0.12 else
                             ("wake" if (mode == "wake" and not in_follow)
                              else "listening"), level=lvl)
                # Wake mode: transcribe the partial recording every ~0.45s and
                # ding the INSTANT "hey Claude" is heard, while you keep
                # talking, instead of after the whole sentence is captured.
                if (probe_wake and not winked
                        and time.time() - last_probe > 0.30):
                    last_probe = time.time()
                    if _snapshot_wav(wav, wav_probe, min_s=0.3):
                        ptxt, _pc = stt.transcribe_ex(wav_probe)
                        if ptxt and wake_match(ptxt)[0]:
                            _cue_event(START_TINK)   # heard it, right away
                            winked = True
                now_active = _read_json(ACTIVE)
                switched = (not now_active
                            or now_active.get("session_id") != sid)
                # Only re-parse the transcript when the FILE actually grew.
                # Parsing the whole JSONL every 0.15s starved this loop on
                # long (multi-MB) sessions; a stat() is ~free and a reply can't
                # land without the file growing.
                newreply = False
                try:
                    tsize = os.path.getsize(tp)
                except OSError:
                    tsize = last_tsize
                if tsize != last_tsize:
                    last_tsize = tsize
                    newreply = bool(core.assistant_replies_after(
                        tp, prev.get(tp, "")))
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
            if cut:
                continue

            # 3) Handle speech: is this really the user talking to Claude?
            try:
                if os.path.getsize(wav) < 2000:
                    continue   # silence: no pop, no ting, stay quiet
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
            # In wake mode, don't let a low confidence/loudness score drop a
            # capture that's actually addressed to the wake word (short "hey
            # Claude" phrases score low); noise/echo are still dropped.
            if why and not (mode == "wake" and wake_accept(text, why, winked)):
                if text:
                    core.log(f"talkd dropped ({why}): {text[:80]}")
                    core.bump_stat("drops")   # signal for the recommender:
                    #                           stray audio -> suggest wake
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
            if not (addressed or winked):
                continue   # ambient chatter, drop silently
            if not winked:
                _cue_event(START_TINK)   # ding now if we didn't already do it
                #                          the instant we heard it mid-recording
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

"""voicebridge core: turn Claude Code hook events into spoken updates.

Design notes:
- Voice is OUTPUT here (say). STT input is a later milestone.
- Everything is opt-in via a flag file so it never surprises you in a
  session where you don't want it: ~/.voicebridge/enabled present = on.
- speak() is non-blocking: it detaches `say` so the hook returns instantly
  and never delays your next turn. A new utterance interrupts the old one,
  which is the seed of real barge-in later.
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

STATE_DIR = Path(os.path.expanduser("~/.voicebridge"))
ENABLED_FLAG = STATE_DIR / "enabled"
LOG_FILE = STATE_DIR / "log"
LAST_SPOKEN = STATE_DIR / "last_spoken"
WATCH_PID = STATE_DIR / "watch.pid"
LANG_FILE = STATE_DIR / "lang"
RATE_FILE = STATE_DIR / "rate"
VOICE_FILE = STATE_DIR / "voice"
MIC_FLAG = STATE_DIR / "mic_on"
REMAINDER = STATE_DIR / "speech_remainder"   # what the cap cut off, for `vb continue`
SPEECH_CHUNKS = STATE_DIR / "speech_chunks"  # current reply, split into chunks
SPEECH_POS = STATE_DIR / "speech_pos"        # index of the chunk playing now
REPLIES_OFF = STATE_DIR / "replies_off"      # speak nothing while set (one key)
PENDING_NOTICE = STATE_DIR / "pending_notice"   # Claude is waiting on you


def set_pending_notice(sid: str, message: str) -> None:
    """Record 'Claude is waiting for a decision' (from the Notification hook)
    so the phone relay can surface it and take a spoken yes/no."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PENDING_NOTICE.write_text(json.dumps(
            {"sid": sid, "message": message, "ts": time.time()}))
    except Exception:
        pass


def get_pending_notice(sid: str = "", max_age: float = 300.0) -> str:
    """The pending decision's message, '' if none/stale/for another session."""
    try:
        d = json.loads(PENDING_NOTICE.read_text())
        if time.time() - float(d.get("ts", 0)) > max_age:
            return ""
        if sid and d.get("sid") and d["sid"] != sid:
            return ""
        return (d.get("message") or "").strip()
    except Exception:
        return ""


def clear_pending_notice() -> None:
    try:
        PENDING_NOTICE.unlink()
    except Exception:
        pass


def replies_muted() -> bool:
    return REPLIES_OFF.exists()


def set_replies_muted(off: bool) -> bool:
    """Toggle/set 'don't speak replies' (mic stays on). Returns the new state."""
    try:
        if off:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            REPLIES_OFF.write_text("1")
        else:
            REPLIES_OFF.unlink()
    except Exception:
        pass
    return off


def speech_held() -> bool:
    """True if the current reply is PAUSED (SIGSTOP'd), so callers don't speak
    newer replies over a deliberate pause."""
    try:
        pid = int(SPEECH_PID.read_text().strip())
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip()
        return stat.startswith("T")
    except Exception:
        return False


def is_voiced(session_id: str) -> bool:
    """True only if THIS session was explicitly voiced with /voice-on.
    Speaking is gated on this so voice is strictly per-session (like Remote
    Control): a session you never voiced never speaks, no matter what global
    flags are set."""
    if not session_id:
        return False
    return (STATE_DIR / "talk" / "voiced" / session_id).exists()


def mic_active() -> bool:
    """True while another voicebridge component owns the session's voice
    (voicemode channel mic, or an active talkd session). Hooks and the
    watcher stay silent then, so nothing is spoken twice.

    MIC_FLAG only counts while a voicemode channel process is actually
    running, so a stale flag (e.g. a failed `vb session` launch) can never
    silence the hooks."""
    if (STATE_DIR / "talk" / "active.json").exists():
        return True
    if MIC_FLAG.exists():
        r = subprocess.run(["pgrep", "-f", "channel/voicemode.ts"],
                           capture_output=True)
        return r.returncode == 0
    return False

# Map a chosen language to the best-fitting macOS `say` voice. Latin-script
# languages (incl. Hinglish) sound best with the Indian-accent English voice;
# Devanagari Hindi needs the Hindi voice. Anything else falls back to default.
VOICE_MAP = {
    "hindi": "Lekha",     # hi_IN, reads Devanagari
    "hinglish": "Rishi",  # en_IN, natural for romanized Hindi + English
}

# Don't speak the same text twice within this window. This is what lets the
# transcript watcher and the Stop hook coexist without double-speaking.
DEDUP_WINDOW_S = 20.0

# Config: env wins, then a saved value (vb rate / vb voice), then default.
# ~175 wpm is a natural, human pace (a touch above macOS default).
DEFAULT_RATE = "175"
MAX_CHARS = int(os.environ.get("VOICEBRIDGE_MAXCHARS", "6000"))


def _read(path, default=""):
    try:
        return path.read_text().strip() or default
    except Exception:
        return default


def get_rate() -> str:
    return os.environ.get("VOICEBRIDGE_RATE") or _read(RATE_FILE) or DEFAULT_RATE


def get_voice() -> str:
    """Explicit voice (env or `vb voice`) wins; else the language's voice."""
    return (os.environ.get("VOICEBRIDGE_VOICE") or _read(VOICE_FILE)
            or voice_for_lang(get_lang()))


def is_enabled() -> bool:
    return ENABLED_FLAG.exists()


def get_lang() -> str:
    """The chosen response language (free-form), or '' for default English."""
    try:
        return LANG_FILE.read_text().strip()
    except Exception:
        return ""


def voice_for_lang(lang: str) -> str:
    """Pick a say voice for a language; '' means system default."""
    return VOICE_MAP.get(lang.strip().lower(), "")


def log(msg: str) -> None:
    """Best-effort debug log; never raises. Timestamped so latency can be
    read straight off the log (issue #2) instead of measured by hand."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        with open(LOG_FILE, "a") as f:
            f.write(f"{ts} {msg.rstrip()}\n")
    except Exception:
        pass


HUD_FILE = STATE_DIR / "hud.json"

# The phases the indicator (the orb / `vb orb`) shows. This is the honest
# answer to "is it listening right now?" so you never talk to a dead mic.
#   listening  mic open, waiting for you to start talking
#   hearing    actively capturing your voice (level > 0 -> the pulse)
#   thinking   transcribing / pasting into the session
#   speaking   a reply is playing out loud
#   wake       wake mode, ignoring the room until "hey Claude"
#   speakonly  voice-on: reads replies aloud, mic OFF (you type / space-to-talk)
#   away       you switched off the bound window, so it stopped listening
#   off        the daemon isn't running (inferred from a stale file)
HUD_PHASES = ("listening", "hearing", "thinking", "speaking", "wake",
              "speakonly", "away")


def set_hud(phase: str, level: float = 0.0, text: str = "") -> None:
    """Publish the daemon's live state + mic level for the indicator to read.
    Best-effort and cheap (atomic replace, never raises), so it's safe to call
    on the hot path of the capture loop."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "phase": phase,
            "level": round(max(0.0, min(1.0, level)), 3),
            "text": text[:80],
            "ts": time.time(),
        })
        tmp = str(HUD_FILE) + f".{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.write(payload)
        os.replace(tmp, HUD_FILE)
    except Exception:
        pass


_SL_LABEL = {
    "listening": "🎙 voice on", "hearing": "🎙 hearing you",
    "thinking": "✍ working", "speaking": "🔊 speaking",
    "wake": "💤 wake-word", "speakonly": "🔉 reads replies",
    "away": "⏸ paused", "off": "○ voice off",
}


def render_statusline(hud: dict, update_cmd: str = "") -> str:
    """The voicebridge SEGMENT of a status line: `vb <state>`, nothing else.

    This is appended to whatever status line you already had, so it has to
    earn every character. It carries state only , no rotating tips: a hint you
    have read twice is noise on someone else's line, and it competed with the
    git/branch/cost segments people actually put there.

    Silent when voice is off, which is most of the time. An empty string means
    "add nothing", so a status line with no voice running looks exactly like it
    did before voicebridge was installed. The one exception is a pending
    update, which is rare, actionable, and disappears once taken."""
    phase = hud.get("phase", "off")
    if phase not in _SL_LABEL or phase == "off":
        return ""
    seg = f"vb {_SL_LABEL[phase]}"
    return f"{seg}  ·  {update_cmd}" if update_cmd else seg


def read_hud() -> dict:
    """Current indicator state. Returns phase 'off' if the file is missing or
    stale (no update recently), i.e. the daemon isn't actually listening, so
    the indicator can't falsely claim a live mic."""
    try:
        d = json.loads(HUD_FILE.read_text())
        if time.time() - float(d.get("ts", 0)) > 5.0:
            return {"phase": "off", "level": 0.0, "text": "", "ts": 0}
        return d
    except Exception:
        return {"phase": "off", "level": 0.0, "text": "", "ts": 0}


_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_MD_TOKENS = re.compile(r"[#>*_~|]")
_LIST_BULLET = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
_WS = re.compile(r"\s+")


def clean_for_speech(text: str, max_chars: int = 0) -> str:
    """Strip markdown/code so speech sounds natural, then cap length.

    We drop fenced code entirely (never read code aloud) and flatten the
    rest to plain sentences. Length is capped at a sentence boundary so we
    never read a wall of text. max_chars=0 uses the configured default.
    """
    if not text:
        return ""
    head, rest = _cap_at_sentence(_strip_for_speech(text),
                                  max_chars or MAX_CHARS)
    return head if not rest else head + "..."


def _code_landmark(m) -> str:
    """Spoken landmark for a fenced code block, with its size, so the ear and
    the eye meet AT the code instead of drifting apart. Swallowing a 30-line
    block into "(code block)" (~0.5s of speech) was the #1 reason listeners
    lost their place: the voice raced past code still filling the screen."""
    block = m.group(0)
    lines = max(1, block.count("\n") - 1)   # minus the two fence lines-ish
    # language from the opening fence, if given (```python -> python)
    first = block.lstrip("`").split("\n", 1)[0].strip()
    lang = first if first.isalpha() and len(first) <= 12 else ""
    what = f"{lang} code" if lang else "code"
    if lines <= 1:
        return f" (a line of {what} on screen) "
    return f" ({what} on screen here, about {lines} lines, skipping it) "


def _strip_for_speech(text: str) -> str:
    """Markdown/code stripping, shared by the capped and stashing paths."""
    t = _FENCE.sub(_code_landmark, text)
    t = _LINK.sub(r"\1", t)
    t = _URL.sub(" a link ", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _LIST_BULLET.sub("", t)
    t = _MD_TOKENS.sub("", t)
    return _WS.sub(" ", t).strip()


def _cap_at_sentence(t: str, cap: int):
    """Split at the last sentence end before cap: returns (spoken, leftover)."""
    if len(t) <= cap:
        return t, ""
    cut = t[:cap]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if m > cap // 2:
        return cut[: m + 1], t[m + 1:].lstrip()
    return cut.rstrip(), t[len(cut):].lstrip()


CAP_NOTICE = " That's the character cap. Say continue to hear the rest."


def clean_and_stash(text: str, max_chars: int = 0) -> str:
    """Cap speech, but stash the leftover and SAY so, rather than trailing off.

    Silent truncation is indistinguishable from a finished answer, so the
    listener can't tell they missed anything. The leftover waits in
    REMAINDER for `vb continue` (or saying "continue") to pick it up.
    """
    if not text:
        return ""
    head, rest = _cap_at_sentence(_strip_for_speech(text),
                                  max_chars or MAX_CHARS)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if rest:
            REMAINDER.write_text(rest)
        elif REMAINDER.exists():
            REMAINDER.unlink()
    except Exception:
        pass
    return head + CAP_NOTICE if rest else head


def speak_remainder() -> bool:
    """Speak what the last cap cut off. False if nothing is pending.

    Chains: speak() re-stashes anything still over the cap, so repeated
    "continue" walks through a long answer a chunk at a time.
    """
    try:
        rest = REMAINDER.read_text().strip()
    except Exception:
        return False
    if not rest:
        return False
    try:
        REMAINDER.unlink()
    except Exception:
        pass
    speak(rest, blocking=True)
    return True


def _blocks_to_text(content) -> str:
    """A transcript message's content is a list of blocks; keep text ones."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p)


def last_assistant_text(transcript_path: str) -> str:
    """Return the text of the final assistant message in the transcript.

    Reads the JSONL from the end so we don't parse the whole file. Skips
    records that are only tool calls (no spoken text).
    """
    p = Path(transcript_path)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(errors="ignore").splitlines()
    except Exception as e:
        log(f"read transcript failed: {e}")
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message", {})
        text = _blocks_to_text(msg.get("content", ""))
        if text.strip():
            return text
    return ""


def print_qr(url: str) -> bool:
    """Print a QR a phone camera can actually read. False if it couldn't.

    Two settings decide whether the code scans at all, and both were wrong:

    border is the quiet zone, the blank margin a scanner needs to find the
    code's edges. The spec asks for 4 modules; at 1 the code sits flush
    against whatever else is in the terminal and many cameras never lock on.

    Error correction L rather than M keeps the module count down for a long
    tunnel URL, so each module is drawn bigger in a fixed-width terminal.
    Redundancy buys nothing here: the "print" is lossless, the failure mode
    is a camera that cannot resolve tiny modules.
    """
    try:
        import qrcode
    except Exception:
        return False
    try:
        qr = qrcode.QRCode(border=4,
                           error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(url)
        qr.make(fit=True)
        # Half blocks: terminal cells are about twice as tall as they are
        # wide, so two rows per character keeps the modules square.
        qr.print_ascii(invert=True)
        return True
    except Exception as e:
        log(f"print_qr failed: {e}")
        return False


def assistant_replies_after(transcript_path: str, after_uuid: str = ""):
    """Every assistant reply that lands after `after_uuid`, oldest first.

    last_assistant_text answers "what is the newest reply", which is the wrong
    question for a speaker that can only look between recordings: a reply that
    arrives while the mic is open is overwritten by the next one and never
    spoken at all. Replies are a queue, so hand back the whole queue and let
    the caller mark off what it has said.

    Returns a list of (uuid, text). An unknown after_uuid means the transcript
    was compacted or replaced under us; the backlog is unknowable then, so
    only the newest reply is returned rather than re-speaking the session.
    """
    p = Path(transcript_path)
    if not p.exists():
        return []
    try:
        lines = p.read_text(errors="ignore").splitlines()
    except Exception as e:
        log(f"read transcript failed: {e}")
        return []
    replies = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant":
            continue
        text = _blocks_to_text(rec.get("message", {}).get("content", ""))
        if text.strip():
            replies.append((rec.get("uuid") or "", text))
    if not after_uuid:
        return replies
    for i, (uid, _) in enumerate(replies):
        if uid == after_uuid:
            return replies[i + 1:]
    return replies[-1:]


def latest_assistant_uuid(transcript_path: str) -> str:
    """The uuid of the newest reply, for marking a session read on join."""
    replies = assistant_replies_after(transcript_path)
    return replies[-1][0] if replies else ""


def _recently_spoken(cleaned: str) -> bool:
    """True if this exact text was spoken within the dedup window.

    Shared across processes via a state file, so whichever of the watcher
    or the Stop hook gets there first wins and the other stays quiet.
    """
    h = hashlib.sha1(cleaned.encode("utf-8", "ignore")).hexdigest()
    now = time.time()
    try:
        prev_h, prev_t = LAST_SPOKEN.read_text().split("\n")[:2]
        if prev_h == h and (now - float(prev_t)) < DEDUP_WINDOW_S:
            return True
    except Exception:
        pass
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_SPOKEN.write_text(f"{h}\n{now}\n")
    except Exception:
        pass
    return False


ENGINE_FILE = STATE_DIR / "engine"     # "say" (default) | "kokoro"
SPEECH_PID = STATE_DIR / "speech.pid"  # pid of the current speech player
KOKORO_PORT = int(os.environ.get("VB_TTS_PORT", "8798"))
_KOKORO_VOICE_RE = re.compile(r"^[a-z]{2}_[a-z]+$")
# Kokoro refuses outside 0.5-2.0 ("Speed should be between 0.5 and 2.0")
# and returns no audio at all, so that is the ceiling for synthesis. Past
# it we re-time the finished WAV instead, which is why MAX_SPEED can be
# higher than KOKORO_MAX_SPEED. Everything else reads these constants.
MIN_SPEED = 0.5
MAX_SPEED = 3.5
KOKORO_MAX_SPEED = 2.0


def get_engine() -> str:
    try:
        e = ENGINE_FILE.read_text().strip()
        return e if e in ("say", "kokoro") else "say"
    except Exception:
        return "say"


def split_speech_chunks(text: str, first_max: int = 60,
                        chunk_max: int = 220) -> list:
    """Chunk a reply for fast-starting, gap-free streaming playback.

    Caps RAMP up: a tiny first chunk (measured: ~33 chars synthesizes in
    ~0.75s vs ~2.1s for a 120-char one, so the voice starts ~1.3s sooner), a
    medium second chunk, then full-size pieces. The ramp matters: each chunk
    must finish synthesizing while the previous one is still PLAYING, and a
    tiny first chunk only buys ~2s of playtime, not enough to hide a 220-char
    synth, so the second chunk stays small too. Sentences longer than a cap
    are split on commas/spaces so a single long sentence can't stall it."""
    # Each step sized so its synth fits inside the PREVIOUS chunk's playtime
    # (~15.5 chars/s speech, ~1s synth per 50 chars): 90 synths in ~1.7s
    # under 60's ~3.9s of play; 150 in ~2.7s under 90's ~5.8s; 220 always fits.
    caps = [first_max, min(90, chunk_max), min(150, chunk_max), chunk_max]
    sents = re.split(r"(?<=[.!?])\s+", text.strip())
    pieces = []
    for s in sents:
        s = s.strip()
        while len(s) > chunk_max:
            cut = s.rfind(", ", 0, chunk_max)
            if cut < chunk_max // 2:
                cut = s.rfind(" ", 0, chunk_max)
            if cut <= 0:
                cut = chunk_max
            pieces.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        if s:
            pieces.append(s)
    if not pieces:
        return [text]
    chunks, cur = [], ""
    cap = caps[0]
    for p in pieces:
        if cur and len(cur) + len(p) + 1 > cap:
            chunks.append(cur)
            cur = p
            cap = caps[min(len(chunks), len(caps) - 1)]
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    # A single long first SENTENCE dodges the ramp (pieces are only split at
    # chunk_max), leaving a big, slow first synth. Force the first chunk small
    # by breaking it at a comma/space near first_max.
    if chunks and len(chunks[0]) > first_max + 30:
        head = chunks[0]
        cut = head.rfind(", ", 0, first_max + 30)
        if cut < first_max // 2:
            cut = head.rfind(" ", 0, first_max + 15)
        if cut > 0:
            chunks[0:1] = [head[:cut + 1].strip(), head[cut + 1:].strip()]
    return chunks


def _retime_wav(wav: str, factor: float) -> str:
    """Play a finished WAV `factor` times faster, keeping pitch (atempo).

    atempo only accepts 0.5-2.0 per pass, so chain passes to reach beyond
    that in either direction. ffmpeg is already an install dependency; if
    it is missing or fails we return the un-retimed file, so speech plays
    at the wrong speed rather than not at all."""
    chain, left = [], factor
    while left > 2.0:
        chain.append("atempo=2.0")
        left /= 2.0
    while left < 0.5:
        chain.append("atempo=0.5")
        left /= 0.5
    chain.append(f"atempo={left:.4f}")
    fast = wav + ".fast.wav"
    try:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav,
                            "-filter:a", ",".join(chain), fast],
                           capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.getsize(fast) > 1000:
            os.replace(fast, wav)
        else:
            log(f"atempo {factor:g}x failed: {r.stderr.decode()[:200]}")
    except Exception as e:
        log(f"atempo unavailable, speaking un-retimed: {e}")
    return wav


def _kokoro_wav(text: str, out: str = "") -> str:
    """Synthesize via the local Kokoro server; '' if unavailable."""
    wav = out or str(STATE_DIR / "speech.wav")
    voice = get_voice()
    if not _KOKORO_VOICE_RE.match(voice or ""):
        voice = "af_heart"
    try:
        want = max(MIN_SPEED, min(MAX_SPEED, float(get_rate()) / 175.0))
    except ValueError:
        want = 1.0
    # Synthesize at Kokoro's fastest, then re-time the rest of the way.
    speed = min(want, KOKORO_MAX_SPEED)
    retime = want / speed
    payload = json.dumps({"text": text, "voice": voice, "speed": speed})
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "60", "-X", "POST",
             "-H", "Content-Type: application/json", "-d", payload,
             f"http://127.0.0.1:{KOKORO_PORT}/synth", "-o", wav],
            capture_output=True, timeout=70)
        if r.returncode == 0 and os.path.getsize(wav) > 1000:
            with open(wav, "rb") as f:
                if f.read(4) == b"RIFF":
                    return _retime_wav(wav, retime) if retime > 1.01 else wav
    except Exception as e:
        log(f"kokoro synth failed: {e}")
    return ""


def speech_active() -> bool:
    """True while a reply is actually playing through the speakers."""
    try:
        os.kill(int(SPEECH_PID.read_text().strip()), 0)
        return True
    except Exception:
        return False


def spoken_speed(x: float) -> str:
    """A speed a TTS voice can say cleanly.

    "1.00"/"1.25" get read out as "one point zero zero" / "one point two
    five", which is a mouthful for a confirmation you hear constantly.
    Percent has no decimal point to trip over: "one hundred percent"."""
    return f"{int(round(x * 100))} percent"


def cue_tick() -> None:
    """A short tick, for confirming something WITHOUT cutting off speech.

    speak() hushes whatever is playing, so using it to confirm a speed
    change mid-reply would kill the reply you were adjusting."""
    try:
        subprocess.Popen(["afplay", "/System/Library/Sounds/Tink.aiff"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


def confirm_speed(x: float) -> None:
    """Confirm a new speed: spoken when idle, a tick when already talking."""
    if speech_active():
        cue_tick()
    else:
        speak(f"{spoken_speed(x)} speed.")


def hold() -> str:
    """Freeze the current reply, or resume a frozen one. One key does both.

    Unlike hush(), which kills the speech outright, SIGSTOP leaves the
    player where it is so SIGCONT picks the sentence up mid-word. The
    process group is stopped as a whole, so the chunked speaker cannot
    move on to the next chunk while paused either.

    Which way to go is read from the process itself ("T" = stopped) rather
    than a flag file, so it cannot get out of step with reality."""
    try:
        pid = int(SPEECH_PID.read_text().strip())
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip()
        if not stat:
            return "nothing is speaking"
        paused = stat.startswith("T")
        os.killpg(pid, signal.SIGCONT if paused else signal.SIGSTOP)
        return "resumed" if paused else "held"
    except Exception:
        # Speech finished between the read and the signal: nothing to do.
        return "nothing is speaking"


def rewind_speech(delta: int) -> str:
    """Re-speak from the sentence `delta` away from the one playing now
    (delta -1 = back one sentence, +1 = skip forward). The worker publishes
    the chunk list and its position, so this hushes and re-speaks from the
    target chunk onward, a small gap, but it's the sentence-granular recovery
    that works in speak-only mode where there's no mic to say "go back"."""
    try:
        chunks = json.loads(SPEECH_CHUNKS.read_text())
        pos = int(SPEECH_POS.read_text().strip())
    except Exception:
        return "nothing is speaking"
    if not chunks:
        return "nothing is speaking"
    if delta > 0 and pos >= len(chunks) - 1:
        # Skipping off the end just ends this reply; the daemon then speaks the
        # next queued reply, so repeated skip walks you forward to the latest.
        hush()
        return "skipped to the end"
    target = max(0, min(len(chunks) - 1, pos + delta))
    hush()
    speak(" ".join(chunks[target:]))
    return "back a sentence" if delta < 0 else "skipped ahead"


def hush() -> None:
    """Stop whatever speech is playing (any engine), including the chunked
    speaker's afplay children (killed via the process group)."""
    subprocess.run(["pkill", "-x", "say"], capture_output=True)
    try:
        pid = int(SPEECH_PID.read_text().strip())
        try:
            os.killpg(pid, 15)   # wrapper started its own session: pgid == pid
        except Exception:
            os.kill(pid, 15)
    except Exception:
        pass


def kokoro_up() -> bool:
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "2",
             f"http://127.0.0.1:{KOKORO_PORT}/health"],
            capture_output=True, timeout=5)
        return r.stdout == b"ok"
    except Exception:
        return False


def ensure_kokoro_server(wait_s: float = 10.0) -> bool:
    """Start the Kokoro server if it isn't running; wait for readiness.
    Keeps the voice consistent instead of silently falling back to say."""
    if kokoro_up():
        return True
    venv_py = STATE_DIR / "kokoro-venv" / "bin" / "python"
    server = Path(__file__).resolve().parent.parent / "scripts" / \
        "kokoro_server.py"
    if not venv_py.exists() or not server.exists():
        return False
    try:
        p = subprocess.Popen([str(venv_py), str(server)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
        (STATE_DIR / "tts.pid").write_text(str(p.pid))
    except Exception as e:
        log(f"kokoro autostart failed: {e}")
        return False
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if kokoro_up():
            log("kokoro server autostarted")
            return True
        time.sleep(0.5)
    return False


def speak_chunks_blocking(text: str) -> None:
    """The chunked-speech worker (runs inside `vb __speak__`).

    Kokoro path: synthesize sentence chunks, PLAY chunk N while chunk N+1
    synthesizes, so the first words come out after one small synth instead
    of after the whole reply. Falls back to `say` when the server is down."""
    set_hud("speaking", text=text)   # indicator: a reply is playing aloud
    # Kokoro's voices are English-only. Detect from the TEXT itself: any
    # Devanagari means this reply needs the macOS Hindi voice, regardless
    # of settings, so auto-detected Hindi conversations just work.
    lang = get_lang().lower()
    is_hindi_text = bool(re.search(r"[ऀ-ॿ]", text))
    kokoro_ok = (get_engine() == "kokoro" and not is_hindi_text
                 and lang not in ("hindi", "हिंदी"))
    if kokoro_ok and not ensure_kokoro_server():
        log("speak fell back to say: kokoro server unavailable")
        kokoro_ok = False
    if kokoro_ok:
        chunks = split_speech_chunks(text)
        # Publish the chunk list so `vb back`/`vb skip` can re-speak from a
        # neighbouring sentence (sentence-level rewind for speak-only mode).
        try:
            SPEECH_CHUNKS.write_text(json.dumps(chunks))
        except Exception:
            pass
        slots = [str(STATE_DIR / f"speech-{i}.wav") for i in (0, 1)]
        cur = _kokoro_wav(chunks[0], out=slots[0]) \
            or _kokoro_wav(chunks[0], out=slots[0])   # one retry
        if not cur:
            log("speak fell back to say: chunk synth failed")
        if cur:
            i = 0
            while i < len(chunks):
                try:
                    SPEECH_POS.write_text(str(i))   # which sentence is playing
                except Exception:
                    pass
                player = subprocess.Popen(
                    ["afplay", cur],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                nxt, nxt_rate = "", ""
                if i + 1 < len(chunks):
                    nxt_rate = get_rate()
                    nxt = _kokoro_wav(chunks[i + 1],
                                      out=slots[(i + 1) % 2])
                player.wait()
                # The next chunk was rendered before this one finished, so a
                # speed change during playback would not be heard for two
                # sentences. Re-time the pending chunk instead of waiting for
                # it to age out: pressing faster/slower now lands on the very
                # next sentence.
                if nxt and nxt_rate and get_rate() != nxt_rate:
                    try:
                        ratio = float(get_rate()) / float(nxt_rate)
                        if abs(ratio - 1.0) > 0.01:
                            _retime_wav(nxt, ratio)
                    except (ValueError, ZeroDivisionError):
                        pass
                if i + 1 < len(chunks) and not nxt:
                    # A later chunk failed to synthesize , DON'T go silent.
                    # Speak the rest with `say` so the whole reply is heard.
                    rest = " ".join(chunks[i + 1:])
                    log("kokoro chunk failed mid-reply; say-fallback for rest")
                    v = get_voice()
                    if _KOKORO_VOICE_RE.match(v or ""):
                        v = ""
                    sc = ["say", "-r", get_rate()] + (["-v", v] if v else [])
                    subprocess.run(sc + [rest], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                    return
                cur = nxt
                i += 1
            return
    voice = get_voice()
    if _KOKORO_VOICE_RE.match(voice or ""):
        # A kokoro voice name means nothing to say; use the language's
        # natural macOS voice instead (e.g. Lekha for Hindi).
        voice = "Lekha" if is_hindi_text else voice_for_lang(get_lang())
    cmd = ["say", "-r", get_rate()]
    if voice:
        cmd += ["-v", voice]
    cmd.append(text)
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_speech(text: str):
    """Start speaking cleaned text; returns the speaker Popen (or None).

    Spawns the chunked-speech worker in its own process group so hush()
    and terminate take the audio down with it. Every utterance is recorded
    for the echo guard first."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "last_spoken_text").write_text(
            json.dumps({"text": text, "ts": time.time()}))
        # Rolling history: echoes of OLDER speech can still be in the air
        # when a newer speak overwrites last_spoken_text, so echo checks
        # must remember everything recent, not just the latest utterance.
        hist = STATE_DIR / "spoken_history"
        now = time.time()
        lines = []
        try:
            for ln in hist.read_text().splitlines():
                try:
                    if now - json.loads(ln)["ts"] < 180:
                        lines.append(ln)
                except Exception:
                    pass
        except FileNotFoundError:
            pass
        lines.append(json.dumps({"text": text, "ts": now}))
        hist.write_text("\n".join(lines[-20:]) + "\n")
        (STATE_DIR / "speech.txt").write_text(text)
    except Exception:
        return None
    vb = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin", "vb")
    log(f"start_speech by pid {os.getpid()} engine={get_engine()}: "
        f"{text[:60]!r}")
    try:
        import sys as _sys
        p = subprocess.Popen(
            [_sys.executable, vb, "__speak__"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception as e:
        log(f"start_speech failed: {e}")
        return None
    try:
        SPEECH_PID.write_text(str(p.pid))
    except Exception:
        pass
    return p


def speak(text: str, blocking: bool = False) -> None:
    """Speak text. Non-blocking by default (hooks); blocking for converse mode
    so we finish talking before we listen again. New speech cuts off old.

    Blocking speech (a live conversation reply) reads the WHOLE reply, not a
    capped preview: cutting off mid-answer breaks the conversation."""
    text = clean_and_stash(text, max_chars=6000 if blocking else 0)
    if not text:
        return
    if _recently_spoken(text):
        return
    hush()
    p = start_speech(text)
    if p is not None and blocking:
        try:
            p.wait()
        except Exception:
            pass


def newest_transcript() -> str:
    """Path to the most recently modified Claude Code transcript.

    While a session is live its file is the one being updated, so newest =
    current session. Returns "" if none found.
    """
    root = Path(os.path.expanduser("~/.claude/projects"))
    best, best_mtime = "", -1.0
    try:
        for f in root.glob("*/*.jsonl"):
            m = f.stat().st_mtime
            if m > best_mtime:
                best, best_mtime = str(f), m
    except Exception as e:
        log(f"newest_transcript failed: {e}")
    return best


def _speakable_from_record(rec: dict, narrate: bool) -> str:
    """Extract something worth saying from one transcript record, or ""."""
    if rec.get("type") != "assistant":
        return ""
    msg = rec.get("message", {})
    content = msg.get("content", "")
    text = _blocks_to_text(content)
    if text.strip():
        return text
    if narrate and isinstance(content, list):
        # Text-free turn = pure tool call. Optionally narrate it briefly.
        tools = [b.get("name") for b in content
                 if isinstance(b, dict) and b.get("type") == "tool_use"]
        tools = [t for t in tools if t]
        if tools:
            return "Running " + ", ".join(tools) + "."
    return ""


def watch_transcript(path: str = "", narrate: bool = False,
                     poll: float = 0.4) -> None:
    """Tail a transcript and speak new assistant messages as they appear.

    Starts at end-of-file so it never replays the backlog. This is what
    makes voice work in an already-running session without a restart, since
    it doesn't depend on hooks being reloaded.
    """
    path = path or newest_transcript()
    if not path:
        log("watch: no transcript found")
        return
    log(f"watch: started on {path}")
    try:
        f = open(path, "r", errors="ignore")
    except Exception as e:
        log(f"watch: open failed: {e}")
        return
    f.seek(0, os.SEEK_END)  # only new messages from here on
    buf = ""
    try:
        while True:
            if not is_enabled():
                # Honor the master switch even while the daemon runs.
                time.sleep(poll)
                continue
            chunk = f.readline()
            if not chunk:
                time.sleep(poll)
                continue
            buf += chunk
            if not buf.endswith("\n"):
                continue  # wait for the rest of a partial line
            line, buf = buf.strip(), ""
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            say_text = _speakable_from_record(rec, narrate)
            if say_text and not mic_active():
                # voicemode's reply tool speaks for the session; stay quiet.
                speak(say_text)
    except KeyboardInterrupt:
        pass
    finally:
        f.close()


def watcher_alive() -> bool:
    try:
        pid = int(WATCH_PID.read_text().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def read_hook_input() -> dict:
    """Parse the JSON that Claude Code sends a hook on stdin."""
    import sys
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(f"stdin parse failed: {e}")
        return {}

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
    """Best-effort debug log; never raises."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass


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


def _strip_for_speech(text: str) -> str:
    """Markdown/code stripping, shared by the capped and stashing paths."""
    t = _FENCE.sub(" (code block) ", text)
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


def get_engine() -> str:
    try:
        e = ENGINE_FILE.read_text().strip()
        return e if e in ("say", "kokoro") else "say"
    except Exception:
        return "say"


def split_speech_chunks(text: str, first_max: int = 120,
                        chunk_max: int = 220) -> list:
    """Chunk a reply for smooth, gap-free streaming playback:
    - a SMALL first chunk so the voice starts fast (~0.8s);
    - then several ~chunk_max pieces at sentence boundaries.
    Each piece synthesizes in ~1-2s while the previous (much longer) piece
    is still playing, so the pipeline never runs dry, no mid-reply gap, and
    nothing is one giant synth that lags. Sentences longer than chunk_max are
    split on commas/spaces so a single long sentence can't stall it."""
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
    chunks, cur, cap = [], "", first_max
    for p in pieces:
        if cur and len(cur) + len(p) + 1 > cap:
            chunks.append(cur)
            cur, cap = p, chunk_max
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _kokoro_wav(text: str, out: str = "") -> str:
    """Synthesize via the local Kokoro server; '' if unavailable."""
    wav = out or str(STATE_DIR / "speech.wav")
    voice = get_voice()
    if not _KOKORO_VOICE_RE.match(voice or ""):
        voice = "af_heart"
    try:
        speed = max(0.6, min(1.6, float(get_rate()) / 175.0))
    except ValueError:
        speed = 1.0
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
                    return wav
    except Exception as e:
        log(f"kokoro synth failed: {e}")
    return ""


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
        slots = [str(STATE_DIR / f"speech-{i}.wav") for i in (0, 1)]
        cur = _kokoro_wav(chunks[0], out=slots[0]) \
            or _kokoro_wav(chunks[0], out=slots[0])   # one retry
        if not cur:
            log("speak fell back to say: chunk synth failed")
        if cur:
            i = 0
            while i < len(chunks):
                player = subprocess.Popen(
                    ["afplay", cur],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                nxt = ""
                if i + 1 < len(chunks):
                    nxt = _kokoro_wav(chunks[i + 1],
                                      out=slots[(i + 1) % 2])
                player.wait()
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

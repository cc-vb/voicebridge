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


def mic_active() -> bool:
    """True while the voicemode channel owns the conversation's voice."""
    return MIC_FLAG.exists()

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
MAX_CHARS = int(os.environ.get("VOICEBRIDGE_MAXCHARS", "700"))


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


def clean_for_speech(text: str) -> str:
    """Strip markdown/code so speech sounds natural, then cap length.

    We drop fenced code entirely (never read code aloud) and flatten the
    rest to plain sentences. Length is capped at a sentence boundary so we
    never read a wall of text.
    """
    if not text:
        return ""
    t = _FENCE.sub(" (code block) ", text)
    t = _LINK.sub(r"\1", t)
    t = _URL.sub(" a link ", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _LIST_BULLET.sub("", t)
    t = _MD_TOKENS.sub("", t)
    t = _WS.sub(" ", t).strip()
    if len(t) <= MAX_CHARS:
        return t
    # Cut at the last sentence end before the cap so it doesn't trail off.
    cut = t[:MAX_CHARS]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if m > MAX_CHARS // 2:
        return cut[: m + 1]
    return cut.rstrip() + "..."


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


def speak(text: str, blocking: bool = False) -> None:
    """Speak text. Non-blocking by default (hooks); blocking for converse mode
    so we finish talking before we listen again. New speech cuts off old."""
    text = clean_for_speech(text)
    if not text:
        return
    if _recently_spoken(text):
        return
    try:
        # Interrupt whatever is currently being said (crude barge-in).
        subprocess.run(["pkill", "-x", "say"], capture_output=True)
    except Exception:
        pass
    voice = get_voice()
    cmd = ["say", "-r", get_rate()]
    if voice:
        cmd += ["-v", voice]
    cmd.append(text)
    try:
        if blocking:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # survive the hook process exiting
            )
    except Exception as e:
        log(f"say failed: {e}")


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

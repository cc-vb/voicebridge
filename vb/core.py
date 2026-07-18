"""voicebridge core: turn Claude Code hook events into spoken updates.

Design notes:
- Voice is OUTPUT here (say). STT input is a later milestone.
- Everything is opt-in via a flag file so it never surprises you in a
  session where you don't want it: ~/.voicebridge/enabled present = on.
- speak() is non-blocking: it detaches `say` so the hook returns instantly
  and never delays your next turn. A new utterance interrupts the old one,
  which is the seed of real barge-in later.
"""

import json
import os
import re
import subprocess
from pathlib import Path

STATE_DIR = Path(os.path.expanduser("~/.voicebridge"))
ENABLED_FLAG = STATE_DIR / "enabled"
LOG_FILE = STATE_DIR / "log"

# Config via env, with sane defaults. Override in your shell profile.
VOICE = os.environ.get("VOICEBRIDGE_VOICE", "")           # "" = system default
RATE = os.environ.get("VOICEBRIDGE_RATE", "200")          # words per minute
MAX_CHARS = int(os.environ.get("VOICEBRIDGE_MAXCHARS", "700"))


def is_enabled() -> bool:
    return ENABLED_FLAG.exists()


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


def speak(text: str) -> None:
    """Speak text without blocking the hook. New speech cuts off old speech."""
    text = clean_for_speech(text)
    if not text:
        return
    try:
        # Interrupt whatever is currently being said (crude barge-in).
        subprocess.run(["pkill", "-x", "say"], capture_output=True)
    except Exception:
        pass
    cmd = ["say", "-r", RATE]
    if VOICE:
        cmd += ["-v", VOICE]
    cmd.append(text)
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # survive the hook process exiting
        )
    except Exception as e:
        log(f"say failed: {e}")


def read_hook_input() -> dict:
    """Parse the JSON that Claude Code sends a hook on stdin."""
    import sys
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(f"stdin parse failed: {e}")
        return {}

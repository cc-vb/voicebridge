"""voicebridge STT: local, private speech-to-text via whisper.cpp.

Nothing about your code leaves the machine. Recording uses sox (`rec`)
with silence auto-stop, so you press to talk, speak, and it ends itself.
"""

import os
import shutil
import subprocess
from pathlib import Path

from . import core

MODEL_DIR = core.STATE_DIR / "models"
# Prefer the more accurate model when present; fall back to base.
_MODEL_PREFERENCE = ["ggml-small.en.bin", "ggml-base.en.bin"]
MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/"
    "resolve/main/ggml-base.en.bin"
)


def _best_model():
    for name in _MODEL_PREFERENCE:
        p = MODEL_DIR / name
        if p.exists():
            return p
    return MODEL_DIR / "ggml-base.en.bin"


MODEL = _best_model()


# Hotkey daemons (skhd) run with a minimal PATH that often lacks Homebrew,
# so we look in the usual brew locations too, not just PATH.
_BREW_BINS = ("/opt/homebrew/bin", "/usr/local/bin")


def _find(name: str) -> str:
    p = shutil.which(name)
    if p:
        return p
    for d in _BREW_BINS:
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return ""


def whisper_bin() -> str:
    for name in ("whisper-cli", "whisper-cpp", "main"):
        p = _find(name)
        if p:
            return p
    return ""


def have_deps() -> dict:
    return {
        "whisper": whisper_bin(),
        "rec": _find("rec"),
        "model": str(MODEL) if MODEL.exists() else "",
    }


def ensure_model() -> bool:
    if MODEL.exists():
        return True
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    core.log(f"downloading model to {MODEL}")
    try:
        subprocess.run(
            ["curl", "-fSL", "-o", str(MODEL), MODEL_URL],
            check=True,
        )
        return MODEL.exists()
    except Exception as e:
        core.log(f"model download failed: {e}")
        return False


def record(wav: str, max_secs: int = 30,
           silence_stop: float = 2.0) -> bool:
    """Record mic to a 16kHz mono wav, stopping after silence.

    Waits for you to start speaking, then ends after `silence_stop`
    seconds of quiet. Whisper wants 16kHz mono, so we record that directly.
    """
    rec = _find("rec")
    if not rec:
        core.log("record: sox `rec` not found")
        return False
    # Start on the very first faint sound (0.05s @ 0.3%) so the opening word
    # isn't eaten by speech detection; stop after `silence_stop` of quiet.
    # `norm` levels it up so you don't have to be loud.
    cmd = [
        rec, "-q", "-c", "1", "-r", "16000", "-b", "16", wav,
        "trim", "0", str(max_secs),
        "silence", "1", "0.05", "0.3%", "1", str(silence_stop), "1.5%",
        "norm", "-1",
    ]
    try:
        subprocess.run(cmd, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return os.path.exists(wav) and os.path.getsize(wav) > 1000
    except Exception as e:
        core.log(f"record failed: {e}")
        return False


def transcribe(wav: str) -> str:
    """Run whisper.cpp on a wav and return cleaned text."""
    wb = whisper_bin()
    if not wb or not MODEL.exists():
        core.log("transcribe: missing whisper binary or model")
        return ""
    cmd = [wb, "-m", str(MODEL), "-f", wav, "-nt", "-np", "-l", "en"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        core.log(f"transcribe failed: {e}")
        return ""
    text = (out.stdout or "").strip()
    # whisper.cpp sometimes emits bracketed non-speech tags; drop them.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned = " ".join(lines)
    for tag in ("[BLANK_AUDIO]", "(blank audio)", "[ Silence ]", "[silence]"):
        cleaned = cleaned.replace(tag, "")
    return cleaned.strip()

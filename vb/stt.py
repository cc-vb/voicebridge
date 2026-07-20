"""voicebridge STT: local, private speech-to-text via whisper.cpp.

Nothing about your code leaves the machine. Recording uses sox (`rec`)
with silence auto-stop, so you press to talk, speak, and it ends itself.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import core

MODEL_DIR = core.STATE_DIR / "models"
# English-only models are more accurate for English; the multilingual model
# handles Hindi/Hinglish and everything else.
_EN_MODELS = ["ggml-small.en.bin", "ggml-base.en.bin"]
_MULTI_MODELS = ["ggml-small.bin", "ggml-base.bin"]
MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/"
    "resolve/main/ggml-base.en.bin"
)


def _best_model(names=None):
    for name in (names or _EN_MODELS):
        p = MODEL_DIR / name
        if p.exists():
            return p
    return MODEL_DIR / (names or _EN_MODELS)[-1]


MODEL = _best_model()


def stt_lang_mode() -> "tuple":
    """(model_path, whisper -l arg). English-only for now: the English model
    is the most accurate for English and avoids mis-detecting English speech
    as another language. (Multilingual support is parked; see git history.)"""
    return _best_model(_EN_MODELS), "en"


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


def record_start(wav: str, max_secs: int = 30,
                 silence_stop: float = 2.0):
    """Start a recording as a Popen (same gating as record()); None on fail.

    Lets the caller poll/terminate mid-recording, which vb talk uses to cut
    a silent wait short when Claude's reply lands.
    """
    rec = _find("rec")
    if not rec:
        core.log("record_start: sox `rec` not found")
        return None
    # No `norm` here: it buffers the whole file until the end, which breaks
    # mid-recording did-speech-start checks that poll the wav's size.
    cmd = [
        rec, "-q", "-c", "1", "-r", "16000", "-b", "16", wav,
        "trim", "0", str(max_secs),
        "silence", "1", "0.05", "0.3%", "1", str(silence_stop), "1.5%",
    ]
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception as e:
        core.log(f"record_start failed: {e}")
        return None


def transcribe(wav: str) -> str:
    """Run whisper.cpp on a wav and return cleaned text."""
    return transcribe_ex(wav)[0]


def transcribe_ex(wav: str) -> "tuple[str, float]":
    """Transcribe and also return whisper's confidence (mean token
    probability, 0..1). Real directed speech scores ~0.7+; background
    chatter, mumble, and noise score low, which lets callers drop them."""
    wb = whisper_bin()
    model, lang = stt_lang_mode()
    if not wb or not model.exists():
        core.log("transcribe: missing whisper binary or model")
        return "", 0.0
    base = wav + ".vbout"
    cmd = [wb, "-m", str(model), "-f", wav, "-nt", "-np", "-l", lang,
           "-ojf", "-of", base]   # -ojf: full JSON includes token probabilities
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        core.log(f"transcribe failed: {e}")
        return "", 0.0
    text = (out.stdout or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned = " ".join(lines)
    for tag in ("[BLANK_AUDIO]", "(blank audio)", "[ Silence ]", "[silence]"):
        cleaned = cleaned.replace(tag, "")
    conf = 0.0
    jpath = base + ".json"
    try:
        with open(jpath) as f:
            data = json.load(f)
        probs = [t.get("p", 0.0)
                 for seg in data.get("transcription", [])
                 for t in seg.get("tokens", [])
                 if not t.get("text", "").startswith("[_")]
        if probs:
            conf = sum(probs) / len(probs)
    except Exception:
        pass
    finally:
        try:
            os.remove(jpath)
        except OSError:
            pass
    return cleaned.strip(), conf


def loudness(wav: str) -> float:
    """RMS amplitude of the capture (0..1). Directed out-loud speech near
    the mic is loud; background conversation and whispers are quiet."""
    sox = _find("sox")
    if not sox:
        return -1.0
    try:
        r = subprocess.run([sox, wav, "-n", "stat"], capture_output=True,
                           text=True, timeout=30)
        m = re.search(r"RMS\s+amplitude:\s+([0-9.]+)", r.stderr)
        return float(m.group(1)) if m else -1.0
    except Exception:
        return -1.0

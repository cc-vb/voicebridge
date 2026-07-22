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


# --- warm resident model (issue #2) ------------------------------------------
# Spawning whisper-cli per utterance reloads the ~half-GB model and dlopen's
# the BLAS/Metal/CPU backends every time: measured ~2.1s cold vs ~0.3s against
# a warm whisper-server. So we keep one server resident (mirrors the Kokoro
# TTS server) and fall back to the CLI whenever the server isn't available,
# so nothing breaks on installs without whisper-server.
WHISPER_PORT = int(os.environ.get("VB_STT_PORT", "8799"))


def whisper_server_bin() -> str:
    return _find("whisper-server")


def whisper_up() -> bool:
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "2", "-o", "/dev/null", "-w", "%{http_code}",
             f"http://127.0.0.1:{WHISPER_PORT}/"],
            capture_output=True, timeout=5)
        # Any HTTP reply means the server is listening (root may 404/501).
        return r.stdout.strip() not in (b"", b"000")
    except Exception:
        return False


def ensure_whisper_server(wait_s: float = 20.0) -> bool:
    """Start a resident whisper-server if one isn't already up; wait for it.
    Returns False (and callers fall back to the CLI) if the binary or model
    is missing, so this is always safe to call."""
    if whisper_up():
        return True
    srv = whisper_server_bin()
    model, lang = stt_lang_mode()
    if not srv or not model.exists():
        return False
    try:
        p = subprocess.Popen(
            [srv, "-m", str(model), "-l", lang,
             "--port", str(WHISPER_PORT), "-nt"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        (core.STATE_DIR / "stt.pid").write_text(str(p.pid))
    except Exception as e:
        core.log(f"whisper server autostart failed: {e}")
        return False
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < wait_s:
        if whisper_up():
            core.log("whisper server autostarted")
            return True
        _t.sleep(0.4)
    return False


def _clean_text(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned = " ".join(lines)
    for tag in ("[BLANK_AUDIO]", "(blank audio)", "[ Silence ]", "[silence]"):
        cleaned = cleaned.replace(tag, "")
    return cleaned.strip()


def _transcribe_server(wav: str) -> "tuple[str, float] | None":
    """Transcribe against the warm whisper-server. Returns (text, conf) with
    conf = mean per-word probability (same 0..1 scale the CLI path derives
    from token probabilities), or None if the server didn't answer so the
    caller falls back to the CLI."""
    if not whisper_up():
        return None
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "30", f"http://127.0.0.1:{WHISPER_PORT}/inference",
             "-F", f"file=@{wav}", "-F", "response_format=verbose_json",
             "-F", "temperature=0.0"],
            capture_output=True, timeout=35)
        data = json.loads(r.stdout.decode("utf-8", "replace"))
    except Exception as e:
        core.log(f"whisper server inference failed: {e}")
        return None
    text = _clean_text(data.get("text", "") or "")
    probs = [w.get("probability", 0.0)
             for seg in data.get("segments", [])
             for w in seg.get("words", [])
             if isinstance(w.get("probability", None), (int, float))]
    conf = sum(probs) / len(probs) if probs else 0.0
    return text, conf


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


def live_level(wav: str, tail_ms: int = 200) -> float:
    """Cheap 0..1 loudness of the last ~tail_ms of a 16k mono s16le recording,
    for the live mic meter. Reads only the tail and does the RMS in-process
    (no sox), so it's fine to call every ~0.15s in the capture loop."""
    import array
    try:
        with open(wav, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            nbytes = min(size - 44, int(16000 * 2 * tail_ms / 1000))
            if nbytes <= 0:
                return 0.0
            nbytes -= nbytes % 2
            f.seek(size - nbytes)
            raw = f.read(nbytes)
        a = array.array("h")
        a.frombytes(raw)
        if not a:
            return 0.0
        rms = (sum(x * x for x in a) / len(a)) ** 0.5
        # sqrt curve: loudness is perceived roughly non-linearly, so a soft
        # voice still visibly moves the meter instead of barely twitching.
        return min(1.0, (rms / 6000.0) ** 0.5)
    except Exception:
        return 0.0


def transcribe(wav: str) -> str:
    """Run whisper.cpp on a wav and return cleaned text."""
    return transcribe_ex(wav)[0]


def transcribe_ex(wav: str) -> "tuple[str, float]":
    """Transcribe and also return whisper's confidence (mean token
    probability, 0..1). Real directed speech scores ~0.7+; background
    chatter, mumble, and noise score low, which lets callers drop them."""
    # Fast path: the warm resident server (issue #2). Falls through to the
    # CLI if it's not up or didn't answer, so behavior is identical otherwise.
    served = _transcribe_server(wav)
    if served is not None:
        return served
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

"""Optional summarized-output layer (standalone voicebridge only).

Turns a full Claude reply into a short SPOKEN briefing via a LOCAL model. This
is a presentation choice on the path where voicebridge itself reads Claude's
reply and decides what to say. It is OFF by default (Full is the spoken
default). When voicebridge is embedded by another product (e.g. Friday), that
product hands voicebridge its own already-summarized text to speak, so this
layer is simply never on that path, there is nothing to double-summarize.

The engine is PLUGGABLE so quality-vs-latency can be compared during testing:
  - "apple"  Apple Foundation Models via the built-in `fm` CLI (zero download,
             fast, modest quality; Apple Silicon + macOS 26 + AI enabled).
  - "mlx"    a small MLX model (needs a one-time download; predictable quality).
  - "ollama" an Ollama model (heaviest setup; convenient if already installed).
Every backend fails SOFT to "" so the caller falls back to speaking the full
reply, never an error. Nothing calls this yet; wiring into the voice flow and a
Full/Summarized toggle come next.
"""

import shutil
import subprocess

from . import core, oslayer

# Replies shorter than this are already quick to hear; skip summarizing them
# (avoids paying model latency to "summarize" a one-liner).
MIN_CHARS = 400
# Keep well under a small model's context; trim very long replies before summary.
MAX_INPUT_CHARS = 6000

_PROMPT = (
    "You are the voice of a coding assistant briefing a developer who is "
    "LISTENING, not reading. Summarize the assistant reply below in 2 to 3 "
    "short sentences, in the plainest terms. You MUST preserve: any decision "
    "made, any warning or caveat, any question directed at the user, and what "
    "changed (files, commands, results). Describe code in words, never quote "
    "it. Output ONLY the summary, nothing else.\n\nREPLY:\n")

_MLX_MODEL = "mlx-community/Qwen2.5-3B-Instruct-4bit"
_OLLAMA_MODEL = "qwen2.5:3b"


# ---------- backend availability -------------------------------------------
def _apple_ready() -> bool:
    return oslayer.IS_MAC and bool(shutil.which("fm"))


def _mlx_ready() -> bool:
    import importlib.util
    return oslayer.IS_MAC and importlib.util.find_spec("mlx_lm") is not None


def _ollama_ready() -> bool:
    if not shutil.which("ollama"):
        return False
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1).read()
        return True
    except Exception:
        return False


def engine_available(prefer: str = "auto") -> str:
    """The summarizer backend to use: 'apple' | 'mlx' | 'ollama' | '' (none).
    `prefer` forces a specific one (for A/B testing); 'auto' picks by priority
    apple -> mlx -> ollama (fast/zero-setup first)."""
    checks = {"apple": _apple_ready, "mlx": _mlx_ready, "ollama": _ollama_ready}
    order = ([prefer] if prefer in checks else ["apple", "mlx", "ollama"])
    for e in order:
        try:
            if checks[e]():
                return e
        except Exception:
            pass
    return ""


# ---------- backends (each returns '' on any failure) -----------------------
def _run_apple(prompt: str) -> str:
    try:
        r = subprocess.run(["fm", "respond", prompt], capture_output=True,
                           text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception as e:
        core.log(f"summarize apple/fm failed: {e}")
        return ""


_MLX = {}   # cache the loaded model+tokenizer for the process lifetime (warm)


def _run_mlx(prompt: str) -> str:
    try:
        from mlx_lm import generate, load
        if "m" not in _MLX:
            _MLX["m"], _MLX["t"] = load(_MLX_MODEL)
        return generate(_MLX["m"], _MLX["t"], prompt=prompt,
                        max_tokens=160, verbose=False).strip()
    except Exception as e:
        core.log(f"summarize mlx failed: {e}")
        return ""


def _run_ollama(prompt: str) -> str:
    try:
        import json
        import urllib.request
        body = json.dumps({"model": _OLLAMA_MODEL, "prompt": prompt,
                           "stream": False, "keep_alive": "24h"}).encode()
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        out = urllib.request.urlopen(req, timeout=30).read()
        return json.loads(out).get("response", "").strip()
    except Exception as e:
        core.log(f"summarize ollama failed: {e}")
        return ""


_RUN = {"apple": _run_apple, "mlx": _run_mlx, "ollama": _run_ollama}


def summarize(text: str, engine: str = "auto") -> str:
    """Return a short spoken briefing of `text`, or '' to mean 'no summary,
    speak the full reply'. Never raises. '' when the text is already short, no
    backend is available, or the backend failed."""
    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        return ""
    e = engine_available(engine)
    if not e:
        return ""
    prompt = _PROMPT + text[:MAX_INPUT_CHARS]
    return _RUN[e](prompt).strip()

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

import os
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

# Summarizing into 2-3 sentences is an easy, instruction-following task, so
# default to the smallest still-instruction-capable model to keep the one-time
# download tiny (~290MB). If its briefings feel thin, bump up with one env var:
#   VB_SUMMARIZE_MODEL=mlx-community/Qwen2.5-1.5B-Instruct-4bit   (~1GB)
#   VB_SUMMARIZE_MODEL=mlx-community/Qwen2.5-3B-Instruct-4bit     (~2GB)
_MLX_MODEL = (os.environ.get("VB_SUMMARIZE_MODEL")
              or "mlx-community/Qwen2.5-0.5B-Instruct-4bit")   # ~290MB
_OLLAMA_MODEL = os.environ.get("VB_SUMMARIZE_MODEL_OLLAMA") or "qwen2.5:0.5b"
# voicebridge-managed MLX venv (built by install.sh, exactly like the Kokoro
# venv) so the user never runs pip. Served warm on a per-uid port like the
# Kokoro/whisper servers.
MLX_VENV = core.STATE_DIR / "mlx-venv"
_MLX_PORT = int(os.environ.get("VB_MLX_PORT") or (6100 + os.getuid() % 1000))

# Summarized-output mode is OPT-IN: presence of this flag == on. Full is the
# spoken default. An embedder (Friday) never sets this, so it always gets full.
MODE_FLAG = core.STATE_DIR / "summarize_mode"


def is_on() -> bool:
    return MODE_FLAG.exists()


def set_mode(on: bool) -> None:
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        if on:
            MODE_FLAG.write_text("1")
        elif MODE_FLAG.exists():
            MODE_FLAG.unlink()
    except OSError as e:
        core.log(f"summarize mode write failed: {e}")


# ---------- backend availability -------------------------------------------
def _apple_ready() -> bool:
    return oslayer.IS_MAC and bool(shutil.which("fm"))


def _mlx_ready() -> bool:
    # "provisioned" == the managed venv exists (install.sh built it). The warm
    # server starts on demand; its first launch downloads the model in the
    # background and summaries fall back to full until it is ready.
    return oslayer.IS_MAC and (MLX_VENV / "bin" / "python").exists()


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


def _mlx_up() -> bool:
    try:
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{_MLX_PORT}/v1/models",
                               timeout=1).read()
        return True
    except Exception:
        return False


def start_mlx_server() -> None:
    """Launch the warm mlx-lm server from the managed venv (fire-and-forget).
    The FIRST launch downloads the model (~2GB) in the background; until it is
    ready, summaries fall back to the full reply. No user pip ever, and the
    server stays warm for the process lifetime so later summaries are fast."""
    if not _mlx_ready() or _mlx_up():
        return
    try:
        subprocess.Popen(
            [str(MLX_VENV / "bin" / "python"), "-m", "mlx_lm.server",
             "--model", _MLX_MODEL, "--port", str(_MLX_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception as e:
        core.log(f"mlx server start failed: {e}")


def _run_mlx(prompt: str) -> str:
    # Never block the speak path: if the warm server isn't up yet, kick it in
    # the background and fall back to the full reply for this turn.
    if not _mlx_up():
        start_mlx_server()
        return ""
    try:
        import json
        import urllib.request
        body = json.dumps({
            "model": _MLX_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 160, "temperature": 0.2}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{_MLX_PORT}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        out = urllib.request.urlopen(req, timeout=40).read()
        return json.loads(out)["choices"][0]["message"]["content"].strip()
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
    out = _RUN[e](prompt).strip()
    if out and _dropped_question(text, out):
        return ""   # safety net: speak the full reply rather than swallow a Q
    return out


def _dropped_question(full: str, brief: str) -> bool:
    """If the reply ends with a question to the user but the briefing has none,
    the summary likely dropped it. Better to speak the full reply than to
    silently swallow a question you need to answer. Conservative (only the
    trailing chunk) so it doesn't over-trigger on incidental '?' mid-reply."""
    return "?" in full[-240:] and "?" not in brief

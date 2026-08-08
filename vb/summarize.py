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
reply, never an error. summarize() tries each ready backend in priority order
and uses the first that returns real text, so a broken preferred engine (e.g.
`fm` present but Apple Intelligence off) never masks a working one. Wired into
the voice flow at core.speak_chunks_blocking (Mac) and call.py's reply emit
(phone), with a Full/Brief toggle on both.
"""

import os
import re
import shutil
import subprocess

from . import core, oslayer

# Replies shorter than this are already quick to hear; skip summarizing them
# (avoids paying model latency to "summarize" a one-liner).
MIN_CHARS = 400
# Keep well under a small model's context; trim very long replies before summary.
MAX_INPUT_CHARS = 6000
# Hard ceiling on a single briefing call. A warm 0.5B model answers in well
# under a second, so this is pure headroom, its real job is to bound the WORST
# case: the brief is computed on the reply path before the phone speaks, so a
# cold/stuck backend must fail FAST to the full reply instead of leaving the
# phone silent while it waits. 40s+ here reads to the listener as "not speaking".
_CALL_TIMEOUT = float(os.environ.get("VB_SUMMARIZE_TIMEOUT") or 8)

_PROMPT = (
    "Summarize the assistant reply below for a developer who is LISTENING. "
    "Write ONE or TWO short sentences, at most 40 words total, in plain terms. "
    "Keep any decision, warning, and what changed (files, commands, results). "
    "Do NOT repeat yourself, do NOT quote code, do NOT copy sentences from the "
    "reply. Output ONLY the summary.\n\nREPLY:\n")

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
                           text=True, timeout=_CALL_TIMEOUT)
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


def _warm_model() -> None:
    """Force the model to load with ONE throwaway request, so the first REAL
    reply doesn't pay the load cost (which would otherwise stall the phone's
    spoken reply for several seconds). Runs in a background thread; harmless if
    it fails, the reply path just falls back to full until the model is ready."""
    import time
    for _ in range(90):              # wait up to ~90s for the server to bind
        if _mlx_up():
            break
        time.sleep(1)
    else:
        return
    try:
        import json
        import urllib.request
        body = json.dumps({
            "model": _MLX_MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{_MLX_PORT}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=120).read()   # blocks until loaded
    except Exception as e:
        core.log(f"mlx warm-up skipped: {e}")


def start_mlx_server() -> None:
    """Launch the warm mlx-lm server from the managed venv (fire-and-forget).
    The FIRST launch downloads the model (~290MB) in the background; until it is
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
        return
    # Pre-warm off the caller's thread so cold-start latency never lands on a
    # user-facing reply.
    import threading
    threading.Thread(target=_warm_model, daemon=True).start()


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
        out = urllib.request.urlopen(req, timeout=_CALL_TIMEOUT).read()
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
        out = urllib.request.urlopen(req, timeout=_CALL_TIMEOUT).read()
        return json.loads(out).get("response", "").strip()
    except Exception as e:
        core.log(f"summarize ollama failed: {e}")
        return ""


_RUN = {"apple": _run_apple, "mlx": _run_mlx, "ollama": _run_ollama}


def _ready_engines(prefer: str = "auto") -> list:
    """Ready backends in priority order (apple -> mlx -> ollama), or [prefer]
    if a specific one is forced. Unlike engine_available (which returns only the
    FIRST), this returns all ready ones so summarize() can FALL THROUGH: a Mac
    with the `fm` binary but Apple Intelligence off no longer masks an installed
    mlx engine, it just tries mlx next."""
    checks = {"apple": _apple_ready, "mlx": _mlx_ready, "ollama": _ollama_ready}
    order = ([prefer] if prefer in checks else ["apple", "mlx", "ollama"])
    ready = []
    for e in order:
        try:
            if checks[e]():
                ready.append(e)
        except Exception:
            pass
    return ready


def summarize(text: str, engine: str = "auto") -> str:
    """Return a short spoken briefing of `text`, or '' to mean 'no summary,
    speak the full reply'. Never raises. '' when the text is already short, no
    backend is available, or every ready backend failed/returned nothing."""
    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        return ""
    prompt = _PROMPT + text[:MAX_INPUT_CHARS]
    # Try each ready backend in order; the first that returns real text wins.
    # A backend that yields "" (Apple Intelligence off, mlx model still warming,
    # ollama model not pulled) does NOT end the search, we fall through.
    for e in _ready_engines(engine):
        out = _RUN[e](prompt).strip()
        # A real summary is clearly SHORTER. Small models sometimes ramble or
        # repeat until they produce something as long as (or longer than) the
        # reply, which is worse than useless as a spoken brief. Reject those and
        # fall through / speak full rather than read out padded garbage.
        if out and len(out) <= len(text) * 0.66:
            return _keep_trailing_question(text, out)
    return ""


_QSENT = re.compile(r"([^.!?\n]*\?)\s*$")


def _last_question(text: str) -> str:
    """The reply's final sentence, if it is a question (ends with '?')."""
    m = _QSENT.search(text.rstrip())
    return m.group(1).strip() if m else ""


def _keep_trailing_question(full: str, brief: str) -> str:
    """Conversation replies almost always END with a question to the user, and a
    tiny model often drops it when summarizing. Discarding the whole brief (the
    old safety net) meant briefing NEVER fired in a live back-and-forth. Instead,
    keep the summary AND re-attach the actual trailing question, so the spoken
    brief is 'the gist, then the question you need to answer'."""
    q = _last_question(full)
    if not q or "?" in brief:
        return brief
    return f"{brief} {q}".strip()

"""The local brain: a small model, kept warm, that never leaves the machine.

Friday needs judgment in three narrow places, and none of them need a big model:

  stakes      is what this agent is asking consequential, or trivial?
  briefing    turn a wall of agent output into two spoken sentences
  wording     say a batch of news the way a person would say it

Deliberately NOT here: deciding whether to interrupt you. That is a rule table
in attention.py, because a model that is ~65% right about interruptions is worse
than rules that are always explainable. The brain advises; the rules decide.

Runtime is llama.cpp's server: one binary, no Python coupling, the model stays
resident so later calls are fast, and it can CONSTRAIN output to a JSON schema,
which is what makes a 4B model reliable at classification instead of chatty.

Everything fails soft. No server, no model, a timeout: every function returns
the empty/neutral answer and the caller carries on with templates. Friday must
work with no brain at all, just less gracefully.
"""

import json
import os
import subprocess
import time
import urllib.request

from . import core, oslayer

MODEL_DIR = core.STATE_DIR / "models"

# The reflex tier. This model does NOT need to be clever: its jobs are a binary
# danger call, a two-sentence compression, and plain wording. Speed and staying
# resident matter more than depth, and real reasoning goes to a big brain
# (Claude) which is already on the machine, per the two-tier design.
#
# Swappable with one env var, because "is 4B enough?" is a question to settle by
# measuring on your own work, not by argument:
#   VB_BRAIN_MODEL=qwen3-8b-instruct-q4_k_m.gguf    smarter, ~5GB, fits 16GB
#   VB_BRAIN_MODEL=qwen3-1.7b-instruct-q4_k_m.gguf  faster, ~1.1GB, 8GB Macs
# Anything ~14B and up will swap and thermally throttle on a fanless Air, which
# makes it both slower AND worse: not a trade worth taking.
MODEL_NAME = (os.environ.get("VB_BRAIN_MODEL")
              or "qwen3-4b-instruct-2507-q4_k_m.gguf")
MODEL_PATH = MODEL_DIR / MODEL_NAME

# Per-uid so two accounts on one Mac never fight over a port (kokoro is 6000+,
# the old mlx summarizer 6100+, whisper 7000+, the phone relay 8790+).
PORT = int(os.environ.get("VB_BRAIN_PORT") or (6200 + oslayer.uid() % 1000))

# A spoken loop cannot wait. These are ceilings, not targets: a warm 4B answers
# a classification in well under a second on Apple Silicon.
TIMEOUT_FAST = 6.0     # stakes: blocks a decision, must be quick
TIMEOUT_SLOW = 12.0    # briefing: longer input, still bounded

_LOG_ONCE = {"missing": False}


# A partially-downloaded model is WORSE than no model: llama-server will fail
# to load a truncated file and the failure looks like a code bug. So readiness
# means "a companion .ok marker exists", written only after a verified download.
OK_MARKER = MODEL_DIR / (MODEL_NAME + ".ok")


def model_ready() -> bool:
    try:
        return (OK_MARKER.exists() and MODEL_PATH.exists()
                and MODEL_PATH.stat().st_size > 1_000_000_000)
    except Exception:
        return False


def mark_model_ok(expected_bytes: int = 0) -> bool:
    """Called after a download completes. Verifies the size (and the GGUF magic
    bytes, which catches an HTML error page saved under a .gguf name) before
    declaring the model usable."""
    try:
        if not MODEL_PATH.exists():
            return False
        size = MODEL_PATH.stat().st_size
        if expected_bytes and size != expected_bytes:
            core.log(f"model size mismatch: {size} != {expected_bytes}")
            return False
        with open(MODEL_PATH, "rb") as f:
            if f.read(4) != b"GGUF":
                core.log("model is not a GGUF file")
                return False
        OK_MARKER.write_text(str(size))
        return True
    except Exception as e:
        core.log(f"model verify failed: {e}")
        return False


def server_bin() -> str:
    from shutil import which
    for name in ("llama-server",):
        p = which(name)
        if p:
            return p
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        p = os.path.join(d, "llama-server")
        if os.path.exists(p):
            return p
    return ""


def up(timeout: float = 1.0) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health",
                               timeout=timeout).read()
        return True
    except Exception:
        return False


def start(wait_s: float = 0.0) -> bool:
    """Launch the resident server, fire and forget. Returns whether it is up
    (immediately, or within wait_s if asked to wait)."""
    if up():
        return True
    binp, ok = server_bin(), model_ready()
    if not binp or not ok:
        if not _LOG_ONCE["missing"]:
            _LOG_ONCE["missing"] = True
            core.log(f"brain unavailable (server={bool(binp)} model={ok}); "
                     "falling back to templates")
        return False
    try:
        subprocess.Popen(
            [binp, "-m", str(MODEL_PATH), "--port", str(PORT),
             "--ctx-size", "4096",          # plenty for a reply + a prompt
             "--n-gpu-layers", "99",        # Metal: put it all on the GPU
             "--parallel", "1", "--no-webui"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **oslayer.detached())
        core.log(f"brain starting on :{PORT}")
    except Exception as e:
        core.log(f"brain start failed: {e}")
        return False
    t0 = time.time()
    while wait_s and time.time() - t0 < wait_s:
        if up():
            return True
        time.sleep(0.5)
    return up()


# Why the last call came back empty, for a caller that wants to tell a
# truncation apart from a refusal. Kept beside the call rather than returned,
# because every caller expects a string and none of them should have to change
# to get an honest one.
last_stop = ""


def _chat(messages: list, timeout: float, schema: dict = None,
          max_tokens: int = 200) -> str:
    """One call. Returns '' on any failure, which every caller treats as
    'no answer, use the template'.

    That conflation has one dangerous case. A reasoning-tuned model emits its
    thinking BEFORE the schema-constrained answer, and the schema binds the
    answer, not how long it thinks first. So a small max_tokens can be spent
    entirely on reasoning, returning finish_reason 'length' with EMPTY content
    -- which arrives here as '' and is read downstream as "that was just
    ordinary conversation". The sentence then falls through silently, and
    nothing anywhere says a model call was cut off.

    Still returns a string, so nothing breaks. But the reason is recorded in
    `last_stop` and a truncation is logged, because a failure that looks
    exactly like a decision is the worst kind to have."""
    if not up():
        start()          # warm it for next time; this call falls back
        return ""
    body = {"messages": messages, "max_tokens": max_tokens,
            "temperature": 0.2, "stream": False}
    if schema:
        # Constrained decoding: the model physically cannot return anything
        # that does not fit the schema, which is what makes a small model
        # dependable for classification.
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "out",
                                                   "schema": schema,
                                                   "strict": True}}
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        global last_stop
        d = json.loads(raw)
        choice = (d.get("choices") or [{}])[0]
        last_stop = choice.get("finish_reason") or ""
        out = ((choice.get("message") or {}).get("content") or "").strip()
        if not out and last_stop == "length":
            # Not "no answer". Cut off mid-thought, and the caller cannot tell.
            thought = len(((choice.get("message") or {}).get(
                "reasoning_content") or ""))
            core.log(f"brain: TRUNCATED at max_tokens={max_tokens} with no "
                     f"answer ({thought} chars of reasoning). Raise max_tokens "
                     f"or add few-shot examples to suppress thinking.")
        return out
    except Exception as e:
        last_stop = "error"
        core.log(f"brain call failed: {e}")
        return ""


# ---- the three jobs -------------------------------------------------------
_STAKES_SCHEMA = {
    "type": "object",
    "properties": {"stakes": {"type": "string", "enum": ["high", "low"]}},
    "required": ["stakes"], "additionalProperties": False,
}

_STAKES_PROMPT = (
    "A coding agent is asking its user a question, or asking permission to act. "
    "Decide if the consequence is HIGH (destructive, irreversible, touches "
    "production, deletes or overwrites data, force-pushes, spends money, "
    "changes credentials or security) or LOW (a normal coding choice: naming, "
    "which library, formatting, where to put a file). Answer with the schema.")


def _clean(text: str) -> str:
    """Normalize model punctuation. Small models love em-dashes, which read as
    an odd stumble in TTS and look wrong in the chat; commas say the same thing."""
    return (text.replace("\u2014", ", ").replace("\u2013", ", ")
                .replace(" ,", ",").replace(",,", ",").strip())


def stakes(question: str) -> str:
    """'high' | 'low' | '' (no opinion). Advisory only: the rule table may
    promote on 'high', but it never silences a question on 'low'."""
    q = (question or "").strip()
    if not q:
        return ""
    out = _chat([{"role": "system", "content": _STAKES_PROMPT},
                 {"role": "user", "content": q[:1200]}],
                TIMEOUT_FAST, schema=_STAKES_SCHEMA, max_tokens=20)
    if not out:
        return ""
    try:
        v = json.loads(out).get("stakes", "")
        return v if v in ("high", "low") else ""
    except Exception:
        return ""


_BRIEF_PROMPT = (
    "Summarize this coding-assistant output for someone LISTENING, not reading. "
    "One or two short sentences, at most 40 words. Keep any decision, warning, "
    "and what changed. Do not repeat yourself, do not quote code, do not copy "
    "sentences. Output only the summary.")


def brief(text: str, min_chars: int = 400) -> str:
    """A spoken briefing, or '' meaning 'just say the full thing'. Rejects any
    'summary' that is not clearly shorter, since small models sometimes pad
    instead of compressing."""
    t = (text or "").strip()
    if len(t) < min_chars:
        return ""
    out = _chat([{"role": "system", "content": _BRIEF_PROMPT},
                 {"role": "user", "content": t[:6000]}],
                TIMEOUT_SLOW, max_tokens=120)
    if out and len(out) <= len(t) * 0.66:
        return _clean(out)
    return ""


_SAY_PROMPT = (
    "Rewrite these status updates as ONE natural spoken sentence or two, the "
    "way a colleague would mention them. Keep every agent name and every fact. "
    "Most important first. No preamble, no lists, no markdown.")


def phrase(items_text: str, enabled: bool = False) -> str:
    """Optionally reword a batched utterance. '' means keep the template.

    OFF BY DEFAULT, on measurement rather than principle: asked to reword three
    separate alerts, the 4B ran them together into one confusing sentence
    ("api hit an error, build failed, jobhunt finished") where the template kept
    them cleanly apart. The templates are plainer and clearer, so they win. Kept
    behind a flag for when a stronger model is swapped in."""
    t = (items_text or "").strip()
    if not enabled or not t:
        return ""
    out = _chat([{"role": "system", "content": _SAY_PROMPT},
                 {"role": "user", "content": t[:1200]}],
                TIMEOUT_FAST, max_tokens=100)
    # Guard against a model that invents facts or rambles: a rewrite may not be
    # much longer than what it was given.
    if out and len(out) <= max(120, len(t) * 1.4):
        return _clean(out)
    return ""


def status() -> dict:
    return {"server": bool(server_bin()), "model": model_ready(),
            "up": up(), "port": PORT, "path": str(MODEL_PATH)}

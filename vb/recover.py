"""Layer 2 self-heal: catch errors, recover automatically, guide the user.

Triggered by errors, not by commands. When voicebridge hits an exception it:
  1. logs it locally (with detail, for the user's own `vb doctor`),
  2. runs the Layer-1 deterministic heals (the error may be a known recoverable
     condition, e.g. a server that died),
  3. maps known error signatures to a plain-language next step, and
  4. feeds an opt-in, scrubbed report to Layer 3 so we can fix it upstream.

It never asks the user to run anything and never AI-patches their install (fixes
ship from upstream via the normal update). `guard()` wraps a risky region so an
unhandled error becomes a recorded, recovered event instead of a silent death.
"""

import json
import time
from contextlib import contextmanager

from . import core, selfheal, telemetry

ERR_LOG = core.STATE_DIR / "errors.jsonl"
_MAX_ERR_LINES = 200

# Known error signatures -> a plain next step. Deterministic; extend freely.
_REMEDIES = [
    ("not authorized", "macOS needs Accessibility permission for your terminal "
                        "(System Settings, Privacy & Security, Accessibility)."),
    ("assistive access", "Enable Accessibility for your terminal in System "
                          "Settings, Privacy & Security."),
    ("address already in use", "A port was busy; cleared stale processes, "
                               "try again in a moment."),
    ("microphone", "Grant your terminal Microphone access in System Settings."),
    ("kokoro", "The voice server was down; it has been restarted."),
    ("whisper", "The transcription server was down; it has been restarted."),
]


def _append_local(where: str, exc: BaseException) -> None:
    """Local, capped error log for the user's own debugging (stays on-machine,
    so it may keep the full message, unlike the scrubbed telemetry report)."""
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "where": where,
               "type": type(exc).__name__, "msg": str(exc)[:500]}
        lines = []
        if ERR_LOG.exists():
            lines = ERR_LOG.read_text(errors="ignore").splitlines()[-(_MAX_ERR_LINES - 1):]
        lines.append(json.dumps(rec))
        ERR_LOG.write_text("\n".join(lines) + "\n")
    except Exception:
        pass


def _append_msg(where: str, msg: str) -> None:
    """Ledger a plain-message problem (no exception), so surfaced errors also
    show up in `vb errors` alongside the caught-exception records."""
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "where": where, "type": "error", "msg": msg[:500]}
        lines = []
        if ERR_LOG.exists():
            lines = ERR_LOG.read_text(errors="ignore").splitlines()[-(_MAX_ERR_LINES - 1):]
        lines.append(json.dumps(rec))
        ERR_LOG.write_text("\n".join(lines) + "\n")
    except Exception:
        pass


def note(where: str, msg: str) -> None:
    """Record a surfaced (already user-shown) problem to the local ledger."""
    _append_msg(where, msg)


def _remedy(exc: BaseException) -> str:
    m = str(exc).lower()
    for needle, hint in _REMEDIES:
        if needle in m:
            return hint
    return ""


_last_seen = {}   # (where:type) -> ts, so a per-tick error can't spam heal/log


def record(where: str, exc: BaseException) -> str:
    """Handle a caught error: log, self-heal, feed telemetry, return a hint
    (or '' if none). Never raises. Throttled per error signature so an error
    that recurs every loop iteration doesn't flood the log or re-heal endlessly."""
    try:
        key = f"{where}:{type(exc).__name__}"
        now = time.time()
        if now - _last_seen.get(key, 0.0) < 30.0:
            return _remedy(exc)   # seen recently: skip the heavy side effects
        _last_seen[key] = now
        core.log(f"recover[{where}]: {type(exc).__name__}: {exc}")
        _append_local(where, exc)
        try:
            selfheal.heal(apply=True)     # Layer 1: fix known recoverable state
        except Exception:
            pass
        try:
            telemetry.send(telemetry.build_report(where, exc))   # Layer 3 (opt-in)
        except Exception:
            pass
        # Surface it so the user actually KNOWS (phone toast + ledger; speak
        # only when we have an actionable remedy, so internal hiccups don't
        # talk over a session). Already ledgered above -> ledger=False.
        hint = _remedy(exc)
        msg = hint if hint else f"a problem with {where}"
        core.surface_error(where, msg, speak=bool(hint), ledger=False)
        return hint
    except Exception:
        return ""


@contextmanager
def guard(where: str, reraise: bool = False):
    """Wrap a risky region so an unhandled exception is recorded and recovered
    instead of silently killing a loop/handler. Set reraise=True where the
    caller still needs the failure to propagate."""
    try:
        yield
    except Exception as e:
        record(where, e)
        if reraise:
            raise


def recent_errors(n: int = 20) -> list:
    try:
        lines = ERR_LOG.read_text(errors="ignore").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []

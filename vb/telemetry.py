"""Layer 3 self-heal: OPT-IN, privacy-scrubbed crash reports.

This is the only part of voicebridge that can send anything off the machine, so
it is OFF by default and refuses to send unless the user explicitly opted in
AND a report endpoint is configured. Reports carry the bare minimum to fix a
bug upstream (which then reaches everyone via the normal update): exception
TYPE, our own stack frames (file:line:func, no locals, no arguments), the
version, and the OS. They deliberately carry NO exception message, NO paths, NO
prompt/transcript/code content, nothing derived from what the user said or did.

Why opt-in and not automatic: "your data never leaves your machine" is the
product's whole promise. Silent telemetry would break it. A one-time consent is
the honest price of letting us fix bugs we can't see.
"""

import json
import os
import platform
import threading
import traceback
import urllib.request

from . import core

OPT_IN = core.STATE_DIR / "telemetry_opt_in"      # presence == consented
URL_FILE = core.STATE_DIR / "telemetry_url"        # where reports go (if any)


def opted_in() -> bool:
    return OPT_IN.exists()


def set_opt_in(on: bool) -> None:
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        if on:
            OPT_IN.write_text("1")
        elif OPT_IN.exists():
            OPT_IN.unlink()
    except OSError as e:
        core.log(f"telemetry opt-in write failed: {e}")


def _endpoint() -> str:
    return (os.environ.get("VB_TELEMETRY_URL", "").strip()
            or core._read(URL_FILE).strip())


def _our_frames(tb) -> list:
    """Only frames inside voicebridge's own package, and only file:line:func,
    never locals or argument values (those could contain user data)."""
    frames = []
    for fr in traceback.extract_tb(tb):
        fn = fr.filename or ""
        if os.sep + "vb" + os.sep in fn or "voicebridge" in fn:
            frames.append(f"{os.path.basename(fn)}:{fr.lineno}:{fr.name}")
    return frames[-8:]


def build_report(where: str, exc: BaseException) -> dict:
    """A scrubbed, content-free crash report. `where` is a FIXED label we set in
    the code (e.g. 'daemon-loop'), never user-derived."""
    return {
        "v": core._local_version(),
        "os": platform.system(),
        "py": platform.python_version(),
        "where": where,
        "err_type": type(exc).__name__,
        "frames": _our_frames(getattr(exc, "__traceback__", None)),
        # NB: intentionally no exception message and no free-form strings.
    }


def send(report: dict) -> None:
    """Fire-and-forget POST, ONLY when opted in and an endpoint is set. Never
    blocks and never raises; if it can't send, it silently drops."""
    if not opted_in():
        return
    url = _endpoint()
    if not url:
        return

    def _post():
        try:
            data = json.dumps(report).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass

    try:
        threading.Thread(target=_post, daemon=True).start()
    except Exception:
        pass

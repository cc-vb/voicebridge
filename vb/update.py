"""Is this copy of voicebridge current?

Issue #10. Testers were running weeks-old code, so shipped fixes never reached
them and their reports were against code we had already changed. The status line
had a branch for "an update is available" and nothing in the codebase ever
created the flag it reads, so that branch was dead: there was no version check
anywhere, of any kind.

The flag is the missing piece. This writes it.

Four rules, because a version check that annoys people gets disabled and then we
are back to stale testers:

    ask rarely       once every six hours, behind a cached answer
    never block      two second timeout, off the request path, fail silent
    never nag        one line, only when actually behind, with the exact command
    always opt out   VB_NO_UPDATE_CHECK=1, or Claude Code's DISABLE_AUTOUPDATER

Nothing about the machine is sent. It is a GET of a public file.
"""

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from . import core

# The canonical published version, fetchable without auth.
MANIFEST_URL = ("https://raw.githubusercontent.com/cc-vb/voicebridge/main/"
                ".claude-plugin/plugin.json")
CACHE = core.STATE_DIR / "update_check.json"
INTERVAL = 6 * 3600        # seconds between real network checks
TIMEOUT = 2.0              # never make anybody wait longer than this


def _repo() -> Path:
    return Path(__file__).resolve().parent.parent


def local_version() -> str:
    """The version of the code that is actually running."""
    try:
        return json.loads((_repo() / ".claude-plugin" / "plugin.json")
                          .read_text()).get("version", "") or ""
    except Exception:
        return ""


def parse(v: str) -> tuple:
    """A comparable version. Numeric, because "2.10.0" < "2.9.0" as strings, and
    that comparison would tell everybody they are up to date forever."""
    out = []
    for part in str(v or "").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:4])


def disabled() -> bool:
    if os.environ.get("VB_NO_UPDATE_CHECK", "") == "1":
        return True
    # Honour Claude Code's own switch: somebody who turned auto-update off does
    # not want a second tool telling them about versions.
    return os.environ.get("DISABLE_AUTOUPDATER", "") == "1"


def _read_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _write_cache(d: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(d))
    except Exception:
        pass


def _fetch_latest() -> str:
    try:
        req = urllib.request.Request(
            MANIFEST_URL, headers={"User-Agent": "voicebridge-update-check"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read()).get("version", "") or ""
    except Exception:
        return ""


def check_latest(force: bool = False) -> dict:
    """{'local', 'latest', 'behind', 'checked_at'}. Never raises.

    Returns the cached answer unless it is older than INTERVAL, so this is
    cheap enough to call from a status line."""
    local = local_version()
    if disabled():
        return {"local": local, "latest": "", "behind": False, "checked_at": 0}
    cached = _read_cache()
    fresh = (not force
             and cached.get("latest")
             and time.time() - float(cached.get("checked_at") or 0) < INTERVAL)
    latest = cached.get("latest", "") if fresh else _fetch_latest()
    if not latest:
        # No network, or the fetch failed. Say what we knew, and do not write a
        # flag on the strength of a failed request.
        return {"local": local, "latest": cached.get("latest", ""),
                "behind": bool(cached.get("behind")),
                "checked_at": float(cached.get("checked_at") or 0)}
    behind = parse(latest) > parse(local)
    _write_cache({"local": local, "latest": latest, "behind": behind,
                  "checked_at": time.time()})
    # This is the flag the status line has always read and nothing ever wrote.
    try:
        if behind:
            core.UPDATE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            core.UPDATE_FLAG.write_text(latest)
        elif core.UPDATE_FLAG.exists():
            core.UPDATE_FLAG.unlink()
    except Exception:
        pass
    return {"local": local, "latest": latest, "behind": behind,
            "checked_at": time.time()}


def check_in_background() -> None:
    """Warm the cache without making anything wait for it."""
    if disabled():
        return
    import threading
    threading.Thread(target=lambda: check_latest(), daemon=True).start()


def install_kind() -> str:
    """'clone' or 'plugin'. They update by completely different means, and
    telling somebody the wrong one is worse than saying nothing."""
    return "clone" if (_repo() / ".git").is_dir() else "plugin"


def how_to_update() -> str:
    """The exact command for THIS install. One line, no choices to make."""
    if install_kind() == "clone":
        return "vb update"
    return ("/plugin marketplace update voicebridge, then /plugin install "
            "voicebridge@voicebridge")


def nudge() -> str:
    """The one line to show when behind, or "" when current.

    Uses only the cache, so it is safe on a status line: no network, no
    blocking, and silence when there is nothing to say."""
    if disabled():
        return ""
    c = _read_cache()
    if not c.get("behind"):
        return ""
    local, latest = c.get("local", "?"), c.get("latest", "?")
    return f"update {local} -> {latest}: {how_to_update()}"


def auto_update_hint() -> str:
    """Why a plugin tester is stale, and the one setting that fixes it.

    Claude Code turns auto-update ON by default only for Anthropic's own
    marketplaces. voicebridge is third-party, so plugin installs never update
    themselves unless this is switched on per marketplace. That single setting is
    the highest-leverage fix for the whole stale-tester problem, and it is not
    something we can do for them."""
    if install_kind() != "plugin":
        return ""
    return ("Third-party plugins do not auto-update by default. Turn it on "
            "once: /plugin, Marketplaces, voicebridge, Enable auto-update.")


def pull(quiet: bool = False) -> tuple:
    """Update a clone in place. Returns (ok, message)."""
    if install_kind() != "clone":
        return False, "This is a plugin install; " + how_to_update()
    before = local_version()
    try:
        r = subprocess.run(["git", "-C", str(_repo()), "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=90)
    except Exception as e:
        return False, f"git pull failed: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "git pull failed").strip()[:300]
    after = local_version()
    check_latest(force=True)     # clears the flag, so the nudge goes away now
    if after == before:
        return True, f"Already current at {after}."
    return True, f"Updated {before} -> {after}."

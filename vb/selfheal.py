"""Self-healing: detect and fix the handful of conditions that otherwise leave a
user stuck, without them ever running `vb doctor` (most never do).

Everything here is DETERMINISTIC and LOCAL: no AI, no network, nothing leaves
the machine, matching voicebridge's privacy posture. Every fix is idempotent and
no-ops when healthy, so it is safe to run on every session start. Two entry
points: `heal(apply=True)` (fix silently, used by the SessionStart hook) and
`report()` (describe without fixing, used by `vb doctor`).

Deliberately NOT here: anything racy or destructive as a silent background fix
(e.g. reaping a live tunnel). Those are surfaced by report()/doctor for the user
to act on. AI-assisted diagnosis and opt-in crash telemetry are separate,
opt-in layers, not this one.
"""

import os
import shutil
from pathlib import Path

from . import core

REPO = Path(__file__).resolve().parent.parent
SELF_VB = REPO / "bin" / "vb"

# "is X running" markers whose process, when dead, must not read as alive.
_PID_FILES = ("watch.pid", "talkd.pid", "tunnel.pid", "orb.pid", "tts.pid",
              "stt.pid", "call.pid", "skhd.pid", "speech.pid")


def _vb_path_ok() -> bool:
    """Does `vb` on PATH resolve to THIS install and run?"""
    p = shutil.which("vb")
    if not p:
        return False
    try:
        return (os.path.realpath(p) == os.path.realpath(str(SELF_VB))
                and os.access(p, os.X_OK))
    except Exception:
        return False


def _local_bin_on_path() -> bool:
    lb = str(Path.home() / ".local" / "bin")
    return lb in os.environ.get("PATH", "").split(os.pathsep)


def _fix_vb_symlink(apply: bool) -> str:
    """`vb` on PATH should resolve to this repo's executable. The live bug was a
    shared /opt/homebrew/bin/vb pointing at another user's stale plugin cache
    (permission denied). Fix per-user via ~/.local/bin/vb, never touching the
    shared link. Auto-fixable only when ~/.local/bin is actually on PATH."""
    if _vb_path_ok():
        return ""
    if not _local_bin_on_path():
        return ("`vb` on PATH does not point at this install; add ~/.local/bin "
                "to your PATH (or re-run install.sh) so it can be relinked")
    if not apply:
        return "`vb` on PATH is missing or points elsewhere (fixable: relink)"
    try:
        lb = Path.home() / ".local" / "bin"
        lb.mkdir(parents=True, exist_ok=True)
        link = lb / "vb"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(SELF_VB)
        return f"relinked ~/.local/bin/vb -> {SELF_VB}"
    except Exception as e:
        core.log(f"selfheal vb symlink: {e}")
        return ""


def _pid_dead(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return False
    except Exception:
        return True


def _reap_stale_pids(apply: bool) -> list:
    """Remove PID files whose process is dead. A stale marker makes 'is X
    running?' read true and can block a real start (mic never opens). Only
    reaps a DEAD pid; a live pid (even if reused) is left alone."""
    out = []
    for name in _PID_FILES:
        f = core.STATE_DIR / name
        try:
            pid = int(f.read_text().strip())
        except Exception:
            continue
        if _pid_dead(pid):
            if apply:
                try:
                    f.unlink()
                except OSError:
                    pass
            out.append(f"cleared stale {name} (pid {pid} not running)")
    return out


def heal(apply: bool = True) -> list:
    """Run the safe self-heals. Returns lines describing what was found/fixed.
    Idempotent and quiet when everything is healthy (returns [])."""
    lines = []
    msg = _fix_vb_symlink(apply)
    if msg:
        lines.append(msg)
    lines.extend(_reap_stale_pids(apply))
    return lines


def report() -> list:
    """Describe fixable conditions WITHOUT changing anything (for doctor)."""
    return heal(apply=False)

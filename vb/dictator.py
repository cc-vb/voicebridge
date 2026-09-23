"""The dictation key lives in its own product now. This finds it.

voicebridge speaks back to you. Dictator only listens: you hold a key, you
talk, the words land where the cursor is. Plenty of people want the second
thing without the first, which is why it was split out, and voicebridge uses
it rather than keeping a second copy that slowly drifts.

Everything here degrades honestly. If dictator is not installed, the commands
say so and say how to get it, instead of failing in a way that looks like the
key is broken.
"""
import os
import shutil
import subprocess
from pathlib import Path

from . import core

# Where to look, in order: whatever is on PATH, then the place the installer
# puts it, then next to this checkout for anyone working on both at once.
_PLACES = (
    Path.home() / "dictator" / "bin" / "dictator",
    Path(__file__).resolve().parent.parent.parent / "dictator" / "bin" / "dictator",
)

MISSING = (
    "The dictation key is a separate product called dictator.\n"
    "  Install it:  git clone https://github.com/KrishOjha1810/dictator.git ~/dictator\n"
    "               && ~/dictator/install.sh\n"
    "  Then:        dictator on"
)


def path() -> str:
    """The dictator command, or "" if it is not installed."""
    found = shutil.which("dictator")
    if found:
        return found
    for p in _PLACES:
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return ""


def installed() -> bool:
    return bool(path())


def run(args) -> int:
    """Hand a command straight through, or explain what is missing."""
    exe = path()
    if not exe:
        print(MISSING)
        return 1
    try:
        return subprocess.run([exe] + list(args)).returncode
    except Exception as e:
        core.log(f"dictator: could not run {exe}: {e}")
        print(f"Could not run dictator: {e}")
        return 1

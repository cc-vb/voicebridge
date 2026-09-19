"""Dictation that is simply always there.

A key you have to switch on with a command is a key you will forget you have.
The promise of hold-to-talk is that it works the way the keyboard works: you
press it and it does the thing, in any app, without having thought about it
beforehand. That means a login item, not a command.

macOS runs this through launchd, which also restarts it if it ever dies, which
matters more here than usual: a dictation key that silently stopped working is
indistinguishable from a broken keyboard.
"""
import os
import plistlib
import subprocess
from pathlib import Path

from . import core

LABEL = "com.voicebridge.dictate"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


REPO = Path(__file__).resolve().parent.parent
APP = Path.home() / "Applications" / "Voicebridge Dictation.app"


def _vb() -> str:
    return str(REPO / "bin" / "vb")


def build_app() -> str:
    """Build the .app that OWNS the permissions.

    Without it, macOS asks the user to approve something called "/usr/bin/env"
    and makes them add it through a file picker, because permissions are
    granted per application and a login item started by launchd is not the
    terminal they granted. With it, there is one entry called "Voicebridge
    Dictation", it asks for itself on first run, and there is nothing to set
    up. That is the entire reason this exists."""
    src = REPO / "native" / "app"
    if not (src / "main.swift").exists():
        return ""
    import shutil
    if not shutil.which("swiftc"):
        return ""
    macos = APP / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["swiftc", "-O", str(src / "main.swift"),
                        "-o", str(macos / "VoicebridgeDictation")],
                       check=True, capture_output=True, timeout=300)
        shutil.copy(src / "Info.plist", APP / "Contents" / "Info.plist")
        # Ad-hoc signing gives the bundle a stable identity, so the permission
        # the user grants survives a rebuild instead of being asked for again.
        subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(APP)],
                       capture_output=True, timeout=120)
        return str(APP)
    except Exception as e:
        core.log(f"dictation app build failed: {e}")
        return ""


def installed() -> bool:
    return PLIST.exists()


def running() -> bool:
    try:
        r = subprocess.run(["launchctl", "list", LABEL],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def on(key: str = "fn") -> str:
    """Install and start. Returns a message to print."""
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    log = str(core.STATE_DIR / "dictate.log")
    # Prefer the bundle: it carries its own permission entry, so the user is
    # asked once, by name, with a reason, and never has to find a checkbox.
    app = build_app()
    if app:
        args = [str(APP / "Contents" / "MacOS" / "VoicebridgeDictation")]
    else:
        args = ["/usr/bin/env", "python3", _vb(), "dictate", key]
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        # NOT --quiet. With no terminal attached this is the only record of
        # what happened, and "it silently did nothing" is the failure we are
        # most likely to hit here.
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        # launchd hands a process almost no PATH, and both sox and whisper live
        # in Homebrew. This is the single most common reason a thing that works
        # in a terminal does nothing as a login item.
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(Path.home()),
        },
    }
    # Clear any previous copy FIRST. Doing it after writing deletes the file
    # we just wrote, which is exactly what it did.
    off(quiet=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    try:
        subprocess.run(["launchctl", "load", "-w", str(PLIST)],
                       capture_output=True, timeout=15)
    except Exception as e:
        return f"wrote {PLIST} but could not start it: {e}"
    how = ("It will ask for the microphone and for Accessibility the first "
           "time, by name.\n" if app else
           "No app bundle (needs Xcode tools), so the permission prompt will "
           "name python3.\n")
    return (f"Dictation is on, and stays on after a restart.\n"
            f"  Hold {key} anywhere and talk.\n"
            f"  {how}"
            f"  Off:  vb always off\n"
            f"  Log:  {log}")


def off(quiet: bool = False) -> str:
    try:
        subprocess.run(["launchctl", "unload", str(PLIST)],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    try:
        PLIST.unlink()
    except Exception:
        pass
    # Leave nothing holding the microphone behind.
    # vb-orb too: leaving it behind means the next start finds it "already
    # running" and keeps a stale build on screen, which is how an orb that had
    # been fixed went on behaving like the broken one.
    for pat in ("bin/vb dictate", "vb-hotkey", "vb-orb"):
        try:
            subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=10)
        except Exception:
            pass
    return "" if quiet else "Dictation is off."


def status() -> str:
    if not installed():
        return "Dictation is not set to start on its own.  (vb always on)"
    return ("Dictation starts on login and is running now."
            if running() else
            "Dictation is installed but not running. Check: vb log")

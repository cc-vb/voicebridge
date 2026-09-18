"""Hold-to-talk on a single key.

The listener itself is a tiny Swift binary (native/hotkey.swift) because only
a CGEventTap can see the Fn/Globe key, and only an event tap gives us the key
RELEASE that hold-to-talk is built on. Our existing hotkeys go through skhd,
which fires on key-press only and so cannot do this at all.

It speaks a one-line-per-gesture protocol on stdout:

    READY <key>       armed
    DOWN              key down: warm the mic NOW
    UP <held_ms>      key up: stop and transcribe
    CANCEL <held_ms>  another key joined, or the press was too short to be
                      speech. Discard whatever was warmed.

                      Note the second case is NOT invisible to the user. A
                      release that short is a clean tap, which is exactly the
                      gesture macOS acts on, so with the Globe key set to Show
                      Emoji (the common default) an aborted press opens the
                      emoji palette. We are listen-only and cannot prevent
                      that. The floor decides only whether WE transcribe.
    LOCKED            the screen locked mid-hold
    BYE               exiting, any open hold already closed

Why hold-only, with no tap or double-tap gesture: macOS runs the Globe key's
own action (emoji picker, input-source switch) on the release of a clean TAP
and ignores a HOLD. So a hold-only design collides with nothing and needs no
change to System Settings, while any tap gesture would fight the OS.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import core

SRC = Path(__file__).resolve().parent.parent / "native" / "hotkey.swift"
BIN = core.STATE_DIR / "bin" / "vb-hotkey"

KEYS = ("fn", "rightcmd", "rightopt", "leftcmd")


def build(force: bool = False) -> str:
    """Compile the listener if needed. Returns a path, or "" with a reason
    logged. Rebuilds when the source is newer than the binary."""
    if not SRC.exists():
        core.log(f"hotkey: source missing at {SRC}")
        return ""
    if BIN.exists() and not force and BIN.stat().st_mtime >= SRC.stat().st_mtime:
        return str(BIN)
    if not shutil.which("swiftc"):
        core.log("hotkey: swiftc not found (install Xcode command line tools)")
        return ""
    BIN.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["swiftc", "-O", str(SRC), "-o", str(BIN)],
                       check=True, capture_output=True, timeout=180)
        return str(BIN)
    except subprocess.CalledProcessError as e:
        core.log(f"hotkey: build failed: {e.stderr.decode()[:400]}")
    except Exception as e:
        core.log(f"hotkey: build failed: {e}")
    return ""


def listen(key: str = "fn", min_hold_ms: int = 0):
    """Start the listener. Returns a Popen whose stdout yields the protocol

    min_hold_ms defaults to 0, i.e. no mis-press floor. A very short hold
    transcribes to nothing and inserts nothing, which is genuinely silent,
    whereas a floor buys nothing (the OS has already acted on the tap) and
    costs us a real utterance whenever someone speaks faster than the
    threshold. Raise it only with data.
    above, or None. The caller owns the process and MUST terminate it; the
    listener closes any open hold on SIGTERM so we can never be left believing
    the key is still down."""
    exe = build()
    if not exe:
        return None
    try:
        return subprocess.Popen(
            [exe, "--key", key, "--min-hold", str(min_hold_ms)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except Exception as e:
        core.log(f"hotkey: launch failed: {e}")
        return None


def measure(key: str = "fn", secs: int = 30) -> dict:
    """Run a self-serve trial so you can see the gestures land and learn your
    own natural hold duration. Prints as it goes; returns the summary."""
    p = listen(key)
    if not p:
        print("could not start the listener; see `vb log`")
        return {}

    holds, cancels = [], []
    deadline = time.time() + secs
    print(f"hold {key} and speak, as you normally would. {secs}s.\n")
    try:
        os.set_blocking(p.stdout.fileno(), False)
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                time.sleep(0.02)
                continue
            line = line.strip()
            if not line:
                continue
            print("  " + line)
            parts = line.split()
            if parts[0] == "UP" and len(parts) > 1:
                holds.append(int(parts[1]))
            elif parts[0] == "CANCEL" and len(parts) > 1:
                cancels.append(int(parts[1]))
            elif parts[0] == "READY":
                pass
    finally:
        p.terminate()
        try:
            p.wait(timeout=2)
        except Exception:
            p.kill()

    print()
    if holds:
        s = sorted(holds)
        print(f"real holds: n={len(s)}  shortest={s[0]}ms  "
              f"median={s[len(s)//2]}ms  longest={s[-1]}ms")
        print("  " + ", ".join(f"{x}ms" for x in s))
    else:
        print("no completed holds recorded (was the key actually held?)")
    if cancels:
        print(f"cancelled (chord or too short): {', '.join(f'{c}ms' for c in cancels)}")
    return {"holds": holds, "cancels": cancels}

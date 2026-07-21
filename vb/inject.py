"""voicebridge injection: put transcribed text into the focused app.

Uses a clipboard paste (Cmd+V) rather than simulated typing: pasting is
far more reliable for long text and doesn't drop characters. The previous
clipboard contents are saved and restored so we don't clobber them.

Requires macOS Accessibility permission for whatever runs this (your
terminal / Claude Code) the first time. macOS will prompt.
"""

import subprocess
import time

from . import core, oslayer


def _pbpaste() -> str:
    try:
        return subprocess.run(["pbpaste"], capture_output=True,
                              text=True).stdout
    except Exception:
        return ""


def _pbcopy(text: str) -> None:
    try:
        subprocess.run(["pbcopy"], input=text, text=True)
    except Exception as e:
        core.log(f"pbcopy failed: {e}")


def _osa(script: str) -> None:
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True)
        if r.returncode != 0:
            core.log(f"osascript rc={r.returncode}: {r.stderr.strip()}")
    except Exception as e:
        core.log(f"osascript failed: {e}")


def press_escape() -> None:
    """Send Escape to the focused app, Claude Code's own key for stopping
    the current generation. This is the REAL interrupt: not just muting the
    voice, but telling Claude to stop thinking, exactly what Esc does when
    you press it in the TUI yourself."""
    if not oslayer.IS_MAC:
        oslayer.press_escape()
        return
    _osa('tell application "System Events" to key code 53')


def press_enter() -> None:
    if not oslayer.IS_MAC:
        oslayer._win_sendkeys("{ENTER}") if oslayer.IS_WIN else oslayer._xdotool("key", "Return")
        return
    _osa('tell application "System Events" to key code 36')


def frontmost_app() -> str:
    """Name of the app that will receive the paste; logged for diagnosis."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first '
             'application process whose frontmost is true'],
            capture_output=True, text=True)
        return r.stdout.strip()
    except Exception:
        return ""


def paste_text(text: str, send: bool = False, expect_app: str = "") -> bool:
    """Paste text into the focused app; optionally press Return to send.

    `expect_app` is the app voice is bound to. If anything else is frontmost
    we refuse: a paste is a keystroke plus Return into whatever has the
    cursor, so an unchecked paste can type your speech into a meeting chat,
    a DM, or a terminal and run it. Returns True if the text was delivered."""
    if not text:
        return False
    if not oslayer.IS_MAC:      # Windows/Linux go through the OS layer
        oslayer.paste_text(text, send)
        return True
    front = frontmost_app()
    if expect_app and front and front.strip().casefold() != expect_app.casefold():
        core.log(f"paste refused: {front} is frontmost, not {expect_app}")
        return False
    core.log(f"paste -> frontmost app: {front or 'unknown'}")
    saved = _pbpaste()
    _pbcopy(text)
    time.sleep(0.05)
    _osa('tell application "System Events" to keystroke "v" using command down')
    time.sleep(0.15)
    if send:
        _osa('tell application "System Events" to key code 36')  # Return
    # Restore the old clipboard after the paste has landed.
    time.sleep(0.2)
    _pbcopy(saved)
    return True

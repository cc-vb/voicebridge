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


def _activate_app(name: str) -> bool:
    """Bring a specific app (by its process name, what frontmost_app returns)
    to the front, so a keystroke/paste lands in IT and not in whatever the
    user is looking at. Returns False if it couldn't be fronted."""
    name = (name or "").strip()
    if not name:
        return False
    esc = name.replace("\\", "\\\\").replace('"', '\\"')
    try:
        r = subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set frontmost of '
             f'process "{esc}" to true'],
            capture_output=True, text=True, timeout=4)
        return r.returncode == 0
    except Exception as e:
        core.log(f"activate {name} failed: {e}")
        return False


def press_escape() -> None:
    """Send Escape to the focused app, Claude Code's own key for stopping
    the current generation. This is the REAL interrupt: not just muting the
    voice, but telling Claude to stop thinking, exactly what Esc does when
    you press it in the TUI yourself."""
    if not oslayer.IS_MAC:
        oslayer.press_escape()
        return
    _osa('tell application "System Events" to key code 53')


def press_shift_tab(expect_app: str = "") -> bool:
    """Send Shift+Tab, Claude Code's key to CYCLE the permission mode
    (normal -> auto-accept edits -> plan -> ...). Delivered to the bound
    terminal even from another app via activate-restore. Returns delivered."""
    if not oslayer.IS_MAC:
        return False
    restore = ""
    if expect_app:
        front = frontmost_app()
        if front.strip().casefold() != expect_app.casefold():
            if not _activate_app(expect_app):
                return False
            restore = front
            time.sleep(0.18)
    _osa('tell application "System Events" to key code 48 using shift down')
    if restore:
        time.sleep(0.05)
        _activate_app(restore)
    return True


def press_enter(expect_app: str = "") -> bool:
    """Press Return. With expect_app, refuse unless that app is frontmost,
    a blind Return from the phone's Allow button once went to whatever
    window happened to be focused, the dialog stayed up, and the "Claude
    needs your input" nag looped forever. Returns whether it was sent."""
    restore = ""
    if expect_app:
        front = frontmost_app()
        if front.strip().casefold() != expect_app.casefold():
            # Bring the terminal forward for the keystroke, then restore, so a
            # permission "yes" lands on the dialog even from another app.
            if not _activate_app(expect_app):
                return False
            restore = front
            time.sleep(0.18)
    if not oslayer.IS_MAC:
        oslayer._win_sendkeys("{ENTER}") if oslayer.IS_WIN else oslayer._xdotool("key", "Return")
        return True
    _osa('tell application "System Events" to key code 36')
    if restore:
        time.sleep(0.05)
        _activate_app(restore)
    return True


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


def _claude_terminal_tty() -> str:
    """The tty of a claude TUI that lives in a Terminal.app tab, for the
    single-session common case. '' if not exactly one (caller falls back)."""
    try:
        ps = subprocess.run(["ps", "-axo", "tty=,comm="],
                            capture_output=True, text=True, timeout=4).stdout
        claude = set()
        for ln in ps.splitlines():
            p = ln.split()
            if p and p[0] not in ("??", "?") and p[-1].endswith("claude"):
                claude.add("/dev/" + p[0])
        tabs = subprocess.run(
            ["osascript", "-e",
             'tell application "Terminal" to get tty of every tab of every window'],
            capture_output=True, text=True, timeout=4).stdout
        term = {t.strip() for t in tabs.replace("\n", ",").split(",")
                if t.strip().startswith("/dev/")}
        both = list(claude & term)
        return both[0] if len(both) == 1 else ""
    except Exception:
        return ""


def _terminal_inject(text: str, tty: str) -> bool:
    """Type text (and submit) into the Terminal tab on `tty` WITHOUT bringing
    Terminal to the front, so a phone prompt lands in the session while the
    user stays on YouTube/Slack, no focus flicker at all. `do script ... in
    <tab>` writes to that tab's tty as if typed; Claude Code reads it as input
    and the trailing return submits it."""
    esc = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    script = (
        'tell application "Terminal"\n'
        'repeat with w in windows\n'
        'repeat with t in tabs of w\n'
        f'if tty of t is "{tty}" then\n'
        f'do script "{esc}" in t\n'
        'return "ok"\n'
        'end if\n'
        'end repeat\n'
        'end repeat\n'
        'end tell\n'
        'return "no"')
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=6)
        return "ok" in r.stdout
    except Exception as e:
        core.log(f"terminal focus-free inject failed: {e}")
        return False


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
    target = (expect_app or "").strip()
    # FOCUS-FREE fast path: if the bound app is Terminal, write straight into
    # the session's tab by tty, no activation, no flicker, and it can't hit
    # the wrong window. Only when submitting (the phone always submits).
    if send and target.casefold() == "terminal":
        tty = _claude_terminal_tty()
        if tty and _terminal_inject(text, tty):
            core.log(f"focus-free inject -> Terminal {tty}")
            return True
        # else fall through to the activate-restore path below
    restore = ""
    if target and front and front.strip().casefold() != target.casefold():
        # Deliver to the BOUND terminal even though the user is in another app
        # (YouTube, Slack, ...). Bring the terminal to the front, paste, then
        # restore whatever they were looking at, so the prompt always lands in
        # the session and never in the wrong window, and they never have to
        # open the terminal themselves.
        if not _activate_app(target):
            core.log(f"paste: couldn't front {target} (was {front}); refusing")
            return False
        restore = front
        time.sleep(0.18)   # let the activation settle before the paste
    core.log(f"paste -> {target or front or 'frontmost'}"
             + (f" (restoring {restore})" if restore else ""))
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
    if restore:
        time.sleep(0.05)
        _activate_app(restore)   # hand focus back to the user's app
    return True

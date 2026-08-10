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


# The apps a prompt may land in when nothing was named. Everything else, and
# especially a browser or a chat window, is refused: that is how speech ends up
# in a DM.
_TERMINALS = ("terminal", "iterm", "ghostty", "alacritty", "kitty", "wezterm",
              "warp", "hyper", "tabby", "rio")


def _is_terminal(app: str) -> bool:
    low = (app or "").strip().casefold()
    return bool(low) and any(t in low for t in _TERMINALS)


def _osa_out(script: str, timeout: float = 8.0) -> str:
    """Run AppleScript and return its output. _osa discards it, which is fine
    for keystrokes and useless for anything that has to be CHECKED."""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception as e:
        core.log(f"osascript failed: {e}")
        return ""


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


def press_escape(expect_app: str = "", target_tty: str = "") -> bool:
    """Send Escape to a NAMED target: Claude Code's own key for stopping.

    This used to be unguarded, so it went to whatever had the cursor. Escape in
    the wrong place is not harmless: it closes dialogs, discards what somebody
    was typing, and leaves the agent you meant to stop still running while you
    believe you stopped it.

    Returns whether it was delivered, so a caller can say "I couldn't stop it"
    instead of reporting success it did not have."""
    if not oslayer.IS_MAC:
        oslayer.press_escape()
        return True
    target = (expect_app or "").strip()
    tty = (target_tty or "").strip()
    if not target and not tty:
        core.log("escape: refusing, no target app or tty was given")
        return False
    front = frontmost_app()
    restore = ""
    if tty:
        # Raise the exact tab first, so Escape cannot land in another window of
        # the same app. Checking the app name alone let the wrong tab pass.
        if not _select_tty(tty):
            core.log(f"escape: refusing, couldn't select tty {tty}")
            return False
        if front and target and front.strip().casefold() != target.casefold():
            restore = front
    elif front and front.strip().casefold() != target.casefold():
        if not _activate_app(target):
            core.log(f"escape: couldn't front {target} (was {front}); refusing")
            return False
        restore = front
        time.sleep(0.18)
    _osa('tell application "System Events" to key code 53')
    if restore:
        time.sleep(0.12)
        _activate_app(restore)
    return True


def _select_tty(tty: str) -> bool:
    """Bring the terminal tab on `tty` to the front. True if it was found."""
    esc = tty.replace('"', '\\"')
    out = _osa_out(f'''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "{esc}" then
        set selected tab of w to t
        set index of w to 1
        activate
        return "ok"
      end if
    end repeat
  end repeat
end tell
return "no"''')
    if "ok" in (out or ""):
        return True
    out = _osa_out(f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with sn in sessions of t
        if tty of sn is "{esc}" then
          select t
          select w
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "no"''')
    return "ok" in (out or "")


def press_shift_tab(expect_app: str = "") -> bool:
    """Send Shift+Tab, Claude Code's key to CYCLE the permission mode
    (normal -> auto-accept edits -> plan -> ...). Delivered to the bound
    terminal even from another app via activate-restore. Returns delivered."""
    if not oslayer.IS_MAC:
        # Was a hard no-op off-Mac (mode never cycled); send Shift+Tab via the
        # OS layer instead.
        if oslayer.IS_WIN:
            oslayer._win_sendkeys("+{TAB}")
        else:
            oslayer._xdotool("key", "shift+Tab")
        return True
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
    if not oslayer.IS_MAC:
        # The expect_app focus guard below is osascript-only (Mac). Off-Mac it
        # used to run FIRST and always fail (osascript absent), so the phone's
        # "yes" never delivered Return. Deliver via the OS layer, best-effort.
        if oslayer.IS_WIN:
            oslayer._win_sendkeys("{ENTER}")
        else:
            oslayer._xdotool("key", "Return")
        return True
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


def _terminal_inject(text: str, tty: str, submit: bool = True) -> bool:
    """Type text (and submit) into the Terminal tab on `tty` WITHOUT bringing
    Terminal to the front, so a phone prompt lands in the session while the
    user stays on YouTube/Slack, no focus flicker at all. `do script ... in
    <tab>` writes to that tab's tty as if typed; Claude Code reads it as input
    and the trailing return submits it."""
    esc = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    # Two steps: `do script` types the prompt (Claude Code takes bulk input as
    # a PASTE and does NOT submit on the embedded return, the "typed but I had
    # to press Enter" bug), then a lone return actually sends it.
    script = (
        'tell application "Terminal"\n'
        'repeat with w in windows\n'
        'repeat with t in tabs of w\n'
        f'if tty of t is "{tty}" then\n'
        f'do script "{esc}" in t\n'
        + ('delay 0.35\ndo script "" in t\n' if submit else '')
        + 'return "ok"\n'
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


def paste_text(text: str, send: bool = False, expect_app: str = "",
               target_tty: str = "") -> bool:
    """Paste text into the focused app; optionally press Return to send.

    `expect_app` is the app voice is bound to. If anything else is frontmost
    we refuse: a paste is a keystroke plus Return into whatever has the
    cursor, so an unchecked paste can type your speech into a meeting chat,
    a DM, or a terminal and run it. Returns True if the text was delivered."""
    if not text:
        return False
    if not oslayer.IS_MAC:      # Windows/Linux go through the OS layer
        return oslayer.paste_text(text, send)   # real success, not blind True
    front = frontmost_app()
    target = (expect_app or "").strip()
    # With no app and no tty there was no guard at all, only the docstring:
    # every caller that had not been given a target pasted into whatever had the
    # cursor and pressed Return. Two callers were in exactly that state.
    #
    # A flat refusal would break the legitimate case, which is `vb talk` while
    # you are sitting in the terminal. So the rule is narrower: with nothing
    # named, deliver only into a TERMINAL, never into a browser or a chat app.
    # If a tty can be worked out for the tab you are looking at, use it, because
    # that is exact rather than "whatever is in front".
    if not target and not (target_tty or "").strip():
        if not _is_terminal(front):
            core.log(f"paste: refusing, nothing was named and {front or 'that'} "
                     f"is not a terminal")
            return False
        try:
            from . import signals
            target_tty = signals.frontmost_tty() or ""
        except Exception:
            target_tty = ""
        core.log(f"paste: no target named, delivering to the focused terminal "
                 f"{front}" + (f" ({target_tty})" if target_tty else ""))
    # FOCUS-FREE fast path: if the bound app is Terminal, write straight into
    # the session's tab by tty, no activation, no flicker, and it can't hit
    # the wrong window. Only when submitting (the phone always submits).
    # target_tty (the SELECTED session's tty, from OWNERS) is preferred so a
    # prompt lands in THE RIGHT session's tab when several are open; else fall
    # back to "the one claude tab" heuristic.
    tty = (target_tty or "").strip()
    if tty or (send and target.casefold() == "terminal"):
        tty = tty or _claude_terminal_tty()
        if tty and _terminal_inject(text, tty, submit=send):
            core.log(f"focus-free inject -> Terminal {tty}"
                     + (" (session-targeted)" if target_tty else ""))
            return True
        # A named tty that could not be written to must NOT become a blind
        # paste into the frontmost window: the whole point of knowing the tty is
        # that a second tab of the same app is a different session. The guard
        # checked the app NAME, so the wrong tab passed it.
        if (target_tty or "").strip():
            core.log(f"paste: refusing, couldn't reach tty {target_tty} and "
                     f"will not type into whatever is in front instead")
            return False
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

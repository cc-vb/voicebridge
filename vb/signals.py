"""Reading the room: the real-world signals the attention engine gates on.

Separated from attention.py on purpose. The decision logic there is pure and
exhaustively tested; the messy part, asking macOS what is going on, lives here
and is allowed to fail. Every signal degrades to the SAFE answer when it cannot
be read, and "safe" always means the same thing: assume we may speak, but never
assume the user is free when we genuinely cannot tell.

Signals, and how reliable each one is on a normal Mac:
  focused_sid    solid   frontmost terminal tab's tty, mapped back to a session
  typed_at       solid   recorded by the prompt hook when you type to an agent
  mic_busy       solid   voicebridge's own mic/session state
  user_speaking  solid   the live call's speaking state
  dnd            partial reading macOS Focus needs Full Disk Access; without it
                         we simply never claim you are in Do Not Disturb
"""

import json
import subprocess
import time

from . import core

TYPED_FILE = core.STATE_DIR / "typed_at.json"
_MAX_TYPED = 40


def note_typed(sid: str) -> None:
    """Record that the user just typed at this agent themselves. The engine
    uses it to stay quiet about an agent you are visibly working in."""
    if not sid:
        return
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        d = {}
        if TYPED_FILE.exists():
            d = json.loads(TYPED_FILE.read_text())
        d[sid] = time.time()
        if len(d) > _MAX_TYPED:      # keep only the most recent sessions
            d = dict(sorted(d.items(), key=lambda kv: kv[1])[-_MAX_TYPED:])
        TYPED_FILE.write_text(json.dumps(d))
    except Exception:
        pass


def typed_at() -> dict:
    try:
        return {k: float(v) for k, v in json.loads(TYPED_FILE.read_text()).items()}
    except Exception:
        return {}


def frontmost_tty() -> str:
    """The tty of the terminal tab you are actually looking at, or ''.

    Only meaningful when a terminal is frontmost: if you are in a browser, we
    return '' and the engine correctly concludes you are not watching any
    agent."""
    if not _is_mac():
        return ""
    try:
        from . import inject
        front = (inject.frontmost_app() or "").lower()
        if "terminal" not in front and "iterm" not in front:
            return ""
        if "iterm" in front:
            script = ('tell application "iTerm2" to get tty of current '
                      'session of current window')
        else:
            script = ('tell application "Terminal" to get tty of selected '
                      'tab of front window')
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=3)
        out = (r.stdout or "").strip()
        return out if out.startswith("/dev/") else ""
    except Exception:
        return ""


def focused_sid(sids) -> str:
    """Which of these sessions you are currently looking at, or ''."""
    tty = frontmost_tty()
    if not tty:
        return ""
    try:
        from . import talkd
        for sid in sids:
            if talkd.tty_for_sid(sid) == tty:
                return sid
    except Exception:
        pass
    return ""


def dnd_on() -> bool:
    """True only when we can PROVE Do Not Disturb is on.

    Reading macOS Focus state needs Full Disk Access, which most users will not
    have granted. Rather than nag for a permission just to stay quiet, we treat
    unreadable as "not in DND" and rely on the other gates (mic in use, hourly
    budget, focused agent) to keep us polite."""
    try:
        import pathlib
        p = pathlib.Path.home() / "Library/DoNotDisturb/DB/Assertions.json"
        data = json.loads(p.read_text())
        rows = (data.get("data") or [{}])[0]
        return bool(rows.get("storeAssertionRecords"))
    except Exception:
        return False


def mic_busy() -> bool:
    """True while the microphone is ours (a live call or a voiced session), so
    we never talk into an open mic."""
    try:
        return bool(core.mic_active() or core.call_live())
    except Exception:
        return False


def user_speaking() -> bool:
    """True while the person is mid-utterance, so we never talk over them.

    Two faults lived here and both failed silently, which is the worst way for
    a gate like this to break: it reports "nobody is talking" and we speak over
    someone. `core.get_hud` has never existed (it is `read_hud`), so the whole
    body was swallowed by the except and this was permanently False. And the
    phase test only accepted `listening`, which is the mic-open-but-quiet state,
    so the one phase that literally means "we can hear them right now" was the
    one it rejected.
    """
    try:
        hud = core.read_hud()
        phase = str(hud.get("phase", ""))
        if phase == "hearing":
            return True                       # loud enough to have crossed the bar
        # Below the `hearing` threshold but not silent: they have just started.
        return phase == "listening" and float(hud.get("level", 0)) > 0.02
    except Exception:
        return False


def _is_mac() -> bool:
    try:
        from . import oslayer
        return oslayer.IS_MAC
    except Exception:
        return False


def gather(sids=(), scores=None, spoken_at=()) -> dict:
    """One context snapshot for the attention engine, from the live machine."""
    from . import attention
    return attention.context(
        now=time.time(),
        focused_sid=focused_sid(sids),
        mic_busy=mic_busy(),
        user_speaking=user_speaking(),
        dnd=dnd_on(),
        spoken_at=list(spoken_at),
        typed_at=typed_at(),
        scores=scores if scores is not None else attention.load_scores(),
        quiet=attention.is_quiet(),
    )

"""voicebridge agent adapters: make the voice layer universal.

Voice INPUT is already agent-agnostic, we paste into whatever text input
is focused, so talking to Codex, Cursor, Cline, or Copilot works today.
The only agent-specific part is reading the reply back to speak it. This
module isolates that behind a tiny adapter so new agents are a few lines,
not a rewrite.

An adapter is: given a "source" (a transcript/log path), return the latest
assistant reply text. Claude Code is implemented (JSONL transcript). Others
that write a readable log can use the generic line/paragraph adapter by
registering a path glob in ~/.voicebridge/agents.json:

    { "codex": {"glob": "~/.codex/sessions/*.jsonl", "kind": "jsonl_text"},
      "aider": {"glob": "~/.aider/*.md",           "kind": "lastpara"} }

Kinds: "claude" (Claude Code JSONL), "jsonl_text" (any JSONL, concatenate
text fields of the last assistant-ish record), "lastpara" (last paragraph
of a text file). Add more kinds here as agents are verified.
"""

import json
import os
from pathlib import Path

from . import core

CONFIG = core.STATE_DIR / "agents.json"


def _load_agents() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return {}


def last_reply(path: str, kind: str = "claude") -> str:
    """Latest assistant reply from a source, per adapter kind."""
    if kind == "claude":
        return core.last_assistant_text(path)
    if kind == "jsonl_text":
        return _jsonl_text(path)
    if kind == "lastpara":
        return _lastpara(path)
    return ""


def _jsonl_text(path: str) -> str:
    """Generic JSONL: walk from the end, return concatenated text of the
    last record that has any text content (works for many agent logs)."""
    try:
        lines = Path(path).read_text(errors="ignore").splitlines()
    except Exception:
        return ""
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        # skip obvious user turns
        role = rec.get("role") or rec.get("type") or ""
        if str(role).lower() in ("user", "human"):
            continue
        text = _dig_text(rec)
        if text.strip():
            return text.strip()
    return ""


def _dig_text(obj) -> str:
    """Pull human-readable text out of an arbitrary record."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("text", "content", "message", "output", "response"):
            if k in obj:
                return _dig_text(obj[k])
    if isinstance(obj, list):
        return " ".join(_dig_text(x) for x in obj)
    return ""


def _lastpara(path: str) -> str:
    try:
        txt = Path(path).read_text(errors="ignore").strip()
    except Exception:
        return ""
    return txt.split("\n\n")[-1].strip() if txt else ""


def discover() -> list:
    """Sessions from registered non-Claude agents (best-effort). Each:
    {label, path, kind, mtime}. Claude sessions come from sessions.roster()."""
    rows = []
    for name, cfg in _load_agents().items():
        glob = os.path.expanduser(cfg.get("glob", ""))
        kind = cfg.get("kind", "jsonl_text")
        if not glob:
            continue
        import glob as _g
        for p in _g.glob(glob):
            try:
                rows.append({"label": name, "path": p, "kind": kind,
                             "mtime": os.path.getmtime(p)})
            except OSError:
                pass
    return rows


# --------------------------------------------------------------------------
# Control seam: how voicebridge DELIVERS to and CONTROLS a target.
#
# The functions above are the READ side (get the reply out of an agent's log),
# already agent-agnostic. The classes below are the matching WRITE/control side
# (send a prompt, interrupt, approve, cycle mode), which was still hard-wired to
# Claude Code at the call sites. Together the two halves are the whole "talk to
# any agent" boundary. Today the only control target is Claude Code in a
# terminal, so there is one adapter. A second target (Codex, Google Antigravity,
# another agent, or Friday) slots in by adding a class, registering its `kind`,
# and teaching kind_for_sid, no call site changes. Adapters lazily import
# inject/talkd inside methods to avoid an import cycle.
# --------------------------------------------------------------------------


class TargetAdapter:
    """How voicebridge controls one kind of agent target. Every method takes
    the session id so an adapter can address the right instance when several
    are open. Returns True on success unless noted."""

    kind = "target"
    reply_kind = "claude"   # how last_reply() should read this target's output

    def send(self, sid: str, text: str) -> bool:
        """Deliver a prompt to the target session and submit it."""
        raise NotImplementedError

    def read_state(self, sid: str, transcript: str = "") -> str:
        """Coarse session state: 'working', 'idle', or similar."""
        raise NotImplementedError

    def interrupt(self, sid: str) -> bool:
        """Stop the target's current generation."""
        raise NotImplementedError

    def approve(self, sid: str) -> bool:
        """Answer a blocking permission prompt affirmatively."""
        raise NotImplementedError

    def decline(self, sid: str) -> bool:
        """Dismiss a blocking permission prompt."""
        raise NotImplementedError

    def cycle_mode(self, sid: str, n: int = 1) -> bool:
        """Advance the target's mode selector n steps (e.g. permission mode)."""
        raise NotImplementedError


class ClaudeCodeAdapter(TargetAdapter):
    """Claude Code running in a terminal. Delivery is a focus-free paste into
    the session's own tab (by tty, via the OWNERS registry) so a prompt lands
    in the selected session even with several open; interrupt/approve/mode are
    the same keystrokes you would press in the TUI yourself."""

    kind = "claude-code"
    reply_kind = "claude"

    def send(self, sid: str, text: str) -> bool:
        from . import inject
        from .talkd import bound_app, tty_for_sid
        return inject.paste_text(text, send=True, expect_app=bound_app(),
                                 target_tty=tty_for_sid(sid))

    def read_state(self, sid: str, transcript: str = "") -> str:
        return core.active_session_state(transcript) if transcript else "idle"

    def interrupt(self, sid: str) -> bool:
        from . import inject
        inject.press_escape()
        return True

    def approve(self, sid: str) -> bool:
        from . import inject
        from .talkd import bound_app
        return inject.press_enter(expect_app=bound_app())

    def decline(self, sid: str) -> bool:
        from . import inject
        inject.press_escape()
        return True

    def cycle_mode(self, sid: str, n: int = 1) -> bool:
        from . import inject
        from .talkd import bound_app
        import time as _t
        ok = True
        n = max(0, n)   # n == 0 is a valid no-op (target mode already current)
        for i in range(n):
            ok = inject.press_shift_tab(expect_app=bound_app()) and ok
            if i + 1 < n:
                _t.sleep(0.18)   # let each Shift+Tab register before the next
        return ok


# The registry is the extension point: map a target `kind` to its adapter.
_REGISTRY = {ClaudeCodeAdapter.kind: ClaudeCodeAdapter()}
_DEFAULT_KIND = ClaudeCodeAdapter.kind


def kind_for_sid(sid: str) -> str:
    """Which kind of control target this session is. Every session is Claude
    Code today; this is where a per-session target marker gets read once other
    adapters exist, so the choice becomes data, not a change at the call site."""
    return _DEFAULT_KIND


def for_sid(sid: str) -> TargetAdapter:
    """The control adapter for a session's target."""
    return _REGISTRY.get(kind_for_sid(sid), _REGISTRY[_DEFAULT_KIND])

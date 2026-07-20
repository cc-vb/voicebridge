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

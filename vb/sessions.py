"""voicebridge sessions: see and steer ALL your Claude Code sessions by voice.

Reads the transcripts under ~/.claude/projects to build a live roster, which
project, how long since it moved, and whether it looks idle (waiting for you)
or still working. Lets you ask "which agents need me?" and "switch to
jobhunt" out loud, so one voice controls a fleet of sessions.
"""

import json
import os
import time
from pathlib import Path

from . import core

PROJECTS = Path(os.path.expanduser("~/.claude/projects"))


def _label(transcript_path: str) -> str:
    """Human name for a session from its encoded project dir.
    .../projects/-Users-me-jobhunt/<uuid>.jsonl  ->  'jobhunt'."""
    try:
        d = Path(transcript_path).parent.name  # -Users-me-jobhunt
        seg = [s for s in d.split("-") if s]
        return seg[-1] if seg else "home"
    except Exception:
        return "session"


def _last_records(path: str, n: int = 12) -> list:
    try:
        lines = Path(path).read_text(errors="ignore").splitlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in lines:
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _state(records: list) -> str:
    """idle = last thing is an assistant reply (your turn); working = a user
    prompt or tool call is the latest (Claude's turn)."""
    for rec in reversed(records):
        t = rec.get("type")
        if t == "assistant":
            msg = rec.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                if any(isinstance(b, dict) and b.get("type") == "tool_use"
                       for b in content):
                    return "working"
            return "idle"
        if t == "user":
            return "working"
    return "idle"


def _ago(secs: float) -> str:
    s = int(secs)
    if s < 60:
        return f"{s} seconds ago"
    if s < 3600:
        return f"{s // 60} minute{'s' if s // 60 != 1 else ''} ago"
    return f"{s // 3600} hour{'s' if s // 3600 != 1 else ''} ago"


def roster(max_age_h: float = 12.0, limit: int = 12) -> list:
    """Active sessions, most-recent first. Each: label, sid, path, mtime,
    ago, state, voiced."""
    from .talkd import VOICED
    voiced = set()
    try:
        voiced = {f.name for f in VOICED.iterdir()}
    except Exception:
        pass
    now = time.time()
    rows = []
    try:
        for f in PROJECTS.glob("*/*.jsonl"):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if now - m > max_age_h * 3600:
                continue
            recs = _last_records(str(f))
            if not recs:
                continue
            rows.append({
                "label": _label(str(f)),
                "sid": f.stem,
                "path": str(f),
                "mtime": m,
                "ago": _ago(now - m),
                "state": _state(recs),
                "voiced": f.stem in voiced,
            })
    except Exception as e:
        core.log(f"roster failed: {e}")
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


def speak_roster() -> str:
    """A spoken-friendly summary of the fleet."""
    rows = roster()
    if not rows:
        return "No active Claude sessions in the last several hours."
    idle = [r for r in rows if r["state"] == "idle"]
    working = [r for r in rows if r["state"] == "working"]
    parts = [f"{len(rows)} session{'s' if len(rows) != 1 else ''}."]
    if idle:
        names = ", ".join(r["label"] for r in idle[:5])
        parts.append(f"Waiting for you: {names}.")
    if working:
        names = ", ".join(r["label"] for r in working[:5])
        parts.append(f"Still working: {names}.")
    return " ".join(parts)


def find(query: str):
    """Match a spoken name to a session (fuzzy-ish substring)."""
    q = query.lower().strip()
    rows = roster(max_age_h=72)
    for r in rows:
        if r["label"].lower() == q:
            return r
    for r in rows:
        if q in r["label"].lower() or r["label"].lower() in q:
            return r
    return None


def switch(query: str) -> str:
    """Move voice to the named session (rebind the exclusive voiced session)."""
    from . import talkd
    r = find(query)
    if not r:
        return f"No session matching '{query}'."
    talkd.VOICED.mkdir(parents=True, exist_ok=True)
    for f in talkd.VOICED.iterdir():
        try:
            f.unlink()
        except OSError:
            pass
    (talkd.VOICED / r["sid"]).write_text(r["path"])
    talkd.ACTIVE.write_text(json.dumps(
        {"session_id": r["sid"], "transcript_path": r["path"],
         "ts": time.time()}))
    return f"Voice moved to {r['label']}."

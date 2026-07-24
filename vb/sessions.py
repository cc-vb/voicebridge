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


_HOMEISH = {"home", "", "documents", "desktop", "downloads"}


def _label(transcript_path: str) -> str:
    """Human name for a session from its encoded project dir. Falls back to
    the session's first user prompt when the dir is a generic home folder,
    so two sessions in ~ don't both read as your username."""
    try:
        d = Path(transcript_path).parent.name  # -Users-me-jobhunt
        seg = [s for s in d.split("-") if s]
        name = seg[-1] if seg else "home"
        # Home-ish dir (or your username) -> use the first prompt instead.
        low = name.lower()
        if low in _HOMEISH or (len(seg) >= 2 and low == seg[-1] and
                               name == Path.home().name):
            snip = _first_prompt(transcript_path)
            if snip:
                return snip
        return name
    except Exception:
        return "session"


def _first_prompt(path: str, words: int = 4) -> str:
    """First few words of the session's first user message, as a label."""
    try:
        for ln in Path(path).read_text(errors="ignore").splitlines():
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if rec.get("type") == "user":
                c = rec.get("message", {}).get("content", "")
                if isinstance(c, list):
                    c = " ".join(b.get("text", "") for b in c
                                 if isinstance(b, dict))
                c = str(c).strip().replace("\n", " ")
                if c and not c.startswith("<"):
                    return " ".join(c.split()[:words]).lower()
    except Exception:
        pass
    return ""


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
    return mark_active(rows[:limit], _live_cwds())


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


def all_rows(max_age_h: float = 72) -> list:
    """Claude sessions plus any registered non-Claude agents (universal)."""
    from . import adapters
    import time as _t
    rows = roster(max_age_h=max_age_h)
    for a in adapters.discover():
        rows.append({
            "label": a["label"], "sid": a["path"], "path": a["path"],
            "kind": a["kind"], "mtime": a["mtime"],
            "ago": _ago(_t.time() - a["mtime"]),
            "state": "working" if _t.time() - a["mtime"] < 15 else "idle",
            "voiced": False,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def find(query: str):
    """Match a spoken name to a session or registered agent."""
    q = query.lower().strip()
    rows = all_rows()
    for r in rows:
        if r["label"].lower() == q:
            return r
    for r in rows:
        if q in r["label"].lower() or r["label"].lower() in q:
            return r
    return None


def read_last(query: str) -> str:
    """Speak a named session's most recent assistant reply."""
    r = find(query)
    if not r:
        return f"No session matching '{query}'."
    from . import adapters
    reply = adapters.last_reply(r["path"], r.get("kind", "claude"))
    if not reply:
        return f"{r['label']} has no reply yet."
    return f"{r['label']} said: {reply}"


def newly_idle(prev: dict, exclude_sid: str = "") -> "tuple[list, dict]":
    """Given prior {sid: state}, return (labels that just went idle, new map).
    Used by the daemon to announce 'X is ready for you' when an OTHER agent
    finishes, the fleet-supervision payoff.

    exclude_sid is the session you're actively voiced in: never announce it.
    Its reply is already spoken to you, and without this the daemon said
    "Heads up: <this session> is ready" after every single reply."""
    now = {}
    freshly = []
    for r in roster():
        now[r["sid"]] = r["state"]
        if r["sid"] == exclude_sid:
            continue
        if r["state"] == "idle" and prev.get(r["sid"]) == "working":
            freshly.append(r["label"])
    return freshly, now


def _live_cwds() -> dict:
    """{cwd: count} of RUNNING Claude Code processes (each open session is a
    process named `claude`; lsof gives its working directory). This is the
    real 'can I talk to it?' signal: closed sessions leave transcripts behind
    but no process."""
    import subprocess as _sp
    cwds = {}
    try:
        # ps, not pgrep: Claude Code rewrites its process identity in a way
        # pgrep cannot match on macOS (verified: ps sees comm "claude",
        # pgrep -x/-f "claude" both return nothing).
        out = _sp.run(["ps", "-axo", "pid=,comm="], capture_output=True,
                      text=True).stdout
        pids = [ln.split(None, 1)[0] for ln in out.splitlines()
                if ln.strip() and ln.split(None, 1)[-1].strip()
                .split("/")[-1] == "claude"]
        for pid in pids[:16]:
            r = _sp.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                        capture_output=True, text=True, timeout=3)
            for ln in r.stdout.splitlines():
                if ln.startswith("n"):
                    cwds[ln[1:]] = cwds.get(ln[1:], 0) + 1
    except Exception:
        pass
    return cwds


def _munge(cwd: str) -> str:
    """A cwd as Claude Code names its project dir (/Users/x/y -> -Users-x-y)."""
    return cwd.replace("/", "-").replace(".", "-")


def mark_active(rows: list, live: dict) -> list:
    """Set row['active']: within each project dir that has N live claude
    processes, the N most recently touched transcripts are the open sessions
    (best possible mapping; a dir's sessions share one munged name)."""
    budget = {_munge(c): n for c, n in live.items()}
    for r in sorted(rows, key=lambda r: r.get("mtime", 0), reverse=True):
        proj = Path(r.get("path", "")).parent.name
        if budget.get(proj, 0) > 0:
            budget[proj] -= 1
            r["active"] = True
        else:
            r["active"] = False
    return rows


def last_preview(path: str, cap: int = 96) -> str:
    """A short last-message preview for the phone's session cards. Reads only
    the file TAIL (transcripts grow to many MB and the phone polls the roster
    every ~10s, whole-file reads would burn CPU for a one-line preview)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", "ignore")
    except Exception:
        return ""
    for line in reversed(tail.splitlines()):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant":
            continue
        text = core._blocks_to_text(rec.get("message", {}).get("content", ""))
        text = core.clean_for_speech(text, max_chars=400).strip()
        if text:
            return text[:cap] + ("..." if len(text) > cap else "")
    return ""


def switch_sid(sid: str) -> str:
    """switch() by exact session id (the phone page sends ids, not labels)."""
    from . import talkd
    r = next((x for x in roster() if x.get("sid") == sid), None)
    if not r:
        return f"No session with id {sid[:8]}."
    if not r.get("active"):
        return (f"{r['label']} is closed, no live session to talk to. "
                f"You can hear its last reply, but not send prompts.")
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


def switch(query: str) -> str:
    """Move voice to the named session (rebind the exclusive voiced session)."""
    from . import talkd
    r = find(query)
    if not r:
        return f"No session matching '{query}'."
    if not r.get("active"):
        return (f"{r['label']} is closed, no live session to talk to. "
                f"Say 'read me {r['label']}'s last reply' to hear it.")
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

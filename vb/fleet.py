"""Fleet sensing: one authoritative view of every agent, and the events worth
reacting to.

WHY THIS EXISTS. voicebridge has always learned what a session is doing by
parsing Claude Code's JSONL transcript. That format is now officially declared
internal and unstable ("can break on any release"), and it never saw the states
that matter most anyway: an agent BLOCKED on a question, an agent that ERRORED,
an agent STUCK making no progress. This module moves sensing onto the official
surfaces and turns raw state into a small, typed event stream:

    `claude agents --json`  ->  authoritative liveness + status per session
    hooks (Notification/Stop) ->  permission asks, needs-input, turn finished
    transcript (fallback)     ->  question text, and everything above if the
                                  official surfaces are missing (older CLI)

The output is a list of Events. Deciding what to DO with an event (speak now,
batch it, stay quiet) is deliberately NOT here, that judgment lives in the
attention engine so this layer stays boring, testable, and shared.

Nothing here speaks, injects, or blocks: it observes and reports.
"""

import json
import subprocess
import time
from pathlib import Path

from . import core

# Event kinds, ordered worst-first (callers may rank on this).
FINISHED = "finished"            # a turn completed
BLOCKED_QUESTION = "blocked"     # agent is asking the human something
PERMISSION = "permission"        # agent wants approval to act
ERROR = "error"                  # the turn failed (crash, rate limit, billing)
STUCK = "stuck"                  # busy, but nothing has changed for a long time

KINDS = (ERROR, PERMISSION, BLOCKED_QUESTION, FINISHED, STUCK)

EVENT_LOG = core.STATE_DIR / "fleet_events.jsonl"
_MAX_LOG_LINES = 2000

# `claude agents --json` shells out (~100-300ms), so a poll loop must not call
# it every tick. One shared short cache keeps callers honest.
_CACHE_TTL = 2.0
_cache: dict = {"ts": 0.0, "rows": None}

# How long a busy agent may show no transcript growth before it counts as stuck.
STUCK_AFTER = 600.0


def agents_json(timeout: float = 5.0) -> list:
    """Live agents from Claude Code's own registry, or [] if unavailable.

    This is the authoritative source: it knows about background agents and
    reports a real status, where transcript mtime can only ever guess."""
    now = time.time()
    if _cache["rows"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["rows"]
    rows = []
    try:
        r = subprocess.run(["claude", "agents", "--json"],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            if isinstance(data, list):
                rows = [x for x in data if isinstance(x, dict)]
    except Exception as e:
        core.log(f"fleet: agents --json unavailable ({e}); using fallback")
    _cache.update(ts=now, rows=rows)
    return rows


def other_users() -> dict:
    """Claude sessions belonging to OTHER accounts on this Mac: {user: count}.

    We can see that they exist (process list), and deliberately nothing more:
    another user's home is unreadable by design, and it should stay that way.
    Reporting "3 sessions under nikhilbajaj, which I can't see into" is honest
    and useful; pretending they do not exist is neither, and pretending to read
    them would be a privacy breach dressed up as a feature."""
    import getpass
    me = getpass.getuser()
    out = {}
    try:
        r = subprocess.run(["ps", "-axo", "user=,command="],
                           capture_output=True, text=True, timeout=5)
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            user, cmd = parts
            if user == me:
                continue
            # the interactive CLI, not our own helpers or greps
            first = cmd.split()[0].rsplit("/", 1)[-1] if cmd.split() else ""
            if first == "claude":
                out[user] = out.get(user, 0) + 1
    except Exception:
        pass
    return out


def _norm_status(s: str) -> str:
    """Claude's status vocabulary -> ours ('working' | 'idle')."""
    s = (s or "").lower()
    if s in ("busy", "working", "running"):
        return "working"
    return "idle"


def snapshot() -> dict:
    """Current fleet: {sid: {...}}. Merges the official registry with what we
    can read from transcripts, and degrades to transcript-only when the
    registry is missing (older Claude Code), so sensing never goes blind."""
    from . import sessions

    out = {}
    official = agents_json()
    for a in official:
        sid = a.get("sessionId") or ""
        if not sid:
            continue
        out[sid] = {
            "sid": sid,
            "label": a.get("name") or "session",
            "status": _norm_status(a.get("status")),
            "pid": a.get("pid"),
            "cwd": a.get("cwd") or "",
            "kind": a.get("kind") or "",
            "source": "agents_json",
            "path": "",
            "mtime": 0.0,
        }
    # Transcript rows fill in paths/mtimes, and are the whole picture when the
    # official registry is unavailable.
    try:
        for r in sessions.roster():
            sid = r.get("sid") or ""
            if not sid:
                continue
            if sid in out:
                out[sid]["path"] = r.get("path") or ""
                out[sid]["mtime"] = r.get("mtime") or 0.0
                if not out[sid]["label"] or out[sid]["label"] == "session":
                    out[sid]["label"] = r.get("label") or out[sid]["label"]
            elif not official:
                out[sid] = {
                    "sid": sid, "label": r.get("label") or "session",
                    "status": r.get("state") or "idle", "pid": None,
                    "cwd": "", "kind": "", "source": "transcript",
                    "path": r.get("path") or "", "mtime": r.get("mtime") or 0.0,
                }
    except Exception as e:
        core.log(f"fleet: roster merge failed: {e}")

    # Enrich with the two things status alone cannot tell us: an open question,
    # and a pending permission notice.
    for sid, row in out.items():
        row["question"] = ""
        row["permission"] = ""
        # WHAT THE SESSION IS ABOUT. Claude's own names are generated from the
        # username ("krishojha-7f"), so they are meaningless to a human and give
        # a model nothing to reason with, which is exactly how "what is it
        # working on" turned into invention. The first real thing the user asked
        # is the honest answer.
        row["topic"] = ""
        try:
            if row["path"]:
                row["topic"] = first_prompt(row["path"])
        except Exception:
            pass
        try:
            if row["path"]:
                q = core.pending_question(row["path"])
                if q:
                    qs = (q.get("questions") or [{}])[0]
                    row["question"] = (qs.get("question")
                                       or qs.get("header") or "a question")
        except Exception:
            pass
        try:
            note = core.get_pending_notice(sid)
            if note:
                row["permission"] = note
        except Exception:
            pass
    return out


def detect_events(prev: dict, cur: dict, now: float = 0.0,
                  stuck_after: float = STUCK_AFTER) -> list:
    """Pure diff of two snapshots -> events. No I/O, so it is fully testable
    and can be replayed against a recorded day to tune thresholds.

    `prev` carries per-sid bookkeeping we set ourselves (`_busy_since`,
    `_last_mtime`, `_stuck_sent`); it is returned updated inside `cur`."""
    now = now or time.time()
    events = []
    for sid, row in cur.items():
        old = prev.get(sid) or {}
        label = row.get("label") or "session"

        # carry bookkeeping forward
        row["_busy_since"] = old.get("_busy_since", 0.0)
        row["_last_mtime"] = old.get("_last_mtime", row.get("mtime") or 0.0)
        row["_stuck_sent"] = old.get("_stuck_sent", False)

        # FIRST SIGHT: baseline silently. On startup (or when an agent appears)
        # we have no history, so every state would look like a transition, and
        # a long-running agent would instantly read as "stuck". Restarting
        # voicebridge must never produce a burst of alerts.
        if not old:
            if row.get("status") == "working":
                row["_busy_since"] = now
            continue

        was, is_ = old.get("status"), row.get("status")

        # working -> idle means the turn finished. A session we have never seen
        # before is NOT a finish (that would fire for every agent on startup).
        if was == "working" and is_ == "idle":
            events.append(_ev(FINISHED, row, "finished its turn"))
        if is_ == "working" and was != "working":
            row["_busy_since"] = now
            row["_stuck_sent"] = False

        # A question or permission that was not open before.
        if row.get("question") and not old.get("question"):
            events.append(_ev(BLOCKED_QUESTION, row, row["question"]))
        if row.get("permission") and not old.get("permission"):
            events.append(_ev(PERMISSION, row, row["permission"]))

        # Stuck: busy, but the transcript has not grown in a long time. Fires
        # once per busy stretch, never repeatedly. An agent WAITING ON THE
        # HUMAN (open question or permission) is blocked by design, not stuck,
        # reporting both would be two interruptions for one situation.
        waiting_on_human = bool(row.get("question") or row.get("permission"))
        mt = row.get("mtime") or 0.0
        if mt and mt != row["_last_mtime"]:
            row["_last_mtime"] = mt
            row["_stuck_sent"] = False
        elif (is_ == "working" and not row["_stuck_sent"] and mt
              and not waiting_on_human
              and now - mt > stuck_after):
            row["_stuck_sent"] = True
            mins = int((now - mt) // 60)
            events.append(_ev(STUCK, row, f"no progress for {mins} minutes"))

    return events


_TOPIC_MAX = 90


def first_prompt(transcript_path: str, max_chars: int = _TOPIC_MAX) -> str:
    """What this session is ABOUT, taken from the first substantial thing the
    human asked. Read from the HEAD of the file (cheap: we stop as soon as we
    find one), because the opening request is what a person remembers a session
    by, far better than a generated id."""
    p = Path(transcript_path)
    if not p.exists():
        return ""
    try:
        with open(p, "r", errors="ignore") as f:
            for _ in range(400):                 # bounded: never scan a huge file
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line or '"user"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "user":
                    continue
                content = (rec.get("message") or {}).get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(b.get("text", "") for b in content
                                    if isinstance(b, dict) and b.get("type") == "text")
                text = " ".join(text.split())
                # Skip machinery and slash commands: they say nothing about the work
                if (not text or text.startswith("/") or text.startswith("<")
                        or len(text) < 12):
                    continue
                return text[:max_chars] + ("…" if len(text) > max_chars else "")
    except Exception:
        pass
    return ""


def _ev(kind: str, row: dict, detail: str) -> dict:
    return {"kind": kind, "sid": row.get("sid", ""),
            "label": row.get("label") or "session",
            "detail": detail, "ts": time.time(),
            "source": row.get("source", "")}


def note_error(sid: str, label: str, detail: str) -> dict:
    """An error reported by a hook (StopFailure and friends). Hooks push these
    in rather than us polling for them."""
    ev = {"kind": ERROR, "sid": sid, "label": label or "session",
          "detail": detail, "ts": time.time(), "source": "hook"}
    log_event(ev)
    return ev


def log_event(ev: dict) -> None:
    """Append to the capped event log. This log is the tuning corpus: the
    attention engine is replayed against recorded real days rather than
    guessed at, so thresholds are set from evidence."""
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        lines = []
        if EVENT_LOG.exists():
            lines = EVENT_LOG.read_text(errors="ignore").splitlines()[-(_MAX_LOG_LINES - 1):]
        lines.append(json.dumps(ev))
        EVENT_LOG.write_text("\n".join(lines) + "\n")
    except Exception:
        pass


def recent_events(n: int = 50) -> list:
    try:
        lines = EVENT_LOG.read_text(errors="ignore").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []


def poll(state: dict) -> "tuple":
    """One sensing tick: (events, new_state). Callers keep `state` between
    ticks. Every event is logged as it is detected."""
    cur = snapshot()
    events = detect_events(state or {}, cur)
    for e in events:
        log_event(e)
    return events, cur

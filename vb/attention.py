"""The attention engine: deciding when an agent's news is worth your ear.

THIS IS THE PRODUCT. Speaking is easy; knowing when NOT to speak is the whole
value. Speak too often and you get muted on day one. Stay quiet while an agent
is blocked and you are useless. The tuning of that threshold is the moat, and
it is deliberately DETERMINISTIC: research on proactive-assistance benchmarks
puts even good models around 65% accurate at "should I interrupt now", which is
worse than a tuned rule table, slower, and impossible to explain to a user who
asks "why did you say that?". So rules decide; the local model is used only to
judge how consequential an agent's question is, and to word the sentence.

The shape, borrowed from decades of interruption research and pager practice:

  severity tiers        P0 speak, P1 hold-and-batch, P2 digest, DROP
  bounded deferral      nothing is dropped for being untimely, it is deferred
                        with a deadline, then delivered
  gates                 never about the agent you are watching, never while
                        your mic is hot, respect Do Not Disturb, hard hourly cap
  batching              several pending items become ONE utterance, worst first
  self-tuning           alert types you ignore quietly demote themselves

Everything here is pure and testable: `decide()` takes an event plus a context
snapshot and returns a decision with the reason attached. Gathering the context
(is the mic hot, which window is focused) and actually speaking live elsewhere.
"""

import json
import time

from . import core

# ---- tiers ---------------------------------------------------------------
P0 = "P0"      # interrupt: something is wrong, or blocked on a real decision
P1 = "P1"      # worth saying, but it can wait for a natural gap
P2 = "P2"      # only ever mentioned in passing, or when you ask
DROP = "DROP"  # say nothing, ever, and say why in the log

# ---- actions -------------------------------------------------------------
SPEAK_NOW = "speak_now"
QUEUE = "queue"        # hold, deliver by `deadline`
DIGEST = "digest"      # only surfaces inside another utterance or on request
DISCARD = "discard"

SCORES_FILE = core.STATE_DIR / "attention_scores.json"

# Deliver-by deadlines per tier (seconds). Bounded deferral: a held item is
# never forgotten, it becomes due.
DEADLINE = {P0: 120.0, P1: 600.0}

# How long a non-urgent item waits before it may be spoken, so that things
# happening at nearly the same time arrive as ONE sentence. This is the usual
# delay; DEADLINE is only the worst case for when you are busy. Without it,
# "jobhunt finished" would sit silent for ten minutes for no reason.
BATCH_WINDOW = 20.0

# How many spoken interruptions per hour before P1 stops piercing. P0 always
# pierces (that is what P0 means).
HOURLY_BUDGET = 4

# An event must survive this long before we will speak it: agents very often
# unblock themselves, and announcing something that already resolved is the
# fastest way to feel dumb.
DEBOUNCE = 30.0

# A question the user has been asked, but has not answered, escalates from
# "blocked" to properly urgent after this long.
BLOCKED_URGENT_AFTER = 45.0

# If the user typed into that agent this recently, they are already on it.
RECENT_FOCUS = 60.0


def _default_scores() -> dict:
    # Start every event type trusted; the world teaches us otherwise.
    return {"finished": 1.0, "blocked": 1.0, "permission": 1.0,
            "error": 1.0, "stuck": 1.0}


def load_scores() -> dict:
    try:
        d = json.loads(SCORES_FILE.read_text())
        base = _default_scores()
        base.update({k: float(v) for k, v in d.items() if k in base})
        return base
    except Exception:
        return _default_scores()


def save_scores(scores: dict) -> None:
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        SCORES_FILE.write_text(json.dumps(scores))
    except Exception:
        pass


def context(now: float = 0.0, focused_sid: str = "", mic_busy: bool = False,
            user_speaking: bool = False, dnd: bool = False,
            spoken_at: list = None, typed_at: dict = None,
            scores: dict = None) -> dict:
    """A snapshot of the world at decision time. Kept as a plain dict so tests
    can construct any situation without touching the machine."""
    return {
        "now": now or time.time(),
        "focused_sid": focused_sid,       # the agent the user is looking at
        "mic_busy": mic_busy,             # a call, a meeting, dictation
        "user_speaking": user_speaking,
        "dnd": dnd,                       # macOS Focus / Do Not Disturb
        "spoken_at": list(spoken_at or []),   # timestamps of recent utterances
        "typed_at": dict(typed_at or {}),     # sid -> last keystroke time
        "scores": dict(scores or _default_scores()),
    }


def tier_for(ev: dict, ctx: dict, stakes: str = "") -> str:
    """How loud is this event, before any gating.

    `stakes` ('high' | 'low') is the local model's read of a question's
    consequence. It can promote a blocked question to P0, but it can never
    silence one, the model advises, the table decides."""
    kind = ev.get("kind", "")
    age = max(0.0, ctx["now"] - float(ev.get("ts", ctx["now"])))

    if kind == "error":
        return P0                     # something broke: always worth saying
    if kind == "permission":
        # An agent asking to ACT is blocking real work. High-stakes actions
        # (destructive, irreversible) are urgent immediately.
        return P0 if (stakes == "high" or age >= BLOCKED_URGENT_AFTER) else P1
    if kind == "blocked":
        return P0 if (stakes == "high" and age >= BLOCKED_URGENT_AFTER) else P1
    if kind == "finished":
        return P1
    if kind == "stuck":
        # Mildly stuck is a footnote; properly stuck deserves a mention.
        return P1 if age >= 600 or ev.get("long") else P2
    return P2


def decide(ev: dict, ctx: dict, stakes: str = "") -> dict:
    """Should we say this, and when? Returns
    {action, tier, reason, deadline}. `reason` is mandatory: every decision
    must be explainable, because the first thing a tester asks is "why did you
    say that?" (or "why didn't you?")."""
    now = ctx["now"]
    kind = ev.get("kind", "")
    sid = ev.get("sid", "")
    age = max(0.0, now - float(ev.get("ts", now)))
    tier = tier_for(ev, ctx, stakes)

    def out(action, reason, deadline=0.0, not_before=0.0, settled=None):
        # `settled` = when this event is CONFIRMED real (past debounce).
        # `not_before` = when we would ideally speak it, to allow grouping.
        # The difference matters: if we are interrupting anyway, a settled item
        # rides along early, but an unsettled one still waits, it may vanish.
        return {"action": action, "tier": tier, "reason": reason,
                "deadline": deadline, "not_before": not_before,
                "settled": now if settled is None else settled,
                "kind": kind, "sid": sid,
                "label": ev.get("label", ""), "detail": ev.get("detail", "")}

    # 1. The user is already on it. Watching or typing into that very agent
    #    means they have seen it; telling them is pure noise.
    if sid and sid == ctx.get("focused_sid"):
        return out(DISCARD, "you are looking at this agent")
    typed = ctx.get("typed_at", {}).get(sid, 0.0)
    if typed and now - typed < RECENT_FOCUS:
        return out(DISCARD, "you just typed into this agent")

    # 2. A demoted event type (you have ignored these) never speaks on its own.
    score = float(ctx.get("scores", {}).get(kind, 1.0))
    if score <= 0.35 and tier != P0:
        return out(DIGEST, f"you tend to ignore '{kind}' alerts")

    # 3. Too fresh to trust. Agents unblock themselves constantly; let it settle
    #    (P0 errors are exempt, an error will not un-happen).
    if age < DEBOUNCE and kind != "error":
        settle = now + (DEBOUNCE - age)
        return out(QUEUE, "waiting to see if it resolves itself",
                   settle, not_before=settle, settled=settle)

    # 4. P2 never speaks alone, by definition.
    if tier == P2:
        return out(DIGEST, "minor, will mention it if something else comes up")

    # 5. The audio channel is not ours: you are talking, or on a call.
    if ctx.get("user_speaking") or ctx.get("mic_busy"):
        return out(QUEUE, "you are speaking or on a call",
                   now + DEADLINE.get(tier, 600.0))

    # 6. Do Not Disturb. Only P0 may pierce, and only once it is properly due.
    if ctx.get("dnd"):
        if tier == P0 and age >= DEADLINE[P0]:
            return out(SPEAK_NOW, "urgent, and it has waited out Do Not Disturb")
        return out(QUEUE, "Do Not Disturb is on", now + DEADLINE.get(tier, 600.0))

    # 7. The hourly budget. P0 always pierces; P1 becomes a digest item rather
    #    than the fifth interruption of the hour.
    spoken_last_hour = len([t for t in ctx.get("spoken_at", []) if now - t < 3600])
    if tier != P0 and spoken_last_hour >= HOURLY_BUDGET:
        return out(DIGEST, "already interrupted you enough this hour")

    if tier == P0:
        return out(SPEAK_NOW, "urgent")
    # Worth saying, but give near-simultaneous news a moment to arrive so it
    # lands as one sentence. Hard-bounded by DEADLINE if you turn out to be busy.
    return out(QUEUE, "worth saying at the next natural gap",
               now + DEADLINE[P1], not_before=now + BATCH_WINDOW)


def due(pending: list, ctx: dict) -> list:
    """Queued items whose deferral deadline has arrived, or which can go now
    that the channel is free. Bounded deferral means nothing waits forever."""
    now = ctx["now"]
    if ctx.get("user_speaking") or ctx.get("mic_busy"):
        return []                      # never talk over a human
    def is_due(p):
        if now >= p.get("deadline", 0.0):
            return True                # the hard deadline: never held longer
        if ctx.get("dnd"):
            return False               # Do Not Disturb: only the deadline wins
        if p.get("tier") == P0:
            return True                # urgent, and the channel is free
        return now >= p.get("not_before", 0.0)   # batch window closed

    if not any(is_due(p) for p in pending):
        return []
    # We are interrupting anyway, so everything that has SETTLED rides along in
    # the same breath. One interruption carrying three items always beats three
    # interruptions, and an item still inside its debounce is deliberately left
    # behind (it may yet resolve itself).
    return [p for p in pending
            if is_due(p) or now >= p.get("settled", p.get("not_before", 0.0))]


def compose(items: list, digest: list = None) -> str:
    """Several pending things become ONE utterance, worst first. Two agents
    needing you is one sentence, not two interruptions."""
    if not items:
        return ""
    order = {P0: 0, P1: 1, P2: 2}
    items = sorted(items, key=lambda i: order.get(i.get("tier"), 3))
    parts = [_phrase(i) for i in items]
    if len(parts) == 1:
        head = parts[0]
    elif len(parts) == 2:
        head = f"Two things. {parts[0]} And {parts[1]}"
    else:
        head = (f"{len(parts)} things. " + " ".join(
            f"{p}" for p in parts[:-1]) + f" And {parts[-1]}")
    extra = len(digest or [])
    if extra:
        head += f" {extra} smaller update{'s' if extra != 1 else ''} waiting."
    return head.strip()


def _phrase(item: dict) -> str:
    """One item, in plain spoken English. Template-based so it works with no
    model at all; the local model can rewrite these when it is available."""
    label = item.get("label") or "an agent"
    kind = item.get("kind")
    detail = (item.get("detail") or "").strip().rstrip(".")

    def end(s):
        # An agent's question already ends in "?"; adding a full stop after it
        # ("drop the users table?.") makes the voice stumble.
        return s if s.endswith(("?", "!", ".")) else s + "."
    if kind == "finished":
        return f"{label} finished."
    if kind == "error":
        return end(f"{label} hit an error: {detail}") if detail else f"{label} hit an error."
    if kind == "permission":
        return end(f"{label} needs your approval: {detail}") if detail else f"{label} needs your approval."
    if kind == "blocked":
        return end(f"{label} is asking: {detail}") if detail else f"{label} needs an answer."
    if kind == "stuck":
        return end(f"{label} looks stuck, {detail}") if detail else f"{label} looks stuck."
    return end(f"{label}: {detail}") if detail else f"{label} has an update."


# ---- feedback: the engine tunes itself -----------------------------------
IGNORED = -0.15        # you were told and did nothing
ANSWERED_FAST = 0.05   # you acted right away: this was worth hearing
LESS_OF_THIS = -0.30   # you said so out loud
DECAY = 0.10           # weekly drift back toward trusting an event type again


def feedback(scores: dict, kind: str, signal: float) -> dict:
    """Nudge one event type's standing. Clamped so nothing is ever permanently
    silenced by a bad afternoon, and nothing becomes un-demotable."""
    s = dict(scores)
    s[kind] = max(0.0, min(1.0, float(s.get(kind, 1.0)) + signal))
    return s


def decay(scores: dict, amount: float = DECAY) -> dict:
    """Drift every score back toward 1.0. Yesterday's annoyance should not
    silence a whole category forever."""
    return {k: min(1.0, v + amount) for k, v in scores.items()}


def should_offer_mute(history: list, kind: str, n: int = 2) -> bool:
    """After N consecutive ignores of the same kind, it is kinder to ask than
    to keep talking: "want me to stop announcing these?" (the pattern phones
    use for notifications you never open)."""
    recent = [h for h in history if h.get("kind") == kind][-n:]
    return len(recent) == n and all(h.get("ignored") for h in recent)

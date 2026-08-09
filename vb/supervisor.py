"""The supervisor loop: watch the fleet, decide, and speak up.

This is the thin wiring that turns three tested pieces into the product:

    fleet.poll()      what changed across every agent
    signals.gather()  what is going on around the user right now
    attention.decide  whether this is worth their ear, and when

Everything hard already happened in those modules. This file only sequences
them and owns the pending queue, so it stays small enough to read in one go.

It runs as a loop (`vb friday`) and can run dry (`--dry`), printing what it
WOULD say instead of speaking. Dry mode is how the thresholds get tuned without
anyone having to listen to a day of alerts.
"""

import time

from . import attention, brain, core, fleet, routing, signals


def new_state() -> dict:
    return {"fleet": {}, "pending": [], "spoken_at": [],
            "scores": attention.load_scores(), "history": [], "digest": [],
            "focus": routing.new_focus()}


def tick(state: dict, speak=None, now: float = 0.0) -> list:
    """One pass: sense, decide, and say whatever is due. Returns the list of
    things actually said (usually zero or one utterance)."""
    events, state["fleet"] = fleet.poll(state.get("fleet") or {})
    sids = list(state["fleet"])
    ctx = signals.gather(sids, scores=state["scores"],
                         spoken_at=state["spoken_at"])
    if now:
        ctx["now"] = now
    # The session you are voiced in is one you are demonstrably present in: its
    # replies are already spoken to you there, so announcing it would repeat
    # yourself. Treat it exactly like the agent you are looking at.
    voiced = state.get("_voiced_sid")
    if voiced and not ctx.get("focused_sid"):
        ctx["focused_sid"] = voiced

    for ev in events:
        d = attention.decide(ev, ctx, stakes=_stakes(ev))
        core.log(f"friday: {ev['kind']} {ev['label']} -> {d['action']} "
                 f"({d['reason']})")
        if d["action"] in (attention.SPEAK_NOW, attention.QUEUE):
            # Never queue the same thing twice. Saying "jobhunt finished. And
            # jobhunt finished." in one breath is the kind of small stupidity
            # that makes people mute a product.
            dup = any(p.get("kind") == d["kind"] and p.get("sid") == d["sid"]
                      for p in state["pending"])
            if not dup:
                state["pending"].append(d)
        elif d["action"] == attention.DIGEST:
            state["digest"].append(d)
        # DISCARD: logged above, and that is the whole point of it

    said = []
    ready = attention.due(state["pending"], ctx)
    if ready:
        # Anything still queued but not due rides along only if it is for the
        # same agent, otherwise it waits its turn.
        text = attention.compose(ready, digest=state["digest"])
        if text:
            said.append(text)
            state["spoken_at"].append(ctx["now"])
            state["spoken_at"] = state["spoken_at"][-20:]
            for r in ready:
                state["history"].append({"kind": r["kind"], "ts": ctx["now"],
                                         "ignored": None})
            state["history"] = state["history"][-50:]
            state["digest"] = []
            # Remember who we just mentioned, so an unqualified reply ("tell it
            # to use Redis") reaches the agent that actually asked.
            state["focus"] = routing.note_spoken(state["focus"], ready,
                                                 now=ctx["now"])
            if speak:
                speak(text)
        state["pending"] = [p for p in state["pending"] if p not in ready]
    return said


def _stakes(ev: dict) -> str:
    """How consequential is what the agent is asking?

    The local model judges this when it is available (one of its three jobs).
    A keyword pass is the floor underneath it, and it also OVERRIDES a 'low'
    verdict: if the text plainly says "drop table" we do not care that a 4B
    model felt relaxed about it. Being wrong low-side only delays a question by
    45 seconds; being wrong high-side interrupts you for nothing."""
    text = f"{ev.get('detail', '')}".lower()
    danger = ("delete", "drop ", "drop table", "rm -rf", "force push",
              "force-push", "reset --hard", "revoke", "production", "prod ",
              "migrate", "truncate", "overwrite", "credentials", "secret",
              "deploy")
    if any(d in text for d in danger):
        return "high"                     # never overridable, it is in the text
    verdict = ""
    try:
        if ev.get("kind") in ("blocked", "permission"):
            verdict = brain.stakes(ev.get("detail", ""))
    except Exception:
        verdict = ""
    return verdict or "low"


def run(dry: bool = False, interval: float = 3.0, once: bool = False) -> int:
    """The loop. `dry` prints instead of speaking, which is how you tune it."""
    state = new_state()
    speak = None if dry else (lambda t: core.speak(t))
    print("friday: watching the fleet"
          + (" (dry run, nothing will be spoken)" if dry else "")
          + ". ctrl-c to stop.")
    try:
        while True:
            said = tick(state, speak=speak)
            for s in said:
                stamp = time.strftime("%H:%M:%S")
                print(f"  {stamp}  SAY: {s}")
            if once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nfriday: stopped.")
    return 0

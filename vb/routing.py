"""Where does what you just said belong?

Speaking up is only half the loop. If Friday says "the auth agent is asking
which token store to use" and you answer "tell it to use the existing Redis
one", that answer has to land in the auth agent, not in whatever session
happens to be in front of you. Getting this wrong is worse than staying silent:
an answer delivered to the wrong agent is a wrong instruction to a machine that
will act on it.

The rules, in the order a person would apply them:

  1. You named an agent            -> that one, obviously
  2. You are answering what was just said, and only one agent is waiting
                                   -> that one
  3. Several agents are waiting and you did not say which
                                   -> ask, never guess
  4. Otherwise                     -> the session you are actually in

Rule 3 is the important one. When in doubt this module refuses to route and
asks a one-line clarifying question, because a coin flip here has consequences.
"""

import re
import time

# How long after Friday mentions an agent an unqualified reply is still
# understood as a reply to it. Longer than a chat turn, shorter than a memory.
REPLY_WINDOW = 180.0

# "tell it to...", "say yes", "use the redis one": phrasing that reads as an
# answer to something rather than a fresh instruction.
_REPLY_CUES = re.compile(
    r"^\s*(tell (it|him|her|them)\b|answer\b|reply\b|say\b|yes\b|no\b|"
    r"use \b|go with\b|pick\b|choose\b|do it\b|approve\b|deny\b|the \w+ one\b)",
    re.I)


def new_focus() -> dict:
    """What Friday has recently told the user about."""
    return {"mentioned": [], "ts": 0.0}


def note_spoken(focus: dict, items: list, now: float = 0.0) -> dict:
    """Record which agents were just mentioned out loud, and which of them are
    actually waiting on an answer (only those can receive a reply)."""
    now = now or time.time()
    waiting = [{"sid": i.get("sid", ""), "label": i.get("label", ""),
                "kind": i.get("kind", "")}
               for i in items
               if i.get("kind") in ("blocked", "permission") and i.get("sid")]
    return {"mentioned": waiting, "ts": now}


def looks_like_reply(text: str) -> bool:
    return bool(_REPLY_CUES.search(text or ""))


def route(text: str, focus: dict, active_sid: str = "",
          find=None, now: float = 0.0) -> dict:
    """Decide where `text` goes.

    Returns {"sid", "label", "why"} to deliver, or {"ask": "..."} when it would
    have to guess between several waiting agents. `find` is the name matcher
    (sessions.find), injected so this stays pure and testable."""
    now = now or time.time()
    text = (text or "").strip()

    # 1. An explicit name always wins, even mid-sentence ("tell jobhunt to...").
    if find:
        named = _named_agent(text, find)
        if named:
            return {"sid": named.get("sid", ""), "label": named.get("label", ""),
                    "why": "you named it"}

    mentioned = focus.get("mentioned") or []
    fresh = mentioned and (now - float(focus.get("ts", 0))) < REPLY_WINDOW

    # 2 and 3. It reads like an answer to what was just said.
    if fresh and looks_like_reply(text):
        if len(mentioned) == 1:
            m = mentioned[0]
            return {"sid": m["sid"], "label": m["label"],
                    "why": "answering the agent that asked"}
        labels = [m["label"] for m in mentioned]
        return {"ask": _which(labels), "candidates": labels,
                "why": "several agents are waiting, refusing to guess"}

    # 4. Anything else is a normal instruction to the session you are in.
    return {"sid": active_sid, "label": "", "why": "your current session"}


def _named_agent(text: str, find):
    """A session whose name appears in what you said. Guarded: a name must
    appear as a whole word, so 'use the api' does not match an agent called
    'a'."""
    try:
        for word in re.findall(r"[A-Za-z0-9][\w.-]{1,}", text):
            hit = find(word)
            if hit and hit.get("label"):
                lab = hit["label"].lower()
                if re.search(rf"\b{re.escape(word.lower())}\b", text.lower()) \
                        and (word.lower() in lab or lab in text.lower()):
                    return hit
    except Exception:
        pass
    return None


def _which(labels: list) -> str:
    if len(labels) == 2:
        return f"Which one, {labels[0]} or {labels[1]}?"
    return "Which one, " + ", ".join(labels[:-1]) + f", or {labels[-1]}?"

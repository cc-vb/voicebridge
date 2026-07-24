#!/usr/bin/env python3
"""UserPromptSubmit hook: steer response language + spoken conversation tone.

If a voicebridge language is set (`vb lang ...`), this prints a short
directive that Claude Code adds to the context, so replies come back in the
language you chose and in a natural, spoken, back-and-forth tone (since
they'll be read aloud). Prints nothing when no language is set, so it never
interferes with normal work.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402


def directive(lang: str) -> str:
    low = lang.lower()
    if low == "auto":
        how = ("Respond in the same language the user's message is in "
               "(English, Hindi in Devanagari, or Hinglish in Latin script, "
               "mirror them).")
    elif "hinglish" in low:
        how = "Respond in Hinglish (a natural, casual mix of Hindi and " \
              "English written in Latin script)."
    elif low in ("hindi", "हिंदी"):
        how = "Respond in Hindi, using Devanagari script."
    else:
        how = f"Respond in {lang}."
    tone = ("Use a warm, natural, spoken conversational tone, as if talking "
            "out loud: first person, concise, and end with a short follow-up "
            "question when it fits naturally. This is a style note for prose "
            "only; do not let it change code, commands, or file contents.")
    return f"[voicebridge] {how} {tone}"


TONE = ("[voicebridge] Keep replies in a warm, natural, spoken "
        "conversational tone, as if talking out loud: first person, concise, "
        "and end with a short follow-up question when it fits. Style note for "
        "prose only; do not let it change code, commands, or file contents.")

# --- the gray "voice on" line: a DECAYING TUTOR (shown only while voiced) ---
# A fixed intro sequence teaches the essentials over the first prompts, then a
# rotation surfaces the rest, retiring each tip once you've clearly learned it
# (inferred from your own settings) or after a small show-cap. Context lines
# (update ready, a cut-off reply) preempt everything. When there's nothing
# useful left to say it falls silent, so a fluent user just sees "voice on".
import json  # noqa: E402

TEACH_DIR = core.STATE_DIR / "teach"
SHOWN = TEACH_DIR / "shown.json"

_INTRO = [
    'silence me anytime with Ctrl+Alt+Cmd+X, or just start typing',
    'Fn+F8 (or Ctrl+Alt+Cmd+H) pauses me mid-word; press again to resume',
    'too fast or slow? tap Fn+F9 / Fn+F7 while I talk (or say "faster")',
    'others nearby? say "wake word mode" so I only reply after "hey Claude"',
    'talk to this session from your phone: run  vb phone  and scan the QR',
]


def _safe(fn, default=""):
    try:
        return fn()
    except Exception:
        return default


# (id, text, learned? -> retire when True, max shows)
def _pool():
    from vb import talkd as _td
    return [
        ("speed", 'say "faster"/"slower", or tap Fn+F9 / Fn+F7 anytime',
         lambda: _safe(core.get_rate) not in ("", "175"), 3),
        ("wake", 'say "wake word mode" so I only reply after "hey Claude"',
         lambda: _safe(_td.get_mode) == "wake", 3),
        ("barge", 'talk over me to interrupt and redirect (agent mode)',
         lambda: False, 2),
        ("stopgen", 'Ctrl+Alt+Cmd+Z silences me AND stops Claude mid-answer',
         lambda: False, 2),
        ("engine", 'want a nicer neural voice? run  vb engine kokoro',
         lambda: _safe(core.get_engine) == "kokoro", 2),
        ("voice", 'change my voice with  vb voice  (dozens of options)',
         lambda: False, 1),
        ("phone", 'take this call anywhere: run  vb phone  and scan the QR',
         lambda: (core.STATE_DIR / "call_secret").exists(), 2),
        ("off", 'say "stop listening" to mute, or /voice-off to end',
         lambda: False, 2),
    ]


def _teach_line() -> str:
    """One line of teaching for the gray hook area, or '' to stay quiet."""
    # Context triggers preempt the tutor (rare, actionable, self-clearing).
    try:
        from vb import talkd as _td
        repo = Path(__file__).resolve().parent.parent
        if ((repo / ".git").is_dir() and _td.VER.exists()
                and _td.VER.read_text().strip() != _td._code_version()):
            return "update ready: run /voice-off then /voice-on for the latest"
    except Exception:
        pass
    if core.REMAINDER.exists():
        return 'that was cut short, say "continue" (or run vb continue) for the rest'
    try:
        shown = json.loads(SHOWN.read_text())
    except Exception:
        shown = {}

    def save():
        try:
            TEACH_DIR.mkdir(parents=True, exist_ok=True)
            SHOWN.write_text(json.dumps(shown))
        except Exception:
            pass

    # Intro sequence: one essential per prompt, in order.
    i = shown.get("_intro", 0)
    if i < len(_INTRO):
        shown["_intro"] = i + 1
        save()
        return _INTRO[i]
    # Behaviour-driven recommendations: what THIS user is struggling with
    # right now beats any generic tip. Signals come from rolling counters
    # (core.bump_stat) written by the daemon and this hook.
    from vb import talkd as _td
    stats = core.get_stats()
    triggers = []
    if stats.get("drops", 0) >= 3 and _safe(_td.get_mode) == "all":
        triggers.append(("ctx_wake", 'hearing things you didn\'t say? say '
                         '"wake word mode", I\'ll only listen after '
                         '"hey Claude"', 3))
    if stats.get("typed_over", 0) >= 3:
        triggers.append(("ctx_hush", "you keep typing over me, "
                         "Ctrl+Alt+Cmd+X silences me instantly", 2))
    for tid, text, cap in triggers:
        if shown.get(tid, 0) < cap:
            shown[tid] = shown.get(tid, 0) + 1
            save()
            return text
    # Steady state: show the least-shown tip that's neither learned nor capped.
    elig = []
    for tid, text, learned, cap in _pool():
        if _safe(learned, False) or shown.get(tid, 0) >= cap:
            continue
        elig.append((shown.get(tid, 0), tid, text))
    if not elig:
        return ""   # fully learned -> fall silent
    elig.sort(key=lambda e: e[0])
    _, tid, text = elig[0]
    shown[tid] = shown.get(tid, 0) + 1
    save()
    return text


def main() -> int:
    # Record which session this prompt belongs to; talkd uses it so
    # /voice-on binds to THIS session, never a guess.
    data = core.read_hook_input()
    sid = data.get("session_id", "")
    tp = data.get("transcript_path", "")
    if sid and tp:
        from vb import talkd
        talkd.record_prompt(sid, tp)

    voiced = bool(sid and core.is_voiced(sid))
    # A new prompt in the VOICED session means you've moved on: stop its speech
    # mid-sentence. Only when THIS session is voiced, so a prompt typed in some
    # OTHER (non-voice) session never silences the session you're listening to.
    if voiced:
        try:
            if core.speech_active():
                # Signal for the recommender: they keep typing over the voice,
                # so the instant-silence key is worth teaching.
                core.bump_stat("typed_over")
        except Exception:
            pass
        core.hush()

    # Gist-first (only when replies are being spoken): leading with a one-line
    # summary means the listener gets the whole point without having to track a
    # wall of text on screen, which is the real fix for "I lost my place".
    gist = (" Begin a substantive reply with a single short spoken-style "
            "sentence that states the outcome, then give the detail."
            if voiced else "")
    lang = core.get_lang().lower()
    # English-only now: only inject a non-English directive if the user
    # explicitly pinned one; otherwise just the spoken-tone note.
    if lang and lang not in ("english", "auto", "en"):
        context = directive(lang) + gist
    else:
        context = TONE + gist

    # The gray line you actually SEE must be a bare `systemMessage` (that's how
    # Claude Code renders a hook note to the user, exactly as PromptGuard does).
    # Emitting it ALONGSIDE hookSpecificOutput.additionalContext suppressed it,
    # so when voiced we emit systemMessage on its own; the model still gets the
    # spoken-tone steering from the SessionStart hint and prior turns.
    tip = ""
    try:
        if voiced:
            line = _teach_line()
            tip = (f"\U0001f399 voice on · {line}" if line
                   else "\U0001f399 voice on")
    except Exception:
        tip = ""
    if tip:
        print(json.dumps({"systemMessage": tip}))
    else:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": context}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

# Rotating discovery hints for the gray hook line (shown only while voiced).
# A different one each prompt, so shortcuts get discovered without nagging.
_TIPS = [
    'silence me: Ctrl+Alt+Cmd+X  ·  or just start typing',
    'say "faster" or "slower" (or tap F9 / F7) to change my pace',
    'say "wake word mode" so I only listen after "hey Claude"',
    'talk over me to interrupt and redirect',
    'say "stop listening" to mute  ·  /voice-off to end',
    'Ctrl+Alt+Cmd+Z stops me mid-answer',
]


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
        print(directive(lang) + gist)
    else:
        print(TONE + gist)

    # When voice is ON for THIS session, surface a compact status + ONE
    # rotating discovery hint in the gray hook area. Unlike the shared status
    # bar (which must stay quiet), this line scrolls away with history, so it
    # can carry a tip without becoming permanent clutter. Silent otherwise, so
    # a non-voice session never sees it.
    try:
        if voiced:
            nf = core.STATE_DIR / "hooktip_n"
            try:
                n = int(nf.read_text().strip()) + 1
            except Exception:
                n = 0
            try:
                nf.write_text(str(n))
            except Exception:
                pass
            print(f"[voicebridge] \U0001f399 voice on · {_TIPS[n % len(_TIPS)]}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

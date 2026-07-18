# voicebridge on mobile (native path)

Decision (2026-07-18): mobile uses the **native** stack, not a custom relay.
Zero build, free, private, and it drives your real local session. The one
tradeoff: no spoken output on the phone (you read replies on the go; you
only *hear* Claude at your desk via `vb on`).

Telegram (walkie-talkie) and Vapi (true call) were considered and deferred.
See ROADMAP.md "Later - mobile voice out" if we revisit.

## The native mobile loop

1. **Desk:** `vb on` (Claude speaks its replies out loud on the Mac).
2. **Start a remote-enabled session:** `/remote-control` (or `/rc`) in the
   terminal, scan the QR with the Claude app.
3. **On the phone (Claude app):**
   - Input: type, or use the app's native **voice dictation** (mic key).
   - Output: read Claude's replies (no TTS on device).
   - You're steering the *same* session running on your Mac.

## Companion feel: push notifications (the key upgrade)

This is what makes the phone reach out to you instead of you checking it.

Prereqs: Claude app installed, signed in with the **same** account as the
terminal, and OS notifications allowed for the app.

Enable in the terminal:

```
/config
```
Turn on:
- **Push when Claude decides** - proactive pings (e.g. long task finished)
- **Push when actions required** - permission prompts / questions

You can also ask in a prompt, e.g. "notify me when the tests finish".

Notes:
- Pushes are skipped while you're focused on the terminal (you're already
  there). Optionally set `CLAUDE_CLIENT_PRESENCE_FILE` to extend that to
  "any time I'm at the machine" via a screen-lock listener.
- iOS Focus modes / Android battery optimization can delay pushes; exempt
  the Claude app if they don't arrive.

## What "mobile" is and isn't here

- IS: full text in/out + native voice **input** + push, on your live session.
- IS NOT: Claude's voice coming back to the phone. That needs Telegram or
  Vapi (deferred).

# voicebridge demo script (60-90 seconds)

Goal: show the ONE thing nobody else does, controlling a fleet of coding
agents by voice. Not "look, voice dictation." Record screen + audio.

## Setup before recording
- Kokoro voice on: `vb engine kokoro` (already default).
- Two or three real Claude Code sessions open in different projects (so the
  fleet is real). Optionally register Codex: `vb agent add codex '<glob>'`.
- Wear headphones (cleaner audio, no echo).
- Terminal font big enough to read on video.

## The script (what you SAY out loud, and what happens)

1. Cold open, no preamble. In a session, type `/voice-on`, then say:
   > "Hey, add input validation to the signup endpoint and run the tests."
   (Show your words landing in the session and Claude starting. Let its
   spoken reply begin, then interrupt out loud:)
   > "Actually, also check the rate limiter while you're in there."
   (Show it stop mid-sentence and take the new instruction. That's barge-in.)

2. Fleet, the money shot. While that runs, say:
   > "Which agents need me?"
   (It speaks: "3 sessions. Waiting for you: jobhunt. Still working: signup,
   ccdash." Let the voice play on camera.)

3. Jump without touching the keyboard:
   > "Switch to jobhunt."
   (It says "Voice moved to jobhunt." Now you're driving a different agent.)
   > "What's the status, and push if the tests pass."

4. Hands-free awareness:
   > (Say nothing. When another agent finishes, it speaks on its own:)
   > "Heads up: signup is ready for you."
   (This is the line that makes people lean in, it watched the fleet for you.)

5. Close on the phone (optional, strong):
   > Type `/vb-phone`, hold up your phone, scan the QR, tap Start, and say
   > "what did signup end up changing?" Let the phone speak the answer.

6. One-liner to end on (say to camera):
   > "One voice, every agent, from anywhere. That's voicebridge."

## What to caption on screen
- When barge-in happens: "interrupt just by talking"
- On the fleet line: "one voice controls every session"
- On the auto-alert: "it tells you when an agent needs you"
- End card: `/plugin marketplace add cc-vb/voicebridge` + the repo URL

## Don'ts
- Don't demo plain dictation, native /voice already does that; lead with the
  fleet, that's the delta.
- Don't explain the install first; show the magic, put install on the end card.
- Keep it under 90s. Cut every pause.

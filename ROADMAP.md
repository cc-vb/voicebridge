# voicebridge roadmap

The bet: Remote Control made the phone a *window* into your agent.
voicebridge makes it a *conversation* with your agent, without losing the
precision that made the terminal worth using.

Each milestone is shippable on its own and proves one piece of the idea.

## M1 - Voice OUT (DONE)

Claude speaks. The "I finally know what it's doing" half.

- [x] `Stop` hook -> speak final reply (transcript-parsed, cleaned)
- [x] `Notification` hook -> speak decision moments
- [x] non-blocking `say`, new speech interrupts old
- [x] opt-in flag + `vb` CLI + config
- [ ] narration via `PreToolUse`: short spoken "editing auth.py now"
      (throttle so it doesn't chatter; summarize, don't read every call)

## M2 - Voice IN (local, private)

You talk back, hands-free, for direction and approval only.

- [ ] install whisper.cpp (local, offline STT) + tiny/base model
- [ ] push-to-talk capture script -> transcript
- [ ] review-before-send: show transcript, confirm, then inject
- [ ] delivery into the live session. Two candidate paths:
  - Remote Control: send as a normal message from the paired device
  - Local: a small stdin bridge / `channels` plugin (Telegram) as relay
- [ ] rule: never dictate code/paths/regex; intent + approvals only

## M3 - Barge-in

Interrupt like a real conversation.

- [ ] push-to-interrupt (hold key / tap) -> `pkill say` + start listening
- [ ] later: VAD full-duplex (hard; watch self-cutoff on interrupt)
- [ ] target: sub-800ms turn feel; measure and log latency

## M4 - Decision-moment voice (the trust win)

Fix what Happy gets wrong: speak the real question, take a spoken answer.

- [ ] detect permission/choice prompts from `Notification` payloads
- [ ] phrase them in plain language ("run git push, yes or no?")
- [ ] map spoken yes/no/redirect back to the approval
- [ ] fall back to screen for anything ambiguous

## M5 - Trustworthy mobile review (nobody solves this)

- [ ] on turn end, generate a spoken diff summary:
      "3 files, the risky one is auth.py, want a walkthrough?"
- [ ] a clean summarized-diff view (counts, risk flags) for the phone
- [ ] voice command to expand/read a specific file's changes

## Cross-cutting

- Latency budget: keep every spoken response under ~800ms to first audio.
- Privacy: STT stays on-device; nothing about code leaves the machine
  beyond what Remote Control already syncs.
- Config surface stays tiny; sensible defaults over knobs.

## Open decisions (revisit as we build)

- Relay for voice IN: lean on Remote Control's paired device, or run our
  own Telegram `channel` for a true "text/voice from anywhere" bot?
- TTS engine: `say` now; evaluate Kokoro (local, nicer voice) at M2.

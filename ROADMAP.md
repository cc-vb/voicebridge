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
- [x] transcript watcher daemon: speaks replies in a live session with no
      restart; cross-process dedup so watcher + hooks never double-speak
- [x] optional `--narrate` for tool-only turns ("Running Bash.")
- [ ] smarter narration via `PreToolUse`: name the file being edited,
      throttle so it doesn't chatter; summarize, don't read every call

## M2 - Voice IN (local, private) - DONE

You talk back, hands-free, for direction and approval only.

- [x] install whisper.cpp (local, offline STT) + base.en model
- [x] press-to-talk capture (sox `rec`, silence auto-stop)
- [x] local transcription (`vb/stt.py`), verified end to end
- [x] delivery into the focused session via clipboard paste (`vb/inject.py`)
- [x] `vb listen [--send] [--delay=N]` + `vb stt` readiness
- [x] rule enforced by habit + docs: dictate intent, not code/paths
- [ ] optional review-before-send UI (currently: not auto-sent unless
      `--send`, so you can eyeball and press Enter)

## M3 - Continuous agent mode - DONE (2026-07-19)

- [x] `vb session`: voicemode channel wires the mic INTO the live session;
      continuous hands-free loop, spoken replies via reply tool, spoken
      permission relay (say yes/no), "stop listening" to mute
- [x] `vb converse`: headless side-chat variant (claude -p --continue)
- [x] press-to-interrupt: `vb listen` hushes `say` the moment you talk
- [x] STT quality: small.en model auto-preferred; first-word capture fixed;
      norm leveling (no shouting)
- [x] human pace/voice: vb rate (175 default), vb voice, vb lang
- [ ] VAD full-duplex barge-in DURING speech (hard; echo/self-cutoff;
      needs AEC) - the one deliberate gap
- [ ] Enhanced/Premium voice: manual download via System Settings ->
      Accessibility -> Spoken Content (can't be scripted), then
      `vb voice "Ava (Enhanced)"`

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

## Mobile - DECIDED (2026-07-18): native

Use the Claude app + Remote Control + push notifications. Free, private,
drives the live session, no build. Tradeoff: no spoken output on device.
See MOBILE.md. Telegram (walkie-talkie) and Vapi (true call) considered
and deferred.

### Mobile voice out - BUILT (2026-07-18), ready to wire

Reversed the "native only" call once push notifications proved not
agent-like. Three paths now in `mobile/`:

- **Telegram native text** (`mobile/TELEGRAM_NATIVE.md`): official plugin,
  drives live session, permission relay. Zero code. Text only. Needs a bot
  token + Bun. Fastest agent-like phone win.
- **Telegram voice channel** (`mobile/telegram-voice/`): custom Bun channel,
  voice note in (whisper) + spoken voice note out (say), text too,
  permission relay. Bundles clean; media round-trip verified. Needs a bot
  token to run live.
- **Vapi true call** (`mobile/vapi/`): OpenAI-compatible relay + channel;
  real-time call bridged to the live session. HTTP layer + injection
  verified; full call path needs a Vapi account + tunnel.

Deps installed: Bun, ffmpeg (opus), whisper.cpp, sox.

## Open decisions (revisit as we build)

- TTS engine: `say` now; evaluate Kokoro (local, nicer voice) later.

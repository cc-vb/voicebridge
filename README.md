# voicebridge

A voice teammate that rides on top of Claude Code **Remote Control**. Your
agent stays on your machine; your phone (or your desk) becomes a hands-free
conversation with it: it narrates as it works, asks you real questions out
loud, you cut in when needed, and you get a diff you can actually trust.

Not a new sync layer. Remote Control already gives us the robust, free,
disconnect-surviving pipe. voicebridge adds the two things it lacks: a
**narrating two-way voice** and a **trustworthy review** on a small screen.

## Why this and not the others

| Tool | Pipe | Voice | Gap we beat |
|---|---|---|---|
| Remote Control (native) | best, free | none | no voice at all |
| Happy Coder | own layer | input only | choices shown as raw JSON |
| Omnara | own layer | in + out | forces voice-first; bad mobile diffs |
| OSS hooks | on hooks | two-way | no polish, no review |

We are the only stack combining: free robust pipe + narrating two-way voice
+ barge-in + trustworthy mobile review. And we skip rebuilding sync, so we
get there faster.

## Design rules (learned from everyone's mistakes)

- **Hybrid, never voice-first.** Voice is for *direction* and *approval*.
  Precise edits, file paths, and diffs stay on screen/keys. (Omnara's error.)
- **Narrate, don't just answer.** Spoken progress is the differentiator;
  most tools only do voice *input*.
- **Decision moments in plain words.** Speak the actual question, never a
  JSON yes/no blob. (Happy's error.)
- **Local STT** (whisper.cpp) for privacy: you'll say customer/arch names.
- **Barge-in matters most and is hardest.** Ship push-to-interrupt first,
  full-duplex VAD later. Over ~800ms of latency feels awkward; keep it tight.

## What works today (Milestone 1: voice OUT)

Two independent paths deliver spoken output, and they dedup against each
other so you never hear anything twice:

1. **Transcript watcher** (`vb on`): a background daemon tails your live
   session transcript and speaks new assistant replies as they land. This
   works in an **already-running** Claude Code session, no restart needed.
2. **Hooks** (`Stop`, `Notification`): activate on the next fresh session
   and cover decision moments. Wired into `~/.claude/settings.json`.

- Code blocks, links, and markdown stripped; length capped at a sentence
  boundary. New speech interrupts old (seed of barge-in).
- Non-blocking: `say` is detached, so nothing delays your session.
- Opt-in via a flag file, so it never surprises you.

### Control it

```
bin/vb on [--narrate]   speak on + start the watcher (--narrate also
                        announces tool-only turns, e.g. "Running Bash.")
bin/vb off              speak off + stop the watcher + hush now
bin/vb status           show state, watcher, config
bin/vb test             speak a test line
bin/vb say TEXT         speak arbitrary text
bin/vb log              debug log
```

`vb on` targets your most recently active session automatically. Optional:
add `~/voicebridge/bin` to your PATH to just type `vb`.

### Agent mode - hands-free continuous conversation

Tap once, then just talk. No button between turns.

```
vb converse        # start agent mode (say "stop" to end, or Ctrl-C)
```

It listens, transcribes locally, injects into the **focused** Claude window,
waits for the reply, speaks it, and listens again, in a loop. Run it in a
spare terminal, then click your Claude window so the paste lands there (the
mic doesn't need focus, only the paste does).

**Headphones recommended but not required.** converse is half-duplex: it
never records while Claude is talking (it speaks the reply blocking, then
listens), and it waits for you to start speaking before capturing, so on
speakers it won't hear itself the way a full talk-over-it system would. Keep
speaker volume moderate. While converse runs it silences the transcript
watcher (it speaks replies itself) and restores it on exit.

Best for conversational back-and-forth; on long multi-tool turns it may
resume listening a little early. This is the free, local version of the
"tap once and just talk" call feel; the phone version of the same is Vapi
(`mobile/vapi/`).

### Language & conversation tone

Make the agent talk to you in your language, with a spoken back-and-forth
feel:

```
vb lang hinglish   # or: english, hindi, or anything you name
vb lang            # show current language + voice
vb lang off        # back to default English
```

This does two things: sets the macOS voice (Hinglish -> Indian-accent
`Rishi`, Hindi -> Devanagari `Lekha`, others -> default), and, via a
UserPromptSubmit hook, tells Claude to reply in that language in a warm,
concise, spoken conversational tone (ending with a natural follow-up when it
fits). It's a prose style note only; it won't change code or commands.

The voice switches immediately; the response-language directive loads at the
start of your next Claude Code session (or just ask Claude to switch now).

### Talk back (input) - Milestone 2: local voice IN

Two ways to talk to Claude:

1. **Native `/voice`** (zero setup): hold spacebar in the TUI and speak.
   Best when your hands are already on the keyboard.
2. **`vb listen`** (voicebridge, fully local via whisper.cpp): press to
   talk, it transcribes on-device and pastes your words into the focused
   Claude window. Nothing leaves the machine.

```
vb stt                    # check STT readiness (binary + model)
vb listen                 # hush Claude, record, transcribe, paste
vb listen --send          # also press Return to send immediately
vb listen --delay=2       # wait 2s so you can focus the Claude window
```

**Barge-in:** `vb listen` runs `pkill say` the instant it starts, so
choosing to talk immediately hushes Claude. That's the interrupt feel.
(Interrupting Claude's actual generation mid-thought is still the CLI's
own Esc; an external voice layer can't reach into that cleanly.)

**Seamless use:** bind `vb listen --send` to a global hotkey (macOS
Shortcuts, Raycast, or skhd) so you never leave the Claude window. See
[SETUP.md](SETUP.md).

**Recommended:** headphones. On speakers, the mic hears Claude's own
voice, which causes false triggers and echo. This is why always-on
listening (VAD) is a stretch goal, not the default.

First run prompts for two macOS permissions: **Microphone** (for `rec`)
and **Accessibility** (for the paste keystroke). See [SETUP.md](SETUP.md).

### Config (env vars)

- `VOICEBRIDGE_VOICE`  say voice name (empty = system default)
- `VOICEBRIDGE_RATE`   words per minute (default 200)
- `VOICEBRIDGE_MAXCHARS` max chars spoken per utterance (default 700)

List voices with `say -v '?'`.

## Mobile

Voice and chat on your phone, driving your live session. See `mobile/`:

- **Telegram, text** (native, zero code): [mobile/TELEGRAM_NATIVE.md](mobile/TELEGRAM_NATIVE.md)
- **Telegram, voice** (hear Claude back): [mobile/telegram-voice/](mobile/telegram-voice/)
- **Vapi true call** (real-time): [mobile/vapi/](mobile/vapi/)
- Notifications-only baseline (Claude app + Remote Control): [MOBILE.md](MOBILE.md)

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full phased plan (STT input,
barge-in, decision-moment voice, mobile diff summary).

## Author

Krish Ojha

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

- `Stop` hook speaks Claude's final reply each turn.
- `Notification` hook speaks the moment Claude needs a decision.
- Code blocks, links, and markdown are stripped; length capped at a
  sentence boundary. New speech interrupts old (seed of barge-in).
- Non-blocking: `say` is detached, so hooks never delay your session.
- Opt-in via a flag file, so it never surprises you.

### Control it

```
bin/vb on        # turn spoken output on
bin/vb off       # turn it off (also stops current speech)
bin/vb status    # show state + config
bin/vb test      # speak a test line
bin/vb say TEXT  # speak arbitrary text
bin/vb log       # debug log
```

Optional: add `~/voicebridge/bin` to your PATH to just type `vb`.

### Config (env vars)

- `VOICEBRIDGE_VOICE`  say voice name (empty = system default)
- `VOICEBRIDGE_RATE`   words per minute (default 200)
- `VOICEBRIDGE_MAXCHARS` max chars spoken per utterance (default 700)

List voices with `say -v '?'`.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full phased plan (STT input,
barge-in, decision-moment voice, mobile diff summary).

## Author

Krish Ojha

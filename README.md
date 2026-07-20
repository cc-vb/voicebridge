# voicebridge

**Talk to Claude Code like a teammate.** voicebridge turns your Claude Code
sessions into hands-free voice conversations: you speak, your words land in
the real session (your files, your context), and Claude answers out loud in
a natural neural voice. On your Mac, or from your phone anywhere.

Everything runs locally: speech-to-text is whisper.cpp on your machine,
text-to-speech is Kokoro (bundled setup) or macOS voices. Free, no cloud
voice services, no per-minute fees.

## What you get

- **/voice-on** inside any session: hands-free conversation with that exact
  session. Talk, hear the reply, talk again. `/voice-off` or say "stop
  listening" to end.
- **Wake-word mode** (`/voice-wake`): ambient, only reacts to "hey Claude
  ...", ignores everything else. Agent mode (`/voice-agent`) takes
  everything.
- **Barge-in**: talk over a long answer out loud and it stops and takes
  your words as the next prompt.
- **Natural voice**: Kokoro neural TTS (54 voices, runs on CPU, Apache
  licensed) with macOS `say` as fallback. `vb engine kokoro`.
- **Phone, free**: a PWA "call" page (`vb call on` + `vb call tunnel`),
  open on your phone, tap Start once, then talk hands-free from anywhere.
  Plus a Telegram bridge (`vb remote on`) for voice notes.
- Smart capture: thinking pauses don't split your prompt, mic noise is
  filtered, and it never hears its own voice (echo guard).

## Quickstart

**As a Claude Code plugin (easiest).** Inside any Claude Code session:

```
/plugin marketplace add cc-vb/voicebridge
/plugin install voicebridge@voicebridge
/voicebridge:setup
```

Setup installs the speech stack (brew packages + local models, one time,
5-10 min) and runs a health check. Grant the two macOS prompts (Microphone
+ Accessibility for your terminal app), then:

```
/voicebridge:voice-on
```

Speak. That's it.

**The commands to know (this is all most people need):**

| Command | What it does |
|---|---|
| `/voicebridge:voice-on` | start talking to this session, hear replies |
| `/voicebridge:voice-wake` | hands-free; only reacts to "hey Claude ..." |
| `/voicebridge:voice-off` | stop |
| `vb phone` | use it from your phone (prints a QR to scan) |
| `vb voice <name>` | change the voice (`vb voice` lists all) |

(Cloned directly instead of the plugin? Same commands without the
`voicebridge:` prefix.)

**Or clone it directly:**

```bash
git clone https://github.com/cc-vb/voicebridge ~/voicebridge
~/voicebridge/install.sh        # deps, model, hooks, commands, health check
```

Then use `/voice-on`, `/voice-wake`, `/voice-agent`, `/voice-off` (no
namespace prefix).

## Install (fresh Mac)

macOS only for now (uses `say`, `osascript`, and CoreAudio via sox).

```bash
git clone https://github.com/cc-vb/voicebridge ~/voicebridge
~/voicebridge/install.sh
```

The installer is idempotent and handles everything: brew packages
(whisper-cpp, sox, ffmpeg), the whisper model download, the `vb` command,
the /voice-on and /voice-off slash commands, and hook registration in
`~/.claude/settings.json` (append-only; your existing hooks are untouched).
It finishes with `vb doctor`, a 9-point health check.

Two one-time macOS permissions are yours to grant when prompted, both for
your terminal app under System Settings -> Privacy & Security:
**Microphone** and **Accessibility**. Without Accessibility your speech is
transcribed but never typed into the session, that's the most common setup
miss, and `vb doctor` calls it out explicitly.

Quick check: `vb test` (should speak), then `/voice-on` inside any Claude
Code session. Note: a few docs and example `.mcp.json` files contain
absolute paths from the author's machine; adjust where noted.

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

### Agent mode: /voice-on inside any session - works on ANY account

The primary mode, and it works like /remote-control: toggle it per session,
from inside the session.

```
/voice-on     # in any Claude Code session: voice binds to THIS session
/voice-off    # leave voice mode (or just say "stop listening")
```

Then keep that window focused and talk: speech is transcribed locally and
typed into the session; replies are spoken aloud. Run multiple sessions?
Voice each one you want with /voice-on; the mic follows whichever voiced
session you interacted with most recently (there is only one mic). Binding
is exact, not guessed: the prompt hook records each session's identity, so
/voice-on attaches to the session you typed it in.

Details: noise like "(air whooshing)" is filtered, never sent; a reply that
lands while you're silent cuts the wait and is spoken immediately;
fire-and-forget injection means nothing can hang; hooks/watcher stay silent
while voice mode owns a session (no double-speak), with stale-flag
protection. Backend: `vb talkd on|off|status|stop`; slash commands live in
`commands/` (installed at `~/.claude/commands/`).

Tradeoff: the voiced session's window must stay focused when you speak (the
text lands at your cursor).

### Manual voice link (`vb talk`)

Older manual variant: binds to your most recently active session from
outside. Prefer /voice-on.

### Agent mode via Channels (`vb session`) - needs Channels enabled

The cleaner architecture when your account allows it (Pro/Max, or an org
Owner enabled Channels). One command starts a real Claude Code session with
your mic wired into it:

```
cd ~/my/project
vb session          # or: vb session ~/my/project
```

Accept the dev-channel dialog once, then just talk. Your speech is
transcribed locally and injected into the LIVE session (your files, your
context, your tools); Claude answers by speaking aloud. Continuous, no
buttons, no window focus. It's the same session you can also type into.

- Say **"stop listening"** to mute the mic (session keeps running); unmute
  with `vb mic on` from any terminal.
- **Permission prompts are spoken**: "I need permission to run Bash...
  say yes or no", and your voice answers them.
- Cues: Tink = listening, Pop = heard you, Morse tick = sent/thinking.
- While the mic is on, the Stop hook and watcher stay silent (the channel
  owns the voice), so nothing is ever spoken twice.
- Implementation: `channel/voicemode.ts`, a custom Claude Code channel
  (research preview, hence the `--dangerously-load-development-channels`
  flag that `vb session` passes for you). Registered user-scope via
  `claude mcp add`, so it works from any project directory.

**Headphones recommended but not required**: half-duplex, it never records
while speaking. Keep speaker volume moderate.

### Side-chat mode (`vb converse`)

A lighter alternative when you don't need a full session UI: talks to a
headless Claude (`claude -p --continue`) in the current directory and speaks
answers. Long turns get soft ticks and spoken "still working" updates.
Say "stop" (even "stop stop stop") to end; Ctrl-C is always clean.

The phone version of the call feel is Vapi (`mobile/vapi/`).

### Better voice: Kokoro (recommended)

Kokoro is an open neural TTS (Apache 2.0) that sounds far more human than
the default macOS voices, runs on plain CPU, and needs no Apple downloads.
One-time setup (~800MB total):

```bash
brew install python@3.10 2>/dev/null || true
/opt/homebrew/bin/python3.10 -m venv ~/.voicebridge/kokoro-venv
~/.voicebridge/kokoro-venv/bin/pip install kokoro-onnx soundfile
curl -fSL -o ~/.voicebridge/models/kokoro-v1.0.onnx --create-dirs \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -fSL -o ~/.voicebridge/models/voices-v1.0.bin \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

Then:

```
vb engine kokoro        # starts the local server, switches the engine
vb voice af_heart       # pick any of 54 voices: curl -s localhost:8798/voices
vb test                 # hear it
```

`vb engine say` switches back; if the Kokoro server is down, speech
automatically falls back to `say`, nothing breaks.

### Speaking Hindi / Hinglish (or other languages)

**Language is auto-detected by default**: speak English, Hindi, or
Hinglish, switch mid-conversation, and both recognition and the reply
language follow you (Hindi replies are even spoken by the Mac's Hindi
voice automatically). Requires the multilingual model, one-time download:

```
curl -fSL -o ~/.voicebridge/models/ggml-small.bin --create-dirs \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin
```

Pinning instead of auto: `vb lang english` (slightly better pure-English
accuracy), `vb lang hindi` (force Devanagari), `vb lang auto` (default).
Control words ("stop listening", "hey Claude") remain English. If Hinglish
captures get dropped, loosen the gate with `vb sens relaxed`.

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

## Use it on your phone (free, hands-free)

Your Mac stays home running the session; your phone becomes the call.

1. On the Mac, in the project you want to talk about:
   ```
   claude                              # keep this session open and focused
   VB_CALL_SECRET=pick-a-secret vb call on
   vb call tunnel                      # prints your PHONE URL (free)
   ```
2. On the phone, open the printed URL with `?k=pick-a-secret`, then use the
   browser's **Add to Home Screen**, voicebridge becomes an app icon that
   opens fullscreen.
3. Tap **Start call** once. Then just talk: your speech reaches the live
   session, and the phone speaks Claude's replies with its own neural
   voice. Say "end call" to hang up.

`vb call tunnel` prints the link **and a scannable QR code**, just point
your phone camera at the terminal, no typing the URL. Keep the Mac awake
while away (`caffeinate -dims`).

**Permanent link (optional, free):** the quick tunnel gives a new URL each
run. For a URL that never changes, use Tailscale Funnel:
```
brew install tailscale && tailscale up
tailscale funnel 8790                 # prints a stable https URL
vb call link <that-url>               # save it; vb call tunnel now shows it
```
After that, `vb call tunnel` just displays your permanent link + QR every
time. `vb call link off` returns to quick tunnels.

Prefer chat? `vb remote on` bridges a Telegram bot instead: send text or a
voice note, get the reply back as text plus a spoken voice note
(`mobile/TELEGRAM_NATIVE.md` covers the official text-only plugin too).

## Mobile internals

Voice and chat on your phone, driving your live session. See `mobile/`:

- **Telegram, text** (native, zero code): [mobile/TELEGRAM_NATIVE.md](mobile/TELEGRAM_NATIVE.md)
- **Telegram, voice** (hear Claude back): [mobile/telegram-voice/](mobile/telegram-voice/)
- **Vapi true call** (real-time): [mobile/vapi/](mobile/vapi/)
- Notifications-only baseline (Claude app + Remote Control): [MOBILE.md](MOBILE.md)

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full phased plan (STT input,
barge-in, decision-moment voice, mobile diff summary).

## License

MIT

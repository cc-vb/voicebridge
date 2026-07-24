# voicebridge setup

## Dependencies (already installed if you ran the build)

```
brew install whisper-cpp sox
```

Model (~142MB, downloaded to `~/.voicebridge/models/ggml-base.en.bin`):

```
curl -fSL -o ~/.voicebridge/models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

Check readiness: `vb stt` should print `ready : True`.

## macOS permissions (first run of `vb listen`)

macOS will prompt for these the first time. If it doesn't, grant them
manually in **System Settings -> Privacy & Security**:

1. **Microphone** -> enable your terminal app (Terminal / iTerm / the app
   running `vb listen`). Needed for `rec` to record.
2. **Accessibility** -> enable the same app. Needed for the Cmd+V paste
   keystroke that drops your transcribed text into the Claude window.

If a paste does nothing, it's almost always the Accessibility toggle.

## Hands-free hotkey (already set up via skhd)

A global hotkey is installed and the skhd service is running:

```
Hotkey:  Cmd + Alt + Ctrl + V
Action:  vb listen --send --cue   (hush, record, transcribe, paste, send)
Config:  ~/.skhdrc
```

**One manual step:** grant skhd **Accessibility** permission, or the hotkey
won't fire. System Settings -> Privacy & Security -> Accessibility -> add /
enable `skhd` (`/opt/homebrew/bin/skhd`). Also grant **Microphone** to skhd
the first time it records.

Audio cues (so you get feedback with no terminal): Tink = started
listening, Pop = stopped, Basso = heard nothing.

Manage the service:

```
skhd --restart-service    # after editing ~/.skhdrc
skhd --stop-service       # disable the hotkey
```

Change the key by editing `~/.skhdrc` then restarting the service.

### Speaking speed and pause hotkeys

`install.sh` writes all of these, and adds any that are missing when you
re-run it, so an older install picks up new keys on the next update.

| Do this | Press | Or press | Runs |
|---|---|---|---|
| Speak faster (+0.25x) | **Fn + F9** | Cmd+Alt+Ctrl+**F** | `vb faster` |
| Speak slower (-0.25x) | **Fn + F7** | Cmd+Alt+Ctrl+**S** | `vb slower` |
| Pause / resume | **Fn + F8** | Cmd+Alt+Ctrl+**H** | `vb hold` |
| Silence the voice | — | Cmd+Alt+Ctrl+**X** | `vb hush` |
| Silence + stop Claude | — | Cmd+Alt+Ctrl+**Z** | `vb stop` |

**Why Fn.** On a Mac the top row sends media keys by default, so a bare F8
reaches the music player rather than skhd and the shortcut looks dead. Hold
**Fn** and the real F-key gets through. To drop the Fn, switch on System
Settings -> Keyboard -> "Use F1, F2, etc. keys as standard function keys";
plain F7/F8/F9 then work. The Cmd+Alt+Ctrl chords ignore that setting
entirely and always fire, so use them if you would rather not change it.

**Pause is not the same as silence.** `vb hold` freezes the reply mid-word
and the second press carries on from that word. `vb hush` ends the reply
for good. Both are worth having; they are not two names for one thing.

Speed runs 0.5x to 3.5x. Changing it mid-reply lands on the next sentence
and confirms with a tick, so it never talks over the reply you are
adjusting. You can also say **"speak faster"**, **"slow down"**, or
**"normal speed"** in agent or wake mode, or run `vb speed 1.5`.

### Alternatives (no skhd)

- **macOS Shortcuts:** new shortcut -> Run Shell Script ->
  `~/voicebridge/bin/vb listen --send --cue`, assign a key.
- **Raycast / Alfred:** a Script Command running the same line.

## Seeing whether the mic is actually open

The most common "is this thing on?" moment. Two indicators, both opt-in,
and no window pops up on its own.

**Status line (recommended).** Puts the state into Claude Code's own status
line, right under your prompt:

```
vb statusline-install     # then restart Claude Code once
vb statusline-uninstall   # ...and that puts it back exactly as it was
```

**Your status line is yours.** If you already have one, we do not replace
it and we do not grow its last line. The installer writes a small wrapper
(`~/.voicebridge/statusline.sh`) that runs *your* command first, hands it
the same JSON payload Claude Code would have, prints its output verbatim ,
however many lines that is , and then adds ONE line of our own underneath:

```
MacBook · GH:you · Opus · git:main · 12k        ← your line 1, untouched
🧠 gfy:OFF · crg:17f/282n/2038e                 ← your line 2, untouched
vb 🎙 voice on                                  ← ours, its own line
```

Your columns stay where you put them. A state that changes every few
seconds has no business in the middle of a line you read left to right.

The segment is state only , `🎙 voice on`, `🎙 hearing you`, `✍ working`,
`🔊 speaking`, `💤 wake-word`, `🔉 reads replies`, `⏸ paused`. No tips, no
rotating hints: on a line you share with git and cost, a suggestion you have
already read is just noise.

**When voice is off, the extra line isn't there at all** , not "voice off",
nothing, not even a blank line , so your status line looks exactly as it did
before you installed voicebridge. The one exception is a pending update,
which is rare, actionable, and goes away once you take it.

Claude Code redraws the line on activity rather than continuously, so treat
it as "what mode am I in", not a live level meter , that's `vb meter` below.

Your settings are backed up to `~/.claude/settings.json.vbbak`, and the
exact `statusLine` block you had is saved to
`~/.voicebridge/statusline_orig.json` so the uninstall is a real undo rather
than a guess. If your status line isn't a `command` type, we refuse to touch
it and tell you what to add by hand.

**Live meter.** For a real moving bar, run this in a second terminal tab or
a tmux split:

```
vb meter
```

It redraws about fifteen times a second, so the bar rises and falls as you
speak , the quickest way to prove the mic is hearing you. Ctrl-C to stop.

**Floating orb.** `vb orb` shows a small draggable window that pulses with
your voice; `vb orb off` hides it. It is opt-in only , `/voice-on` does not
spawn it.

## The loop, day to day

1. `vb on` once (starts the watcher; Claude speaks its replies).
2. Wear headphones.
3. Hit your hotkey, speak, pause. Your words paste into Claude and send.
4. Claude replies out loud. Hit the hotkey again to cut in and respond.

Turn it all off with `vb off`.

## Tuning

- Voice/speed of output: `VOICEBRIDGE_VOICE`, `VOICEBRIDGE_RATE` env vars.
  List voices: `say -v '?'`.
- Silence sensitivity / max length of a recording: `record()` args in
  `vb/stt.py` (`silence_stop`, `max_secs`).
- Bigger/more accurate model: swap `ggml-base.en.bin` for
  `ggml-small.en.bin` (slower, more accurate) and update `MODEL` in
  `vb/stt.py`.

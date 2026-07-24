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

### Speaking speed hotkeys

```
F9  /  Cmd + Alt + Ctrl + F   speak faster (+0.25x)
F7  /  Cmd + Alt + Ctrl + S   speak slower (-0.25x)
```

Both pairs run `vb faster` / `vb slower` (0.5x to 3.5x). Plain **F9/F7 only
reach skhd if the keyboard is in "standard function keys" mode** — otherwise
macOS consumes them as media keys and you must press **Fn+F9**. Turn the mode
on in System Settings -> Keyboard -> "Use F1, F2, etc. keys as standard
function keys", or just use the Cmd+Alt+Ctrl chords, which always fire.

You can also say **"speak faster"** / **"speak slower"**, or run `vb speed 1.5`.

### Alternatives (no skhd)

- **macOS Shortcuts:** new shortcut -> Run Shell Script ->
  `~/voicebridge/bin/vb listen --send --cue`, assign a key.
- **Raycast / Alfred:** a Script Command running the same line.

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

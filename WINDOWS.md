# voicebridge on Windows

Windows 10 and 11, x64. The install and everything that does not need a
microphone or speakers is tested on a clean Windows machine by CI
([`.github/workflows/windows.yml`](.github/workflows/windows.yml)). The parts
that need hardware are checked by a one-minute script you run yourself
([below](#check-it-on-your-pc)).

## Install

In PowerShell (a normal one, not as administrator):

```powershell
irm https://raw.githubusercontent.com/cc-vb/voicebridge/main/install.ps1 | iex
```

or from a clone: `powershell -ExecutionPolicy Bypass -File install.ps1`.
It asks no questions and is safe to run again. It sets up:

| Step | What | Where |
|---|---|---|
| Python | finds Python 3.9+ (installs 3.12 with winget if there is none) | |
| Code | `git clone` (or a zip when git is missing) | `%USERPROFILE%\voicebridge` |
| Recorder | `pip install --user sounddevice` | |
| Speech to text | whisper.cpp b5454 Windows build, SHA256-pinned | `%USERPROFILE%\.voicebridge\whisper` |
| Model | `ggml-small.en.bin`, ~466MB | `%USERPROFILE%\.voicebridge\models` |
| `vb` command | `vb.cmd`, folder added to your user PATH | `%USERPROFILE%\.voicebridge\bin` |
| Voice | Kokoro in its own venv, ~400MB (skip with `$env:VB_KOKORO="0"`) | `%USERPROFILE%\.voicebridge\kokoro-venv` |
| Claude Code | hooks in `settings.json`, slash commands | `%USERPROFILE%\.claude` |

Then:

1. **Microphone:** Settings > Privacy & security > Microphone, turn on
   "Let desktop apps access your microphone".
2. Open a **new** terminal (PATH changes only reach new ones) and run
   `vb doctor`.
3. In any Claude Code session: `/voice-on`, and talk.

## Check it on your PC

```powershell
powershell -ExecutionPolicy Bypass -File $HOME\voicebridge\scripts\windows-smoke.ps1
```

It checks Python, `vb` on PATH, whisper, the model, your microphone, records
3 seconds, transcribes them, speaks a line and pastes into Notepad, and
prints PASS or FAIL with the fix for each step. If something fails, paste the
output into an issue.

## What is different from the Mac

| Piece | Mac | Windows |
|---|---|---|
| Recording | sox `rec` | `vb/winrec.py` (sounddevice), same silence rules |
| Speech to text | whisper.cpp | whisper.cpp, same models |
| Voice | Kokoro | Kokoro, same |
| Fallback voice | `say` | SAPI (`System.Speech`) |
| Typing into the session | paste by AppleScript, or straight into the Terminal tab | clipboard paste (`Ctrl+V` by SendKeys), Unicode kept |
| Playing audio | afplay | `System.Media.SoundPlayer` |
| Pausing a reply (`vb hold`) | SIGSTOP | suspends the player process tree |
| Hotkeys | skhd | AutoHotkey (below) |

Why no sox: Windows has no `rec`, no dependable winget package for sox, and
its device naming would need special cases everywhere. sounddevice ships
PortAudio inside its wheel, so one `pip install` is the whole dependency.

## Hotkeys

skhd is macOS only. With [AutoHotkey](https://www.autohotkey.com/) v2:

```ahk
^!#z:: Run "vb stop"      ; Ctrl+Alt+Win+Z: silence and stop Claude
^!#x:: Run "vb hush"      ; Ctrl+Alt+Win+X: silence
F9::  Run "vb faster"
F7::  Run "vb slower"
```

Or just start typing: that always interrupts.

## What CI proves on every push to the Windows branch

- `irm ... | iex` installs end to end on a clean `windows-latest`, under
  Windows PowerShell 5.1, with no input, and a second run changes nothing
- `vb` resolves on PATH in a new shell and `vb --help` and `vb stt` work
- the hooks are registered with the real Python (not the `python3` Store
  stub) and the slash commands are copied
- `whisper-cli.exe` transcribes a recorded sentence correctly
- the recorder writes a valid, correctly gated WAV (fed from a file: CI
  machines have no microphone)
- the Windows voice writes a non-empty WAV, with a typographic apostrophe
  in the text
- the clipboard round-trips Hindi and typographic quotes unchanged
- the whole pytest suite passes on Windows, including Kokoro synthesising
  speech and the process checks (a liveness check never kills, pausing
  reaches the child that plays the sound, another process's working
  directory reads back correctly)

## What only a person can check

Run the smoke script above for these:

- a real microphone: device, permission, level
- hearing the voice through real speakers
- a paste landing in a real focused window
- the AutoHotkey bindings

## Known gaps

- **Session targeting.** On a Mac a prompt can go straight into one
  Terminal tab by its tty, without bringing the window forward. Windows has
  no equivalent, so it pastes into the bound app's window and refuses if a
  different app is in front. Keep the terminal you voiced in focused.
- **`vb session` (the Bun channel) is macOS only**: it plays audio with
  afplay. `/voice-on`, which is the normal path, does not use it.
- **The floating orb is macOS only** (a Swift app).
- **Spoken summaries need Ollama on Windows.** The Mac uses Apple's model
  or MLX first; on Windows only the Ollama backend applies, and without it
  replies are read in full (summaries are off by default anyway).
- **ARM64 Windows is untested.** The whisper build is x64.

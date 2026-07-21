# voicebridge on Windows (beta, needs a tester)

The core is now cross-platform (`vb/oslayer.py` abstracts every OS-specific
call). Windows uses built-in SAPI for the fallback voice and SendKeys for
typing into the session; Kokoro (the good voice) and whisper.cpp run the
same as on Mac. **This path is built but not yet verified on a real Windows
machine, help testing is welcome.**

## Install

```powershell
irm https://raw.githubusercontent.com/cc-vb/voicebridge/main/install.ps1 | iex
```
or from a clone: `powershell -ExecutionPolicy Bypass -File install.ps1`

The installer sets up: ffmpeg + Python (via winget), the whisper model,
a `vb` command shim, Kokoro voice, hooks, and the slash commands.

## Two manual pieces

1. **Recording**: install a recorder on PATH. Easiest is sox
   (`scoop install sox` or `choco install sox`), which `vb` already knows
   how to drive. Grant your terminal Microphone permission (Windows
   Settings -> Privacy & security -> Microphone).
2. **whisper.cpp**: put `whisper-cli.exe` on PATH (winget, or build from
   ggerganov/whisper.cpp). `vb stt` tells you if it's found.
3. **Interrupt hotkey** (optional): skhd is macOS-only. On Windows use
   [AutoHotkey](https://www.autohotkey.com/) with a one-line script:
   ```ahk
   ^!#z:: Run, vb stop      ; Ctrl+Alt+Win+Z = stop
   ^!#x:: Run, vb hush      ; Ctrl+Alt+Win+X = silence
   F9::  Run, vb faster     ; F9 = speed up
   F7::  Run, vb slower     ; F7 = slow down
   ```
   Or just start typing, that always interrupts.

## What's shared vs platform-specific

| Piece | Mac | Windows |
|---|---|---|
| STT | whisper.cpp | whisper.cpp (same) |
| Voice (Kokoro) | local server | local server (same) |
| Fallback voice | `say` | SAPI (`System.Speech`) |
| Type into session | osascript paste | SendKeys `^v` |
| Interrupt key send | osascript Esc | SendKeys `{ESC}` |
| Play audio | afplay | `System.Media.SoundPlayer` |
| Hotkey daemon | skhd | AutoHotkey |

## Known gaps (contributions welcome)

- App-binding (voice only pastes into the bound app) is macOS-only so far;
  on Windows the paste goes to the focused window, keep it focused.
- The `vb phone` QR/tunnel path is untested on Windows (cloudflared has a
  Windows build; should work).
- No `vb doctor` Windows-specific checks yet.

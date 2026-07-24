# Accessibility

voicebridge exists so you can build software by **conversation** instead of by
keyboard and screen. For a lot of people that's a convenience. For some, it's
the difference between coding and not.

## Who this helps

- **RSI, wrist strain, tendonitis.** Dictate prompts and hear replies without
  a keyboard. Every control has a spoken command or a single hotkey, so a
  full session can run with almost no typing.
- **Limited hand mobility.** `/voice-agent` is fully hands-free: talk, listen,
  talk back. Interrupt by talking over a reply, or with one key.
- **Low vision / eye strain.** Replies are read aloud in a natural voice, so
  you don't have to read walls of text. Long code blocks are announced
  ("about 30 lines of Python on screen") rather than skipped silently, and
  replies lead with a one-line spoken summary so you get the point first.
- **Focus / reading fatigue.** Hear the gist, then drill in only where you
  want, at a speed you choose (0.5x to 3.5x).

## What's built for it

- **Speak, don't type.** `/voice-agent` (hands-free) and `/voice-wake`
  ("hey Claude ...") capture your voice; `/voice-on` reads replies aloud
  while you type at your own pace.
- **Follow along by ear.** Pause and resume mid-word (Fn+F8), go back or skip
  a sentence, or re-hear the last reply (Fn+F6), all without a mouse.
- **Recover easily.** Lost your place? `back` / `repeat` on a hotkey re-speak
  the sentence or the whole reply. No need to find your spot on screen.
- **Your pace.** "faster" / "slower" by voice or Fn+F9 / Fn+F7, and a pause
  that resumes mid-sentence so you can catch up reading.
- **Local and private.** Speech-to-text (whisper.cpp) and text-to-speech
  (Kokoro) run on your machine. Your voice and code never leave it.
- **No hard dependency on precise input.** Every action has a spoken form or
  a large, forgiving hotkey chord (Cmd+Alt+Ctrl+letter) that works regardless
  of the function-key setting.

## Honest limits (today)

- macOS is the tested platform; Windows/Linux support is in progress.
- Injecting your spoken prompt requires the bound window to be focused (a
  safety feature so speech never lands in the wrong app).
- Voice commands ("faster", "repeat") work in mic-on modes; in speak-only
  mode use the hotkeys instead.

If a specific need isn't covered, that's a bug worth filing, accessibility is
a first-class goal here, not an afterthought.

# telegram-voice channel

A custom Claude Code channel that gives you a **voice** companion on your
phone, talking to your **live** session. Voice both ways, all STT/TTS local:

- you send a **voice note** (or text) from Telegram
- it transcribes locally with whisper.cpp and injects into your session
- Claude works against your real files and replies
- the reply comes back as **text + a spoken voice note** you play
- permission prompts relay to Telegram (`yes <id>` / `no <id>`)

This is the "hear Claude on my phone" piece that native text-only channels
don't do. It handles text too, so use it instead of the native plugin
(one poller per bot).

## Setup

1. **Deps** (once):
   ```
   cd /Users/krishojha/voicebridge/mobile/telegram-voice
   bun install
   ```
   Needs `whisper-cli`, `ffmpeg`, `say` (all installed during the build) and
   the model at `~/.voicebridge/models/ggml-base.en.bin`.

2. **Bot token:** create a bot via [@BotFather](https://t.me/BotFather)
   (`/newbot`), then save the token:
   ```
   mkdir -p ~/.voicebridge/telegram
   printf 'TELEGRAM_BOT_TOKEN=12345:ABC...\n' > ~/.voicebridge/telegram/.env
   ```
   Use a **different** bot than the native plugin if you run both.

3. **Register the server** for any project (user-level), add to
   `~/.claude.json` under `mcpServers` (absolute path already set in the
   local `.mcp.json` you can copy), or just run Claude Code from a dir that
   has this `.mcp.json`.

4. **Run with the channel on** (custom channels need the dev flag during
   the research preview):
   ```
   claude --dangerously-load-development-channels server:telegram-voice
   ```
   Accept the dev-channel warning and the new-MCP-server consent.

5. **Allowlist yourself:** message your bot once. It replies with your
   Telegram id. Add it:
   ```
   echo "<your-id>" >> ~/.voicebridge/telegram/allow
   ```
   Restart the channel. Now only you can talk to it.

## Use it

From anywhere: hold the Telegram mic, speak, send. Claude replies with a
voice note you play. It's talking to the session on your Mac against your
real files. Keep a session open (or a persistent one) so messages arrive.

## Notes / tuning

- Replies are phrased to be heard (the channel instructions tell Claude to
  keep it conversational). TTS voice/rate: it uses macOS `say`.
- Bigger model = better transcription: swap the model path in `channel.ts`.
- Security: only sender ids in `~/.voicebridge/telegram/allow` are accepted;
  everything else is dropped.
- Privacy: STT/TTS run locally; only the audio/text sent to Telegram leaves
  the machine.

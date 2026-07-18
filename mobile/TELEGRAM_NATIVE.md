# Telegram - native text channel (fastest agent-like phone win)

This uses Anthropic's official Telegram channel plugin. It drives your
**live** local session from your phone: you message the bot, it injects
into the running Claude Code session against your real files, Claude replies
back in Telegram, and it can even relay permission prompts so you approve
tool use from your phone. Zero code. **Text only** (for voice, use the
custom voice channel in `telegram-voice/`).

Requires **Bun** (installed during the build; check `bun --version`).

## Setup (about 3 minutes)

1. **Create a bot:** open [@BotFather](https://t.me/BotFather) in Telegram,
   send `/newbot`, pick a name and a username ending in `bot`, copy the
   token it gives you (looks like `12345:ABC-DEF...`).

2. **Install the plugin** (in a Claude Code session):
   ```
   /plugin install telegram@claude-plugins-official
   /reload-plugins
   ```
   If it says the marketplace isn't found:
   ```
   /plugin marketplace add anthropics/claude-plugins-official
   ```

3. **Configure the token:**
   ```
   /telegram:configure <token>
   ```

4. **Restart Claude Code with the channel on:**
   ```
   claude --channels plugin:telegram@claude-plugins-official
   ```

5. **Pair your phone:** message your bot anything; it replies with a
   pairing code. Back in Claude Code:
   ```
   /telegram:access pair <code>
   /telegram:access policy allowlist
   ```

## Use it

From anywhere, text your bot. It talks to the session running on your Mac.
Keep a Claude Code session open (or a persistent/background one) so
messages arrive.

Permission prompts relay to Telegram: reply `yes <id>` or `no <id>`.

## Note: one bot, one poller

Telegram only allows one long-poller per bot. So run **either** this native
plugin **or** the custom voice channel on a given bot, not both at once.
The voice channel handles text too, so if you want voice, use a separate
bot for it (or just use the voice channel for everything).

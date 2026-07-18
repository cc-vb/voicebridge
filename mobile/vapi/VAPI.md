# Vapi relay - true phone call to your live session

Dial a number, talk in real time, and Vapi's speech reaches the Claude Code
session running on your Mac; Claude's reply is spoken back on the call.

This is the heaviest path and the one you actually pay for. Be honest with
yourself about whether the free Telegram voice channel is enough before
setting this up.

## What it costs / needs

- A **Vapi** account (voice-AI platform) + a **phone number** (per-minute cost).
- A **public tunnel** (e.g. `ngrok`, `cloudflared`) to expose the local relay.
- Claude Code running with the relay channel on (turns can take seconds;
  configure Vapi patience/filler so the call doesn't feel dead).

## How it fits together

```
phone --call--> Vapi --STT--> POST /chat/completions --> relay --> live session
     <--speak-- Vapi <--TTS--  reply text  <--------------------  reply tool
```

Vapi treats the relay as an OpenAI-compatible "custom LLM". The relay is
also a Claude Code channel, so each turn injects into your real session and
Claude's reply (via the channel `reply` tool) is returned for Vapi to speak.

## Setup

1. **Deps:**
   ```
   cd /Users/krishojha/voicebridge/mobile/vapi && bun install
   ```

2. **Start Claude Code with the relay channel** (from a dir with this
   `.mcp.json`, or register it in `~/.claude.json`):
   ```
   claude --dangerously-load-development-channels server:vapi-relay
   ```
   The relay listens on `127.0.0.1:8790` (set `VAPI_RELAY_PORT` to change).
   Optional: set `VAPI_SHARED_SECRET` and send it as a Bearer token from
   Vapi so only Vapi can post.

3. **Tunnel the port:**
   ```
   ngrok http 8790          # or: cloudflared tunnel --url http://localhost:8790
   ```
   Copy the public https URL.

4. **Configure Vapi:**
   - Create an Assistant.
   - Set its Model to **Custom LLM**, URL =
     `https://<your-tunnel>/chat/completions`.
   - If you set `VAPI_SHARED_SECRET`, add it as the Authorization Bearer.
   - Pick a voice (TTS) and a transcriber (STT) in Vapi.
   - Attach a phone number.

5. **Call the number and talk.** Each thing you say is injected into your
   live session; Claude's spoken reply comes back on the call.

## Reality check

- Latency: a Claude Code turn is not sub-second. Set Vapi's response timeout
  high and enable a filler phrase so silence doesn't read as a dropped call.
  `VAPI_TURN_TIMEOUT_MS` (default 120000) caps how long the relay waits.
- Concurrency: this relay assumes one call at a time (one in-flight turn).
- Verified so far: the relay bundles and exposes the endpoint; the full call
  path is only testable once your Vapi account + tunnel are live.

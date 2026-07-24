---
description: Show every voicebridge voice command - slash commands, hotkeys, spoken commands, and vb CLI
allowed-tools: Bash(vb help), Bash(/opt/homebrew/bin/vb help), Bash(vb talkd status), Bash(/opt/homebrew/bin/vb talkd status)
---

Run `/opt/homebrew/bin/vb talkd status` with the Bash tool to see whether
voice is currently on, then show the user this reference verbatim, with a
first line saying whether voice is ON for this session or OFF.

Do not paraphrase or shorten the tables. Do not run any other command.

---

**Talk to a session** (type these inside the session you want to talk to)

| Command | What it does |
|---|---|
| `/voice-on` | Speak-only: replies read aloud; you type or press space to dictate |
| `/voice-agent` | Hands-free: our mic listens to everything you say |
| `/voice-wake` | Hands-free, but only reacts to "hey Claude ..." |
| `/voice-off` | Stop voice, this session only |
| `/voice-off-all` | Stop voice everywhere and release the mic, from any terminal |
| `/phone` | Talk from your phone: prints a QR to scan |
| `/voice-help` | This list |

**Interrupt me** (least to most forceful)

| Way | What happens |
|---|---|
| Just start typing | Cuts the voice off and takes your new prompt |
| Talk over the reply | Barge-in: stops speaking and takes your words |
| **Cmd+Alt+Ctrl+X** | Silences the voice immediately (`vb hush`) |
| **Cmd+Alt+Ctrl+Z** | Silences AND stops Claude mid-answer (`vb stop`) |
| **F9 / F7** | Speak faster / slower |

**Say these out loud** (no typing needed)

| Say | What happens |
|---|---|
| "stop listening" | Leaves voice mode for this session |
| "agent mode" / "wake word mode" | Switches listening mode |
| "speak faster" / "slower" | Changes playback speed |
| "continue" | Resumes a reply that hit the character cap |
| "which agents need me?" | Hear which sessions are waiting vs working |
| "switch to `<name>`" | Move your voice to another session |
| "read me `<name>`'s last reply" | Hear another session's latest reply |

**From the terminal** (`vb` controls everything, not just this session)

| Command | What it does |
|---|---|
| `vb talkd status` | Is voice on? Which session, which app, mic state |
| `vb phone` | Phone access: QR code + link |
| `vb remote on` | Telegram bridge: text or voice-note your bot |
| `vb call on` | A real phone call to your live session (needs Vapi) |
| `vb sessions` | List running sessions |
| `vb alerts on\|off` | Announce when another agent finishes |
| `vb voice <name>` | Change the voice (`vb voice` lists all) |
| `vb speed 1.5` | Playback speed, 0.5x to 3.5x |
| `vb hold` | Pause the reply; run again to resume (F8) |
| `vb pause <secs>` | Silence that ends an utterance (default 2.5) |
| `vb doctor` | Health check |
| `vb selftest` | Not hearing you? This says exactly why |
| `vb help` | Full CLI guide |

**Rule of thumb:** `/commands` control THIS session. `vb ...` controls everything.

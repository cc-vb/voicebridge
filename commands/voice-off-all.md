---
description: Voice mode OFF everywhere - clears every session and releases the mic (voicebridge)
allowed-tools: Bash(vb talkd:*), Bash(/opt/homebrew/bin/vb talkd:*)
---

Run `/opt/homebrew/bin/vb talkd off-all` with the Bash tool now.

Then reply with exactly: "Voice off everywhere." followed by the status
block the command printed, so the user can see the daemon is stopped and
no sessions remain.

---
description: Wake-word mode - voice only reacts to "hey Claude ..."; everything else typed
allowed-tools: Bash(vb talkd:*), Bash(vb mode:*), Bash(/opt/homebrew/bin/vb:*)
---

Run these two commands with the Bash tool now:
1. `/opt/homebrew/bin/vb talkd on`
2. `/opt/homebrew/bin/vb mode wake`

If both succeeded, reply with exactly: "Wake mode. I'll only listen when
you say hey Claude." If either printed an ERROR, relay it instead.

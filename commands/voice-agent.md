---
description: Agent mode - continuous voice conversation, every utterance goes to Claude
allowed-tools: Bash(vb talkd:*), Bash(vb mode:*), Bash(/opt/homebrew/bin/vb:*)
---

Run these two commands with the Bash tool now:
1. `/opt/homebrew/bin/vb talkd on`
2. `/opt/homebrew/bin/vb mode all`

If both succeeded, reply with exactly: "Agent mode. I'm taking everything
now." If either printed an ERROR, relay it instead.

---
description: Use voicebridge from your phone - starts the call relay and shows a QR to scan
allowed-tools: Bash(vb phone), Bash(/opt/homebrew/bin/vb phone), Bash(vb:*), Bash(/opt/homebrew/bin/vb:*)
---

Run `/opt/homebrew/bin/vb phone` with the Bash tool now (it returns on its
own once the link is ready; do not wait beyond that).

Then show the user the PHONE URL from the output and tell them in one line:
open it on your phone (or scan the QR), tap Start call, and talk. Keep this
Mac awake and the session focused. If the output shows an error (e.g.
cloudflared missing), relay that instead.

---
description: Use voicebridge from your phone - starts the call relay and shows a QR to scan
allowed-tools: Bash(vb phone), Bash(/opt/homebrew/bin/vb phone), Bash(vb:*), Bash(/opt/homebrew/bin/vb:*), Read
---

Run `/opt/homebrew/bin/vb phone` with the Bash tool now (it returns on its
own once the link is ready; do not wait beyond that).

Claude Code COLLAPSES tool output, so the user cannot see the QR the command
printed. You MUST re-print it in your reply, which is never collapsed:

1. Read the file `~/.voicebridge/phone_qr.txt` (the command just wrote it).
2. Paste its QR block VERBATIM in your reply inside a fenced code block
   (no edits, no trimming, every line exactly as-is), followed by the
   PHONE URL on its own line.
3. Then one line: scan the QR (or open the URL) on your phone, tap Start
   call, and talk. Keep the Mac awake and the session focused.

If the command output shows an error (e.g. cloudflared missing), relay that
instead.

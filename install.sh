#!/bin/bash
# voicebridge installer: one command from clone to talking.
# Idempotent: safe to re-run any time. macOS only.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$HOME/.voicebridge"
MODEL="$STATE_DIR/models/ggml-small.en.bin"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin"
CLAUDE_DIR="$HOME/.claude"

say_step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

[ "$(uname)" = "Darwin" ] || { echo "voicebridge is macOS-only for now."; exit 1; }

say_step "1/6 Homebrew packages (whisper-cpp, sox, ffmpeg)"
if ! command -v brew >/dev/null; then
  echo "Homebrew is required: https://brew.sh"; exit 1
fi
for pkg in whisper-cpp sox ffmpeg; do
  if brew list "$pkg" >/dev/null 2>&1; then
    echo "  $pkg: already installed"
  else
    brew install "$pkg"
  fi
done

say_step "2/6 Whisper model (~466MB, one time)"
if [ -f "$MODEL" ]; then
  echo "  model: already present"
else
  mkdir -p "$(dirname "$MODEL")"
  curl -fSL -o "$MODEL" "$MODEL_URL"
fi

say_step "3/6 vb command on PATH"
BIN_DIR="/opt/homebrew/bin"; [ -d "$BIN_DIR" ] || BIN_DIR="/usr/local/bin"
ln -sf "$REPO_DIR/bin/vb" "$BIN_DIR/vb"
echo "  linked: $BIN_DIR/vb -> $REPO_DIR/bin/vb"

say_step "4/6 Slash commands (/voice-on, /voice-off)"
mkdir -p "$CLAUDE_DIR/commands"
for f in voice-on.md voice-off.md; do
  # Point the command at wherever vb actually landed on this machine.
  sed "s|/opt/homebrew/bin/vb|$BIN_DIR/vb|g" "$REPO_DIR/commands/$f" \
    > "$CLAUDE_DIR/commands/$f"
done
echo "  installed to $CLAUDE_DIR/commands/"

say_step "5/6 Hooks in ~/.claude/settings.json"
python3 - "$REPO_DIR" "$CLAUDE_DIR/settings.json" <<'PY'
import json, os, sys

repo, path = sys.argv[1], sys.argv[2]
settings = {}
if os.path.exists(path):
    with open(path) as f:
        settings = json.load(f)
hooks = settings.setdefault("hooks", {})

WANTED = {
    "Stop": f"{repo}/hooks/on_stop.py",
    "Notification": f"{repo}/hooks/on_notify.py",
    "UserPromptSubmit": f"{repo}/hooks/on_prompt.py",
}
changed = False
for event, script in WANTED.items():
    entries = hooks.setdefault(event, [])
    flat = json.dumps(entries)
    if "voicebridge/hooks/" in flat or script in flat:
        print(f"  {event}: voicebridge hook already registered")
        continue
    entries.append({
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": f'python3 "{script}" # voicebridge',
            "timeout": 5,
        }],
    })
    print(f"  {event}: registered")
    changed = True
if changed:
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
    print("  settings.json updated")
PY

say_step "6/6 Health check"
"$BIN_DIR/vb" doctor || true

cat <<'EOF'

Done. Two one-time macOS permissions remain (System Settings ->
Privacy & Security), for YOUR TERMINAL APP:
  * Microphone      (so it can hear you)
  * Accessibility   (so speech can be typed into your session;
                     without it you'll be heard but nothing appears)

Then, inside any Claude Code session:   /voice-on
Speak. Say "stop listening" to end.    Tune with: vb voice / vb rate / vb lang
EOF

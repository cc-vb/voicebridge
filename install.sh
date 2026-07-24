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

say_step "4/6 Slash commands (/voice-on, /voice-off, /phone, /voice-help, ...)"
mkdir -p "$CLAUDE_DIR/commands"
# Every command in commands/, not a hand-kept list: that list silently fell
# behind and left /phone, /voice-stop and /voice-off-all uninstalled, so they
# existed in the repo and did nothing on the machine.
for src in "$REPO_DIR"/commands/*.md; do
  # Point the command at wherever vb actually landed on this machine.
  sed "s|/opt/homebrew/bin/vb|$BIN_DIR/vb|g" "$src" \
    > "$CLAUDE_DIR/commands/$(basename "$src")"
done
echo "  installed $(ls -1 "$REPO_DIR"/commands/*.md | wc -l | tr -d ' ') commands to $CLAUDE_DIR/commands/"

say_step "5/7 Hooks in ~/.claude/settings.json"
if [ "${VB_SKIP_HOOKS:-}" = "1" ]; then
  echo "  skipped (plugin install provides hooks via hooks.json)"
else
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
fi

say_step "6/7 Kokoro neural voice (recommended, ~800MB one time)"
KOKORO_VENV="$STATE_DIR/kokoro-venv"
KOKORO_MODEL="$STATE_DIR/models/kokoro-v1.0.onnx"
# Kokoro is the default voice; set VB_KOKORO=0 only to skip it.
WANT_KOKORO="${VB_KOKORO:-1}"
if [ "$WANT_KOKORO" = "1" ]; then
  if ! [ -x /opt/homebrew/bin/python3.10 ] && ! [ -x /opt/homebrew/bin/python3.12 ]; then
    brew install python@3.12
  fi
  PYBIN=$(ls /opt/homebrew/bin/python3.1[0-9] 2>/dev/null | head -1)
  if [ ! -f "$KOKORO_VENV/bin/python" ]; then
    "$PYBIN" -m venv "$KOKORO_VENV"
    "$KOKORO_VENV/bin/pip" -q install --upgrade pip
    "$KOKORO_VENV/bin/pip" -q install kokoro-onnx soundfile
  fi
  pip3 install --quiet --user qrcode 2>/dev/null || true
  if true; then :
  fi
  [ -f "$KOKORO_MODEL" ] || curl -fSL -o "$KOKORO_MODEL" --create-dirs \
    https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
  [ -f "$STATE_DIR/models/voices-v1.0.bin" ] || curl -fSL \
    -o "$STATE_DIR/models/voices-v1.0.bin" \
    https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
  "$BIN_DIR/vb" engine kokoro
  "$BIN_DIR/vb" voice af_heart >/dev/null || true
  "$BIN_DIR/vb" tts on >/dev/null 2>&1 || true   # warm the server now
  echo "  Kokoro installed and set as the voice engine (voice: af_heart)"
else
  echo "  skipped (enable later per README 'Better voice: Kokoro')"
fi

say_step "7/8 Hotkeys (hush, interrupt, speed, pause)"
if brew list skhd >/dev/null 2>&1 || brew install koekeishiya/formulae/skhd; then
  SKHDRC="$HOME/.skhdrc"
  touch "$SKHDRC"
  # Add each binding on its own. The old guard asked "has voicebridge ever
  # written here?" and skipped everything if so, which meant an install
  # predating a new key never got that key, no matter how often it updated.
  # Keying off the individual shortcut also means a shortcut you rebound
  # yourself is left exactly as you set it.
  add_key() {   # add_key <shortcut> <vb subcommand> <comment>
    grep -qF "$1 :" "$SKHDRC" && return 0
    printf '\n# voicebridge: %s\n%s : %s %s\n' "$3" "$1" "$BIN_DIR/vb" "$2" \
      >> "$SKHDRC"
  }
  add_key "cmd + alt + ctrl - x" hush   "silence the voice instantly"
  add_key "cmd + alt + ctrl - z" stop   "silence AND stop Claude generating"
  add_key "f9"                   faster "speak faster (+0.25x)"
  add_key "f7"                   slower "speak slower (-0.25x)"
  add_key "f8"                   hold   "pause the reply; press again to resume"
  # F7/F8/F9 are the media-key row unless the keyboard is in standard
  # function-key mode, in which case they never reach skhd and the keys look
  # broken. These chords are immune to that setting, so there is always a
  # combination that works without changing a system preference.
  add_key "cmd + alt + ctrl - f" faster "speak faster (works without Fn)"
  add_key "cmd + alt + ctrl - s" slower "speak slower (works without Fn)"
  add_key "cmd + alt + ctrl - h" hold   "pause/resume (works without Fn)"
  add_key "f6"                   repeat "re-speak the last reply"
  add_key "cmd + alt + ctrl - r" repeat "re-speak the last reply (no Fn)"
  # Deliberately not `skhd --start-service`: that leaves a keyboard hook
  # running at login forever for a key that only matters while we speak.
  # voice-on starts skhd, voice-off stops it.
  echo "  hush/interrupt : Cmd+Alt+Ctrl+X  ·  Cmd+Alt+Ctrl+Z"
  echo "  speed / pause  : Fn+F9 faster · Fn+F7 slower · Fn+F8 pause"
  echo "                   (or Cmd+Alt+Ctrl+F / S / H, no Fn needed)"
  echo "  Plain F7/F8/F9 work once System Settings -> Keyboard ->"
  echo "  'Use F1, F2, etc. keys as standard function keys' is on."
  echo "  Grant skhd Accessibility the first time it starts."
else
  echo "  (skhd optional; typing a prompt also interrupts instantly)"
fi

say_step "8/8 Health check"
"$BIN_DIR/vb" doctor || true

cat <<'EOF'

Done. Two one-time macOS permissions remain (System Settings ->
Privacy & Security), for YOUR TERMINAL APP:
  * Microphone      (so it can hear you)
  * Accessibility   (so speech can be typed into your session;
                     without it you'll be heard but nothing appears)

Then, inside any Claude Code session:   /voice-on
Speak. Say "stop listening" to end.    Tune with: vb voice / vb rate / vb lang

Want to SEE whether the mic is open? Add the voice state to Claude Code's
status line. Yours is printed exactly as it is (however many lines it has)
and the voice state goes on ONE extra line underneath -- and that line is
absent entirely while voice is off:
  vb statusline-install     then restart Claude Code once
  vb statusline-uninstall   puts your original line back
Or run  vb meter  in a second terminal tab for a live moving level bar.
EOF

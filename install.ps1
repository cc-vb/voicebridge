# voicebridge Windows installer (PowerShell 5.1 or 7, Windows 10/11 x64).
#
#   irm https://raw.githubusercontent.com/cc-vb/voicebridge/main/install.ps1 | iex
#   or, from a clone:  powershell -ExecutionPolicy Bypass -File install.ps1
#
# Safe to run again: every step checks before it downloads or writes.
# Proven end to end on windows-latest by .github/workflows/windows.yml.
#
# Settings, all optional, as environment variables:
#   VB_HOME      where to put the code when not run from a clone ($HOME\voicebridge)
#   VB_REPO_URL  which repository to clone (https://github.com/cc-vb/voicebridge)
#   VB_REF       which branch or tag (main)
#   VB_KOKORO=0  skip the Kokoro voice (saves ~400MB; the Windows voice is used)

# Everything runs inside one script block: under `irm | iex` a bare `exit`
# would close the user's PowerShell window, and a `throw` here only ends
# the install.
& {
$ErrorActionPreference = "Stop"
# Windows PowerShell redraws its progress bar for every chunk, which makes
# a 466MB download take many minutes instead of seconds.
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Step($m) { Write-Host "`n== $m ==" -ForegroundColor Cyan }
function Note($m) { Write-Host "  $m" }
function Warn($m) { Write-Host "  $m" -ForegroundColor Yellow }
# Native programs (git, pip, winget) write progress to stderr. Windows
# PowerShell 5.1 turns that into error records whenever its streams are
# redirected, as they are in CI, and with "Stop" the first "Cloning into..."
# ends the install. So natives run with "Continue" and are judged by their
# exit code instead (Must), which is the only signal they mean.
function Native([scriptblock]$sb) {
  $ErrorActionPreference = "Continue"
  & $sb
}
function Must($what) {
  if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

# Download to a .part file and rename on success, so an interrupted download
# never leaves a truncated file that the next run sees as "present".
function Fetch($url, $out) {
  $part = "$out.part"
  Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $part
  Move-Item -Force $part $out
}

$State  = "$HOME\.voicebridge"
$Models = "$State\models"
$BinDir = "$State\bin"
$Claude = "$HOME\.claude"
New-Item -ItemType Directory -Force -Path $Models, $BinDir, "$Claude\commands" | Out-Null

# whisper.cpp, pinned. b5454 is the newest release that ships a Windows build
# (later tags have no assets), and the hash below is of the exact file we
# tested; a re-uploaded or tampered zip stops the install instead of running.
$WhisperUrl = "https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip"
$WhisperSha = "6BA69E3482D7826214F90A6A9C84CA07782AEC1E1D0C6A7C30C994FD5D816CCB"

Step "1/7 Python"
# `python` can be the Microsoft Store stub, which exits 9009 without running
# anything, so a name on PATH proves nothing: ask it for its version.
function Find-Python {
  foreach ($c in @("python", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
      $src = $cmd.Source
      $exe = Native { & $src -c "import sys; print(sys.executable if sys.version_info >= (3, 9) else '')" 2>$null }
      if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { return $exe.Trim() }
    } catch {}
  }
  return $null
}
$Py = Find-Python
if (-not $Py) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "Python 3.9+ is needed and winget is not available. Install Python from https://www.python.org/downloads/ (tick 'Add to PATH') and run this again."
  }
  Native { winget install --silent --accept-package-agreements --accept-source-agreements -e --id Python.Python.3.12 }
  # winget edits PATH in the registry; this session still has the old one.
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path", "User")
  $Py = Find-Python
  if (-not $Py) { throw "Python installed but not found; open a new PowerShell and run this again." }
}
Note "python: $Py"

Step "2/7 voicebridge code"
if ($PSScriptRoot -and (Test-Path "$PSScriptRoot\bin\vb")) {
  $Repo = $PSScriptRoot
  Note "using this clone: $Repo"
} else {
  # Run through `irm | iex`: there is no script folder, so fetch the code.
  $Repo    = if ($env:VB_HOME) { $env:VB_HOME } else { "$HOME\voicebridge" }
  $RepoUrl = if ($env:VB_REPO_URL) { $env:VB_REPO_URL } else { "https://github.com/cc-vb/voicebridge" }
  $Ref     = if ($env:VB_REF) { $env:VB_REF } else { "main" }
  if (Test-Path "$Repo\bin\vb") {
    Note "already at $Repo"
    if (Test-Path "$Repo\.git") {
      Native { git -C $Repo pull --ff-only 2>&1 | Out-Null }
      if ($LASTEXITCODE -ne 0) { Warn "couldn't fast-forward $Repo; keeping what is there" }
    }
  } elseif (Get-Command git -ErrorAction SilentlyContinue) {
    # A clone, so the session-start hook can keep it up to date.
    Native { git clone --quiet --depth 1 --branch $Ref "$RepoUrl.git" $Repo 2>&1 | Out-Host }
    Must "git clone"
  } else {
    # No git: a snapshot. Works the same; it just won't self-update.
    $zip = "$env:TEMP\voicebridge-src.zip"
    $tmp = "$env:TEMP\voicebridge-src"
    Fetch "$RepoUrl/archive/$Ref.zip" $zip
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Force $zip $tmp
    $inner = Get-ChildItem $tmp | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $Repo | Out-Null
    Copy-Item -Recurse -Force "$($inner.FullName)\*" $Repo
    Remove-Item -Recurse -Force $tmp, $zip
  }
  Note "code: $Repo"
}

Step "3/7 Recorder (sounddevice)"
# Windows has no sox `rec`, and there is no dependable winget package for it.
# vb/winrec.py records through sounddevice instead, whose Windows wheel ships
# PortAudio inside it, so this one pip install is the whole dependency.
Native { & $Py -m pip install --user --quiet --disable-pip-version-check sounddevice 2>&1 | Out-Host }
Must "pip install sounddevice"
Native { & $Py -c "import sounddevice" } ; Must "import sounddevice"
Note "sounddevice ok"

Step "4/7 whisper.cpp (speech to text, pinned b5454)"
$WDir = "$State\whisper"
if ((Test-Path "$WDir\whisper-cli.exe") -and
    ((Get-Content "$WDir\.sha256" -ErrorAction SilentlyContinue) -eq $WhisperSha)) {
  Note "whisper-cli present"
} else {
  $zip = "$env:TEMP\whisper-bin-x64.zip"
  Fetch $WhisperUrl $zip
  $got = (Get-FileHash -Algorithm SHA256 $zip).Hash
  if ($got -ne $WhisperSha) {
    Remove-Item -Force $zip
    throw "whisper.cpp download has SHA256 $got, expected $WhisperSha. Not installing it."
  }
  $tmp = "$env:TEMP\whisper-bin-x64"
  if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
  Expand-Archive -Force $zip $tmp
  # The exe needs its DLLs beside it, so the whole folder moves, and it goes
  # under the state dir rather than onto PATH: vb looks there directly, and
  # a PATH edit would only reach terminals opened after this one.
  $src = if (Test-Path "$tmp\Release") { "$tmp\Release" } else { $tmp }
  New-Item -ItemType Directory -Force -Path $WDir | Out-Null
  Copy-Item -Force "$src\*" $WDir
  Set-Content -Encoding ascii "$WDir\.sha256" $WhisperSha
  Remove-Item -Recurse -Force $tmp, $zip
  Note "whisper-cli: $WDir\whisper-cli.exe"
}

Step "5/7 Whisper model (English, ~466MB, one time)"
$Model = "$Models\ggml-small.en.bin"
if (-not (Test-Path $Model)) {
  Fetch "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin" $Model
} else { Note "model present" }

Step "6/7 vb command"
# A vb.cmd next to nothing else, in a folder that goes on the user PATH. The
# python path is written in full so the Store stub can never be the one run.
$Shim = "$BinDir\vb.cmd"
"@echo off`r`n`"$Py`" `"$Repo\bin\vb`" %*" | Set-Content -Encoding ascii $Shim
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $UserPath) { $UserPath = "" }
if (($UserPath -split ";") -notcontains $BinDir) {
  [Environment]::SetEnvironmentVariable("Path", ($UserPath.TrimEnd(";") + ";" + $BinDir).TrimStart(";"), "User")
  Note "added $BinDir to your PATH (new terminals pick it up)"
}
if (($env:Path -split ";") -notcontains $BinDir) { $env:Path += ";$BinDir" }
Native { & $Shim --help | Out-Null } ; Must "vb --help"
Note "wrote $Shim"
# ffmpeg is optional: it only changes the speaking speed of Kokoro audio.
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Native { winget install --silent --accept-package-agreements --accept-source-agreements -e --id Gyan.FFmpeg 2>&1 | Out-Null }
  }
  if ($LASTEXITCODE -ne 0 -or -not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Warn "ffmpeg not installed (optional: speed changes on the Kokoro voice)"
  }
}

Step "7/7 Voice, hooks and slash commands"
if ($env:VB_KOKORO -ne "0") {
  $Venv = "$State\kokoro-venv"
  if (-not (Test-Path "$Venv\Scripts\python.exe")) {
    Native { & $Py -m venv $Venv } ; Must "python -m venv"
  }
  Native { & "$Venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check kokoro-onnx soundfile qrcode 2>&1 | Out-Host }
  Must "pip install kokoro-onnx"
  $KM = "$Models\kokoro-v1.0.onnx"
  $KV = "$Models\voices-v1.0.bin"
  $KBase = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
  if (-not (Test-Path $KM)) { Fetch "$KBase/kokoro-v1.0.onnx" $KM }
  if (-not (Test-Path $KV)) { Fetch "$KBase/voices-v1.0.bin" $KV }
  Native { & $Shim engine kokoro | Out-Null ; & $Shim voice af_heart | Out-Null }
  Note "Kokoro voice ready"
} else {
  Note "Kokoro skipped (VB_KOKORO=0): replies use the Windows voice"
}
$SettingsPath = "$Claude\settings.json"
Native { & $Py "$Repo\bin\vb" __install_hooks__ $Repo $SettingsPath }
Must "registering hooks"
Copy-Item "$Repo\commands\*.md" "$Claude\commands\" -Force

Write-Host @"

voicebridge is installed.

Two things are yours to do:
  1. Microphone: Settings > Privacy & security > Microphone, turn on
     "Let desktop apps access your microphone".
  2. Open a NEW terminal (so 'vb' is on PATH) and run:  vb doctor

Then, inside any Claude Code session:  /voice-on   and start talking.
Silence: /voice-stop   |   Hotkeys: see WINDOWS.md (AutoHotkey)
"@ -ForegroundColor Green
}

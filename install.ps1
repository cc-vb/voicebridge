# voicebridge Windows installer (PowerShell).
# WINDOWS: UNVERIFIED end to end - built on macOS, needs a Windows tester.
# Run in PowerShell:  irm https://raw.githubusercontent.com/cc-vb/voicebridge/main/install.ps1 | iex
# or, from a clone:    powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"

function Step($m) { Write-Host "`n== $m ==" -ForegroundColor Cyan }

$Repo   = if ($PSScriptRoot) { $PSScriptRoot } else { "$HOME\voicebridge" }
$State  = "$HOME\.voicebridge"
$Models = "$State\models"
$Claude = "$HOME\.claude"
New-Item -ItemType Directory -Force -Path $Models, "$Claude\commands" | Out-Null

Step "1/6 Dependencies (winget: whisper.cpp deps via ffmpeg, python)"
# whisper.cpp: prefer a prebuilt whisper-cli.exe on PATH; else guide the user.
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  winget install --silent --accept-package-agreements --accept-source-agreements Gyan.FFmpeg
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  winget install --silent --accept-package-agreements --accept-source-agreements Python.Python.3.12
}
if (-not (Get-Command whisper-cli -ErrorAction SilentlyContinue)) {
  Write-Host "  NOTE: install whisper.cpp (whisper-cli.exe) and put it on PATH:" -ForegroundColor Yellow
  Write-Host "        winget install whisper-cli   (or build from ggerganov/whisper.cpp)"
}

Step "2/6 Whisper model (English, ~466MB, one time)"
$Model = "$Models\ggml-small.en.bin"
if (-not (Test-Path $Model)) {
  Invoke-WebRequest -Uri "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin" -OutFile $Model
} else { Write-Host "  model present" }

Step "3/6 vb command on PATH"
# A vb.cmd shim that calls python on bin/vb, placed in a WindowsApps-style dir.
$BinDir = "$env:LOCALAPPDATA\Microsoft\WindowsApps"
if (-not (Test-Path $BinDir)) { $BinDir = "$HOME\bin"; New-Item -ItemType Directory -Force -Path $BinDir | Out-Null }
$Shim = "$BinDir\vb.cmd"
"@echo off`r`npython `"$Repo\bin\vb`" %*" | Set-Content -Encoding ascii $Shim
Write-Host "  wrote $Shim"

Step "4/6 Kokoro neural voice (default; ~800MB)"
if ($env:VB_KOKORO -ne "0") {
  $Venv = "$State\kokoro-venv"
  if (-not (Test-Path "$Venv\Scripts\python.exe")) {
    python -m venv $Venv
    & "$Venv\Scripts\pip.exe" -q install --upgrade pip
    & "$Venv\Scripts\pip.exe" -q install kokoro-onnx soundfile qrcode
  }
  $KM = "$Models\kokoro-v1.0.onnx"
  if (-not (Test-Path $KM)) {
    Invoke-WebRequest "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx" -OutFile $KM
    Invoke-WebRequest "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" -OutFile "$Models\voices-v1.0.bin"
  }
  & $Shim engine kokoro
  & $Shim voice af_heart
}

Step "5/6 Hooks in ~/.claude/settings.json"
$SettingsPath = "$Claude\settings.json"
python "$Repo\bin\vb" __install_hooks__ $Repo $SettingsPath 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "  (register hooks manually per README if this step warns)"
}
Copy-Item "$Repo\commands\*.md" "$Claude\commands\" -Force

Step "6/6 Done"
Write-Host @"

Two things left, yours to do:
  1. Microphone permission: Windows Settings -> Privacy -> Microphone -> allow
     your terminal app.
  2. Interrupt hotkey: install AutoHotkey and add a hotkey running 'vb stop'
     (see WINDOWS.md), or just start typing to interrupt.

Then, inside any Claude Code session:  /voice-on   and start talking.
Silence: run /voice-stop  |  Phone: /phone
"@ -ForegroundColor Green

# voicebridge Windows smoke test, for a person at a real PC.
#
# CI proves everything that does not need hardware. This checks the rest:
# your microphone, your speakers, and pasting into a real window. It takes
# about a minute and tells you, for each step, PASS or FAIL and what to do.
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows-smoke.ps1
#
# Run it after install.ps1, in a NEW terminal. Paste the whole output into
# an issue if anything fails.

$ErrorActionPreference = "Continue"
$State = "$HOME\.voicebridge"
$script:fails = 0
$Tmp = Join-Path $env:TEMP "vb-smoke"
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

function Result($name, $ok, $fix, $detail = "") {
  if ($ok) {
    Write-Host ("  PASS  " + $name + $(if ($detail) { "  ($detail)" } else { "" })) -ForegroundColor Green
  } else {
    $script:fails++
    Write-Host ("  FAIL  " + $name + $(if ($detail) { "  ($detail)" } else { "" })) -ForegroundColor Red
    Write-Host "        fix: $fix" -ForegroundColor Yellow
  }
}

# The interpreter the vb shim uses, so we test what vb will actually run.
$Py = $null
$Shim = "$State\bin\vb.cmd"
if (Test-Path $Shim) {
  $line = (Get-Content $Shim | Where-Object { $_ -match '^"' } | Select-Object -First 1)
  if ($line -match '^"([^"]+)"') { $Py = $Matches[1] }
}
$Repo = $null
if ($line -match '"([^"]+)\\bin\\vb"') { $Repo = $Matches[1] }

Write-Host "`nvoicebridge Windows smoke test`n"

# 1. Python
Result "python" ($Py -and (Test-Path $Py)) "run install.ps1 again; it writes the python path into $Shim" $Py

# 2. vb on PATH
$vb = Get-Command vb -ErrorAction SilentlyContinue
Result "vb on PATH" ($null -ne $vb) "open a NEW terminal (PATH changes only reach new ones), or run install.ps1 again" $(if ($vb) { $vb.Source } else { "" })

# 3. whisper-cli
$Wcli = "$State\whisper\whisper-cli.exe"
$wok = $false
if (Test-Path $Wcli) {
  & $Wcli --help *> $null
  $wok = ($LASTEXITCODE -eq 0)
}
Result "whisper-cli runs" $wok "run install.ps1 again. If it exists but will not start, install the Microsoft Visual C++ Redistributable (x64) from https://aka.ms/vs/17/release/vc_redist.x64.exe" $Wcli

# 4. model
$Model = "$State\models\ggml-small.en.bin"
$mok = (Test-Path $Model) -and ((Get-Item $Model).Length -gt 400MB)
Result "whisper model" $mok "delete $Model and run install.ps1 again (it was missing or cut short)"

# 5. microphone device
$dev = ""
if ($Py) {
  $dev = & $Py -c "import sounddevice as sd; d = sd.query_devices(kind='input'); print(d['name'])" 2>$null
}
Result "microphone device" ([bool]$dev) "plug in or enable a microphone (Settings > System > Sound > Input), then: $Py -m pip install --user sounddevice" "$dev"

# 6. record 3 seconds
$Wav = "$Tmp\smoke.wav"
Remove-Item -Force $Wav -ErrorAction SilentlyContinue
$level = -1.0
if ($Py -and $Repo -and $dev) {
  Write-Host "`n  >>> Say 'the quick brown fox jumps over the lazy dog' now (3 seconds) <<<`n" -ForegroundColor Cyan
  & $Py "$Repo\vb\winrec.py" $Wav --max 3
  $level = [double](& $Py -c "import sys; sys.path.insert(0, r'$Repo'); from vb import stt; print(stt.loudness(r'$Wav'))" 2>$null)
}
Result "record 3s" ((Test-Path $Wav) -and (Get-Item $Wav -ErrorAction SilentlyContinue).Length -gt 80000) "Settings > Privacy & security > Microphone: turn on 'Let desktop apps access your microphone'"
Result "mic hears sound" ($level -ge 0.004) "the take is silent: check the mic is not muted and is the default Input device (Settings > System > Sound)" ("level {0:N3}" -f $level)

# 7. transcribe what you said
$heard = ""
if ($wok -and $mok -and (Test-Path $Wav)) {
  $heard = (& $Wcli -m $Model -f $Wav -nt -np 2>$null) -join " "
}
Result "transcribe" ($heard.Trim().Length -gt 0) "nothing came back; if the take was silent fix the mic first" "heard: $($heard.Trim())"

# 8. speak
$spoke = $false
if ($Py -and $Repo) {
  Write-Host "`n  >>> You should hear: 'voicebridge can speak' <<<`n" -ForegroundColor Cyan
  & $Py "$Repo\bin\vb" say "voicebridge can speak" | Out-Null
  Start-Sleep -Seconds 4
  $ans = Read-Host "  Did you hear it? (y/n)"
  $spoke = ($ans -match '^[yY]')
}
Result "speak" $spoke "check the speaker volume and default output device; 'vb engine say' switches to the Windows voice if Kokoro is the problem; 'vb log' shows why"

# 9. paste into Notepad
$pasted = $false
if ($Py -and $Repo) {
  $np = Start-Process notepad -PassThru
  Start-Sleep -Seconds 2
  $txt = "voicebridge paste test"
  & $Py -c "import sys,time; sys.path.insert(0, r'$Repo'); from vb import oslayer; time.sleep(0.5); print(oslayer.paste_text(sys.argv[1], False))" $txt | Out-Null
  Start-Sleep -Seconds 1
  $ans = Read-Host "  Does Notepad show '$txt'? (y/n)"
  $pasted = ($ans -match '^[yY]')
  Stop-Process -Id $np.Id -Force -ErrorAction SilentlyContinue
}
Result "paste into a window" $pasted "keep the target window in front while it pastes; if it typed nothing, an antivirus may block SendKeys"

Write-Host ""
if ($script:fails -eq 0) {
  Write-Host "ALL PASS. Open Claude Code and type /voice-on" -ForegroundColor Green
} else {
  Write-Host "$($script:fails) step(s) failed. Fix the first one and run this again." -ForegroundColor Red
}

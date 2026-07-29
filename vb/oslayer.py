"""voicebridge OS layer: the only place with per-platform code.

Everything OS-specific (speak, play audio, type text, press keys, record)
routes through here so the rest of voicebridge is platform-agnostic. macOS
is the tested path; Windows implementations are provided and structured but
need a Windows machine to verify (marked WINDOWS: UNVERIFIED).

Detection: IS_MAC / IS_WIN / IS_LINUX.
"""

import os
import shutil
import subprocess
import sys

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

_BREW_BINS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def which(name: str) -> str:
    p = shutil.which(name)
    if p:
        return p
    for d in _BREW_BINS:
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return ""


# ---------- play a wav file --------------------------------------------------
def play_cmd(wav: str) -> list:
    if IS_MAC:
        return ["afplay", wav]
    if IS_WIN:
        # WINDOWS: UNVERIFIED. SoundPlayer plays and blocks until done.
        ps = (f"(New-Object System.Media.SoundPlayer "
              f"'{wav}').PlaySync() # VBSPEAK")   # tag: stop_all_speech targets it
        return ["powershell", "-NoProfile", "-Command", ps]
    # Linux
    return [which("aplay") or "aplay", wav]


# ---------- system TTS (fallback when Kokoro engine is off/unavailable) ------
def _sapi_rate(rate: str) -> int:
    """Words-per-minute -> SAPI's -10..10 scale, coerced in Python so a
    non-numeric rate can't become a PowerShell parse error (silent TTS fail)."""
    try:
        return max(-10, min(10, int((float(rate) / 175.0 - 1) * 5)))
    except (TypeError, ValueError):
        return 0


def tts_to_wav(text: str, out_wav: str, rate: str, voice: str) -> bool:
    """Synthesize `text` to a wav with the OS voice. Returns success."""
    if IS_MAC:
        aiff = out_wav + ".aiff"
        cmd = ["say", "-r", rate, "-o", aiff]
        if voice:
            cmd += ["-v", voice]
        cmd.append(text)
        if subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode != 0:
            return False
        ff = which("ffmpeg")
        if ff:
            subprocess.run([ff, "-y", "-i", aiff, out_wav],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return os.path.exists(out_wav)
        return False
    if IS_WIN:
        # WINDOWS: UNVERIFIED. Built-in SAPI writes a wav, no install needed.
        safe = text.replace("'", "''")
        wav = out_wav.replace("\\", "\\\\")
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate={_sapi_rate(rate)}; "
            f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('{safe}'); $s.Dispose()"
        )
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0 and os.path.exists(out_wav)
    # Linux: espeak-ng
    es = which("espeak-ng") or which("espeak")
    if es:
        return subprocess.run([es, "-w", out_wav, text],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    return False


def tts_speak_blocking(text: str, rate: str, voice: str) -> None:
    """Speak directly (no wav), blocking. Used by the say-fallback path."""
    if IS_MAC:
        cmd = ["say", "-r", rate]
        if voice:
            cmd += ["-v", voice]
        cmd.append(text)
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif IS_WIN:
        safe = text.replace("'", "''")
        ps = ("Add-Type -AssemblyName System.Speech; "
              "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.Rate={_sapi_rate(rate)}; $s.Speak('{safe}') # VBSPEAK")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        es = which("espeak-ng") or which("espeak")
        if es:
            subprocess.run([es, text], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)


def stop_all_speech() -> None:
    """Kill the playing audio/TTS process, only ours, only this user's."""
    uid = str(os.getuid()) if hasattr(os, "getuid") else ""

    def _pk(name):
        subprocess.run(["pkill", "-x"] + (["-U", uid] if uid else []) + [name],
                       capture_output=True)
    if IS_MAC:
        _pk("say")
        _pk("afplay")
    elif IS_WIN:
        # WINDOWS: kill ONLY our own tagged speech powershell (VBSPEAK), never
        # every powershell on the machine. The old `taskkill /F /IM
        # powershell.exe` force-killed unrelated user work (build scripts,
        # terminals, even the launcher). Exclude this stopper's own pid.
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" "
              "| Where-Object { $_.CommandLine -like '*VBSPEAK*' "
              "-and $_.ProcessId -ne $PID } "
              "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        _pk("aplay")
        _pk("espeak-ng")   # the espeak fallback speaks directly, also stop it
        _pk("espeak")


# ---------- type text into / send keys to the focused app --------------------
def paste_text(text: str, send: bool) -> bool:
    """Put text in the focused app (clipboard paste), optionally press Enter.
    Returns whether delivery actually happened, so a caller never marks a
    prompt 'delivered' when the OS had no tool to type it."""
    if IS_MAC:
        _mac_paste(text, send)
        return True
    if IS_WIN:
        return _win_paste(text, send)
    return _linux_paste(text, send)


def press_escape() -> None:
    if IS_MAC:
        _osa('tell application "System Events" to key code 53')
    elif IS_WIN:
        _win_sendkeys("{ESC}")
    else:
        _xdotool("key", "Escape")


# ---- mac impls ----
def _osa(script: str) -> None:
    subprocess.run(["osascript", "-e", script],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _mac_paste(text: str, send: bool) -> None:
    subprocess.run(["pbcopy"], input=text, text=True)
    _osa('tell application "System Events" to keystroke "v" using command down')
    if send:
        _osa('tell application "System Events" to key code 36')


# ---- windows impls (WINDOWS: UNVERIFIED) ----
def _win_paste(text: str, send: bool) -> bool:
    try:
        subprocess.run("clip", input=text, text=True, shell=True)
    except Exception:
        return False
    _win_sendkeys("^v" + ("{ENTER}" if send else ""))
    return True


def _win_sendkeys(keys: str) -> None:
    ps = ("Add-Type -AssemblyName System.Windows.Forms; "
          f"[System.Windows.Forms.SendKeys]::SendWait('{keys}')")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------- convert a phone photo into something a session can view ---------
def heic_to_jpeg(src: str, out: str) -> bool:
    """Transcode a HEIC/HEIF photo to JPEG. Returns whether `out` was written.

    An iPhone hands you HEIC, which Claude cannot view, so an unconverted
    attachment reaches the session as a file it can only describe by name.
    macOS has sips built in; elsewhere we try the image tools a machine
    running voicebridge plausibly already has (ffmpeg is installed by both
    installers) and give up honestly rather than pretending.

    WINDOWS/LINUX: UNVERIFIED. ffmpeg only decodes HEIC when built with
    libheif, so the ImageMagick and heif-convert attempts are the ones
    likely to do the work; if none succeed the caller keeps the original."""
    if IS_MAC:
        return _run_ok(["sips", "-s", "format", "jpeg", src, "--out", out],
                       out)
    for cmd in (["magick", src, out],           # ImageMagick 7
                ["convert", src, out],          # ImageMagick 6
                ["heif-convert", src, out],     # libheif tools
                ["ffmpeg", "-y", "-i", src, out]):
        exe = which(cmd[0])
        if not exe:
            continue
        if _run_ok([exe, *cmd[1:]], out):
            return True
    return False


def _run_ok(cmd: list, out: str) -> bool:
    """Run a converter and confirm it actually produced a non-empty file: a
    zero exit code with no output would otherwise read as success."""
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
    except Exception:
        return False
    return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0


def secure_file(path: str, dirs: bool = False) -> None:
    """Make a path owner-only where the OS has POSIX modes.

    On Windows chmod moves only the read-only bit, so this is a no-op there
    and the uploads directory inherits its parent's ACL instead. Worth
    stating plainly: the 0600 guarantee is a POSIX one."""
    if IS_WIN:
        return
    try:
        os.chmod(path, 0o700 if dirs else 0o600)
    except OSError:
        pass


# ---- linux impls ----
def _xdotool(*args) -> None:
    xd = which("xdotool")
    if xd:
        subprocess.run([xd, *args], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


def _linux_paste(text: str, send: bool) -> bool:
    xc = which("xclip")
    xd = which("xdotool")
    # Need both: without xclip we'd Ctrl+V whatever stale thing is already on
    # the clipboard; without xdotool we can't send the keystroke at all. Either
    # way, refuse rather than pretend we delivered.
    if not xc or not xd:
        return False
    subprocess.run([xc, "-selection", "clipboard"], input=text, text=True)
    _xdotool("key", "ctrl+v")
    if send:
        _xdotool("key", "Return")
    return True

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
              f"'{wav}').PlaySync()")
        return ["powershell", "-NoProfile", "-Command", ps]
    # Linux
    return [which("aplay") or "aplay", wav]


# ---------- system TTS (fallback when Kokoro engine is off/unavailable) ------
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
            f"$s.Rate=[int](({rate}/175.0-1)*5); "
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
              f"$s.Rate=[int](({rate}/175.0-1)*5); $s.Speak('{safe}')")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        es = which("espeak-ng") or which("espeak")
        if es:
            subprocess.run([es, text], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)


def stop_all_speech() -> None:
    """Kill any playing audio/TTS process."""
    if IS_MAC:
        subprocess.run(["pkill", "-x", "say"], capture_output=True)
        subprocess.run(["pkill", "-x", "afplay"], capture_output=True)
    elif IS_WIN:
        subprocess.run(["taskkill", "/F", "/IM", "powershell.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["pkill", "-x", "aplay"], capture_output=True)


# ---------- type text into / send keys to the focused app --------------------
def paste_text(text: str, send: bool) -> None:
    """Put text in the focused app (clipboard paste), optionally press Enter."""
    if IS_MAC:
        _mac_paste(text, send)
    elif IS_WIN:
        _win_paste(text, send)
    else:
        _linux_paste(text, send)


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
def _win_paste(text: str, send: bool) -> None:
    subprocess.run("clip", input=text, text=True, shell=True)
    _win_sendkeys("^v" + ("{ENTER}" if send else ""))


def _win_sendkeys(keys: str) -> None:
    ps = ("Add-Type -AssemblyName System.Windows.Forms; "
          f"[System.Windows.Forms.SendKeys]::SendWait('{keys}')")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---- linux impls ----
def _xdotool(*args) -> None:
    xd = which("xdotool")
    if xd:
        subprocess.run([xd, *args], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


def _linux_paste(text: str, send: bool) -> None:
    xc = which("xclip")
    if xc:
        subprocess.run([xc, "-selection", "clipboard"], input=text, text=True)
    _xdotool("key", "ctrl+v")
    if send:
        _xdotool("key", "Return")

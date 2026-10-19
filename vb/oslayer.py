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
        # SoundPlayer plays and blocks until done. Verified on windows-latest
        # (the CI plays a SAPI wav through it).
        ps = (f"(New-Object System.Media.SoundPlayer "
              f"{_ps_str(wav)}).PlaySync() # VBSPEAK")   # tag: stop_all_speech targets it
        return ["powershell", "-NoProfile", "-Command", ps]
    # Linux
    return [which("aplay") or "aplay", wav]


# ---------- getting text into PowerShell intact ------------------------------
# The reply used to be pasted into the command as a '...' literal with only
# ASCII quotes doubled. PowerShell also ends a single-quoted string at the
# typographic quotes (U+2018/2019/201A/201B), which Claude writes all the
# time ("it\u2019s"), so ordinary replies either raised a parse error (no
# speech at all) or, worse, ran the rest of the sentence as code. The text
# now goes through a UTF-8 file and is never part of the command.
_PS_QUOTES = "'\u2018\u2019\u201a\u201b"


def _ps_str(s: str) -> str:
    """A PowerShell single-quoted literal for a short string we control (a
    path), with every character PowerShell treats as a quote doubled."""
    return "'" + "".join(c * 2 if c in _PS_QUOTES else c for c in s) + "'"


def _text_file(text: str) -> str:
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="vbtts-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _ps_read(path: str) -> str:
    return f"[IO.File]::ReadAllText({_ps_str(path)}, [Text.Encoding]::UTF8)"


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


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
        # Built-in SAPI writes a wav, no install needed. Verified on
        # windows-latest by tests/test_windows.py.
        txt = _text_file(text)
        try:
            ps = (
                "Add-Type -AssemblyName System.Speech; "
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.Rate={_sapi_rate(rate)}; "
                f"$s.SetOutputToWaveFile({_ps_str(out_wav)}); "
                f"$s.Speak({_ps_read(txt)}); $s.Dispose()"
            )
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        finally:
            _unlink(txt)
        return r.returncode == 0 and os.path.exists(out_wav) \
            and os.path.getsize(out_wav) > 44
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
        txt = _text_file(text)
        try:
            ps = ("Add-Type -AssemblyName System.Speech; "
                  "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.Rate={_sapi_rate(rate)}; $s.Speak({_ps_read(txt)}) "
                  "# VBSPEAK")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            _unlink(txt)
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
    # Not `clip`: it reads stdin in the OEM codepage, so Hindi or any accent
    # arrives as question marks, and it appends a newline the paste then
    # carries into the prompt. clipboard_set goes through Set-Clipboard.
    if not clipboard_set(text):
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


# ---------- processes ---------------------------------------------------------
# Everything below exists because three POSIX idioms this codebase leaned on
# do something quite different on Windows, and two of them are dangerous:
#
#   os.kill(pid, 0)    POSIX: "is it alive?". Windows: Python calls
#                      TerminateProcess for ANY signal that is not a console
#                      control event, so this KILLS the process, with exit code
#                      0. Nine liveness checks used it, against pids read back
#                      from files, and Windows recycles pids quickly: a stale
#                      speech.pid could have terminated the user's browser.
#   os.getuid()        Does not exist on Windows. Five modules called it at
#                      IMPORT time to derive a per-user port, so voicebridge
#                      could not even be imported.
#   signal.SIGKILL     Does not exist on Windows either; AttributeError the
#                      moment the force-kill path ran.
#
# ps, pgrep, pkill and lsof are simply absent. The Windows side uses the Win32
# API through ctypes rather than shelling out to PowerShell for every probe:
# PowerShell takes 200 to 500ms to start, and some of these run every few
# seconds while voice mode is on.

def uid() -> int:
    """A per-user number, for deriving per-user ports and the like.

    POSIX: the real uid. Windows has no integer uid, so this is a stable hash
    of the user name: two accounts on one PC still get different ports, which
    is the only property the callers rely on."""
    if hasattr(os, "getuid"):
        return os.getuid()
    import zlib
    name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return zlib.crc32(name.lower().encode()) & 0xFFFF


if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED = 0x1000
    _PROCESS_SUSPEND_RESUME = 0x0800
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _TH32CS_SNAPPROCESS = 0x00000002

    class _PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(wintypes.DWORD)]
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + \
        [ctypes.POINTER(wintypes.FILETIME)] * 4
    # 64-bit sizes and addresses: without argtypes ctypes passes a Python
    # int as a 32-bit c_int, which truncates any address above 4GB.
    _k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]


def alive(pid: int) -> bool:
    """Is `pid` a running process. Never signals it, on any platform."""
    if not pid or pid <= 0:
        return False
    if not IS_WIN:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True          # exists, belongs to someone else
        except OSError:
            return False
        return True
    h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        # Access denied means it exists and is not ours to open.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


def processes() -> list:
    """One snapshot of every process: [(pid, ppid, name)], name being the bare
    executable name as ps `comm` gives it ('claude', 'claude.exe')."""
    out = []
    if IS_WIN:
        snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return out
        try:
            e = _PE32()
            e.dwSize = ctypes.sizeof(_PE32)
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out.append((int(e.th32ProcessID), int(e.th32ParentProcessID),
                            e.szExeFile))
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            _k32.CloseHandle(snap)
        return out
    try:
        txt = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return out
    for line in txt.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            try:
                out.append((int(parts[0]), int(parts[1]),
                            os.path.basename(parts[2].strip())))
            except ValueError:
                pass
    return out


def proc_name(pid: int) -> str:
    for p, _, name in processes():
        if p == pid:
            return name
    return ""


def proc_parent(pid: int) -> int:
    for p, pp, _ in processes():
        if p == pid:
            return pp
    return 0


def is_claude(name: str) -> bool:
    """Is this executable name Claude Code. On Windows the native install is
    `claude.exe`; comparing against bare 'claude' made every session look
    dead there."""
    n = os.path.basename(name or "").lower()
    return n in ("claude", "claude.exe")


def proc_start(pid: int) -> str:
    """A stable per-process start marker, to tell a recycled pid from the
    process that first held it. '' when unknown."""
    if not pid or pid <= 0:
        return ""
    if IS_WIN:
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return ""
        try:
            t = [wintypes.FILETIME() for _ in range(4)]
            if not _k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
                return ""
            return str((t[0].dwHighDateTime << 32) | t[0].dwLowDateTime)
        finally:
            _k32.CloseHandle(h)
    try:
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                              capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except Exception:
        return ""


def proc_stopped(pid: int) -> bool:
    """Is the process suspended (paused speech). POSIX reads ps `stat`.
    Windows has no cheap equivalent, so callers there track the pause state
    themselves; this answers False."""
    if IS_WIN or not pid:
        return False
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True,
                            timeout=3).stdout.strip()
    except Exception:
        return False
    return st.startswith("T")


def proc_tty(pid: int) -> str:
    """The /dev tty a process runs on, '' if none. Windows has no ttys, and
    the phone path there pastes into the focused window instead."""
    if IS_WIN or not pid:
        return ""
    try:
        t = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                           capture_output=True, text=True,
                           timeout=3).stdout.strip()
    except Exception:
        return ""
    return ("/dev/" + t) if t and t not in ("??", "?") else ""


def cmdlines() -> list:
    """[(pid, full command line)] for every process we can see.

    The slow one on Windows (a PowerShell CIM query, 0.3 to 1s), so it is
    only used where matching on arguments is the point: finding OUR
    recorder by its wav path, our whisper server by its port."""
    if IS_WIN:
        ps = ("Get-CimInstance Win32_Process | ForEach-Object "
              "{ \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        try:
            txt = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True,
                                 timeout=15).stdout
        except Exception:
            return []
        out = []
        for line in txt.splitlines():
            pid_s, _, cmd = line.partition("\t")
            try:
                out.append((int(pid_s), cmd))
            except ValueError:
                pass
        return out
    try:
        txt = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return []
    out = []
    for line in txt.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            try:
                out.append((int(parts[0]), parts[1]))
            except ValueError:
                pass
    return out


def proc_cmdline(pid: int) -> str:
    if not IS_WIN:
        try:
            return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                  capture_output=True, text=True,
                                  timeout=3).stdout.strip()
        except Exception:
            return ""
    for p, cmd in cmdlines():
        if p == pid:
            return cmd
    return ""


def terminate(pid: int, force: bool = False) -> bool:
    """Ask a process to stop (force: make it). Returns whether a signal was
    delivered. Windows has no SIGTERM: both cases are TerminateProcess, which
    is what Popen.terminate() does there too."""
    if not pid or pid <= 0:
        return False
    if IS_WIN:
        h = _k32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not h:
            return False      # gone, or another user's: not ours to stop
        try:
            return bool(_k32.TerminateProcess(h, 1))
        finally:
            _k32.CloseHandle(h)
    import signal as _sig
    try:
        os.kill(pid, _sig.SIGKILL if force else _sig.SIGTERM)
        return True
    except OSError:
        return False


def terminate_tree(pid: int) -> None:
    """Stop a process and everything it started. POSIX callers start these
    with their own session, so the group id is the pid; Windows has no
    groups and taskkill /T walks the tree instead."""
    if not pid or pid <= 0:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        os.killpg(pid, 15)
    except OSError:
        terminate(pid)


def set_paused(pid: int, paused: bool) -> bool:
    """Pause or resume a process (speech playback). POSIX: SIGSTOP/SIGCONT to
    its group. Windows: NtSuspendProcess / NtResumeProcess, undocumented but
    stable since XP and what every process manager uses."""
    if not pid or pid <= 0:
        return False
    if IS_WIN:
        # The whole tree, which is what killpg reaches on POSIX. The speech
        # pid is a Python worker and the sound comes out of the PowerShell
        # player it started, so suspending only the worker would leave the
        # reply talking while hold() says "held".
        nt = ctypes.WinDLL("ntdll")
        fn = nt.NtSuspendProcess if paused else nt.NtResumeProcess
        fn.argtypes = [wintypes.HANDLE]
        ok = False
        for p in [pid] + descendants(pid):
            h = _k32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, p)
            if not h:
                continue
            try:
                ok = (fn(h) == 0) or ok
            finally:
                _k32.CloseHandle(h)
        return ok
    import signal as _sig
    try:
        os.killpg(pid, _sig.SIGSTOP if paused else _sig.SIGCONT)
        return True
    except OSError:
        return False


def kill_matching(pattern: str, exact_name: bool = False) -> int:
    """Stop processes whose name is `pattern` (exact_name) or whose command
    line contains it. Ours only, never the caller. Returns how many.

    POSIX keeps the exact pkill call this code always made (`-U uid` is the
    "ours only" boundary). On Windows OpenProcess refuses another user's
    process without admin, which is the same boundary drawn by the OS."""
    if not IS_WIN:
        args = ["pkill"] + (["-U", str(uid())] if hasattr(os, "getuid") else [])
        args += (["-x", pattern] if exact_name else ["-f", pattern])
        try:
            r = subprocess.run(args, capture_output=True, timeout=5)
            return 1 if r.returncode == 0 else 0
        except Exception:
            return 0
    me = os.getpid()
    if exact_name:
        want = (pattern.lower(), pattern.lower() + ".exe")
        targets = [p for p, _, name in processes() if name.lower() in want]
    else:
        targets = [p for p, cmd in cmdlines() if pattern in cmd]
    return sum(1 for p in targets if p != me and terminate(p))


def any_matching(pattern: str, exact_name: bool = False) -> bool:
    if not IS_WIN:
        args = ["pgrep"] + (["-U", str(uid())] if hasattr(os, "getuid") else [])
        args += (["-x", pattern] if exact_name else ["-f", pattern])
        try:
            return subprocess.run(args, capture_output=True,
                                  timeout=5).returncode == 0
        except Exception:
            return False
    if exact_name:
        want = (pattern.lower(), pattern.lower() + ".exe")
        return any(name.lower() in want for _, _, name in processes())
    me = os.getpid()
    return any(pattern in cmd for p, cmd in cmdlines() if p != me)


# ---------- clipboard and the focused app ------------------------------------
def clipboard_get() -> str:
    if IS_MAC:
        try:
            return subprocess.run(["pbpaste"], capture_output=True, text=True,
                                  timeout=3).stdout
        except Exception:
            return ""
    if IS_WIN:
        try:
            return subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "Get-Clipboard -Raw"],
                capture_output=True, timeout=10).stdout.decode(
                    "utf-8", "replace").rstrip("\r\n")
        except Exception:
            return ""
    xc = which("xclip")
    if not xc:
        return ""
    return subprocess.run([xc, "-selection", "clipboard", "-o"],
                          capture_output=True, text=True).stdout


def clipboard_set(text: str) -> bool:
    """Put text on the clipboard, Unicode intact.

    Windows deliberately does NOT use `clip.exe`: it reads stdin in the OEM
    code page, so Hindi, Devanagari and anything outside ASCII arrives as
    question marks, and it appends a newline. For a product whose point is
    Hinglish that is not a corner case. Set-Clipboard reading UTF-8 from a
    file is the reliable route."""
    if IS_MAC:
        try:
            subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
            return True
        except Exception:
            return False
    if IS_WIN:
        import tempfile
        try:
            fd, path = tempfile.mkstemp(suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            ps = f"Set-Clipboard -Value ({_ps_read(path)})"
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, timeout=10)
            return r.returncode == 0
        except Exception:
            return False
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass
    xc = which("xclip")
    if not xc:
        return False
    subprocess.run([xc, "-selection", "clipboard"], input=text, text=True)
    return True


def frontmost_app() -> str:
    """The name of the app in front, '' if unknown. Windows: the executable
    of the foreground window's process, which is what a user would call it
    ('WindowsTerminal.exe', 'Code.exe')."""
    if IS_MAC:
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first '
                 'application process whose frontmost is true'],
                capture_output=True, text=True, timeout=3)
            return r.stdout.strip()
        except Exception:
            return ""
    if IS_WIN:
        try:
            u32 = ctypes.WinDLL("user32")
            hwnd = u32.GetForegroundWindow()
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return proc_name(int(pid.value))
        except Exception:
            return ""
    return ""


# ---------- a short system sound ----------------------------------------------
def chime_path() -> str:
    """The OS's own short notification sound, '' if there is none."""
    for p in (("/System/Library/Sounds/Tink.aiff",) if IS_MAC else
              (os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                            "Media", "Windows Notify System Generic.wav"),
               os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                            "Media", "chimes.wav")) if IS_WIN else ()):
        if os.path.exists(p):
            return p
    return ""


def play_async(path: str):
    """Start playing a sound file and return the Popen, None if impossible."""
    try:
        return subprocess.Popen(play_cmd(path), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception:
        return None


# ---------- an exclusive, non-blocking file lock ------------------------------
def try_lock(f) -> bool:
    """Take an exclusive lock on an open file without waiting. Released by the
    OS when the holder dies, which is the property the mic lock depends on:
    flock on POSIX, msvcrt.locking on Windows (byte-range, same lifetime)."""
    try:
        if IS_WIN:
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


# ---------- recording ---------------------------------------------------------
def record_cmd(rec: str, wav: str, max_secs: float, silence_stop: float = 0.0,
               norm: bool = False) -> list:
    """The command that records 16kHz mono 16-bit to `wav`.

    `rec` is the sox binary the caller found (POSIX); ignored on Windows.
    silence_stop > 0: trim leading quiet and end after that much silence (the
    hands-free take). 0: record until stopped (push-to-talk).

    The POSIX command is exactly the one stt.py always built, so nothing on a
    Mac changes. Windows runs vb/winrec.py under this same interpreter."""
    if IS_WIN:
        here = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, os.path.join(here, "winrec.py"), wav,
               "--max", str(max_secs)]
        if silence_stop > 0:
            cmd += ["--silence", str(silence_stop)]
        if norm:
            cmd.append("--norm")
        return cmd
    cmd = [rec, "-q", "-c", "1", "-r", "16000", "-b", "16", wav,
           "trim", "0", str(max_secs)]
    if silence_stop > 0:
        cmd += ["silence", "1", "0.05", "0.3%", "1", str(silence_stop), "1.5%"]
    if norm:
        cmd += ["norm", "-1"]
    return cmd


def recorder_available(rec: str) -> bool:
    """Can we record at all. Windows: sounddevice importable, which is the
    only dependency winrec has."""
    if IS_WIN:
        try:
            import sounddevice  # noqa: F401
            return True
        except Exception:
            return False
    return bool(rec)


def unlock(f) -> None:
    """Release a lock taken with try_lock. Closing the file would too."""
    try:
        if IS_WIN:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass


def pids_matching(pattern: str) -> list:
    """Our own pids whose command line contains `pattern`, never the caller.
    POSIX: the same `pgrep -U uid -f` this code always ran."""
    me = os.getpid()
    if not IS_WIN:
        args = ["pgrep"] + (["-U", str(uid())] if hasattr(os, "getuid") else [])
        try:
            r = subprocess.run(args + ["-f", pattern], capture_output=True,
                               text=True, timeout=5)
        except Exception:
            return []
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    return [p for p, cmd in cmdlines() if pattern in cmd and p != me]


def venv_python(venv) -> str:
    """The interpreter inside a venv. POSIX: bin/python. Windows: the venv
    module puts it in Scripts\\python.exe, and looking in bin/ is why the
    Kokoro server could never be started there."""
    if IS_WIN:
        return os.path.join(str(venv), "Scripts", "python.exe")
    return os.path.join(str(venv), "bin", "python")


def descendants(pid: int) -> list:
    """Every process started under `pid`, children first. Windows only needs
    this (POSIX reaches the tree through the process group)."""
    kids = {}
    for p, pp, _ in processes():
        kids.setdefault(pp, []).append(p)
    out, todo = [], list(kids.get(pid, []))
    while todo:
        p = todo.pop(0)
        if p in out or p == pid:
            continue          # a recycled pid can make the table cyclic
        out.append(p)
        todo.extend(kids.get(p, []))
    return out


# The Mac cue sounds and their nearest Windows equivalents. Windows ships a
# "Speech On/Off" pair for exactly this (it is what its own speech
# recognition plays), so start and stop stay distinguishable by ear.
_WIN_CUES = {
    "Tink": "Speech On.wav",
    "Pop": "Speech Off.wav",
    "Morse": "Speech Sleep.wav",
    "Basso": "Speech Misrecognition.wav",
}


def cue_sound(mac_path: str) -> str:
    """The file to play for a cue named by its macOS path; '' if none."""
    if IS_MAC:
        return mac_path
    if IS_WIN:
        stem = os.path.splitext(os.path.basename(mac_path))[0]
        name = _WIN_CUES.get(stem, "")
        if name:
            f = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                             "Media", name)
            if os.path.exists(f):
                return f
    return chime_path()


def proc_cwd(pid: int) -> str:
    """A process's working directory, '' if it can't be read.

    POSIX: lsof. Windows has no tool for it, and no API either: the only
    place it lives is the target's own PEB (ProcessParameters, CurrentDirectory),
    so this reads it out of the other process's memory, which is how Process
    Explorer shows the same column. 64-bit both sides, which is the only
    Python Windows build voicebridge installs."""
    if not pid or pid <= 0:
        return ""
    if not IS_WIN:
        try:
            r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd",
                                "-Fn"], capture_output=True, text=True,
                               timeout=3)
        except Exception:
            return ""
        for ln in r.stdout.splitlines():
            if ln.startswith("n"):
                return ln[1:]
        return ""
    try:
        nt = ctypes.WinDLL("ntdll")
        h = _k32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFO|VM_READ
        if not h:
            return ""
        try:
            pbi = (ctypes.c_void_p * 6)()
            nt.NtQueryInformationProcess.argtypes = [
                wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                wintypes.ULONG, ctypes.c_void_p]
            if nt.NtQueryInformationProcess(h, 0, pbi, ctypes.sizeof(pbi),
                                            None) != 0:
                return ""
            peb = pbi[1]

            def _read(addr, n):
                buf = ctypes.create_string_buffer(n)
                got = ctypes.c_size_t()
                if not _k32.ReadProcessMemory(h, addr, buf, n,
                                              ctypes.byref(got)) \
                        or got.value != n:
                    raise OSError("short read")
                return buf.raw

            import struct
            params = struct.unpack("<Q", _read(peb + 0x20, 8))[0]
            # CurrentDirectory.DosPath is a UNICODE_STRING at +0x38.
            ln, _mx, _pad, buf = struct.unpack(
                "<HHIQ", _read(params + 0x38, 16))
            if not ln or not buf:
                return ""
            path = _read(buf, ln).decode("utf-16-le")
            # Windows keeps a trailing backslash on it ("C:\x\"); POSIX doesn't.
            return path[:-1] if len(path) > 3 and path.endswith("\\") else path
        finally:
            _k32.CloseHandle(h)
    except Exception:
        return ""


def other_users_claude() -> dict:
    """{user: count} of Claude Code sessions run by OTHER accounts here.

    POSIX: ps user column. Windows: tasklist /V, which is the cheapest thing
    that reports a process's owner (CIM's GetOwner is a method call per
    process). Processes of other users show as 'N/A' unless we are elevated,
    which is the honest answer: there are sessions, and we can't say whose."""
    import getpass
    me = getpass.getuser()
    out = {}
    if IS_WIN:
        import csv
        import io
        try:
            txt = subprocess.run(
                ["tasklist", "/V", "/FO", "CSV", "/NH",
                 "/FI", "IMAGENAME eq claude.exe"],
                capture_output=True, text=True, timeout=15).stdout
        except Exception:
            return out
        for row in csv.reader(io.StringIO(txt)):
            if len(row) < 7 or not is_claude(row[0]):
                continue
            user = row[6].rsplit("\\", 1)[-1]
            if user.casefold() == me.casefold():
                continue
            out[user] = out.get(user, 0) + 1
        return out
    try:
        r = subprocess.run(["ps", "-axo", "user=,command="],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return out
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        user, cmd = parts
        if user == me:
            continue
        # the interactive CLI, not our own helpers or greps
        first = cmd.split()[0].rsplit("/", 1)[-1] if cmd.split() else ""
        if first == "claude":
            out[user] = out.get(user, 0) + 1
    return out


def stop_say() -> None:
    """Silence the system voice only (a barge-in), leaving Kokoro playback to
    its own owner. Mac: `pkill -x say`, as every caller did inline. Windows:
    our tagged SAPI PowerShell, which is the only system voice we start."""
    if IS_WIN:
        stop_all_speech()
        return
    subprocess.run(["pkill", "-x", "say" if IS_MAC else "espeak-ng"],
                   capture_output=True)


def open_file(path: str) -> bool:
    """Open a file in its default app (the phone QR image). Mac: `open`.
    Windows: os.startfile, the shell's own verb, no console flash."""
    try:
        if IS_MAC:
            subprocess.run(["open", path], capture_output=True)
            return True
        if IS_WIN:
            os.startfile(path)   # noqa: only exists on Windows
            return True
    except Exception:
        pass
    return False

"""voicebridge remote: drive your live session from your phone, anywhere.

A Telegram bridge that needs NO Claude Channels permission (org accounts
often block Channels): it runs entirely on your Mac. Your phone sends a
text or a voice note to your bot; the bridge transcribes locally, types it
into the focused Claude session (same injection as /voice-on), waits for
the reply in the transcript, and sends it back as text plus a spoken voice
note you can play.

Requirements while you're away:
  - the Mac stays awake (e.g. `caffeinate -dims`) with the Claude session
    open AND focused (injection types at the cursor)
  - bot token at ~/.voicebridge/telegram/.env (TELEGRAM_BOT_TOKEN=...)
  - your Telegram id in ~/.voicebridge/telegram/allow

Control: vb remote on | off | status
"""

import json
import os
import re
import subprocess
import sys
import time

from . import core, inject, stt
from .talkd import ACTIVE, _read_json

TG_DIR = core.STATE_DIR / "telegram"
PID = core.STATE_DIR / "remote.pid"
TMP = core.STATE_DIR / "remote-tmp"

FFMPEG = "/opt/homebrew/bin/ffmpeg"
if not os.path.exists(FFMPEG):
    FFMPEG = "/usr/local/bin/ffmpeg"


def _token() -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if tok:
        return tok
    try:
        for line in (TG_DIR / ".env").read_text().splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    return ""


def _allow() -> set:
    try:
        return {l.strip() for l in (TG_DIR / "allow").read_text().splitlines()
                if l.strip()}
    except Exception:
        return set()


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{_token()}/{method}"


def _tg(method: str, payload: dict, timeout: int = 40) -> dict:
    """Call the Bot API with curl (no extra deps)."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "-H", "Content-Type: application/json",
             "-d", json.dumps(payload), _api(method)],
            capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout or "{}")
    except Exception as e:
        core.log(f"remote tg {method} failed: {e}")
        return {}


def _send_text(chat_id: str, text: str) -> None:
    for i in range(0, len(text), 3800):
        _tg("sendMessage", {"chat_id": chat_id, "text": text[i:i + 3800]})


def _send_voice(chat_id: str, text: str) -> None:
    """say -> opus voice note -> Telegram. Best-effort; text already sent."""
    try:
        TMP.mkdir(parents=True, exist_ok=True)
        aiff = str(TMP / "reply.aiff")
        ogg = str(TMP / "reply.ogg")
        spoken = core.clean_for_speech(text, max_chars=3000)
        if not spoken:
            return
        # Same voice as the desk: prefer Kokoro; fall back to say.
        src = ""
        if core.get_engine() == "kokoro" and core.ensure_kokoro_server():
            wav = str(TMP / "reply-k.wav")
            src = core._kokoro_wav(spoken, out=wav)
        if not src:
            voice = core.get_voice()
            if not voice or core._KOKORO_VOICE_RE.match(voice):
                voice = ""
            cmd = ["/usr/bin/say", "-r", core.get_rate(), "-o", aiff]
            if voice:
                cmd += ["-v", voice]
            cmd.append(spoken)
            subprocess.run(cmd, timeout=240, check=True)
            src = aiff
        subprocess.run([FFMPEG, "-y", "-i", src, "-c:a", "libopus",
                        "-b:a", "32k", ogg],
                       capture_output=True, timeout=120, check=True)
        subprocess.run(
            ["curl", "-s", "-F", f"chat_id={chat_id}",
             "-F", f"voice=@{ogg};type=audio/ogg", _api("sendVoice")],
            capture_output=True, timeout=60)
    except Exception as e:
        core.log(f"remote voice-note failed: {e}")


def _download(file_id: str, out: str) -> bool:
    info = _tg("getFile", {"file_id": file_id})
    path = (info.get("result") or {}).get("file_path")
    if not path:
        return False
    url = f"https://api.telegram.org/file/bot{_token()}/{path}"
    r = subprocess.run(["curl", "-s", "-o", out, url],
                       capture_output=True, timeout=120)
    return r.returncode == 0 and os.path.getsize(out) > 100


def _target_transcript() -> str:
    """Prefer the session you voiced with /voice-on; else the newest."""
    active = _read_json(ACTIVE)
    if active and os.path.exists(active.get("transcript_path", "")):
        return active["transcript_path"]
    return core.newest_transcript()


def _resolve_text(msg: dict) -> str:
    voice = msg.get("voice") or msg.get("audio")
    if voice and voice.get("file_id"):
        TMP.mkdir(parents=True, exist_ok=True)
        raw = str(TMP / "in.oga")
        wav = str(TMP / "in.wav")
        if not _download(voice["file_id"], raw):
            return ""
        r = subprocess.run([FFMPEG, "-y", "-i", raw, "-ar", "16000",
                            "-ac", "1", wav], capture_output=True, timeout=120)
        if r.returncode != 0:
            return ""
        return stt.transcribe(wav)
    return (msg.get("text") or "").strip()


def run_daemon() -> int:
    if not _token():
        print("no bot token at ~/.voicebridge/telegram/.env")
        return 1
    core.log("remote: started")
    offset = 0
    first = _tg("getUpdates", {"timeout": 0, "offset": -1})
    if first.get("result"):
        offset = first["result"][-1]["update_id"] + 1
    fails = 0
    while True:
        res = _tg("getUpdates", {"timeout": 25, "offset": offset}, timeout=35)
        if not res or res.get("ok") is False or "result" not in res:
            # Network hiccup / no connection. Back off and stay quiet, one
            # log line per 10 consecutive failures instead of spamming.
            fails += 1
            if fails % 10 == 1:
                core.log(f"remote: getUpdates unavailable (x{fails}), backing off")
            time.sleep(min(30, 2 * fails))
            continue
        fails = 0
        for u in res.get("result") or []:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            frm = str((msg.get("from") or {}).get("id", ""))
            chat = str((msg.get("chat") or {}).get("id", ""))
            if not frm or not chat:
                continue
            allow = _allow()
            if not allow:
                _send_text(chat, f"voicebridge remote: your id is {frm}. Add "
                                 f"it to ~/.voicebridge/telegram/allow and "
                                 f"restart (vb remote off; vb remote on).")
                continue
            if frm not in allow:
                continue

            text = _resolve_text(msg)
            if not text:
                _send_text(chat, "(couldn't read that, try again?)")
                continue
            if re.match(r"^/?status$", text, re.IGNORECASE):
                tp = _target_transcript()
                _send_text(chat, f"linked to: {tp or '(no session found)'}")
                continue

            tp = _target_transcript()
            if not tp:
                _send_text(chat, "no Claude session is open on the Mac.")
                continue
            if msg.get("voice") or msg.get("audio"):
                _send_text(chat, f"heard: {text}")
            prev = core.last_assistant_text(tp)
            core.log(f"remote you: {text}")
            inject.paste_text(text, send=True)

            # Wait for the reply; keep the phone informed on long turns.
            t0 = time.time()
            reply = ""
            told = 0
            while time.time() - t0 < 600:
                time.sleep(2)
                cur = core.last_assistant_text(tp)
                if cur and cur != prev:
                    reply = cur
                    break
                waited = time.time() - t0
                if waited > 45 and told == 0:
                    _send_text(chat, "(still working...)")
                    told = 1
                elif waited > 240 and told == 1:
                    _send_text(chat, "(long task, still going...)")
                    told = 2
            if reply:
                _send_text(chat, reply)
                _send_voice(chat, reply)
            else:
                _send_text(chat, "(no reply within 10 minutes; check the Mac "
                                 "or send /status)")


def _alive() -> bool:
    try:
        os.kill(int(PID.read_text().strip()), 0)
        return True
    except Exception:
        return False


def on() -> str:
    if not _token():
        return ("ERROR: no bot token. Put TELEGRAM_BOT_TOKEN=... in "
                "~/.voicebridge/telegram/.env (create a bot via @BotFather).")
    if _alive():
        return "remote bridge already running"
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    vb = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin", "vb")
    p = subprocess.Popen([sys.executable, vb, "remote", "__run__"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    PID.write_text(str(p.pid))
    return (f"remote bridge ON (pid {p.pid}). Message your bot from your "
            f"phone. Keep this Mac awake and the session focused.")


def off() -> str:
    try:
        os.kill(int(PID.read_text().strip()), 15)
    except Exception:
        pass
    try:
        PID.unlink()
    except FileNotFoundError:
        pass
    return "remote bridge OFF"


def status() -> str:
    lines = [f"bridge : {'running' if _alive() else 'stopped'}",
             f"token  : {'set' if _token() else 'MISSING'}",
             f"allow  : {', '.join(_allow()) or '(empty)'}",
             f"target : {_target_transcript() or '(no session)'}"]
    return "\n".join(lines)

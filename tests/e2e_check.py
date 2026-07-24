"""voicebridge end-to-end functional check (manual / integration).

Unlike the unit tests, this exercises the REAL integrations: the warm STT
server, whisper transcription, the TTS engine, and the daemon lifecycle
(start, version stamp, stale-restart, and mic release on stop). It starts and
stops a real daemon (idle, no voiced session, so it never opens your mic) and
the STT server.

Run it directly (NOT under pytest, the filename has no test_ prefix on
purpose):  python3 tests/e2e_check.py

Prints a PASS/FAIL matrix and exits non-zero if anything fails.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core, stt, talkd  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="vb_e2e_"))
WAV = str(TMP / "probe.wav")
results = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"EXC {e!r}"
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")


def _make_probe():
    aiff = str(TMP / "p.aiff")
    subprocess.run(["say", "-o", aiff, "run the tests and push it"],
                   capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", aiff, "-ar", "16000", "-ac", "1",
                    WAV], capture_output=True)


def deps():
    miss = [k for k, v in stt.have_deps().items() if not v]
    return (not miss), "all present" if not miss else f"missing {miss}"


def warm_server():
    ok = stt.ensure_whisper_server(wait_s=25)
    return (ok and stt.whisper_up()), f"up={stt.whisper_up()}"


def warm_transcribe():
    t = time.time()
    text, conf = stt.transcribe_ex(WAV)
    dt = time.time() - t
    return ("run the tests" in text.lower() and dt < 2.5), \
        f"{dt:.2f}s conf {conf:.2f} -> {text!r}"


def live_level():
    lvl = stt.live_level(WAV)
    return (0.0 < lvl <= 1.0), f"level={lvl:.2f}"


def tts_audio():
    if core.kokoro_up():
        w = core._kokoro_wav("hello there", out=str(TMP / "kok.wav"))
        return (bool(w) and os.path.getsize(w) > 1000), "kokoro wav"
    out = str(TMP / "t.aiff")
    subprocess.run(["say", "-o", out, "hello"], capture_output=True)
    return (os.path.exists(out) and os.path.getsize(out) > 1000), "say audio"


def modes():
    got = [(talkd.set_mode(m), talkd.get_mode())[1]
           for m in ("all", "wake", "speak")]
    talkd.set_mode("all")
    return (got == ["all", "wake", "speak"]), f"{got}"


def hud():
    core.set_hud("hearing", 0.7)
    a = core.read_hud()["phase"] == "hearing"
    import json
    (core.STATE_DIR / "hud.json").write_text(json.dumps(
        {"phase": "listening", "ts": time.time() - 99}))
    b = core.read_hud()["phase"] == "off"
    return (a and b), f"set/read={a} stale->off={b}"


def statusline():
    # A state-only segment while speaking, and nothing at all when off (the
    # line belongs to the user, not to us).
    out = core.render_statusline({"phase": "speaking"})
    quiet = core.render_statusline({"phase": "off"}) == ""
    return (out == "vb 🔊 speaking" and quiet), f"{out!r} off-silent={quiet}"


def isolation():
    return (talkd.app_focused("", "X") is False       # can't confirm -> stop
            and talkd.app_focused("X", "X") is True    # match -> ok
            and talkd.app_focused("Y", "X") is False   # mismatch -> stop
            and talkd.app_focused("z", "") is True), "fail-closed matrix"


def cue():
    armed, dings = True, 0
    for _ in range(200):
        if talkd.should_ding("all", False, armed):
            dings, armed = dings + 1, False
    return (dings == 1), f"{dings} ding over 200 idle cycles"


def lifecycle():
    if talkd.VOICED.exists():
        for f in talkd.VOICED.iterdir():
            f.unlink()
    try:
        talkd.ACTIVE.unlink()
    except FileNotFoundError:
        pass
    talkd.stop_daemon()
    time.sleep(0.5)
    talkd.ensure_daemon()
    time.sleep(1.0)
    up = talkd.daemon_alive()
    ver_ok = talkd.VER.read_text().strip() == talkd._code_version()
    talkd.VER.write_text("1")            # simulate stale code
    old = talkd.PID.read_text().strip()
    talkd.ensure_daemon()
    time.sleep(1.0)
    restarted = talkd.PID.read_text().strip() != old and talkd.daemon_alive()
    talkd.stop_daemon()
    time.sleep(0.8)
    mic_free = subprocess.run(["pgrep", "-f", "talkd.wav"],
                              capture_output=True).returncode != 0
    return (up and ver_ok and restarted and mic_free), \
        f"start={up} stamp={ver_ok} stale_restart={restarted} mic_released={mic_free}"


def main():
    _make_probe()
    print("voicebridge end-to-end check")
    check("dependencies", deps)
    check("warm STT server", warm_server)
    check("warm transcription <2.5s + correct", warm_transcribe)
    check("live mic level", live_level)
    check("TTS produces audio", tts_audio)
    check("modes all/wake/speak", modes)
    check("HUD set/read + staleness", hud)
    check("status line (state only, silent when off)", statusline)
    check("per-session isolation (fail-closed)", isolation)
    check("cue dings once, not per cycle", cue)
    check("daemon lifecycle + mic release + stale-restart", lifecycle)
    n = sum(results)
    print(f"\n{n}/{len(results)} end-to-end checks passed")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

"""The Windows port: the OS layer's process, lock, recorder and speech pieces.

Two kinds of test live here.

  * Cross-platform ones run everywhere, including on a Mac. They pin the
    things the port changed underneath shared code: that checking a pid is
    alive never kills it, that the recorder writes a valid WAV even when it
    is killed mid-take, that the Mac sox command line is byte for byte what
    it was.
  * Windows-only ones need the real thing (SAPI, the clipboard, the
    installed whisper.cpp). They skip elsewhere, with the reason.

On CI (VB_WINDOWS_E2E=1) a missing whisper or model is a failure, not a
skip: the installer ran first, so "not installed" means the installer broke.

Run: python3 -m pytest -q tests/test_windows.py
"""
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import oslayer, winrec  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "speech-16k.wav"
E2E = os.environ.get("VB_WINDOWS_E2E") == "1"
win_only = pytest.mark.skipif(not oslayer.IS_WIN, reason="Windows only")


def _sleeper(**kw):
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], **kw)


def _secs(path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


# ---- processes ---------------------------------------------------------------

def test_alive_never_kills_what_it_checks():
    # The bug this port exists to prevent: on Windows os.kill(pid, 0) is
    # TerminateProcess, so every liveness check used to kill the daemon it
    # was checking on.
    p = _sleeper()
    try:
        for _ in range(5):
            assert oslayer.alive(p.pid)
        time.sleep(0.2)
        assert p.poll() is None, "alive() killed the process it checked"
    finally:
        p.kill()
        p.wait()


def test_alive_is_false_for_an_exited_process():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert oslayer.alive(p.pid) is False
    assert oslayer.alive(0) is False
    assert oslayer.alive(-5) is False


def test_terminate_stops_a_process():
    p = _sleeper()
    assert oslayer.terminate(p.pid)
    assert p.wait(timeout=10) is not None


def test_process_table_sees_us_and_our_children():
    p = _sleeper()
    try:
        table = {pid: (ppid, name) for pid, ppid, name in oslayer.processes()}
        assert os.getpid() in table
        assert p.pid in table and table[p.pid][0] == os.getpid()
        assert p.pid in oslayer.descendants(os.getpid())
        assert "python" in oslayer.proc_name(p.pid).lower()
    finally:
        p.kill()
        p.wait()


def test_proc_cwd_reads_another_process(tmp_path):
    # Windows reads it out of the target's PEB; a wrong offset returns
    # garbage rather than failing, so check the actual value.
    p = _sleeper(cwd=str(tmp_path))
    try:
        time.sleep(0.5)
        got = oslayer.proc_cwd(p.pid)
        assert got, "no cwd read"
        assert os.path.normcase(os.path.realpath(got)) == \
            os.path.normcase(os.path.realpath(str(tmp_path)))
    finally:
        p.kill()
        p.wait()


def test_pause_and_resume_a_process_tree(tmp_path):
    # hold() pauses a reply mid-sentence. The speech pid is a worker whose
    # CHILD makes the sound, so the pause has to reach the child too.
    tick = tmp_path / "tick"
    child = ("import time,sys\n"
             "p=sys.argv[1]\n"
             "while True:\n"
             "    open(p,'a').write('x'); time.sleep(0.05)\n")
    parent = ("import subprocess,sys,time\n"
              f"subprocess.Popen([sys.executable,'-c',{child!r},sys.argv[1]])\n"
              "time.sleep(60)\n")
    p = subprocess.Popen([sys.executable, "-c", parent, str(tick)],
                         start_new_session=True)
    try:
        deadline = time.time() + 10
        while (not tick.exists() or tick.stat().st_size < 3) \
                and time.time() < deadline:
            time.sleep(0.05)
        assert tick.exists(), "child never started"
        assert oslayer.set_paused(p.pid, True)
        time.sleep(0.3)
        frozen = tick.stat().st_size
        time.sleep(0.5)
        assert tick.stat().st_size == frozen, "the child kept running"
        assert oslayer.set_paused(p.pid, False)
        time.sleep(0.5)
        assert tick.stat().st_size > frozen, "the child did not resume"
    finally:
        oslayer.terminate_tree(p.pid)
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


def test_mic_lock_is_exclusive(tmp_path):
    lock = tmp_path / "mic.lock"
    a = open(lock, "a+")
    b = open(lock, "a+")
    try:
        assert oslayer.try_lock(a)
        assert not oslayer.try_lock(b), "two holders of the mic lock"
        oslayer.unlock(a)
        assert oslayer.try_lock(b)
        oslayer.unlock(b)
    finally:
        a.close()
        b.close()


def test_uid_is_stable_and_in_range():
    assert oslayer.uid() == oslayer.uid()
    assert oslayer.uid() >= 0


# ---- the recorder ------------------------------------------------------------

def test_mac_record_command_is_unchanged(monkeypatch):
    # The sox command line on a Mac is exactly what stt.py built inline
    # before it moved into the OS layer.
    monkeypatch.setattr(oslayer, "IS_WIN", False)
    assert oslayer.record_cmd("rec", "a.wav", 30, 2.0, True) == [
        "rec", "-q", "-c", "1", "-r", "16000", "-b", "16", "a.wav",
        "trim", "0", "30", "silence", "1", "0.05", "0.3%", "1", "2.0",
        "1.5%", "norm", "-1"]
    assert oslayer.record_cmd("rec", "a.wav", 120) == [
        "rec", "-q", "-c", "1", "-r", "16000", "-b", "16", "a.wav",
        "trim", "0", "120"]


def test_windows_record_command_runs_winrec(monkeypatch):
    monkeypatch.setattr(oslayer, "IS_WIN", True)
    cmd = oslayer.record_cmd("", "a.wav", 30, 2.0, True)
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("winrec.py") and os.path.exists(cmd[1])
    assert cmd[2:] == ["a.wav", "--max", "30", "--silence", "2.0", "--norm"]


def test_winrec_gates_silence_like_sox(tmp_path):
    # The fixture is 0.5s of silence, the sentence, then 1.5s of silence.
    out = tmp_path / "take.wav"
    rc = winrec.run(str(out), max_secs=30, silence=1.0, norm=False,
                    from_file=str(FIXTURE))
    assert rc == 0
    total = _secs(FIXTURE)
    got = _secs(out)
    # Leading silence dropped, the speech kept, stopped ~1s into the tail.
    assert 2.0 < got < total - 0.4, (got, total)
    with open(out, "rb") as f:
        head = f.read(4)
    assert head == b"RIFF"


def test_winrec_push_to_talk_keeps_everything_up_to_max(tmp_path):
    out = tmp_path / "ptt.wav"
    assert winrec.run(str(out), max_secs=1.0, silence=0, norm=False,
                      from_file=str(FIXTURE)) == 0
    assert abs(_secs(out) - 1.0) < 0.11


def test_winrec_header_is_true_while_recording(tmp_path):
    # Windows' terminate() runs no cleanup, so the header has to be right
    # after every block, not just at exit. Read it mid-take.
    out = tmp_path / "live.wav"
    w = winrec._Writer(str(out))
    w.write(b"\x01\x00" * 1600)
    with wave.open(str(out), "rb") as r:
        assert r.getnframes() == 1600
    w.write(b"\x01\x00" * 1600)
    with wave.open(str(out), "rb") as r:
        assert r.getnframes() == 3200
    w.close()


def test_winrec_norm_brings_the_peak_to_minus_one_dbfs(tmp_path):
    out = tmp_path / "n.wav"
    assert winrec.run(str(out), max_secs=30, silence=0, norm=True,
                      from_file=str(FIXTURE)) == 0
    import array
    with wave.open(str(out), "rb") as r:
        a = array.array("h")
        a.frombytes(r.readframes(r.getnframes()))
    peak = max(abs(x) for x in a)
    assert 0.85 * 32768 < peak <= 0.9 * 32768


def test_winrec_as_a_subprocess_writes_a_wav(tmp_path):
    # The way stt.py actually runs it.
    out = tmp_path / "sub.wav"
    script = os.path.join(os.path.dirname(oslayer.__file__), "winrec.py")
    r = subprocess.run([sys.executable, script, str(out), "--max", "30",
                        "--silence", "1.0", "--from-file", str(FIXTURE)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert _secs(out) > 2.0


def test_loudness_without_sox(tmp_path):
    from vb import stt
    assert stt._loudness_py(str(FIXTURE)) > 0.004
    quiet = tmp_path / "q.wav"
    with wave.open(str(quiet), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 1600)
    assert stt._loudness_py(str(quiet)) == 0.0


# ---- quoting text into PowerShell ---------------------------------------------

def test_powershell_literal_doubles_every_quote_it_honours():
    # PowerShell ends a '...' string at typographic quotes as well, and a
    # reply saying "it's" used to break out of the string.
    s = oslayer._ps_str("it\u2019s Bob's \u2018x\u2019")
    assert s == "'it\u2019\u2019s Bob''s \u2018\u2018x\u2019\u2019'"


def test_venv_python_path():
    py = oslayer.venv_python("V")
    if oslayer.IS_WIN:
        assert py == os.path.join("V", "Scripts", "python.exe")
    else:
        assert py == os.path.join("V", "bin", "python")


# ---- Windows only: the real platform pieces -------------------------------------

@win_only
def test_clipboard_roundtrip_keeps_unicode():
    # clip.exe turned this into question marks; Set-Clipboard must not.
    text = "voicebridge \u0928\u092e\u0938\u094d\u0924\u0947 it\u2019s fine"
    assert oslayer.clipboard_set(text)
    assert oslayer.clipboard_get() == text


@win_only
def test_sapi_writes_a_wav(tmp_path):
    out = tmp_path / "sapi.wav"
    assert oslayer.tts_to_wav("Hello from voicebridge, it\u2019s working.",
                              str(out), "175", "")
    assert out.stat().st_size > 4000
    assert _secs(out) > 0.5


@win_only
def test_chime_and_cues_resolve():
    assert oslayer.chime_path()
    assert oslayer.cue_sound("/System/Library/Sounds/Tink.aiff")


@win_only
def test_frontmost_and_fleet_do_not_crash():
    assert isinstance(oslayer.frontmost_app(), str)
    assert isinstance(oslayer.other_users_claude(), dict)


@win_only
def test_whisper_transcribes_the_fixture():
    from vb import stt
    if not stt.whisper_bin() or not stt.MODEL.exists():
        if E2E:
            pytest.fail("whisper-cli or the model is missing after install")
        pytest.skip("whisper.cpp not installed (run install.ps1)")
    text, conf = stt.transcribe_ex(str(FIXTURE))
    low = text.lower()
    assert "quick brown fox" in low and "lazy dog" in low, text
    assert conf > 0.3


@win_only
def test_kokoro_speaks_on_windows(tmp_path):
    # Kokoro used to be unreachable on Windows: the speech worker returned
    # early into SAPI before ever trying it.
    from vb import core
    venv = core.STATE_DIR / "kokoro-venv"
    if not os.path.exists(oslayer.venv_python(venv)):
        if E2E and os.environ.get("VB_KOKORO") != "0":
            pytest.fail("Kokoro venv missing after install")
        pytest.skip("Kokoro not installed")
    assert core.ensure_kokoro_server(wait_s=120), "Kokoro server did not start"
    out = str(tmp_path / "k.wav")
    assert core._kokoro_wav("Kokoro on Windows.", out) == out
    assert _secs(out) > 0.5


@win_only
@pytest.mark.skipif(not os.environ.get("VB_MIC_TEST"),
                    reason="needs a microphone; CI runners have none")
def test_records_from_the_microphone(tmp_path):
    out = tmp_path / "mic.wav"
    assert winrec.run(str(out), max_secs=2, silence=0, norm=False) == 0
    assert _secs(out) > 1.5

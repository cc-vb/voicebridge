"""/voice-off-all: end voice everywhere and give the microphone back.

Found by hand: `vb talkd status` listed five voiced sessions, four of them
from windows closed the day before. Nothing removes a marker for a session
that is closed rather than turned off, and /voice-off only knows about the
session it runs in, so those leftovers kept the daemon alive holding the
input device — and they could not be cleared from the windows that made
them, because those windows were gone.

Deleting the markers by hand did not help either: the daemon only re-reads
the directory when it hears a spoken exit phrase.

/voice-off keeps its old per-session behaviour. This covers the new command.
"""

import pytest

from vb import core, talkd


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    talk = tmp_path / "talk"
    voiced = talk / "voiced"
    voiced.mkdir(parents=True)
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(talkd, "STATE", talk)
    monkeypatch.setattr(talkd, "VOICED", voiced)
    monkeypatch.setattr(talkd, "LAST", talk / "last_prompt.json")
    monkeypatch.setattr(talkd, "ACTIVE", talk / "active.json")
    monkeypatch.setattr(talkd, "PID", talk / "talkd.pid")
    monkeypatch.setattr(talkd, "HOTKEY_PID", talk / "skhd.pid")
    monkeypatch.setattr(talkd, "APP", talk / "app")
    return tmp_path


@pytest.fixture
def daemon(monkeypatch):
    """A running daemon that records whether it was told to stop."""
    stopped = []
    monkeypatch.setattr(talkd, "daemon_alive", lambda: True)
    monkeypatch.setattr(talkd, "stop_daemon", lambda: stopped.append(True))
    return stopped


def _voice(sid):
    (talkd.VOICED / sid).write_text(f"/transcripts/{sid}.jsonl")


def _sids():
    return sorted(f.name for f in talkd.VOICED.iterdir())


# ---------- clearing every session -------------------------------------------

def test_clears_all_sessions(daemon):
    """The regression: four markers from closed windows, one live."""
    for sid in ("old1", "old2", "old3", "old4", "mine"):
        _voice(sid)

    out = talkd.voice_off_all()

    assert _sids() == []
    assert "5 session(s)" in out


def test_names_what_it_cleared(daemon):
    _voice("aaaaaaaa-1111")
    _voice("bbbbbbbb-2222")
    out = talkd.voice_off_all()
    assert "aaaaaaaa" in out and "bbbbbbbb" in out


def test_clears_the_active_pointer(daemon):
    _voice("mine")
    talkd.ACTIVE.write_text('{"session_id": "mine"}')
    talkd.voice_off_all()
    assert not talkd.ACTIVE.exists()


def test_works_from_a_terminal_that_never_voiced(daemon):
    """The whole point: run it from anywhere, including a fresh window with
    no recorded prompt of its own."""
    _voice("someone-elses-session")

    out = talkd.voice_off_all()

    assert "ERROR" not in out
    assert _sids() == []


# ---------- releasing the microphone -----------------------------------------

def test_stops_the_daemon(daemon):
    """Stopping the daemon is what actually frees the input device."""
    _voice("mine")
    out = talkd.voice_off_all()
    assert daemon == [True]
    assert "mic released" in out


def test_stops_the_daemon_even_with_no_sessions_registered(daemon):
    """The state we were left in by hand: zero markers, daemon still holding
    the mic. Deleting files does not wake it, so this must still stop it."""
    out = talkd.voice_off_all()
    assert daemon == [True]
    assert "no voiced sessions" in out


def test_says_so_when_the_daemon_was_already_stopped(monkeypatch):
    monkeypatch.setattr(talkd, "daemon_alive", lambda: False)
    stopped = []
    monkeypatch.setattr(talkd, "stop_daemon", lambda: stopped.append(True))

    out = talkd.voice_off_all()

    assert stopped == []
    assert "already stopped" in out


def test_reports_status(daemon):
    """No second command needed to confirm the mic is back."""
    _voice("mine")
    assert "daemon :" in talkd.voice_off_all()


def test_is_idempotent(daemon):
    _voice("mine")
    talkd.voice_off_all()
    out = talkd.voice_off_all()
    assert "no voiced sessions" in out
    assert _sids() == []


# ---------- /voice-off is unchanged ------------------------------------------

def test_off_still_only_touches_its_own_session():
    _voice("mine")
    _voice("someone-elses-window")
    talkd.LAST.write_text('{"session_id": "mine"}')

    out = talkd.voice_off(ensure=False)

    assert _sids() == ["someone-elses-window"]
    assert "1 voiced session(s) remain" in out


def test_off_still_errors_without_a_recorded_prompt():
    assert talkd.voice_off().startswith("ERROR")


def test_off_still_stops_the_daemon_when_it_was_the_last_one(monkeypatch):
    stopped = []
    monkeypatch.setattr(talkd, "stop_daemon", lambda: stopped.append(True))
    _voice("mine")
    talkd.LAST.write_text('{"session_id": "mine"}')

    out = talkd.voice_off()

    assert stopped == [True]
    assert "mic stopped" in out


# ---------- the silence hotkey follows voice ---------------------------------

def test_hotkey_starts_with_voice(tmp_path, monkeypatch):
    """skhd should not be a login daemon: the key only means something while
    we are speaking."""
    started = []
    monkeypatch.setattr(talkd.shutil, "which", lambda n: "/usr/bin/skhd")
    monkeypatch.setattr(talkd.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1})())
    monkeypatch.setattr(talkd.subprocess, "Popen",
                        lambda *a, **k: started.append(a) or type(
                            "P", (), {"pid": 4242})())
    talkd.hotkey_up()
    assert started
    assert talkd.HOTKEY_PID.read_text() == "4242"


def test_hotkey_not_started_twice(monkeypatch):
    monkeypatch.setattr(talkd.shutil, "which", lambda n: "/usr/bin/skhd")
    monkeypatch.setattr(talkd.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(talkd.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("started a second skhd"))
    talkd.hotkey_up()


def test_hotkey_noop_without_skhd_installed(monkeypatch):
    monkeypatch.setattr(talkd.shutil, "which", lambda n: None)
    monkeypatch.setattr(talkd.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("no skhd to start"))
    talkd.hotkey_up()
    assert not talkd.HOTKEY_PID.exists()


def test_hotkey_stops_the_one_we_started(monkeypatch):
    killed = []
    talkd.HOTKEY_PID.write_text("4242")
    monkeypatch.setattr(talkd, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(talkd.os, "kill", lambda pid, sig: killed.append(pid))
    talkd.hotkey_down()
    assert killed == [4242]
    assert not talkd.HOTKEY_PID.exists()


def test_hotkey_leaves_the_users_own_skhd_alone(monkeypatch):
    """No pid file means skhd was already running for the user's own
    bindings. Killing it would take their other hotkeys with it."""
    monkeypatch.setattr(talkd.os, "kill",
                        lambda pid, sig: pytest.fail("killed a foreign skhd"))
    talkd.hotkey_down()

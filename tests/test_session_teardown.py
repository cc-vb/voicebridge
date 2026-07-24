"""Voice must not outlive the session it was bound to.

Covers the owner-pid liveness test, the daemon's teardown when the session it
was voiced in disappears, and the tunnel reaper that stops every public link
into this Mac rather than only the most recent one.

Run: python3 tests/test_session_teardown.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import call, sessions, talkd  # noqa: E402


# ---- who owns the session ----------------------------------------------------

class _stub:
    """Swap a module attribute for the duration of a `with` block."""

    def __init__(self, mod, name, value):
        self.mod, self.name, self.value = mod, name, value

    def __enter__(self):
        self.old = getattr(self.mod, self.name)
        setattr(self.mod, self.name, self.value)

    def __exit__(self, *exc):
        setattr(self.mod, self.name, self.old)


def test_alive_when_owner_pid_is_a_live_claude():
    with _stub(talkd, "_is_claude_pid", lambda pid: pid == 4242):
        assert talkd.session_alive({"session_id": "s",
                                    "owner_pid": 4242}) is True


def test_dead_when_owner_pid_is_gone():
    with _stub(talkd, "_is_claude_pid", lambda pid: False):
        assert talkd.session_alive({"session_id": "s",
                                    "owner_pid": 4242}) is False


def test_no_recorded_owner_falls_back_to_any_claude_running():
    """Sessions voiced before the owner was recorded: an open mic can only be
    legitimate while SOME Claude session exists."""
    with _stub(talkd, "_any_claude_running", lambda: True):
        assert talkd.session_alive({"session_id": "s"}) is True
    with _stub(talkd, "_any_claude_running", lambda: False):
        assert talkd.session_alive({"session_id": "s"}) is False


def test_garbage_owner_pid_does_not_crash_the_watchdog():
    with _stub(talkd, "_any_claude_running", lambda: True):
        assert talkd.session_alive({"session_id": "s",
                                    "owner_pid": "nope"}) is True


# ---- what teardown actually stops --------------------------------------------

def _fake_state(tmp_path, monkeypatch, sids):
    voiced = tmp_path / "voiced"
    voiced.mkdir()
    for s in sids:
        (voiced / s).write_text("/x/transcript.jsonl")
    monkeypatch.setattr(talkd, "VOICED", voiced)
    monkeypatch.setattr(talkd, "ACTIVE", tmp_path / "active.json")
    monkeypatch.setattr(talkd, "CALL_OWNER", tmp_path / "call_owner")
    return voiced


def test_last_voiced_session_closing_releases_the_mic(tmp_path, monkeypatch):
    voiced = _fake_state(tmp_path, monkeypatch, ["S1"])
    calls = []
    monkeypatch.setattr(talkd.core, "hush", lambda: calls.append("hush"))
    monkeypatch.setattr(talkd, "stop_daemon", lambda: calls.append("stop"))
    out = talkd.session_closed("S1", "session closed")
    assert not (voiced / "S1").exists()      # marker gone
    assert calls == ["hush", "stop"]         # silenced, then mic released
    assert "mic released" in out


def test_other_voiced_sessions_keep_the_daemon(tmp_path, monkeypatch):
    _fake_state(tmp_path, monkeypatch, ["S1", "S2"])
    calls = []
    monkeypatch.setattr(talkd.core, "hush", lambda: None)
    monkeypatch.setattr(talkd, "stop_daemon", lambda: calls.append("stop"))
    out = talkd.session_closed("S1")
    assert calls == []                       # S2 is still listening
    assert "mic released" not in out


def test_closing_an_unvoiced_session_touches_nothing(tmp_path, monkeypatch):
    _fake_state(tmp_path, monkeypatch, ["S1"])
    calls = []
    monkeypatch.setattr(talkd.core, "hush", lambda: calls.append("hush"))
    monkeypatch.setattr(talkd, "stop_daemon", lambda: calls.append("stop"))
    assert talkd.session_closed("SOMEONE-ELSE") == ""
    assert calls == []                       # never silence another session


def test_phone_link_dies_with_the_session_that_opened_it(tmp_path, monkeypatch):
    _fake_state(tmp_path, monkeypatch, ["S1"])
    (tmp_path / "call_owner").write_text("S1")
    monkeypatch.setattr(talkd.core, "hush", lambda: None)
    monkeypatch.setattr(talkd, "stop_daemon", lambda: None)
    monkeypatch.setattr(call, "off", lambda: "off")
    out = talkd.session_closed("S1")
    assert "phone link closed" in out
    assert not (tmp_path / "call_owner").exists()


def test_phone_link_survives_an_unrelated_session_closing(tmp_path, monkeypatch):
    _fake_state(tmp_path, monkeypatch, ["S1", "S2"])
    (tmp_path / "call_owner").write_text("S2")
    monkeypatch.setattr(talkd.core, "hush", lambda: None)
    monkeypatch.setattr(talkd, "stop_daemon", lambda: None)
    monkeypatch.setattr(call, "off",
                        lambda: (_ for _ in ()).throw(AssertionError("killed")))
    assert "phone link" not in talkd.session_closed("S1")


# ---- the public links ---------------------------------------------------------

PS = """\
  26936 /opt/homebrew/bin/cloudflared tunnel --url http://127.0.0.1:8790
  29390 /opt/homebrew/bin/cloudflared tunnel --url http://127.0.0.1:8790
  97443 cloudflared tunnel --url http://localhost:3000
  12000 /usr/bin/ssh -R 80:localhost:8790 nokey@localhost.run
"""


def test_reaper_finds_every_tunnel_on_our_port():
    assert call.tunnel_pids(PS, 8790) == [26936, 29390]


def test_reaper_leaves_your_own_tunnels_alone():
    """A cloudflared you started for your own work is not ours to kill."""
    assert 97443 not in call.tunnel_pids(PS, 8790)
    assert call.tunnel_pids(PS, 3000) == [97443]


def test_reaper_accepts_the_localhost_spelling():
    assert call.tunnel_pids(
        "5 /opt/homebrew/bin/cloudflared tunnel --url http://localhost:8790",
        8790) == [5]


def test_reaper_ignores_junk_lines():
    assert call.tunnel_pids("\n  \nnotapid cloudflared --url "
                            "http://127.0.0.1:8790\n", 8790) == []


# ---- which sessions the phone may call ----------------------------------------

def _row(sid, proj="-Users-k-app", mtime=100):
    return {"sid": sid, "label": sid, "mtime": mtime,
            "path": f"/x/.claude/projects/{proj}/{sid}.jsonl"}


def test_recorded_owner_beats_the_process_guess():
    """Two sessions in one project dir, one still open. The dir-counting guess
    marks the most recent one active; the recorded owners know which."""
    rows = [_row("open", mtime=100), _row("exited", mtime=200)]
    with _stub(talkd, "known_owners", lambda: {"open": 11, "exited": 22}), \
            _stub(talkd, "live_claude_pids", lambda: {11}):
        out = {r["sid"]: r["active"]
               for r in sessions.mark_active(rows, {"/Users/k/app": 1})}
    assert out == {"open": True, "exited": False}


def test_unknown_sessions_keep_the_old_guess():
    rows = [_row("legacy", mtime=200)]
    with _stub(talkd, "known_owners", lambda: {}):
        assert sessions.mark_active(rows, {"/Users/k/app": 1})[0]["active"]


def test_owner_lookup_failing_never_breaks_the_roster():
    def boom():
        raise OSError("no /proc for you")
    rows = [_row("a", mtime=200)]
    with _stub(talkd, "known_owners", boom):
        assert sessions.mark_active(rows, {"/Users/k/app": 1})[0]["active"]


# ---- the relay's front door ---------------------------------------------------

class _Req:
    """Just enough of a request for Handler._authed."""

    def __init__(self, path, headers=None):
        self.path, self.headers = path, headers or {}


def test_relay_refuses_everything_when_no_secret_is_configured():
    """Fail closed: an unconfigured relay behind a public tunnel would
    otherwise let anyone with the URL type into this Mac."""
    with _stub(call, "_secret", lambda: ""):
        assert call.Handler._authed(_Req("/sessions?k=anything")) is False
        assert call.Handler._authed(_Req("/sessions")) is False


def test_relay_accepts_the_configured_secret_only():
    with _stub(call, "_secret", lambda: "vb-right"):
        assert call.Handler._authed(_Req("/sessions?k=vb-right")) is True
        assert call.Handler._authed(_Req("/sessions?k=vb-wrong")) is False
        assert call.Handler._authed(
            _Req("/sessions", {"x-vapi-secret": "vb-right"})) is True


if __name__ == "__main__":
    test_recorded_owner_beats_the_process_guess()
    test_unknown_sessions_keep_the_old_guess()
    test_owner_lookup_failing_never_breaks_the_roster()
    test_relay_refuses_everything_when_no_secret_is_configured()
    test_relay_accepts_the_configured_secret_only()
    test_alive_when_owner_pid_is_a_live_claude()
    test_dead_when_owner_pid_is_gone()
    test_no_recorded_owner_falls_back_to_any_claude_running()
    test_garbage_owner_pid_does_not_crash_the_watchdog()
    test_reaper_finds_every_tunnel_on_our_port()
    test_reaper_leaves_your_own_tunnels_alone()
    test_reaper_accepts_the_localhost_spelling()
    test_reaper_ignores_junk_lines()
    print("ok  teardown: owner liveness + tunnel reaper "
          "(run under pytest for the monkeypatch cases)")

"""Hold a key anywhere, speak, and the words land where your cursor is.

This is the one path allowed to paste into any app, so the guards matter more
than the feature. The failure everyone in this category ships is text arriving
in a window you were not looking at.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vb import dictate


def _quiet(monkeypatch):
    monkeypatch.setattr(dictate.core, "set_hud", lambda *a, **k: None)
    monkeypatch.setattr(dictate.core, "log", lambda *a, **k: None)


def test_moving_to_another_app_mid_sentence_drops_the_words(monkeypatch):
    """The one failure that cannot be undone by pressing undo: you may not even
    see where the text went."""
    _quiet(monkeypatch)
    pasted = []
    monkeypatch.setattr(dictate.stt, "transcribe", lambda w: "deploy the thing")
    monkeypatch.setattr(dictate.inject, "frontmost_app", lambda: "Slack")
    monkeypatch.setattr(dictate, "_paste_where_you_are",
                        lambda t, send=False: pasted.append(t))
    monkeypatch.setattr(dictate.os, "unlink", lambda p: None)

    d = dictate.Dictation()
    d._finish("/tmp/x.wav", "Terminal")      # pressed in Terminal, now in Slack
    assert pasted == []


def test_staying_put_pastes(monkeypatch):
    _quiet(monkeypatch)
    pasted = []
    monkeypatch.setattr(dictate.stt, "transcribe", lambda w: "deploy the thing")
    monkeypatch.setattr(dictate.inject, "frontmost_app", lambda: "Terminal")
    monkeypatch.setattr(dictate, "_paste_where_you_are",
                        lambda t, send=False: pasted.append(t))
    monkeypatch.setattr(dictate.os, "unlink", lambda p: None)

    d = dictate.Dictation()
    d._finish("/tmp/x.wav", "Terminal")
    assert pasted == ["deploy the thing"]


def test_silence_pastes_nothing(monkeypatch):
    _quiet(monkeypatch)
    pasted = []
    monkeypatch.setattr(dictate.stt, "transcribe", lambda w: "   ")
    monkeypatch.setattr(dictate.inject, "frontmost_app", lambda: "Terminal")
    monkeypatch.setattr(dictate, "_paste_where_you_are",
                        lambda t, send=False: pasted.append(t))
    monkeypatch.setattr(dictate.os, "unlink", lambda p: None)
    dictate.Dictation()._finish("/tmp/x.wav", "Terminal")
    assert pasted == []


def test_a_failed_transcription_is_not_pasted_as_an_error(monkeypatch):
    _quiet(monkeypatch)
    pasted = []
    def boom(w):
        raise RuntimeError("whisper died")
    monkeypatch.setattr(dictate.stt, "transcribe", boom)
    monkeypatch.setattr(dictate.inject, "frontmost_app", lambda: "Terminal")
    monkeypatch.setattr(dictate, "_paste_where_you_are",
                        lambda t, send=False: pasted.append(t))
    monkeypatch.setattr(dictate.os, "unlink", lambda p: None)
    dictate.Dictation()._finish("/tmp/x.wav", "Terminal")
    assert pasted == []


def test_a_mis_press_never_reaches_the_model(monkeypatch):
    """Too short to be speech. Stop the recorder and go no further."""
    _quiet(monkeypatch)
    started = []
    monkeypatch.setattr(dictate.threading, "Thread",
                        lambda **k: started.append(k) or _NoThread())
    d = dictate.Dictation()
    d.proc = _FakeProc()
    d.up(held_ms=80)
    assert started == []


def test_a_real_hold_does_reach_the_model(monkeypatch):
    _quiet(monkeypatch)
    started = []
    monkeypatch.setattr(dictate.threading, "Thread",
                        lambda **k: started.append(k) or _NoThread())
    d = dictate.Dictation()
    d.proc = _FakeProc()
    d.up(held_ms=1200)
    assert len(started) == 1


def test_a_chord_cancels_without_transcribing(monkeypatch):
    """fn+left is Home. It must not become a recording."""
    _quiet(monkeypatch)
    d = dictate.Dictation()
    p = _FakeProc()
    d.proc = p
    d.cancel()
    assert p.killed and d.proc is None


def test_the_helper_is_preferred_when_it_exists(monkeypatch):
    """The helper waits for a READ RECEIPT before restoring the clipboard.
    The fallback below guesses with a timer, and a guess about somebody else's
    scheduling is what pastes your old clipboard when the machine is busy."""
    calls = []
    monkeypatch.setattr(dictate, "_paste_helper", lambda: "/fake/vb-paste")
    monkeypatch.setattr(dictate.subprocess, "run",
                        lambda a, **k: calls.append(a) or _Ok())
    dictate._paste_where_you_are("hello there")
    assert calls and calls[0][:2] == ["/fake/vb-paste", "hello there"]
    assert "--send" not in calls[0]


def test_the_helper_is_told_to_send_only_when_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(dictate, "_paste_helper", lambda: "/fake/vb-paste")
    monkeypatch.setattr(dictate.subprocess, "run",
                        lambda a, **k: calls.append(a) or _Ok())
    dictate._paste_where_you_are("hello", send=True)
    assert "--send" in calls[0]


def test_without_the_helper_the_clipboard_still_comes_back(monkeypatch):
    """A machine with no Swift toolchain falls back to the timing version.
    Worse, but it must not lose what the user said or what they had copied."""
    clip = {"v": "something I had copied"}
    order = []
    monkeypatch.setattr(dictate, "_paste_helper", lambda: "")
    monkeypatch.setattr(dictate.inject, "_pbpaste", lambda: clip["v"])
    def copy(t):
        clip["v"] = t
        order.append(t)
    monkeypatch.setattr(dictate.inject, "_pbcopy", copy)
    monkeypatch.setattr(dictate.inject, "_osa", lambda s: order.append("PASTE"))
    monkeypatch.setattr(dictate.time, "sleep", lambda s: None)

    dictate._paste_where_you_are("hello there")
    assert order == ["hello there", "PASTE", "something I had copied"]
    assert clip["v"] == "something I had copied"


class _Ok:
    returncode = 0
    stdout = b"read"
    stderr = b""


def test_return_is_never_pressed_by_default(monkeypatch):
    """Dictation puts words in a field. Deciding to send them is yours, and it
    is what makes a misfire harmless."""
    monkeypatch.setattr(dictate, "_paste_helper", lambda: "")
    keys = []
    monkeypatch.setattr(dictate.inject, "_pbpaste", lambda: "")
    monkeypatch.setattr(dictate.inject, "_pbcopy", lambda t: None)
    monkeypatch.setattr(dictate.inject, "_osa", lambda s: keys.append(s))
    monkeypatch.setattr(dictate.time, "sleep", lambda s: None)
    dictate._paste_where_you_are("hello")
    assert not any("key code 36" in k for k in keys)


class _FakeProc:
    def __init__(self):
        self.killed = False
    def terminate(self):
        self.killed = True
    def wait(self, timeout=None):
        return 0
    def kill(self):
        self.killed = True


class _NoThread:
    def start(self):
        pass

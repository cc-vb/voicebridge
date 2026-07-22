"""The speak-and-listen loop itself, not just the guards it calls.

A refactor once left a stale `overlap` reference in the log line after
say.terminate(). The NameError landed in the broad `except`, so every
barge-in cut the reply off and then threw the user's words away: replies
died mid-sentence for no visible reason. The guard functions were fully
tested and all passed, because the bug was in the glue between them.

These tests drive _speak_interruptible with fakes, so the loop's control
flow and failure handling are covered without a mic or a speaker.
"""

import pytest

from vb import core, stt, talkd


class FakeSay:
    """A speech player that finishes after `ticks` polls."""

    def __init__(self, ticks=3):
        self.ticks = ticks
        self.terminated = False
        self.waited = False

    def poll(self):
        if self.terminated:
            return 0
        self.ticks -= 1
        return None if self.ticks > 0 else 0

    def terminate(self):
        self.terminated = True

    def wait(self):
        self.waited = True


class FakeRec:
    """A recorder that always has a finished capture ready."""

    def poll(self):
        return 0

    def terminate(self):
        pass

    def wait(self):
        pass


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Wire up talkd so the loop runs against fakes."""
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(talkd, "STATE", tmp_path)
    monkeypatch.setattr(talkd, "VOICED", tmp_path / "voiced")
    (tmp_path / "voiced").mkdir()
    monkeypatch.setattr(talkd, "BARGE", tmp_path / "barge")
    monkeypatch.setattr(talkd, "SENS", tmp_path / "sens")
    # APP is computed from the real state dir at import, so without this the
    # loop reads ~/.voicebridge/talk/app and calls the real frontmost-app
    # check: if a bound app exists and this process isn't it, the barge loop
    # breaks early and the test flakes. Point it at the (absent) tmp file so
    # bound_app() is "" (unbound) and the focus gate stays out of the way.
    monkeypatch.setattr(talkd, "APP", tmp_path / "app")

    monkeypatch.setattr(core, "clean_for_speech", lambda t, max_chars=0: t)
    monkeypatch.setattr(core, "_recently_spoken", lambda t: False)
    monkeypatch.setattr(core, "hush", lambda: None)
    monkeypatch.setattr(stt, "record_start",
                        lambda wav, **kw: FakeRec())
    monkeypatch.setattr(stt, "loudness", lambda wav: 0.02)
    monkeypatch.setattr(talkd.os.path, "getsize", lambda p: 99999)
    monkeypatch.setattr(talkd.os, "remove", lambda p: None)
    return monkeypatch


def test_a_real_barge_in_is_returned_not_swallowed(wired, monkeypatch):
    """The regression: the words that cut the reply must come back."""
    say = FakeSay(ticks=99)
    monkeypatch.setattr(core, "start_speech", lambda t: say)
    monkeypatch.setattr(stt, "transcribe_ex",
                        lambda wav: ("no stop use the other branch", 0.9))

    got = talkd._speak_interruptible("a long reply being read aloud")

    assert got == "no stop use the other branch"
    assert say.terminated, "speech should be cut when a real barge-in lands"


def test_uninterrupted_reply_plays_out_and_returns_empty(wired, monkeypatch):
    say = FakeSay(ticks=3)
    monkeypatch.setattr(core, "start_speech", lambda t: say)
    monkeypatch.setattr(stt, "transcribe_ex", lambda wav: ("Adios.", 0.9))

    assert talkd._speak_interruptible("a reply") == ""
    assert not say.terminated, "a stray word must not cut the reply"


def test_a_crash_after_cutting_speech_still_returns_the_words(wired,
                                                              monkeypatch):
    """Exactly the shape of the NameError bug: if we already stopped the
    speech, the user's words must not be silently discarded."""
    say = FakeSay(ticks=99)
    monkeypatch.setattr(core, "start_speech", lambda t: say)
    monkeypatch.setattr(stt, "transcribe_ex",
                        lambda wav: ("no stop use the other branch", 0.9))

    real_log = core.log
    calls = {"n": 0}

    def exploding_log(msg):
        # Blow up on the post-terminate log line, like the stale `overlap`
        # reference did, but leave the earlier logging intact.
        if "barge-in" in msg:
            calls["n"] += 1
            raise NameError("name 'overlap' is not defined")
        return real_log(msg)

    monkeypatch.setattr(core, "log", exploding_log)

    got = talkd._speak_interruptible("a long reply being read aloud")

    assert calls["n"] == 1, "the exploding path should have been hit"
    assert say.terminated
    assert got == "no stop use the other branch", (
        "speech was cut, so the words that cut it must be returned")


def test_barge_disabled_plays_the_reply_to_the_end(wired, monkeypatch):
    (talkd.BARGE).write_text("off")
    say = FakeSay(ticks=99)
    monkeypatch.setattr(core, "start_speech", lambda t: say)

    assert talkd._speak_interruptible("a reply") == ""
    assert say.waited
    assert not say.terminated


def test_no_speech_player_is_handled(wired, monkeypatch):
    monkeypatch.setattr(core, "start_speech", lambda t: None)
    assert talkd._speak_interruptible("a reply") == ""


def test_recorder_failure_lets_the_reply_finish(wired, monkeypatch):
    """No mic (device busy in a meeting) must not cut the reply short."""
    say = FakeSay(ticks=99)
    monkeypatch.setattr(core, "start_speech", lambda t: say)
    monkeypatch.setattr(stt, "record_start", lambda wav, **kw: None)

    assert talkd._speak_interruptible("a reply") == ""
    assert say.waited
    assert not say.terminated

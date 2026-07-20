"""Voice must reach the session it was turned on in, and nowhere else.

Motivated by a live incident: during a video meeting the user's speech was
pasted into Chrome (the log shows `paste -> frontmost app: Google Chrome`)
and Return was pressed, and the meeting's microphone dropped out while the
daemon held the input device in a record loop.

Both come from the same assumption, that whatever is frontmost is the right
target. It isn't.
"""

import pytest

from vb import core, inject, talkd


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(talkd, "STATE", tmp_path / "talk")
    monkeypatch.setattr(talkd, "APP", tmp_path / "talk" / "app")
    (tmp_path / "talk").mkdir(parents=True)
    return tmp_path


# ---------- the focus gate ---------------------------------------------------

def test_bound_app_focused_is_live(isolated_state):
    assert talkd.app_focused("Ghostty", "Ghostty") is True


@pytest.mark.parametrize("other", [
    "Google Chrome",     # the meeting itself
    "zoom.us",
    "Slack",
    "Messages",
    "Terminal",          # a DIFFERENT terminal is still not ours
])
def test_any_other_app_is_dormant(isolated_state, other):
    assert talkd.app_focused(other, "Ghostty") is False


def test_app_match_ignores_case_and_padding(isolated_state):
    assert talkd.app_focused(" ghostty ", "Ghostty") is True


def test_unbound_falls_back_to_legacy_always_on(isolated_state):
    """Upgrading from a version with no binding must not go silent."""
    assert talkd.app_focused("Google Chrome", "") is True


def test_undetectable_frontmost_does_not_deadlock_the_mic(isolated_state):
    """osascript can fail; failing closed forever would look like a hang."""
    assert talkd.app_focused("", "Ghostty") is True


# ---------- binding ----------------------------------------------------------

def test_bind_and_read_back(isolated_state):
    talkd.bind_app("iTerm2")
    assert talkd.bound_app() == "iTerm2"


def test_bound_app_is_empty_before_any_bind(isolated_state):
    assert talkd.bound_app() == ""


def test_bind_ignores_an_undetectable_app(isolated_state, monkeypatch):
    monkeypatch.setattr(inject, "frontmost_app", lambda: "")
    talkd.bind_app("Ghostty")
    talkd.bind_app()           # detection failed; must not clobber the bind
    assert talkd.bound_app() == "Ghostty"


# ---------- injection refuses the wrong target -------------------------------

class Spy:
    """Records whether the paste actually reached the keyboard."""

    def __init__(self):
        self.scripts = []

    def __call__(self, script):
        self.scripts.append(script)


@pytest.fixture
def spy(monkeypatch):
    s = Spy()
    monkeypatch.setattr(inject, "_osa", s)
    monkeypatch.setattr(inject, "_pbcopy", lambda text: None)
    monkeypatch.setattr(inject, "_pbpaste", lambda: "")
    return s


def test_paste_refused_when_a_meeting_is_frontmost(monkeypatch, spy):
    """The live incident: no keystroke, no Return, nothing typed."""
    monkeypatch.setattr(inject, "frontmost_app", lambda: "Google Chrome")
    assert inject.paste_text("cancel the migration", send=True,
                             expect_app="Ghostty") is False
    assert spy.scripts == []


def test_paste_delivered_to_the_bound_app(monkeypatch, spy):
    monkeypatch.setattr(inject, "frontmost_app", lambda: "Ghostty")
    assert inject.paste_text("run the tests", send=True,
                             expect_app="Ghostty") is True
    assert any("keystroke" in s for s in spy.scripts)
    assert any("key code 36" in s for s in spy.scripts)   # Return pressed


def test_unbound_paste_still_works(monkeypatch, spy):
    monkeypatch.setattr(inject, "frontmost_app", lambda: "Google Chrome")
    assert inject.paste_text("hello", send=True) is True


def test_empty_text_is_never_pasted(monkeypatch, spy):
    monkeypatch.setattr(inject, "frontmost_app", lambda: "Ghostty")
    assert inject.paste_text("", send=True, expect_app="Ghostty") is False
    assert spy.scripts == []

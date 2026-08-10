"""Issue #8: a prompt must never land in a window you did not name.

The guard existed and failed in three concrete ways, all of which are here:

  1. It was skipped entirely when no app was named, which is precisely the
     phone-only session that never ran /voice-on. Two callers were in that
     state, so their text went to whatever had the cursor, plus Return.
  2. press_escape carried no guard at all, and Escape in the wrong place
     discards what somebody was typing while the agent you meant to stop keeps
     running.
  3. It compared app NAMES, so a second tab of the same terminal passed the
     check and received the keystrokes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vb import inject  # noqa: E402


def _stub(monkey: dict):
    """Replace the parts that touch the machine, remember what was attempted."""
    done = []
    inject.frontmost_app = lambda: monkey.get("front", "Slack")
    inject._terminal_inject = lambda text, tty, submit=True: (
        done.append(("tty", tty, text)) or monkey.get("tty_ok", True))
    inject._claude_terminal_tty = lambda: monkey.get("guess_tty", "")
    inject._activate_app = lambda name: (
        done.append(("activate", name)) or monkey.get("activate_ok", True))
    inject._select_tty = lambda tty: (
        done.append(("select", tty)) or monkey.get("select_ok", True))
    inject._osa = lambda script: done.append(("osa", script[:40]))
    inject._pbpaste = lambda: ""
    inject._pbcopy = lambda t: done.append(("copy", t))
    return done


def test_a_paste_with_no_named_target_never_reaches_a_chat_window():
    """This is the phone-only session: no /voice-on, so no bound app. It used to
    paste into the frontmost window and press Return, whatever that was."""
    done = _stub({"front": "Slack"})
    ok = inject.paste_text("rm -rf something", send=True)
    assert ok is False, "typed into whatever was in front"
    assert not [d for d in done if d[0] in ("copy", "osa")], done
    done = _stub({"front": "Google Chrome"})
    assert inject.paste_text("cancel the migration", send=True) is False


def test_a_named_tty_is_used_and_the_frontmost_app_is_irrelevant():
    """Being in Slack must not matter: the tab is addressed directly."""
    done = _stub({"front": "Slack"})
    ok = inject.paste_text("hello", send=True, expect_app="Terminal",
                           target_tty="/dev/ttys004")
    assert ok is True
    assert done[0] == ("tty", "/dev/ttys004", "hello"), done
    assert not [d for d in done if d[0] == "activate"], "raised a window anyway"


def test_an_unreachable_tty_never_degrades_into_a_blind_paste():
    """The point of knowing the tty is that another tab of the same app is a
    different session. Falling back to "frontmost" threw that away, and the
    name-only guard let the wrong tab through."""
    done = _stub({"front": "Terminal", "tty_ok": False})
    ok = inject.paste_text("hello", send=True, expect_app="Terminal",
                           target_tty="/dev/ttys004")
    assert ok is False, "fell back to typing into the front window"
    assert not [d for d in done if d[0] in ("copy", "osa")], done


def test_escape_with_no_target_is_refused():
    done = _stub({})
    assert inject.press_escape() is False
    assert not [d for d in done if d[0] == "osa"], done


def test_escape_selects_the_exact_tab_before_sending():
    """Checking the app name let Escape land in a second window of the same
    terminal. The tab is raised first, then restored."""
    done = _stub({"front": "Slack"})
    ok = inject.press_escape(expect_app="Terminal", target_tty="/dev/ttys004")
    assert ok is True
    kinds = [d[0] for d in done]
    assert kinds[0] == "select", done
    assert "osa" in kinds, done
    assert ("activate", "Slack") in done, "did not put you back where you were"


def test_escape_refuses_when_the_tab_cannot_be_found():
    done = _stub({"front": "Slack", "select_ok": False})
    assert inject.press_escape(expect_app="Terminal",
                               target_tty="/dev/ttys004") is False
    assert not [d for d in done if d[0] == "osa"], done


def test_interrupt_reports_failure_instead_of_claiming_success():
    """It always returned True, so "stopped it" was printed whether or not
    anything was stopped."""
    from vb import adapters
    a = adapters.ClaudeCodeAdapter()
    inject.press_escape = lambda expect_app="", target_tty="": False
    assert a.interrupt("some-sid") is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  injection target: fails closed, addresses the tab, never guesses")

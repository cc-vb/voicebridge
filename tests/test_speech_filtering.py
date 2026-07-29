"""Machinery must never be spoken (hook output, system-reminders), but real
text with angle brackets / comparisons must survive.

Run: python3 tests/test_speech_filtering.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402


def test_strips_hook_and_system_machinery():
    out = core._strip_machinery(
        "Done. <user-prompt-submit-hook>saved a learning to notes</user-prompt-submit-hook> "
        "Next up?")
    assert "saved a learning" not in out
    assert "Done." in out and "Next up?" in out

    out2 = core._strip_machinery(
        "Before <system-reminder>internal: do not reveal this</system-reminder> after")
    assert "internal: do not reveal" not in out2
    assert "Before" in out2 and "after" in out2

    out3 = core._strip_machinery(
        "<local-command-stdout>hook ran: 3 findings</local-command-stdout>ok")
    assert "hook ran" not in out3 and "ok" in out3


def test_leaves_real_text_untouched():
    # a real comparison and a code-ish tag are NOT machinery
    s = "use x < 5 and a <div> tag, and 2 > 1"
    assert core._strip_machinery(s) == s


def test_clean_for_speech_drops_machinery_end_to_end():
    spoken = core.clean_for_speech(
        "Fixed the bug. <session-start-hook>starting up, loaded 5 memories"
        "</session-start-hook> Want me to run the tests?")
    assert "loaded 5 memories" not in spoken
    assert "Fixed the bug" in spoken and "run the tests" in spoken


if __name__ == "__main__":
    test_strips_hook_and_system_machinery()
    test_leaves_real_text_untouched()
    test_clean_for_speech_drops_machinery_end_to_end()
    print("ok  speech filtering: machinery stripped, real text kept")

"""Regression: clean_for_speech must actually run (not be mocked away).

A refactor once dropped the _FENCE regex definition while it was still
referenced in _strip_for_speech, so every real speak call raised NameError.
The suite stayed green because the speak tests mock clean_for_speech. This
test calls the real thing and exercises each markdown-stripping pattern, so a
missing pattern fails loudly here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402


def test_clean_for_speech_runs_on_all_markdown():
    out = core.clean_for_speech(
        "Here is ```def f(): pass``` and `inline` and **bold** and "
        "[a link](http://example.com) and a plain https://x.io URL.\n- bullet")
    assert "def f" not in out          # fenced code never read aloud
    assert "code on screen" in out     # replaced by a spoken landmark
    assert "inline" in out             # inline code kept, backticks gone
    assert "`" not in out
    assert "**" not in out             # markdown tokens gone
    assert "http" not in out           # urls spoken as words / stripped
    assert out                          # non-empty, no exception


def test_clean_for_speech_empty_is_safe():
    assert core.clean_for_speech("") == ""


def test_code_block_becomes_a_spoken_landmark_with_size():
    # A fenced block must NOT vanish into two words; it should announce itself
    # so the listener's eye and ear meet at the code (the "lose my place" fix).
    out = core.clean_for_speech(
        "Here:\n```python\na = 1\nb = 2\nc = 3\nreturn a\n```\nDone.")
    assert "code on screen" in out
    assert "lines" in out            # carries a size
    assert "a = 1" not in out        # never reads the code aloud
    assert "python" in out           # language when the fence gives it


def test_inline_code_is_still_kept_not_landmarked():
    out = core.clean_for_speech("run `npm test` first")
    assert "npm test" in out
    assert "code on screen" not in out


if __name__ == "__main__":
    test_clean_for_speech_runs_on_all_markdown()
    test_clean_for_speech_empty_is_safe()
    print("ok  clean_for_speech runs end to end (guards the _FENCE regression)")

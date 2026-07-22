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
    assert "def f" not in out          # fenced code dropped
    assert "(code block)" in out
    assert "inline" in out             # inline code kept, backticks gone
    assert "`" not in out
    assert "**" not in out             # markdown tokens gone
    assert "http" not in out           # urls spoken as words / stripped
    assert out                          # non-empty, no exception


def test_clean_for_speech_empty_is_safe():
    assert core.clean_for_speech("") == ""


if __name__ == "__main__":
    test_clean_for_speech_runs_on_all_markdown()
    test_clean_for_speech_empty_is_safe()
    print("ok  clean_for_speech runs end to end (guards the _FENCE regression)")

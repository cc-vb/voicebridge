"""Sentence-level back/skip: re-speak from a neighbouring chunk."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402


def _wire(tmp, pos, chunks=("One.", "Two.", "Three.", "Four.")):
    core.STATE_DIR = tmp
    core.SPEECH_CHUNKS = tmp / "c"
    core.SPEECH_POS = tmp / "p"
    core.hush = lambda: None
    spoke = {}
    core.speak = lambda t, blocking=False: spoke.__setitem__("t", t)
    core.SPEECH_CHUNKS.write_text(json.dumps(list(chunks)))
    core.SPEECH_POS.write_text(str(pos))
    return spoke


def test_back_respeaks_from_previous_sentence():
    tmp = Path(tempfile.mkdtemp())
    spoke = _wire(tmp, pos=2)                 # playing "Three."
    assert core.rewind_speech(-1) == "back a sentence"
    assert spoke["t"] == "Two. Three. Four."


def test_skip_jumps_to_next_sentence():
    tmp = Path(tempfile.mkdtemp())
    spoke = _wire(tmp, pos=1)                 # playing "Two."
    core.rewind_speech(1)
    assert spoke["t"] == "Three. Four."


def test_skip_at_end_ends_the_reply():
    tmp = Path(tempfile.mkdtemp())
    spoke = _wire(tmp, pos=3)                 # last sentence
    # Skipping off the end ends the current reply (daemon then plays the next
    # queued one); it does not re-speak anything itself.
    assert core.rewind_speech(1) == "skipped to the end"
    assert "t" not in spoke


def test_back_at_start_clamps():
    tmp = Path(tempfile.mkdtemp())
    spoke = _wire(tmp, pos=0)
    core.rewind_speech(-1)
    assert spoke["t"].startswith("One.")      # clamped to first, still speaks


def test_nothing_playing_is_safe():
    tmp = Path(tempfile.mkdtemp())
    core.STATE_DIR = tmp
    core.SPEECH_CHUNKS = tmp / "nope"
    core.SPEECH_POS = tmp / "nope2"
    assert core.rewind_speech(-1) == "nothing is speaking"


if __name__ == "__main__":
    test_back_respeaks_from_previous_sentence()
    test_skip_jumps_to_next_sentence()
    test_skip_at_end_ends_the_reply()
    test_back_at_start_clamps()
    test_nothing_playing_is_safe()
    print("ok  rewind: back / skip / end-noop / start-clamp / safe-when-idle")

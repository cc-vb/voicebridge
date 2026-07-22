"""Wake-mode reliability: lenient acceptance, partial-wav wake detection.

Run: python3 tests/test_wake.py   (no pytest needed)
"""
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import talkd, stt  # noqa: E402


def test_wake_match_homophones_and_prompt_extraction():
    for text, want_addr, want_prompt in [
        ("hey Claude run the tests", True, "run the tests"),
        ("Hey Claude, run the tests", True, "run the tests"),
        ("hey cloud open the file", True, "open the file"),   # homophone
        ("hey Claude", True, ""),                             # bare wake
        ("just talking about the weather", False, ""),
    ]:
        addr, prompt = talkd.wake_match(text)
        assert addr is want_addr, (text, addr)
        assert prompt == want_prompt, (text, prompt)


def test_wake_accept_leniency():
    # A low-confidence capture (why="quiet") that IS addressed -> accept.
    assert talkd.wake_accept("hey Claude do it", "quiet", winked=False) is True
    # Already dinged mid-recording -> accept even if final text looks off.
    assert talkd.wake_accept("ay clawed uh", "quiet", winked=True) is True
    # Not addressed, not winked -> don't rescue it.
    assert talkd.wake_accept("random noise words", "quiet", winked=False) is False
    # Noise/echo are never rescued.
    assert talkd.wake_accept("hey Claude", "noise", winked=True) is False
    assert talkd.wake_accept("hey Claude", "echo", winked=True) is False


def _write_wav(path, seconds, framerate=16000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(b"\x01\x00" * int(framerate * seconds))


def test_snapshot_wav_produces_valid_transcribable_file():
    tmp = Path(tempfile.mkdtemp())
    src, dst = str(tmp / "src.wav"), str(tmp / "dst.wav")
    _write_wav(src, 1.0)
    assert talkd._snapshot_wav(src, dst) is True
    # dst must be a valid WAV of ~1s
    with wave.open(dst) as w:
        assert w.getframerate() == 16000
        assert 0.9 < w.getnframes() / 16000 < 1.1
    # too little audio -> False
    tiny = str(tmp / "tiny.wav")
    _write_wav(tiny, 0.1)
    assert talkd._snapshot_wav(tiny, dst) is False


if __name__ == "__main__":
    test_wake_match_homophones_and_prompt_extraction()
    test_wake_accept_leniency()
    test_snapshot_wav_produces_valid_transcribable_file()
    print("ok  wake: match + lenient accept + partial-wav snapshot")

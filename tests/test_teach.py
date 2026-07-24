"""The gray-line decaying tutor: intro sequence, retire-when-learned, silence.

Run: python3 tests/test_teach.py   (no pytest needed)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402
import hooks.on_prompt as op  # noqa: E402


def _fresh(tmp):
    from vb import talkd
    core.STATE_DIR = tmp
    core.REMAINDER = tmp / "speech_remainder"
    core.RATE_FILE = tmp / "rate"
    core.ENGINE_FILE = tmp / "engine"
    op.core = core
    op.TEACH_DIR = tmp / "teach"
    op.SHOWN = op.TEACH_DIR / "shown.json"
    (tmp / "talk").mkdir(parents=True, exist_ok=True)
    # learned-checks read these module globals; rebind to the tmp dir so the
    # test controls them (they're bound to the real ~/.voicebridge at import).
    talkd.STATE = tmp / "talk"
    talkd.MODE = tmp / "talk" / "mode"
    talkd.VER = tmp / "talk" / "talkd.ver"   # absent -> no update trigger


def test_intro_then_rotation_then_silence():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    # First 5 lines are the intro, in order.
    intro = [op._teach_line() for _ in range(5)]
    assert "silence me" in intro[0]
    assert "pauses me" in intro[1]
    assert intro == op._INTRO
    # Then it rotates through the pool without immediately repeating.
    nxt = [op._teach_line() for _ in range(6)]
    assert len(set(nxt)) >= 3, nxt
    # Eventually, after caps, it falls silent.
    for _ in range(40):
        op._teach_line()
    assert op._teach_line() == "", "tutor should decay to silence"


def test_learned_tips_retire():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    core.RATE_FILE.write_text("262")          # user changed speed
    (tmp / "talk" / "mode").write_text("wake")  # and used wake mode
    for _ in range(5):
        op._teach_line()                       # burn the intro
    seen = " ".join(op._teach_line() for _ in range(30))
    assert "faster" not in seen, "speed tip should retire once speed changed"
    assert "wake word mode" not in seen, "wake tip should retire once in wake"


def test_context_trigger_preempts_with_remainder():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    core.REMAINDER.write_text("the rest of the reply")
    assert "continue" in op._teach_line()      # preempts the intro


if __name__ == "__main__":
    test_intro_then_rotation_then_silence()
    test_learned_tips_retire()
    test_context_trigger_preempts_with_remainder()
    print("ok  tutor: intro -> rotation -> silence, retire-on-learned, context")

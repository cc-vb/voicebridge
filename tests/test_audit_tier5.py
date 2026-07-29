"""Audit Tier 5 edge fixes: bounded tail reads, version parse, foreign-script.

Run: python3 tests/test_audit_tier5.py   (no pytest needed)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core, talkd  # noqa: E402


def _write(lines):
    p = Path(tempfile.mkdtemp()) / "t.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return str(p)


def test_last_assistant_text_common_and_fallback():
    tp = _write([
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "the answer"}]}},
    ])
    assert core.last_assistant_text(tp) == "the answer"

    # Fallback path: the last assistant text sits before >256KB of later
    # records, so the tail window misses it and the full-file scan must find it.
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": "buried reply"}]}}]
    filler = {"type": "user", "message": {"content": "x" * 500}}
    lines += [filler] * 800   # ~400KB of trailing user records
    tp2 = _write(lines)
    assert core.last_assistant_text(tp2) == "buried reply"


def test_active_state_widens_for_huge_final_record():
    # A final record larger than the 64KB tail window must still be seen, else
    # state wrongly reads "idle" while the turn is working.
    tp = _write([
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done earlier"}]}},
        {"type": "user", "message": {"content": "y" * 80000}},   # huge, last
    ])
    assert core.active_session_state(tp) == "working"


def test_vtuple_tolerates_prerelease_and_orders():
    assert core._vtuple("2.20.10") > core._vtuple("2.20.9")
    assert core._vtuple("2.21.0-rc1") == (2, 21, 0)   # suffix no longer -> (0,)
    assert core._vtuple("2.21.0-rc1") > core._vtuple("2.20.11")


def test_foreign_script_keeps_accented_english():
    assert talkd._foreign_script("café résumé naïve") is False   # loanwords ok
    assert talkd._foreign_script("hello there") is False
    assert talkd._foreign_script("这是中文内容") is True           # real non-Latin


if __name__ == "__main__":
    test_last_assistant_text_common_and_fallback()
    test_active_state_widens_for_huge_final_record()
    test_vtuple_tolerates_prerelease_and_orders()
    test_foreign_script_keeps_accented_english()
    print("ok  tier5: tail reads, version parse, foreign-script")

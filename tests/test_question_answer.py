"""Answering an AskUserQuestion from the phone.

The safety-critical decision is `_answerable_index`: we keystroke-drive the
picker ONLY for a single single-select question with one valid pick. Everything
else must return None (-> phone says "answer on the Mac") so we never blindly
select a wrong option. Also checks the adapter drives pick_option with the index.

Run: python3 tests/test_question_answer.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import call, adapters  # noqa: E402


def _q(multi=False, nopts=3):
    return {"multiSelect": multi, "options": [{"label": f"o{i}"} for i in range(nopts)]}


def test_answerable_only_for_single_select_one_pick():
    ai = call._answerable_index
    # the one safe case: 1 single-select question, 1 valid pick
    assert ai([_q()], [[1]]) == 1
    assert ai([_q()], [[0]]) == 0
    # NOT safe -> None (finish on the Mac)
    assert ai([_q(multi=True)], [[1]]) is None          # multi-select
    assert ai([_q(), _q()], [[0], [1]]) is None         # multiple questions
    assert ai([_q()], [[0, 2]]) is None                 # more than one pick
    assert ai([_q()], [[]]) is None                     # nothing picked
    assert ai([_q(nopts=2)], [[5]]) is None             # out of range
    assert ai([], []) is None                           # no question


def test_adapter_drives_pick_option_with_the_index():
    from vb import inject
    from vb import talkd
    seen = {}
    orig = (inject.pick_option, talkd.bound_app)
    try:
        inject.pick_option = lambda index=0, expect_app="": (seen.__setitem__("idx", index), True)[1]
        talkd.bound_app = lambda: "Terminal"
        ok = adapters.ClaudeCodeAdapter().answer_question("sid", 2)
        assert ok is True
        assert seen["idx"] == 2
    finally:
        inject.pick_option, talkd.bound_app = orig


if __name__ == "__main__":
    test_answerable_only_for_single_select_one_pick()
    test_adapter_drives_pick_option_with_the_index()
    print("ok  question answer: safe gate + adapter drives the picker")

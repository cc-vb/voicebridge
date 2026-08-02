"""Summarizer layer: backend selection, short-skip, soft-fail.

The real model calls need a backend installed; these lock the orchestration
(which is what decides correctness and the never-raise contract).

Run: python3 tests/test_summarize.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import summarize  # noqa: E402


def test_short_text_is_not_summarized():
    assert summarize.summarize("done.") == ""            # under MIN_CHARS
    assert summarize.summarize("") == ""


def test_uses_selected_backend_and_passes_the_reply():
    long = "The reply. " * 60                             # over MIN_CHARS
    seen = {}
    orig_ready, orig_run = summarize._ready_engines, dict(summarize._RUN)
    try:
        summarize._ready_engines = lambda prefer="auto": ["mlx"]
        summarize._RUN["mlx"] = lambda p: seen.setdefault("prompt", p) and "" or "BRIEF."
        out = summarize.summarize(long, "mlx")
        assert out == "BRIEF."
        assert "The reply." in seen["prompt"]             # backend got the text
    finally:
        summarize._ready_engines = orig_ready
        summarize._RUN.update(orig_run)


def test_no_backend_falls_back_to_empty():
    orig = summarize._ready_engines
    try:
        summarize._ready_engines = lambda prefer="auto": []
        assert summarize.summarize("x" * 500, "auto") == ""
    finally:
        summarize._ready_engines = orig


def test_falls_through_a_broken_engine_to_the_next():
    # apple is "ready" but returns "" (e.g. Apple Intelligence off); mlx works.
    # summarize must NOT stop at apple, it must try mlx next.
    long = "The reply. " * 60
    orig_ready, orig_run = summarize._ready_engines, dict(summarize._RUN)
    try:
        summarize._ready_engines = lambda prefer="auto": ["apple", "mlx"]
        summarize._RUN["apple"] = lambda p: ""            # broken/unavailable
        summarize._RUN["mlx"] = lambda p: "MLX BRIEF."
        assert summarize.summarize(long, "auto") == "MLX BRIEF."
    finally:
        summarize._ready_engines = orig_ready
        summarize._RUN.update(orig_run)


def test_engine_available_priority_and_override():
    orig = (summarize._apple_ready, summarize._mlx_ready, summarize._ollama_ready)
    try:
        summarize._apple_ready = lambda: False
        summarize._mlx_ready = lambda: True
        summarize._ollama_ready = lambda: True
        assert summarize.engine_available("auto") == "mlx"     # apple off -> mlx
        assert summarize.engine_available("ollama") == "ollama"  # forced
        assert summarize.engine_available("apple") == ""         # forced, not ready
    finally:
        (summarize._apple_ready, summarize._mlx_ready,
         summarize._ollama_ready) = orig


def test_dropped_question_only_fires_on_a_trailing_question():
    dq = summarize._dropped_question
    # reply ENDS with a question, brief has none -> discard (speak full)
    assert dq("I did the work. Want me to proceed?", "I did the work.") is True
    # incidental '?' mid-reply but ends on a statement -> keep the brief
    assert dq("Is a > b? Then c runs. All done now.", "It runs c and finishes.") is False
    # brief preserves the question -> keep the brief
    assert dq("Should I proceed?", "Should I proceed with it?") is False


def test_mode_toggle_default_off():
    import tempfile
    orig = summarize.MODE_FLAG
    try:
        summarize.MODE_FLAG = Path(tempfile.mkdtemp()) / "m"
        assert summarize.is_on() is False        # opt-in: off by default
        summarize.set_mode(True)
        assert summarize.is_on() is True
        summarize.set_mode(False)
        assert summarize.is_on() is False
    finally:
        summarize.MODE_FLAG = orig


if __name__ == "__main__":
    test_short_text_is_not_summarized()
    test_uses_selected_backend_and_passes_the_reply()
    test_no_backend_falls_back_to_empty()
    test_falls_through_a_broken_engine_to_the_next()
    test_engine_available_priority_and_override()
    test_dropped_question_only_fires_on_a_trailing_question()
    test_mode_toggle_default_off()
    print("ok  summarize: skip-short, backend selection, soft-fail, mode toggle")

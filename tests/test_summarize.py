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
    orig_avail, orig_run = summarize.engine_available, dict(summarize._RUN)
    try:
        summarize.engine_available = lambda prefer="auto": "mlx"
        summarize._RUN["mlx"] = lambda p: seen.setdefault("prompt", p) and "" or "BRIEF."
        out = summarize.summarize(long, "mlx")
        assert out == "BRIEF."
        assert "The reply." in seen["prompt"]             # backend got the text
    finally:
        summarize.engine_available = orig_avail
        summarize._RUN.update(orig_run)


def test_no_backend_falls_back_to_empty():
    orig = summarize.engine_available
    try:
        summarize.engine_available = lambda prefer="auto": ""
        assert summarize.summarize("x" * 500, "auto") == ""
    finally:
        summarize.engine_available = orig


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
    test_engine_available_priority_and_override()
    test_mode_toggle_default_off()
    print("ok  summarize: skip-short, backend selection, soft-fail, mode toggle")

"""Cross-platform safety fixes in the OS layer (Tier 3).

Run: python3 tests/test_oslayer_safety.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import oslayer  # noqa: E402


def test_sapi_rate_coerces_safely():
    # A non-numeric rate must not become a PowerShell parse error.
    assert oslayer._sapi_rate("not-a-number") == 0
    assert isinstance(oslayer._sapi_rate("175"), int)
    assert -10 <= oslayer._sapi_rate("999999") <= 10   # clamped
    assert -10 <= oslayer._sapi_rate("1") <= 10


def test_linux_paste_refuses_without_tools():
    # Without xclip/xdotool, refuse rather than Ctrl+V a stale clipboard and
    # claim success (which would mark a lost prompt as delivered).
    orig = oslayer.which
    try:
        oslayer.which = lambda name: ""
        assert oslayer._linux_paste("hello", True) is False
    finally:
        oslayer.which = orig


if __name__ == "__main__":
    test_sapi_rate_coerces_safely()
    test_linux_paste_refuses_without_tools()
    print("ok  oslayer safety: rate coercion, paste refuses without tools")

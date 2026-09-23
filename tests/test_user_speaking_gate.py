"""The gate that stops us talking over a human.

This had two silent faults at once: it called a function that has never
existed (swallowed by a bare except, so it was permanently False), and its
phase test rejected `hearing`, the one phase that means we can hear the person
right now. A gate that fails to False is the dangerous direction: it reports an
empty room and we talk over someone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vb import core, signals


def _hud(monkeypatch, phase, level):
    monkeypatch.setattr(core, "read_hud", lambda: {"phase": phase, "level": level})


def test_hearing_means_speaking(monkeypatch):
    """`hearing` is the phase for "loud enough to have crossed the bar"."""
    _hud(monkeypatch, "hearing", 0.9)
    assert signals.user_speaking() is True


def test_quiet_start_still_counts(monkeypatch):
    """Below the hearing threshold but not silent: they have just begun."""
    _hud(monkeypatch, "listening", 0.5)
    assert signals.user_speaking() is True


def test_open_but_silent_mic_is_not_speaking(monkeypatch):
    _hud(monkeypatch, "listening", 0.0)
    assert signals.user_speaking() is False


def test_our_own_voice_is_not_theirs(monkeypatch):
    """A loud level while WE speak must never read as the user talking."""
    _hud(monkeypatch, "speaking", 0.9)
    assert signals.user_speaking() is False


def test_never_calls_a_function_that_does_not_exist():
    """The original bug: `core.get_hud` is not a thing. Assert it stays gone,
    so a future edit cannot quietly reintroduce the same swallowed call."""
    assert not hasattr(core, "get_hud")
    assert hasattr(core, "read_hud")


def test_a_broken_hud_does_not_raise(monkeypatch):
    def boom():
        raise RuntimeError("hud unreadable")
    monkeypatch.setattr(core, "read_hud", boom)
    assert signals.user_speaking() is False

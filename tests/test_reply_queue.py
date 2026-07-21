"""No reply may be silently dropped.

Found live: a weather answer was never spoken. The log had no start_speech
line for it at all, and the previous reply stayed the last thing said.

The daemon can only look for new replies between recordings, and in agent
mode the mic is open nearly all the time. It asked "is the newest reply
different from the last one I said", so a reply that arrived while the mic
was open was overwritten by the next one and never spoken. The Stop hook,
which is event-driven and would have caught it, deliberately stands down
whenever a session is voiced (core.mic_active), so nothing else covers it.

Replies are a queue, not a level to sample.
"""

import json

import pytest

from vb import core


def _transcript(tmp_path, *records):
    p = tmp_path / "session.jsonl"
    with open(p, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return str(p)


def _reply(uuid, text):
    return {"type": "assistant", "uuid": uuid,
            "message": {"content": [{"type": "text", "text": text}]}}


def _user(text="hi"):
    return {"type": "user", "uuid": "u-" + text,
            "message": {"content": [{"type": "text", "text": text}]}}


def _tool_only(uuid):
    """An assistant record with no speakable text."""
    return {"type": "assistant", "uuid": uuid,
            "message": {"content": [{"type": "tool_use", "name": "Bash",
                                     "input": {}}]}}


# ---------- the queue ---------------------------------------------------------

def test_returns_every_reply_when_nothing_is_marked_read(tmp_path):
    t = _transcript(tmp_path, _reply("a", "first"), _reply("b", "second"))
    assert [x[1] for x in core.assistant_replies_after(t)] == ["first", "second"]


def test_returns_only_what_came_after(tmp_path):
    t = _transcript(tmp_path, _reply("a", "first"), _reply("b", "second"))
    assert [x[1] for x in core.assistant_replies_after(t, "a")] == ["second"]


def test_returns_nothing_when_caught_up(tmp_path):
    t = _transcript(tmp_path, _reply("a", "first"), _reply("b", "second"))
    assert core.assistant_replies_after(t, "b") == []


def test_the_regression_a_reply_that_landed_while_listening(tmp_path):
    """Two replies arrive between two looks. Both must be spoken; the older
    one is exactly the weather answer that went missing."""
    t = _transcript(tmp_path,
                    _reply("a", "acknowledged"),
                    _reply("b", "the weather is light rain"),
                    _reply("c", "anything else?"))

    pending = core.assistant_replies_after(t, "a")

    assert [x[1] for x in pending] == ["the weather is light rain",
                                       "anything else?"]


def test_order_is_oldest_first(tmp_path):
    t = _transcript(tmp_path, *[_reply(str(i), f"r{i}") for i in range(5)])
    assert [x[1] for x in core.assistant_replies_after(t)] == [
        "r0", "r1", "r2", "r3", "r4"]


def test_skips_tool_only_records(tmp_path):
    """A record that is only a tool call has nothing to say."""
    t = _transcript(tmp_path, _reply("a", "first"), _tool_only("b"),
                    _reply("c", "second"))
    assert [x[1] for x in core.assistant_replies_after(t, "a")] == ["second"]


def test_ignores_user_records(tmp_path):
    t = _transcript(tmp_path, _user("question"), _reply("a", "answer"))
    assert [x[1] for x in core.assistant_replies_after(t)] == ["answer"]


def test_survives_a_corrupt_line(tmp_path):
    p = tmp_path / "session.jsonl"
    p.write_text(json.dumps(_reply("a", "first")) + "\n"
                 + "{not json\n"
                 + json.dumps(_reply("b", "second")) + "\n")
    assert [x[1] for x in core.assistant_replies_after(str(p), "a")] == ["second"]


# ---------- the transcript moving under us ------------------------------------

def test_unknown_marker_speaks_only_the_newest(tmp_path):
    """Compaction replaces the transcript, so the backlog is unknowable. Say
    the newest reply rather than re-speaking the whole session."""
    t = _transcript(tmp_path, _reply("x", "first"), _reply("y", "second"))
    assert [r[1] for r in core.assistant_replies_after(t, "gone")] == ["second"]


def test_missing_transcript_is_empty(tmp_path):
    assert core.assistant_replies_after(str(tmp_path / "nope.jsonl")) == []


def test_empty_transcript_is_empty(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert core.assistant_replies_after(str(p)) == []


# ---------- joining a session -------------------------------------------------

def test_latest_uuid_marks_history_read(tmp_path):
    """/voice-on must not read the session's backlog aloud."""
    t = _transcript(tmp_path, _reply("a", "old"), _reply("b", "newest"))

    joined = core.latest_assistant_uuid(t)

    assert joined == "b"
    assert core.assistant_replies_after(t, joined) == []


def test_latest_uuid_of_an_empty_session_is_blank(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert core.latest_assistant_uuid(str(p)) == ""


# ---------- the phone QR must actually scan ----------------------------------

def test_qr_has_the_quiet_zone_a_scanner_needs(capsys):
    """border=1 shipped for a long time: below the 4-module quiet zone the
    spec requires, so cameras could not find the code's edges."""
    import qrcode
    captured = {}
    real = qrcode.QRCode

    def spy(**kw):
        captured.update(kw)
        return real(**kw)

    qrcode.QRCode = spy
    try:
        assert core.print_qr("https://example.trycloudflare.com/?k=vb-1") is True
    finally:
        qrcode.QRCode = real
    assert captured["border"] >= 4


def test_qr_uses_low_error_correction_for_bigger_modules(capsys):
    """A long tunnel URL at correction M needs more modules, and each one is
    drawn smaller in a fixed-width terminal. The print is lossless; the
    failure mode is a camera that cannot resolve tiny modules."""
    import qrcode
    captured = {}
    real = qrcode.QRCode

    def spy(**kw):
        captured.update(kw)
        return real(**kw)

    qrcode.QRCode = spy
    try:
        core.print_qr("https://example.trycloudflare.com/?k=vb-1")
    finally:
        qrcode.QRCode = real
    assert captured["error_correction"] == qrcode.constants.ERROR_CORRECT_L


def test_qr_is_printed(capsys):
    core.print_qr("https://example.trycloudflare.com/?k=vb-1")
    out = capsys.readouterr().out
    assert "█" in out
    assert max(len(l) for l in out.splitlines()) < 80   # fits a terminal


def test_qr_reports_failure_instead_of_raising(monkeypatch, capsys):
    """No qrcode installed must print the install hint, not a traceback."""
    import builtins
    real_import = builtins.__import__

    def no_qrcode(name, *a, **k):
        if name == "qrcode":
            raise ImportError("nope")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_qrcode)
    assert core.print_qr("https://example.com") is False

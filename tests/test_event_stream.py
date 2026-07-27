"""The event stream has to stay open, because everything rides on it.

/events is the phone's primary channel: replies, delivery acks, permission
moments, session switches and readiness all arrive on it, and the design is
ONE long-lived connection rather than request-by-request polling.

Found while adding readiness: the loop's subscriber queue was named `q`, and
the pending-question check later rebound `q` to a dict. One tick later
`q.get(timeout=1.0)` was a dict.get() with a keyword argument, which raised
and ended the loop, and the cleanup then called _SUBS.discard() on an
unhashable dict so the real queue was never unregistered.

It hid because EventSource reconnects on its own. The channel still appeared
to work while actually being a one-second reconnect loop that leaked a
subscriber every time round, and any event emitted only on CHANGE (readiness
is one) could never fire, because the loop never reached a second tick.

Run: python3 tests/test_event_stream.py   (no pytest needed)
"""
import json
import select
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import call, core  # noqa: E402

SECRET = "vb-testsecret-not-a-real-one"
_PORT = None


def _relay():
    """One isolated relay for the module. STATE_DIR is redirected at a temp
    directory FIRST: the secret is written to disk, and a test has no business
    touching the real ~/.voicebridge of whoever runs it."""
    global _PORT
    if _PORT:
        return _PORT
    tmp = Path(tempfile.mkdtemp(prefix="vb-events-"))
    core.STATE_DIR = tmp
    call.PID = tmp / "call.pid"
    call.EPOCH = tmp / "call_epoch"
    call._write_secret(SECRET)
    call.DRYRUN = True

    # A real file on disk: the loop stats it and parses it every tick.
    tp = tmp / "transcript.jsonl"
    tp.write_text("")
    call._target_transcript = lambda: str(tp)

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    _PORT = s.getsockname()[1]
    s.close()
    call.PORT = _PORT
    threading.Thread(target=call.run_daemon, daemon=True).start()
    for _ in range(40):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{_PORT}/health", timeout=0.5) as r:
                if r.read() == b"ok":
                    break
        except Exception:
            time.sleep(0.1)
    return _PORT


def _open_stream():
    """A hand-rolled GET rather than urlopen.

    urlopen hands back a buffered reader whose readline() blocks until the
    next frame, so a "read for 3 seconds" actually waited out the keepalive
    and every test cost ten. Speaking HTTP to the socket keeps the deadline
    honest and leaves nothing buffered where the test cannot see it."""
    port = _relay()
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET /events?k={SECRET} HTTP/1.1\r\n"
              f"Host: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n".encode())
    return s


def _read_for(s, seconds):
    """Frames arriving within the deadline, and whether the server hung up.
    An empty read and a quiet stream mean opposite things here, so they are
    reported separately rather than both as "nothing"."""
    buf, closed, t0 = b"", False, time.time()
    while True:
        left = seconds - (time.time() - t0)
        if left <= 0:
            break
        if not select.select([s], [], [], min(left, 0.25))[0]:
            continue
        try:
            chunk = s.recv(8192)
        except OSError:
            closed = True
            break
        if not chunk:
            closed = True
            break
        buf += chunk
    body = buf.split(b"\r\n\r\n", 1)[-1] if b"\r\n\r\n" in buf else buf
    frames = [ln.strip().decode(errors="replace")
              for ln in body.split(b"\n") if ln.strip()]
    return frames, closed


def _events(frames):
    return [f.split("event: ", 1)[1] for f in frames if f.startswith("event: ")]


# ---------- the regression ---------------------------------------------------

def test_stream_survives_well_past_the_first_tick():
    """The bug closed it at almost exactly 1.0s, one tick in."""
    st = _open_stream()
    try:
        _, closed = _read_for(st, 3.5)
    finally:
        st.close()
    assert not closed, "stream closed early: the loop died after a tick"


def test_subscriber_is_unregistered_when_the_client_leaves():
    """The dict rebinding also broke cleanup, so every reconnect left a dead
    queue in _SUBS forever and _broadcast kept writing into all of them.

    Tracks the QUEUE OBJECT this stream registers rather than counting _SUBS:
    a stream closed by an earlier test only unregisters when its loop next
    tries to write, which can be a ping away, so counts drift underneath a
    test that has nothing to do with them."""
    before = set(call._SUBS)
    st = _open_stream()
    _read_for(st, 1.5)
    mine = set(call._SUBS) - before
    assert len(mine) == 1, f"expected one new subscriber, got {len(mine)}"
    q = mine.pop()

    st.close()
    # Nudge the loop into a write: cleanup happens when the socket fails, and
    # otherwise the next write is up to a 10s keepalive away.
    for _ in range(60):
        if q not in call._SUBS:
            break
        call._broadcast({"type": "probe"})
        time.sleep(0.1)
    assert q not in call._SUBS, "subscriber leaked after disconnect"


def test_a_pending_question_does_not_clobber_the_subscriber_queue():
    """The exact trigger: an open AskUserQuestion put a dict where the queue
    was. The question must be delivered AND the stream must outlive it."""
    saved = core.pending_question
    core.pending_question = lambda tp: {
        "id": "q1", "question": "Which one?", "options": ["a", "b"]}
    try:
        st = _open_stream()
        try:
            frames, closed = _read_for(st, 3.5)
        finally:
            st.close()
    finally:
        core.pending_question = saved
    assert "question" in _events(frames), "the question never arrived"
    assert not closed, "the question killed the stream"


# ---------- what the stream promises -----------------------------------------

def test_readiness_is_pushed_up_front_not_only_on_change():
    """A phone connecting to an already-blocked Mac must see the banner
    without waiting for a flip that may never come."""
    st = _open_stream()
    try:
        frames, _ = _read_for(st, 2.0)
    finally:
        st.close()
    evs = _events(frames)
    assert evs[:2] == ["hello", "ready"], f"opening frames were {evs[:3]}"
    payload = json.loads(frames[frames.index("event: ready") + 1]
                         .split("data: ", 1)[1])
    assert "ok" in payload and "reason" in payload


def test_keepalive_ping_arrives_so_proxies_do_not_idle_it_out():
    """Shortened from the real ten ticks: this asserts that the keepalive
    FIRES on its cadence, and waiting out the shipped 10s to learn that would
    cost the suite ten seconds on every run."""
    saved = call.SSE_PING_TICKS
    call.SSE_PING_TICKS = 2
    try:
        st = _open_stream()          # opened AFTER the patch: the loop reads
        try:                         # the module global on each tick
            frames, closed = _read_for(st, 4.0)
        finally:
            st.close()
    finally:
        call.SSE_PING_TICKS = saved
    assert not closed
    assert any(f.startswith(": ping") for f in frames), "no keepalive fired"


def test_stream_requires_the_secret():
    port = _relay()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/events?k=wrong",
                               timeout=3)
    except urllib.error.HTTPError as e:
        assert e.code == 401
    else:
        raise AssertionError("the event stream served an unauthenticated client")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall event stream tests passed")

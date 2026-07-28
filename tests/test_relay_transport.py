"""Relay transport perf: gzip text bodies + HTTP/1.1 keep-alive.

Over a tunnel the ~280KB page was sent raw every load (Tailscale Funnel does
not compress like Cloudflare's edge did) and every spoken reply paid a fresh
TCP+TLS handshake. gzip cuts the page ~3x; keep-alive reuses the socket so all
but the first reply skip the handshake. These lock that in.

Run: python3 tests/test_relay_transport.py   (no pytest needed)
"""
import gzip
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import call  # noqa: E402


def _handler(accept_encoding=""):
    """A Handler instance with the socket machinery stubbed so _reply can run
    without a real connection."""
    h = object.__new__(call.Handler)
    h.headers = {"Accept-Encoding": accept_encoding}   # dict.get mimics Message
    h.close_connection = False
    h.wfile = io.BytesIO()
    h.sent = {}
    h.code = None
    h.send_response = lambda c: setattr(h, "code", c)
    h.send_header = lambda k, v: h.sent.__setitem__(k, v)
    h.end_headers = lambda: None
    return h


def test_keepalive_is_http11():
    # HTTP/1.1 is what enables socket reuse across requests.
    assert call.Handler.protocol_version == "HTTP/1.1"


def test_gzip_compresses_large_text_when_accepted():
    h = _handler("gzip, deflate")
    body = b"<html>" + b"x" * 5000 + b"</html>"
    call.Handler._reply(h, 200, body, "text/html; charset=utf-8")
    assert h.sent.get("Content-Encoding") == "gzip"
    assert h.sent.get("Vary") == "Accept-Encoding"
    wire = h.wfile.getvalue()
    assert len(wire) < len(body)                    # actually smaller
    assert gzip.decompress(wire) == body            # and lossless
    assert h.sent["Content-Length"] == str(len(wire))
    assert h.close_connection is False              # 2xx stays keep-alive


def test_no_gzip_when_client_does_not_accept():
    h = _handler("")                                # no Accept-Encoding
    body = b"<html>" + b"x" * 5000 + b"</html>"
    call.Handler._reply(h, 200, body, "text/html; charset=utf-8")
    assert "Content-Encoding" not in h.sent
    assert h.wfile.getvalue() == body


def test_small_bodies_not_gzipped():
    h = _handler("gzip")
    body = b"{}"                                    # under the 1KB threshold
    call.Handler._reply(h, 200, body, "application/json")
    assert "Content-Encoding" not in h.sent
    assert h.wfile.getvalue() == body


def test_binary_audio_never_gzipped():
    h = _handler("gzip")
    body = b"OggS" + b"\x00" * 5000                 # opus/ogg, already compact
    call.Handler._reply(h, 200, body, "audio/ogg")
    assert "Content-Encoding" not in h.sent
    assert h.wfile.getvalue() == body


def test_error_responses_close_the_socket():
    # A half-read request body on a kept-alive socket would desync the next
    # request; error paths (which may not drain the body) must close.
    h = _handler("gzip")
    call.Handler._reply(h, 401, b"unauthorized", "text/plain")
    assert h.close_connection is True


if __name__ == "__main__":
    test_keepalive_is_http11()
    test_gzip_compresses_large_text_when_accepted()
    test_no_gzip_when_client_does_not_accept()
    test_small_bodies_not_gzipped()
    test_binary_audio_never_gzipped()
    test_error_responses_close_the_socket()
    print("ok  relay transport: gzip text, keep-alive, error-close")

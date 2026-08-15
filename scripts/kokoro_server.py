#!/usr/bin/env python3
"""Kokoro TTS server: loads the model once, synthesizes over local HTTP.

Runs inside the voicebridge Kokoro venv (see `vb tts on`). Endpoints:
  GET  /health          -> ok
  POST /synth  {"text": ..., "voice": "af_heart", "speed": 1.0} -> WAV bytes

Local-only (127.0.0.1). Model: ~/.voicebridge/models/kokoro-v1.0.onnx
"""

import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import soundfile as sf
from kokoro_onnx import Kokoro

HOME = os.path.expanduser("~")
MODEL = os.path.join(HOME, ".voicebridge", "models", "kokoro-v1.0.onnx")
VOICES = os.path.join(HOME, ".voicebridge", "models", "voices-v1.0.bin")
PORT = int(os.environ.get("VB_TTS_PORT", "8798"))

def _load():
    """Kokoro on the fastest backend this Mac actually has.

    kokoro_onnx defaults to CPU, which was costing about a third of every
    synthesis: measured here at 1.04s warm on CPU against 0.68s on CoreML for
    the same sentence, and time-to-first-audio is the whole felt latency of a
    spoken reply. CoreML takes longer to load (3.3s against 0.8s) but that
    happens once, in the background, at startup.

    It falls back rather than failing: CoreML claims only about a thousand of
    the model's 2476 nodes, and it has been reported broken on some macOS
    versions. A slower voice is much better than no voice.
    """
    want = os.environ.get("ONNX_PROVIDER", "")
    if not want:
        try:
            import onnxruntime as rt
            if "CoreMLExecutionProvider" in rt.get_available_providers():
                want = "CoreMLExecutionProvider"
        except Exception:
            want = ""
    if want:
        os.environ["ONNX_PROVIDER"] = want
        try:
            return Kokoro(MODEL, VOICES)
        except Exception as e:
            print(f"kokoro: {want} failed ({str(e)[:80]}), using CPU",
                  flush=True)
            os.environ["ONNX_PROVIDER"] = "CPUExecutionProvider"
    return Kokoro(MODEL, VOICES)


kokoro = _load()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _out(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._out(200, b"ok", "text/plain")
        elif self.path == "/voices":
            self._out(200, json.dumps(sorted(kokoro.get_voices())).encode(),
                      "application/json")
        else:
            self._out(404, b"nope", "text/plain")

    def do_POST(self):
        if self.path != "/synth":
            self._out(404, b"nope", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n))
            text = (req.get("text") or "").strip()
            voice = req.get("voice") or "af_heart"
            speed = float(req.get("speed") or 1.0)
            if not text:
                raise ValueError("empty text")
            samples, sr = kokoro.create(text, voice=voice, speed=speed)
            buf = io.BytesIO()
            sf.write(buf, samples, sr, format="WAV")
            self._out(200, buf.getvalue(), "audio/wav")
        except Exception as e:
            self._out(500, str(e).encode(), "text/plain")


print(f"kokoro server on 127.0.0.1:{PORT}", flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

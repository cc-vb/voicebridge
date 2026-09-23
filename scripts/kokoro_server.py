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
# The default must MATCH what vb/core.py computes, or the server comes up on a
# port nobody looks at. It did: core.py uses 6000 + uid % 1000 while this
# defaulted to a fixed 8798, so a manually started server was invisible to the
# product and sat there answering nobody.
PORT = int(os.environ.get("VB_TTS_PORT") or (6000 + os.getuid() % 1000))

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
    # CoreML is NOT preferred, deliberately.
    #
    # The old code chose it when available and fell back only if CONSTRUCTION
    # raised. But CoreML on macOS 26 fails at SYNTHESIS, not at load: the
    # session builds fine and then every single request dies with "Error in
    # dynamically resizing for sequence length". So the fallback never fired,
    # the server stayed up answering 500s, and voicebridge quietly spoke
    # through `say` instead. It ran that way for weeks and nothing said so.
    #
    # It was never worth much anyway: measured at ~0.36s saved per synthesis
    # against ~2.5s of extra startup.
    want = os.environ.get("ONNX_PROVIDER", "") or "CPUExecutionProvider"
    os.environ["ONNX_PROVIDER"] = want
    k = Kokoro(MODEL, VOICES)

    # Prove it can actually speak before claiming to be up. A provider that
    # loads and cannot synthesise is the failure this whole comment is about.
    try:
        k.create("ok", voice="af_heart", speed=1.0, lang="en-us")
    except Exception as e:
        print(f"kokoro: {want} loads but cannot synthesise ({str(e)[:90]}); "
              f"retrying on CPU", flush=True)
        os.environ["ONNX_PROVIDER"] = "CPUExecutionProvider"
        k = Kokoro(MODEL, VOICES)
        k.create("ok", voice="af_heart", speed=1.0, lang="en-us")
    return k


import re as _re

# espeak-ng marks a mid-sentence language switch inline, like "(en)word(hi)".
# Every character of those markers is in Kokoro's vocabulary, so they are
# tokenised and SPOKEN: the voice literally says "in" and "hee" the moment any
# non-English text arrives. The PyTorch kokoro package builds its backend with
# language_switch="remove-flags"; the ONNX path we use does not, so we strip
# them here.
_LANG_FLAG = _re.compile(r"\((?:en|hi|enus|engb|es|fr|it|pt|ja|zh)\)")


def _strip_lang_flags(text: str) -> str:
    return _LANG_FLAG.sub("", text or "")


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

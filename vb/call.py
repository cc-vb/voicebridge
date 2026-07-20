"""voicebridge call: a real phone call with your live session. No Channels.

The true hands-free mobile mode: dial a phone number, talk, hear replies,
no taps, works while driving. Vapi (voice-AI platform) handles the call,
speech-to-text, and natural neural voices; this relay is the "custom LLM"
it talks to, and it bridges each turn into your live Claude session:

  phone --call--> Vapi --STT--> POST /chat/completions --> this relay
                                                            | inject into
                                                            | focused session
       <--speak-- Vapi <--TTS--  reply text  <-- transcript watch

Channels-free: injection is local (same path as /voice-on), so it works on
org accounts where Claude Channels is blocked.

Needs (one-time, yours): a Vapi account + phone number (paid per minute),
and a tunnel to expose the relay (e.g. `ngrok http 8790`). Point the Vapi
assistant's Custom LLM URL at https://<tunnel>/chat/completions and set a
shared secret. See mobile/vapi/VAPI.md.

Control: vb call on | off | status     Env: VB_CALL_PORT, VB_CALL_SECRET,
VB_CALL_TIMEOUT (seconds per turn), VB_CALL_DRYRUN (test without injecting).
"""

import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core, inject
from .talkd import ACTIVE, _read_json

PID = core.STATE_DIR / "call.pid"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
if not os.path.exists(FFMPEG):
    FFMPEG = "/usr/local/bin/ffmpeg"
PORT = int(os.environ.get("VB_CALL_PORT", "8790"))
SECRET = os.environ.get("VB_CALL_SECRET", "")
TIMEOUT = float(os.environ.get("VB_CALL_TIMEOUT", "90"))
DRYRUN = bool(os.environ.get("VB_CALL_DRYRUN"))


def _target_transcript() -> str:
    active = _read_json(ACTIVE)
    if active and os.path.exists(active.get("transcript_path", "")):
        return active["transcript_path"]
    return core.newest_transcript()


def _extract_user_text(body: dict) -> str:
    """Last user message; content may be a string or a list of parts."""
    msgs = body.get("messages") or []
    for m in reversed(msgs):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):
            parts = [p.get("text", "") for p in c
                     if isinstance(p, dict) and p.get("type") == "text"]
            return " ".join(parts).strip()
    return ""


def _ask_session(text: str) -> str:
    """Inject a turn into the live session and wait for the reply."""
    if DRYRUN:
        return f"dry run reply to: {text}"
    tp = _target_transcript()
    if not tp:
        return "I can't find an open session on the Mac."
    prev = core.last_assistant_text(tp)
    core.log(f"call you: {text}")
    inject.paste_text(text, send=True)
    t0 = time.time()
    while time.time() - t0 < TIMEOUT:
        time.sleep(1.0)
        cur = core.last_assistant_text(tp)
        if cur and cur != prev:
            # Phone answers should be speech-shaped: no markdown/code.
            return core.clean_for_speech(cur, max_chars=2500)
    return "Still working on that. Ask me again in a moment."


def _openai_json(text: str) -> bytes:
    return json.dumps({
        "id": f"chatcmpl-vb{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "voicebridge-live-session",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
    }).encode()


def _sse(text: str) -> bytes:
    chunk = {
        "id": f"chatcmpl-vb{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "voicebridge-live-session",
        "choices": [{"index": 0, "delta": {"role": "assistant",
                                           "content": text},
                     "finish_reason": None}],
    }
    done = dict(chunk)
    done["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    return (f"data: {json.dumps(chunk)}\n\n"
            f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n").encode()


PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.svg">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0e1116">
<title>voicebridge call</title>
<style>
 body{font-family:-apple-system,system-ui,sans-serif;background:#0e1116;color:#e6e9ef;
      margin:0;padding:24px;display:flex;flex-direction:column;min-height:92vh}
 h1{font-size:20px;margin:0 0 4px} .sub{color:#8b93a3;font-size:13px;margin-bottom:20px}
 #btn{font-size:22px;padding:22px;border-radius:16px;border:0;background:#2f6df6;
      color:#fff;width:100%;font-weight:600}
 #btn.live{background:#d64545}
 #state{margin:18px 0;font-size:16px;color:#9fd39f;min-height:22px}
 #log{flex:1;overflow-y:auto;font-size:14px;line-height:1.5}
 .you{color:#8ab4ff}.bot{color:#e6e9ef;margin-bottom:10px}
</style></head><body>
<h1>voicebridge</h1>
<div class="sub">hands-free call with your Claude session</div>
<button id="btn">Start call</button>
<div id="state"></div><div id="log"></div>
<script>
const K = new URLSearchParams(location.search).get('k') || '';
const btn=document.getElementById('btn'), state=document.getElementById('state'),
      log=document.getElementById('log');
let live=false, rec=null, media=null, audioCtx=null;

function say(text, cb){
  const u=new SpeechSynthesisUtterance(text);
  const vs=speechSynthesis.getVoices().filter(v=>v.lang.startsWith('en'));
  const best=vs.find(v=>/siri|premium|enhanced|natural/i.test(v.name))||vs[0];
  if(best)u.voice=best; u.rate=1.0; u.onend=()=>cb&&cb();
  speechSynthesis.speak(u);
}
function add(cls,t){const d=document.createElement('div');d.className=cls;
  d.textContent=(cls==='you'?'you: ':'')+t;log.prepend(d);}
async function ask(text){
  add('you',text); state.textContent='thinking...';
  try{
    const r=await fetch('/ask?k='+encodeURIComponent(K),{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    const j=await r.json(); add('bot',j.reply);
    state.textContent='speaking...';
    say(j.reply,()=>{ if(live) setTimeout(listen,350); });
  }catch(e){ state.textContent='connection error'; if(live) setTimeout(listen,1500); }
}
// Path A: native speech recognition (Chrome/Android, iOS Safari 14.5+)
const SR = window.SpeechRecognition||window.webkitSpeechRecognition;
function listenSR(){
  rec=new SR(); rec.lang='en-US'; rec.interimResults=false; rec.maxAlternatives=1;
  state.textContent='listening... (speak)';
  rec.onresult=e=>{const t=e.results[0][0].transcript.trim();
    if(/^(stop listening|end call|hang up)[.!]?$/i.test(t)){stop();return;}
    if(t)ask(t); else if(live)listen();};
  rec.onerror=()=>{ if(live) setTimeout(listen,800); };
  rec.onend=()=>{ if(live && state.textContent.startsWith('listening')) setTimeout(listen,400); };
  rec.start();
}
// Path B: record with silence detection, server-side whisper
async function listenWhisper(){
  state.textContent='listening... (speak)';
  const stream=media||(media=await navigator.mediaDevices.getUserMedia({audio:true}));
  audioCtx=audioCtx||new (window.AudioContext||window.webkitAudioContext)();
  const src=audioCtx.createMediaStreamSource(stream), an=audioCtx.createAnalyser();
  an.fftSize=2048; src.connect(an);
  const buf=new Float32Array(an.fftSize);
  const mr=new MediaRecorder(stream); const chunks=[];
  mr.ondataavailable=e=>chunks.push(e.data);
  let spoke=false, quiet=0;
  const iv=setInterval(()=>{
    an.getFloatTimeDomainData(buf);
    let s=0; for(const v of buf) s+=v*v; const rms=Math.sqrt(s/buf.length);
    if(rms>0.02){spoke=true;quiet=0;} else if(spoke){quiet+=1;}
    if((spoke&&quiet>=8)||(!spoke&&iv._n++>150)){clearInterval(iv);mr.stop();}
  },250); iv._n=0;
  mr.onstop=async()=>{
    src.disconnect();
    if(!spoke){ if(live)listen(); return; }
    state.textContent='transcribing...';
    const blob=new Blob(chunks,{type:mr.mimeType||'audio/webm'});
    try{
      const r=await fetch('/stt?k='+encodeURIComponent(K),{method:'POST',body:blob});
      const j=await r.json();
      const t=(j.text||'').trim();
      if(/^(stop listening|end call|hang up)[.!]?$/i.test(t)){stop();return;}
      if(t)ask(t); else if(live)listen();
    }catch(e){ if(live) setTimeout(listen,1500); }
  };
  mr.start();
}
function listen(){ if(!live)return; (SR?listenSR:listenWhisper)(); }
function stop(){ live=false; try{rec&&rec.stop()}catch(e){}
  speechSynthesis.cancel(); btn.textContent='Start call'; btn.classList.remove('live');
  state.textContent='call ended'; }
btn.onclick=()=>{
  if(live){stop();return;}
  live=true; btn.textContent='End call'; btn.classList.add('live');
  speechSynthesis.getVoices();
  say('Connected. Talk to me.',()=>listen());
};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the daemon quiet
        core.log("call http: " + fmt % args)

    def _reply(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not SECRET:
            return True
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        if q.get("k", [""])[0] == SECRET:
            return True
        got = (self.headers.get("x-vapi-secret", "")
               or self.headers.get("Authorization", "")
               .removeprefix("Bearer ").strip())
        return got == SECRET

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._reply(200, b"ok", "text/plain")
        elif path == "/manifest.json":   # PWA install (public, harmless)
            self._reply(200, json.dumps({
                "name": "voicebridge",
                "short_name": "voicebridge",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#0e1116",
                "theme_color": "#0e1116",
                "icons": [{"src": "/icon.svg", "sizes": "any",
                           "type": "image/svg+xml"}],
            }).encode(), "application/json")
        elif path == "/icon.svg":
            self._reply(200, (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
                '<rect width="100" height="100" rx="22" fill="#2f6df6"/>'
                '<rect x="42" y="18" width="16" height="40" rx="8" fill="#fff"/>'
                '<path d="M30 52a20 20 0 0 0 40 0" stroke="#fff" stroke-width="7"'
                ' fill="none" stroke-linecap="round"/>'
                '<rect x="47" y="72" width="6" height="12" fill="#fff"/>'
                '</svg>').encode(), "image/svg+xml")
        elif path == "/":
            if not self._authed():
                self._reply(401, b"add ?k=<secret> to the URL", "text/plain")
                return
            self._reply(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._reply(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if not self._authed():
            self._reply(401, b"unauthorized", "text/plain")
            return
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b""

        if path == "/ask":  # web-call page: {"text": ...} -> {"reply": ...}
            try:
                text = (json.loads(raw or b"{}").get("text") or "").strip()
            except Exception:
                self._reply(400, b"bad request", "text/plain")
                return
            answer = (_ask_session(text) if text
                      else "Sorry, I didn't catch that.")
            self._reply(200, json.dumps({"reply": answer}).encode(),
                        "application/json")
            return

        if path == "/stt":  # web-call fallback: audio blob -> {"text": ...}
            from . import stt as _stt
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".webm",
                                             delete=False) as f:
                f.write(raw)
                src = f.name
            wav = src + ".wav"
            text = ""
            try:
                r = subprocess.run([FFMPEG, "-y", "-i", src, "-ar", "16000",
                                    "-ac", "1", wav],
                                   capture_output=True, timeout=120)
                if r.returncode == 0:
                    text = _stt.transcribe(wav)
            finally:
                for p in (src, wav):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            self._reply(200, json.dumps({"text": text}).encode(),
                        "application/json")
            return

        if path.endswith("chat/completions"):  # Vapi custom-LLM endpoint
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                self._reply(400, b"bad request", "text/plain")
                return
            text = _extract_user_text(body)
            answer = (_ask_session(text) if text
                      else "Sorry, I didn't catch that.")
            if body.get("stream"):
                self._reply(200, _sse(answer), "text/event-stream")
            else:
                self._reply(200, _openai_json(answer), "application/json")
            return

        self._reply(404, b"not found", "text/plain")


def run_daemon() -> int:
    core.log(f"call: relay listening on 127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


def _alive() -> bool:
    try:
        os.kill(int(PID.read_text().strip()), 0)
        return True
    except Exception:
        return False


def on() -> str:
    if _alive():
        return "call relay already running"
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET:   # persist so `vb call tunnel` builds the correct ?k= link
        (core.STATE_DIR / "call_secret").write_text(SECRET)
    vb = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin", "vb")
    env = dict(os.environ)
    p = subprocess.Popen([sys.executable, vb, "call", "__run__"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    PID.write_text(str(p.pid))
    return (f"call relay ON (pid {p.pid}, port {PORT}). Tunnel it "
            f"(`ngrok http {PORT}`) and point your Vapi assistant's Custom "
            f"LLM at https://<tunnel>/chat/completions. See mobile/vapi/VAPI.md.")


def off() -> str:
    try:
        os.kill(int(PID.read_text().strip()), 15)
    except Exception:
        pass
    try:
        PID.unlink()
    except FileNotFoundError:
        pass
    return "call relay OFF"


def status() -> str:
    return "\n".join([
        f"relay  : {'running' if _alive() else 'stopped'} (port {PORT})",
        f"secret : {'set' if SECRET else '(none - set VB_CALL_SECRET!)'}",
        f"target : {_target_transcript() or '(no session)'}",
    ])

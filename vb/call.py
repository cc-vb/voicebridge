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
EPOCH = core.STATE_DIR / "call_epoch"   # bumped by /switch: aborts stale turns
FFMPEG = "/opt/homebrew/bin/ffmpeg"
if not os.path.exists(FFMPEG):
    FFMPEG = "/usr/local/bin/ffmpeg"
PORT = int(os.environ.get("VB_CALL_PORT", "8790"))
TIMEOUT = float(os.environ.get("VB_CALL_TIMEOUT", "90"))
DRYRUN = bool(os.environ.get("VB_CALL_DRYRUN"))


def _secret() -> str:
    """The shared secret, re-read per use, FILE first, env as fallback.
    The daemon's env is frozen at spawn, so file-first is what makes live
    rotation work: `vb phone` persists the (possibly new) secret and a relay
    that's already running accepts the new link immediately, no restart, no
    surprise 401s. on() always persists an env-provided secret to the file."""
    try:
        s = (core.STATE_DIR / "call_secret").read_text().strip()
        if s:
            return s
    except Exception:
        pass
    return os.environ.get("VB_CALL_SECRET", "")


SECRET = _secret()   # kept for status(); auth checks call _secret() live


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


def _epoch() -> str:
    try:
        return EPOCH.read_text().strip()
    except Exception:
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
    # Guard the paste: it lands in the FRONTMOST Mac app, so if the bound
    # terminal isn't focused (or the screen is locked) refuse loudly instead
    # of typing into Slack / waiting 90s for a reply that can never come.
    from .talkd import bound_app
    if not inject.paste_text(text, send=True, expect_app=bound_app()):
        return ("I couldn't type into the session, the terminal isn't the "
                "focused window on your Mac. Bring it to the front, or check "
                "the screen isn't locked, then try again.")
    ep = _epoch()
    t0 = time.time()
    while time.time() - t0 < TIMEOUT:
        time.sleep(1.0)
        if _epoch() != ep:
            return "Okay, switched. Go ahead."   # /switch ended this turn
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

function pickVoice(){
  const vs=speechSynthesis.getVoices().filter(v=>v.lang.startsWith('en'));
  return vs.find(v=>/siri|premium|enhanced|natural/i.test(v.name))||vs[0];
}
// Chrome/Safari silently cut off long utterances (a known bug), which made
// it stop after the first sentence. Split into sentence-sized chunks and
// chain them via onend so the WHOLE reply is spoken.
function say(text, cb){
  speechSynthesis.cancel();
  const best=pickVoice();
  const parts=(text.match(/[^.!?]+[.!?]*\s*/g)||[text])
    .reduce((a,s)=>{ // keep chunks <=160 chars
      if(a.length&&(a[a.length-1]+s).length<=160)a[a.length-1]+=s; else a.push(s);
      return a;},[]);
  let i=0;
  (function next(){
    if(i>=parts.length){cb&&cb();return;}
    const u=new SpeechSynthesisUtterance(parts[i++].trim());
    if(best)u.voice=best; u.rate=1.0;
    u.onend=next; u.onerror=next;
    speechSynthesis.speak(u);
  })();
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

# Shown on 401 for "/": the installed PWA loses ?k= (a manifest start_url
# must not carry the secret), so this page auto-recovers from localStorage,
# or asks once and remembers. Dark and calm, not a bare error string.
AUTH_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>voicebridge</title><style>
body{margin:0;background:#0e1116;color:#e5e7eb;font:16px -apple-system,system-ui;
display:flex;flex-direction:column;align-items:center;justify-content:center;
height:100vh;gap:14px;padding:24px;box-sizing:border-box;text-align:center}
input{background:#1a2029;color:#e5e7eb;border:1px solid #2d3644;border-radius:12px;
padding:14px;font-size:17px;width:min(320px,80vw);text-align:center}
button{background:#2f6df6;color:#fff;border:0;border-radius:12px;padding:14px 28px;
font-size:17px}
</style></head><body>
<div style="font-size:40px">&#127897;</div>
<div>Enter the key from <b>vb phone</b> on your Mac<br>
<span style="color:#8b95a5;font-size:14px">(the part after ?k= in the link)</span></div>
<input id="k" placeholder="vb-xxxxxxxx" autocapitalize="none" autocorrect="off">
<button onclick="go()">Connect</button>
<script>
var qs=new URLSearchParams(location.search), s=localStorage.getItem('vbk');
if(qs.get('k')){localStorage.removeItem('vbk');}     /* that key was wrong */
else if(s){location.href='/?k='+encodeURIComponent(s);}
function go(){var v=document.getElementById('k').value.trim();
if(v){localStorage.setItem('vbk',v);location.href='/?k='+encodeURIComponent(v);}}
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
        secret = _secret()   # live: a rotated secret works without restart
        if not secret:
            return True
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        if q.get("k", [""])[0] == secret:
            return True
        got = (self.headers.get("x-vapi-secret", "")
               or self.headers.get("Authorization", "")
               .removeprefix("Bearer ").strip())
        return got == secret

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
        elif path == "/sessions":
            # Fleet roster for the phone: which agents exist, who's idle.
            if not self._authed():
                self._reply(401, b"unauthorized", "text/plain")
                return
            from . import sessions as _sess
            rows = [{"sid": r.get("sid", ""), "label": r.get("label", ""),
                     "state": r.get("state", "")} for r in _sess.roster()]
            active = _read_json(ACTIVE) or {}
            self._reply(200, json.dumps(
                {"sessions": rows,
                 "active": active.get("session_id", "")}).encode(),
                "application/json")
        elif path == "/last":
            # Hear another session's latest reply WITHOUT switching/injecting.
            if not self._authed():
                self._reply(401, b"unauthorized", "text/plain")
                return
            from urllib.parse import urlparse, parse_qs
            from . import sessions as _sess
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            self._reply(200, json.dumps(
                {"reply": _sess.read_last(q)}).encode(), "application/json")
        elif path == "/poll":
            # Latest reply of the active session, no injection: lets the page
            # check back after a long turn instead of dead-ending on timeout.
            if not self._authed():
                self._reply(401, b"unauthorized", "text/plain")
                return
            tp = _target_transcript()
            cur = core.last_assistant_text(tp) if tp else ""
            self._reply(200, json.dumps(
                {"reply": core.clean_for_speech(cur, max_chars=2500)}
            ).encode(), "application/json")
        elif path == "/":
            if not self._authed():
                # A friendly recovery page: the installed PWA drops ?k= (its
                # start_url can't carry the secret), so redirect from
                # localStorage when the page saved it, else ask for it once.
                self._reply(401, AUTH_PAGE.encode(),
                            "text/html; charset=utf-8")
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

        if path == "/switch":  # {"query": "jobhunt"} -> move the call there
            try:
                q = (json.loads(raw or b"{}").get("query") or "").strip()
            except Exception:
                self._reply(400, b"bad request", "text/plain")
                return
            from . import sessions as _sess
            msg = _sess.switch(q) if q else "Which session?"
            try:
                EPOCH.write_text(str(time.time()))   # end any in-flight turn
            except Exception:
                pass
            self._reply(200, json.dumps({"result": msg}).encode(),
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


def _health_local(timeout: float = 0.6) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/health", timeout=timeout) as r:
            return r.read() == b"ok"
    except Exception:
        return False


def on() -> str:
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    # ALWAYS persist the secret (even if the relay is already up): auth reads
    # it live, so the link `vb phone` prints works without a restart.
    s = os.environ.get("VB_CALL_SECRET", "")
    if s:
        (core.STATE_DIR / "call_secret").write_text(s)
    if _alive():
        return "call relay already running"
    if _health_local():
        # Something else (a stale orphan) owns the port and answers /health.
        # Adopt it: auth reads the secret file live, so it serves fine.
        return f"call relay already running on port {PORT} (adopted)"
    vb = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin", "vb")
    env = dict(os.environ)
    p = subprocess.Popen([sys.executable, vb, "call", "__run__"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    PID.write_text(str(p.pid))
    # Verify it actually came up: a silent bind failure (port in use) used to
    # print "relay ON" and leave the phone hitting a corpse through the tunnel.
    t0 = time.time()
    while time.time() - t0 < 4.0:
        if _health_local(0.4):
            return (f"call relay ON (pid {p.pid}, port {PORT}). Tunnel it "
                    f"(`ngrok http {PORT}`) or run `vb phone`.")
        if p.poll() is not None:
            break
        time.sleep(0.2)
    return (f"ERROR: relay did not come up on port {PORT} (in use?). "
            f"Try `vb call off`, then `vb phone` again.")


def off() -> str:
    try:
        os.kill(int(PID.read_text().strip()), 15)
    except Exception:
        pass
    try:
        PID.unlink()
    except FileNotFoundError:
        pass
    # Kill the quick tunnel too: orphaned cloudflareds used to pile up, each
    # pointing old QRs at whatever owns the port next (confusing half-working
    # links). vb phone records tunnel.pid for exactly this.
    tp = core.STATE_DIR / "tunnel.pid"
    try:
        os.kill(int(tp.read_text().strip()), 15)
    except Exception:
        pass
    try:
        tp.unlink()
    except FileNotFoundError:
        pass
    return "call relay OFF (tunnel stopped)"


def status() -> str:
    return "\n".join([
        f"relay  : {'running' if _alive() else 'stopped'} (port {PORT})",
        f"secret : {'set' if SECRET else '(none - set VB_CALL_SECRET!)'}",
        f"target : {_target_transcript() or '(no session)'}",
    ])

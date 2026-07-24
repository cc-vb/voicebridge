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


import re as _re
YES_RE = _re.compile(r"^\s*(yes|yeah|yep|ok(ay)?|sure|go ahead|approve[d]?|"
                     r"allow( it)?|do it|confirm)[.!\s]*$", _re.IGNORECASE)
NO_RE = _re.compile(r"^\s*(no|nope|deny|don'?t( do it)?|reject|cancel|"
                    r"stop)[.!\s]*$", _re.IGNORECASE)


def _active_sid() -> str:
    return (_read_json(ACTIVE) or {}).get("session_id", "")


def _handle_pending(text: str, pending: str) -> str:
    """A decision is blocking the session. Spoken yes -> Enter (accept the
    highlighted default), spoken no -> Escape (dismiss). Anything else gets
    told what Claude is asking, typing prose into a permission dialog would
    go nowhere anyway."""
    if YES_RE.match(text):
        core.clear_pending_notice()
        inject.press_enter()
        return ""   # approved: fall through to waiting for the reply
    if NO_RE.match(text):
        core.clear_pending_notice()
        inject.press_escape()
        return "Okay, declined. What next?"
    q = core.clean_for_speech(pending, max_chars=300)
    return f"Claude is waiting on you: {q}. Say yes to allow, or no to decline."


def _ask_session(text: str) -> str:
    """Inject a turn into the live session and wait for the reply."""
    if DRYRUN:
        return f"dry run reply to: {text}"
    tp = _target_transcript()
    if not tp:
        return "I can't find an open session on the Mac."
    prev = core.last_assistant_text(tp)
    core.log(f"call you: {text}")
    # Permission relay: if Claude is blocked on a decision, a spoken yes/no
    # answers it with the right KEYSTROKE (text pasted into a permission
    # dialog goes nowhere); anything else hears what's being asked.
    pending = core.get_pending_notice(_active_sid())
    if pending:
        out = _handle_pending(text, pending)
        if out:
            return out
    else:
        # Guard the paste: it lands in the FRONTMOST Mac app, so if the bound
        # terminal isn't focused (or the screen is locked) refuse loudly
        # instead of typing into Slack / waiting 90s for nothing.
        from .talkd import bound_app
        if not inject.paste_text(text, send=True, expect_app=bound_app()):
            return ("I couldn't type into the session, the terminal isn't "
                    "the focused window on your Mac. Bring it to the front, "
                    "or check the screen isn't locked, then try again.")
    ep = _epoch()
    t0 = time.time()
    while time.time() - t0 < TIMEOUT:
        time.sleep(1.0)
        if _epoch() != ep:
            return "Okay, switched. Go ahead."   # /switch ended this turn
        fresh = core.get_pending_notice(_active_sid())
        if fresh:
            # Claude hit a decision moment mid-turn: surface it NOW instead
            # of timing out with "still working" while it sits blocked.
            q = core.clean_for_speech(fresh, max_chars=300)
            return (f"Claude is waiting on you: {q}. Say yes to allow, "
                    f"or no to decline.")
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


PAGE = r"""<!DOCTYPE html>
<!--
  voicebridge call page v3. Drop-in replacement for PAGE in vb/call.py.
  Embed as a RAW string (r prefix) so regex backslashes survive. This file
  contains no triple double-quote sequence anywhere, so it is safe inside
  a Python raw triple-quoted string.

  Endpoint contract used (every call carries ?k=SECRET):
    GET  /            serves this page (401 recovery page without a valid k)
    POST /ask         body {"text":"..."} returns {"reply":"..."}. The server
                      may block up to ~90s and answer with a timeout phrase
                      ("Still working on that..."); the page treats that as
                      "turn still in progress" and keeps polling /poll.
    GET  /poll        {"reply":"<latest cleaned reply of the active session>"}
                      Snapshot, no injection. The page records this BEFORE an
                      ask and polls until the text changes, then speaks it.
    GET  /status      {"pending":"<question>"} empty string when Claude is not
                      blocked on a permission decision. Polled ~4s while a turn
                      is working, ~8s while idle on a live call. Non-empty:
                      speak it and show the YES / NO pair (each sends POST /ask
                      with text "yes" or "no", the permission relay).
    GET  /sessions    {"sessions":[{"id","name","state","current",
                      "pending":bool}]}. Control room roster. Polled ~8s while
                      the sheet is open, ~20s in the background of a live call.
    GET  /last?q=NAME {"reply":"..."} latest reply of the named session,
                      spoken WITHOUT switching the call.
    POST /switch      body {"id":"..."} repoints the call at that session.
    GET  /chat        {"turns":[{"role":"user"|"assistant","text":"..."}]}
                      most recent last, cleaned for reading. Renders the chat
                      sheet; refreshed after each completed turn and on
                      pull-to-refresh. Page degrades to its local transcript
                      when the endpoint is missing.
    POST /heartbeat   empty body, every 5s while the call is live. Freshness
                      tells the Mac to keep its own speakers quiet; the page
                      shows the "audio on phone" chip while beats land.
    POST /stt         audio blob (webm/mp4) returns {"text":"..."}, the
                      whisper fallback when native SpeechRecognition is absent.

  All inline, no external assets or CDNs. iOS Safari and Android Chrome
  quirks handled: SpeechSynthesis chunking, visibilitychange recovery,
  wake lock re-arm, data-URI manifest that preserves ?k= on Add to Home
  Screen, safe-area insets, prefers-reduced-motion.
-->
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#0a0d14">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="voicebridge">
<link rel="apple-touch-icon" href="/icon.svg">
<title>voicebridge call</title>
<style>
/* Registered custom properties so state hue changes tween instead of snap.
   Old browsers skip @property and simply cut to the new color. */
@property --o1 { syntax:'<color>'; inherits:true; initial-value:#dfe7f8; }
@property --o2 { syntax:'<color>'; inherits:true; initial-value:#5f74b0; }
@property --o3 { syntax:'<color>'; inherits:true; initial-value:#1c2440; }
@property --glow { syntax:'<color>'; inherits:true; initial-value:rgba(95,116,176,.34); }
@property --ring { syntax:'<color>'; inherits:true; initial-value:rgba(120,140,200,.5); }

* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { height:100%; overflow:hidden; overscroll-behavior:none; }
body {
  position:fixed; inset:0; margin:0;
  display:flex; flex-direction:column;
  background:#0a0d14; color:#e8ebf2;
  font-family:ui-rounded, -apple-system, "SF Pro Rounded", system-ui, "Segoe UI", Roboto, sans-serif;
  user-select:none; -webkit-user-select:none; touch-action:manipulation;
  transition:--o1 .9s ease, --o2 .9s ease, --o3 .9s ease, --glow .9s ease, --ring .9s ease;
  --o1:#dfe7f8; --o2:#5f74b0; --o3:#1c2440;
  --glow:rgba(95,116,176,.34); --ring:rgba(120,140,200,.5);
  --danger:#e5484d; --amber:#e5a13d; --mint:#46d7c3;
  --dim:#96a0b5; --surface:#141926; --line:#232b3d;
}
body[data-state="listening"] { --o1:#e4fff8; --o2:#37bfae; --o3:#093330; --glow:rgba(70,215,195,.42); --ring:rgba(90,225,205,.55); }
body[data-state="thinking"]  { --o1:#ece4ff; --o2:#8674e6; --o3:#241d4e; --glow:rgba(140,120,235,.42); --ring:rgba(160,140,255,.55); }
body[data-state="speaking"]  { --o1:#ffffff; --o2:#8fb2f2; --o3:#16294f; --glow:rgba(160,195,255,.55); --ring:rgba(180,205,255,.6); }
body[data-state="needs"]     { --o1:#ffe9df; --o2:#e0755f; --o3:#3f150f; --glow:rgba(235,125,100,.45); --ring:rgba(255,150,125,.55); }
body[data-state="muted"]     { --o1:#ccd1db; --o2:#59606f; --o3:#191d26; --glow:rgba(120,128,148,.22); --ring:rgba(130,138,158,.35); }
body[data-state="ended"]     { --o1:#b9bfca; --o2:#464c5a; --o3:#14171f; --glow:rgba(100,106,124,.16); --ring:rgba(110,118,138,.25); }

button { font:inherit; color:inherit; background:none; border:0; cursor:pointer; padding:0; }
button:focus-visible { outline:2px solid #9db9ff; outline-offset:3px; border-radius:14px; }

/* ==== top: session pill + audio-ownership chip ==== */
header {
  padding:calc(env(safe-area-inset-top, 0px) + 14px) 16px 6px;
  display:flex; flex-direction:column; align-items:center; gap:8px;
}
#pill {
  display:flex; align-items:center; gap:8px;
  min-height:44px; padding:10px 16px; border-radius:999px;
  background:rgba(255,255,255,.055); border:1px solid var(--line);
  font-size:15px; color:#dfe4ee; max-width:80vw;
}
#pill .dot { width:8px; height:8px; border-radius:50%; background:var(--o2); flex:none;
  transition:background .9s ease; }
#pill .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#pill svg { width:14px; height:14px; opacity:.6; flex:none; }
#chip {
  display:none; align-items:center; gap:6px;
  padding:4px 12px; border-radius:999px;
  background:rgba(70,215,195,.08); border:1px solid rgba(70,215,195,.28);
  font-size:11px; letter-spacing:.09em; text-transform:uppercase; color:#6fd8c8;
}
#chip.on { display:inline-flex; }
#chip .cdot { width:6px; height:6px; border-radius:50%; background:var(--mint);
  animation:softpulse 2.4s ease-in-out infinite; }
@keyframes softpulse { 0%,100% { opacity:1; } 50% { opacity:.35; } }

/* ==== middle: the orb ==== */
main { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:34px; min-height:0; }
#orbzone { position:relative; width:min(64vw, 280px); aspect-ratio:1; }
.ripple {
  position:absolute; inset:0; border-radius:50%;
  border:1.5px solid var(--ring); opacity:0; pointer-events:none;
}
body[data-state="thinking"] .ripple,
body[data-state="needs"] .ripple { animation:ripple 2.6s cubic-bezier(.2,.6,.35,1) infinite; }
body[data-state="thinking"] .ripple:nth-child(2),
body[data-state="needs"] .ripple:nth-child(2) { animation-delay:.85s; }
body[data-state="thinking"] .ripple:nth-child(3),
body[data-state="needs"] .ripple:nth-child(3) { animation-delay:1.7s; }
@keyframes ripple { 0% { transform:scale(1); opacity:.55; } 100% { transform:scale(1.85); opacity:0; } }

#orbscale { position:absolute; inset:0;
  transform:scale(calc(1 + var(--level, 0) * .16));
  transition:transform .14s linear;
}
#orb {
  position:absolute; inset:0; border-radius:50%;
  background:radial-gradient(circle at 33% 28%, var(--o1) 0%, var(--o2) 46%, var(--o3) 82%);
  box-shadow:0 0 70px 6px var(--glow), inset 0 0 46px rgba(0,0,0,.35);
  animation:breathe 5.4s ease-in-out infinite;
  overflow:hidden;
}
#orb::after { /* slow drifting sheen so the sphere feels alive, not flat */
  content:""; position:absolute; inset:-30%;
  background:conic-gradient(from 0deg, transparent 0 62%, rgba(255,255,255,.16) 74%, transparent 86%);
  animation:sheen 11s linear infinite;
}
@keyframes breathe { 0%,100% { transform:scale(1); } 50% { transform:scale(1.045); } }
@keyframes sheen { to { transform:rotate(360deg); } }
body[data-state="speaking"] #orb { animation:breathe 5.4s ease-in-out infinite, speakglow 1.5s ease-in-out infinite; }
body[data-state="speaking"] #orb::after { animation-duration:4.5s; }
@keyframes speakglow {
  0%,100% { box-shadow:0 0 70px 6px var(--glow), inset 0 0 46px rgba(0,0,0,.35); }
  50%     { box-shadow:0 0 120px 26px var(--glow), inset 0 0 32px rgba(0,0,0,.22); }
}
body[data-state="ended"] #orb, body[data-state="muted"] #orb { animation:none; }

#status {
  font-size:15px; letter-spacing:.42em; text-indent:.42em; /* balance tracking */
  text-transform:lowercase; color:var(--dim); min-height:22px; text-align:center;
  padding:0 24px; font-variant-numeric:tabular-nums;
}
body[data-state="needs"] #status { color:#f0a294; }

/* ==== bottom: call controls ==== */
footer {
  display:flex; align-items:center; justify-content:center; gap:34px;
  padding:10px 24px calc(env(safe-area-inset-bottom, 0px) + 26px);
}
.ctl {
  width:64px; height:64px; border-radius:50%; position:relative;
  display:flex; align-items:center; justify-content:center;
  background:rgba(255,255,255,.07); border:1px solid var(--line);
  transition:background .25s ease, transform .1s ease;
}
.ctl:active { transform:scale(.93); }
.ctl svg { width:26px; height:26px; }
#muteBtn { width:80px; height:80px; background:rgba(255,255,255,.1); }
#muteBtn svg { width:32px; height:32px; }
#muteBtn.muted { background:#e8ebf2; color:#0a0d14; }
#muteBtn .off { display:none; }
#muteBtn.muted .off { display:block; }
#muteBtn.muted .on { display:none; }
#endBtn { background:var(--danger); border-color:transparent; color:#fff; }
#chatBtn.active { background:rgba(255,255,255,.18); }

/* ==== decision panel: the permission relay ==== */
#decide {
  position:fixed; left:14px; right:14px; z-index:35;
  bottom:calc(env(safe-area-inset-bottom, 0px) + 124px);
  background:var(--surface); border:1px solid rgba(229,72,77,.4);
  border-radius:20px; padding:16px 16px 14px;
  transform:translateY(calc(100% + 180px));
  transition:transform .3s cubic-bezier(.3,.9,.3,1);
  box-shadow:0 8px 40px rgba(0,0,0,.5);
}
#decide.open { transform:translateY(0); }
#decide .eyebrow { font-size:11px; letter-spacing:.16em; text-transform:uppercase;
  color:#ff8a8e; margin:0 0 8px; display:flex; align-items:center; gap:7px; }
#decide .eyebrow .ddot { width:7px; height:7px; border-radius:50%; background:var(--danger);
  animation:softpulse 1.6s ease-in-out infinite; }
#decideQ { font-size:15.5px; line-height:1.5; color:#e8ebf2; margin:0;
  max-height:7.5em; overflow-y:auto; -webkit-overflow-scrolling:touch;
  user-select:text; -webkit-user-select:text; }
#decide .row { display:flex; gap:12px; margin-top:14px; }
#decide .row button { flex:1; min-height:58px; border-radius:16px;
  font-size:18px; font-weight:650; letter-spacing:.04em; }
#decide .row button:active { transform:scale(.97); }
#yesBtn { background:var(--mint); color:#06231f; }
#noBtn { border:1.5px solid rgba(229,72,77,.6); color:#ff9a9e; }

/* ==== toast: background session news ==== */
#toast {
  position:fixed; left:50%; z-index:36;
  bottom:calc(env(safe-area-inset-bottom, 0px) + 132px);
  transform:translate(-50%, 16px);
  display:flex; align-items:center; gap:9px;
  background:#1a2130; border:1px solid var(--line); border-radius:999px;
  padding:11px 18px; font-size:14px; color:#dfe4ee;
  opacity:0; pointer-events:none; transition:opacity .3s ease, transform .3s ease;
  max-width:86vw; box-shadow:0 6px 28px rgba(0,0,0,.45);
}
#toast.show { opacity:1; transform:translate(-50%, 0); pointer-events:auto; }
#toast .tdot { width:7px; height:7px; border-radius:50%; background:var(--amber); flex:none; }
#toast .t { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* ==== sheets ==== */
#scrim { position:fixed; inset:0; background:rgba(4,6,10,.55); opacity:0;
  pointer-events:none; transition:opacity .25s ease; z-index:40; }
body.sheet-open #scrim { opacity:1; pointer-events:auto; }
.sheet {
  position:fixed; left:0; right:0; bottom:0; z-index:50;
  background:var(--surface); border-radius:20px 20px 0 0;
  border-top:1px solid var(--line);
  transform:translateY(105%); transition:transform .3s cubic-bezier(.3,.9,.3,1);
  padding:10px 18px calc(env(safe-area-inset-bottom, 0px) + 16px);
  max-height:62vh; display:flex; flex-direction:column;
}
.sheet.open { transform:translateY(0); }
.sheet .grab { width:38px; height:4px; border-radius:2px; background:#39435a; margin:2px auto 12px; flex:none; }
.sheet h2 { font-size:13px; font-weight:600; letter-spacing:.14em; text-transform:uppercase;
  color:var(--dim); margin:0 0 10px; flex:none; display:flex; align-items:baseline; gap:10px; }
.sheet h2 .count { font-weight:500; letter-spacing:.04em; text-transform:none; font-size:12.5px; color:#5b6479; }
.sheet .scroll { overflow-y:auto; overscroll-behavior:contain; -webkit-overflow-scrolling:touch; min-height:60px; }
.sheet .hint { font-size:12.5px; color:var(--dim); line-height:1.5; padding:8px 12px 0; flex:none; }

/* control room: full-height so a fleet of sessions fits */
#sessSheet {
  height:calc(100vh - 64px); height:calc(100dvh - 64px);
  max-height:none;
}
#sessSheet .scroll { flex:1; }

/* chat: non-modal, floats above the call controls so mute and end
   stay reachable while reading */
#chatSheet {
  left:10px; right:10px;
  bottom:calc(env(safe-area-inset-bottom, 0px) + 122px);
  border:1px solid var(--line); border-radius:20px;
  height:52vh; height:52dvh; max-height:none; padding-bottom:14px; z-index:30;
  transform:translateY(calc(100% + 140px));
}
#chatSheet.open { transform:translateY(0); }
#pullHint { height:0; overflow:hidden; text-align:center; font-size:12px;
  color:var(--dim); line-height:28px; transition:height .18s ease; flex:none; }
#chatLines { display:flex; flex-direction:column; gap:8px; padding:2px 2px 6px; }
.bub {
  max-width:84%; padding:10px 14px; border-radius:18px;
  font-size:15.5px; line-height:1.5; white-space:pre-wrap; word-break:break-word;
  user-select:text; -webkit-user-select:text;
}
.bub.user { align-self:flex-end; background:rgba(70,215,195,.15);
  border:1px solid rgba(70,215,195,.22); border-bottom-right-radius:6px; color:#e7fffa; }
.bub.assistant { align-self:flex-start; background:rgba(255,255,255,.06);
  border:1px solid var(--line); border-bottom-left-radius:6px; }
.bub.live { border-style:dashed; }
#chatLines .empty { color:var(--dim); font-size:14px; align-self:center; padding:18px 8px; text-align:center; line-height:1.55; }

/* control room cards */
.card {
  display:flex; align-items:center; gap:6px; border-radius:16px;
  border:1px solid transparent; margin-bottom:2px; padding-right:6px;
}
.card.current { background:rgba(255,255,255,.05); border-color:var(--line); }
.card .main {
  flex:1; min-width:0; display:flex; align-items:center; gap:12px;
  text-align:left; padding:13px 8px 13px 12px; min-height:60px; border-radius:14px;
}
.card .main:active { background:rgba(255,255,255,.05); }
.sdot { width:10px; height:10px; border-radius:50%; background:var(--mint); flex:none; }
.sdot.working { background:var(--amber); animation:softpulse 1.6s ease-in-out infinite; }
.sdot.needs { background:var(--danger); }
.card .meta { min-width:0; flex:1; }
.card .meta .n { display:block; font-size:16px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.card .meta .c { display:block; font-size:12.5px; color:var(--dim); margin-top:2px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.badge { flex:none; font-size:10.5px; letter-spacing:.1em; text-transform:uppercase;
  border-radius:999px; padding:4px 10px; }
.badge.needs { color:#ff9a9e; border:1px solid rgba(229,72,77,.55); background:rgba(229,72,77,.12);
  animation:softpulse 1.6s ease-in-out infinite; }
.badge.oncall { color:var(--mint); border:1px solid rgba(70,215,195,.4); }
.hear {
  flex:none; width:46px; height:46px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  background:rgba(255,255,255,.07); border:1px solid var(--line);
}
.hear:active { transform:scale(.92); }
.hear svg { width:20px; height:20px; }
.hear.busy { opacity:.45; }

/* ==== overlays: start / switching ==== */
.overlay {
  position:fixed; inset:0; z-index:60; background:#0a0d14;
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  gap:18px; text-align:center; padding:32px 28px; transition:opacity .35s ease;
}
.overlay.hidden { opacity:0; pointer-events:none; }
.overlay .glyph { width:74px; height:74px; border-radius:50%;
  background:radial-gradient(circle at 33% 28%, #dfe7f8 0%, #5f74b0 46%, #1c2440 82%);
  box-shadow:0 0 50px 4px rgba(95,116,176,.35); animation:breathe 5.4s ease-in-out infinite; }
.overlay h1 { font-size:24px; font-weight:650; margin:6px 0 0; letter-spacing:-.01em; }
.overlay p { font-size:15.5px; line-height:1.6; color:var(--dim); max-width:34ch; margin:0; }
.overlay .cta {
  margin-top:10px; min-height:58px; padding:16px 42px; border-radius:999px;
  background:#e8ebf2; color:#0a0d14; font-size:17px; font-weight:650;
}
.overlay .cta:active { transform:scale(.96); }
.overlay .fine { font-size:12.5px; color:#6b7488; max-width:36ch; line-height:1.55; }

#switchOverlay { background:rgba(6,8,13,.88); backdrop-filter:blur(8px);
  -webkit-backdrop-filter:blur(8px); z-index:70; gap:22px; }
#switchOverlay .spin { width:42px; height:42px; border-radius:50%;
  border:2.5px solid var(--line); border-top-color:#9db9ff;
  animation:spin 1s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
#switchMsg { font-size:16px; color:#dfe4ee; letter-spacing:.02em; }

/* ==== reduced motion: state still legible via color and text ==== */
@media (prefers-reduced-motion: reduce) {
  #orb, #orb::after, .overlay .glyph, body[data-state="speaking"] #orb { animation:none; }
  body[data-state="thinking"] .ripple,
  body[data-state="needs"] .ripple { animation:none; opacity:.35; transform:scale(1.25); }
  #orbscale { transition:none; transform:none; }
  .sheet, #decide, #toast { transition:none; }
  #switchOverlay .spin { animation:none; border-top-color:var(--line); }
  .sdot.working, .badge.needs, #chip .cdot, #decide .eyebrow .ddot { animation:none; }
}
</style></head><body data-state="ended">

<header>
  <button id="pill" aria-haspopup="dialog" aria-label="Session: live session. Open control room.">
    <span class="dot" aria-hidden="true"></span>
    <span class="name" id="pillName">live session</span>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
  </button>
  <span id="chip" role="status"><span class="cdot" aria-hidden="true"></span>audio on phone</span>
</header>

<main>
  <div id="orbzone" aria-hidden="true">
    <div class="ripple"></div><div class="ripple"></div><div class="ripple"></div>
    <div id="orbscale"><div id="orb"></div></div>
  </div>
  <div id="status" role="status" aria-live="polite">call ended</div>
</main>

<footer>
  <button class="ctl" id="chatBtn" aria-pressed="false" aria-label="Show chat">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9.2 9.2 0 0 1-3.9-.9L3 20l1-4.1a8.2 8.2 0 0 1-1-4.4 8.4 8.4 0 0 1 9-8.4 8.4 8.4 0 0 1 9 8.4z"/>
      <path d="M8.5 10.5h7"/><path d="M8.5 13.5h4.5"/>
    </svg>
  </button>
  <button class="ctl" id="muteBtn" aria-pressed="false" aria-label="Mute microphone">
    <svg class="on" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="12" rx="3" fill="currentColor" stroke="none"/>
      <path d="M6 12a6 6 0 0 0 12 0"/><path d="M12 18v3"/>
    </svg>
    <svg class="off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="12" rx="3" fill="currentColor" stroke="none"/>
      <path d="M6 12a6 6 0 0 0 12 0"/><path d="M12 18v3"/><path d="M4 4l16 16"/>
    </svg>
  </button>
  <button class="ctl" id="endBtn" aria-label="End call">
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12 9.5c-3.3 0-6.4 1-8.9 2.9-.6.4-.8 1.2-.5 1.9l.9 1.9c.3.7 1.1 1 1.8.8l2.6-.9c.6-.2 1-.8 1-1.4v-1.3c2-.6 4.2-.6 6.2 0v1.3c0 .6.4 1.2 1 1.4l2.6.9c.7.2 1.5-.1 1.8-.8l.9-1.9c.3-.7.1-1.5-.5-1.9A14.6 14.6 0 0 0 12 9.5z"/>
    </svg>
  </button>
</footer>

<section id="decide" role="alertdialog" aria-label="Claude needs a decision" aria-live="assertive">
  <p class="eyebrow"><span class="ddot" aria-hidden="true"></span>claude needs you</p>
  <p id="decideQ"></p>
  <div class="row">
    <button id="yesBtn">Yes, allow</button>
    <button id="noBtn">No, decline</button>
  </div>
</section>

<button id="toast" aria-live="polite">
  <span class="tdot" aria-hidden="true"></span><span class="t" id="toastText"></span>
</button>

<div id="scrim"></div>

<section class="sheet" id="chatSheet" role="dialog" aria-label="Chat">
  <div class="grab" aria-hidden="true"></div>
  <h2>Chat</h2>
  <div class="scroll" id="chatScroll">
    <div id="pullHint">pull to refresh</div>
    <div id="chatLines"><p class="empty">The conversation with this session appears here.</p></div>
  </div>
</section>

<section class="sheet" id="sessSheet" role="dialog" aria-label="Control room">
  <div class="grab" aria-hidden="true"></div>
  <h2>Control room <span class="count" id="sessCount"></span></h2>
  <div class="scroll" id="sessList"></div>
  <p class="hint">Tap a session to move the call there. The speaker icon plays its last reply without switching.</p>
</section>

<div class="overlay" id="startOverlay">
  <div class="glyph" aria-hidden="true"></div>
  <h1 id="startTitle">voicebridge</h1>
  <p id="startBody">A live call with your coding session. Your phone will ask to use the
     microphone; audio goes only to your Mac and nowhere else.</p>
  <button class="cta" id="startBtn">Start call</button>
  <p class="fine" id="startFine">Speech is transcribed on-device when the browser supports it,
     otherwise on your Mac with whisper. Say "end call" any time to hang up.</p>
</div>

<div class="overlay hidden" id="switchOverlay">
  <div class="spin" aria-hidden="true"></div>
  <div id="switchMsg" role="status" aria-live="polite">ending previous call</div>
</div>

<script>
'use strict';
/* ============================================================ state */
const K = new URLSearchParams(location.search).get('k') || '';
const $ = id => document.getElementById(id);
const statusEl=$('status'), pillName=$('pillName'), muteBtn=$('muteBtn'),
      chatBtn=$('chatBtn'), chatLines=$('chatLines'), chatScroll=$('chatScroll'),
      pullHint=$('pullHint'), chipEl=$('chip'), decideEl=$('decide'),
      decideQ=$('decideQ'), toastEl=$('toast'), toastText=$('toastText'),
      sessList=$('sessList'), sessCount=$('sessCount');
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const TTS = 'speechSynthesis' in window;

let live=false, muted=false, state='ended';
let gen=0;                 // listen generation: bump to invalidate in-flight mic work
let turnId=0, turnActive=false;   // turn generation: bump to invalidate stale turn work
let rec=null, recActive=false, media=null, audioCtx=null;
let wakeLock=null, speechCancelled=false;

function setState(s, label){
  state=s;
  document.body.dataset.state = s;
  statusEl.textContent = label !== undefined ? label : s;
}
function urlFor(path){
  return path + (path.indexOf('?') >= 0 ? '&' : '?') + 'k=' + encodeURIComponent(K);
}
async function jget(path){
  const r = await fetch(urlFor(path));
  if(!r.ok) throw new Error('http ' + r.status);
  return r.json();
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* mic-level meter drives the orb scale via --level (0..1) */
let level=0, levelTarget=0;
function bumpLevel(v){ levelTarget = Math.max(levelTarget, v); }
(function levelLoop(){
  level += (levelTarget - level) * .25;
  levelTarget *= .9;
  if(level < .004) level = 0;
  document.documentElement.style.setProperty('--level', level.toFixed(3));
  requestAnimationFrame(levelLoop);
})();

/* ============================================================ PWA polish */
/* Data-URI manifest so Add to Home Screen keeps the ?k= secret in start_url
   (the server's /manifest.json points start_url at "/" and would land on 401). */
(function(){
  const icon = 'data:image/svg+xml,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' +
    '<rect width="100" height="100" rx="22" fill="#0a0d14"/>' +
    '<circle cx="50" cy="50" r="26" fill="#5f74b0"/>' +
    '<circle cx="42" cy="42" r="10" fill="#dfe7f8" opacity=".8"/></svg>');
  const man = { name:'voicebridge', short_name:'voicebridge', display:'standalone',
    background_color:'#0a0d14', theme_color:'#0a0d14',
    start_url:location.href, scope:new URL('/', location.href).href,
    icons:[{ src:icon, sizes:'any', type:'image/svg+xml' }] };
  const l = document.createElement('link');
  l.rel = 'manifest';
  l.href = 'data:application/manifest+json,' + encodeURIComponent(JSON.stringify(man));
  document.head.appendChild(l);
})();

async function acquireWakeLock(){
  try { if('wakeLock' in navigator) wakeLock = await navigator.wakeLock.request('screen'); }
  catch(e){ /* low battery or unsupported: fine, the call still works */ }
}
function releaseWakeLock(){ try{ wakeLock && wakeLock.release(); }catch(e){} wakeLock=null; }

/* ============================================================ speech out */
let voices=[];
function refreshVoices(){ if(TTS) voices = speechSynthesis.getVoices(); }
if(TTS) speechSynthesis.onvoiceschanged = refreshVoices;
refreshVoices();

function pickVoice(){
  const en = voices.filter(v => (v.lang||'').toLowerCase().startsWith('en'));
  return en.find(v => /siri|premium|enhanced|natural|neural/i.test(v.name)) || en[0] || voices[0];
}
/* Mobile browsers cut long utterances (a known engine bug), so split into
   sentence-sized chunks and chain them; each chunk is short enough that the
   per-utterance timeout never fires. */
function chunkText(text, max){
  const sents = text.match(/[^.!?\n]+[.!?]*\s*/g) || [text];
  const out = [];
  for(const s of sents){
    if(out.length && (out[out.length-1] + s).length <= max) out[out.length-1] += s;
    else if(s.length <= max) out.push(s);
    else for(let i=0; i<s.length; i+=max) out.push(s.slice(i, i+max));
  }
  return out.map(x => x.trim()).filter(Boolean);
}
function stopSpeaking(){ speechCancelled = true; if(TTS) speechSynthesis.cancel(); }
function say(text, done){
  if(!TTS){ done && done(); return; }
  speechSynthesis.cancel();
  speechCancelled = false;
  const parts = chunkText(text, 170);
  let i = 0;
  (function next(){
    if(speechCancelled) return;
    if(i >= parts.length){ done && done(); return; }
    const u = new SpeechSynthesisUtterance(parts[i++]);
    const v = pickVoice(); if(v) u.voice = v;
    u.rate = 1.0;
    u.onend = () => setTimeout(next, 50);
    u.onerror = () => setTimeout(next, 50);
    speechSynthesis.speak(u);
  })();
}
/* A short interjection (hear-last, pending question) that then returns to
   whatever the call was doing, without ending the working turn. */
function speakAside(text){
  stopListening(); stopSpeaking();
  setState('speaking');
  say(text, resumeAfterSpeech);
}
function resumeAfterSpeech(){
  if(!live){ setState('ended', 'call ended'); return; }
  if(decisionOpen){ setState('needs', 'needs you'); return; }
  if(turnActive){ setWorking(); return; }
  if(muted){ setState('muted'); return; }
  setState('listening'); setTimeout(listen, 300);
}
/* Backgrounding pauses synthesis on both platforms; resume when we return,
   re-arm the wake lock (released whenever the tab hides), kick a heartbeat,
   and restart the mic if a listen was in flight. */
document.addEventListener('visibilitychange', () => {
  if(document.visibilityState !== 'visible') return;
  if(TTS && speechSynthesis.paused) speechSynthesis.resume();
  if(live){ acquireWakeLock(); beatOnce(); }
  if(live && !muted && state === 'listening' && !recActive) listen();
});

/* soft chime for background-session news; WebAudio, no assets */
function chime(){
  try{
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if(audioCtx.state === 'suspended') audioCtx.resume();
    const t = audioCtx.currentTime + .01;
    [[880, 0], [1318.5, .16]].forEach(pair => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = 'sine'; o.frequency.value = pair[0];
      g.gain.setValueAtTime(0, t + pair[1]);
      g.gain.linearRampToValueAtTime(.055, t + pair[1] + .02);
      g.gain.exponentialRampToValueAtTime(.0001, t + pair[1] + .34);
      o.connect(g); g.connect(audioCtx.destination);
      o.start(t + pair[1]); o.stop(t + pair[1] + .4);
    });
  }catch(e){}
}

/* ============================================================ chat */
/* Local turn log renders instantly; GET /chat replaces it with the server's
   cleaned transcript when available (refreshed after each completed turn and
   on pull-to-refresh). Falls back to local-only when /chat is missing. */
let localTurns = [];
let chatHasServer = false;
function bubble(role, text, liveNow){
  const d = document.createElement('div');
  d.className = 'bub ' + (role === 'user' ? 'user' : 'assistant') + (liveNow ? ' live' : '');
  d.textContent = text;
  return d;
}
function chatScrollBottom(){ chatScroll.scrollTop = chatScroll.scrollHeight; }
function renderChat(){
  chatLines.textContent = '';
  if(!localTurns.length){
    const p = document.createElement('p'); p.className = 'empty';
    p.textContent = 'The conversation with this session appears here.';
    chatLines.appendChild(p);
    return;
  }
  localTurns.forEach(t => chatLines.appendChild(bubble(t.role, t.text)));
  chatScrollBottom();
}
function chatAdd(role, text){
  if(!text) return;
  localTurns.push({ role: role, text: text });
  const empty = chatLines.querySelector('.empty');
  if(empty) empty.remove();
  chatLines.appendChild(bubble(role, text, role !== 'user' && turnActive));
  chatScrollBottom();
}
async function refreshChat(){
  try{
    const j = await jget('/chat');
    if(Array.isArray(j.turns)){
      localTurns = j.turns
        .map(t => ({ role: t.role === 'user' ? 'user' : 'assistant',
                     text: String(t.text || '').trim() }))
        .filter(t => t.text);
      chatHasServer = true;
      renderChat();
      return true;
    }
  }catch(e){ /* endpoint missing or offline: keep the local log */ }
  if(!chatHasServer) renderChat();
  return false;
}
/* pull down at the top of the chat to refresh */
(function(){
  let y0 = 0, pulling = false, dist = 0, busy = false;
  chatScroll.addEventListener('touchstart', e => {
    if(busy) return;
    if(chatScroll.scrollTop <= 0){ y0 = e.touches[0].clientY; pulling = true; dist = 0; }
  }, { passive:true });
  chatScroll.addEventListener('touchmove', e => {
    if(!pulling || busy) return;
    dist = e.touches[0].clientY - y0;
    if(dist > 0 && chatScroll.scrollTop <= 0){
      e.preventDefault();
      const h = Math.min(dist * .4, 54);
      pullHint.style.height = h + 'px';
      pullHint.textContent = h >= 46 ? 'release to refresh' : 'pull to refresh';
    }
  }, { passive:false });
  chatScroll.addEventListener('touchend', async () => {
    if(!pulling || busy) return;
    pulling = false;
    if(Math.min(dist * .4, 54) >= 46){
      busy = true;
      pullHint.textContent = 'refreshing';
      pullHint.style.height = '28px';
      await refreshChat();
      busy = false;
    }
    pullHint.style.height = '0px';
    dist = 0;
  });
})();

/* ============================================================ the turn engine */
/* A turn is IN PROGRESS until the session's latest reply text CHANGES from
   what it was before the ask. The single POST /ask is only a fast path: the
   server gives up at ~90s with a stock phrase, and tunnels can drop long
   requests entirely, so GET /poll every few seconds is what actually
   completes long turns. */
const STILL_RE = /^still working on that/i;
const WAITING_RE = /^claude is waiting on you/i;
let workT0 = 0, workTicker = null;

function fmtElapsed(ms){
  const s = Math.floor(ms / 1000);
  if(s < 100) return s + 's';
  return Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';
}
function setWorking(){
  setState('thinking', 'working');
  if(workTicker) clearInterval(workTicker);
  workTicker = setInterval(() => {
    if(state === 'thinking') statusEl.textContent = 'working  ' + fmtElapsed(Date.now() - workT0);
  }, 1000);
}
function stopWorkTicker(){ if(workTicker){ clearInterval(workTicker); workTicker = null; } }

async function startTurn(text){
  const id = ++turnId;
  turnActive = true;
  chatAdd('user', text);
  workT0 = Date.now();
  setWorking();
  /* baseline BEFORE the ask so a changed /poll snapshot means "reply landed" */
  let baseline = '';
  try{ baseline = String((await jget('/poll')).reply || '').trim(); }catch(e){}
  if(id !== turnId || !live) return;
  sendAsk(id, text);
  pollUntilChanged(id, baseline);
}
function sendAsk(id, text){
  fetch(urlFor('/ask'), {
    method:'POST', headers:{ 'Content-Type':'application/json' },
    body:JSON.stringify({ text: text }) })
  .then(r => r.json())
  .then(j => {
    if(id !== turnId || !live) return;
    const rep = String(j.reply || '').trim();
    if(!rep || STILL_RE.test(rep)) return;         // long turn: /poll takes over
    if(WAITING_RE.test(rep)){ showDecision(rep); return; }   // permission moment
    finishTurn(id, rep);
  })
  .catch(() => { /* tunnel dropped the long request; /poll covers it */ });
}
async function pollUntilChanged(id, baseline){
  let n = 0;
  while(id === turnId && live){
    await sleep(n++ < 100 ? 3000 : 6000);   // ease off after five minutes
    if(id !== turnId || !live) return;
    try{
      const rep = String((await jget('/poll')).reply || '').trim();
      if(id !== turnId || !live) return;
      if(rep && rep !== baseline){ finishTurn(id, rep); return; }
    }catch(e){ /* offline blip: keep waiting, the elapsed label keeps counting */ }
  }
}
function finishTurn(id, reply){
  if(id !== turnId) return;
  turnId++;                       // one winner: kill the other waiters
  turnActive = false;
  stopWorkTicker();
  hideDecision();
  chatAdd('assistant', reply);
  refreshChat();                  // swap in the server's cleaned transcript
  pollSessions();                 // states likely changed with the turn
  setState('speaking');
  say(reply, () => {
    if(!live) return;
    if(muted){ setState('muted'); }
    else { setState('listening'); setTimeout(listen, 300); }
  });
}
function cancelTurn(){
  turnId++;
  turnActive = false;
  stopWorkTicker();
  hideDecision();
}
function handleUtterance(t){
  if(/^(stop listening|end call|hang up|goodbye)[.!]?$/i.test(t)){ endCall(); return; }
  if(t) startTurn(t);
  else if(live && !muted) setTimeout(listen, 300);
}

/* ============================================================ permission relay */
/* Claude is blocked on a yes/no. Speak the question once, park the orb in the
   "needs you" state, and put two big buttons on screen; each answers through
   the normal turn engine (POST /ask with "yes" or "no"), so the reply that
   follows the decision is caught by the same polling. */
let decisionOpen = false;
function showDecision(q){
  decideQ.textContent = q;
  if(!decisionOpen){
    decisionOpen = true;
    decideEl.classList.add('open');
    stopWorkTicker();
    stopListening();
    setState('needs', 'needs you');
    chime();
    speakAside(q);
  }
}
function hideDecision(){
  decisionOpen = false;
  decideEl.classList.remove('open');
}
$('yesBtn').addEventListener('click', () => { hideDecision(); cancelTurn(); startTurn('yes'); });
$('noBtn').addEventListener('click', () => { hideDecision(); cancelTurn(); startTurn('no'); });

/* /status poll: every ~4s while a turn is working, ~8s while idle on a live
   call, so a permission prompt surfaces even when Claude hits it mid-turn or
   between turns. An empty pending while the panel is open means it was
   answered elsewhere (for example on the Mac): put the call back where it was. */
let liveGen = 0;
async function statusLoop(myGen){
  while(live && myGen === liveGen){
    await sleep(turnActive ? 4000 : 8000);
    if(!live || myGen !== liveGen) return;
    try{
      const p = String((await jget('/status')).pending || '').trim();
      if(!live || myGen !== liveGen) return;
      if(p){
        showDecision('Claude is waiting on you: ' + p + '. Yes to allow, or no to decline.');
      }else if(decisionOpen){
        hideDecision();
        if(state === 'needs') resumeAfterSpeech();
      }
    }catch(e){}
  }
}

/* ============================================================ heartbeat */
/* While the call is live the phone owns the audio: a beat every 5s keeps the
   Mac's speech suppressed server-side, and the chip tells the user so. */
let hbTimer = null;
async function beatOnce(){
  if(!live){ chipEl.classList.remove('on'); return; }
  try{
    const r = await fetch(urlFor('/heartbeat'), { method:'POST' });
    chipEl.classList.toggle('on', r.ok && live);
  }catch(e){ chipEl.classList.remove('on'); }
}
function startHeartbeat(){
  stopHeartbeat();
  beatOnce();
  hbTimer = setInterval(beatOnce, 5000);
}
function stopHeartbeat(){
  if(hbTimer){ clearInterval(hbTimer); hbTimer = null; }
  chipEl.classList.remove('on');
}

/* ============================================================ speech in */
/* ---- path A: native speech recognition (Chrome Android, iOS Safari 14.5+) */
function listenSR(){
  if(recActive) return;
  const myGen = gen;
  rec = new SR();
  rec.lang = 'en-US';
  rec.interimResults = true;   // interim events drive the orb pulse
  rec.maxAlternatives = 1;
  recActive = true;
  setState('listening');
  let finalText = '';
  rec.onresult = e => {
    if(myGen !== gen) return;
    bumpLevel(.65);
    for(let i = e.resultIndex; i < e.results.length; i++)
      if(e.results[i].isFinal) finalText += e.results[i][0].transcript;
    if(finalText){
      const t = finalText.trim(); finalText = '';
      try{ rec.stop(); }catch(_){}
      handleUtterance(t);
    }
  };
  rec.onspeechstart = () => { if(myGen === gen) bumpLevel(.6); };
  rec.onerror = ev => {
    recActive = false;
    if(myGen !== gen) return;
    if(ev.error === 'not-allowed' || ev.error === 'service-not-allowed'){ micDenied(); return; }
    if(live && !muted && state === 'listening') setTimeout(listen, 900);
  };
  rec.onend = () => {
    recActive = false;
    if(myGen !== gen) return;
    if(live && !muted && state === 'listening') setTimeout(listen, 400);
  };
  rec.start();
}

/* ---- path B: record + silence detection, whisper on the Mac via /stt */
async function listenWhisper(){
  if(recActive) return;
  const myGen = gen;
  setState('listening');
  try{ media = media || await navigator.mediaDevices.getUserMedia({ audio:true }); }
  catch(e){ micDenied(); return; }
  if(myGen !== gen) return;
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  if(audioCtx.state === 'suspended') audioCtx.resume();
  const src = audioCtx.createMediaStreamSource(media);
  const an = audioCtx.createAnalyser(); an.fftSize = 2048; src.connect(an);
  const buf = new Float32Array(an.fftSize);
  const mr = new MediaRecorder(media); const chunks = [];
  mr.ondataavailable = e => chunks.push(e.data);
  recActive = true;
  let spoke = false, quiet = 0, idle = 0;
  const iv = setInterval(() => {
    if(myGen !== gen){ clearInterval(iv); try{ mr.stop(); }catch(_){} return; }
    an.getFloatTimeDomainData(buf);
    let s = 0; for(const v of buf) s += v*v;
    const rms = Math.sqrt(s / buf.length);
    levelTarget = Math.max(levelTarget, Math.min(1, rms * 10));
    if(rms > .02){ spoke = true; quiet = 0; } else if(spoke) quiet++;
    if((spoke && quiet >= 6) || (!spoke && ++idle > 200)){ clearInterval(iv); mr.stop(); }
  }, 250);
  mr.onstop = async () => {
    src.disconnect(); recActive = false;
    if(myGen !== gen) return;
    if(!spoke){ if(live && !muted) listen(); return; }
    setState('thinking', 'thinking');
    const blob = new Blob(chunks, { type: mr.mimeType || 'audio/webm' });
    try{
      const r = await fetch(urlFor('/stt'), { method:'POST', body:blob });
      const j = await r.json();
      if(myGen !== gen) return;
      handleUtterance((j.text || '').trim());
    }catch(e){
      if(live && !muted) setTimeout(listen, 1600);
    }
  };
  mr.start();
}
function listen(){ if(!live || muted || turnActive || decisionOpen) return; (SR ? listenSR : listenWhisper)(); }
function stopListening(){
  gen++;
  if(rec){ try{ rec.onend = null; rec.onerror = null; rec.stop(); }catch(_){} rec = null; }
  recActive = false;
}

/* ============================================================ call lifecycle */
const startOverlay=$('startOverlay'), startBtn=$('startBtn');
function micDenied(){
  live = false; liveGen++;
  cancelTurn(); stopListening(); stopSpeaking(); stopHeartbeat(); releaseWakeLock();
  setState('ended', 'microphone blocked');
  $('startTitle').textContent = 'Microphone is blocked';
  $('startBody').textContent = 'Allow microphone access for this site in your browser settings, then start the call again.';
  $('startFine').textContent = 'iOS: Settings, Safari, Microphone. Android: the lock icon in the address bar.';
  startBtn.textContent = 'Try again';
  startOverlay.classList.remove('hidden');
}
async function startCall(){
  startOverlay.classList.add('hidden');
  live = true; muted = false;
  liveGen++;
  muteBtn.classList.remove('muted');
  muteBtn.setAttribute('aria-pressed', 'false');
  refreshVoices();
  acquireWakeLock();
  setState('thinking', 'connecting');
  /* Prime the mic permission inside the tap gesture so the browser prompt has
     clear context; on the native-SR path release the stream right away so it
     never fights the recognizer for the device. */
  try{
    const s = await navigator.mediaDevices.getUserMedia({ audio:true });
    if(SR) s.getTracks().forEach(t => t.stop()); else media = s;
  }catch(e){ micDenied(); return; }
  startHeartbeat();
  statusLoop(liveGen);
  refreshChat();
  pollSessions();
  /* This first utterance also unlocks SpeechSynthesis under autoplay rules. */
  setState('speaking', 'connecting');
  say('Connected.', () => { if(live) listen(); });
}
function endCall(){
  live = false; liveGen++;
  cancelTurn(); stopListening(); stopSpeaking(); stopHeartbeat(); releaseWakeLock();
  setState('ended', 'call ended');
  $('startTitle').textContent = 'Call ended';
  $('startBody').textContent = 'Your session is still running on the Mac. Start a new call whenever you are ready.';
  startBtn.textContent = 'Start call';
  startOverlay.classList.remove('hidden');
}
startBtn.addEventListener('click', startCall);
$('endBtn').addEventListener('click', endCall);

muteBtn.addEventListener('click', () => {
  if(!live) return;
  muted = !muted;
  muteBtn.classList.toggle('muted', muted);
  muteBtn.setAttribute('aria-pressed', String(muted));
  muteBtn.setAttribute('aria-label', muted ? 'Unmute microphone' : 'Mute microphone');
  if(muted){
    stopListening();
    if(state === 'listening') setState('muted');
  }else if(state === 'muted' || state === 'listening'){
    setState('listening'); listen();
  } /* muted flipped during working/speaking: the turn loop checks it after */
});

/* ============================================================ sheets */
const scrim=$('scrim'), chatSheet=$('chatSheet'), sessSheet=$('sessSheet');
let sessOpen = false;
/* control room is modal (scrim); chat is an independent toggle */
function openSessSheet(){
  sessOpen = true;
  sessSheet.classList.add('open');
  document.body.classList.add('sheet-open');
  pollSessions();
}
function closeSessSheet(){
  sessOpen = false;
  sessSheet.classList.remove('open');
  document.body.classList.remove('sheet-open');
}
scrim.addEventListener('click', closeSessSheet);
$('pill').addEventListener('click', () => { sessOpen ? closeSessSheet() : openSessSheet(); });
chatBtn.addEventListener('click', () => {
  const open = chatSheet.classList.toggle('open');
  chatBtn.classList.toggle('active', open);
  chatBtn.setAttribute('aria-pressed', String(open));
  chatBtn.setAttribute('aria-label', open ? 'Hide chat' : 'Show chat');
  if(open){ chatScrollBottom(); refreshChat(); }
});

/* ============================================================ control room */
function isWorkingState(st){ return /work|think|run|busy/i.test(String(st || '')); }
async function fetchSessions(){
  try{
    const j = await jget('/sessions');
    return Array.isArray(j.sessions) ? j.sessions : null;
  }catch(e){ return null; }
}
function stateLabel(s){
  if(s.pending) return 'waiting on a decision';
  if(isWorkingState(s.state)) return 'working';
  return s.state || 'idle';
}
function sessionCard(s){
  const card = document.createElement('div');
  card.className = 'card' + (s.current ? ' current' : '');

  const main = document.createElement('button');
  main.className = 'main';
  main.setAttribute('aria-label', (s.current ? 'On call with ' : 'Move the call to ') + (s.name || 'session'));
  const dot = document.createElement('span');
  dot.className = 'sdot' + (s.pending ? ' needs' : (isWorkingState(s.state) ? ' working' : ''));
  const meta = document.createElement('span'); meta.className = 'meta';
  const n = document.createElement('span'); n.className = 'n'; n.textContent = s.name || 'session';
  const c = document.createElement('span'); c.className = 'c'; c.textContent = stateLabel(s);
  meta.append(n, c);
  main.append(dot, meta);
  if(s.pending){
    const b = document.createElement('span'); b.className = 'badge needs'; b.textContent = 'needs you';
    main.appendChild(b);
  }else if(s.current){
    const b = document.createElement('span'); b.className = 'badge oncall'; b.textContent = 'on call';
    main.appendChild(b);
  }
  main.addEventListener('click', () => switchTo(s));

  const hear = document.createElement('button');
  hear.className = 'hear';
  hear.setAttribute('aria-label', 'Hear the last reply from ' + (s.name || 'this session'));
  hear.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"' +
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M11 5 6.5 8.5H3v7h3.5L11 19z" fill="currentColor" stroke="none"/>' +
    '<path d="M15 9a4.2 4.2 0 0 1 0 6"/><path d="M17.8 6.6a8 8 0 0 1 0 10.8"/></svg>';
  hear.addEventListener('click', ev => { ev.stopPropagation(); hearLast(s, hear); });

  card.append(main, hear);
  return card;
}
function renderSessions(list){
  const keep = sessList.scrollTop;
  sessList.textContent = '';
  if(!list || !list.length){
    const h = document.createElement('p'); h.className = 'hint';
    h.textContent = list ? 'No open sessions found on the Mac.'
      : 'The relay has no session roster yet; the call follows the focused session on your Mac.';
    sessList.appendChild(h);
    sessCount.textContent = '';
    return;
  }
  list.forEach(s => sessList.appendChild(sessionCard(s)));
  sessCount.textContent = list.length + (list.length === 1 ? ' session' : ' sessions');
  sessList.scrollTop = keep;
}
async function hearLast(s, btn){
  btn.classList.add('busy');
  let rep = '';
  try{ rep = String((await jget('/last?q=' + encodeURIComponent(s.name || s.id))).reply || '').trim(); }
  catch(e){ rep = ''; }
  btn.classList.remove('busy');
  const name = s.name || 'that session';
  speakAside(rep ? ('From ' + name + ': ' + rep) : ('No reply yet from ' + name + '.'));
}

/* roster poll: ~8s with the sheet open, ~20s in the background of a live
   call. Background flips (working to needs-you, working to idle) get a toast
   and a soft chime so parallel sessions can call for attention. */
const prevSess = {};
let sessBusy = false;
async function pollSessions(){
  if(sessBusy) return;
  sessBusy = true;
  const list = await fetchSessions();
  sessBusy = false;
  if(!list){ if(sessOpen) renderSessions(null); return; }
  const cur = list.find(s => s.current);
  if(cur && cur.name){
    pillName.textContent = cur.name;
    $('pill').setAttribute('aria-label', 'Session: ' + cur.name + '. Open control room.');
  }
  for(const s of list){
    const key = s.id || s.name;
    if(!key) continue;
    const prev = prevSess[key];
    if(prev && live && !s.current){
      if(!prev.pending && s.pending){
        toast((s.name || 'a session') + ' needs you'); chime();
      }else if(!s.pending && isWorkingState(prev.state) && !isWorkingState(s.state)){
        toast((s.name || 'a session') + ' finished'); chime();
      }
    }
    prevSess[key] = { state: s.state, pending: !!s.pending };
  }
  if(sessOpen) renderSessions(list);
}
(async function sessionsLoop(){
  for(;;){
    try{ await pollSessions(); }catch(e){}
    await sleep(sessOpen ? 8000 : (live ? 20000 : 30000));
  }
})();

/* toast */
let toastTimer = null;
function toast(msg){
  toastText.textContent = msg;
  toastEl.classList.add('show');
  if(toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 4500);
}
toastEl.addEventListener('click', () => {
  toastEl.classList.remove('show');
  openSessSheet();
});

/* ============================================================ switching */
const switchOverlay=$('switchOverlay'), switchMsg=$('switchMsg');
async function switchTo(s){
  closeSessSheet();
  if(s.current) return;
  /* the "ending previous call" affordance: freeze the loops, show intent */
  switchMsg.textContent = 'ending previous call';
  switchOverlay.classList.remove('hidden');
  cancelTurn(); stopSpeaking(); stopListening();
  let ok = false;
  try{
    const r = await fetch(urlFor('/switch'), {
      method:'POST', headers:{ 'Content-Type':'application/json' },
      body:JSON.stringify({ id: s.id }) });
    ok = r.ok;
  }catch(e){}
  if(ok){
    pillName.textContent = s.name || 'session';
    $('pill').setAttribute('aria-label', 'Session: ' + pillName.textContent + '. Open control room.');
    switchMsg.textContent = 'connected to ' + pillName.textContent;
    localTurns = []; chatHasServer = false;   // new session, new transcript
    renderChat();
    refreshChat();
    await sleep(900);
  }else{
    switchMsg.textContent = 'could not switch; the session may have closed';
    pollSessions();
    await sleep(1900);
  }
  switchOverlay.classList.add('hidden');
  if(live){ if(muted) setState('muted'); else { setState('listening'); listen(); } }
}

/* Name the pill (and prime the roster diff) on load; also warm the chat so
   the sheet opens full the first time. First paint keeps the pre-permission
   explainer on top; body starts data-state "ended" so the orb sits dim
   behind it until the call begins. */
pollSessions();
refreshChat();
</script></body></html>
"""

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
            active = (_read_json(ACTIVE) or {}).get("session_id", "")
            now = time.time()
            rows = [{"id": r.get("sid", ""), "name": r.get("label", ""),
                     "state": r.get("state", ""),
                     "current": r.get("sid", "") == active,
                     "pending": bool(core.get_pending_notice(
                         r.get("sid", "") or "-")),
                     "last": _sess.last_preview(r.get("path", "")),
                     "ago": int(max(0, now - r.get("mtime", now)))}
                    for r in _sess.roster()]
            self._reply(200, json.dumps({"sessions": rows}).encode(),
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
        elif path == "/chat":
            # The conversation as chat bubbles: the phone user can READ what
            # happened (and catch up on anything they missed hearing).
            if not self._authed():
                self._reply(401, b"unauthorized", "text/plain")
                return
            tp = _target_transcript()
            turns = core.recent_turns(tp, n=30) if tp else []
            self._reply(200, json.dumps({"turns": turns}).encode(),
                        "application/json")
        elif path == "/status":
            # Is Claude waiting on a decision right now? (For page/app polls.)
            if not self._authed():
                self._reply(401, b"unauthorized", "text/plain")
                return
            pend = core.get_pending_notice(_active_sid())
            self._reply(200, json.dumps(
                {"pending": core.clean_for_speech(pend, max_chars=300)
                 if pend else ""}).encode(), "application/json")
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

        if path == "/heartbeat":
            # The page pings this every ~5s during a live call. While fresh,
            # the phone OWNS the audio: core.start_speech stays silent on the
            # Mac, so replies speak on the phone only (the whole point when
            # you're away from the laptop).
            core.mark_call_live()
            self._reply(200, b"{}", "application/json")
            return

        if path == "/switch":  # {"id": sid} or {"query": "jobhunt"}
            try:
                body = json.loads(raw or b"{}")
                sid = (body.get("id") or "").strip()
                q = (body.get("query") or "").strip()
            except Exception:
                self._reply(400, b"bad request", "text/plain")
                return
            from . import sessions as _sess
            if sid:
                msg = _sess.switch_sid(sid)
            elif q:
                msg = _sess.switch(q)
            else:
                msg = "Which session?"
            ok = msg.startswith("Voice moved")
            try:
                EPOCH.write_text(str(time.time()))   # end any in-flight turn
            except Exception:
                pass
            self._reply(200 if ok else 404,
                        json.dumps({"ok": ok, "name": msg,
                                    "result": msg}).encode(),
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

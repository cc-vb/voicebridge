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


def _write_secret(s: str) -> None:
    """Persist the shared secret readable by this account only. It is the
    whole authentication of a URL that can type into your Mac, so it has no
    business sitting in a world-readable file."""
    path = core.STATE_DIR / "call_secret"
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(s)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


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


# Turn ledger for idempotent /ask retries: id -> {tp, prev, ts}. A tunnel can
# drop the RESPONSE after the request injected, so the page must be able to
# re-POST the same turn id and resume waiting without a second injection.
_ASKED: dict = {}

# SSE subscribers: one queue per open /events connection. The stream is the
# seamless channel the polling never was: replies, delivery acks, and
# permission moments PUSH to the phone the second they happen, and
# EventSource reconnects by itself when the tunnel hiccups.
import queue as _queue
import threading as _threading
_SUBS: set = set()
_SUBS_LOCK = _threading.Lock()


def _broadcast(ev: dict) -> None:
    with _SUBS_LOCK:
        for q in list(_SUBS):
            try:
                q.put_nowait(ev)
            except Exception:
                pass


def _prune_asked(max_age: float = 900.0) -> None:
    now = time.time()
    for k in [k for k, v in _ASKED.items() if now - v.get("ts", 0) > max_age]:
        _ASKED.pop(k, None)


def _await_reply(rec: dict) -> str:
    """Resume waiting for a turn that already injected (idempotent retry)."""
    tp, prev = rec.get("tp", ""), rec.get("prev", "")
    if not tp:
        return "I can't find the session anymore."
    ep = _epoch()
    t0 = time.time()
    while time.time() - t0 < TIMEOUT:
        time.sleep(1.0)
        if _epoch() != ep:
            return "Okay, switched. Go ahead."
        fresh = core.get_pending_notice(_active_sid())
        if fresh:
            q = core.clean_for_speech(fresh, max_chars=300)
            return (f"Claude is waiting on you: {q}. Say yes to allow, "
                    f"or no to decline.")
        cur = core.last_assistant_text(tp)
        if cur and cur != prev:
            return core.clean_for_speech(cur, max_chars=2500)
    return "Still working on that. Ask me again in a moment."


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
    from .talkd import bound_app
    if YES_RE.match(text):
        if not inject.press_enter(expect_app=bound_app()):
            # Keystroke had nowhere to go: keep the notice (the dialog is
            # still up!) and say exactly what's wrong instead of looping.
            return ("I couldn't press allow, the terminal isn't the focused "
                    "window on your Mac. Bring it to the front and say yes "
                    "again.")
        core.clear_pending_notice()
        return ""   # approved: fall through to waiting for the reply
    if NO_RE.match(text):
        core.clear_pending_notice()
        inject.press_escape()
        return "Okay, declined. What next?"
    q = core.clean_for_speech(pending, max_chars=300)
    return f"Claude is waiting on you: {q}. Say yes to allow, or no to decline."


def _inject_only(text: str) -> str:
    """Inject a turn WITHOUT waiting: '' on success, else a speakable reason.
    The stream (SSE) carries completion, so the non-blocking /ask path
    returns the moment the prompt is truly in the session (critic finding:
    the 90s blocking wait invented the retry-vs-ack races)."""
    if DRYRUN:
        return ""
    tp = _target_transcript()
    if not tp:
        return "I can't find an open session on the Mac."
    core.log(f"call you: {text}")
    pending = core.get_pending_notice(_active_sid())
    if pending:
        return _handle_pending(text, pending)   # '' when yes was delivered
    from .talkd import bound_app
    if not inject.paste_text(text, send=True, expect_app=bound_app()):
        return ("I couldn't type into the session, the terminal isn't the "
                "focused window on your Mac. Bring it to the front, or check "
                "the screen isn't locked, then try again.")
    return ""


def _ask_session(text: str, turn_id: str = "") -> str:
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
    if turn_id:
        # Delivery ack on the event stream: the phone stops guessing whether
        # its prompt actually landed in the session.
        _broadcast({"type": "ack", "id": turn_id})
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
  voicebridge call page v8 (file: call_page_v13.html). Drop-in replacement
  for PAGE in vb/call.py. Embed as a RAW string (r prefix) so regex
  backslashes survive. This file contains no triple double-quote sequence
  anywhere, so it is safe inside a Python raw triple-quoted string.
  100% ASCII.

  CHANGES v7 to v8, the designer pass ("our own agent orb"), spec by spec:

    1. THE ORB IS NOW A SOFT ORGANIC GRADIENT SPHERE, not a flat disc.
       Inside a masked circle (#orb, overflow hidden, border-radius 50%)
       sit a deep-navy base gradient and FOUR drifting radial-gradient
       layers (.gl1 aqua-periwinkle, .gl2 teal, .gl3 violet, .glw a small
       warm peach accent), plus a conic sheen and a faint SVG-turbulence
       grain tile (data URI, static) for the watercolor-paper feel. Every
       layer animates ONLY transform (slow rotate/translate/scale loops at
       different periods, so the blobs never repeat visibly) and opacity;
       the speaking glow pulse moved off box-shadow onto a .halo div that
       animates opacity/scale. No canvas, no filters, no per-frame paint:
       compositor-only work, 60fps on mid-range phones. Layer colors are
       registered @property custom properties (--ga --gb --gc --gw --glow
       --ring), so state changes TWEEN over .9s instead of snapping.
    2. CALL CONTROL LIVES ON THE ORB. The old chat/mute/end button row is
       gone. A round chip (#callChip) overlaps the orb's bottom edge like
       a badge: idle = green call icon, tap to start (audio unlock still
       happens inside this tap, same as the old Start button); live = red
       hang-up icon, tap to end. Tapping the ORB while live toggles MUTE
       (and, while a reply is speaking, first tap still stops the voice,
       the v7 read-along feature, kept). Mute shows as a small mic-slash
       badge on the chip's shoulder AND the dimmed muted orb palette. The
       full-screen start overlay now appears only for microphone-denied
       recovery; a one-line #idleHint under the status carries the mic
       privacy note it used to show.
    3. LIVING STATES, one per call state, all transform/opacity only:
       - speaking: the gradient FLOWS, layer periods drop from ~20-40s to
         7-13s so the liquid visibly swirls; the halo breathes.
       - listening (user speaks): the whole orb pulses scale with the mic
         level via the existing --level pipeline; the RMS the barge
         monitor and the whisper listener already compute now feeds
         bumpLevel too (reuse, no new audio graph). When no fresh level
         has landed for 2s (native SpeechRecognition exposes none between
         results), #orbscale.steady falls back to a 1.2s heart-beat
         keyframe, toggled from the existing rAF level loop.
       - thinking: the sheen layer spins fast at higher opacity, a slow
         shimmer; ripples kept.
       - idle/ended: near-still, layers paused (ended) or barely
         drifting, soft breathe only.
       - prefers-reduced-motion: every orb animation off, static gradient;
         the status line's text states carry the information.
       BATTERY: body.bg (page hidden, set on visibilitychange) and
       body.home (home screen up) pause ALL orb animations via
       animation-play-state, so a pocketed phone burns nothing.
    4. VOICE / CHAT SEGMENTED CONTROL at the bottom (the only footer
       control now). Voice = the full orb experience. Chat = body.mode-chat:
       the SAME orb shrinks to 56px top-center (same element, same state
       animations, chip hidden at that size; tapping the mini orb still
       mutes), and the chat surface (transcript + the v7 composer) fills
       the screen between the mini orb and the segmented control,
       safe-area and keyboard (--kb) aware. This replaces the chat-sheet
       entry point on the call screen: the old chat button is gone and
       the UNREAD DOT lives on the Chat segment. The sheet code (half/full
       positions, drag, chevron) is kept but parked; in chat mode the drag
       and size chevron are disabled and the same #chatSheet element is
       re-anchored by CSS. Hardware back leaves chat mode first (history
       push/pop, same pattern the sheet used).
    5. VOICE PICKER in settings. Under the Phone / Natural source toggle,
       when Natural is active, three curated voice cards appear: Heart
       (af_heart, warm default), Bella (af_bella, bright conversational),
       Michael (am_michael, calm male). The choice persists in
       localStorage key vbvoice_name and rides along as {"voice": id} in
       EVERY /tts POST body (the server already accepts it); it takes
       effect on the next reply, and the hint text says so.
    Also in this pass: the permission panel's YES/NO buttons restyled to
    the new identity (mint gradient pill / quiet coral ghost), the start
    overlay glyph re-drawn as a mini gradient sphere, and the decide/toast
    anchors re-measured for the shorter footer. Turn engine, SSE stream,
    barge-in, stitching, permission relay, home screen, heartbeat, deep
    links, PWA manifest, and the Mac voice pipeline are untouched except
    where these features required a hook.

  CHANGES v6 to v7 (kept, condensed): three-position chat sheet
    (half/full/closed) with real drag + chevron; bulletproof "new
    messages" pill; typed composer that mutes the mic on focus and sends
    through the same turn engine; "Claude is working" typing row; copy
    button on every bubble; tap-the-orb / chat-bar button to stop the
    voice; unread dot for replies that land with the chat closed.
  CHANGES v5 to v6 (kept): voice-source toggle Phone / Mac(natural);
    Kokoro chunks via POST /tts played gapless through WebAudio with
    prefetch; automatic silent fallback to the phone voice on failure.
  CHANGES v4 to v5 (kept): barge-in (AEC + RMS monitor while replies
    speak); pause-tolerant stitching with follow-up windows; active vs
    inactive sessions everywhere; the long tail of iOS hardening (TTS
    warm-up in the tap, async voices, chunked synthesis with watchdogs,
    srDead whisper fallback, real /stt mimeType, standalone safe-area
    floors).

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
                      blocked on a permission decision. Checked immediately at
                      call start, then every ~4s while a turn is working and
                      ~8s while idle on a live call. Non-empty: speak it and
                      show the YES / NO pair (each sends POST /ask with text
                      "yes" or "no", the permission relay).
    GET  /sessions    {"sessions":[{"id":"...","name":"...","state":"...",
                      "current":bool,"active":bool,"pending":bool,
                      "last":"<one-line reply preview>","ago":<seconds since
                      last activity>}]}. "active" true means a live Claude
                      process owns the session; false means a leftover
                      transcript that can be read but NOT talked to. Renders
                      the HOME groups and the control room sheet.
    GET  /last?q=NAME {"reply":"..."} latest reply of the named session,
                      spoken WITHOUT switching the call; also fills the
                      read-only sheet for inactive sessions.
    POST /switch      body {"id":"..."} repoints the call at that session.
    GET  /chat        {"turns":[{"role":"user"|"assistant","text":"..."}]}
                      most recent last, cleaned for reading. Renders the chat
                      surface; refreshed after each completed turn and on
                      pull-to-refresh. Page degrades to its local transcript
                      when the endpoint is missing.
    POST /heartbeat   empty body, every 5s while the call is live. Freshness
                      tells the Mac to keep its own speakers quiet; the page
                      shows the "audio on phone" chip while beats land.
    POST /stt         audio blob returns {"text":"..."}, the whisper fallback
                      when native SpeechRecognition is absent or disabled.
                      The page sets Content-Type to the recorder's real
                      mimeType (audio/webm on Android, audio/mp4 on iOS).
    POST /tts         body {"text":"...","voice":"<kokoro voice id>"} returns
                      audio/wav synthesized by the Mac's Kokoro neural voice;
                      503 when Kokoro is unavailable; max ~600 chars per
                      request (the page sends <=300). "voice" comes from the
                      settings voice picker (localStorage vbvoice_name);
                      every failure path falls back to the phone voice.

  All inline, no external assets or CDNs. iOS Safari and Android Chrome
  quirks handled as listed above, plus the data-URI manifest that preserves
  ?k= (and &s= when the page was opened with one) on Add to Home Screen,
  safe-area insets, prefers-reduced-motion, and parked animations while the
  home screen is visible or the page is hidden (reduced battery burn).
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
<title>voicebridge</title>
<style>
/* Registered custom properties so state hue changes tween instead of snap.
   Old browsers skip @property and simply cut to the new color. These are
   the FOUR gradient-layer inks of the sphere plus glow and ring. */
@property --ga { syntax:'<color>'; inherits:true; initial-value:rgba(126,150,220,.55); }
@property --gb { syntax:'<color>'; inherits:true; initial-value:rgba(70,215,195,.30); }
@property --gc { syntax:'<color>'; inherits:true; initial-value:rgba(134,116,230,.34); }
@property --gw { syntax:'<color>'; inherits:true; initial-value:rgba(232,170,124,.18); }
@property --glow { syntax:'<color>'; inherits:true; initial-value:rgba(95,116,176,.30); }
@property --ring { syntax:'<color>'; inherits:true; initial-value:rgba(120,140,200,.5); }

* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { height:100%; overflow:hidden; overscroll-behavior:none; }
body {
  position:fixed; inset:0; margin:0;
  display:flex; flex-direction:column;
  background:#0a0d14; color:#e8ebf2;
  font-family:ui-rounded, -apple-system, "SF Pro Rounded", system-ui, "Segoe UI", Roboto, sans-serif;
  user-select:none; -webkit-user-select:none; touch-action:manipulation;
  transition:--ga .9s ease, --gb .9s ease, --gc .9s ease, --gw .9s ease,
    --glow .9s ease, --ring .9s ease;
  --ga:rgba(126,150,220,.55); --gb:rgba(70,215,195,.30);
  --gc:rgba(134,116,230,.34); --gw:rgba(232,170,124,.18);
  --glow:rgba(95,116,176,.30); --ring:rgba(120,140,200,.5);
  --danger:#e5484d; --amber:#e5a13d; --mint:#46d7c3;
  --dim:#96a0b5; --surface:#141926; --line:#232b3d;
}
/* the state palettes: aqua/teal listening, violet thinking, bright flow
   speaking, warm coral needs-you, ash muted, dusk ended */
body[data-state="listening"] { --ga:rgba(120,240,220,.60); --gb:rgba(47,174,157,.44); --gc:rgba(64,120,200,.32); --gw:rgba(232,170,124,.20); --glow:rgba(70,215,195,.38); --ring:rgba(90,225,205,.55); }
body[data-state="thinking"]  { --ga:rgba(183,166,255,.58); --gb:rgba(122,103,224,.44); --gc:rgba(56,150,205,.26); --gw:rgba(232,170,124,.15); --glow:rgba(140,120,235,.38); --ring:rgba(160,140,255,.55); }
body[data-state="speaking"]  { --ga:rgba(236,246,255,.72); --gb:rgba(127,180,240,.52); --gc:rgba(134,116,230,.38); --gw:rgba(240,190,150,.24); --glow:rgba(160,195,255,.50); --ring:rgba(180,205,255,.6); }
body[data-state="needs"]     { --ga:rgba(255,196,168,.62); --gb:rgba(224,117,95,.48); --gc:rgba(150,60,84,.40); --gw:rgba(240,170,120,.30); --glow:rgba(235,125,100,.40); --ring:rgba(255,150,125,.55); }
body[data-state="muted"]     { --ga:rgba(172,180,198,.32); --gb:rgba(96,106,126,.28); --gc:rgba(66,74,98,.28); --gw:rgba(190,170,158,.10); --glow:rgba(120,128,148,.20); --ring:rgba(130,138,158,.35); }
body[data-state="ended"]     { --ga:rgba(140,155,200,.28); --gb:rgba(72,96,136,.22); --gc:rgba(96,84,168,.20); --gw:rgba(210,170,140,.08); --glow:rgba(100,106,124,.14); --ring:rgba(110,118,138,.25); }

button { font:inherit; color:inherit; background:none; border:0; cursor:pointer; padding:0; }
button:focus-visible { outline:2px solid #9db9ff; outline-offset:3px; border-radius:14px; }

/* ==== home screen: the session list ==== */
#home {
  position:fixed; inset:0; z-index:45; background:#0a0d14;
  display:flex; flex-direction:column;
  opacity:1; transition:opacity .28s ease;
}
body:not(.home) #home { opacity:0; pointer-events:none; }
/* park the orb and controls while home is up: no animations burning battery */
body.home header, body.home main, body.home footer, body.home #decide { visibility:hidden; }
.hhead {
  flex:none; display:flex; align-items:center; gap:10px;
  padding:calc(env(safe-area-inset-top, 0px) + 24px) 22px 14px;
}
.hhead h1 { font-size:21px; font-weight:650; letter-spacing:-.01em; margin:0; }
#homeDot {
  width:8px; height:8px; border-radius:50%; background:#39435a; margin-top:2px;
  transition:background .4s ease, box-shadow .4s ease;
}
#homeDot.ok { background:var(--mint); box-shadow:0 0 8px rgba(70,215,195,.55); }
.hlist {
  flex:1; overflow-y:auto; -webkit-overflow-scrolling:touch;
  overscroll-behavior:contain; padding:2px 14px 12px; min-height:0;
}
.hgroup {
  font-size:11.5px; letter-spacing:.15em; text-transform:uppercase;
  color:#5b6479; margin:14px 8px 8px;
}
.hgroup:first-child { margin-top:4px; }
.hcard {
  display:block; width:100%; text-align:left;
  background:var(--surface); border:1px solid var(--line); border-radius:18px;
  padding:14px 16px 13px; margin-bottom:10px;
  transition:transform .1s ease, background .2s ease;
}
.hcard:active { transform:scale(.985); background:#182032; }
.hcard.needs { border-color:rgba(229,72,77,.45); }
.hcard.closed { opacity:.55; }
.hcard .r1 { display:flex; align-items:baseline; gap:10px; }
.hcard .r1 .n {
  flex:1; min-width:0; font-size:16.5px; font-weight:600;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}
.hcard .r1 .ago { flex:none; font-size:12.5px; color:#5b6479; font-variant-numeric:tabular-nums; }
.hcard .r2 { display:flex; align-items:center; gap:8px; margin-top:7px; min-height:20px; }
.hcard .r2 .lbl { font-size:12.5px; letter-spacing:.04em; color:var(--dim); }
.hcard .r2 .lbl.working { color:var(--amber); }
.hcard .r2 .lbl.ready { color:#58cdb9; }
.hcard .r2 .lbl.closed { color:#5b6479; }
.hcard .last {
  margin-top:7px; font-size:13.5px; color:#7d8699; line-height:1.4;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}
.hempty { color:var(--dim); font-size:14px; line-height:1.6; text-align:center; padding:52px 30px; }
.hfoot {
  flex:none; text-align:center; font-size:12px; color:#5b6479; margin:0;
  padding:8px 20px calc(env(safe-area-inset-bottom, 0px) + 14px);
}

/* ==== back chevron: call screen only ==== */
#backBtn {
  position:fixed; z-index:65;
  top:calc(env(safe-area-inset-top, 0px) + 12px);
  left:calc(env(safe-area-inset-left, 0px) + 12px);
  width:44px; height:44px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  background:rgba(255,255,255,.07); border:1px solid var(--line);
}
#backBtn:active { transform:scale(.92); }
#backBtn svg { width:20px; height:20px; }
body.home #backBtn { display:none; }

/* ==== settings gear (call screen only, mirrors the back chevron) ==== */
#setBtn {
  position:fixed; z-index:65;
  top:calc(env(safe-area-inset-top, 0px) + 12px);
  right:calc(env(safe-area-inset-right, 0px) + 12px);
  width:44px; height:44px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  background:rgba(255,255,255,.07); border:1px solid var(--line);
  color:#96a0b5;
}
#setBtn:active { transform:scale(.92); }
#setBtn svg { width:19px; height:19px; }
body.home #setBtn { display:none; }

/* settings sheet rows + segmented source control */
.setrow {
  display:flex; align-items:center; justify-content:space-between; gap:14px;
  padding:10px 4px 6px;
}
.setrow .setlbl { font-size:15.5px; color:#dfe4ee; }
.seg {
  display:flex; gap:0; border:1px solid var(--line); border-radius:999px;
  overflow:hidden; background:rgba(255,255,255,.04);
}
.seg button {
  min-height:44px; padding:10px 16px; font-size:14px; color:var(--dim);
}
.seg button.sel { background:#e8ebf2; color:#0a0d14; font-weight:600; }
.seg button:not(.sel):active { background:rgba(255,255,255,.08); }
/* v8: the curated Natural voices, three cards under the source toggle */
#voiceCards { display:flex; gap:8px; padding:8px 2px 4px; }
#voiceCards.off { display:none; }
.vcard {
  flex:1; display:flex; flex-direction:column; align-items:center; gap:3px;
  background:rgba(255,255,255,.04); border:1px solid var(--line);
  border-radius:14px; padding:13px 8px 11px; min-height:44px;
  transition:border-color .2s ease, background .2s ease;
}
.vcard:active { transform:scale(.97); }
.vcard.sel { border-color:rgba(70,215,195,.55); background:rgba(70,215,195,.08); }
.vcard .vn { font-size:15px; font-weight:600; color:#dfe4ee; }
.vcard.sel .vn { color:#7fe6d5; }
.vcard .vd { font-size:11.5px; color:var(--dim); text-align:center; line-height:1.35; }

/* iOS A2HS standalone quirk: with a black-translucent status bar the page
   sits under the clock, and some older devices report a 0 top inset in
   standalone mode. Give the top rows a hard floor so nothing hides. */
body.standalone .hhead { padding-top:max(calc(env(safe-area-inset-top, 0px) + 24px), 46px); }
body.standalone header { padding-top:max(calc(env(safe-area-inset-top, 0px) + 14px), 36px); }
body.standalone #backBtn { top:max(calc(env(safe-area-inset-top, 0px) + 12px), 34px); }
body.standalone #setBtn { top:max(calc(env(safe-area-inset-top, 0px) + 12px), 34px); }

/* ==== top: session pill + audio-ownership chip ==== */
header {
  padding:calc(env(safe-area-inset-top, 0px) + 14px) 16px 6px;
  display:flex; flex-direction:column; align-items:center; gap:8px;
}
#pill {
  display:flex; align-items:center; gap:8px;
  min-height:44px; padding:10px 16px; border-radius:999px;
  background:rgba(255,255,255,.055); border:1px solid var(--line);
  font-size:15px; color:#dfe4ee; max-width:62vw;
}
#pill .dot { width:8px; height:8px; border-radius:50%; background:var(--ring); flex:none;
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

/* ==== middle: the gradient sphere ==== */
main { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:30px; min-height:0; }
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

/* mic level drives the sphere scale via --level (0..1); .steady is the
   1.2s heart-beat fallback when no fresh level is available (native SR
   gives none between results) */
#orbscale { position:absolute; inset:0;
  transform:scale(calc(1 + var(--level, 0) * .16));
  transition:transform .14s linear;
}
@keyframes hb {
  0%, 100% { transform:scale(1); }
  14% { transform:scale(1.05); }
  28% { transform:scale(1.012); }
  42% { transform:scale(1.065); }
  62% { transform:scale(1); }
}
body[data-state="listening"] #orbscale.steady { animation:hb 1.2s ease-in-out infinite; }

/* the glow lives on its own layer and pulses via opacity/scale ONLY
   (an animated box-shadow would repaint every frame) */
.halo {
  position:absolute; inset:-22%; border-radius:50%; pointer-events:none;
  background:radial-gradient(circle, var(--glow) 0%, rgba(0,0,0,0) 62%);
  opacity:.85; transition:opacity .9s ease;
}
body[data-state="speaking"] .halo { animation:halopulse 1.6s ease-in-out infinite; }
@keyframes halopulse { 0%,100% { opacity:.5; transform:scale(1); } 50% { opacity:1; transform:scale(1.07); } }
body[data-state="ended"] .halo, body[data-state="muted"] .halo { opacity:.35; }

/* the masked sphere: deep navy base, drifting watercolor layers on top */
#orb {
  position:absolute; inset:0; border-radius:50%; overflow:hidden;
  background:radial-gradient(circle at 50% 40%, #1a2445 0%, #0c1122 60%, #070a13 100%);
  box-shadow:0 0 60px 4px var(--glow), inset 0 0 46px rgba(3,5,12,.5);
  animation:breathe 6.5s ease-in-out infinite;
}
@keyframes breathe { 0%,100% { transform:scale(1); } 50% { transform:scale(1.03); } }
.gl {
  position:absolute; inset:-30%; border-radius:50%; pointer-events:none;
  will-change:transform;
}
.gl1 { background:radial-gradient(46% 46% at 31% 30%, var(--ga) 0%, rgba(0,0,0,0) 72%);
  animation:drift1 21s ease-in-out infinite; }
.gl2 { background:radial-gradient(50% 50% at 69% 64%, var(--gb) 0%, rgba(0,0,0,0) 74%);
  animation:drift2 27s ease-in-out infinite; }
.gl3 { background:radial-gradient(42% 42% at 52% 80%, var(--gc) 0%, rgba(0,0,0,0) 75%);
  animation:drift3 34s ease-in-out infinite; }
.glw { background:radial-gradient(26% 26% at 76% 26%, var(--gw) 0%, rgba(0,0,0,0) 70%);
  animation:drift2 41s ease-in-out infinite reverse; }
/* seamless back-and-forth loops: 0% and 100% match, the middle sloshes */
@keyframes drift1 {
  0%, 100% { transform:rotate(0deg) translate3d(2%,-1%,0) scale(1); }
  50% { transform:rotate(170deg) translate3d(-3%,3%,0) scale(1.1); }
}
@keyframes drift2 {
  0%, 100% { transform:rotate(0deg) translate3d(-2%,2%,0) scale(1.04); }
  50% { transform:rotate(-150deg) translate3d(3%,-3%,0) scale(.94); }
}
@keyframes drift3 {
  0%, 100% { transform:rotate(0deg) translate3d(0,2%,0) scale(1); }
  50% { transform:rotate(120deg) translate3d(-2%,-3%,0) scale(1.12); }
}
/* speaking: the liquid FLOWS (shorter periods = visible swirl) */
body[data-state="speaking"] .gl1 { animation-duration:7s; }
body[data-state="speaking"] .gl2 { animation-duration:9s; }
body[data-state="speaking"] .gl3 { animation-duration:11s; }
body[data-state="speaking"] .glw { animation-duration:13s; }
/* listening: gently alive under the level pulse */
body[data-state="listening"] .gl1 { animation-duration:13s; }
body[data-state="listening"] .gl2 { animation-duration:17s; }
body[data-state="listening"] .gl3 { animation-duration:21s; }
/* ended: parked; the .9s color tween still dusks the sphere */
body[data-state="ended"] .gl, body[data-state="ended"] #orb { animation-play-state:paused; }
body[data-state="muted"] #orb { animation-play-state:paused; }
/* the sheen: a slow conic glint; THINKING spins it fast = the shimmer */
.sheen {
  position:absolute; inset:-25%; pointer-events:none;
  background:conic-gradient(from 0deg, rgba(0,0,0,0) 0 58%, rgba(255,255,255,.10) 72%, rgba(0,0,0,0) 86%);
  animation:sheenspin 26s linear infinite;
  opacity:.5; transition:opacity .9s ease;
}
body[data-state="thinking"] .sheen { animation-duration:6.5s; opacity:.95; }
body[data-state="speaking"] .sheen { animation-duration:11s; opacity:.55; }
body[data-state="ended"] .sheen, body[data-state="muted"] .sheen { opacity:.15; }
@keyframes sheenspin { to { transform:rotate(360deg); } }
/* faint grain: a static SVG-turbulence tile, the watercolor-paper tooth */
.grain {
  position:absolute; inset:0; pointer-events:none; opacity:.07;
  mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='0.6'/%3E%3C/svg%3E");
}

/* ==== the call chip: a badge on the sphere's bottom edge ==== */
#callChip {
  position:absolute; left:50%; bottom:-12px; transform:translateX(-50%);
  width:64px; height:64px; border-radius:50%; z-index:3;
  display:flex; align-items:center; justify-content:center;
  background:linear-gradient(180deg, #5ce8b8 0%, #2fae9d 100%); color:#06231f;
  border:3px solid #0a0d14;
  box-shadow:0 6px 22px rgba(0,0,0,.45);
  transition:background .3s ease, transform .1s ease;
}
#callChip:active { transform:translateX(-50%) scale(.93); }
#callChip svg { width:26px; height:26px; }
#callChip .ic-end { display:none; }
body.live #callChip { background:linear-gradient(180deg, #ef6a6e 0%, #c9363c 100%); color:#fff; }
body.live #callChip .ic-end { display:block; }
body.live #callChip .ic-call { display:none; }
/* mute badge on the chip's shoulder (the orb also dims via the muted palette) */
#muteMark {
  position:absolute; top:-5px; right:-5px; width:24px; height:24px;
  border-radius:50%; display:none; align-items:center; justify-content:center;
  background:#e8ebf2; color:#0a0d14; border:2px solid #0a0d14;
}
#muteMark svg { width:13px; height:13px; }
body.muted #muteMark { display:flex; }

#status {
  font-size:15px; letter-spacing:.42em; text-indent:.42em; /* balance tracking */
  text-transform:lowercase; color:var(--dim); min-height:22px; text-align:center;
  padding:0 24px; font-variant-numeric:tabular-nums;
}
body[data-state="needs"] #status { color:#f0a294; }
/* the idle hint replaces the old full-screen start overlay for normal
   starts; the overlay remains only for microphone-denied recovery */
#idleHint {
  display:none; font-size:13px; color:#6b7488; line-height:1.6;
  text-align:center; max-width:34ch; margin:-14px 0 0; padding:0 20px;
}
body[data-state="ended"]:not(.home):not(.mode-chat) #idleHint { display:block; }

/* ==== bottom: the Voice / Chat segmented control ==== */
footer {
  display:flex; align-items:center; justify-content:center;
  padding:8px 24px calc(env(safe-area-inset-bottom, 0px) + 22px);
}
.modeseg {
  display:flex; gap:4px; padding:4px; border-radius:999px;
  background:rgba(255,255,255,.05); border:1px solid var(--line);
}
.modeseg button {
  position:relative; min-width:108px; min-height:44px; border-radius:999px;
  display:flex; align-items:center; justify-content:center; gap:7px;
  font-size:15px; color:var(--dim); letter-spacing:.02em;
  transition:background .2s ease, color .2s ease;
}
.modeseg button svg { width:16px; height:16px; }
.modeseg button.sel { background:#e8ebf2; color:#0a0d14; font-weight:650; }
.modeseg button:not(.sel):active { background:rgba(255,255,255,.08); }
/* v8: the unread dot lives on the Chat segment now */
#segChat.unread::after {
  content:''; position:absolute; top:7px; right:12px;
  width:8px; height:8px; border-radius:50%; background:var(--mint);
  box-shadow:0 0 7px rgba(70,215,195,.8);
}
#segChat.sel.unread::after { background:#1f8a79; box-shadow:none; }

/* ==== decision panel: the permission relay, restyled to the new look ==== */
#decide {
  position:fixed; left:14px; right:14px; z-index:35;
  bottom:calc(env(safe-area-inset-bottom, 0px) + 104px);
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
#decide .row button { flex:1; min-height:56px; border-radius:999px;
  font-size:17px; font-weight:650; letter-spacing:.04em; }
#decide .row button:active { transform:scale(.97); }
#yesBtn { background:linear-gradient(180deg, #5ce8b8 0%, #2fae9d 100%);
  color:#06231f; box-shadow:0 4px 18px rgba(70,215,195,.22); }
#noBtn { border:1.5px solid rgba(229,72,77,.55); color:#ff9a9e;
  background:rgba(229,72,77,.08); }

/* ==== toast: background session news ==== */
#toast {
  position:fixed; left:50%; z-index:36;
  bottom:calc(env(safe-area-inset-bottom, 0px) + 112px);
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

/* read-only sheet for closed (inactive) sessions */
#closedSheet .closedtext {
  font-size:15px; line-height:1.55; color:#c7cdd9; white-space:pre-wrap;
  word-break:break-word; user-select:text; -webkit-user-select:text;
  margin:0; padding:2px 4px 8px;
}

/* chat: the v7 three-position sheet CSS is KEPT (half here, .full below);
   v8's chat MODE re-anchors this same element full-height under the mini
   orb via body.mode-chat rules further down. --kb lifts it over the
   on-screen keyboard (set from visualViewport while the composer is
   focused). */
#chatSheet {
  left:10px; right:10px;
  bottom:calc(env(safe-area-inset-bottom, 0px) + 96px + var(--kb, 0px));
  border:1px solid var(--line); border-radius:20px;
  height:52vh;
  height:min(52dvh, calc(100dvh - var(--kb, 0px) - 150px));
  max-height:none; padding-bottom:10px; z-index:30;
  transform:translateY(calc(100% + 140px));
  transition:transform .3s cubic-bezier(.3,.9,.3,1), top .3s ease,
    left .3s ease, right .3s ease, bottom .3s ease, height .3s ease,
    border-radius .3s ease, padding .3s ease;
}
#chatSheet.open { transform:translateY(0); }
#chatSheet.dragging { transition:none; }
/* FULL SCREEN (legacy sheet position, kept): chat fills everything. */
#chatSheet.full {
  left:0; right:0; bottom:var(--kb, 0px);
  height:100vh;
  height:calc(100dvh - var(--kb, 0px));
  border-radius:0; border:0; z-index:62;
  padding-top:calc(env(safe-area-inset-top, 0px) + 8px);
  padding-bottom:calc(env(safe-area-inset-bottom, 0px) + 10px);
}
body.chat-full header, body.chat-full main, body.chat-full footer,
body.chat-full #backBtn, body.chat-full #setBtn { visibility:hidden; }
body.chat-full #decide { z-index:66; }
body.chat-full #toast { z-index:66; }
body.standalone #chatSheet.full {
  padding-top:max(calc(env(safe-area-inset-top, 0px) + 8px), 38px); }
#chatHead { flex:none; touch-action:none; }
.chatbar { display:flex; align-items:center; gap:10px; padding:0 2px 8px; }
.chatbar h2 { margin:0; flex:1; min-width:0; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; display:block; }
#chatSizeBtn, #hushBtn {
  flex:none; width:38px; height:38px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  background:rgba(255,255,255,.06); border:1px solid var(--line);
  color:#96a0b5;
}
/* stop-the-voice lives in the chat bar too (in chat mode the orb is tiny);
   only shown while a reply speaks */
#hushBtn { display:none; color:#9db9ff; border-color:rgba(140,170,240,.35); }
body[data-state="speaking"] #hushBtn { display:flex; }
#hushBtn:active { transform:scale(.92); }
#hushBtn svg { width:18px; height:18px; }
#chatSizeBtn:active { transform:scale(.92); }
#chatSizeBtn svg { width:18px; height:18px; transition:transform .25s ease; }
#chatSheet.full #chatSizeBtn svg { transform:rotate(180deg); }
#chatSheet.full .chatbar h2 {
  font-size:16px; font-weight:600; text-transform:none;
  letter-spacing:.01em; color:#dfe4ee;
}
/* composer: type instead of talking (meetings, quiet rooms) */
.composer { flex:none; display:flex; align-items:center; gap:8px; padding:8px 2px 0; }
#composeIn {
  flex:1; min-width:0; min-height:44px;
  background:#0e1420; border:1px solid var(--line); border-radius:999px;
  color:#e8ebf2; padding:11px 16px; font-size:15px; font-family:inherit;
  user-select:text; -webkit-user-select:text;
}
#composeIn::placeholder { color:#5b6479; }
#sendBtn {
  flex:none; width:44px; height:44px; border-radius:50%;
  background:var(--mint); color:#06231f;
  display:flex; align-items:center; justify-content:center;
}
#sendBtn:active { transform:scale(.92); }
#sendBtn svg { width:20px; height:20px; }
/* typing indicator: "Claude is working" row while a turn is in flight */
.typing {
  align-self:flex-start; display:flex; align-items:center; gap:5px;
  padding:10px 14px; border-radius:18px; border-bottom-left-radius:6px;
  border:1px solid var(--line); background:rgba(255,255,255,.04);
  color:var(--dim); font-size:13.5px;
}
.typing .tl { margin-right:4px; }
.typing .td { width:6px; height:6px; border-radius:50%; background:var(--dim);
  animation:softpulse 1.2s ease-in-out infinite; }
.typing .td:nth-child(3) { animation-delay:.2s; }
.typing .td:nth-child(4) { animation-delay:.4s; }
/* copy button: floats top-right INSIDE the bubble */
.copybtn {
  float:right; width:30px; height:30px; margin:-4px -8px 2px 8px;
  border-radius:9px; display:flex; align-items:center; justify-content:center;
  color:#8a94a8; background:rgba(10,13,20,.3);
}
.copybtn:active { transform:scale(.9); color:#e8ebf2; }
.copybtn svg { width:15px; height:15px; }
#jumpBtn.hidden, #homeFilter.hidden { display:none; }
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
.bub .bwrap { display:block; }
.bub.clamp .bwrap { max-height:19em; overflow:hidden;
  -webkit-mask-image:linear-gradient(#000 72%, transparent);
  mask-image:linear-gradient(#000 72%, transparent); }
.showmore { display:block; margin-top:6px; background:none; border:0;
  color:#46d7c3; font-size:13px; padding:2px 0; letter-spacing:.03em; }
.cblk { background:#0a0e18; border:1px solid var(--line); border-radius:10px;
  padding:10px 12px; margin:8px 0; font:12.5px/1.45 ui-monospace,Menlo,monospace;
  overflow-x:auto; white-space:pre; max-width:100%; }
.ichip { background:rgba(255,255,255,.09); border-radius:5px; padding:1px 5px;
  font:.92em ui-monospace,Menlo,monospace; }
#jumpBtn { position:absolute; bottom:74px; left:50%; transform:translateX(-50%);
  z-index:5; background:#182032; color:#46d7c3; border:1px solid rgba(70,215,195,.35);
  border-radius:999px; padding:7px 14px; font-size:13px; }
#chatSheet.full #jumpBtn { bottom:calc(74px + env(safe-area-inset-bottom, 0px)); }
#homeFilter { width:100%; box-sizing:border-box; margin:0 0 10px;
  background:#131a29; border:1px solid var(--line); border-radius:12px;
  color:#e5ecf7; padding:10px 14px; font-size:15px; }
.hcard .r1 .mono { flex:none; width:26px; height:26px; border-radius:8px;
  display:inline-flex; align-items:center; justify-content:center;
  font-size:13px; font-weight:700; color:#e9eef8; align-self:center; }
#chatLines .empty { color:var(--dim); font-size:14px; align-self:center; padding:18px 8px; text-align:center; line-height:1.55; }

/* ==== v8 CHAT MODE: mini orb top-center, transcript fills the screen ==== */
body.mode-chat main { flex:none; gap:0; padding:6px 0 0; }
body.mode-chat #orbzone { width:56px; }
body.mode-chat #status, body.mode-chat #idleHint { display:none; }
body.mode-chat #callChip { display:none; }
body.mode-chat #chatSheet {
  left:10px; right:10px; height:auto; max-height:none;
  top:calc(env(safe-area-inset-top, 0px) + 138px);
  bottom:calc(env(safe-area-inset-bottom, 0px) + 88px + var(--kb, 0px));
  z-index:25; border-radius:18px;
}
body.standalone.mode-chat #chatSheet {
  top:max(calc(env(safe-area-inset-top, 0px) + 138px), 158px);
}
/* no drag positions in chat mode: the mode owns the geometry */
body.mode-chat #chatHead .grab, body.mode-chat #chatSizeBtn { display:none; }

/* control room cards */
.card {
  display:flex; align-items:center; gap:6px; border-radius:16px;
  border:1px solid transparent; margin-bottom:2px; padding-right:6px;
}
.card.current { background:rgba(255,255,255,.05); border-color:var(--line); }
.card.closed { opacity:.55; }
.card .main {
  flex:1; min-width:0; display:flex; align-items:center; gap:12px;
  text-align:left; padding:13px 8px 13px 12px; min-height:60px; border-radius:14px;
}
.card .main:active { background:rgba(255,255,255,.05); }
.sdot { width:10px; height:10px; border-radius:50%; background:var(--mint); flex:none; }
.sdot.working { background:var(--amber); animation:softpulse 1.6s ease-in-out infinite; }
.sdot.needs { background:var(--danger); }
.sdot.closed { background:#39435a; }
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

/* ==== overlays: mic-denied recovery / switching ==== */
.overlay {
  position:fixed; inset:0; z-index:60; background:#0a0d14;
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  gap:18px; text-align:center; padding:32px 28px; transition:opacity .35s ease;
}
.overlay.hidden { opacity:0; pointer-events:none; }
.overlay .glyph { width:74px; height:74px; border-radius:50%;
  background:
    radial-gradient(46% 46% at 30% 32%, rgba(126,150,220,.7) 0%, rgba(0,0,0,0) 72%),
    radial-gradient(50% 50% at 68% 62%, rgba(70,215,195,.4) 0%, rgba(0,0,0,0) 74%),
    radial-gradient(30% 30% at 74% 28%, rgba(232,170,124,.3) 0%, rgba(0,0,0,0) 70%),
    radial-gradient(circle at 50% 42%, #1a2445 0%, #0c1122 60%, #070a13 100%);
  box-shadow:0 0 50px 4px rgba(95,116,176,.35); animation:breathe 6.5s ease-in-out infinite; }
.overlay h1 { font-size:24px; font-weight:650; margin:6px 0 0; letter-spacing:-.01em; }
.overlay p { font-size:15.5px; line-height:1.6; color:var(--dim); max-width:34ch; margin:0; }
.overlay .cta {
  margin-top:10px; min-height:58px; padding:16px 42px; border-radius:999px;
  background:linear-gradient(180deg, #5ce8b8 0%, #2fae9d 100%);
  color:#06231f; font-size:17px; font-weight:650;
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

/* ==== battery: page hidden or home up = every orb animation parked ==== */
body.bg .gl, body.bg .sheen, body.bg .halo, body.bg .grain,
body.bg #orb, body.bg #orbscale, body.bg .ripple,
body.home .gl, body.home .sheen, body.home .halo,
body.home #orb, body.home #orbscale, body.home .ripple {
  animation-play-state:paused;
}

/* ==== reduced motion: static gradient, state legible via color and text ==== */
@media (prefers-reduced-motion: reduce) {
  #orb, .gl, .glw, .sheen, .halo, #orbscale, .overlay .glyph { animation:none !important; }
  body[data-state="thinking"] .ripple,
  body[data-state="needs"] .ripple { animation:none; opacity:.35; transform:scale(1.25); }
  #orbscale { transition:none; transform:none; }
  .sheet, #decide, #toast, #home, .hcard { transition:none; }
  #switchOverlay .spin { animation:none; border-top-color:var(--line); }
  .sdot.working, .badge.needs, #chip .cdot, #decide .eyebrow .ddot { animation:none; }
  .typing .td { animation:none; }
  #chatSizeBtn svg { transition:none; }
}
</style></head><body data-state="ended" class="home">

<section id="home" aria-label="Sessions">
  <div class="hhead">
    <h1>voicebridge</h1>
    <span id="homeDot" role="status" aria-label="relay connecting"></span>
  </div>
  <input id="homeFilter" class="hidden" type="search" placeholder="filter sessions"
         autocapitalize="none" autocorrect="off">
  <div class="hlist" id="homeList"></div>
  <p class="hfoot">Tap an active session to call it. Earlier sessions are read-only.</p>
</section>

<button id="backBtn" aria-label="End the call and go back to sessions">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg>
</button>

<button id="setBtn" aria-haspopup="dialog" aria-label="Call settings">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="3.2"/>
    <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.09a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.09a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z"/>
  </svg>
</button>

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
  <div id="orbzone" role="button" tabindex="0"
       aria-label="Orb. While live, tap to mute or unmute; while a reply is speaking, tap to stop the voice.">
    <div class="ripple"></div><div class="ripple"></div><div class="ripple"></div>
    <div id="orbscale">
      <div class="halo" aria-hidden="true"></div>
      <div id="orb" aria-hidden="true">
        <div class="gl gl1"></div>
        <div class="gl gl2"></div>
        <div class="gl gl3"></div>
        <div class="gl glw"></div>
        <div class="sheen"></div>
        <div class="grain"></div>
      </div>
    </div>
    <button id="callChip" aria-label="Start call">
      <svg class="ic-call" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M6.62 10.79a15.05 15.05 0 0 0 6.59 6.59l2.2-2.2a1 1 0 0 1 1.02-.24 11.36 11.36 0 0 0 3.57.57 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1 11.36 11.36 0 0 0 .57 3.57 1 1 0 0 1-.25 1.02z"/>
      </svg>
      <svg class="ic-end" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M12 9.5c-3.3 0-6.4 1-8.9 2.9-.6.4-.8 1.2-.5 1.9l.9 1.9c.3.7 1.1 1 1.8.8l2.6-.9c.6-.2 1-.8 1-1.4v-1.3c2-.6 4.2-.6 6.2 0v1.3c0 .6.4 1.2 1 1.4l2.6.9c.7.2 1.5-.1 1.8-.8l.9-1.9c.3-.7.1-1.5-.5-1.9A14.6 14.6 0 0 0 12 9.5z"/>
      </svg>
      <span id="muteMark" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
          stroke-linecap="round" stroke-linejoin="round">
          <rect x="9" y="3" width="6" height="12" rx="3" fill="currentColor" stroke="none"/>
          <path d="M4 4l16 16"/>
        </svg>
      </span>
    </button>
  </div>
  <div id="status" role="status" aria-live="polite">call ended</div>
  <p id="idleHint">Tap the call button to start. Your phone will ask for the
     microphone; audio goes only to your Mac. Say "end call" any time.</p>
</main>

<footer>
  <div class="modeseg" role="tablist" aria-label="Voice or chat mode">
    <button id="segVoice" class="sel" role="tab" aria-selected="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="9" y="3" width="6" height="12" rx="3" fill="currentColor" stroke="none"/>
        <path d="M6 12a6 6 0 0 0 12 0"/><path d="M12 18v3"/>
      </svg>
      Voice
    </button>
    <button id="segChat" role="tab" aria-selected="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9.2 9.2 0 0 1-3.9-.9L3 20l1-4.1a8.2 8.2 0 0 1-1-4.4 8.4 8.4 0 0 1 9-8.4 8.4 8.4 0 0 1 9 8.4z"/>
      </svg>
      Chat
    </button>
  </div>
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
  <div id="chatHead">
    <div class="grab" aria-hidden="true"></div>
    <div class="chatbar">
      <h2 id="chatTitle">Chat</h2>
      <button id="hushBtn" aria-label="Stop the voice, keep reading">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M11 5 6.5 8.5H3v7h3.5L11 19z" fill="currentColor" stroke="none"/>
          <path d="M15 9.5l5 5"/><path d="M20 9.5l-5 5"/>
        </svg>
      </button>
      <button id="chatSizeBtn" aria-label="Expand chat to full screen">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
          stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 15l6-6 6 6"/></svg>
      </button>
    </div>
  </div>
  <button id="jumpBtn" class="hidden" aria-label="Jump to newest">new messages</button>
  <div class="scroll" id="chatScroll">
    <div id="pullHint">pull to refresh</div>
    <div id="chatLines"><p class="empty">The conversation with this session appears here.</p></div>
  </div>
  <div class="composer">
    <input id="composeIn" type="text" placeholder="type instead of talking"
           autocapitalize="sentences" autocomplete="off" autocorrect="on"
           enterkeyhint="send" aria-label="Type a prompt to the session">
    <button id="sendBtn" aria-label="Send typed prompt">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 19V5"/><path d="M5 12l7-7 7 7"/>
      </svg>
    </button>
  </div>
</section>

<section class="sheet" id="sessSheet" role="dialog" aria-label="Control room">
  <div class="grab" aria-hidden="true"></div>
  <h2>Control room <span class="count" id="sessCount"></span></h2>
  <div class="scroll" id="sessList"></div>
  <p class="hint">Tap a session to move the call there. The speaker icon plays its last reply without switching. Closed sessions are read-only.</p>
</section>

<section class="sheet" id="closedSheet" role="dialog" aria-label="Closed session">
  <div class="grab" aria-hidden="true"></div>
  <h2>Closed session <span class="count" id="closedName"></span></h2>
  <div class="scroll"><p class="closedtext" id="closedBody"></p></div>
  <p class="hint">This session is closed, you can read it but not talk to it.</p>
</section>

<section class="sheet" id="setSheet" role="dialog" aria-label="Call settings">
  <div class="grab" aria-hidden="true"></div>
  <h2>Settings</h2>
  <div class="setrow">
    <span class="setlbl">Voice</span>
    <div class="seg" role="radiogroup" aria-label="Voice source">
      <button id="voicePhoneBtn" role="radio" aria-checked="true">Phone</button>
      <button id="voiceMacBtn" role="radio" aria-checked="false">Natural voice</button>
    </div>
  </div>
  <div id="voiceCards" role="radiogroup" aria-label="Natural voice">
    <button class="vcard" data-v="af_heart" role="radio" aria-checked="true">
      <span class="vn">Heart</span><span class="vd">warm, the default</span>
    </button>
    <button class="vcard" data-v="af_bella" role="radio" aria-checked="false">
      <span class="vn">Bella</span><span class="vd">bright, conversational</span>
    </button>
    <button class="vcard" data-v="am_michael" role="radio" aria-checked="false">
      <span class="vn">Michael</span><span class="vd">calm, male</span>
    </button>
  </div>
  <p class="hint">Natural voice is the Kokoro neural voice from your Mac; the phone plays
     it, your Mac just makes the audio. Pick who speaks above; a new voice takes effect
     on the next reply. If the Mac is unreachable the phone voice takes over automatically.</p>
</section>

<div class="overlay hidden" id="startOverlay">
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
const PARAMS = new URLSearchParams(location.search);
const K = PARAMS.get('k') || '';
const S = PARAMS.get('s') || '';   /* deep link: skip home, land on this session */
const $ = id => document.getElementById(id);
const statusEl=$('status'), pillName=$('pillName'),
      segVoice=$('segVoice'), segChat=$('segChat'), callChip=$('callChip'),
      orbScaleEl=$('orbscale'),
      chatLines=$('chatLines'), chatScroll=$('chatScroll'), jumpBtn=$('jumpBtn'),
      chatHead=$('chatHead'), chatTitle=$('chatTitle'), chatSizeBtn=$('chatSizeBtn'),
      composeIn=$('composeIn'), sendBtn=$('sendBtn'),
      pullHint=$('pullHint'), chipEl=$('chip'), decideEl=$('decide'),
      decideQ=$('decideQ'), toastEl=$('toast'), toastText=$('toastText'),
      sessList=$('sessList'), sessCount=$('sessCount'),
      homeList=$('homeList'), homeDot=$('homeDot'),
      closedName=$('closedName'), closedBody=$('closedBody');
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const TTS = 'speechSynthesis' in window;

let live=false, muted=false, state='ended';
let gen=0;                 // listen generation: bump to invalidate in-flight mic work
let turnId=0, turnActive=false;   // turn generation: bump to invalidate stale turn work
let rec=null, recActive=false, media=null, audioCtx=null;
let wakeLock=null, speechCancelled=false;
let onHome = !S;           // which screen is up; body.home mirrors this
let lastRoster = null;     // latest /sessions list, re-rendered on goHome
let currentSid = S || '';  // which session the call screen is pointed at
let wantStartName = !!S;   // deep link: name the start overlay once roster lands
/* iOS: webkitSpeechRecognition needs Siri and Dictation enabled; when it is
   off the recognizer errors service-not-allowed. After 2 consecutive such
   errors we stop retrying for the REST OF THIS SESSION and use the whisper
   path (srDead), instead of an infinite error-retry loop. */
let srDead=false, srFails=0;
/* voice source: 'phone' = browser SpeechSynthesis (default fallback),
   'mac' = the Mac's Kokoro neural voice via POST /tts. Persisted. */
let voicePref='mac';
try{
  var _vp = localStorage.getItem('vbvoice');
  /* Natural (Kokoro) is the DEFAULT on iOS and Android; 'phone' only when
     the user explicitly chose it. Unreachable Kokoro still auto-falls back
     to the phone voice mid-reply, so the default is safe everywhere. */
  voicePref = (_vp === 'phone') ? 'phone' : 'mac';
}catch(e){ voicePref = 'mac'; }
/* v8: WHICH Kokoro voice speaks. Three curated ids, picked in settings,
   persisted, sent as {"voice": id} in every /tts body. A switch applies
   from the next reply (no mid-reply engine restart). */
const VOICE_IDS = ['af_heart', 'af_bella', 'am_michael'];
let voiceName = 'af_heart';
try{
  var _vn = localStorage.getItem('vbvoice_name');
  if(VOICE_IDS.indexOf(_vn) >= 0) voiceName = _vn;
}catch(e){}
let macDead=false;        // 2 consecutive failed Mac replies: stop trying this session
let macFails=0;           // consecutive reply-level /tts failures
let macToastShown=false;  // the fallback toast shows once, then falls back silently
let macSrc=null;          // the AudioBufferSourceNode currently playing Mac audio
/* chat sheet state (v7 code kept): three positions (closed / half / full).
   v8's chat MODE reuses chatOpenState with the geometry owned by CSS. */
let chatOpenState=false, chatPos='half', chatPosPref='half', chatHist=false;
let typingMute=false;
let renderedTurns=0;      // turns currently in the DOM (pill append detection)
/* v8: which mode the segmented control is in; body.mode-chat mirrors it */
let mode='voice', modeHist=false;

/* rows without "active" come from an older server: treat them as live */
function isActiveSess(s){ return !s || s.active !== false; }

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
function jpost(path, body){
  return fetch(urlFor(path), {
    method:'POST', headers:{ 'Content-Type':'application/json' },
    body:JSON.stringify(body) });
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* mic-level meter drives the orb scale via --level (0..1). Every RMS the
   page already computes (whisper listener, barge monitor) funnels through
   bumpLevel, so the sphere pulses WITH the user's voice. When listening
   but no fresh level has landed for 2s (native SR emits none between
   results), .steady switches the pulse to a 1.2s heart-beat keyframe. */
let level=0, levelTarget=0, lastLevelAt=0;
function bumpLevel(v){ levelTarget = Math.max(levelTarget, v); lastLevelAt = Date.now(); }
(function levelLoop(){
  level += (levelTarget - level) * .25;
  levelTarget *= .9;
  if(level < .004) level = 0;
  document.documentElement.style.setProperty('--level', level.toFixed(3));
  orbScaleEl.classList.toggle('steady',
    state === 'listening' && Date.now() - lastLevelAt > 2000);
  requestAnimationFrame(levelLoop);
})();

/* ============================================================ PWA polish */
/* Data-URI manifest so Add to Home Screen keeps the ?k= secret in start_url
   (the server's /manifest.json points start_url at "/" and would land on 401).
   location.href also carries &s= when the page was deep-linked, so an icon
   added from a deep link reopens straight into that session; a plain open
   has no s and the icon lands on home. */
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

/* iOS A2HS: standalone display reports via navigator.standalone (classic
   Safari) or the display-mode media query (spec). Tag the body so CSS can
   give the top rows a safe-area floor where the inset reports 0. */
if(window.navigator.standalone === true ||
   (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)){
  document.body.classList.add('standalone');
}

/* Screen Wake Lock: not present on older iOS. Deliberately NO hidden-video
   keep-awake hack; the screen may dim, and the visibilitychange handler
   below recovers everything (TTS resume, lock re-arm, mic restart) when
   the user returns. */
async function acquireWakeLock(){
  try {
    if('wakeLock' in navigator){
      wakeLock = await navigator.wakeLock.request('screen');
      /* the OS releases the lock on lock/background; forget the handle so
         visibilitychange knows to re-arm a fresh one */
      wakeLock.addEventListener('release', () => { wakeLock = null; });
    }
  }
  catch(e){ /* low battery or unsupported: fine, the call still works */ }
}
function releaseWakeLock(){ try{ wakeLock && wakeLock.release(); }catch(e){} wakeLock=null; }

/* ============================================================ speech out */
let voices=[];
/* iOS: getVoices() is EMPTY until voiceschanged fires (voices load async).
   Refresh the list on that event; pickVoice runs per chunk, so the first
   reply after the list lands automatically gets the good voice. */
function refreshVoices(){ if(TTS) voices = speechSynthesis.getVoices(); }
if(TTS) speechSynthesis.onvoiceschanged = refreshVoices;
refreshVoices();

function pickVoice(){
  const en = voices.filter(v => (v.lang||'').toLowerCase().startsWith('en'));
  return en.find(v => /siri|premium|enhanced|natural|neural/i.test(v.name)) || en[0] || voices[0];
}
/* Mobile browsers cut long utterances (a known engine bug), so split into
   sentence-sized chunks (<=170 chars, well under the 200-char danger zone)
   and chain them; an iOS synthesis stall then loses at most one chunk. */
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
/* Cancel EVERY output path: local synthesis AND the Mac-voice WebAudio
   pipeline (barge-in relies on this killing whichever one is speaking). */
function stopSpeaking(){
  speechCancelled = true;
  if(TTS) speechSynthesis.cancel();
  stopMacAudio();
  stopBarge();
}
/* iOS unlocks speechSynthesis only inside a user gesture: speak a silent
   warm-up utterance AND create/resume the AudioContext in the start tap,
   before any await breaks the gesture context. */
function unlockAudio(){
  try{
    if(TTS){
      const w = new SpeechSynthesisUtterance(' ');
      w.volume = 0;
      speechSynthesis.speak(w);
    }
  }catch(e){}
  try{
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if(audioCtx.state === 'suspended') audioCtx.resume();  /* starts suspended on iOS */
  }catch(e){}
}
/* dispatcher: every caller keeps using say(); the voice setting decides
   which engine speaks. Mac voice out of service falls through to the phone. */
function say(text, done){
  if(voicePref === 'mac' && !macDead){ sayMac(text, done); return; }
  sayPhone(text, done);
}
function sayPhone(text, done){
  if(!TTS){ done && done(); return; }
  speechSynthesis.cancel();
  speechCancelled = false;
  const parts = chunkText(text, 170);
  let i = 0;
  /* iOS PAUSES synthesis when the screen locks or the tab hides; besides the
     visibilitychange resume, this pump pokes it back every 1.5s so a stall
     mid-reply self-heals even without a visibility event. */
  const pump = setInterval(() => {
    if(speechCancelled){ clearInterval(pump); return; }
    try{ if(speechSynthesis.paused) speechSynthesis.resume(); }catch(e){}
  }, 1500);
  (function next(){
    if(speechCancelled){ clearInterval(pump); return; }
    if(i >= parts.length){ clearInterval(pump); done && done(); return; }
    const chunk = parts[i++];
    const u = new SpeechSynthesisUtterance(chunk);
    const v = pickVoice(); if(v) u.voice = v;
    u.rate = 1.0;
    /* iOS sometimes drops an utterance with NEITHER end nor error: a
       per-chunk watchdog advances the chain so the call never wedges in
       the speaking state. speak() queues, so a late finish cannot overlap. */
    let advanced = false;
    const advance = () => { if(advanced) return; advanced = true; setTimeout(next, 50); };
    u.onend = advance;
    u.onerror = advance;
    setTimeout(advance, 3000 + chunk.length * 90);
    speechSynthesis.speak(u);
  })();
}

/* ---- the Mac (Kokoro) voice pipeline ----
   Sentence chunks of <=300 chars each POST to /tts and come back as WAV.
   Playback is WebAudio (decodeAudioData + AudioBufferSourceNode): gapless
   chaining is reliable and, unlike a fresh HTMLAudioElement per chunk, it
   plays fine on iOS because the AudioContext was unlocked in the start tap.
   Chunk N+1 is PREFETCHED while chunk N plays, so the network hides behind
   the audio. LATENCY: the first chunk still costs a round trip plus Kokoro
   synthesis, roughly a second slower than local speechSynthesis; that is
   the accepted tradeoff for the natural voice. Failures (non-200, 4s
   timeout, decode error) fall back to the phone voice for the REST of the
   reply, silently except for a one-time toast; two consecutive failed
   replies set macDead and the session stays on the phone voice.
   v8: every request carries the picked voice id (settings voice cards). */
function stopMacAudio(){
  if(macSrc){ try{ macSrc.onended = null; macSrc.stop(); }catch(e){} macSrc = null; }
}
function fetchTts(text){
  const ctl = new AbortController();
  const tm = setTimeout(() => ctl.abort(), 4000);   // 4s per chunk, then phone voice
  const p = fetch(urlFor('/tts'), {
    method:'POST', headers:{ 'Content-Type':'application/json' },
    body:JSON.stringify({ text: text, voice: voiceName }), signal: ctl.signal })
  .then(r => {
    if(!r.ok) throw new Error('tts ' + r.status);   // 503 = Kokoro unavailable
    return r.arrayBuffer();
  })
  .then(ab => new Promise((res, rej) => {
    /* callback form: older iOS Safari has no promise decodeAudioData */
    audioCtx.decodeAudioData(ab, res, rej);
  }))
  .finally(() => clearTimeout(tm));
  p.catch(() => {});   // mark handled; the awaiter still sees the rejection
  return p;
}
function playBuf(buf){
  return new Promise(res => {
    const s = audioCtx.createBufferSource();
    s.buffer = buf;
    s.connect(audioCtx.destination);
    macSrc = s;
    s.onended = () => { if(macSrc === s) macSrc = null; res(); };
    s.start();
  });
}
function sayMac(text, done){
  try{
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if(audioCtx.state === 'suspended') audioCtx.resume();
  }catch(e){ sayPhone(text, done); return; }
  speechCancelled = false;
  const parts = chunkText(text, 300);   // server caps around 600; stay well under
  if(!parts.length){ done && done(); return; }
  let idx = 0;
  let pending = fetchTts(parts[0]);
  (async function pump(){
    while(!speechCancelled && idx < parts.length){
      let buf = null;
      try{ buf = await pending; }catch(e){ buf = null; }
      if(speechCancelled) return;       // barged or ended while fetching
      if(!buf){
        /* fall back to the phone voice for what remains of this reply */
        macFails++;
        if(macFails >= 2) macDead = true;
        if(!macToastShown){
          macToastShown = true;
          toast('Mac voice unavailable, using phone voice');
        }
        sayPhone(parts.slice(idx).join(' '), done);
        return;
      }
      macFails = 0;
      /* prefetch the next chunk while this one plays: the gapless pipeline */
      pending = (idx + 1 < parts.length) ? fetchTts(parts[idx + 1]) : null;
      await playBuf(buf);
      idx++;
    }
    if(!speechCancelled && done) done();
  })();
}
/* A short interjection (hear-last, pending question) that then returns to
   whatever the call was doing, without ending the working turn. */
function speakAside(text){
  stopListening(); stopSpeaking();
  setState('speaking');
  if(live && !decisionOpen) startBarge();   /* asides are interruptible too */
  say(text, () => { stopBarge(); resumeAfterSpeech(); });
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
   and restart the mic if a listen was in flight. Home refreshes its list.
   This is also the whole wake-lock fallback story on older iOS: recover
   well, no fake keep-awake.
   v8 battery: body.bg parks every orb animation while the page is hidden. */
document.addEventListener('visibilitychange', () => {
  const vis = document.visibilityState === 'visible';
  document.body.classList.toggle('bg', !vis);
  if(!vis) return;
  if(TTS && speechSynthesis.paused) speechSynthesis.resume();
  /* iOS suspends the AudioContext in the background: the Mac-voice pipeline
     and the chime need it running again */
  try{ if(audioCtx && audioCtx.state !== 'running') audioCtx.resume(); }catch(e){}
  if(live){ acquireWakeLock(); beatOnce(); }
  if(live && !muted && state === 'listening' && !recActive) listen();
  if(onHome) pollSessions();
});
/* iOS bfcache restore: timers and speech come back stale; treat it like a
   visibility return. */
window.addEventListener('pageshow', e => {
  if(!e.persisted) return;
  document.body.classList.remove('bg');
  if(TTS && speechSynthesis.paused) speechSynthesis.resume();
  try{ if(audioCtx && audioCtx.state !== 'running') audioCtx.resume(); }catch(e){}
  if(live){ acquireWakeLock(); beatOnce(); }
  pollSessions();
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

/* ============================================================ barge-in */
/* While a reply is speaking, a mic monitor watches for the HUMAN talking
   over it. The stream is opened with echoCancellation on: the phone's AEC
   subtracts the device's own speaker output, so sustained RMS means a real
   voice, not our own TTS. Rules:
     - no trigger in the first 600ms of speech (speaker echo settles)
     - RMS must stay above threshold for 7 consecutive 50ms frames (~350ms);
       a single spike (cough, clatter) never triggers
     - on trigger: cancel synthesis, play a tiny acknowledgment tick, drop
       into the normal listening flow (the whisper path reuses this stream)
     - monitor + stream released the moment speaking ends
     - getUserMedia failure = no barge
   v8: the monitor's RMS also feeds bumpLevel, so the sphere pulses with
   the user's voice the moment they talk over a reply (spec 3, reuse). */
const BARGE_RMS = .04;      // above the whisper speech floor (.02): talk, not rustle
const BARGE_HOLD = 7;       // 7 x 50ms = ~350ms of sustained voice
const BARGE_SETTLE = 600;   // ms of speech ignored up front
let bargeIv = null, bargeSrc = null, bargeOwn = null, bargeArming = false;
function ackTick(){
  /* tiny WebAudio blip: "heard you, go ahead" */
  try{
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if(audioCtx.state === 'suspended') audioCtx.resume();
    const t = audioCtx.currentTime + .01;
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = 'sine'; o.frequency.value = 1320;
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(.06, t + .012);
    g.gain.exponentialRampToValueAtTime(.0001, t + .12);
    o.connect(g); g.connect(audioCtx.destination);
    o.start(t); o.stop(t + .16);
  }catch(e){}
}
function stopBarge(){
  if(bargeIv){ clearInterval(bargeIv); bargeIv = null; }
  if(bargeSrc){ try{ bargeSrc.disconnect(); }catch(e){} bargeSrc = null; }
  if(bargeOwn){ try{ bargeOwn.getTracks().forEach(t => t.stop()); }catch(e){} bargeOwn = null; }
}
async function startBarge(){
  /* typingMute keeps the whole mic off, the barge monitor included */
  if(bargeIv || bargeArming || !live || muted || typingMute) return;
  bargeArming = true;
  let stream = media;   // whisper path: reuse the stream that is already open
  if(!stream){
    try{
      stream = await navigator.mediaDevices.getUserMedia({
        audio:{ echoCancellation:true, noiseSuppression:true } });
    }catch(e){ bargeArming = false; return; }   // degrade: no barge this reply
    if(SR && !srDead) bargeOwn = stream;   // SR path: ours alone, release after speech
    else media = stream;                   // whisper path adopts it for listening
  }
  bargeArming = false;
  /* speech may have finished while getUserMedia was up */
  if(!live || muted || speechCancelled || state !== 'speaking'){ stopBarge(); return; }
  try{
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if(audioCtx.state === 'suspended') audioCtx.resume();
    bargeSrc = audioCtx.createMediaStreamSource(stream);
    const an = audioCtx.createAnalyser(); an.fftSize = 2048;
    bargeSrc.connect(an);
    const buf = new Float32Array(an.fftSize);
    const t0 = Date.now();
    let hot = 0;
    bargeIv = setInterval(() => {
      if(!live || muted || speechCancelled || state !== 'speaking'){ stopBarge(); return; }
      if(Date.now() - t0 < BARGE_SETTLE) return;   // echo settle window
      an.getFloatTimeDomainData(buf);
      let s = 0; for(const v of buf) s += v * v;
      const rms = Math.sqrt(s / buf.length);
      bumpLevel(Math.min(1, rms * 9));   // the sphere pulses with the human
      if(rms > BARGE_RMS) hot++; else hot = 0;     // must be SUSTAINED voice
      if(hot >= BARGE_HOLD){
        stopBarge();
        stopSpeaking();          // cut the reply mid-sentence
        ackTick();               // audible "go ahead"
        setState('listening');
        setTimeout(listen, 120); // straight into the normal listening flow
      }
    }, 50);
  }catch(e){ stopBarge(); }
}

/* ============================================================ chat */
/* Local turn log renders instantly; GET /chat replaces it with the server's
   cleaned transcript when available (refreshed after each completed turn and
   on pull-to-refresh). Falls back to local-only when /chat is missing. */
let localTurns = [];
let chatHasServer = false;
function renderRich(el, text){
  /* tiny safe renderer: fenced blocks -> real <pre> (mono, x-scroll),
     `inline` -> code chips. All content set via textContent, never HTML. */
  const chunks = String(text).split('```');
  for(let i = 0; i < chunks.length; i++){
    let seg = chunks[i];
    if(i % 2){
      const pre = document.createElement('pre'); pre.className = 'cblk';
      pre.textContent = seg.replace(/^[a-zA-Z0-9_+-]*\n/, '').replace(/\n$/, '');
      el.appendChild(pre);
    }else if(seg){
      const p = document.createElement('span');
      const bits = seg.split(/`([^`\n]+)`/);
      for(let j = 0; j < bits.length; j++){
        if(j % 2){
          const c = document.createElement('code'); c.className = 'ichip';
          c.textContent = bits[j]; p.appendChild(c);
        }else if(bits[j]){
          p.appendChild(document.createTextNode(bits[j]));
        }
      }
      el.appendChild(p);
    }
  }
}
/* copy any bubble's text ("Claude read out an error, I need it as text").
   clipboard API needs a secure context (the tunnel is https); the
   hidden-textarea path covers plain-http and older browsers. */
function legacyCopy(t){
  try{
    const ta = document.createElement('textarea');
    ta.value = t;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    ta.setAttribute('readonly', '');
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }catch(e){ return false; }
}
function copyText(t){
  const done = () => toast('copied');
  const fail = () => { legacyCopy(t) ? done() : toast('copy failed'); };
  try{
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(t).then(done, fail);
      return;
    }
  }catch(e){}
  fail();
}
function bubble(role, text, liveNow){
  const d = document.createElement('div');
  d.className = 'bub ' + (role === 'user' ? 'user' : 'assistant') + (liveNow ? ' live' : '');
  /* the copy button goes FIRST so its float pulls it to the top-right
     and the text wraps around it instead of underneath */
  const cp = document.createElement('button');
  cp.className = 'copybtn';
  cp.setAttribute('aria-label', 'Copy this message');
  cp.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"' +
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="9" width="11" height="11" rx="2.5"/>' +
    '<path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>';
  cp.addEventListener('click', ev => { ev.stopPropagation(); copyText(String(text)); });
  d.appendChild(cp);
  const wrap = document.createElement('div'); wrap.className = 'bwrap';
  renderRich(wrap, text);
  d.appendChild(wrap);
  /* long replies collapse so one wall of text cannot bury the chat */
  const t = String(text);
  if(t.length > 1100 || (t.match(/\n/g) || []).length > 14){
    d.classList.add('clamp');
    const more = document.createElement('button');
    more.className = 'showmore'; more.textContent = 'show more';
    more.addEventListener('click', ev => {
      ev.stopPropagation();
      const clamped = d.classList.toggle('clamp');
      more.textContent = clamped ? 'show more' : 'show less';
    });
    d.appendChild(more);
  }
  return d;
}
/* pill rule, bulletproof: the pill may exist ONLY when (chat surface open)
   AND (user scrolled up more than ~150px from the bottom) AND (content was
   appended after they scrolled up). Every path that mutates the transcript
   funnels through chatAdd or renderChat below, and both enforce it; a
   scroll listener hides the pill the moment the bottom is reached. */
function chatNearBottom(){
  return chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight < 150;
}
function chatScrollBottom(){
  chatScroll.scrollTop = chatScroll.scrollHeight;
  jumpBtn.classList.add('hidden');
}
chatScroll.addEventListener('scroll', () => {
  if(chatNearBottom()) jumpBtn.classList.add('hidden');
}, { passive:true });
jumpBtn.addEventListener('click', chatScrollBottom);
/* "Claude is working" row pinned to the end of the transcript while a
   turn is in flight; in chat mode the orb is tiny, so the chat surface
   itself has to show progress. */
function syncTyping(){
  let row = document.getElementById('typingRow');
  if(turnActive && live){
    if(!row){
      row = document.createElement('div');
      row.id = 'typingRow'; row.className = 'typing';
      const tl = document.createElement('span');
      tl.className = 'tl'; tl.textContent = 'Claude is working';
      row.appendChild(tl);
      for(let i = 0; i < 3; i++){
        const td = document.createElement('span'); td.className = 'td';
        row.appendChild(td);
      }
    }
    chatLines.appendChild(row);   // re-append keeps it LAST
  }else if(row) row.remove();
}
function renderChat(){
  const sheetOpen = chatOpenState;
  const wasNear = !sheetOpen || chatNearBottom();
  const keepTop = chatScroll.scrollTop;
  const grew = localTurns.length > renderedTurns;
  chatLines.textContent = '';
  if(!localTurns.length){
    const p = document.createElement('p'); p.className = 'empty';
    p.textContent = 'The conversation with this session appears here.';
    chatLines.appendChild(p);
    renderedTurns = 0;
    jumpBtn.classList.add('hidden');
    return;
  }
  localTurns.forEach(t => chatLines.appendChild(bubble(t.role, t.text)));
  syncTyping();
  renderedTurns = localTurns.length;
  /* respect the reader: a re-render (server transcript swap) must not yank
     someone who scrolled up back to the bottom mid-read */
  if(wasNear){ chatScrollBottom(); }
  else{
    chatScroll.scrollTop = keepTop;
    if(grew) jumpBtn.classList.remove('hidden');
  }
}
function chatAdd(role, text){
  if(!text) return;
  localTurns.push({ role: role, text: text });
  const empty = chatLines.querySelector('.empty');
  if(empty) empty.remove();
  const sheetOpen = chatOpenState;
  const stick = !sheetOpen || chatNearBottom();
  chatLines.appendChild(bubble(role, text, role !== 'user' && turnActive));
  syncTyping();
  renderedTurns = localTurns.length;
  /* a reply that lands while the chat surface is CLOSED leaves a dot on
     the Chat segment instead of a pill nobody can see (v8: the dot moved
     from the old chat button to the segmented control) */
  if(role !== 'user' && !sheetOpen) segChat.classList.add('unread');
  if(stick) chatScrollBottom();
  else if(sheetOpen) jumpBtn.classList.remove('hidden');
}
function resetChat(){
  localTurns = []; chatHasServer = false;
  segChat.classList.remove('unread');   // the dot was about the old session
  renderChat();
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
  /* baseline by UUID (text baselines mis-finish turns on trims); the /ask
     ack also carries it, whichever arrives first wins */
  baselineUuid = '';
  try{ const bj = await jget('/poll'); if(bj && bj.uuid) baselineUuid = bj.uuid; }catch(e){}
  if(id !== turnId || !live) return;
  sendAsk(id, text);
  pollUntilChanged(id);
}
const bootKey = Math.random().toString(36).slice(2, 10);
function sendAsk(id, text, attempt){
  attempt = attempt || 0;
  /* Idempotent turn key: the server injects ONCE per key, so a retried POST
     (tunnels drop requests) resumes waiting instead of double-typing. This
     was the "listening, working... but the prompt never arrived" bug: a
     dropped /ask meant nothing was ever injected while the UI said working. */
  curAskKey = 'T' + bootKey + '-' + id;
  if(!attempt) askDelivered = false;
  jpost('/ask', { text: text, id: curAskKey, stream: true })
  .then(r => { if(!r.ok) throw new Error('http ' + r.status); return r.json(); })
  .then(j => {
    if(id !== turnId || !live) return;
    if(j.ok && j.delivered){
      /* non-blocking protocol: the prompt IS in the session; completion
         arrives on the stream. This response IS the delivery receipt. */
      askDelivered = true; pendingSend = null;
      if(j.uuid && !baselineUuid) baselineUuid = j.uuid;
      setState('thinking', 'working, delivered');
      return;
    }
    const rep = String(j.reply || '').trim();
    if(!rep || STILL_RE.test(rep)) return;
    if(WAITING_RE.test(rep)){ showDecision(rep); return; }   // permission moment
    finishTurn(id, rep);
  })
  .catch(() => {
    if(id !== turnId || !live) return;
    if(attempt < 4){
      setState('thinking', 'reconnecting...');
      setTimeout(() => { if(id === turnId && live) sendAsk(id, text, attempt + 1); },
                 1500 * (attempt + 1));
      return;
    }
    if(askDelivered){
      setState('thinking', 'working...');
      return;
    }
    /* Could not deliver: QUEUE it and keep trying ourselves, never make the
       human be the retry loop. Same idempotency key = safe to re-send. */
    if(!pendingSend){
      pendingSend = { id: id, text: text };
      toast('connection is down, retrying automatically');
      speakAside('The connection is down. I will keep trying to send that.');
      setState('thinking', 'retrying...');
    }
  });
}
async function pollUntilChanged(id){
  let n = 0;
  while(id === turnId && live){
    await sleep(sseOk ? 15000 : (n++ < 100 ? 3000 : 6000));   // stream is primary
    if(id !== turnId || !live) return;
    try{
      const j = await jget('/poll');
      const rep = String(j.reply || '').trim(), u = String(j.uuid || '');
      if(id !== turnId || !live) return;
      if(!baselineUuid && u){ baselineUuid = u; continue; }   // late baseline
      if(u && rep && u !== baselineUuid && u !== lastUuid){
        lastUuid = u;
        finishTurn(id, rep); return;
      }
    }catch(e){ /* offline blip: keep waiting, the elapsed label keeps counting */ }
  }
}

/* ---- idle reply-watcher: while the call is live and no phone turn is in
   flight, ANY new reply in the session (e.g. typed on the desktop) speaks
   HERE. The Mac is silenced during a call, so without this nobody says it. */
let lastUuid = '';
function speakIncoming(rep){
  stopListening();
  chatAdd('assistant', rep); refreshChat();
  setState('speaking');
  startBarge();
  say(rep, () => {
    stopBarge();
    if(!live) return;
    if(muted){ setState('muted'); }
    else { setState('listening'); listen(); }
  });
}
async function idleWatch(){
  if(!live || turnActive || sseOk) return;   // SSE is the primary channel
  let j;
  try{ j = await jget('/poll'); }catch(e){ return; }
  const u = String(j.uuid || ''), rep = String(j.reply || '').trim();
  if(!u) return;
  if(!lastUuid){ lastUuid = u; return; }   // first sight: baseline, not speech
  if(u === lastUuid || !rep) return;
  if(!live || turnActive) return;
  lastUuid = u;
  speakIncoming(rep);
}
setInterval(idleWatch, 5000);

/* ---- the SSE stream: ONE persistent connection pushes replies, delivery
   acks and permission moments the moment they happen. EventSource reconnects
   on its own; while it's healthy the polling above becomes a mere backstop. */
let es = null, sseOk = false, askDelivered = false, curAskKey = '';
let baselineUuid = '', pendingSend = null;
setInterval(() => {
  if(pendingSend && live && pendingSend.id === turnId && !askDelivered){
    sendAsk(pendingSend.id, pendingSend.text, 3);   // one shot per beat
  }
}, 8000);
function startEvents(){
  if(es) try{ es.close(); }catch(e){}
  try{ es = new EventSource('/events?k=' + encodeURIComponent(K)); }
  catch(e){ es = null; return; }
  es.onopen = () => { sseOk = true; };
  es.onerror = () => { sseOk = false; };
  es.addEventListener('hello', e => {
    try{
      const d = JSON.parse(e.data);
      if(!d.uuid) return;
      if(!lastUuid){ lastUuid = d.uuid; return; }
      if(d.uuid !== lastUuid){
        /* a reply landed while the stream was down: catch up NOW */
        jget('/poll').then(j => {
          const rep = String(j.reply || '').trim();
          if(j.uuid && j.uuid !== lastUuid && rep){
            lastUuid = j.uuid;
            if(turnActive){ finishTurn(turnId, rep); }
            else if(live){ speakIncoming(rep); }
          }
        }).catch(() => {});
      }
    }catch(x){}
  });
  es.addEventListener('switched', e => {
    try{ const d = JSON.parse(e.data); if(d.uuid) lastUuid = d.uuid; }catch(x){}
  });
  es.addEventListener('ack', e => {
    try{
      const d = JSON.parse(e.data);
      if(turnActive && d.id === curAskKey){
        askDelivered = true;
        setState('thinking', 'working, delivered');
      }
    }catch(x){}
  });
  es.addEventListener('reply', e => {
    try{
      const d = JSON.parse(e.data);
      const rep = String(d.reply || '').trim(), u = String(d.uuid || '');
      if(!u || !rep || u === lastUuid) return;
      lastUuid = u;
      if(turnActive){ finishTurn(turnId, rep); }
      else if(live){ speakIncoming(rep); }
    }catch(x){}
  });
  es.addEventListener('pending', e => {
    try{
      const d = JSON.parse(e.data);
      if(!live) return;
      if(d.q) showDecision(d.q); else hideDecision();
    }catch(x){}
  });
}
startEvents();
function finishTurn(id, reply){
  if(id !== turnId) return;
  pendingSend = null;
  turnId++;                       // one winner: kill the other waiters
  turnActive = false;
  stopWorkTicker();
  hideDecision();
  chatAdd('assistant', reply);
  refreshChat();                  // swap in the server's cleaned transcript
  jget('/poll').then(j => { if(j && j.uuid) lastUuid = j.uuid; }).catch(() => {});
  pollSessions();                 // states likely changed with the turn
  setState('speaking');
  startBarge();                   // talking over the reply interrupts it
  say(reply, () => {
    stopBarge();
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
  syncTyping();   // drop the "Claude is working" row with the turn
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
let lastDecisionQ = '', lastDecisionAt = 0;
function showDecision(q){
  decideQ.textContent = q;
  if(!decisionOpen){
    decisionOpen = true;
    decideEl.classList.add('open');
    stopWorkTicker();
    stopListening();
    setState('needs', 'needs you');
    chime();
    /* never re-read the SAME question in a loop (it re-surfaces while the
       Mac-side dialog is still up); once a minute at most */
    if(q !== lastDecisionQ || Date.now() - lastDecisionAt > 60000){
      lastDecisionQ = q; lastDecisionAt = Date.now();
      speakAside(q);
    }
  }
}
function hideDecision(){
  decisionOpen = false;
  decideEl.classList.remove('open');
}
$('yesBtn').addEventListener('click', () => { stopSpeaking(); hideDecision(); cancelTurn(); startTurn('yes'); });
$('noBtn').addEventListener('click', () => { stopSpeaking(); hideDecision(); cancelTurn(); startTurn('no'); });

/* /status poll: checked right away at call start (so a needs-you card tapped
   on home surfaces its question within a beat of connecting), then every ~4s
   while a turn is working and ~8s while idle on a live call. An empty pending
   while the panel is open means it was answered elsewhere (for example on the
   Mac): put the call back where it was. */
let liveGen = 0;
async function statusLoop(myGen){
  while(live && myGen === liveGen){
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
    await sleep(turnActive ? 4000 : 8000);
    if(!live || myGen !== liveGen) return;
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

/* ============================================================ stitching */
/* Pause tolerance: end-of-speech does NOT send the prompt. The transcript
   lands in stitchBuf and a follow-up window holds the send:
     - 0.9s when it sounded finished (ends in . ? !)
     - 2.4s when it is clearly mid-thought (trailing comma or conjunction)
     - 1.6s otherwise
   The mic STAYS OPEN during the window; if the user resumes, the pending
   send is cancelled, the continuation appends, and the window re-arms. The
   status line counts down with shrinking dots so it never feels stuck. */
let stitchBuf = '', stitchTimer = null, stitchTick = null, stitchDeadline = 0;
let sttPending = 0;          // whisper segments still transcribing on the Mac
let flushWaits = 0;          // bounded wait for those segments at flush time
let whisperVoice = () => false;   // is the CURRENT whisper segment already voiced?
const CMD_RE = /^(stop listening|end call|hang up|goodbye)[.!]?$/i;

function holdMs(t){
  t = String(t || '').trim();
  if(/[.?!]$/.test(t)) return 900;                    // sounded finished
  if(/(,|\b(and|or|but|so|because|then|plus|also|with))$/i.test(t)) return 2400;  // mid-thought
  return 1600;                                        // default follow-up window
}
function stopStitchTick(){ if(stitchTick){ clearInterval(stitchTick); stitchTick = null; } }
function armStitch(){
  if(stitchTimer) clearTimeout(stitchTimer);
  flushWaits = 0;
  const ms = holdMs(stitchBuf);
  stitchDeadline = Date.now() + ms;
  stitchTimer = setTimeout(flushStitch, ms);
  stopStitchTick();
  stitchTick = setInterval(() => {   // ". . ." shrinking toward the send
    if(!stitchTimer || state !== 'listening'){ stopStitchTick(); return; }
    const left = Math.max(0, stitchDeadline - Date.now());
    const dots = 1 + Math.min(3, Math.floor(left / 600));
    statusEl.textContent = 'listening ' + '. '.repeat(dots).trim();
  }, 200);
}
function holdStitchOnSpeech(){
  /* the user resumed inside the window: cancel the pending send, keep buffer */
  if(stitchTimer){ clearTimeout(stitchTimer); stitchTimer = null; }
  stopStitchTick();
  if(state === 'listening') statusEl.textContent = 'listening';
}
function clearStitch(){
  if(stitchTimer){ clearTimeout(stitchTimer); stitchTimer = null; }
  stopStitchTick();
  stitchBuf = '';
}
function stitchAppend(t){
  t = String(t || '').trim();
  if(!t){
    if(!stitchBuf && live && !muted && state === 'listening' && !recActive) listen();
    return;
  }
  stitchBuf += (stitchBuf ? ' ' : '') + t;
  const whole = stitchBuf.trim();
  if(CMD_RE.test(whole)){   // "end call" should not sit in a hold window
    clearStitch(); stopListening(); handleUtterance(whole); return;
  }
  /* whisper path: if the user is ALREADY talking again, that segment will
     append and re-arm when it lands; arming a timer now would race it */
  if(whisperVoice()) return;
  armStitch();
}
function flushStitch(){
  stitchTimer = null;
  stopStitchTick();
  if(sttPending > 0 && flushWaits < 32){   // a segment is still transcribing
    flushWaits++;
    stitchTimer = setTimeout(flushStitch, 250);
    return;
  }
  flushWaits = 0;
  const t = stitchBuf.trim();
  stitchBuf = '';
  if(!t){
    if(live && !muted && state === 'listening' && !recActive) listen();
    return;
  }
  stopListening();       // close the mic; the turn engine owns the call now
  handleUtterance(t);
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
  rec.onresult = e => {
    if(myGen !== gen) return;
    bumpLevel(.65);
    let finals = '', interim = false;
    for(let i = e.resultIndex; i < e.results.length; i++){
      if(e.results[i].isFinal) finals += e.results[i][0].transcript;
      else interim = true;
    }
    /* interim speech inside a follow-up window = the user resumed */
    if(interim && stitchTimer) holdStitchOnSpeech();
    if(finals){
      srFails = 0;   // the service clearly works
      /* do NOT stop and send; hold a follow-up window and keep the
         recognizer running so a continuation stitches on */
      stitchAppend(finals.trim());
    }
  };
  rec.onspeechstart = () => {
    if(myGen !== gen) return;
    bumpLevel(.6);
    if(stitchTimer) holdStitchOnSpeech();
  };
  rec.onerror = ev => {
    recActive = false;
    if(myGen !== gen) return;
    if(ev.error === 'not-allowed'){ micDenied(); return; }
    if(ev.error === 'service-not-allowed'){
      /* iOS: Siri and Dictation is off, the recognizer can never work.
         Two consecutive failures flip this session to the whisper path
         permanently instead of looping on errors. */
      if(++srFails >= 2) srDead = true;
      if(live && !muted && state === 'listening') setTimeout(listen, srDead ? 200 : 900);
      return;
    }
    if(live && !muted && state === 'listening') setTimeout(listen, 900);
  };
  rec.onend = () => {
    recActive = false;
    if(myGen !== gen) return;
    /* restart keeps the mic open through pauses and hold windows */
    if(live && !muted && state === 'listening') setTimeout(listen, 400);
  };
  rec.start();
}

/* ---- path B: record + silence detection, whisper on the Mac via /stt */
async function listenWhisper(){
  if(recActive) return;
  const myGen = gen;
  setState('listening');
  /* echoCancellation also serves the barge-in monitor, which shares this
     stream while replies speak */
  try{ media = media || await navigator.mediaDevices.getUserMedia(
    { audio:{ echoCancellation:true, noiseSuppression:true } }); }
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
  /* stitching peeks at this: a voiced current segment means "the user is
     already continuing", so no hold timer should race it */
  whisperVoice = () => (myGen === gen && recActive && spoke);
  const iv = setInterval(() => {
    if(myGen !== gen){ clearInterval(iv); try{ mr.stop(); }catch(_){} return; }
    an.getFloatTimeDomainData(buf);
    let s = 0; for(const v of buf) s += v*v;
    const rms = Math.sqrt(s / buf.length);
    bumpLevel(Math.min(1, rms * 10));   // the sphere pulses with the voice
    if(rms > .02){
      spoke = true; quiet = 0;
      if(stitchTimer) holdStitchOnSpeech();   // resumed inside the window
    } else if(spoke) quiet++;
    /* ~1.4s of RMS silence ends the utterance (7 x 200ms); thinking pauses
       shorter than that stay inside ONE segment, and longer ones are caught
       by the stitch window that follows */
    if((spoke && quiet >= 7) || (!spoke && ++idle > 250)){ clearInterval(iv); mr.stop(); }
  }, 200);
  mr.onstop = () => {
    src.disconnect(); recActive = false;
    if(myGen !== gen) return;
    if(!spoke){
      /* silence-only segment: keep the mic open; a pending stitch window
         (if any) will fire and send on its own */
      if(live && !muted && state === 'listening') listen();
      return;
    }
    /* REOPEN the mic immediately so a continuation spoken while whisper
       chews on this segment is never lost */
    if(live && !muted && state === 'listening') setTimeout(listen, 60);
    sttPending++;
    /* iOS MediaRecorder emits audio/mp4 (not webm): send the REAL mimeType
       so the server-side ffmpeg knows what arrived */
    const blob = new Blob(chunks, { type: mr.mimeType || 'audio/webm' });
    fetch(urlFor('/stt'), { method:'POST',
      headers:{ 'Content-Type': blob.type || 'application/octet-stream' },
      body:blob })
    .then(r => r.json())
    .then(j => {
      sttPending = Math.max(0, sttPending - 1);
      if(myGen !== gen) return;   // muted / ended / flushed while transcribing
      stitchAppend(String(j.text || '').trim());
    })
    .catch(() => {
      sttPending = Math.max(0, sttPending - 1);
      /* transcription failed: if something is already stitched, still send
         it after a window rather than dropping the user's words */
      if(myGen === gen && stitchBuf && !stitchTimer && !whisperVoice()) armStitch();
    });
  };
  mr.start();
}
function listen(){
  /* typingMute: the composer has focus, the mic stays off until blur; keep
     the status honest instead of claiming "listening" with a dead mic */
  if(typingMute){
    if(live && !muted && state === 'listening') setState('muted', 'typing');
    return;
  }
  if(!live || muted || turnActive || decisionOpen) return;
  /* srDead: SR proved unusable this session (iOS service-not-allowed twice) */
  ((SR && !srDead) ? listenSR : listenWhisper)();
}
function stopListening(){
  gen++;
  clearStitch();   // a half-held prompt dies with the listener
  if(rec){ try{ rec.onend = null; rec.onerror = null; rec.stop(); }catch(_){} rec = null; }
  recActive = false;
}

/* ============================================================ call lifecycle */
/* v8: the full-screen start overlay is now ONLY the microphone-denied
   recovery surface. Normal starts and ends live on the call chip riding
   the orb's bottom edge; #idleHint carries the mic privacy note. */
const startOverlay=$('startOverlay'), startBtn=$('startBtn');
const START_BODY = 'A live call with your coding session. Your phone will ask to use the ' +
  'microphone; audio goes only to your Mac and nowhere else.';
const START_FINE = 'Speech is transcribed on-device when the browser supports it, ' +
  'otherwise on your Mac with whisper. Say "end call" any time to hang up.';
function resetStartOverlay(name){
  $('startTitle').textContent = name || 'voicebridge';
  $('startBody').textContent = START_BODY;
  $('startFine').textContent = START_FINE;
  startBtn.textContent = 'Start call';
}
function syncLiveUi(){
  document.body.classList.toggle('live', live);
  callChip.setAttribute('aria-label', live ? 'End call' : 'Start call');
}
function micDenied(){
  live = false; liveGen++;
  cancelTurn(); stopListening(); stopSpeaking(); stopHeartbeat(); releaseWakeLock();
  setMuted(false);
  syncLiveUi();
  if(mode === 'chat') setMode('voice');            // the overlay owns the screen
  if(chatOpenState && chatPos === 'full') setChatPos('half');
  setState('ended', 'microphone blocked');
  $('startTitle').textContent = 'Microphone is blocked';
  $('startBody').textContent = 'Allow microphone access for this site in your browser settings, then start the call again.';
  $('startFine').textContent = 'iOS: Settings, Safari, Microphone. Android: the lock icon in the address bar.';
  startBtn.textContent = 'Try again';
  startOverlay.classList.remove('hidden');
}
async function startCall(){
  /* iOS: everything audio must be unlocked INSIDE the tap, before any await:
     silent TTS warm-up + AudioContext resume (it starts suspended). The
     call chip tap and the recovery overlay button both land here. */
  unlockAudio();
  startOverlay.classList.add('hidden');
  live = true;
  setMuted(false);
  syncLiveUi();
  liveGen++;
  srFails = 0;   // a fresh call gets a fresh chance (srDead stays for the session)
  chatPosPref = 'half';   // the remembered sheet position is per call
  refreshVoices();
  acquireWakeLock();
  setState('thinking', 'connecting');
  /* Prime the mic permission inside the tap gesture so the browser prompt has
     clear context; on the native-SR path release the stream right away so it
     never fights the recognizer for the device. */
  try{
    const s = await navigator.mediaDevices.getUserMedia(
      { audio:{ echoCancellation:true, noiseSuppression:true } });
    if(SR && !srDead) s.getTracks().forEach(t => t.stop()); else media = s;
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
  setMuted(false);
  syncLiveUi();
  /* ending from chat mode returns to the orb so the call chip is reachable */
  if(mode === 'chat') setMode('voice');
  if(chatOpenState && chatPos === 'full') setChatPos('half');
  setState('ended', 'call ended');
}
startBtn.addEventListener('click', startCall);

/* ==== the call chip: start when idle, hang up when live (spec 2) ==== */
callChip.addEventListener('click', ev => {
  ev.stopPropagation();   // the chip rides ON the orb; never also toggle mute
  if(live){ endCall(); return; }
  startCall();            // unlockAudio runs synchronously inside this tap
});

/* ==== mute: tap the orb while live (spec 2); shown as a slash badge on
   the chip plus the dimmed muted palette ==== */
function setMuted(m){
  muted = m;
  document.body.classList.toggle('muted', muted);
  $('orbzone').setAttribute('aria-label', live
    ? (muted ? 'Orb. Microphone muted; tap to unmute.'
             : 'Orb. Tap to mute; while a reply is speaking, tap to stop the voice.')
    : 'Orb. Start the call with the call button below.');
  if(muted){
    stopListening();
    stopBarge();   // muted means muted: no barge monitor either
    if(state === 'listening') setState('muted');
  }else if(state === 'muted' || state === 'listening'){
    setState('listening'); listen();
  } /* muted flipped during working/speaking: the turn loop checks it after */
}
function toggleMute(){
  if(!live) return;
  setMuted(!muted);
}

/* ============================================================ sheets */
const scrim=$('scrim'), chatSheet=$('chatSheet'), sessSheet=$('sessSheet'),
      closedSheet=$('closedSheet'), setSheet=$('setSheet');
let sessOpen = false, closedOpen = false, setOpen = false;
/* control room, the closed-session sheet, and settings are modal (scrim);
   chat is an independent surface */
function syncScrim(){
  document.body.classList.toggle('sheet-open', sessOpen || closedOpen || setOpen);
}
function openSessSheet(){
  sessOpen = true;
  sessSheet.classList.add('open');
  syncScrim();
  pollSessions();
}
function closeSessSheet(){
  sessOpen = false;
  sessSheet.classList.remove('open');
  syncScrim();
}
/* settings sheet (the gear): voice source + the v8 voice picker */
function renderVoiceCards(){
  const cards = setSheet.querySelectorAll('#voiceCards .vcard');
  for(let i = 0; i < cards.length; i++){
    const sel = cards[i].getAttribute('data-v') === voiceName;
    cards[i].classList.toggle('sel', sel);
    cards[i].setAttribute('aria-checked', String(sel));
  }
  /* the picker only makes sense while the Natural source is active */
  $('voiceCards').classList.toggle('off', voicePref !== 'mac');
}
function setVoiceName(id){
  if(VOICE_IDS.indexOf(id) < 0) return;
  const changed = id !== voiceName;
  voiceName = id;
  try{ localStorage.setItem('vbvoice_name', id); }catch(e){}
  renderVoiceCards();
  if(changed) toast('voice changes on the next reply');
}
$('voiceCards').addEventListener('click', ev => {
  const card = ev.target.closest ? ev.target.closest('.vcard') : null;
  if(card) setVoiceName(card.getAttribute('data-v'));
});
function renderVoicePref(){
  const mac = voicePref === 'mac';
  $('voiceMacBtn').classList.toggle('sel', mac);
  $('voiceMacBtn').setAttribute('aria-checked', String(mac));
  $('voicePhoneBtn').classList.toggle('sel', !mac);
  $('voicePhoneBtn').setAttribute('aria-checked', String(!mac));
  renderVoiceCards();
}
function setVoicePref(p){
  voicePref = p;
  try{ localStorage.setItem('vbvoice', p); }catch(e){}
  /* re-selecting Mac gives it a fresh chance after earlier failures */
  if(p === 'mac'){ macDead = false; macFails = 0; }
  renderVoicePref();
}
function openSetSheet(){
  renderVoicePref();
  setOpen = true;
  setSheet.classList.add('open');
  syncScrim();
}
function closeSetSheet(){
  setOpen = false;
  setSheet.classList.remove('open');
  syncScrim();
}
$('setBtn').addEventListener('click', () => { setOpen ? closeSetSheet() : openSetSheet(); });
$('voicePhoneBtn').addEventListener('click', () => setVoicePref('phone'));
$('voiceMacBtn').addEventListener('click', () => setVoicePref('mac'));
renderVoicePref();
/* read-only sheet for a closed (inactive) session; never starts a call */
async function openClosedSheet(s){
  closedOpen = true;
  closedName.textContent = s.name || 'session';
  closedBody.textContent = String(s.last || '').trim() || 'Loading the last reply...';
  closedSheet.classList.add('open');
  document.body.classList.add('sheet-open');
  try{
    const rep = String((await jget('/last?q=' +
      encodeURIComponent(s.name || s.id || ''))).reply || '').trim();
    if(!closedOpen) return;
    if(rep) closedBody.textContent = rep;
    else if(!String(s.last || '').trim())
      closedBody.textContent = 'No reply recorded for this session.';
  }catch(e){
    if(closedOpen && !String(s.last || '').trim())
      closedBody.textContent = 'Could not load the last reply.';
  }
}
function closeClosedSheet(){
  closedOpen = false;
  closedSheet.classList.remove('open');
  syncScrim();
}
/* ---- the chat surface ----
   v8 primary path: the Voice / Chat segmented control (setMode below);
   body.mode-chat re-anchors #chatSheet full-height under the mini orb.
   The v7 three-position sheet machinery (half / full / drag / chevron) is
   KEPT below but parked: nothing on the call screen routes into it while
   the segmented control exists, and every handler no-ops in chat mode. */
function applyChatPos(){
  if(mode === 'chat'){
    /* chat MODE owns the geometry: always open, never the legacy full */
    chatSheet.classList.add('open');
    chatSheet.classList.remove('full');
    document.body.classList.remove('chat-full');
    chatTitle.textContent = 'Chat';
    return;
  }
  const full = chatOpenState && chatPos === 'full';
  chatSheet.classList.toggle('open', chatOpenState);
  chatSheet.classList.toggle('full', full);
  document.body.classList.toggle('chat-full', full);
  /* full screen: the slim top bar names the SESSION, not just "Chat" */
  chatTitle.textContent = full ? (pillName.textContent || 'Chat') : 'Chat';
  chatSizeBtn.setAttribute('aria-label',
    full ? 'Collapse chat to half screen' : 'Expand chat to full screen');
}
function setChatPos(pos){
  chatPos = pos;
  chatPosPref = pos;   // remembered for the rest of this call
  applyChatPos();
}
function openChatSheet(pos){
  if(mode === 'chat') return;   // the mode already shows the transcript
  chatOpenState = true;
  chatPos = pos || chatPosPref || 'half';
  chatPosPref = chatPos;
  segChat.classList.remove('unread');
  applyChatPos();
  chatScrollBottom();          // on open: ALWAYS at the bottom, pill hidden
  refreshChat();
  /* hardware/gesture back closes the sheet instead of leaving the page */
  if(!chatHist){
    try{ history.pushState({ vbchat: 1 }, ''); chatHist = true; }catch(e){}
  }
}
function closeChatSheet(fromPop){
  if(mode === 'chat') return;   // leaving chat is setMode('voice'), not this
  const was = chatOpenState;
  chatOpenState = false;
  applyChatPos();
  jumpBtn.classList.add('hidden');
  try{ composeIn.blur(); }catch(e){}
  if(chatHist){
    chatHist = false;
    if(!fromPop && was){ try{ history.back(); }catch(e){} }
  }
}
/* ---- v8: the Voice / Chat segmented control (spec 4) ---- */
function renderSeg(){
  segVoice.classList.toggle('sel', mode === 'voice');
  segVoice.setAttribute('aria-selected', String(mode === 'voice'));
  segChat.classList.toggle('sel', mode === 'chat');
  segChat.setAttribute('aria-selected', String(mode === 'chat'));
}
function setMode(m, fromPop){
  if(m === mode){ renderSeg(); return; }
  mode = m;
  document.body.classList.toggle('mode-chat', m === 'chat');
  renderSeg();
  if(m === 'chat'){
    chatOpenState = true;
    chatPos = 'half';           // the legacy positions stay parked
    applyChatPos();
    segChat.classList.remove('unread');   // opening the chat reads the news
    chatScrollBottom();
    refreshChat();
    /* hardware/gesture back returns to Voice instead of leaving the page */
    if(!modeHist){
      try{ history.pushState({ vbmode: 1 }, ''); modeHist = true; }catch(e){}
    }
  }else{
    chatOpenState = false;
    document.body.classList.remove('mode-chat');
    chatSheet.classList.remove('open');
    jumpBtn.classList.add('hidden');
    try{ composeIn.blur(); }catch(e){}
    if(modeHist){
      modeHist = false;
      if(!fromPop){ try{ history.back(); }catch(e){} }
    }
  }
}
segVoice.addEventListener('click', () => setMode('voice'));
segChat.addEventListener('click', () => setMode('chat'));
window.addEventListener('popstate', () => {
  if(mode === 'chat'){ modeHist = false; setMode('voice', true); return; }
  if(chatOpenState) closeChatSheet(true);
  else { chatHist = false; modeHist = false; }
});
chatSizeBtn.addEventListener('click', () => {
  if(mode === 'chat') return;   // the mode owns the geometry
  setChatPos(chatPos === 'full' ? 'half' : 'full');
});
/* drag the sheet header (legacy sheet positions only; chat MODE geometry
   is fixed): up from half = full screen, down = half / closed. */
(function(){
  let dragging = false, y0 = 0, h0 = 0, lastY = 0, lastT = 0, vel = 0;
  function endDrag(){
    if(!dragging) return;
    dragging = false;
    chatSheet.classList.remove('dragging');
    const dy = lastY - y0;   // down positive
    const H = window.innerHeight || 1;
    if(chatPos === 'half'){
      const curH = h0 - dy;
      if(vel < -.6 || curH > H * .72) setChatPos('full');
      else if(vel > .6 || dy > 130) closeChatSheet();
      else applyChatPos();                       // snap back to half
    }else{
      if(vel > 1.0 || dy > H * .55) closeChatSheet();
      else if(vel > .25 || dy > 120) setChatPos('half');
      else applyChatPos();                       // snap back to full
    }
    chatSheet.style.height = '';
    chatSheet.style.transform = '';
  }
  chatHead.addEventListener('touchstart', e => {
    if(mode === 'chat' || !chatOpenState || e.touches.length !== 1) return;
    dragging = true;
    y0 = lastY = e.touches[0].clientY;
    lastT = Date.now();
    vel = 0;
    h0 = chatSheet.getBoundingClientRect().height;
    chatSheet.classList.add('dragging');
  }, { passive:true });
  chatHead.addEventListener('touchmove', e => {
    if(!dragging) return;
    e.preventDefault();
    const y = e.touches[0].clientY, now = Date.now();
    if(now > lastT) vel = (y - lastY) / (now - lastT);   // px per ms
    lastY = y; lastT = now;
    const dy = y - y0;
    if(chatPos === 'half'){
      /* bottom-anchored: growing height tracks an upward drag */
      const nh = Math.min(window.innerHeight, Math.max(90, h0 - dy));
      chatSheet.style.height = nh + 'px';
    }else{
      /* full: slide the whole sheet down with the finger */
      chatSheet.style.transform = 'translateY(' + Math.max(0, dy) + 'px)';
    }
  }, { passive:false });
  chatHead.addEventListener('touchend', endDrag);
  chatHead.addEventListener('touchcancel', endDrag);
})();
/* ---- the typed composer (silent prompts from the phone) ---- */
function sendTyped(){
  const t = (composeIn.value || '').trim();
  if(!t) return;
  if(!live){ toast('start the call first, then type away'); return; }
  if(decisionOpen){ toast('answer the yes or no first'); return; }
  if(turnActive){ toast('still working on the last one'); return; }
  composeIn.value = '';
  stopSpeaking();     // typing over a talking reply = silent barge-in
  stopListening();
  startTurn(t);       // the EXACT same turn engine as speech
}
sendBtn.addEventListener('click', sendTyped);
/* keep the input focused across a send tap: iOS otherwise blurs first,
   the keyboard collapses, the sheet shifts, and the tap can miss; this
   also keeps the keyboard up for a quick follow-up */
sendBtn.addEventListener('mousedown', e => e.preventDefault());
composeIn.addEventListener('keydown', e => {
  if(e.key === 'Enter'){ e.preventDefault(); sendTyped(); }
});
/* focusing the field mutes the mic (no accidental hot mic in a meeting,
   and no recognizer transcribing keyboard clicks); blur restores exactly
   the state the call wants */
composeIn.addEventListener('focus', () => {
  typingMute = true;
  stopListening();
  stopBarge();
  if(live && !muted && state === 'listening') setState('muted', 'typing');
});
composeIn.addEventListener('blur', () => {
  typingMute = false;
  if(live && !muted && !turnActive && !decisionOpen && state !== 'speaking'){
    setState('listening'); listen();
  }
});
/* lift the chat surface over the on-screen keyboard (iOS lays the keyboard
   over fixed elements; visualViewport is the only honest signal) */
(function(){
  const vv = window.visualViewport;
  if(!vv) return;
  function kb(){
    const gap = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    chatSheet.style.setProperty('--kb', (gap > 40 ? gap : 0) + 'px');
    if(gap > 40 && chatOpenState && chatNearBottom()) chatScrollBottom();
  }
  vv.addEventListener('resize', kb);
  vv.addEventListener('scroll', kb);
})();
/* ---- the orb is a control surface (spec 2):
   while a reply speaks, tap = stop the voice (read-along users finish
   before the speech does, kept from v7); otherwise, while live, tap
   toggles MUTE. The call chip stops propagation, so start/end never
   double-fires a mute. */
function hushVoice(){
  if(state !== 'speaking') return;
  stopSpeaking();
  resumeAfterSpeech();
}
function orbTap(){
  if(state === 'speaking'){ hushVoice(); return; }
  toggleMute();
}
$('orbzone').addEventListener('click', orbTap);
$('orbzone').addEventListener('keydown', e => {
  if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); orbTap(); }
});
$('hushBtn').addEventListener('click', hushVoice);
scrim.addEventListener('click', () => { closeSessSheet(); closeClosedSheet(); closeSetSheet(); });
$('pill').addEventListener('click', () => { sessOpen ? closeSessSheet() : openSessSheet(); });

/* ============================================================ roster (shared) */
function isWorkingState(st){ return /work|think|run|busy/i.test(String(st || '')); }
async function fetchSessions(){
  try{
    const j = await jget('/sessions');
    return Array.isArray(j.sessions) ? j.sessions : null;
  }catch(e){ return null; }
}
function stateLabel(s){
  if(!isActiveSess(s)) return 'closed, read-only';
  if(s.pending) return 'waiting on a decision';
  if(isWorkingState(s.state)) return 'working';
  return s.state || 'idle';
}

/* ============================================================ home screen */
/* Seconds-ago to a compact age: 45 becomes "45s", 3000 becomes "50m",
   7200 becomes "2h", 200000 becomes "2d". */
function fmtAgo(sec){
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  if(s < 60) return s + 's';
  if(s < 3600) return Math.floor(s / 60) + 'm';
  if(s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
}
function homeCard(s){
  const closed = !isActiveSess(s);
  const b = document.createElement('button');
  b.className = 'hcard' + (closed ? ' closed' : (s.pending ? ' needs' : ''));
  b.setAttribute('aria-label', closed
    ? ((s.name || 'session') + ', closed, read only')
    : ('Call ' + (s.name || 'session') + (s.pending ? ', needs you' : '')));

  const r1 = document.createElement('div'); r1.className = 'r1';
  /* deterministic colored monogram: different projects tellable at a glance */
  const mono = document.createElement('span'); mono.className = 'mono';
  let h = 0; const nm = String(s.name || 's');
  for(let i = 0; i < nm.length; i++) h = (h * 31 + nm.charCodeAt(i)) >>> 0;
  mono.style.background = 'hsl(' + (h % 360) + ' 42% 30%)';
  mono.textContent = nm.charAt(0).toUpperCase();
  const n = document.createElement('span'); n.className = 'n';
  n.textContent = s.name || 'session';
  r1.appendChild(mono);
  const ago = document.createElement('span'); ago.className = 'ago';
  ago.textContent = (s.ago === undefined || s.ago === null) ? '' : fmtAgo(s.ago) + ' ago';
  r1.append(n, ago);

  const r2 = document.createElement('div'); r2.className = 'r2';
  const dot = document.createElement('span');
  dot.className = 'sdot' + (closed ? ' closed'
    : (s.pending ? ' needs' : (isWorkingState(s.state) ? ' working' : '')));
  r2.appendChild(dot);
  if(closed){
    const lbl = document.createElement('span');
    lbl.className = 'lbl closed'; lbl.textContent = 'closed, read-only';
    r2.appendChild(lbl);
  }else if(s.pending){
    const badge = document.createElement('span');
    badge.className = 'badge needs'; badge.textContent = 'needs you';
    r2.appendChild(badge);
  }else{
    const lbl = document.createElement('span');
    const working = isWorkingState(s.state);
    lbl.className = 'lbl ' + (working ? 'working' : 'ready');
    lbl.textContent = working ? 'working' : 'ready';
    r2.appendChild(lbl);
  }

  b.append(r1, r2);
  const preview = String(s.last || '').trim();
  if(preview){
    const last = document.createElement('div'); last.className = 'last';
    last.textContent = preview;
    b.appendChild(last);
  }
  /* closed cards NEVER route into a call: read-only sheet instead */
  b.addEventListener('click', () => { closed ? openClosedSheet(s) : openSession(s); });
  return b;
}
function groupLabel(text){
  const g = document.createElement('div');
  g.className = 'hgroup';
  g.textContent = text;
  return g;
}
let lastHomeList = null;
const homeFilter = document.getElementById('homeFilter');
homeFilter.addEventListener('input', () => renderHome(lastHomeList));
function renderHome(list){
  const keep = homeList.scrollTop;
  homeList.textContent = '';
  if(!list || !list.length){
    const p = document.createElement('p'); p.className = 'hempty';
    p.textContent = list
      ? 'No open sessions on the Mac. Start one in a terminal and it appears here.'
      : 'Cannot reach the relay. Check the tunnel and that vb call is on.';
    homeList.appendChild(p);
    return;
  }
  /* priority sections, what needs you first, then working, then ready,
     then closed read-only. Counts in each header; filter when the list grows. */
  lastHomeList = list;
  const q = (homeFilter.value || '').trim().toLowerCase();
  homeFilter.classList.toggle('hidden', list.length <= 8 && !q);
  const rows = q ? list.filter(s => String(s.name || '').toLowerCase().includes(q)) : list;
  const act = rows.filter(isActiveSess);
  const needs = act.filter(s => s.pending);
  const work = act.filter(s => !s.pending && isWorkingState(s.state));
  const ready = act.filter(s => !s.pending && !isWorkingState(s.state));
  const old = rows.filter(s => !isActiveSess(s));
  const sec = (label, arr) => {
    if(!arr.length) return;
    homeList.appendChild(groupLabel(label + '  (' + arr.length + ')'));
    arr.forEach(s => homeList.appendChild(homeCard(s)));
  };
  sec('Needs you', needs);
  sec('Working', work);
  sec('Ready', ready);
  if(!act.length && !q){
    const p = document.createElement('p'); p.className = 'hempty';
    p.textContent = 'No live sessions right now. Earlier sessions below are read-only.';
    homeList.appendChild(p);
  }
  sec('Earlier', old);
  homeList.scrollTop = keep;
}

/* ============================================================ control room */
function sessionCard(s){
  const closed = !isActiveSess(s);
  const card = document.createElement('div');
  card.className = 'card' + (s.current ? ' current' : '') + (closed ? ' closed' : '');

  const main = document.createElement('button');
  main.className = 'main';
  main.setAttribute('aria-label', closed
    ? ((s.name || 'session') + ', closed, read only')
    : ((s.current ? 'On call with ' : 'Move the call to ') + (s.name || 'session')));
  const dot = document.createElement('span');
  dot.className = 'sdot' + (closed ? ' closed'
    : (s.pending ? ' needs' : (isWorkingState(s.state) ? ' working' : '')));
  const meta = document.createElement('span'); meta.className = 'meta';
  const n = document.createElement('span'); n.className = 'n'; n.textContent = s.name || 'session';
  const c = document.createElement('span'); c.className = 'c'; c.textContent = stateLabel(s);
  meta.append(n, c);
  main.append(dot, meta);
  if(!closed && s.pending){
    const b = document.createElement('span'); b.className = 'badge needs'; b.textContent = 'needs you';
    main.appendChild(b);
  }else if(!closed && s.current){
    const b = document.createElement('span'); b.className = 'badge oncall'; b.textContent = 'on call';
    main.appendChild(b);
  }
  /* closed rows open the read-only sheet; the call NEVER moves to them */
  main.addEventListener('click', () => {
    if(closed){ closeSessSheet(); openClosedSheet(s); }
    else switchTo(s);
  });

  const hear = document.createElement('button');
  hear.className = 'hear';
  hear.setAttribute('aria-label', 'Hear the last reply from ' + (s.name || 'this session'));
  hear.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"' +
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M11 5 6.5 8.5H3v7h3.5L11 19z" fill="currentColor" stroke="none"/>' +
    '<path d="M15 9a4.2 4.2 0 0 1 0 6"/><path d="M17.8 6.6a8 8 0 0 1 0 10.8"/></svg>';
  /* unlockAudio runs synchronously INSIDE this tap: hear-last can play the
     Mac voice through WebAudio without ever passing the start-call tap */
  hear.addEventListener('click', ev => { ev.stopPropagation(); unlockAudio(); hearLast(s, hear); });

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

/* roster poll: ~10s while home is visible, ~8s with the control room open,
   ~20s in the background of a live call, ~30s otherwise. Background flips
   (working to needs-you, working to idle) get a toast and a soft chime so
   parallel sessions can call for attention; tapping the toast moves the call
   to that session. The home header dot mirrors relay reachability. */
const prevSess = {};
let sessBusy = false;
async function pollSessions(){
  if(sessBusy) return;
  sessBusy = true;
  const list = await fetchSessions();
  sessBusy = false;
  homeDot.classList.toggle('ok', !!list);
  homeDot.setAttribute('aria-label', list ? 'relay connected' : 'relay unreachable');
  if(!list){
    if(sessOpen) renderSessions(null);
    if(onHome) renderHome(null);
    return;
  }
  lastRoster = list;
  const cur = list.find(s => s.current);
  if(cur){
    if(cur.id) currentSid = cur.id;
    if(cur.name){
      pillName.textContent = cur.name;
      $('pill').setAttribute('aria-label', 'Session: ' + cur.name + '. Open control room.');
      if(chatPos === 'full' && chatOpenState && mode !== 'chat') chatTitle.textContent = cur.name;
    }
    if(wantStartName && !live){
      $('startTitle').textContent = cur.name || 'voicebridge';
      wantStartName = false;
    }
  }
  for(const s of list){
    if(!isActiveSess(s)) continue;   // closed transcripts never flip state
    const key = s.id || s.name;
    if(!key) continue;
    const prev = prevSess[key];
    if(prev && live && !s.current){
      if(!prev.pending && s.pending){
        toast((s.name || 'a session') + ' needs you', s); chime();
      }else if(!s.pending && isWorkingState(prev.state) && !isWorkingState(s.state)){
        toast((s.name || 'a session') + ' finished', s); chime();
      }
    }
    prevSess[key] = { state: s.state, pending: !!s.pending };
  }
  if(sessOpen) renderSessions(list);
  if(onHome) renderHome(list);
}
(async function sessionsLoop(){
  for(;;){
    try{ await pollSessions(); }catch(e){}
    await sleep(sessOpen ? 8000 : (live ? 20000 : (onHome ? 10000 : 30000)));
  }
})();

/* toast: tapping goes to the session it is about (switch + call screen) */
let toastTimer = null, toastSess = null;
function toast(msg, sess){
  toastSess = sess || null;
  toastText.textContent = msg;
  toastEl.classList.add('show');
  if(toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 4500);
}
toastEl.addEventListener('click', () => {
  toastEl.classList.remove('show');
  const s = toastSess; toastSess = null;
  if(s){
    if(!isActiveSess(s)){ openClosedSheet(s); return; }   // belt and braces
    if(live) switchTo(s);
    else openSession(s);
    return;
  }
  if(!onHome) openSessSheet();
});

/* ============================================================ switching */
const switchOverlay=$('switchOverlay'), switchMsg=$('switchMsg');
async function switchTo(s){
  closeSessSheet();
  /* never point the call at a closed session: read-only sheet instead */
  if(!isActiveSess(s)){ openClosedSheet(s); return; }
  if(s.current) return;
  /* the "ending previous call" affordance: freeze the loops, show intent */
  switchMsg.textContent = 'ending previous call';
  switchOverlay.classList.remove('hidden');
  cancelTurn(); stopSpeaking(); stopListening();
  let ok = false;
  try{
    const r = await jpost('/switch', { id: s.id });
    ok = r.ok;
  }catch(e){}
  if(ok){
    if(s.id) currentSid = s.id;
    pillName.textContent = s.name || 'session';
    $('pill').setAttribute('aria-label', 'Session: ' + pillName.textContent + '. Open control room.');
    if(chatPos === 'full' && chatOpenState && mode !== 'chat') chatTitle.textContent = pillName.textContent;
    switchMsg.textContent = 'connected to ' + pillName.textContent;
    resetChat();                              // new session, new transcript
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

/* ============================================================ navigation */
/* HOME is the resting screen; the call screen (orb, chip, mode control)
   sits under it. Tapping an ACTIVE home card repoints the relay and lands
   on the idle orb, where the call chip's tap grants the mic; a needs-you
   card works the same and the permission panel surfaces right after
   connecting (immediate /status check). Closed cards open the read-only
   sheet and go nowhere near the call screen. The back chevron ends any
   live call and returns home. */
function goCall(name){
  onHome = false;
  document.body.classList.remove('home');
  if(name){
    pillName.textContent = name;
    $('pill').setAttribute('aria-label', 'Session: ' + name + '. Open control room.');
  }
  resetStartOverlay(name);
  startOverlay.classList.add('hidden');   // v8: the chip starts the call
  setState('ended', 'ready to call');
}
function goHome(){
  onHome = true;
  cancelTurn(); stopListening(); stopSpeaking(); stopHeartbeat(); releaseWakeLock();
  setMuted(false);
  syncLiveUi();
  setMode('voice');
  hideDecision(); closeSessSheet(); closeClosedSheet(); closeSetSheet(); closeChatSheet();
  startOverlay.classList.add('hidden');
  setState('ended', 'call ended');
  document.body.classList.add('home');
  renderHome(lastRoster);
  pollSessions();
}
async function openSession(s){
  if(!isActiveSess(s)){ openClosedSheet(s); return; }   // guard every entry path
  goCall(s.name || 'session');
  if(s.id && s.id !== currentSid) resetChat();
  if(s.id) currentSid = s.id;
  if(!s.current){
    let ok = false;
    try{ ok = (await jpost('/switch', { id: s.id })).ok; }catch(e){}
    if(!ok){ goHome(); return; }
  }
  pollSessions();
  refreshChat();
}
async function backHome(){
  if(live){
    switchMsg.textContent = 'ending call';
    switchOverlay.classList.remove('hidden');
    live = false; liveGen++;
    cancelTurn(); stopListening(); stopSpeaking(); stopHeartbeat(); releaseWakeLock();
    syncLiveUi();
    await sleep(700);
    switchOverlay.classList.add('hidden');
  }
  goHome();
}
$('backBtn').addEventListener('click', backHome);

/* ============================================================ boot */
/* No auto-call: the page opens on home (roster primed below). A deep link
   with &s= checks the roster FIRST: a deep link to a CLOSED session must
   never reach the call screen, it lands on home with the read-only sheet.
   A live target skips home, repoints the relay, and rests on the idle orb
   (the chip starts the call); if the switch fails the page falls back to
   home. */
if(S){
  document.body.classList.remove('home');
  setState('ended', 'ready to call');
  (async () => {
    let row = null;
    try{
      const list = await fetchSessions();
      if(list){ lastRoster = list; row = list.find(x => x.id === S) || null; }
    }catch(e){}
    if(row && !isActiveSess(row)){
      wantStartName = false;
      goHome();
      openClosedSheet(row);
      return;
    }
    let ok = false;
    try{ ok = (await jpost('/switch', { id: S })).ok; }catch(e){}
    if(!ok){ wantStartName = false; goHome(); return; }
    pollSessions();
    refreshChat();
  })();
}
syncLiveUi();
renderSeg();
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
        # Never cache: phones held onto old page versions ("I see no change")
        # and stale rosters. The page is one request; freshness wins.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        secret = _secret()   # live: a rotated secret works without restart
        if not secret:
            # Fail CLOSED. This used to serve everything unauthenticated when
            # no secret was configured, and "no secret" is not a rare state:
            # deleting the state file or running `vb call on` by hand reaches
            # it. Behind a public tunnel that is an open door for typing into
            # someone's Mac, so an unconfigured relay answers nothing instead.
            return False
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
                     "active": bool(r.get("active")),
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
        elif path == "/events":
            # SSE: ONE long-lived connection instead of lossy request-by-
            # request polling. Pushes replies (with uuid), delivery acks, and
            # permission moments the second they happen; a comment ping every
            # ~10s keeps proxies from idling the stream out; EventSource on
            # the phone reconnects automatically after any hiccup.
            if not self._authed():
                self._reply(401, b"unauthorized", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit(kind, payload):
                self.wfile.write(
                    (f"event: {kind}\ndata: {json.dumps(payload)}\n\n")
                    .encode())
                self.wfile.flush()

            q = _queue.Queue()
            with _SUBS_LOCK:
                _SUBS.add(q)
            try:
                tp = _target_transcript()
                last_u = core.latest_assistant_uuid(tp) if tp else ""
                emit("hello", {"uuid": last_u})
                last_pend, last_size, ticks, last_tp = "", -1, 0, tp
                while True:
                    try:
                        ev = q.get(timeout=1.0)   # acks etc, or a 1s tick
                        emit(ev.get("type", "event"), ev)
                    except _queue.Empty:
                        pass
                    tp = _target_transcript()
                    if tp != last_tp:
                        # Session switched: re-baseline SILENTLY, or the new
                        # session's months-old last reply plays as if fresh.
                        last_tp = tp
                        last_u = core.latest_assistant_uuid(tp) if tp else ""
                        last_size = -1
                        emit("switched", {"uuid": last_u})
                        continue
                    # stat() gate: only re-parse the transcript when it GREW,
                    # a multi-MB parse per second per stream would burn CPU.
                    try:
                        size = os.path.getsize(tp) if tp else -1
                    except OSError:
                        size = -1
                    if size != last_size:
                        last_size = size
                        u = core.latest_assistant_uuid(tp) if tp else ""
                        if u and u != last_u:
                            last_u = u
                            cur = core.clean_for_speech(
                                core.last_assistant_text(tp), max_chars=2500)
                            emit("reply", {"uuid": u, "reply": cur})
                    pend = core.get_pending_notice(_active_sid())
                    pend = (core.clean_for_speech(pend, max_chars=300)
                            if pend else "")
                    if pend != last_pend:
                        last_pend = pend
                        emit("pending", {"q": pend})
                    ticks += 1
                    if ticks % 10 == 0:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except Exception:
                pass   # client went away; EventSource will reconnect
            finally:
                with _SUBS_LOCK:
                    _SUBS.discard(q)
            return
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
            # uuid lets the page track "is this reply NEW" robustly (text
            # diffing breaks on trims/caps) and powers the idle reply-watcher
            # that speaks desktop-initiated replies during a live call.
            self._reply(200, json.dumps(
                {"reply": core.clean_for_speech(cur, max_chars=2500),
                 "uuid": core.latest_assistant_uuid(tp) if tp else ""}
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

        if path == "/ask":  # web-call page: {"text","id"} -> {"reply"}
            try:
                body = json.loads(raw or b"{}")
                text = (body.get("text") or "").strip()
                turn_id = str(body.get("id") or "")
            except Exception:
                self._reply(400, b"bad request", "text/plain")
                return
            want_stream = bool(body.get("stream"))
            if want_stream and turn_id:
                # Non-blocking protocol: inject (idempotently), ACK at once
                # with the reply-uuid BASELINE; completion arrives only on
                # the event stream. One channel, one truth.
                if turn_id in _ASKED:
                    self._reply(200, json.dumps(
                        {"ok": True, "delivered": True,
                         "uuid": _ASKED[turn_id].get("base", "")}).encode(),
                        "application/json")
                    return
                tp0 = _target_transcript()
                base = core.latest_assistant_uuid(tp0) if tp0 else ""
                why = _inject_only(text) if text else "Sorry, I didn't catch that."
                if why:
                    self._reply(200, json.dumps(
                        {"ok": False, "reply": why}).encode(),
                        "application/json")
                    return
                _ASKED[turn_id] = {
                    "tp": tp0,
                    "prev": core.last_assistant_text(tp0) if tp0 else "",
                    "base": base, "ts": time.time()}
                _prune_asked()
                _broadcast({"type": "ack", "id": turn_id})
                self._reply(200, json.dumps(
                    {"ok": True, "delivered": True, "uuid": base}).encode(),
                    "application/json")
                return
            # Idempotency: the page retries a failed POST with the SAME id
            # (tunnels drop requests). A retry of a turn that already injected
            # must never paste the prompt twice, it just resumes waiting.
            if turn_id and turn_id in _ASKED:
                answer = _await_reply(_ASKED[turn_id])
            else:
                if turn_id:
                    tp0 = _target_transcript()
                    _ASKED[turn_id] = {
                        "tp": tp0,
                        "prev": core.last_assistant_text(tp0) if tp0 else "",
                        "ts": time.time()}
                    _prune_asked()
                answer = (_ask_session(text, turn_id) if text
                          else "Sorry, I didn't catch that.")
            self._reply(200, json.dumps({"reply": answer}).encode(),
                        "application/json")
            return

        if path == "/tts":
            # Synthesize with the Mac's Kokoro voice and return the WAV, so
            # the phone can play the SAME natural voice as the desktop
            # instead of the browser's robotic default. Body: {"text": ...}.
            try:
                b = json.loads(raw or b"{}")
                text = (b.get("text") or "").strip()
                voice = (b.get("voice") or "").strip()
            except Exception:
                self._reply(400, b"bad request", "text/plain")
                return
            import tempfile
            wav = ""
            if text and core.kokoro_up():
                # Per-request temp file: the relay is threaded, a shared
                # output path would let concurrent chunks clobber each other.
                fd, tmp = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                wav = core._kokoro_wav(text[:600], out=tmp, voice=voice)
                if wav != tmp:
                    # mkstemp already created the file, so a synthesis that
                    # fails (or writes elsewhere) leaves it behind: one empty
                    # file per failed chunk, and the page retries per chunk.
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            if wav:
                try:
                    with open(wav, "rb") as f:
                        self._reply(200, f.read(), "audio/wav")
                    return
                except Exception:
                    pass
                finally:
                    try:
                        os.remove(wav)
                    except OSError:
                        pass
            self._reply(503, b"kokoro unavailable", "text/plain")
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
        _write_secret(s)
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


def tunnel_pids(ps_output: str, port: int) -> list:
    """Every cloudflared in `ps -axo pid=,args=` output that is tunnelling OUR
    relay port. Matching on the port (not just the name) is deliberate: a
    cloudflared you run for your own work must survive `vb call off`."""
    want = (f"--url http://127.0.0.1:{port}", f"--url http://localhost:{port}")
    pids = []
    for line in ps_output.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, args = line.partition(" ")
        if "cloudflared" not in args:
            continue
        if any(w in args for w in want):
            try:
                pids.append(int(head))
            except ValueError:
                pass
    return pids


def reap_tunnels(keep: int = 0) -> int:
    """Stop every tunnel pointing at this relay, not just the last one.

    Only the pid in tunnel.pid was ever killed, so each `vb phone` orphaned the
    tunnel before it: publicly reachable URLs into this Mac that nothing would
    ever close, still live days later. Returns how many were stopped."""
    import signal

    def _ours() -> list:
        try:
            out = subprocess.run(["ps", "-axo", "pid=,args="],
                                 capture_output=True, text=True,
                                 timeout=5).stdout
        except Exception:
            return []
        return [p for p in tunnel_pids(out, PORT)
                if p not in (keep, os.getpid())]

    victims = _ours()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        # cloudflared drains connections on SIGTERM and can sit there for a
        # while. "The link is closed" has to be true the moment we say it, so
        # anything still serving after the grace period is killed outright.
        if not _ours():
            break
        for pid in _ours():
            try:
                os.kill(pid, sig)
            except Exception:
                pass
        time.sleep(0.4)
    n = len(victims) - len(_ours())
    if n:
        core.log(f"call: stopped {n} tunnel(s) on port {PORT}")
    return n


def off() -> str:
    try:
        os.kill(int(PID.read_text().strip()), 15)
    except Exception:
        pass
    try:
        PID.unlink()
    except FileNotFoundError:
        pass
    # Every tunnel on our port, not only the recorded one: the recorded pid is
    # just the most recent, and an old QR still routes into this Mac.
    n = reap_tunnels()
    tp = core.STATE_DIR / "tunnel.pid"
    try:
        os.kill(int(tp.read_text().strip()), 15)
    except Exception:
        pass
    try:
        tp.unlink()
    except FileNotFoundError:
        pass
    return f"call relay OFF ({n or 'no'} tunnel(s) stopped)"


def status() -> str:
    return "\n".join([
        f"relay  : {'running' if _alive() else 'stopped'} (port {PORT})",
        f"secret : {'set' if SECRET else '(none - set VB_CALL_SECRET!)'}",
        f"target : {_target_transcript() or '(no session)'}",
    ])

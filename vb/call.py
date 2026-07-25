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
_ASKED_FILE = core.STATE_DIR / "asked.json"


def _asked_load() -> None:
    """Restore the turn ledger after a relay restart: without this, a client
    retrying an already-injected turn against a fresh relay would inject the
    same prompt a second time (critic finding #8)."""
    try:
        _ASKED.update(json.loads(_ASKED_FILE.read_text()))
    except Exception:
        pass


def _asked_save() -> None:
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _ASKED_FILE.write_text(json.dumps(_ASKED))
    except Exception:
        pass

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
    _asked_save()   # ledger survives relay restarts (idempotency holds)


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
  voicebridge call page v14 (file: call_page_v14.html). Drop-in replacement
  for PAGE in vb/call.py. Embed as a RAW string (r prefix) so regex
  backslashes survive. This file contains no triple double-quote sequence
  anywhere, so it is safe inside a Python raw triple-quoted string.
  100% ASCII.

  CHANGES, v12 base to v14, each brief item and where it lives:

    1. TOP BAR, fixed with safe-area insets. The back chevron (#backBtn,
       44px, its own fixed button so it can float over the start overlay)
       still ends the call confirm-free through backHome() with the brief
       "ending call" beat on the switch overlay. The session pill (#pill,
       state dot + name, 15px/600) is centered in the new fixed header and
       opens the control room sheet; the settings gear (#setBtn, 44px)
       mirrors it on the right. CSS: "header" block + #pill + #backBtn +
       #setBtn.
    2. AUDIO-ROUTE PILL directly under the bar: #chip, NON-tappable
       (pointer-events:none), inline speaker-with-arcs + smartphone-outline
       SVGs plus the text "Sound on this phone" (12px, dim). Shown only
       while heartbeats land (beatOnce() toggles .on). No handset glyph,
       no "audio" wording anywhere in it.
    3. CENTER: the v12 orb rendering is untouched (gradient, breathe,
       sheen, ripples, speakglow). A permanent state word (#stateWord,
       13px/600, uppercase, .08em tracking, ui-rounded stack) sits under
       the orb and mirrors the state: Listening / Thinking / Speaking /
       Muted / Needs you, set inside setState(). The old #status line is
       demoted to a small detail row (elapsed time, stitch countdown,
       "typing"); it never repeats the state word. Only the orb is
       tappable in the center (stop-the-voice, hushVoice()).
    4. BOTTOM CONTROL ROW in the thumb zone, safe-area padded, 40px gaps
       (>= 24px), every control in a .ctlwrap with an 11px/500 label:
       - MUTE (#muteBtn, 56px): one inline mic SVG (rounded capsule +
         stand arc + stem) plus a 45-degree slash group. Unmuted:
         translucent white chip, label "Mute". Muted: chip fills #E5484D,
         the slash shows (white line over a 2px-equivalent casing stroke
         painted in the chip color so the mic reads through it), label
         flips to "Muted" (#muteLbl). The slash shows the CURRENT state.
       - END (#endBtn, 64px): solid #E5484D circle, white HORIZONTAL
         on-hook handset, label "End". The only red-by-default control.
       - CHAT (#chatBtn, 56px): speech bubble, label "Chat", mint unread
         dot (.unread::after) when replies land while chat is closed.
    5. CHAT IS A FULL-SCREEN MODE now (#chatSheet fills the viewport; the
       drag gesture, half state, grab handle and size chevron are RETIRED,
       code and CSS both). Entered via the Chat control, exited via the
       chat header back chevron (#chatBackBtn) AND hardware back
       (pushState on open, popstate closes, unchanged plumbing). The chat
       header holds the back chevron, the session name (#chatTitle), a
       state dot mirroring the orb color (#chatDot, var(--o2)), and the
       speaker-x hush button (#hushBtn) shown only while speaking.
    6. TRANSCRIPT, ChatGPT convention: USER messages are right-aligned
       mint bubbles (.bub.user, max-width 85%); AGENT replies are OPEN
       full-width text on the background (.msg-a, max column 65ch). Body
       text 16px/1.5 on the system-ui chat stack (16px also stops the iOS
       zoom-on-focus in the composer). Code blocks (.cblk) are 13px/1.45
       ui-monospace cards on #0a0e18 with their own overflow-x so they
       never widen the page. Timestamps render as grouped cluster
       dividers (.tsdiv, 11.5px, dim, centered) when >= 10 minutes pass
       between stamped messages, never per-message; refreshChat()
       preserves stamps across server re-renders by role+text matching.
    7. KEPT IN CHAT, wired as before: show-more clamp on long replies;
       per-message copy button on user AND agent messages (top-right of
       the text block); jump-to-bottom pill (#jumpBtn, same open+scrolled
       up+appended rules); "Claude is working" three-dot row while
       turnActive (syncTyping); pull-to-refresh; composer with "type
       instead of talking", focus mutes the mic with status "typing",
       Enter sends, visualViewport keyboard lift via --kb. NEW: delivery
       state row (#deliveryRow / syncDelivery()) under the last user
       bubble, "sending" / "delivered" / "failed, tap to retry", wired to
       askDelivered and pendingSend; tapping retries with the same
       idempotent key.
    8. FONT STACKS, exact: display/state/labels = ui-rounded, "SF Pro
       Rounded", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif
       (body). Chat body = system-ui, -apple-system, "Segoe UI", Roboto,
       "Helvetica Neue", Arial, sans-serif (.bub, .msg-a, #composeIn,
       .tsdiv, .dstate, .typing). Code = ui-monospace, "SF Mono",
       SFMono-Regular, Menlo, Consolas, "Roboto Mono", "Liberation Mono",
       monospace (.cblk, .ichip).
    9. SETTINGS SHEET kept: source toggle (Phone / Natural voice) plus
       the Heart / Bella / Michael voice cards (#voiceCards), restyled to
       the new type roles; each hint is one sentence. The chosen Kokoro
       id persists (vbvoice_name) and rides every /tts body as
       {"voice": id} (fetchTts).
   10. STATES: all v12 orb colors and motion kept; the state word always
       mirrors them; reduced-motion keeps color + text (animations off,
       ripples static). Battery: home parks the call screen (visibility)
       and full-screen chat parks the orb/controls the same way.

  Everything below this line in the older notes still applies to the
  turn engine, stream protocol, stitching, barge-in and iOS quirks.

  CHANGES v6 to v7, a user-perspective overhaul of the chat experience:

    1. THREE-POSITION CHAT SHEET (half / FULL SCREEN / closed).
       User problem: "the sheet looks draggable but is not, and I cannot
       read a long reply in a half-height window while you are talking".
       - The grab handle (and the whole sheet header) is now really
         draggable: drag up from half to go full screen, drag down to
         collapse to half or close; a flick works too (velocity, not just
         distance). An expand/collapse chevron in the sheet header does the
         same by tap.
       - FULL SCREEN hides the orb, the call controls, and the corner
         buttons (visibility, so the call itself keeps running untouched);
         a slim top bar shows the SESSION NAME plus the collapse chevron,
         and the transcript fills the screen, safe-area insets respected.
         The permission panel and toasts are re-stacked ABOVE the full
         sheet so "needs you" can never hide behind the chat.
       - The reply bubble is appended BEFORE speech starts (existing turn
         engine order), so with the sheet open the user reads along in
         full while the voice talks: the core "I just want to see the chat
         while you are talking" case.
       - The sheet remembers its last position (half or full) for the rest
         of the call and reopens there; a new call resets to half.
       - Hardware/gesture BACK closes the sheet instead of leaving the
         page (history.pushState on open, popstate closes).
    2. "NEW MESSAGES" PILL MADE BULLETPROOF. Two real bugs found and
       fixed: (a) the page had NO .hidden CSS rule, so classList.add(
       'hidden') never actually hid the pill, it was visible whenever the
       sheet was; (b) the pill had NO click handler, tapping it did
       nothing. Now: targeted #jumpBtn.hidden/#homeFilter.hidden display:
       none rules; the pill may appear ONLY when the sheet is open AND the
       user has scrolled up more than ~150px from the bottom AND content
       was appended after they scrolled up (both the live-append path and
       the server refreshChat re-render path obey this, and the re-render
       now PRESERVES the reading position instead of yanking to the
       bottom). Opening the sheet always scrolls to the bottom and hides
       the pill; a scroll listener hides it the moment the user reaches
       the bottom; tapping it jumps to the newest message.
    3. TYPED COMPOSER at the bottom of the chat sheet.
       User problem: "I am in a meeting / a quiet room and cannot speak."
       A text field plus send button submits through the EXACT same turn
       engine as speech (startTurn: same idempotent /ask key, same SSE
       completion, same polling backstop). Focusing the field auto-mutes
       the mic (stops the recognizer and the barge monitor, status shows
       "typing"); blur restores listening if the user had not muted
       manually. Enter sends. Typing while a reply is speaking cancels the
       speech, same as barge-in. The sheet lifts above the on-screen
       keyboard via visualViewport (--kb custom property).
    4. "CLAUDE IS WORKING" TYPING-INDICATOR ROW while turnActive.
       User problem: with the chat open the user saw their bubble and then
       nothing; the orb (which shows progress) may be hidden in full
       screen. A subtle assistant-side row with three pulsing dots sits at
       the end of the transcript for the whole working phase (static dots
       under prefers-reduced-motion).
    5. COPY BUTTON ON EVERY BUBBLE with a "copied" toast.
       User problem: "Claude read out an error message / a command and I
       need it as text on my phone." A small always-visible copy icon
       floats in the top-right of each bubble (a button, not long-press:
       long-press already means text selection on iOS and the two would
       fight). navigator.clipboard with a hidden-textarea execCommand
       fallback for older browsers.
    6. STOP THE VOICE BY TOUCH: tap the orb, or the speaker-off button
       that appears in the chat top bar while a reply is speaking.
       User problem: reading along in the chat, the user often finishes
       before the voice does (or someone walks in); barge-in requires
       SPEAKING, which is exactly what they cannot do, and in full-screen
       chat the orb is hidden, so the chat bar needs its own stop surface.
       Either one silences the reply and drops straight back into
       listening (or muted/working, whatever the call state wants).
    7. UNREAD DOT on the chat button.
       User problem: with the sheet closed, a reply that arrived while the
       phone was in a pocket left no visible trace. A small mint dot on
       the chat control marks unseen assistant messages; opening the sheet
       clears it.
    All of it self-contained (no CDNs), ASCII only, reduced-motion aware;
    the turn engine, stream protocol, barge-in, stitching, permission
    panel, home screen, and Mac voice pipeline are untouched except where
    these features required a hook.

  CHANGES v5 to v6:
    VOICE-SOURCE TOGGLE: a small gear on the call screen opens a settings
    sheet with one option, "Voice: Phone / Mac (natural)". Default stays
    Phone (browser SpeechSynthesis); the choice persists in localStorage
    (key vbvoice). When Mac is selected, replies are synthesized by the
    Mac's Kokoro neural voice: the text splits into sentence chunks of
    <=300 chars (the server accepts ~600), each chunk POSTs to /tts, and
    the returned WAVs play SEQUENTIALLY and gap-free through WebAudio
    (decodeAudioData + AudioBufferSourceNode; the AudioContext is already
    unlocked in the start tap, which is what makes this reliable on iOS).
    Chunk N+1 is prefetched while chunk N plays, an audio pipeline, so the
    only wait is the first chunk. LATENCY NOTE: that first audio lands
    about a second later than local synthesis; that is the tradeoff for
    the natural voice. Barge-in still works: the RMS monitor cancels the
    WebAudio playback path too, not just speechSynthesis. Any non-200 or
    a 4s per-chunk timeout falls back automatically and silently to the
    phone voice for the rest of the reply (one-time toast "Mac voice
    unavailable, using phone voice"); two consecutive failed replies stop
    trying the Mac voice for the rest of the session.

  CHANGES v4 to v5:
    1. BARGE-IN: while a reply is being spoken, the user starting to talk
       interrupts it and becomes the next prompt. A mic monitor runs during
       speechSynthesis playback (getUserMedia with echoCancellation and
       noiseSuppression, AnalyserNode RMS; phone AEC removes the device's
       own speaker output, so sustained voice means the human). RMS above
       threshold for ~350ms (7 x 50ms frames, never a single spike) cancels
       the synthesis, plays a tiny WebAudio acknowledgment tick, and routes
       into the normal listening flow. The whisper path reuses the already
       open stream. Guards: no barge in the first 600ms of speech (echo
       settle); monitor stream released when speaking ends; if getUserMedia
       fails the page degrades to no barge (v4 behavior).
    2. PAUSE TOLERANCE / STITCHING: end-of-speech no longer sends the
       prompt immediately. The transcript goes into a stitch buffer and a
       follow-up window holds the send: 1.6s by default, 2.4s when the text
       ends in a comma or a trailing conjunction (and/or/but/so/...), 0.9s
       when it ends in . ? or !. The mic stays open; resuming speech
       cancels the pending send, appends the continuation, and re-arms the
       window. The whisper path now requires ~1.4s of RMS silence to end an
       utterance (was 1.5s at a coarser tick, effectively shorter). The
       status line shows "listening . . ." with shrinking dots as a subtle
       countdown, orb stays in the listening color, so it never feels stuck.
    3. ACTIVE vs INACTIVE SESSIONS: /sessions rows carry "active".
       Home renders two groups: "Active" (normal cards, tappable to call)
       and "Earlier" (dimmed, NOT tappable into a call; tapping opens a
       small read-only sheet with the last reply and a closed-session
       note). No call, prompt, or switch can target an inactive session,
       from home, the control room, a toast, or an &s= deep link; a deep
       link to an inactive session lands on the read-only sheet instead of
       the call screen. Rows without "active" (older server) act live.
    4. iOS HARDENING, each fix commented at the site:
       - TTS warm-up utterance spoken inside the Start tap gesture
       - AudioContext created/resumed inside the same tap
       - voices load async: re-picked on voiceschanged, per chunk
       - synthesis chunked to <=170 chars (a stall loses one chunk), a
         resume pump for the screen-lock pause, a per-chunk watchdog for
         utterances iOS drops without end or error events
       - webkitSpeechRecognition needs Siri and Dictation: 2 consecutive
         service-not-allowed errors permanently fall back to the whisper
         path for this session
       - /stt uploads send the real MediaRecorder mimeType (audio/mp4 on
         iOS) as the Content-Type
       - no Screen Wake Lock on older iOS: no fake keep-awake tricks, just
         thorough visibilitychange recovery (resume TTS, re-arm lock where
         supported, restart the mic, kick a heartbeat)
       - Add to Home Screen standalone: body.standalone class from
         navigator.standalone / display-mode, safe-area floors for the top
         rows where older devices report a 0 inset

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
                      the HOME groups (Active / Earlier) and the control room
                      sheet. Polled ~10s while home is visible, ~8s while the
                      control room sheet is open, ~20s in the background of a
                      live call, ~30s otherwise. "ago" renders 45 "45s",
                      3000 "50m", 7200 "2h".
    GET  /last?q=NAME {"reply":"..."} latest reply of the named session,
                      spoken WITHOUT switching the call; also fills the
                      read-only sheet for inactive sessions.
    POST /switch      body {"id":"..."} repoints the call at that session.
                      Sent by home cards, the &s= deep link, toast taps, and
                      the control room sheet; never sent for inactive rows.
    GET  /chat        {"turns":[{"role":"user"|"assistant","text":"..."}]}
                      most recent last, cleaned for reading. Renders the chat
                      sheet; refreshed after each completed turn and on
                      pull-to-refresh. Page degrades to its local transcript
                      when the endpoint is missing.
    POST /heartbeat   empty body, every 5s while the call is live. Freshness
                      tells the Mac to keep its own speakers quiet; the page
                      shows the "Sound on this phone" pill while beats land.
    POST /stt         audio blob returns {"text":"..."}, the whisper fallback
                      when native SpeechRecognition is absent or disabled.
                      The page sets Content-Type to the recorder's real
                      mimeType (audio/webm on Android, audio/mp4 on iOS).
    POST /tts         body {"text":"...","voice":"af_heart|af_bella|
                      am_michael"} returns audio/wav synthesized by the
                      Mac's Kokoro neural voice; 503 when Kokoro is
                      unavailable; max ~600 chars per request (the page
                      sends <=300). Used only when the settings sheet has
                      Voice set to "Natural voice"; every failure path
                      falls back to the phone's SpeechSynthesis.

  All inline, no external assets or CDNs. iOS Safari and Android Chrome
  quirks handled as listed above, plus the data-URI manifest that preserves
  ?k= (and &s= when the page was opened with one) on Add to Home Screen,
  safe-area insets, prefers-reduced-motion, and parked animations while the
  home screen is visible (reduced battery burn).
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
  font-family:ui-rounded, "SF Pro Rounded", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
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
/* v5: group labels for the Active / Earlier split */
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
/* v5: inactive (closed) sessions render dimmed and never start a call */
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

/* ==== back chevron: call screen only, floats over the start overlay too ==== */
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

/* ==== v6: settings gear (call screen only, mirrors the back chevron) ==== */
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

/* v6: settings sheet rows + segmented voice control */
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
/* the curated Natural voices: three cards under the source toggle */
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
.vcard .vd { font-size:11px; font-weight:500; color:var(--dim); text-align:center; line-height:1.35; }

/* iOS A2HS standalone quirk: with a black-translucent status bar the page
   sits under the clock, and some older devices report a 0 top inset in
   standalone mode. Give the top rows a hard floor so nothing hides. */
body.standalone .hhead { padding-top:max(calc(env(safe-area-inset-top, 0px) + 24px), 46px); }
body.standalone header { padding-top:max(calc(env(safe-area-inset-top, 0px) + 12px), 34px); }
body.standalone #backBtn { top:max(calc(env(safe-area-inset-top, 0px) + 12px), 34px); }
body.standalone #setBtn { top:max(calc(env(safe-area-inset-top, 0px) + 12px), 34px); }

/* ==== top bar: fixed, safe-area. The back chevron and the gear are their
   own fixed 44px buttons (z:65, they float over the start overlay); the
   header carries the centered session pill and, DIRECTLY UNDER the bar,
   the non-tappable audio-route pill. ==== */
header {
  position:fixed; top:0; left:0; right:0; z-index:55;
  padding:calc(env(safe-area-inset-top, 0px) + 12px) 64px 0;
  display:flex; flex-direction:column; align-items:center; gap:8px;
  pointer-events:none;
}
#pill {
  pointer-events:auto;
  display:flex; align-items:center; gap:8px;
  min-height:44px; padding:10px 16px; border-radius:999px;
  background:rgba(255,255,255,.055); border:1px solid var(--line);
  font-size:15px; font-weight:600; color:#dfe4ee; max-width:58vw;
}
#pill .dot { width:8px; height:8px; border-radius:50%; background:var(--o2); flex:none;
  transition:background .9s ease; }
#pill .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
/* the audio-route pill: pure status, never a button, never a handset glyph */
#chip {
  display:none; align-items:center; gap:6px;
  padding:5px 12px; border-radius:999px;
  background:rgba(255,255,255,.04); border:1px solid var(--line);
  font-size:12px; letter-spacing:.02em; color:var(--dim);
  pointer-events:none;
}
#chip.on { display:inline-flex; }
#chip svg { width:13px; height:13px; flex:none; opacity:.85; }
@keyframes softpulse { 0%,100% { opacity:1; } 50% { opacity:.35; } }

/* ==== middle: the orb (v12 rendering kept as-is) ==== */
main { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center;
  gap:24px; min-height:0;
  padding-top:calc(env(safe-area-inset-top, 0px) + 100px); }
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

/* the permanent one-word state label under the orb (ui-rounded chain via
   the body font); #status is demoted to a small detail line beneath it */
#stateWord {
  font-size:13px; font-weight:600; text-transform:uppercase;
  letter-spacing:.08em; text-indent:.08em; /* balance tracking */
  color:#b9c2d4; min-height:18px; text-align:center;
  transition:color .4s ease;
}
body[data-state="needs"] #stateWord { color:#f0a294; }
body[data-state="listening"] #stateWord { color:#7fe0d2; }
body[data-state="speaking"] #stateWord { color:#aac6f6; }
body[data-state="thinking"] #stateWord { color:#b3a6f2; }
#status {
  font-size:12.5px; letter-spacing:.06em; text-indent:.06em;
  text-transform:lowercase; color:var(--dim); min-height:18px; text-align:center;
  padding:0 24px; font-variant-numeric:tabular-nums; margin-top:-14px;
}
body[data-state="needs"] #status { color:#f0a294; }

/* ==== bottom: the control row (thumb zone). Mute / End / Chat, each with
   an 11px/500 label beneath; End is the only red-by-default control. ==== */
footer {
  display:flex; align-items:flex-start; justify-content:center; gap:40px;
  padding:10px 24px calc(env(safe-area-inset-bottom, 0px) + 22px);
}
.ctlwrap { display:flex; flex-direction:column; align-items:center; gap:7px; min-width:64px; }
.ctllbl { font-size:11px; font-weight:500; color:var(--dim); letter-spacing:.02em; }
.ctl {
  width:56px; height:56px; border-radius:50%; position:relative;
  display:flex; align-items:center; justify-content:center;
  background:rgba(255,255,255,.07); border:1px solid var(--line);
  transition:background .25s ease, transform .1s ease;
}
.ctl:active { transform:scale(.93); }
.ctl svg { width:24px; height:24px; }
#muteBtn { background:rgba(255,255,255,.1); }
/* the 45-degree slash shows the CURRENT state: visible only while muted,
   its casing stroke painted in the chip color so the mic reads through */
#muteBtn .slash { display:none; }
#muteBtn.muted { background:var(--danger); border-color:transparent; color:#fff; }
#muteBtn.muted .slash { display:block; }
#muteBtn .slashcase { stroke:var(--danger); stroke-width:6; }
#muteBtn .slashline { stroke:currentColor; stroke-width:2.2; }
#endBtn { width:64px; height:64px; background:var(--danger); border-color:transparent; color:#fff; }
#endBtn svg { width:28px; height:28px; }
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

/* v5: read-only sheet for closed (inactive) sessions */
#closedSheet .closedtext {
  font-size:15px; line-height:1.55; color:#c7cdd9; white-space:pre-wrap;
  word-break:break-word; user-select:text; -webkit-user-select:text;
  margin:0; padding:2px 4px 8px;
}

/* chat: a FULL-SCREEN MODE (the v7 drag sheet, half state, grab handle
   and size chevron are retired). Slides up over the call screen; the call
   keeps running underneath (the orb/controls hide via body.chat-full,
   visibility only). --kb lifts the composer over the on-screen keyboard
   (set from visualViewport while the composer is focused). */
#chatSheet {
  position:fixed; inset:0; z-index:62;
  background:#0a0d14; border:0; border-radius:0;
  display:flex; flex-direction:column;
  transform:translateY(100%); transition:transform .3s cubic-bezier(.3,.9,.3,1);
  padding:0 0 calc(env(safe-area-inset-bottom, 0px) + 10px + var(--kb, 0px));
}
#chatSheet.open { transform:translateY(0); }
body.chat-full header, body.chat-full main, body.chat-full footer,
body.chat-full #backBtn, body.chat-full #setBtn { visibility:hidden; }
/* "needs you" and toasts must NEVER hide behind the full-screen chat */
body.chat-full #decide { z-index:66; }
body.chat-full #toast { z-index:66; }
/* chat header: back chevron + session name + state dot + hush */
#chatHead {
  flex:none; display:flex; align-items:center; gap:10px;
  padding:calc(env(safe-area-inset-top, 0px) + 10px) 12px 10px;
  border-bottom:1px solid var(--line);
}
body.standalone #chatHead {
  padding-top:max(calc(env(safe-area-inset-top, 0px) + 10px), 34px); }
#chatBackBtn {
  flex:none; width:44px; height:44px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  background:rgba(255,255,255,.07); border:1px solid var(--line);
}
#chatBackBtn:active { transform:scale(.92); }
#chatBackBtn svg { width:20px; height:20px; }
/* the dot mirrors the orb state color, same tween as the pill dot */
#chatDot { flex:none; width:8px; height:8px; border-radius:50%;
  background:var(--o2); transition:background .9s ease; }
#chatTitle {
  margin:0; flex:1; min-width:0; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap;
  font-size:16px; font-weight:600; letter-spacing:.01em; color:#dfe4ee;
}
#hushBtn {
  flex:none; width:38px; height:38px; border-radius:50%;
  display:none; align-items:center; justify-content:center;
  background:rgba(255,255,255,.06); border:1px solid rgba(140,170,240,.35);
  color:#9db9ff;
}
/* stop-the-voice lives in the chat header because the orb (the other stop
   surface) is hidden in chat mode; only shown while a reply speaks */
body[data-state="speaking"] #hushBtn { display:flex; }
#hushBtn:active { transform:scale(.92); }
#hushBtn svg { width:18px; height:18px; }
/* composer: type instead of talking (meetings, quiet rooms) */
.composer { flex:none; display:flex; align-items:center; gap:8px; padding:8px 14px 0; }
#composeIn {
  flex:1; min-width:0; min-height:44px;
  background:#0e1420; border:1px solid var(--line); border-radius:999px;
  color:#e8ebf2; padding:11px 16px;
  /* 16px stops the iOS zoom-on-focus */
  font-size:16px; line-height:1.5;
  font-family:system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
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
  padding:4px 2px; color:var(--dim); font-size:13.5px;
  font-family:system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
.typing .tl { margin-right:4px; }
.typing .td { width:6px; height:6px; border-radius:50%; background:var(--dim);
  animation:softpulse 1.2s ease-in-out infinite; }
.typing .td:nth-child(3) { animation-delay:.2s; }
.typing .td:nth-child(4) { animation-delay:.4s; }
/* copy button: floats top-right INSIDE the text block on user AND agent
   messages (a button, not long-press: long-press means selection on iOS) */
.copybtn {
  float:right; width:30px; height:30px; margin:-4px -8px 2px 8px;
  border-radius:9px; display:flex; align-items:center; justify-content:center;
  color:#8a94a8; background:rgba(10,13,20,.3);
}
.msg-a .copybtn { margin-right:0; background:rgba(255,255,255,.05); }
.copybtn:active { transform:scale(.9); color:#e8ebf2; }
.copybtn svg { width:15px; height:15px; }
/* unread dot: a reply arrived while chat was closed */
#chatBtn.unread::after {
  content:''; position:absolute; top:9px; right:9px;
  width:9px; height:9px; border-radius:50%; background:var(--mint);
  box-shadow:0 0 7px rgba(70,215,195,.8);
}
/* the pill and the home filter hide via a targeted rule so the .hidden
   overlays keep their opacity fade */
#jumpBtn.hidden, #homeFilter.hidden { display:none; }
#pullHint { height:0; overflow:hidden; text-align:center; font-size:12px;
  color:var(--dim); line-height:28px; transition:height .18s ease; flex:none; }
#chatScroll {
  flex:1; min-height:0; padding:0 16px;
  overflow-y:auto; overscroll-behavior:contain; -webkit-overflow-scrolling:touch;
}
#chatLines {
  display:flex; flex-direction:column; gap:12px; padding:10px 0 8px;
  width:100%; max-width:720px; margin:0 auto;
}
/* USER messages only get bubbles: right-aligned, mint tint, max 85%.
   16px/1.5 on the system-ui chat stack. */
.bub {
  max-width:85%; padding:10px 14px; border-radius:18px;
  font-size:16px; line-height:1.5; white-space:pre-wrap; word-break:break-word;
  font-family:system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  user-select:text; -webkit-user-select:text;
}
.bub.user { align-self:flex-end; background:rgba(70,215,195,.15);
  border:1px solid rgba(70,215,195,.22); border-bottom-right-radius:6px; color:#e7fffa; }
.bub.live { border-style:dashed; }
/* AGENT replies: OPEN full-width text on the background, max column 65ch */
.msg-a {
  align-self:flex-start; width:100%; max-width:65ch; padding:2px 0;
  font-size:16px; line-height:1.5; white-space:pre-wrap; word-break:break-word;
  font-family:system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  color:#e8ebf2; user-select:text; -webkit-user-select:text;
}
.bub .bwrap, .msg-a .bwrap { display:block; }
.bub.clamp .bwrap, .msg-a.clamp .bwrap { max-height:19em; overflow:hidden;
  -webkit-mask-image:linear-gradient(#000 72%, transparent);
  mask-image:linear-gradient(#000 72%, transparent); }
.showmore { display:block; margin-top:6px; background:none; border:0;
  color:#46d7c3; font-size:13px; padding:2px 0; letter-spacing:.03em; }
/* code: monospace cards with their own x-scroll, never widening the page */
.cblk { background:#0a0e18; border:1px solid var(--line); border-radius:10px;
  padding:10px 12px; margin:8px 0;
  font:13px/1.45 ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Roboto Mono", "Liberation Mono", monospace;
  overflow-x:auto; white-space:pre; max-width:100%; }
.ichip { background:rgba(255,255,255,.09); border-radius:5px; padding:1px 5px;
  font:.92em ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Roboto Mono", "Liberation Mono", monospace; }
/* timestamps: grouped cluster dividers, never per-message */
.tsdiv {
  align-self:center; text-align:center; font-size:11.5px; color:#5b6479;
  padding:8px 0 0; letter-spacing:.04em;
  font-family:system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
/* delivery state under the last user bubble */
.dstate {
  align-self:flex-end; font-size:11.5px; color:var(--dim);
  padding:0 4px; margin-top:-6px; background:none; border:0;
  font-family:system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
.dstate.fail { color:#ff9a9e; }
#jumpBtn { position:absolute; left:50%; transform:translateX(-50%);
  bottom:calc(84px + env(safe-area-inset-bottom, 0px) + var(--kb, 0px));
  z-index:5; background:#182032; color:#46d7c3; border:1px solid rgba(70,215,195,.35);
  border-radius:999px; padding:7px 14px; font-size:13px; }
#homeFilter { width:100%; box-sizing:border-box; margin:0 0 10px;
  background:#131a29; border:1px solid var(--line); border-radius:12px;
  color:#e5ecf7; padding:10px 14px; font-size:15px; }
.hcard .r1 .mono { flex:none; width:26px; height:26px; border-radius:8px;
  display:inline-flex; align-items:center; justify-content:center;
  font-size:13px; font-weight:700; color:#e9eef8; align-self:center; }
#chatLines .empty { color:var(--dim); font-size:14px; align-self:center; padding:18px 8px; text-align:center; line-height:1.55; }

/* control room cards */
.card {
  display:flex; align-items:center; gap:6px; border-radius:16px;
  border:1px solid transparent; margin-bottom:2px; padding-right:6px;
}
.card.current { background:rgba(255,255,255,.05); border-color:var(--line); }
.card.closed { opacity:.55; }   /* v5: inactive rows are read-only */
.card .main {
  flex:1; min-width:0; display:flex; align-items:center; gap:12px;
  text-align:left; padding:13px 8px 13px 12px; min-height:60px; border-radius:14px;
}
.card .main:active { background:rgba(255,255,255,.05); }
.sdot { width:10px; height:10px; border-radius:50%; background:var(--mint); flex:none; }
.sdot.working { background:var(--amber); animation:softpulse 1.6s ease-in-out infinite; }
.sdot.needs { background:var(--danger); }
.sdot.closed { background:#39435a; }   /* v5 */
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
  .sheet, #chatSheet, #decide, #toast, #home, .hcard { transition:none; }
  #switchOverlay .spin { animation:none; border-top-color:var(--line); }
  .sdot.working, .badge.needs, #decide .eyebrow .ddot { animation:none; }
  /* typing dots hold steady; state stays legible via color and text */
  .typing .td { animation:none; }
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
  </button>
  <span id="chip" role="status">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M11 5 6.5 8.5H3v7h3.5L11 19z" fill="currentColor" stroke="none"/>
      <path d="M15 9a4.2 4.2 0 0 1 0 6"/><path d="M17.8 6.6a8 8 0 0 1 0 10.8"/>
    </svg>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="7" y="2.5" width="10" height="19" rx="2.5"/>
      <path d="M10.5 18.5h3"/>
    </svg>
    Sound on this phone
  </span>
</header>

<main>
  <div id="orbzone" aria-hidden="true">
    <div class="ripple"></div><div class="ripple"></div><div class="ripple"></div>
    <div id="orbscale"><div id="orb"></div></div>
  </div>
  <div id="stateWord" role="status" aria-live="polite"></div>
  <div id="status" role="status" aria-live="polite">call ended</div>
</main>

<footer>
  <div class="ctlwrap">
    <button class="ctl" id="muteBtn" aria-pressed="false" aria-label="Mute microphone">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="9" y="3" width="6" height="12" rx="3" fill="currentColor" stroke="none"/>
        <path d="M6 12a6 6 0 0 0 12 0"/><path d="M12 18v3"/>
        <g class="slash"><path class="slashcase" d="M5 4l14 14"/><path class="slashline" d="M5 4l14 14"/></g>
      </svg>
    </button>
    <span class="ctllbl" id="muteLbl">Mute</span>
  </div>
  <div class="ctlwrap">
    <button class="ctl" id="endBtn" aria-label="End call">
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M12 9.5c-3.3 0-6.4 1-8.9 2.9-.6.4-.8 1.2-.5 1.9l.9 1.9c.3.7 1.1 1 1.8.8l2.6-.9c.6-.2 1-.8 1-1.4v-1.3c2-.6 4.2-.6 6.2 0v1.3c0 .6.4 1.2 1 1.4l2.6.9c.7.2 1.5-.1 1.8-.8l.9-1.9c.3-.7.1-1.5-.5-1.9A14.6 14.6 0 0 0 12 9.5z"/>
      </svg>
    </button>
    <span class="ctllbl">End</span>
  </div>
  <div class="ctlwrap">
    <button class="ctl" id="chatBtn" aria-pressed="false" aria-label="Show chat">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9.2 9.2 0 0 1-3.9-.9L3 20l1-4.1a8.2 8.2 0 0 1-1-4.4 8.4 8.4 0 0 1 9-8.4 8.4 8.4 0 0 1 9 8.4z"/>
        <path d="M8.5 10.5h7"/><path d="M8.5 13.5h4.5"/>
      </svg>
    </button>
    <span class="ctllbl">Chat</span>
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

<section id="chatSheet" role="dialog" aria-label="Chat">
  <div id="chatHead">
    <button id="chatBackBtn" aria-label="Back to the call">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg>
    </button>
    <span id="chatDot" aria-hidden="true"></span>
    <h2 id="chatTitle">Chat</h2>
    <button id="hushBtn" aria-label="Stop the voice, keep reading">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M11 5 6.5 8.5H3v7h3.5L11 19z" fill="currentColor" stroke="none"/>
        <path d="M15 9.5l5 5"/><path d="M20 9.5l-5 5"/>
      </svg>
    </button>
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
  <p class="hint">Natural voice is made on your Mac and played here, and the phone voice
     takes over automatically if the Mac is unreachable.</p>
  <p class="hint">A new voice takes effect on the next reply.</p>
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
const statusEl=$('status'), stateWord=$('stateWord'), pillName=$('pillName'),
      muteBtn=$('muteBtn'), muteLbl=$('muteLbl'),
      chatBtn=$('chatBtn'), chatLines=$('chatLines'), chatScroll=$('chatScroll'), jumpBtn=$('jumpBtn'),
      chatHead=$('chatHead'), chatTitle=$('chatTitle'), chatBackBtn=$('chatBackBtn'),
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
/* v6 voice source: 'phone' = browser SpeechSynthesis (default), 'mac' = the
   Mac's Kokoro neural voice via POST /tts. Persisted in localStorage. */
let voicePref='mac';
try{
  var _vp = localStorage.getItem('vbvoice');
  /* Natural (Kokoro) is the DEFAULT on iOS and Android; 'phone' only when
     the user explicitly chose it. Unreachable Kokoro still auto-falls back
     to the phone voice mid-reply, so the default is safe everywhere. */
  voicePref = (_vp === 'phone') ? 'phone' : 'mac';
}catch(e){ voicePref = 'mac'; }
/* WHICH Kokoro voice speaks. Three curated ids, picked in settings,
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
/* chat is a full-screen MODE now (open / closed, no drag positions);
   typingMute keeps the mic off while the composer has focus. */
let chatOpenState=false, chatHist=false;
let typingMute=false;
let renderedTurns=0;      // turns currently in the DOM (pill append detection)

/* rows without "active" come from an older server: treat them as live */
function isActiveSess(s){ return !s || s.active !== false; }

/* the permanent one-word state label under the orb; the old status line is
   the small detail row (elapsed time, countdown dots, "typing") and never
   just repeats the word */
const STATE_WORDS = { listening:'Listening', thinking:'Thinking', speaking:'Speaking',
  muted:'Muted', needs:'Needs you', ended:'' };
function setState(s, label){
  state=s;
  document.body.dataset.state = s;
  const word = STATE_WORDS[s] !== undefined ? STATE_WORDS[s] : s;
  stateWord.textContent = word;
  let detail = label !== undefined ? label : '';
  if(detail && detail.toLowerCase() === word.toLowerCase()) detail = '';
  statusEl.textContent = detail;
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
/* v6 dispatcher: every caller keeps using say(); the voice setting decides
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

/* ---- v6: the Mac (Kokoro) voice pipeline ----
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
   replies set macDead and the session stays on the phone voice. */
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
   well, no fake keep-awake. */
document.addEventListener('visibilitychange', () => {
  if(document.visibilityState !== 'visible') return;
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
     - getUserMedia failure = no barge, exactly the v4 behavior */
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
  /* v7: typingMute keeps the whole mic off, the barge monitor included */
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
/* v7: copy any bubble's text ("Claude read out an error, I need it as
   text"). clipboard API needs a secure context (the tunnel is https);
   the hidden-textarea path covers plain-http and older browsers. */
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
  /* ChatGPT convention: USER messages get right-aligned mint bubbles;
     AGENT replies are open full-width text on the background */
  const d = document.createElement('div');
  d.className = (role === 'user' ? 'bub user' : 'msg-a') + (liveNow ? ' live' : '');
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
/* v7 pill rule, bulletproof: the pill may exist ONLY when (sheet open) AND
   (user scrolled up more than ~150px from the bottom) AND (content was
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
jumpBtn.addEventListener('click', chatScrollBottom);   // v6 bug: it had NO handler
/* "Claude is working" row pinned to the end of the transcript while a
   turn is in flight; the orb is hidden in chat mode, so the chat itself
   has to show progress. */
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
  syncDelivery();
}
/* delivery state under the LAST user bubble: "sending" until the ask is
   acked, "delivered" once askDelivered, "failed, tap to retry" when the
   retry queue (pendingSend) holds it; tapping resends the SAME idempotent
   key, so a retry can never double-type the prompt. */
function syncDelivery(){
  let row = document.getElementById('deliveryRow');
  const bubs = chatLines.querySelectorAll('.bub.user');
  const last = bubs.length ? bubs[bubs.length - 1] : null;
  if(!turnActive || !live || !last){ if(row) row.remove(); return; }
  if(!row){
    row = document.createElement('button');
    row.id = 'deliveryRow'; row.className = 'dstate';
    row.addEventListener('click', () => {
      if(pendingSend && live && !askDelivered){
        setState('thinking', 'retrying...');
        sendAsk(pendingSend.id, pendingSend.text, 3);
        syncDelivery();
      }
    });
  }
  if(pendingSend && !askDelivered){
    row.classList.add('fail'); row.textContent = 'failed, tap to retry';
  }else if(askDelivered){
    row.classList.remove('fail'); row.textContent = 'delivered';
  }else{
    row.classList.remove('fail'); row.textContent = 'sending';
  }
  last.insertAdjacentElement('afterend', row);
}
/* timestamps render as grouped CLUSTER dividers (>= 10 minutes of gap),
   never per-message; turns without a stamp simply never open a cluster */
const CLUSTER_MS = 600000;
function fmtStamp(ts){
  const d = new Date(ts), now = new Date();
  const hm = d.toLocaleTimeString([], { hour:'numeric', minute:'2-digit' });
  if(d.toDateString() === now.toDateString()) return hm;
  return d.toLocaleDateString([], { weekday:'short' }) + ' ' + hm;
}
function tsDivider(ts){
  const el = document.createElement('div');
  el.className = 'tsdiv';
  el.textContent = fmtStamp(ts);
  return el;
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
  let prevTs = 0;
  localTurns.forEach(t => {
    if(t.ts && (!prevTs || t.ts - prevTs >= CLUSTER_MS)) chatLines.appendChild(tsDivider(t.ts));
    if(t.ts) prevTs = t.ts;
    chatLines.appendChild(bubble(t.role, t.text));
  });
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
function lastStampTs(){
  for(let i = localTurns.length - 1; i >= 0; i--){
    if(localTurns[i].ts) return localTurns[i].ts;
  }
  return 0;
}
function chatAdd(role, text){
  if(!text) return;
  const ts = Date.now();
  const prevTs = lastStampTs();
  localTurns.push({ role: role, text: text, ts: ts });
  const empty = chatLines.querySelector('.empty');
  if(empty) empty.remove();
  const sheetOpen = chatOpenState;
  const stick = !sheetOpen || chatNearBottom();
  if(!prevTs || ts - prevTs >= CLUSTER_MS) chatLines.appendChild(tsDivider(ts));
  chatLines.appendChild(bubble(role, text, role !== 'user' && turnActive));
  syncTyping();
  renderedTurns = localTurns.length;
  /* a reply that lands while chat is CLOSED leaves a dot on the chat
     button instead of a pill nobody can see */
  if(role !== 'user' && !sheetOpen) chatBtn.classList.add('unread');
  if(stick) chatScrollBottom();
  else if(sheetOpen) jumpBtn.classList.remove('hidden');
}
function resetChat(){
  localTurns = []; chatHasServer = false;
  chatBtn.classList.remove('unread');   // the dot was about the old session
  renderChat();
}
async function refreshChat(){
  try{
    const j = await jget('/chat');
    if(Array.isArray(j.turns)){
      /* the server transcript has no timestamps: carry local stamps over
         by role+text so cluster dividers survive the re-render */
      const tsMap = {};
      localTurns.forEach(t => {
        if(!t.ts) return;
        const k = t.role + '\n' + t.text;
        (tsMap[k] = tsMap[k] || []).push(t.ts);
      });
      localTurns = j.turns
        .map(t => ({ role: t.role === 'user' ? 'user' : 'assistant',
                     text: String(t.text || '').trim() }))
        .filter(t => t.text);
      localTurns.forEach(t => {
        const k = t.role + '\n' + t.text;
        if(tsMap[k] && tsMap[k].length) t.ts = tsMap[k].shift();
      });
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
  /* reset the delivery flags BEFORE the bubble renders so the delivery
     row starts at "sending", never a stale "delivered" */
  askDelivered = false; pendingSend = null;
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
      syncDelivery();
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
      syncDelivery();
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
        syncDelivery();
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
  startBarge();                   // v5: talking over the reply interrupts it
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
  syncTyping();   // v7: drop the "Claude is working" row with the turn
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
      /* v5: do NOT stop and send; hold a follow-up window and keep the
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
    levelTarget = Math.max(levelTarget, Math.min(1, rms * 10));
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
function micDenied(){
  live = false; liveGen++;
  cancelTurn(); stopListening(); stopSpeaking(); stopHeartbeat(); releaseWakeLock();
  /* the full-screen chat would sit above the recovery overlay */
  closeChatSheet();
  setState('ended', 'microphone blocked');
  $('startTitle').textContent = 'Microphone is blocked';
  $('startBody').textContent = 'Allow microphone access for this site in your browser settings, then start the call again.';
  $('startFine').textContent = 'iOS: Settings, Safari, Microphone. Android: the lock icon in the address bar.';
  startBtn.textContent = 'Try again';
  startOverlay.classList.remove('hidden');
}
async function startCall(){
  /* iOS: everything audio must be unlocked INSIDE the tap, before any await:
     silent TTS warm-up + AudioContext resume (it starts suspended) */
  unlockAudio();
  startOverlay.classList.add('hidden');
  live = true; muted = false;
  liveGen++;
  srFails = 0;   // a fresh call gets a fresh chance (srDead stays for the session)
  muteBtn.classList.remove('muted');
  muteBtn.setAttribute('aria-pressed', 'false');
  muteLbl.textContent = 'Mute';
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
  /* the full-screen chat would sit above the call-ended overlay */
  closeChatSheet();
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
  muteLbl.textContent = muted ? 'Muted' : 'Mute';   // the label shows CURRENT state
  if(muted){
    stopListening();
    stopBarge();   // muted means muted: no barge monitor either
    if(state === 'listening') setState('muted');
  }else if(state === 'muted' || state === 'listening'){
    setState('listening'); listen();
  } /* muted flipped during working/speaking: the turn loop checks it after */
});

/* ============================================================ sheets */
const scrim=$('scrim'), chatSheet=$('chatSheet'), sessSheet=$('sessSheet'),
      closedSheet=$('closedSheet'), setSheet=$('setSheet');
let sessOpen = false, closedOpen = false, setOpen = false;
/* control room, the closed-session sheet, and settings are modal (scrim);
   chat is an independent toggle */
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
/* settings sheet (the gear): the voice source + the curated voice cards */
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
/* v5: read-only sheet for a closed (inactive) session; never starts a call */
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
/* ---- chat as a full-screen MODE (the drag sheet and half state are
   retired). In: the Chat control. Out: the chat header back chevron OR
   hardware/gesture back (pushState on open, popstate closes). ---- */
function applyChatMode(){
  chatSheet.classList.toggle('open', chatOpenState);
  document.body.classList.toggle('chat-full', chatOpenState);
  /* the chat header names the SESSION; the dot beside it mirrors the orb */
  chatTitle.textContent = pillName.textContent || 'Chat';
  chatBtn.classList.toggle('active', chatOpenState);
  chatBtn.setAttribute('aria-pressed', String(chatOpenState));
  chatBtn.setAttribute('aria-label', chatOpenState ? 'Hide chat' : 'Show chat');
}
function openChatSheet(){
  chatOpenState = true;
  chatBtn.classList.remove('unread');
  applyChatMode();
  chatScrollBottom();          // on open: ALWAYS at the bottom, pill hidden
  refreshChat();
  /* hardware/gesture back closes the chat instead of leaving the page */
  if(!chatHist){
    try{ history.pushState({ vbchat: 1 }, ''); chatHist = true; }catch(e){}
  }
}
function closeChatSheet(fromPop){
  const was = chatOpenState;
  chatOpenState = false;
  applyChatMode();
  jumpBtn.classList.add('hidden');
  try{ composeIn.blur(); }catch(e){}
  if(chatHist){
    chatHist = false;
    if(!fromPop && was){ try{ history.back(); }catch(e){} }
  }
}
window.addEventListener('popstate', () => {
  if(chatOpenState) closeChatSheet(true);
  else chatHist = false;
});
chatBackBtn.addEventListener('click', () => closeChatSheet());
/* ---- v7: the typed composer (silent prompts from the phone) ---- */
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
/* lift the sheet over the on-screen keyboard (iOS lays the keyboard over
   fixed elements; visualViewport is the only honest signal) */
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
/* ---- v7: tap the orb to stop the voice (read-along users finish before
   the speech does; barge-in needs SPEAKING, which a meeting forbids) */
function hushVoice(){
  if(state !== 'speaking') return;
  stopSpeaking();
  resumeAfterSpeech();
}
$('orbzone').addEventListener('click', hushVoice);
$('hushBtn').addEventListener('click', hushVoice);
scrim.addEventListener('click', () => { closeSessSheet(); closeClosedSheet(); closeSetSheet(); });
$('pill').addEventListener('click', () => { sessOpen ? closeSessSheet() : openSessSheet(); });
chatBtn.addEventListener('click', () => {
  chatOpenState ? closeChatSheet() : openChatSheet();
});

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
  /* v7: priority sections, what needs you first, then working, then ready,
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
     Mac voice through WebAudio without ever passing the Start call tap */
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
      chatTitle.textContent = cur.name;   // the chat header names the session
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
    chatTitle.textContent = pillName.textContent;   // the chat header names the session
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
/* HOME is the resting screen; the call screen (orb and controls) sits under
   it. Tapping an ACTIVE home card repoints the relay and lands on the start
   overlay, where the Start call tap grants the mic; a needs-you card works
   the same and the permission panel surfaces right after connecting
   (immediate /status check). Closed cards open the read-only sheet and go
   nowhere near the call screen. The back chevron ends any live call and
   returns home. */
function goCall(name){
  onHome = false;
  document.body.classList.remove('home');
  if(name){
    pillName.textContent = name;
    $('pill').setAttribute('aria-label', 'Session: ' + name + '. Open control room.');
  }
  resetStartOverlay(name);
  setState('ended', '');
  startOverlay.classList.remove('hidden');
}
function goHome(){
  onHome = true;
  cancelTurn(); stopListening(); stopSpeaking(); stopHeartbeat(); releaseWakeLock();
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
   A live target skips home, repoints the relay, and rests on the start
   overlay; if the switch fails the page falls back to home. */
if(S){
  document.body.classList.remove('home');
  startOverlay.classList.remove('hidden');
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

            q = _queue.Queue(maxsize=64)   # bounded: dead clients can't grow it forever
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
    _asked_load()
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

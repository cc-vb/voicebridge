# voicebridge , end-to-end test plan

Run top to bottom. Each test has **Do**, **Expect (pass)**, and where useful
**Try to break it**. macOS. Wear headphones for the voice tests.

Quick log to watch in a spare terminal while testing:
```
tail -f ~/.voicebridge/log
```

---

## 0. Automated self-checks (no mic needed) , run these first

```
python3 - <<'PY'
import sys; sys.path.insert(0,"/Users/krishojha/voicebridge")
from vb import core, stt, talkd, sessions, adapters, inject, oslayer
print("imports OK")
print("wake  :", talkd.wake_match("hey cloud fix it")[0], talkd.wake_match("the weather is cloudy")[0])
print("noise :", talkd.is_noise("MBC 뉴스"), talkd.is_noise("run the tests"))
print("exit  :", talkd._is_exit("stop stop stop"), talkd._is_exit("stop the server crashing"))
print("engine:", core.get_engine(), "| stt:", stt.stt_lang_mode()[1])
PY
```
**Expect:** `imports OK`; wake `True False`; noise `True False`; exit `True False`;
engine `kokoro`, stt `en`.

---

## 1. Install / health
- **Do:** `vb doctor`
- **Expect:** all 9 lines PASS. Any FAIL prints the exact fix. Mic +
  Accessibility must be granted to your terminal app.

## 2. Voice OUT (it speaks)
- **Do:** `vb test`
- **Expect:** you hear a spoken line in the Kokoro voice (`af_heart`).
- **Try:** `vb engine say; vb test; vb engine kokoro; vb test` , both voices
  work, no crash, second is the neural one.

## 3. voice-on , the core loop
- **Do:** in a Claude Code session, `/voice-on`. Wait for the ding. Say
  *"what files are in this folder?"*
- **Expect:** your words appear as a message in THIS session; Claude replies;
  you hear the reply. It listens again on its own.
- **Try to break:** speak a mid-sentence pause ("add a test... for the login
  path") , it should arrive as ONE prompt, not two.

## 4. Interrupts (all four)
- **Typing:** while it's speaking, type anything , voice cuts off instantly.
- **Hush hotkey:** while speaking, press `Cmd+Alt+Ctrl+X` , audio stops,
  generation continues.
- **Stop hotkey:** during a long generation, press `Cmd+Alt+Ctrl+Z` (or run
  `vb stop`) , voice stops AND Claude stops generating (Esc sent).
- **Barge-in:** while it's speaking a long reply, say *"wait, stop"* out loud
  , it should cut off and take your words. (Needs headphones to be reliable.)

## 5. Wake mode
- **Do:** `/voice-wake`. Talk normally / to another person.
- **Expect:** silence, nothing injected. Then say *"hey Claude, what day is
  it?"* , only that reaches Claude.
- **Try to break:** say a sentence containing "cloudy" or "loud" mid-way ,
  must NOT trigger. Say "hey cloud" / "you cloud" / "glory" , SHOULD trigger.

## 6. Fleet control (the differentiator)
Open 2-3 Claude sessions in different project folders first.
- **`vb sessions`** , lists them with idle/working + last-active; `*` marks
  the voiced one.
- **Voice:** say *"which agents need me?"* , it speaks who's waiting vs
  working.
- **Voice:** say *"switch to <projectname>"* , "Voice moved to X"; confirm
  `vb sessions` now stars X.
- **Voice:** say *"read me <name>'s last reply"* , speaks that session's last
  reply without switching.
- **Auto-alert:** start a slow task in another session; when it finishes you
  should hear *"heads up, <name> is ready for you"* (within ~12s). Toggle
  with `vb alerts off/on`.

## 7. Universal / other agents
- **Do (proves the mechanism):**
  ```
  mkdir -p /tmp/fake && printf '%s\n%s\n' \
    '{"role":"user","content":"hi"}' \
    '{"role":"assistant","content":"All tests pass."}' > /tmp/fake/s.jsonl
  vb agent add demo '/tmp/fake/*.jsonl' jsonl_text
  vb agent test demo
  ```
  **Expect:** "extracted reply: All tests pass." Then `vb sessions` shows
  `demo`, and *"read me demo's last reply"* speaks it. Cleanup: `vb agent
  remove demo`.
- **Real:** register your actual Codex/Cursor log glob and `vb agent test` it.

## 8. Phone
- **Do:** `vb phone` (or `/phone`).
- **Expect:** prints a URL and a scannable QR. Scan on phone, tap Start,
  say something , the Mac session answers and the phone speaks the reply.
  Keep the Mac awake + session focused.

## 9. Language / voice knobs
- `vb voice` lists voices; `vb voice am_michael; vb test` , voice changes.
- `vb rate 200; vb test; vb rate 175` , pace changes.
- Note: English-only STT right now (Hindi/Hinglish parked).

## 10. Stop everything
- **Per session:** `/voice-off` , this session stops; "run /voice-on to start".
- **Everywhere:** `/voice-stop` (or `vb off`) , daemon stops, all voice off.
- **Expect:** after either, no more listening; `vb sessions` still lists
  sessions but none voiced.

## 11. Multi-session safety (the bug we fixed)
- **Do:** `/voice-on` in session A, then `/voice-on` in session B.
- **Expect:** only B is voiced (`vb sessions` stars only B). A's speech no
  longer gets injected anywhere. Sessions never hear each other.

## 12. Noise resilience (try to make it misfire)
- Play a foreign-language video near the mic , should be dropped (`vb log`
  shows "dropped"). English TV chatter at low volume , dropped (too-quiet).
  Whisper/mumble , dropped (low-confidence). Only clear, directed speech
  should get through. Tune with `vb sens relaxed|normal|strict`.

## 13. Windows (needs a Windows machine , mynk03)
- Not testable on macOS. See WINDOWS.md: run `install.ps1`, then the same
  tests 1-12. Report what breaks.

---

## Pass bar for a demo / launch
1, 3, 4, 5, 6 must all pass cleanly on your machine. 6 (fleet) is the one
that has to feel magical , that's the whole pitch.

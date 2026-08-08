# Issue: Claude's questions (AskUserQuestion) aren't answerable from the phone

## Symptom
When Claude asks a question via the AskUserQuestion picker, the phone shows a
"thinking / working" state that never resolves. The only way to move forward is
to walk to the Mac and select an option in the terminal. From the phone it looks
like the session just hung.

## What already exists (so this is a fix, not a from-scratch build)
- `core.pending_question(transcript)` detects an OPEN AskUserQuestion by finding
  the newest `tool_use` named `AskUserQuestion` with no matching `tool_result`
  yet. Returns `{id, questions:[...]}`. (vb/core.py)
- The relay emits a `question` SSE event while one is open, `question_clear`
  when it resolves. (vb/call.py, /events loop)
- The phone renders option cards: `showQuestion` / `buildQuestionCard` /
  `toggleOpt`, with single- and multi-select. (vb/call.py page JS)

## Root cause (the gap)
Answering is wired as a **plain text turn**: `submitQuestion()` builds a string
like `"Header: Chosen Label"` and calls `startTurn(ans)`, which pastes it into
the session as a NEW prompt. But AskUserQuestion in Claude Code is an
**interactive picker** (navigate with arrow keys, Enter to choose), not a text
prompt. Pasting text does not drive that widget, so:
- the picker stays open on the Mac (session reads as "working" -> phone shows
  "thinking"), and
- the pasted answer either lands in the picker's free-text/"Other" field or is
  dropped, neither of which cleanly selects the intended option.

Detection and rendering are probably fine; the **answer delivery** is the break.

## How Claude Code does it (what to learn from)
AskUserQuestion is a terminal UI. A choice is made by moving the highlight to the
Nth option and pressing Enter (or typing into the "Other" free-text field, then
Enter). So to answer it programmatically we must drive it the same way the
keyboard does, not paste a sentence.

## Proposed approach
1. **Repro + confirm** (fast): open an AskUserQuestion, watch the transcript and
   the phone. Confirm the card renders and that `startTurn` text does NOT resolve
   the picker.
2. **Answer by keystrokes, through the adapter seam** (the real fix): when the
   phone submits, send the picker the right keys instead of pasting text, e.g.
   Down x (index) then Enter, per selected option; for the free-text "Other"
   case, type the text then Enter. This belongs in the TargetAdapter
   (`vb/adapters.py`) as an `answer_question(sid, question, selection)` method, so
   other targets can implement it differently. Verify index-to-keystroke mapping
   against the real widget (does it wrap? is there a search filter? multi-select
   toggle key?).
3. **Fallback / minimum bar** (ship even if 2 is fragile): make the phone clearly
   show "Claude is asking you something" with the options AS the primary state
   (not "thinking"), so at worst the user knows to answer, and add an explicit
   "answer on the Mac" hint. Never leave it looking like a hang.

## Acceptance criteria
- When Claude opens an AskUserQuestion, the phone shows the question + options as
  the foreground state within ~1s, not a generic "thinking".
- Tapping an option (single or multi) actually resolves the picker on the Mac and
  the session continues, no manual Mac selection needed.
- Free-text "Other" answers work.
- If answering can't be delivered, the phone says so plainly instead of hanging.

## UPDATE (live-tested): the real root cause is DETECTION, not answering
A live probe polled `core.pending_question(transcript)` once a second for 35s
while an AskUserQuestion was open on the phone: **0 detections**. Claude Code
does NOT write the AskUserQuestion `tool_use` block to the session transcript
until AFTER it is answered (the block count rose 17 -> 20 only once the test
questions were answered). So the transcript-scrape approach can NEVER see an
open question, which is why the phone only ever showed the permission card
(from the Notification hook) and never the option cards.

Consequence: the SSE/`/status` "suppress the yes/no while a question is open"
change and the `/answer` keystroke path are correct groundwork but INERT, they
depend on detecting the open question, which currently never happens.

### The fix path: a PreToolUse hook
voicebridge hooks today are SessionStart, SessionEnd, UserPromptSubmit, Stop,
Notification, NOT PreToolUse. A **PreToolUse hook matching `AskUserQuestion`**
receives the tool INPUT (the questions + options) BEFORE the tool blocks. Plan:
1. Add a PreToolUse hook (matcher `AskUserQuestion`) that writes the questions
   to a state file, e.g. `~/.voicebridge/open_question.json` (+ the tool_use id).
2. The relay reads that file (not the transcript) to emit the `question` event
   with real options -> phone renders the cards.
3. Clear it via a PostToolUse hook on `AskUserQuestion` (and/or the existing
   post-answer transcript detection) so the card goes away once answered.
4. Then the `/answer` keystroke path (already built) actually gets exercised;
   the single-select keystroke mapping still needs live confirmation.
Requires registering the new hook (settings/plugin) + a Claude Code restart to
take effect, so this is a build-and-verify task, not a hot fix. Park like brief
mode until it genuinely works end to end.

## Notes
- This is the highest-value interaction gap for the phone experience: a blocked
  question currently strands a remote user completely.
- Related: permission prompts already have their own yes/no decision card path
  (`showDecision` + keystroke relay); AskUserQuestion should reuse that
  keystroke-delivery pattern rather than the text-turn path.

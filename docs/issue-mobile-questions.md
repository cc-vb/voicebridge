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

## Notes
- This is the highest-value interaction gap for the phone experience: a blocked
  question currently strands a remote user completely.
- Related: permission prompts already have their own yes/no decision card path
  (`showDecision` + keystroke relay); AskUserQuestion should reuse that
  keystroke-delivery pattern rather than the text-turn path.

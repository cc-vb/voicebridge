# Using voicebridge with other coding agents (universal layer)

voicebridge is agent-agnostic on the way IN: your speech is pasted into
whatever text input is focused, so talking to **Codex, Cursor, Cline,
Copilot, aider**, or any terminal/editor agent already works today.

The only agent-specific part is reading the reply back to speak it. That's
pluggable via `~/.voicebridge/agents.json`:

```json
{
  "codex":  { "glob": "~/.codex/sessions/*.jsonl", "kind": "jsonl_text" },
  "aider":  { "glob": "~/.aider.chat.history.md",  "kind": "lastpara"   }
}
```

- **glob**: where that agent writes its session log(s).
- **kind**:
  - `claude`     Claude Code JSONL (built in, no config needed)
  - `jsonl_text` any JSONL log; speaks the last assistant record's text
  - `lastpara`   plain-text log; speaks the last paragraph

Once registered, `vb sessions` lists them alongside Claude sessions and the
fleet voice commands ("which agents need me?", "switch to codex", "read me
aider's last reply") work across all of them.

## Status

Input (voice -> agent) is verified working for any focused input. Output
adapters: `claude` is tested; `jsonl_text` and `lastpara` are tested on
synthetic logs. The exact `glob`/`kind` for Codex/Cursor/Cline needs a
quick confirmation from someone running that agent, paths and formats
shift, so we ship the mechanism and let the community pin each one. PRs
welcome (the repo has open collaborators).

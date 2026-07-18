---
description: One-time voicebridge setup (deps, whisper model, vb command, Kokoro voice)
allowed-tools: Bash(*)
---

Run the voicebridge installer with the Bash tool now. It is idempotent and
can take 5-10 minutes on first run (brew packages + ~1.2GB of local
speech models):

```
VB_SKIP_HOOKS=1 VB_KOKORO=1 bash "${CLAUDE_PLUGIN_ROOT}/install.sh"
```

If `${CLAUDE_PLUGIN_ROOT}` is empty in your environment, locate the plugin
copy first with:
`find ~/.claude/plugins -maxdepth 5 -name install.sh -path "*voicebridge*" | head -1`
and run that path instead (still with `VB_SKIP_HOOKS=1 VB_KOKORO=1`).

VB_SKIP_HOOKS=1 is required: the plugin already provides the hooks, so the
installer must not also register them in settings.json.

Afterwards, summarize the doctor results in 2-3 short sentences. If
Microphone or Accessibility failed, tell the user: System Settings ->
Privacy & Security -> enable both for their terminal app, then run
`vb doctor` to re-check. Finish by telling them to run
/voicebridge:voice-on inside any session and just talk.

---
description: Delegate the rest of the prompt to Kimi CLI in the background (see ~/.claude/skills/kimi/SKILL.md for the full delegation pattern)
argument-hint: <task; include explicit file paths>
---

Apply the kimi delegation skill (`~/.claude/skills/kimi/SKILL.md`) to: $ARGUMENTS

Follow it strictly: assemble inputs, fire kimi in the background with `run_in_background: true`, wait for the `=== KIMI_DONE` marker, present output verbatim. Do not explore the codebase yourself — kimi's tools handle that.

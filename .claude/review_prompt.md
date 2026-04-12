## Project override: prompt/instruction repository

This project consists primarily of prompts, agent instructions, guides, and configuration — not production application code.

When reviewing `.md` files (prompts, guides, agent instructions):
- Use WARNING instead of CRITICAL for missing implementation details in orchestration instructions. The Lead (Claude Code) has full tool access and can resolve procedural gaps at runtime.
- Do not block for: missing timeout/retry specifics, shell variable lifecycle, subprocess orchestration details, or tool availability concerns — these are runtime decisions, not code bugs.
- CRITICAL is still appropriate for: logical contradictions, incorrect file paths, wrong model names, broken references to nonexistent files, and security issues.

When reviewing `.py` files: apply the full global review rules without modification.

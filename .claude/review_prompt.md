<critical>
CRITICAL severity is reserved exclusively for `.py` files.
All findings in `.md` files use WARNING severity — no exceptions.
A WARNING in a `.md` file never justifies a BLOCK verdict.
</critical>

## Project context

This repository contains prompts, agent instructions, guides, and configuration — not production application code. The Lead agent (Claude Code) has full tool access and resolves procedural gaps at runtime.

## Severity rules by file type

### `.py` files — full review rules
Apply the global review prompt without modification.

### `.md` files — WARNING only
These are orchestration instructions, not executable code. Use WARNING for all findings, including:
- Logical contradictions between sections
- Incorrect file paths or broken references
- Missing timeout/retry specifics, shell variable lifecycle, subprocess details
- Tool availability concerns

### Design decisions (treat as intentional, not bugs)
- `parse_verdict()` uses case-insensitive matching — LLMs may output `ok` or `Ok`
- Empty/whitespace-only reviewer output → fail-open (tool crash, not a review result)
- The fail-closed contract (BLOCK on missing verdict) applies only to non-empty responses

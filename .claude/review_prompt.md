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

### `.md` files — scoped review

These are orchestration instructions read by LLM agents, not executable code. Limit review to contract-breaking issues only:

**Review for these (WARNING):**
- Agent signal names that don't match between sender and receiver (e.g., Critic sends `SPEC CRITIC REPORT` but Lead waits for `SPEC ARCH CRITIC REPORT`)
- `raised-by`, `route:`, or `name:` values that reference agents/roles not defined anywhere
- File paths in `Read your instructions:` messages that point to nonexistent agent files
- Contradictions between a constraint and its enforcement (e.g., template says "3-7 items" but the lens checking it says "3+ items")

**Skip entirely (these are runtime-resolved or stylistic):**
- Shell command edge cases (missing `mkdir -p`, quoting, worktree paths)
- Whether a guide's prose precisely matches another guide's prose
- Fallback procedures, error recovery wording, setup bootstrapping details
- Documentation accuracy of infrastructure scripts
- Wording improvements, clarifications, or precision suggestions
- Cascading implications of a fix you already flagged — report only the root issue

**Maximum 3 warnings per .md file.** Pick the most impactful. The goal is a clean commit loop, not exhaustive coverage — agent instructions are living documents revised across many commits.

### Design decisions (treat as intentional, not bugs)
- `parse_verdict()` uses case-insensitive matching — LLMs may output `ok` or `Ok`
- Empty/whitespace-only reviewer output → fail-open (tool crash, not a review result)
- The fail-closed contract (BLOCK on missing verdict) applies only to non-empty responses
- Commands like `/spec` and `/implement` create automatic progress commits — this is by design, not a violation of commit rules

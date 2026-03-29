# Claude Code Global Configuration

Global configuration for Claude Code sessions across all projects.

## Directory Structure

- `CLAUDE.md` — Global behavioral instructions (loaded by every session)
- `settings.json` — Hook configuration, environment variables, plugins
- `agents/` — Agent definitions for SDD (Spec-Driven Development) teams
- `commands/` — Slash commands for SDD workflow
- `hooks/` — PostToolUse and pre-commit hook scripts
- `git-hooks/` — Git hooks (pre-commit chain)
- `review_prompt.md` — Pre-commit AI code review prompt
- `templates/` — SDD spec and draft templates
- `skills/` — On-demand instruction sets

## Code Standards

### Type Annotations (Mandatory)

All Python code must include complete type annotations:

- Every function/method parameter must be annotated
- Every function/method must have a return type annotation
- `*args` and `**kwargs` must be annotated
- Use `from __future__ import annotations` for modern syntax

Enforced at multiple levels:

1. **CLAUDE.md** — Instructs Claude to always write annotated code
2. **PostToolUse hook** (`hooks/lint.py`) — Runs `ruff --select ANN` on every file edit
3. **Pre-commit review** (`review_prompt.md`) — AI reviewer flags missing annotations as CRITICAL
4. **Agent definitions** — Coder, Tester, Code-Reviewer, and Test-Reviewer agents all have explicit annotation rules

### Linting

All Python files are checked with `ruff` on every edit (PostToolUse hook). Pre-commit hook chains:

1. Project-local linters (if configured)
2. AI-powered code review via Claude (`pre_commit_review.py`)

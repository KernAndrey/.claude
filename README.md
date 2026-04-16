# Claude Code Global Configuration

Global configuration for Claude Code sessions across all projects.

## Directory Structure

- `CLAUDE.md` — Global behavioral instructions (loaded by every session)
- `settings.json` — Hook configuration, environment variables, plugins
- `agents/` — Agent definitions for SDD (Spec-Driven Development) teams
- `commands/` — Slash commands for SDD workflow
- `hooks/` — PostToolUse hook scripts (lint, guard)
- `git-hooks/` — Git hooks (pre-commit chain wrapper)
- `review/` — Pre-commit AI code review (3 lenses: bugs, architecture, tests)
  - `prompts/` — lens prompts, arbiter, single-call `combined.md`
  - `hook.py` — review entry point
  - `guides/` — SDD review runner guides
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
2. **PostToolUse hook** (`hooks/lint.py`) — Runs `ruff --select ANN` on every file edit (catches missing annotations at write-time)
3. **Agent definitions** — Coder, Tester, Code-Reviewer, and Test-Reviewer agents all have explicit annotation rules

(The pre-commit AI review intentionally does not duplicate linter scope — ruff's ANN rules cover type annotations before review runs.)

### Linting

All Python files are checked with `ruff` on every edit (PostToolUse hook). Pre-commit hook chains:

1. Project-local linters (if configured)
2. AI-powered code review via `review/hook.py`

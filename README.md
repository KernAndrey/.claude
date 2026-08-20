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

## Account switching (`cc-switch`)

`cc_switch.py` (symlinked as `~/bin/cc-switch`) swaps Claude Code OAuth
accounts by rewriting `~/.claude/.credentials.json` and the `oauthAccount`
field in `~/.claude.json`. Running sessions pick the change up without a
restart. Profiles live in `~/.claude-profiles/<name>.json`.

Manual: `add` / `use` / `list` / `current` / `remove`, plus `usage` to refresh
and print every account's limits and `pick` to move to the account with the
lowest weekly usage.

### Automatic switching

`cc-switch auto on` enables it; the flag is permanent until `auto off`. The
statusline is the only sensor — it already receives live 5h/7d percentages,
so there is no daemon and no polling interval.

Switching happens for one of two reasons: the active account hit a limit
(5h ≥ 95% or 7d ≥ 99%), or weekly usage drifted apart (another account is
≥ 5pp lower). The rule is always "rank every usable profile and take the
lowest weekly one", never "leave the current one", which is why it cannot
loop. When nothing clears the entry bar the active account stays put and the
earliest server-provided reset is recorded in `.exhausted`.

A candidate is confirmed live before any swap, so a revoked or expired login
is never switched into. Only the winner is confirmed, because refreshing
rotates the refresh token — the one operation that can lose access to an
account. The active profile is never refreshed; Claude Code owns its tokens.

A candidate that could not be reached (network error, or a throttled refresh)
is not evidence of anything: those pause for `CC_SWITCH_RETRY_SECONDS` instead
of recording a reset deadline, so a blip costs minutes rather than silencing
switching until the window rolls over.

Every command that writes credentials or profiles — `use`, `add`, `remove`,
`usage`, `pick`, and the automatic tick — takes one `flock` around the whole
read-decide-write sequence. They run the same sequence, so without it a manual
switch racing a statusline tick could undo itself, and two refreshes of the
same expired token would retire a perfectly good account.

`pick` checks every candidate live before deciding, then takes the genuinely
lowest one — stored snapshots only order the checks.

The active account is resolved from the *live* credentials everywhere, never
from the `.active` marker, which goes stale as soon as Claude Code logs into a
different saved account on its own. That marker is a hint for display; acting
on it would refresh the running session's own token, and — worse — save one
account's credentials into another account's profile, destroying it.

Thresholds are overridable via `CC_SWITCH_EXIT_5H`, `CC_SWITCH_EXIT_7D`,
`CC_SWITCH_ENTER_5H`, `CC_SWITCH_ENTER_7D`, `CC_SWITCH_BALANCE_GAP_7D`,
`CC_SWITCH_SETTLE_SECONDS`, and `CC_SWITCH_RETRY_SECONDS`. Percentages must be
finite and within 0..100, and each entry bar must stay at or below its exit
threshold — a NaN would otherwise pass silently and crash every later tick.

State in `~/.claude-profiles/`: `.auto` (flag), `.gate` (the cheap trigger the
statusline reads on every render), `.exhausted` / `.settle` (deadlines),
`.auto.log` (decisions), `.lock` (flock around read-decide-write).

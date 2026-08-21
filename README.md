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

`current`, `list` and `usage` all report when each login expires — the date
plus how far off it is (`27 Aug 15:10 (in 6d)`). That is the refresh token's
expiry: past it no switching helps and the account needs `claude` to sign in
again, so it is worth seeing before it happens rather than after.
`usage --json` carries the raw epoch as `login_expires_at`.

The date and the verdict beside it always come from the same copy: the live
credentials for the account that is signed in, the saved profile for every
other. Mixing them printed a future date next to `! refresh token expired`
for anyone who had just signed in again — the profile still holds the token
they retired.

### Automatic switching

`cc-switch auto on` enables it; the flag is permanent until `auto off`. The
statusline is the only sensor — it already receives live 5h/7d percentages,
so there is no daemon and no polling interval.

Switching happens for one of three reasons. The first outranks the others: the
live login stopped working — its refresh token expired, or the credentials file
is missing or damaged. That case cannot wait for a threshold, because a
logged-out session reports no usage at all; the gate carries the login's expiry
so the statusline can wake a tick on time alone. That rescue considers every
saved profile, including the one `.active` names: with no live credentials
there is no email to identify the running account by, so the marker is a
guess, and the profile it points at may hold the only working login left. The other two are the ordinary
ones: the active account hit a limit (5h ≥ 95% or 7d ≥ 99%), or weekly usage
drifted apart (another account is ≥ 5pp lower). The rule is always "rank every usable profile and take the
lowest weekly one", never "leave the current one", which is why it cannot
loop. When nothing clears the entry bar the active account stays put and the
earliest server-provided reset is recorded in `.exhausted`.

A candidate is confirmed live before any swap, so a revoked or expired login
is never switched into. Only the winner is confirmed, because refreshing
rotates the refresh token — the one operation that can lose access to an
account. The account Claude Code is signed into is never refreshed; it owns
those tokens, and it is recognised by its email rather than by any profile
name. That protection lapses the moment the login stops working: with no
session left to protect, handing out the unusable live token in place of a
profile's own only made the API reject it and marked the profile holding
the recovery as unauthorized.

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
lowest one — stored snapshots only order the checks. It follows the same
dead-login rule as the automatic path: with no live credentials it considers
every profile, the one `.active` names included.

The statusline's own check is a trigger, not a parser. It reads the
credentials file as text with every space, tab and newline removed — JSON
allows any amount of either around a colon, and matching the spellings one
by one called a pretty-printed file dead on every render — and anything it
cannot vouch for wakes a tick, which parses properly: no refresh token, an
empty or non-string one, or a file that never closes its object. Erring the other way costs nothing
visible and loses everything: a logged-out session reports no usage, so no
other trigger would ever fire.

The active account is resolved from the *live* credentials everywhere, never
from the `.active` marker, which goes stale as soon as Claude Code logs into a
different saved account on its own. That marker is a hint for display; acting
on it would refresh the running session's own token, and — worse — save one
account's credentials into another account's profile, destroying it.

The statusline compares whole tenths of a percent, so every trigger written
to `.gate` is rounded *up* to one. The direction matters: a trigger below the
real threshold wakes a tick that then declines to switch and writes the same
trigger back, a process per render for as long as usage sits in the gap,
while a trigger above it simply waits one more tenth.

Thresholds are overridable via `CC_SWITCH_EXIT_5H`, `CC_SWITCH_EXIT_7D`,
`CC_SWITCH_ENTER_5H`, `CC_SWITCH_ENTER_7D`, `CC_SWITCH_BALANCE_GAP_7D`,
`CC_SWITCH_SETTLE_SECONDS`, and `CC_SWITCH_RETRY_SECONDS`. Percentages must be
finite and within 0..100, and each entry bar must stay at or below its exit
threshold — a NaN would otherwise pass silently and crash every later tick.

An exhaustion deadline never holds a dead login back. `.exhausted` records
that every account was at its limit and can be days out, while being logged
out stops the session entirely — so the dead-login check answers to the
settle window alone, in the tick and in the statusline gate alike. The settle
window still applies: that barrier exists to stop a switch undoing itself,
and without it a dead login with nowhere to go would wake a tick on every
render.

State in `~/.claude-profiles/`: `.auto` (flag), `.gate` (the cheap trigger the
statusline reads on every render: `not_before recheck_after trigger_5h
trigger_7d login_deadline settle_until`), `.live` (the account cc-switch last resolved
from the real credentials — what the statusline displays, refreshed by every
invocation that resolves the active account, so it stays honest even with
auto-switching off), `.exhausted` / `.settle` (deadlines), `.auto.log`
(decisions), `.lock` (flock around read-decide-write). `.active` remains as a
hint and a fallback, but nothing decides on it.

A malformed profile — a hand-edited `expiresAt`, a garbled stored snapshot — is
skipped with a reason rather than taking the whole command down, and a garbled
statusline payload (a non-finite percentage) is ignored instead of being
persisted.

The settle window is armed before anything is swapped, and a switch that cannot
arm it is refused outright rather than performed half-protected: the next render
still carries the outgoing account's percentages, and without the barrier a tick
reading those would switch straight back.

# Global Instructions

## Environment
- Ubuntu 24.04, user: kern

## Behavior

<critical>
When a test fails, fix the bug in production code, not the assertion. If unsure how to fix, report the bug to the user. Loosening assertions masks real bugs — this applies even under time pressure.
</critical>

- Before implementation, verify the task is fully clear. Ask about requirements, edge cases, and expected behavior that the codebase alone cannot answer.
- Critically evaluate proposed solutions and assumptions. If you see a better approach, suggest it.
- While debugging, fix the root cause. A cosmetic patch hiding the real problem is worse than no fix.
- When modifying config files (linters, formatters), ask whether scope should be global (~/.config/) or project-local. This user maintains multiple projects with different configurations.

## Git Safety
- Use standard push — force push destroys shared history for all collaborators.
- Preserve all branches — deleted branches are unrecoverable for collaborators.
- Rebase only personal feature branches — rebasing main/master/dev rewrites shared history.

## Code Style

- Keep methods short and focused — extract logic into helper methods.
- Extract a class when 2+ consumers exist. Single-use logic stays as a method.
- If a method exceeds ~30 lines, consider splitting it.
- Handle errors explicitly: no silent catches, use specific exception types, write actionable error messages.
- All Python code must have complete type annotations: every function parameter, return type, *args, and **kwargs. Use `from __future__ import annotations` for modern syntax.

## Linting

After modifying .py files, run `ruff check --fix <changed_files>`.
Then verify with `ruff check <changed_files>`.
Before every commit, run the project's test suite — full suite or the subset relevant to changed files — and commit only after tests pass. Skip only when the change touches nothing tests cover (pure docs, CLAUDE.md, unrelated config). Do not delegate regression detection to the review hook or CI — they are backstops, not primary checks.
Run all pre-commit hooks on every commit — hooks enforce lint and security checks.
Every commit is AI-reviewed by a pre-commit hook. The review can BLOCK the commit with specific issues. When blocked: fix the reported issues and commit again. Do not skip the hook (`--no-verify`), do not amend — create a new commit.
If the diff exceeds 2000 lines the hook rejects it outright — split into smaller commits.
The review hook can take up to 20 minutes — always run `git commit` with `run_in_background: true` to avoid timeout.
If ruff reports unfixable errors, fix them manually before proceeding.
Verify type annotations with `ruff check --select ANN <changed_files>`.
Missing annotations must be added before proceeding.

### After every commit — review WARNINGs

After each successful commit, list every `[WARNING]` from the review and assign one decision per item: **Fix now**, **Fix in follow-up `<task-id>`** (only when out of scope), or **Accept because `<concrete reason>`**. Default to fixing when no specific reason to accept exists.

### Test discipline

Applies to every commit that adds or modifies code paths — new function, new `if/elif/else` branch, new template conditional, new user-facing surface, bug fix.

Before `git add`:
1. Enumerate every added or modified code path in your reply, by name. This list is the contract you will fulfil with tests — skipping the enumeration is skipping the discipline.
2. For each item, decide: **test now** or **skip because `<concrete reason>`** (trivial fallthrough, pass-through wrapper, covered by existing test `<name>`). Default to testing. A bare «trivial» is not a reason.
3. Write tests for everything marked **test now**. For a bug fix, the test must fail before the fix and pass after.
4. Run tests locally. Commit only after they pass.

Do this in one pass, before the first `git commit`. Do not rely on the pre-commit review hook to surface missing coverage — by the time it blocks you, you have already burned ~10× the user's wall-time and API budget compared to front-loading the tests. Untested changes regress. Saving your own output tokens here is a false economy — the cost shifts onto the user's budget and waiting time.

When the review hook blocks for missing tests, the only acceptable response is to write real tests for the flagged code paths and address EVERY `[CRITICAL]` and EVERY `[WARNING]` in the next commit. The following are not fixes — they are cheating, and the next review will block them again or worse, ship broken code:
- deleting the production code so the branch disappears;
- writing stub tests (`assert True`, mocking the very thing under test, tests that pass without exercising the branch);
- splitting the commit so the untested code lands in a later one;
- escalating to the user to skip tests — tests are not negotiable with the user.

Partial fixes only re-trigger the same block. Write the tests.

## Agent Teams
Use agent teams when 3+ files span different domains (models, views, tests).
Single-file changes and quick fixes run faster without team coordination.

## MCP
When working with library/framework APIs, use the context7 MCP tool to fetch up-to-date documentation before writing code.

## Ports
Check port availability before starting a server (`ss -tlnp | grep :<port>`) — a blocked port causes silent startup failures.

## Terminal Notifications

When you finish working on a task, signal completion via terminal:

- If task completed successfully, no action required:
  `echo -e '\033]0;✅ DONE\007\a'`

- If human action is required (manual testing, decision needed, merge conflict, etc.):
  `echo -e '\033]0;⚠️ ACTION REQUIRED\007\a'`

- If task failed or blocked:
  `echo -e '\033]0;❌ BLOCKED\007\a'`

Always execute the appropriate echo as your very last command.

## Compact instructions
On compaction preserve:
- List of modified files with change descriptions
- Current status of each task
- Commands for running tests
- Key architectural decisions from this session
- Last test failures (if any)

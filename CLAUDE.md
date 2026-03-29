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
Run all pre-commit hooks on every commit — hooks enforce lint and security checks.
If ruff reports unfixable errors, fix them manually before proceeding.
Verify type annotations with `ruff check --select ANN <changed_files>`.
Missing annotations must be added before proceeding.

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

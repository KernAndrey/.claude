# Global Instructions

## Environment
- Ubuntu 24.04, user: kern

## Behavior

- While planning or before implementation, ALWAYS think if the task is totally clear. Ask clarifying questions about requirements, edge cases, and expected behavior that cannot be extracted by yourself from project.
- User can be wrong or offer bad ideas. Always try to find and offer better solution.
- While debugging always solve the root reason, not a cosmetic patch. Clean architecture on first place
- NEVER weaken or adjust tests to make them pass around a bug. If a test reveals a real inconsistency or bug — fix the bug, not the test. If unsure how to fix — report the bug to the user and ask what to do. Silently loosening assertions to get green tests is a serious mistake.
- User can be wrong. Critically evaluate proposed solutions and assumptions. If you see a better approach — suggest it, don't silently comply.
- When creating or modifying config files (linters, formatters, etc.), always ask whether the scope should be global (~/.config/) or project-local. User has multiple projects with different requirements.

## Code Style

- Keep methods short and focused — extract logic into helper methods.
- Follow SOLID principles pragmatically, not dogmatically.
- If a method exceeds ~30 lines, consider splitting it.
- Handle errors explicitly: no silent catches, use specific exception types, write actionable error messages.

## Git rules
- NEVER force push
- NEVER delete branches
- NEVER rebase shared branches (main, master, dev)

## Linting

After modifying .py files, run `ruff check --fix <changed_files>`.
Then verify with `ruff check <changed_files>`.
Do NOT use `git commit --no-verify`.
If ruff reports unfixable errors, fix them manually before proceeding.

## Agent Teams
When a task involves 3+ files across different domains (models, views, tests),
consider spawning teammates with clear file ownership.
Do NOT use teams for single-file changes or quick fixes.

## MCP
When working with library/framework APIs, use the context7 MCP tool to fetch up-to-date documentation before writing code.

## Ports availability
Always check which ports are free before starting the server
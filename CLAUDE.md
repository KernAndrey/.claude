# Global Instructions

## Environment
- Ubuntu 24.04, user: kern

## Behavior

- While planning or before implementation, ALWAYS think if the task is totally clear. Ask clarifying questions about requirements, edge cases, and expected behavior that cannot be extracted by yourself from project.
- While debugging always solve the root reason, not a cosmetic patch. Clean architecture on first place 
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
- Work in feature branches, never commit directly to main
- Use conventional commits
- Always get git diff before commit. Split to a few logical commits if needed
- Never add any Co-Authored-By Claude/AI attribution in commit messages. NEVER!
- Write commit messages in English

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
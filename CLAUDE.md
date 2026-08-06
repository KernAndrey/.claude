# Global Instructions

## Environment
Ubuntu 24.04, user: kern.

<critical>
When a test fails, fix the bug in production code, not the assertion.
Fix any test that fails — including one that was already failing before your
change. A test's origin is irrelevant: "it was already broken" / "pre-existing"
/ "not caused by my change" never justifies leaving a failure unfixed.
If unsure how to fix, report to the user. Loosening assertions masks real bugs.
</critical>

## Behavior
- Verify the task is fully clear before implementing — ask about requirements, edge cases, expected behavior the code can't answer.
- Critically evaluate proposed solutions; suggest a better approach when you see one.
- Fix root causes, not symptoms — a cosmetic patch hiding the real problem is worse than no fix.
- For config files (linters, formatters): ask whether scope is global (`~/.config/`) or project-local — this user maintains many projects with different configs.

## Git Safety
- Standard push only — force push destroys shared history.
- Preserve branches — deletion is unrecoverable for collaborators.
- Rebase only personal feature branches — never main/master/dev.

## Code Style
- Methods short and focused; split when >30 lines.
- Extract a class when 2+ consumers exist; single-use logic stays a method.
- Errors handled explicitly: specific exception types, actionable messages, no silent catches.
- Python: full type annotations on every parameter, return, `*args`, `**kwargs`. Use `from __future__ import annotations`.

## Linting (Python)
After editing `.py` files: `ruff check --fix <files>`, then `ruff check --select ANN <files>`. Fix unfixable errors and missing annotations manually before proceeding.

## Commits
Use the `commit` skill for all commits. It owns the full procedure: security scan, test-coverage preflight, pre-commit hooks (gitleaks/semgrep), AI-review handling, post-commit WARNINGs review, and the 3000-line diff cap.

## Agent Teams
3+ files spanning different domains (models, views, tests) → use a team. Single-file changes and quick fixes run faster solo.

## MCP / Docs
For library/framework APIs, fetch docs via the `context7` MCP tool before writing code.

## Ports
Verify port availability before starting a server: `ss -tlnp | grep :<port>`. A blocked port causes silent startup failures.

## Compact instructions
Preserve on compaction: modified files + change descriptions, current task statuses, test commands, key architectural decisions, last test failures.

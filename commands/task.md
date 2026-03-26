Create a task draft from the description below.

## Instructions

1. Read `.tasks.toml` in project root. Get `id_prefix`, `dir`, and `counter_file`. If `.tasks.toml` is missing, tell the user to run `/task-init` first and stop.
2. Read the counter from `counter_file` (default: `tasks/.counter`), increment by 1, save back.
3. Generate ID: `{id_prefix}-{counter:03d}` (e.g. `TMS-042`).
4. Generate a slug from the description (kebab-case, max 5 words, ASCII only).
5. Copy template from `~/.claude/templates/sdd/draft.md`. If `.claude/templates/draft.md` exists in the project, use that instead (project override).
6. Fill placeholders: `{{ID}}`, `{{TITLE}}`, `{{DATE}}`, `{{DESCRIPTION}}`.
7. Save to `{dir}/1-draft/{ID}-{slug}.md`.
8. Output: ID, file path, brief summary.

## Description

$ARGUMENTS

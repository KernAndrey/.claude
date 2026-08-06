Create a task draft from the description below.

## Instructions

1. Locate the SDD config and read `id_prefix`, `dir`, and `counter_file` from it:
   - Look for `.tasks.toml` at the project root and one or two levels deep (`*/.tasks.toml`, `*/*/.tasks.toml`), skipping `node_modules/`, `.git/`, `vendor/`, and plugin/cache directories — a repo may carry several SDD roots, e.g. one per client.
   - One config → use it. Several → a project rule naming the choice wins; otherwise match the description to a root by the directory it names (a client, an addon, a module). `/task` mints a new ID, so there is no `id_prefix` to match on — when two roots stay plausible after reading the description, ask the user which board before allocating an ID.
   - Paths inside a config resolve relative to that config's own directory: `dir = "tasks"` in `clients/acme/.tasks.toml` means the board lives at `clients/acme/tasks/`.
   - No `.tasks.toml` anywhere → tell the user to run `/task-init` first and stop.
2. Read the counter from `counter_file` (default: `{dir}/.counter`), increment by 1, save back.
3. Generate ID: `{id_prefix}-{counter:03d}` (e.g. `TMS-042`).
4. Generate a slug from the description (kebab-case, max 5 words, ASCII only).
5. Copy template from `~/.claude/templates/sdd/draft.md`. If `.claude/templates/draft.md` exists in the project, use that instead (project override).
6. Fill placeholders: `{{ID}}`, `{{TITLE}}`, `{{DATE}}`, `{{DESCRIPTION}}`.
7. Save to `{dir}/1-draft/{ID}-{slug}.md`.
8. Output: ID, file path, brief summary.

## Description

$ARGUMENTS

Create a task draft from the description below.

## Instructions

1. Read `~/.claude/templates/sdd/board-root.md` and follow it to resolve `{main_root}`,
   discover the SDD configs, and define `{board}` and `{counter_file}`. The board
   lives in the main worktree — this command allocates an ID from it and writes there even
   when you are standing in a linked worktree.
2. Select the root. One config → use it. Several → a project rule naming the choice wins;
   otherwise match the description to a root by the directory it names (a client, an
   addon, a module). `/task` mints a new ID, so there is no `id_prefix` to match on — when
   two roots stay plausible after reading the description, ask the user which board before
   allocating an ID.
3. Read the counter from `{counter_file}`, increment by 1, save back.
4. Generate ID: `{id_prefix}-{counter:03d}` (e.g. `TMS-042`), then run the free-ID check
   from `board-root.md` §5 before using it.
5. Generate a slug from the description (kebab-case, max 5 words, ASCII only).
6. Copy template from `~/.claude/templates/sdd/draft.md`. If `.claude/templates/draft.md`
   exists in the project, use that instead (project override).
7. Fill placeholders: `{{ID}}`, `{{TITLE}}`, `{{DATE}}`, `{{DESCRIPTION}}`.
8. Save to `{board}/1-draft/{ID}-{slug}.md`.
9. Commit the board change per `board-root.md` §6, message
   `chore(sdd): add {ID} draft`.
10. Output: ID, absolute file path, brief summary, plus the `board-root.md` §7 report when
    `{main_root}` differs from the current directory.

## Description

$ARGUMENTS

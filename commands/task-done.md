Mark a task as done after manual review.

## Instructions

Read `~/.claude/templates/sdd/board-root.md` and follow it to resolve `{main_root}`, discover the SDD configs, and define `{board}`. The board lives in the main worktree — this command reads and moves a file there even when you are standing in a linked worktree. Select the root: the config whose `id_prefix` matches the task ID, or — when `$ARGUMENTS` is a bare slug — the root whose board actually holds a matching file, asking the user when several do.

1. Find the file by `$ARGUMENTS` (ID or slug) in `{board}/5-review/`.
2. Verify `status: review`. If not — report error and stop.
3. Update frontmatter: `status: done`, `done_date: {TODAY}`, `updated: {TODAY}`.
4. Move file from `{board}/5-review/` to `{board}/6-done/`.
5. Commit the board change per `board-root.md` §6, message `chore(sdd): close {ID}`.
6. Output confirmation with task ID and title, plus the `board-root.md` §7 report when `{main_root}` differs from the current directory.

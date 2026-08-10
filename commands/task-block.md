Block a task with a reason.

## Instructions

Read `~/.claude/templates/sdd/board-root.md` and follow it to resolve `{main_root}`, discover the SDD configs, and define `{board}`. The board lives in the main worktree — this command reads and moves a file there even when you are standing in a linked worktree. Select the root: the config whose `id_prefix` matches the task ID, or — when `$ARGUMENTS` is a bare slug — the root whose board actually holds a matching file, asking the user when several do.

1. Parse `$ARGUMENTS`: first token is the task identifier (ID or slug), the rest is the reason.
2. Find the file in any active directory under `{board}` (1-draft, 2-spec, 3-ready, 4-in-progress, 5-review).
3. Save previous status: `previous_status: {current_status}`.
4. Update: `status: blocked`, `blocked_reason: {reason}`, `blocked_date: {TODAY}`, `updated: {TODAY}`.
5. Move to `{board}/7-blocked/`.
6. Commit the board change per `board-root.md` §6, message `chore(sdd): block {ID}`.
7. Output confirmation with task ID, previous status, and reason, plus the `board-root.md` §7 report when `{main_root}` differs from the current directory.

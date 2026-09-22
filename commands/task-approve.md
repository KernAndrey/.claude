Approve a specification and move it to ready status.

## Instructions

Read `~/.claude/templates/sdd/board-root.md` and follow it to resolve `{main_root}`, discover the SDD configs, and define `{board}`. The board lives in the main worktree — this command reads and moves a file there even when you are standing in a linked worktree. Select the root: the config whose `id_prefix` matches the task ID, or — when `$ARGUMENTS` is a bare slug — the root whose board actually holds a matching file, asking the user when several do.

1. Find the file by `$ARGUMENTS` (ID or slug) in `{board}/2-spec/`.
2. Read the spec file.
3. **Template version guard.** Check that the spec contains a `## Definition of Done` heading. This header is present in every spec authored against the current template and absent in legacy specs. If missing, refuse with:

   ```
   Spec {ID} was written against the legacy template.

   Add the following sections before approving (see ~/.claude/templates/sdd/spec.md
   for the canonical layout):
     - ## Glossary
     - ## Examples
     - ## Testing Strategy
     - ## Change Control
     - ## Definition of Done
     - ## Blockers
     - ## Architecture & Implementation Plan must split into
       ### Architecture Decisions (hard) and ### Implementation Guidance (soft)

   Then re-run /task-approve {ID}.
   ```

   Stop.

4. **Blockers check.** Parse the `## Blockers` section. Count level-3 headings matching `### b-N — …` whose body contains a line matching `status: open` (case-insensitive). Ignore entries with `status: resolved-by-user`. If the count is > 0, refuse with:

   ```
   Spec {ID} has {N} open blockers. Resolve them via /spec {ID} before approving.

   Open blockers:
     - b-1: {first line of question}
     - b-2: {first line of question}
   ```

   Stop.

5. Verify `status: awaiting-approval` in frontmatter. If not, report error and stop.
6. Update frontmatter: `status: ready`, `approved_date: {TODAY}`, `updated: {TODAY}`.
7. Move file from `{board}/2-spec/` to `{board}/3-ready/`.
8. Commit the board change per `board-root.md` §6, message `chore(sdd): approve {ID}`.
9. Do NOT create a git branch here — `/implement` handles branch and worktree creation from `dev`.
10. Output: `Spec {ID} approved. Run /implement {ID} to start implementation.` Add the `board-root.md` §7 report when `{main_root}` differs from the current directory.

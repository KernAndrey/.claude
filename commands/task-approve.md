Approve a specification and move it to ready status.

## Instructions

1. Find the file by `$ARGUMENTS` (ID or slug) in `tasks/spec/`.
2. Verify `status: awaiting-approval`. If not — report error and stop.
3. Update frontmatter: `status: ready`, `approved_date: {TODAY}`, `updated: {TODAY}`.
4. Move file from `tasks/spec/` to `tasks/ready/`.
5. If `.tasks.toml` has `auto_branch = true`:
   - Create git branch `task/{ID}-{slug}` from the current main branch (main or master).
   - Report branch name.
6. Output: "Spec {ID} approved. Run `/implement {ID}` to start implementation."

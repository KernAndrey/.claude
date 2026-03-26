Mark a task as done after manual review.

## Instructions

1. Find the file by `$ARGUMENTS` (ID or slug) in `tasks/5-review/`.
2. Verify `status: review`. If not — report error and stop.
3. Update frontmatter: `status: done`, `done_date: {TODAY}`, `updated: {TODAY}`.
4. Move file from `tasks/5-review/` to `tasks/6-done/`.
5. Output confirmation with task ID and title.

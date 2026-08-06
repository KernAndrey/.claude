Mark a task as done after manual review.

## Instructions

`{dir}` = the `dir` of the applicable `.tasks.toml`. A repo may carry several SDD roots (root plus `*/.tasks.toml`, `*/*/.tasks.toml`, skipping `node_modules/`, `.git/`, `vendor/` and plugin/cache directories); pick the config whose `id_prefix` matches the task ID, or — when `$ARGUMENTS` is a bare slug — the root whose board actually holds a matching file, asking the user when several do. Resolve `dir` relative to that config's own directory.

1. Find the file by `$ARGUMENTS` (ID or slug) in `{dir}/5-review/`.
2. Verify `status: review`. If not — report error and stop.
3. Update frontmatter: `status: done`, `done_date: {TODAY}`, `updated: {TODAY}`.
4. Move file from `{dir}/5-review/` to `{dir}/6-done/`.
5. Output confirmation with task ID and title.

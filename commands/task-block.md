Block a task with a reason.

## Instructions

`{dir}` = the `dir` of the applicable `.tasks.toml`. A repo may carry several SDD roots (root plus `*/.tasks.toml`, `*/*/.tasks.toml`); pick the config whose `id_prefix` matches the task ID, and resolve its `dir` relative to that config's own directory.

1. Parse `$ARGUMENTS`: first token is the task identifier (ID or slug), the rest is the reason.
2. Find the file in any active directory under `{dir}` (1-draft, 2-spec, 3-ready, 4-in-progress, 5-review).
3. Save previous status: `previous_status: {current_status}`.
4. Update: `status: blocked`, `blocked_reason: {reason}`, `blocked_date: {TODAY}`, `updated: {TODAY}`.
5. Move to `{dir}/7-blocked/`.
6. Output confirmation with task ID, previous status, and reason.

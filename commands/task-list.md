Show the current task board.

## Instructions

1. Find every `.tasks.toml` — project root plus one or two levels deep (`*/.tasks.toml`, `*/*/.tasks.toml`), skipping `node_modules/`, `.git/`, `vendor/` and plugin/cache directories — and read `id_prefix` and `dir` from each. `dir` resolves relative to its own config's directory, so `dir = "tasks"` in `clients/acme/.tasks.toml` means the board lives at `clients/acme/tasks/`. None found → tell user to run `/task-init` and stop.
2. Scan all subdirectories of every `{dir}` (except `archive`). Directories are numbered for display order: `1-draft`, `2-spec`, etc. With several configs, print one board per config and title each with its `id_prefix`.
3. For each `.md` file, read frontmatter: id, title, status, priority, updated, group, depends_on.
4. Display a grouped table. If any tasks have a `group` field, cluster them first, then show ungrouped tasks.

```
Task Board

📦 {group-slug} ({N} tasks: {X} draft, {Y} spec, ...)
| ID      | Title                    | Status   | Priority | Depends On      |
|---------|--------------------------|----------|----------|-----------------|

📦 {another-group} (...)
| ...     | ...                      | ...      | ...      | ...             |

Ungrouped
Blocked (N)
| ID      | Title                    | Priority | Since      | Reason          |
|---------|--------------------------|----------|------------|-----------------|

In Progress (N)
| ID      | Title                    | Priority | Started    |
|---------|--------------------------|----------|------------|

Review (N)
| ID      | Title                    | Priority | Completed  |
|---------|--------------------------|----------|------------|

Draft: N | Spec: N | Ready: N | Done: N
```

If no tasks have `group` — skip the grouped section entirely and display as before.

5. If `$ARGUMENTS` contains a filter (status name, priority, ID pattern, or group name) — apply it.

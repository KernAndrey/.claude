Show the current task board.

## Instructions

1. Read `.tasks.toml` for the `dir` setting. If missing → tell user to run `/task-init` and stop.
2. Scan all subdirectories of `tasks/` (except `archive`).
3. For each `.md` file, read frontmatter: id, title, status, priority, updated.
4. Display a grouped table:

```
Task Board

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

5. If `$ARGUMENTS` contains a filter (status name, priority, or ID pattern) — apply it.

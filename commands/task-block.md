Block a task with a reason.

## Instructions

1. Parse `$ARGUMENTS`: first token is the task identifier (ID or slug), the rest is the reason.
2. Find the file in any active directory (1-draft, 2-spec, 3-ready, 4-in-progress, 5-review).
3. Save previous status: `previous_status: {current_status}`.
4. Update: `status: blocked`, `blocked_reason: {reason}`, `blocked_date: {TODAY}`, `updated: {TODAY}`.
5. Move to `tasks/7-blocked/`.
6. Output confirmation with task ID, previous status, and reason.

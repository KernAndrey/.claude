Block a task with a reason.

## Instructions

1. Parse `$ARGUMENTS`: first token is the task identifier (ID or slug), the rest is the reason.
2. Find the file in any active directory (draft, spec, ready, in-progress, review).
3. Save previous status: `previous_status: {current_status}`.
4. Update: `status: blocked`, `blocked_reason: {reason}`, `blocked_date: {TODAY}`, `updated: {TODAY}`.
5. Move to `tasks/blocked/`.
6. Output confirmation with task ID, previous status, and reason.

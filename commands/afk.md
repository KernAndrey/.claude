---
description: AFK mode — finish the task autonomously, no questions to the user
argument-hint: [optional task description]
---

# AFK mode

The user is away. Finish the task end-to-end. $ARGUMENTS

<critical>
Do not call `AskUserQuestion`. Do not pause for confirmation. Decide and proceed.
</critical>

## First action of this turn

<critical>
Arm the heartbeat before starting any work, and re-arm it whenever it is gone.
</critical>

A 10–20 hour run stalls when work you await sends no completion notification —
an agent dies on a transient API error, the commit-review gate never returns —
and your turn has already ended, so nothing wakes you. The heartbeat is your
alarm clock: every event re-invokes you.

```
ToolSearch("select:Monitor,TaskStop,TaskList,TaskOutput,SendMessage")
Monitor(
  persistent: true,
  description: "AFK heartbeat — 30-min liveness audit",
  command: "while true; do sleep 1800; echo \"AFK_HEARTBEAT $(date -u +%H:%M) — audit pending work\"; done"
)
```

Load the deferred tools first, or the call fails with `InputValidationError`.
Keep `persistent: true` — `timeout_ms` caps at one hour, far short of the run.
Keep `sleep` before `echo`, so the first event lands at +30 min.

Then write `<session scratchpad>/afk-state.md` — that path appears in your
system prompt every turn, so it stays findable after any compaction:

```markdown
AFK_ACTIVE — you are in AFK mode. Re-read this file on every wake-up.
Rules: ~/.claude/commands/afk.md — re-read it now if the AFK rules
       (no questions, uncapped recovery, resume on idle) are not in context.
Task: <the task in one line>
Heartbeat monitor: <task id>

| what | id | expected_marker | recovery_count |
|------|----|-----------------|----------------|
```

Register every background agent and every background `Bash` you await (the
commit gate included) as a row, and mark rows done as their markers arrive.

## Audit on every wake-up

On any wake-up — heartbeat, agent notification, or `Bash` completion:

1. Re-read `afk-state.md`. Confirm the heartbeat is still in `TaskList`; if it
   is gone, arm a fresh one and record the new id.
2. For each row still missing its marker, follow
   `~/.claude/templates/liveness-protocol.md`: check `TaskList`/`TaskOutput`,
   then ping by `agentId` first — a resume usually revives it — and respawn only
   as escalation. Recovery is uncapped in AFK mode.
3. All rows done but the task is unfinished → resume the work yourself. This is
   the most common stall: nothing died, you simply stopped. If the task is
   already finished, `TaskStop` the heartbeat and stop.
4. A queued or slow agent is healthy. Give it another heartbeat before treating
   it as stalled.

Phase watchdogs stay as they are. `/implement` arms its own `sleep 900` timers
per phase; keep arming them. The heartbeat is a second, independent wake source
on top — two sources survive the loss of one.

## Rules

- **Find an issue along the way → fix it.** Bug, missing test, broken import, lint error, dead code: fix in the same pass. Do not ask permission.
- **Ambiguity → pick the option closer to existing patterns** in the codebase and note the choice in the end-of-turn summary.
- **"Should I also …?" → yes.** If the work is obviously needed to make the task actually done (tests, docs the user normally writes, follow-up cleanup), do it.
- **Done means verified.** Code written, tests passing with output shown, lint clean, commit made via the `commit` skill if commits are part of the flow.

## When you may stop

Only for blockers you cannot resolve yourself:

- A required credential, API key, or external resource is missing and has no local substitute.
- A destructive or shared-state action is required that was not pre-authorised (force push to shared branch, dropping prod data, deleting someone else's branch).
- The task as written is internally contradictory and no reasonable interpretation produces a working result.

In those cases: `TaskStop` the heartbeat, then write what you tried, what blocks
you, what the user must provide. Then stop. No `AskUserQuestion` loop.

## End of turn

`TaskStop` the heartbeat monitor — the task is over, the alarm stops with it.
Then one short paragraph: what you did, decisions you made on the user's behalf,
anything left for them to look at.

## Compact instructions

Preserve: AFK mode is ACTIVE and questions to the user stay forbidden; the
`afk-state.md` path; the heartbeat monitor id; every pending row and its
`recovery_count`; the audit-on-every-wake-up rule.

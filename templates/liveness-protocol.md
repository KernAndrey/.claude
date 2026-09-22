# Liveness protocol — surviving dead background agents

You are a lead awaiting messages from agents spawned with `Agent(run_in_background: true)`. Agents sometimes die mid-run (typically a transient API error); a dead agent sends no completion notification, and nothing else wakes you — without this protocol the task hangs forever.

## Arm the watchdog

Whenever you are awaiting at least one agent message — after a spawn wave, during fix rounds, during re-review — a dead-man timer must be armed:

```
Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_{PHASE}")
```

One timer per phase, not per agent (e.g. `WATCHDOG_CODE`, `WATCHDOG_REVIEW`). Its completion re-invokes you even if no agent notification ever arrives — it is your guaranteed wake-up. Note the returned task id. When the phase completes, cancel the timer with `TaskStop`.

## Audit on every wake-up

On ANY wake-up — agent notification or WATCHDOG alike — compare the expected completion markers in your registry against what you have received:

- **All markers received** → phase complete. `TaskStop` the timer, proceed.
- **Markers missing, wake-up was an agent notification** → process it and keep waiting; the timer stays armed.
- **Markers missing, wake-up was the WATCHDOG** → run the liveness check below for each missing agent, then re-arm the timer (`sleep 900` again).

Because the audit runs on *every* wake-up, a dead timer is not fatal: the next agent notification triggers the same audit — notice the missing timer and re-arm.

## Liveness check (per missing agent)

Look up the agent's status via `TaskList` / `TaskOutput`:

1. **running** → alive, just slow. Keep waiting.
2. **completed, but the final message lacks its expected marker** → not a death. Read its `TaskOutput`: if the needed content is there (changed files, results, report), accept the work; otherwise resume it by `agentId` and ask for the final report.
3. **failed / not found** → recovery ladder.

## Recovery ladder

1. **Ping** — `SendMessage(to: "{agentId}", "Your instance was interrupted. Continue your work; when done, resend your final report with its completion marker.")`. Resume restores the agent's full context — this usually suffices. Re-arm the timer.
2. **Respawn** — if the pinged agent is still silent at the next WATCHDOG: spawn a fresh `Agent()` with the original prompt. For write-agents (Coders, Tester) prepend: *"A previous instance died mid-work. Inspect the current state first (`git status` / `git diff` in the worktree) and continue from it — do not start over."* Read-only agents (reviewers, critics) respawn with the original prompt as-is. Record the new `agentId`; increment the agent's recovery count.
3. **Cap** — 3 recovery cycles per agent (one cycle = ping, then respawn if the ping went unanswered). Cap exhausted → stop the phase and report to the user: which agent died, what you tried, last known output. Exception: in AFK mode (`/afk` was invoked in this session) there is no cap — keep cycling ping → respawn.

## Double-spawn race

If a pinged instance answers after its replacement was already spawned: the first arriving completion wins. Send the loser "Stop — your work was accepted from another instance." and `TaskStop` it. Mark the agent completed exactly once in the registry.

## Registry additions

Extend your agent registry with two columns: `expected_marker` (e.g. `CODER DONE.`) and `recovery_count`. Track the current phase's watchdog task id alongside.

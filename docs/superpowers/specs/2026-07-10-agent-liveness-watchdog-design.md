# Agent Liveness Watchdog — Design

**Date:** 2026-07-10
**Status:** approved design, pending implementation plan
**Scope:** SDD lead flows — `commands/implement.md`, `commands/spec.md`, `commands/implement-kimi.md`

## Problem

SDD lead flows spawn subagents with `run_in_background: true` and complete each
phase only upon receiving a completion marker (`CODER DONE.`, `TESTER DONE.`,
etc.). When a subagent dies mid-run (typically a transient API error), that
marker never arrives. The lead's turn has already ended and it has no
independent wake-up source, so the whole task hangs indefinitely until a human
nudges the session.

`workflows/implement.js` already solves this class of failure at the engine
level (`spawnWithBudget` — bounded null-retry). The gap is only in the prose
flows. `commands/fast-implement.md` spawns no background agents and is not
affected.

## Decisions (from brainstorm)

- **Scope:** SDD command flows only; no global CLAUDE.md rule for now.
- **Recovery policy:** fully automatic. Ping first — deaths are usually
  transient, the agent's context survives, and a `SendMessage` resume revives
  it. Fresh respawn is a rare escalation.
- **Watchdog period:** 15 minutes (`sleep 900`).
- **Recovery cap:** 3 cycles per agent; unlimited in AFK mode.
- **Placement:** one shared protocol file (3 consumers), referenced from each
  command. Inline duplication rejected — the protocol will be tuned (period,
  caps, respawn preamble) and three copies would drift.

## Design

### New file: `templates/liveness-protocol.md`

The protocol the lead follows. Contents:

1. **Arm.** Whenever the lead is awaiting at least one agent message — after a
   spawn wave, during bug-fix rounds, during review — keep a dead-man timer
   armed:
   `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_<phase>")`.
   One timer per phase, not per agent. The timer's completion re-invokes the
   lead even if no agent notification ever arrives.
2. **Audit on every wake-up** (agent notification or WATCHDOG — either):
   compare the registry's expected markers against received ones. All present →
   `TaskStop` the timer, proceed to the next phase.
3. **WATCHDOG fired with agents missing** — for each missing agent, check
   `TaskList` / `TaskOutput`:
   - *running* → agent is alive, just slow → re-arm the timer, keep waiting;
   - *completed without its marker* → read `TaskOutput`; if the needed content
     is there (changed files, results), accept the work; otherwise treat as
     silent and enter the recovery ladder;
   - *failed / gone* → recovery ladder.
4. **Recovery ladder** (per agent):
   1. **Ping** — `SendMessage(agentId)`: "your instance was interrupted —
      continue / resend your final report". Resume restores full context; this
      usually suffices. Re-arm the timer.
   2. **Respawn** — if still silent by the next WATCHDOG: fresh `Agent()` with
      the same prompt plus, for write-agents (Coders, Tester), the preamble:
      "the previous instance died mid-work; inspect the current state first
      (`git status` / `git diff` in the worktree) and continue from it — do not
      start over." Read-only agents (reviewers, critics) respawn plain.
   3. **Cap** — 3 recovery cycles per agent, counted in the lead's registry.
      Exhausted → stop the phase, report to the user (the Stop hook's
      `notify.sh` fires the push). In AFK mode — i.e. `/afk` was invoked in
      this session, so its instructions are in the lead's context — no cap,
      keep cycling ping → respawn.
5. **Double-spawn race.** If a pinged instance answers after a replacement was
   already spawned: the first arriving DONE wins; the other instance gets a
   "stop — work accepted from another instance" message and `TaskStop`. The
   registry marks the agent completed exactly once.
6. **Registry extensions.** Per agent the lead tracks: `agentId`, expected
   marker, ping/respawn counts, current watchdog task id.
7. **Self-healing.** Because the audit runs on *every* wake-up, a dead timer is
   not fatal — the next agent notification triggers the same audit and re-arms.

### Command file changes

Each of the three commands gets a short instruction at its agent-coordination
section: *"Whenever you are awaiting agent messages, follow the liveness
protocol: read and apply `~/.claude/templates/liveness-protocol.md`."* — plus
an explicit "arm the watchdog" line at each spawn wave / wait point:

- `implement.md`: Phase 1a (coders wave), Phase 1b (tester + fix rounds),
  Phase 2 (reviewers wave), Phase 3 fix rounds.
- `spec.md`: analyst, architect, and critics spawn points.
- `implement-kimi.md`: its background spawn points.

### Out of scope

- Death of the lead itself (not a subagent problem; cannot be fixed from
  inside the session).
- `/implement-wf` (already handles agent death via bounded null-retry).
- A global rule for ad-hoc subagent use outside SDD flows (possible later
  generalization once the protocol proves itself).

## Validation plan (manual, one-off)

Prompt behavior has no automated tests; validate on a sandbox spec:

1. Temporarily set the protocol period to 60 s.
2. Run `/implement` on a tiny two-coder spec.
3. Simulate death: have the lead `TaskStop` one coder right after spawning
   (or kill its tmux pane — `teammateMode: tmux`).
4. Expect: within ≤60 s the lead wakes on WATCHDOG, audits the registry,
   pings the dead coder, work completes; registry shows 1 recovery cycle.
5. Separately test the *completed-without-marker* branch: instruct one coder
   to finish without `CODER DONE.` — the lead must accept the work from
   `TaskOutput`, not restart the agent.
6. Restore the 900 s period.

## Alternatives considered

- **External Monitor watching agent liveness** (transcript mtimes, tmux
  panes): rejected — subagent transcripts are keyed by session UUID with no
  stable agent→file mapping, the monitor is itself another failure point, and
  long-thinking agents produce false positives.
- **Full migration to the Workflow engine** (`/implement-wf`): a separate,
  already-planned track; replaces the interactive lead model rather than
  fixing the prose flows.

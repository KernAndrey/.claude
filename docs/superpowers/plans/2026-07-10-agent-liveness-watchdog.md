# Agent Liveness Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SDD leads never hang waiting for a dead background agent — a dead-man timer wakes them, and a ping-first recovery ladder revives the agent.

**Architecture:** One shared protocol file (`templates/liveness-protocol.md`) holds the full procedure: arm a background `sleep 900` timer per phase, audit the registry on every wake-up, recover missing agents (ping by `agentId` first, fresh respawn as escalation). The three prose SDD commands reference it and add explicit "arm" lines at each spawn wave / wait point. `implement.md`'s existing broken "Liveness backstop" section (no wake-up mechanism, pings by `name` which cannot reach a dead agent) is replaced, and `spec.md`'s broken "~15 min" bullet is rewritten.

**Tech Stack:** Markdown prompt files only. No production code, no automated tests — verification is grep/read-back per task plus a manual sandbox scenario at the end.

**Spec:** `docs/superpowers/specs/2026-07-10-agent-liveness-watchdog-design.md`

## Global Constraints

- Watchdog period: **900 s** (`sleep 900`). 60 s is used *only* inside the manual validation scenario and must be restored.
- Recovery cap: **3 cycles per agent**; **no cap** when `/afk` was invoked in the session.
- All commits go through the `commit` skill (repo rule), directly on `main` (repo convention, user-approved). Stage only the files named in the task — `settings.json` and `skills/kimi/SKILL.md` carry unrelated WIP.
- Write in English, match the imperative style of the existing command files.
- Do not touch `commands/fast-implement.md` (no background agents) or `workflows/implement.js` (already handles agent death via `spawnWithBudget`).

---

### Task 1: Create the shared protocol file

**Files:**
- Create: `templates/liveness-protocol.md`

**Interfaces:**
- Produces: the file path `~/.claude/templates/liveness-protocol.md` referenced verbatim by Tasks 2–4, and the watchdog naming convention `WATCHDOG_{PHASE}` used in their arm lines.

- [ ] **Step 1: Write the file**

Create `templates/liveness-protocol.md` with exactly this content:

````markdown
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
````

- [ ] **Step 2: Verify**

Run: `grep -c "WATCHDOG" ~/.claude/templates/liveness-protocol.md`
Expected: `5` (heading references and examples; any count ≥4 is fine — the point is the file exists and is non-empty)

Run: `grep -n "Recovery ladder" ~/.claude/templates/liveness-protocol.md`
Expected: one match.

- [ ] **Step 3: Commit**

Invoke the `commit` skill with args: "Commit only templates/liveness-protocol.md. Message: `feat: add liveness protocol for SDD leads (dead-agent watchdog)`. Direct to main per repo convention."

---

### Task 2: Wire the protocol into commands/implement.md

**Files:**
- Modify: `commands/implement.md:32-34` (replace broken "Liveness backstop"), plus one arm line per phase (1a, 1b, 2, 3).

**Interfaces:**
- Consumes: `~/.claude/templates/liveness-protocol.md` from Task 1; watchdog names `WATCHDOG_CODE`, `WATCHDOG_TEST`, `WATCHDOG_REVIEW`, `WATCHDOG_FIX`.

- [ ] **Step 1: Replace the broken backstop section**

In `commands/implement.md`, replace this text (currently lines 32–34):

```markdown
### Liveness backstop

Completion notifications cover the normal case. If an agent produces no completion notification after ~15 minutes, send `STATUS CHECK: progress? blockers?` by `name`. If still no response after one more interval, the lead investigates directly: read the agent's last output and the relevant files, then either resume it with a specific hint or take over the task. Max **3 restart attempts** per role; after that the lead does the work directly.
```

with:

```markdown
### Liveness protocol

A dead agent (transient API error) sends no completion notification, and nothing else wakes you — the old "check after ~15 minutes" backstop never fired because your turn had already ended. Read and follow `~/.claude/templates/liveness-protocol.md`: keep a dead-man timer armed whenever you await agent messages, audit the registry on every wake-up, and recover missing agents — ping by `agentId` first (usually sufficient), fresh respawn as escalation. 3 recovery cycles per agent; no cap in AFK mode. Each phase below names its watchdog.
```

- [ ] **Step 2: Add the Phase 1a arm line**

Immediately after the sentence `For single-coder tasks (one entry in Work breakdown), the spawn is the same — just one agent, scope and file list copied from the spec.` add:

```markdown
Right after the spawn wave, arm the phase watchdog: `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_CODE")` (liveness protocol). `TaskStop` it when the phase completes.
```

- [ ] **Step 3: Add the Phase 1b arm line**

Immediately after the Tester spawn code block (the ` ``` ` closing the `Agent(subagent_type: "Tester", ...)` snippet), add:

```markdown
Arm the phase watchdog: `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_TEST")`. Keep it armed through bug-fix rounds — you are awaiting agent messages the whole time.
```

- [ ] **Step 4: Add the Phase 2 arm line**

Immediately after the reviewers spawn code block (the ` ``` ` closing the `Agent(subagent_type: "{type}", ...)` snippet, before "UI-Reviewer gets two extra lines"), add:

```markdown
Arm the phase watchdog: `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_REVIEW")`.
```

- [ ] **Step 5: Add the Phase 3 arm line**

In `#### Step 2: Fix round`, immediately after the blockquote ending `> Re-run all tests after fixes. Then your message must be `TESTER FIX ROUND DONE.`` add:

```markdown
Fix rounds await agent messages like any phase — arm `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_FIX")` per round.
```

- [ ] **Step 6: Verify**

Run: `grep -n "WATCHDOG_" ~/.claude/commands/implement.md`
Expected: 4 matches (CODE, TEST, REVIEW, FIX).

Run: `grep -n "Liveness backstop" ~/.claude/commands/implement.md`
Expected: no matches.

Run: `grep -n "liveness-protocol.md" ~/.claude/commands/implement.md`
Expected: 1 match.

- [ ] **Step 7: Commit**

Invoke the `commit` skill with args: "Commit only commands/implement.md. Message: `feat(implement): replace dead liveness backstop with watchdog protocol`. Direct to main per repo convention."

---

### Task 3: Wire the protocol into commands/spec.md

**Files:**
- Modify: `commands/spec.md` — Phase 2 intro (after the Addressing paragraph, currently line 122), the broken `~15 min` bullet (currently line 155), the critics batch (currently line 176 / 219).

**Interfaces:**
- Consumes: `~/.claude/templates/liveness-protocol.md` from Task 1; watchdog names `WATCHDOG_ANALYST`, `WATCHDOG_ARCHITECT`, `WATCHDOG_CRITICS`.

- [ ] **Step 1: Add the protocol reference to the Phase 2 intro**

Immediately after the paragraph `**Addressing:** running agent → `SendMessage(to: "spec-analyst")`; completed agent → `SendMessage(to: "{agentId}")`. The completion notification is your done signal — do not poll for it.` add:

```markdown
**Liveness:** a dead agent sends no completion notification and nothing else wakes you. Follow `~/.claude/templates/liveness-protocol.md`: arm a dead-man timer per phase (`Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_ANALYST")` — likewise `WATCHDOG_ARCHITECT`, `WATCHDOG_CRITICS`), audit on every wake-up, recover via ping-by-`agentId` first, respawn as escalation.
```

- [ ] **Step 2: Fix the broken ~15 min bullet in the 2a message loop**

Replace this bullet (currently line 155):

```markdown
- If no completion notification after ~15 min: send `STATUS CHECK` by `name` (it is still running). On second silence, surface to the user.
```

with:

```markdown
- On `WATCHDOG_ANALYST` firing with no completion: run the liveness check from the protocol — `TaskList` status, then ping by `agentId` (a dead agent is not reachable by `name`), respawn as escalation.
```

- [ ] **Step 3: Add the critics arm line**

Immediately after the paragraph `Spawn all three critics as background agents in one batch (three `Agent` calls in a single response). They run in parallel — no dependencies between them. Record all three `agentId`s.` add:

```markdown
Arm one watchdog for the batch: `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_CRITICS")`.
```

- [ ] **Step 4: Verify**

Run: `grep -n "WATCHDOG_" ~/.claude/commands/spec.md`
Expected: matches for ANALYST (×2), ARCHITECT, CRITICS (×2).

Run: `grep -n "STATUS CHECK" ~/.claude/commands/spec.md`
Expected: no matches.

- [ ] **Step 5: Commit**

Invoke the `commit` skill with args: "Commit only commands/spec.md. Message: `feat(spec): wire liveness watchdog protocol into Phase 2 agent loops`. Direct to main per repo convention."

---

### Task 4: Wire the protocol into commands/implement-kimi.md

**Files:**
- Modify: `commands/implement-kimi.md` — reviewers section only (currently line 303). The kimi CLI bash-shell watchdog (strikes system, lines ~76–92) is a different mechanism for a different failure mode — do not touch it.

**Interfaces:**
- Consumes: `~/.claude/templates/liveness-protocol.md` from Task 1; watchdog name `WATCHDOG_REVIEW`.

- [ ] **Step 1: Add the protocol reference at the reviewers wait point**

Replace this paragraph (currently line 303):

```markdown
The completion notification is your done signal — do not poll for it. Each reviewer reports with the standard `REVIEWER:`/`VERDICT:`/`DEPTH:`/`FINDINGS:`/`SUMMARY:` block. Reject reports without a DEPTH block — re-run that reviewer (resume by `agentId` or spawn a fresh instance). Same rule if DEPTH counts look implausibly low for the diff.
```

with:

```markdown
The completion notification is your done signal — do not poll for it. A dead reviewer sends no notification: whenever you await reviewer reports (initial pass and re-review rounds), keep a dead-man timer armed per `~/.claude/templates/liveness-protocol.md` — `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_REVIEW")`, audit on every wake-up, ping by `agentId` first, respawn as escalation. Each reviewer reports with the standard `REVIEWER:`/`VERDICT:`/`DEPTH:`/`FINDINGS:`/`SUMMARY:` block. Reject reports without a DEPTH block — re-run that reviewer (resume by `agentId` or spawn a fresh instance). Same rule if DEPTH counts look implausibly low for the diff.
```

- [ ] **Step 2: Verify**

Run: `grep -n "WATCHDOG_REVIEW\|liveness-protocol" ~/.claude/commands/implement-kimi.md`
Expected: 1 line matching both.

- [ ] **Step 3: Commit**

Invoke the `commit` skill with args: "Commit only commands/implement-kimi.md. Message: `feat(implement-kimi): arm liveness watchdog while awaiting reviewers`. Direct to main per repo convention."

---

### Task 5: Manual validation (run with the user — do not automate)

**Files:** none (sandbox run).

This task is executed by the user (or a supervised session), not by a plan-executing subagent. Executor: present this checklist and stop.

- [ ] **Step 1:** Temporarily change `sleep 900` → `sleep 60` in `templates/liveness-protocol.md` (do not commit).
- [ ] **Step 2:** In a sandbox repo with SDD structure, run `/implement` on a tiny two-coder spec.
- [ ] **Step 3:** Simulate death: after the coder wave spawns, kill one coder — ask the lead to `TaskStop` it, or kill its tmux pane (`teammateMode: tmux`).
- [ ] **Step 4:** Expected: within ≤60 s the lead wakes on `WATCHDOG_CODE`, audits the registry, pings the dead coder by `agentId`; work completes; registry shows recovery_count = 1 for that coder.
- [ ] **Step 5:** Test the completed-without-marker branch: instruct one coder (via the spec or a follow-up message) to finish without saying `CODER DONE.` — the lead must accept the work from `TaskOutput`, not respawn.
- [ ] **Step 6:** Restore `sleep 900` in `templates/liveness-protocol.md` (`git checkout -- templates/liveness-protocol.md` if Step 1 was the only change).

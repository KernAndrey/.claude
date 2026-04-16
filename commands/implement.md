Implement an approved specification using an agent team.

All agents in this workflow are **teammates** spawned via `TeamCreate` + `Agent` with `team_name`. Never use the `Agent` tool without `team_name` — standalone subagents break coordination, messaging, and idle tracking.

Begin by saying to the user: **"I will spawn an agent team to implement this spec. I am the lead — I coordinate, I don't code or review."**

## Reviewer mode (required)

`$ARGUMENTS` must include `--reviewers claudecode` or `--reviewers opencode`. If missing — **ask the user before proceeding:**

> Which reviewer mode?
> - `claudecode` — Claude Code teammates (full coordination, re-review with context, UI review supported)
> - `opencode` — OpenCode `--pure` via GitHub Copilot (stateless, cheaper)

Parse `$ARGUMENTS` to extract both the task identifier and `--reviewers {mode}`. Store `{reviewer_mode}` for Phase 2 and 3.

## Quality mandate

Thoroughness over speed. This task may run for hours — that is expected and acceptable. Every phase completes fully. Each phase exists for a reason that automated tools (hooks, linters, CI) cannot replace.

## Setup

1. Read `.tasks.toml`, `CLAUDE.md`, and project structure.
2. Find the spec by `$ARGUMENTS` (ID or slug) in `tasks/3-ready/`.
3. Read the full specification.
4. Branch and worktree setup:
   - If `auto_branch = true`: fetch latest `dev` branch (`git fetch origin dev`), then `wt create task/{ID}-{slug} --base origin/dev`. Set `{worktree_path}` to the path returned by `wt create`. All teammates work inside the worktree directory.
   - If `auto_branch = false`: stay on the current branch. Set `{worktree_path}` to the current project root directory.
5. **Review prompt setup** (run inside `{worktree_path}`): if the project has `.claude/review_prompt.md`, reviewers will apply it as project-specific rules (severity overrides, design decisions to treat as intentional). In opencode mode, run the symlink setup from `~/.claude/review/guides/opencode-runner.md` inside the worktree to create agent symlinks under `.claude/agents-global/` (required because `opencode --pure` rejects paths outside the project).
6. Move spec to `tasks/4-in-progress/`. Update `status: in-progress`.
7. Note the **base branch** for diffs: `dev` if `auto_branch = true`, otherwise the current branch. Reviewers will need it.
8. **Create the team:** `TeamCreate` with `team_name: "impl-{ID}"`. You are the lead.

## Team & Communication

All agents are spawned as **teammates** (`team_name: "impl-{ID}"`). The lead has:
- **Live messaging** — SendMessage to any teammate, including idle ones.
- **Automatic delivery** — teammate messages arrive as conversation turns.
- **Idle notifications** — system notifies when a teammate goes idle.

**Done signals are mandatory.** Only `CODER DONE`, `TESTER DONE`, or `REVIEWER: ...` confirms completion. `idle_notification` alone is NOT a status update — it is the default post-turn state and carries no information about progress.

### Watchdog Protocol (mandatory)

Teammates that go idle may be genuinely waiting OR silently stuck. Never rely on passive waiting. For every teammate you spawn:

1. **On spawn**, start a 10-minute background timer:
   ```
   Bash(run_in_background=true, command="sleep 600 && echo watchdog:{teammate_name}")
   ```
2. **When the timer fires**, check the teammate's activity signal. The signal depends on role:
   - **Coder** — mtimes of files in its Work breakdown `files:` list (`stat -c '%Y %n' {files} | sort -n`). Fresh = any file modified <5 min ago.
   - **Tester** — mtimes of any file under `tests/` or matching `*test*` in the worktree. Fresh = any such file modified <5 min ago.
   - **Code-Reviewer / Test-Reviewer / Spec-Auditor / Security-Reviewer** — reviewers don't write files. Activity signal is teammate message count since last tick (check your conversation). Fresh = ≥1 message from the reviewer in the last 10 min (including PROGRESS heartbeats).
   - **UI-Reviewer** — either a message in the last 10 min OR new files under `/tmp/` / worktree matching `*.png` (screenshots).
3. **Classify the tick:**
   - Fresh signal → teammate is working. Restart the timer, reset strike counter.
   - Stale signal → send `STATUS CHECK: progress? blockers?`, restart the timer, add 1 strike.
4. **Strike escalation** (strikes are consecutive stale ticks):
   - **2 strikes** (~20 min no activity) → final ping: "No progress detected. Reply within the next turn or you will be replaced."
   - **3 strikes** (~30 min) → kill the teammate (`{type: "shutdown_request"}`; `tmux kill-pane` if unresponsive), spawn a replacement with narrower scope and a summary of completed work.
5. **Unstick loopers.** If a teammate reports the same error repeatedly — intervene with a specific hint (code pointer, alternative approach, spec clarification) instead of waiting.
6. **Crash recovery.** If a teammate's process terminates (pane closes, context overflow), read their last output, then spawn a replacement with (a) narrowed context, (b) summary of completed work, (c) specific remaining tasks. Max **3 restart attempts** per teammate; after that the lead takes over directly.

## Agent Team — 6 teammates, sequential phases

Each teammate reads their agent file for full instructions.
Complete every phase in sequence. All phases are mandatory.

---

### Phase 1a: Code

#### Read the Architect's Work breakdown

The `## Architecture & Implementation Plan → Work breakdown → Coders` subsection of the spec is authoritative. The Architect already decided how many Coders to spawn and which files each one owns. **You do not re-analyze the spec for parallelization** — just spawn what's listed.

#### Sanity check (lead, ~30 seconds)

Before spawning, verify the breakdown isn't broken:
- Take the union of `files:` lists from all coders. Does it match the full set under "Files to create" + "Files to modify"? Flag gaps and overlaps.
- Are file paths real (or explicitly noted as new)?
- Does each coder's scope make sense given the file list?

If the breakdown is broken (gaps, overlaps, nonsense scopes): **do not silently fix it**. Stop, report the issue to the user, and ask whether to (a) patch the breakdown manually before continuing, or (b) send the spec back to `tasks/2-spec/` for the Architect to redo. If (b): move the spec file back from `tasks/4-in-progress/` to `tasks/2-spec/`, reset frontmatter `status` from `in-progress` to `awaiting-approval`, remove the worktree if one was created (`wt remove task/{ID}-{slug}`), and shut down any teammates already spawned. The Critic should have caught this — flag it as a Critic miss too.

#### Spawn Coders from the breakdown

For each Coder listed in Work breakdown, spawn it as a teammate (`name: "coder-N"`, `team_name: "impl-{ID}"`). Send the task via message:

> Read your instructions: `~/.claude/agents/coder.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> **Your scope** (from spec → Work breakdown → coder-N): {scope text from spec}
> **Files you own:** {files list from spec}
> **Do not touch any other files in the spec** — they belong to other coders.
> Implement your scope. Message me when done with `CODER DONE.` and list of changed files.
> **Heartbeat:** every 10 min of work OR 5 file edits, send a one-line `PROGRESS: [just finished] → [doing next]`. If blocked for more than 5 min, send `BLOCKED: [reason]`. Silent idling is not acceptable — you are watchdogged on 10-min intervals.

For single-coder tasks (one entry in Work breakdown), the message is the same — just one teammate, scope and file list copied from the spec.

Monitor: if any Coder goes idle without a done signal — send a status check.

**Phase 1a is complete when all Coders from the breakdown have messaged:** `CODER DONE.` with changed files lists.

---

### Phase 1b: Test

Say: **"Coders are done. Spawning Tester to write tests. I will coordinate bug fixes between them if needed."**

Start only after Phase 1a is complete.

There is always exactly **one** Tester, regardless of how many Coders ran. Parallel testers are intentionally excluded — they conflict on shared test infrastructure (DBs, fixtures, ports). One Tester sees all the code and writes tests for the full implementation.

Spawn **Tester** as a teammate (`name: "tester"`, `team_name: "impl-{ID}"`).
Send the task via message:

> Read your instructions: `~/.claude/agents/tester.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Coding is done. Changed files: {combined changed files from all coders}
> Write tests for the implementation. Message me when done with `TESTER DONE.` and test results.
> If you find a production bug, message me with `PRODUCTION BUG FOUND` and details, including the affected file path so I can route the fix to the right coder.
> **Heartbeat:** every 10 min of work OR 5 file edits, send a one-line `PROGRESS: [just finished] → [doing next]`. If blocked for more than 5 min, send `BLOCKED: [reason]`. Silent idling is not acceptable — you are watchdogged on 10-min intervals.

If Tester reports `PRODUCTION BUG FOUND`:
- Map the affected file → owning Coder via Work breakdown's `files:` lists. Message that Coder.
- Wait for Coder's `CODER FIX APPLIED` message.
- Message Tester to re-run affected tests.
- Repeat until all bugs resolved.
- Maximum **7 bug-fix rounds**. If bugs persist after 7 rounds — lead investigates directly: read the failing test, read the production code, diagnose and fix.

Monitor: if Tester goes idle without a done signal — send a status check.

**Phase 1b is complete when Tester messages:** `TESTER DONE.` with test count and results.

---

### Phase 2: Review (4–5 parallel reviewers)

Say: **"Code and tests are done. Spawning 4-5 reviewers in parallel. I will wait for all reports before proceeding."**

This phase runs after Phase 1 regardless of time spent or code quality. All reviewers must report. Hooks, automated linters, CI checks, or prior review rounds do not substitute for Phase 2.

**Start only after Phase 1b is complete.**

#### Determine if UI review is needed

Check the changed files list from Coder. If ANY file matches a frontend pattern — spawn the UI-Reviewer:
- `.xml`, `.html`, `.css`, `.scss`, `.less` — always
- `.js`, `.jsx`, `.ts`, `.tsx`, `.vue`, `.svelte` — always
- `.qweb`, `.mako`, `.jinja2` — template files

If all changes are purely backend (`.py`, `.sql`, config `.json`) — skip UI-Reviewer.

#### Reviewer list

- **Code-Reviewer** — production code quality
- **Test-Reviewer** — test quality and coverage
- **Spec-Auditor** — spec compliance
- **Security-Reviewer** — security and architecture
- **UI-Reviewer** — visual verification *(only if frontend files changed)*

Each reviewer will report in this format (defined in their agent file):
```
REVIEWER: {role}
VERDICT: CLEAN/SECURE/COMPLIANT | HAS FINDINGS

DEPTH:
- {items audited: count, list or summary — format varies by role}
- {additional depth fields specific to the reviewer}

FINDINGS: ...
SUMMARY: X findings (Y MUST FIX, Z NIT/CONCERN)
```

**Reject reports without a DEPTH block.** The DEPTH counts are how you detect shallow reviews. If a reviewer reports `VERDICT` and `FINDINGS` but omits `DEPTH`, re-run that reviewer. Same rule if counts look implausibly low for the diff (e.g. "Methods audited: 2" on a 20-method diff).

---

#### Mode A: `--reviewers claudecode`

Read and follow `~/.claude/review/guides/claudecode-runner.md` — Phase 2 section.

#### Mode B: `--reviewers opencode`

Read and follow `~/.claude/review/guides/opencode-runner.md` — Phase 2 section.

---

### Phase 3: Fix & Verify (lead-orchestrated)

Say: **"All reviewers reported. I will now orchestrate fix rounds — sending findings to Coder and Tester, then re-reviewing until all MUST FIX items are resolved."**

Precondition: All spawned Phase 2 reviewers must have reported.

#### Step 1: Assess

From all reviewer reports, build two fix lists:
- **Coder fixes**: `MUST FIX` / `CRITICAL` findings from Code-Reviewer, Spec-Auditor, Security-Reviewer, UI-Reviewer
- **Tester fixes**: `MUST FIX` findings from Test-Reviewer, missing coverage from Spec-Auditor

If zero `MUST FIX` / `CRITICAL` across all reviewers — skip to Finalization.

**Conflict resolution priority:** Security CRITICAL > Spec compliance > Code quality.

#### Step 2: Fix round

Group production fixes by owning Coder (use Work breakdown's `files:` lists to map file → coder). Message each affected Coder only with their fixes:
> These findings need to be fixed. For each item: severity, source reviewer, file:line, description.
> After fixing, message me: `CODER FIX ROUND DONE.` Include a note if any API or behavior changed.

If any Coder reports API/behavior changes — forward those to the Tester.

Message Tester with all test fixes (if any):
> These test findings need to be fixed. For each item: severity, source reviewer, test file, description.
> Re-run all tests after fixes. Then message me: `TESTER FIX ROUND DONE.`

#### Step 3: Verification

Follow the re-review procedure from the active reviewer runner guide:
- **claudecode:** `~/.claude/review/guides/claudecode-runner.md` — Phase 3 Step 3
- **opencode:** `~/.claude/review/guides/opencode-runner.md` — Phase 3 Step 3

#### Step 4: Fix loop and escalation

If any reviewer returned non-PASS — repeat Steps 2-3. Maximum **7 iterations**.

If the SAME finding persists unfixed for 2 consecutive iterations — lead investigates directly and fixes it.

After 7 iterations with findings still unresolved:
- Lead takes over: read the code, diagnose, and fix the remaining issues directly.
- If lead cannot fix — **ask user**: "These findings remain after 7 fix rounds and my own attempt. Options:
  (A) Continue to manual review — remaining issues documented in Known Concerns.
  (B) Abort — return spec to `tasks/3-ready/` with findings attached as implementation notes."
- If user picks B: revert worktree changes, move spec back, shutdown teammates.

---

## Finalization (Lead)

Say: **"Fix rounds complete. Running gate check, final test suite, then committing and pushing."**

### Gate check — verify before continuing:

- Phase 1a — **every** Coder from Work breakdown sent `CODER DONE`? If NO → message the missing one(s) NOW.
- Phase 1b — Tester sent `TESTER DONE` with test count? If NO → message Tester NOW.
- Phase 2 — Code-Reviewer reported? If NO → message or spawn NOW.
- Phase 2 — Test-Reviewer reported? If NO → message or spawn NOW.
- Phase 2 — Spec-Auditor reported? If NO → message or spawn NOW.
- Phase 2 — Security-Reviewer reported? If NO → message or spawn NOW.
- Phase 2 — UI-Reviewer reported? (only if spawned) If NO → message or spawn NOW.
- Phase 3 — Fix iterations completed (or no MUST FIX items)? If NO → run NOW.

### Final test run

Message Tester: "Run the full test suite and report results."
All tests pass → proceed to Steps. Tests fail → back to Phase 3 Step 2 for one more fix round.

### Steps

Run inside the worktree directory when `auto_branch = true`:

1. Append sections from `~/.claude/templates/sdd/implementation-sections.md` to the spec file:
   - **Implementation Summary**: what was done, key decisions
   - **Known Concerns**: unresolved findings (reviewer name, severity, description for each)
   - **Auto-Review Results**: test results, criteria coverage, verbatim VERDICT and SUMMARY from each spawned reviewer
   - **Steps for Manual Review**: 3-7 concrete steps. Format: `N. [Action] → [Expected result]`

2. Update frontmatter:
   - `status: review`, `completed: {TODAY}`, `updated: {TODAY}`
   - `branch: task/{ID}-{slug}` (if `auto_branch = true`; otherwise current branch)

3. Move file from `tasks/4-in-progress/` to `tasks/5-review/`.

4. Git commit — split changes into logical commits. Group by cohesive unit: each feature chunk together with its tests, config changes separately, etc. Each commit gets a conventional commit message prefixed with the task ID:
   - `feat({ID}): add order model with validation and tests`
   - `feat({ID}): add order API endpoints and tests`
   - `chore({ID}): update config for order module`
   Do not lump all changes into a single commit — logical splitting makes bisect and revert possible.

5. If `auto_branch = true`: `git push -u origin task/{ID}-{slug}` (inside worktree).

6. If `auto_branch = true`: `wt remove task/{ID}-{slug}`.

7. Shutdown all teammates: send `{type: "shutdown_request"}` to each.

8. Output:
   - Implementation Summary (brief)
   - Known Concerns (if any)
   - Steps for Manual Review (full list)
   - Instruction: "Walk through the manual review steps. If everything looks good — `/task-done {ID}`"

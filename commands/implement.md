Implement an approved specification using an agent team.

All agents in this workflow are **teammates** spawned via `TeamCreate` + `Agent` with `team_name`. Never use the `Agent` tool without `team_name` — standalone subagents break coordination, messaging, and idle tracking.

Begin by saying to the user: **"I will spawn an agent team to implement this spec. I am the lead — I coordinate, I don't code or review."**

## Quality mandate

Thoroughness over speed. This task may run for hours — that is expected and acceptable. Every phase completes fully. Each phase exists for a reason that automated tools (hooks, linters, CI) cannot replace.

## Setup

1. Read `.tasks.toml`, `CLAUDE.md`, and project structure.
2. Find the spec by `$ARGUMENTS` (ID or slug) in `tasks/3-ready/`.
3. Read the full specification.
4. Branch and worktree setup:
   - If `auto_branch = true`: fetch latest `dev` branch (`git fetch origin dev`), then `wt create task/{ID}-{slug} --base origin/dev`. Set `{worktree_path}` to the path returned by `wt create`. All teammates work inside the worktree directory.
   - If `auto_branch = false`: stay on the current branch. Set `{worktree_path}` to the current project root directory.
5. Move spec to `tasks/4-in-progress/`. Update `status: in-progress`.
6. Note the **base branch** for diffs: `dev` if `auto_branch = true`, otherwise the current branch. Reviewers will need it.
7. **Create the team:** `TeamCreate` with `team_name: "impl-{ID}"`. You are the lead.

## Team & Communication

All agents are spawned as **teammates** (`team_name: "impl-{ID}"`). This gives the lead:
- **Live messaging** — SendMessage to any teammate at any time (even idle ones).
- **Automatic delivery** — teammate messages arrive as conversation turns, no polling needed.
- **Idle notifications** — the system notifies the lead when a teammate goes idle.

### Coordination rules
1. **Teammates go idle between turns — this is normal.** Idle means "waiting for input", not "stuck". Send a message to wake them up.
2. **Supervise actively.** If a teammate goes idle without sending a done signal — message them: `STATUS CHECK: What is your current progress? Any blockers?`
3. **Unstick looping agents.** If a teammate reports the same error or action repeatedly — intervene with a specific suggestion: point to relevant code, suggest a different approach, or clarify the spec.
4. **Done signals are mandatory.** Only `CODER DONE`, `TESTER DONE`, or `REVIEWER: ...` confirms completion.
5. **Replace a non-responsive teammate:** read their output → diagnose the issue → spawn a replacement with a corrected prompt.
6. **Recover from crashes.** If any teammate's process terminates (tmux pane closes, context overflow):
   - Read their last output.
   - Spawn a replacement with (a) narrowed context — only relevant files, (b) summary of completed work, (c) specific remaining tasks.
   - Resume from where the previous teammate stopped.
   - Maximum **3 restart attempts** per teammate. After 3 failed restarts — lead takes over the remaining work directly.

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

#### Spawn reviewers

Spawn all as teammates and send each their task:

- **Code-Reviewer** (`name: "code-reviewer"`) — production code quality
- **Test-Reviewer** (`name: "test-reviewer"`) — test quality and coverage
- **Spec-Auditor** (`name: "spec-auditor"`) — spec compliance
- **Security-Reviewer** (`name: "security-reviewer"`) — security and architecture
- **UI-Reviewer** (`name: "ui-reviewer"`) — visual verification *(only if frontend files changed)*

Each code reviewer message:

> Read your instructions: `~/.claude/agents/{agent-name}.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Base branch for diff: `{base_branch}`
> Report findings to me using the format from your agent file.

UI-Reviewer message (when spawned):

> Read your instructions: `~/.claude/agents/ui-reviewer.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Base branch for diff: `{base_branch}`
> Changed files: {combined changed files from all coders}
> URL hints: {any relevant URLs or pages you can identify from the spec}
> Report findings to me using the format from your agent file.

If UI-Reviewer reports `VERDICT: BLOCKED` (cannot start dev server, browser unavailable):
- Kill the reviewer and spawn a replacement with a troubleshooting hint (check port, install deps, try alternative start command).
- Retry up to **3 times**, each with a different hint.
- After 3 failed attempts: document reason in Known Concerns, add manual UI check to Steps for Manual Review, and continue.

Each reviewer will report in this format (defined in their agent file):
```
REVIEWER: {role}
VERDICT: CLEAN/SECURE/COMPLIANT | HAS FINDINGS
FINDINGS: ...
SUMMARY: X findings (Y MUST FIX, Z NIT/CONCERN)
```

Monitor: track which reviewers have reported. If any goes idle without reporting — send a status check.

**Phase 2 is complete when ALL spawned reviewers have reported to the lead.**

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

Message existing reviewers who had `MUST FIX` or `CRITICAL` findings:
> This is a **re-review**. Check ONLY your previously raised MUST FIX / CRITICAL items.
> Report: `PASS` if all resolved, or list remaining issues.

If a reviewer is unresponsive after 1 status check — spawn a replacement with the same narrowed scope (re-check listed items only).

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

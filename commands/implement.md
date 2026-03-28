Implement an approved specification using an agent team.

**QUALITY MANDATE**: Thoroughness over speed. This task may run for hours — that is expected and acceptable. Every phase completes fully. Each phase exists for a reason that automated tools (hooks, linters, CI) cannot replace.

## Setup

1. Read `.tasks.toml`, `CLAUDE.md`, and project structure.
2. Find the spec by `$ARGUMENTS` (ID or slug) in `tasks/3-ready/`.
3. Read the full specification.
4. Branch and worktree setup:
   - If `auto_branch = true`: `wt create task/{ID}-{slug}`. Set `{worktree_path}` to the path returned by `wt create`. All teammates work inside the worktree directory.
   - If `auto_branch = false`: stay on the current branch. Set `{worktree_path}` to the current project root directory.
5. Move spec to `tasks/4-in-progress/`. Update `status: in-progress`.
6. Note the **base branch** for diffs (usually `main`). Reviewers will need it.
7. **Create the team:** `TeamCreate` with `team_name: "impl-{ID}"`. You are the lead.

## Team & Communication

All agents are spawned as **teammates** (`team_name: "impl-{ID}"`). This gives the lead:
- **Live messaging** — SendMessage to any teammate at any time (even idle ones).
- **Automatic delivery** — teammate messages arrive as conversation turns, no polling needed.
- **Idle notifications** — the system notifies the lead when a teammate goes idle.

**COORDINATION RULES**:
1. **Teammates go idle between turns — this is normal.** Idle means "waiting for input", not "stuck". Send a message to wake them up.
2. **Supervise actively.** If a teammate goes idle without sending a done signal — message them: `STATUS CHECK: What is your current progress? Any blockers?`
3. **Unstick looping agents.** If a teammate reports the same error or action repeatedly — intervene with a specific suggestion: point to relevant code, suggest a different approach, or clarify the spec.
4. **Done signals are mandatory.** Only `CODER DONE`, `TESTER DONE`, or `REVIEWER: ...` confirms completion.
5. **Replace a non-responsive teammate:** read their output → diagnose the issue → spawn a replacement with a corrected prompt.

## Agent Team — 6 teammates, sequential phases

Each teammate reads their agent file for full instructions.
All phases are mandatory. No phase may be skipped or merged.

---

### Phase 1a: Code

Spawn **Coder** as a teammate (`name: "coder"`, `team_name: "impl-{ID}"`).
Send the task via message:

> Read your instructions: `~/.claude/agents/coder.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Implement the spec. Message me when done with `CODER DONE.` and list of changed files.

Monitor: if Coder goes idle without a done signal — send a status check.

**Phase 1a is complete when Coder messages:** `CODER DONE.` with changed files list.

---

### Phase 1b: Test

Start only after Phase 1a is complete.
Spawn **Tester** as a teammate (`name: "tester"`, `team_name: "impl-{ID}"`).
Send the task via message:

> Read your instructions: `~/.claude/agents/tester.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Coder is done. Changed files: {changed_files_from_coder}
> Write tests for the implementation. Message me when done with `TESTER DONE.` and test results.
> If you find a production bug, message me with `PRODUCTION BUG FOUND` and details.

If Tester reports `PRODUCTION BUG FOUND`:
- Message Coder with the bug report (Coder is still alive as a teammate).
- Wait for Coder's `CODER FIX APPLIED` message.
- Message Tester to re-run affected tests.
- Repeat until all bugs resolved.

Monitor: if Tester goes idle without a done signal — send a status check.

**Phase 1b is complete when Tester messages:** `TESTER DONE.` with test count and results.

---

### Phase 2: Review (4 parallel reviewers)

This phase runs after Phase 1 regardless of time spent or code quality. All 4 reviewers must report. Hooks, automated linters, CI checks, or prior review rounds do not substitute for Phase 2.

**Start only after Phase 1b is complete.**

Spawn all 4 as teammates and send each their task:

- **Code-Reviewer** (`name: "code-reviewer"`) — production code quality
- **Test-Reviewer** (`name: "test-reviewer"`) — test quality and coverage
- **Spec-Auditor** (`name: "spec-auditor"`) — spec compliance
- **Security-Reviewer** (`name: "security-reviewer"`) — security and architecture

Each reviewer message:

> Read your instructions: `~/.claude/agents/{agent-name}.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Base branch for diff: `{base_branch}`
> Report findings to me using the format from your agent file.

Each reviewer will report in this format (defined in their agent file):
```
REVIEWER: {role}
VERDICT: CLEAN/SECURE/COMPLIANT | HAS FINDINGS
FINDINGS: ...
SUMMARY: X findings (Y MUST FIX, Z ...)
```

Monitor: track which reviewers have reported. If any goes idle without reporting — send a status check.

**Phase 2 is complete when ALL 4 reviewers have reported to the lead.**

---

### Phase 3: Fix & Verify (lead-orchestrated)

Precondition: All 4 Phase 2 reviewers must have reported.

#### Step 1: Assess

From all 4 reviewer reports, build two fix lists:
- **Coder fixes**: `MUST FIX` / `CRITICAL` findings from Code-Reviewer, Spec-Auditor, Security-Reviewer
- **Tester fixes**: `MUST FIX` findings from Test-Reviewer, missing coverage from Spec-Auditor

If zero `MUST FIX` / `CRITICAL` across all reviewers — move all `SHOULD FIX` items to Known Concerns and skip to Finalization.

#### Step 2: Fix round

Message Coder with all production code fixes:
> These findings need to be fixed. For each item: severity, source reviewer, file:line, description.
> After fixing, message me: `CODER FIX ROUND DONE.` Include a note if any API or behavior changed.

If Coder reports API/behavior changes — forward those to Tester.

Message Tester with all test fixes (if any):
> These test findings need to be fixed. For each item: severity, source reviewer, test file, description.
> Re-run all tests after fixes. Then message me: `TESTER FIX ROUND DONE.`

#### Step 3: Verification

Spawn ONLY the reviewers who had `MUST FIX` or `CRITICAL` findings (or message existing ones).
Same task as Phase 2, but add:
> This is a **re-review**. Check ONLY your previously raised MUST FIX / CRITICAL items.
> Report: `PASS` if all resolved, or list remaining issues.

#### Step 4: Loop

If any reviewer returned non-PASS — repeat steps 2-3.
Maximum **5 iterations**.

#### Step 5: Escalation

If the SAME finding persists unfixed for 2 consecutive iterations — lead investigates
directly and either fixes it or documents why it cannot be resolved within current scope.

After 5 iterations: move all remaining items to Known Concerns with full detail.

**Conflict resolution priority:** Security CRITICAL > Spec compliance > Code quality > Style nits.

---

## Finalization (Lead)

### Gate check — verify before continuing:

- Phase 1a — Coder sent `CODER DONE`? If NO → message Coder NOW.
- Phase 1b — Tester sent `TESTER DONE` with test count? If NO → message Tester NOW.
- Phase 2 — Code-Reviewer reported? If NO → message or spawn NOW.
- Phase 2 — Test-Reviewer reported? If NO → message or spawn NOW.
- Phase 2 — Spec-Auditor reported? If NO → message or spawn NOW.
- Phase 2 — Security-Reviewer reported? If NO → message or spawn NOW.
- Phase 3 — Fix iterations completed (or no MUST FIX items)? If NO → run NOW.

### Steps

Run inside the worktree directory when `auto_branch = true`:

1. Append sections from `~/.claude/templates/sdd/implementation-sections.md` to the spec file:
   - **Implementation Summary**: what was done, key decisions, what was deferred
   - **Known Concerns**: unresolved findings (reviewer name, severity, description for each)
   - **Auto-Review Results**: test results, criteria coverage, verbatim VERDICT and SUMMARY from each of the 4 reviewers
   - **Steps for Manual Review**: 3-7 concrete steps. Format: `N. [Action] → [Expected result]`

2. Update frontmatter:
   - `status: review`, `completed: {TODAY}`, `updated: {TODAY}`
   - `branch: task/{ID}-{slug}` (if `auto_branch = true`; otherwise current branch)

3. Move file from `tasks/4-in-progress/` to `tasks/5-review/`.

4. Git commit: `feat({ID}): {title}`

5. If `auto_branch = true`: `wt remove task/{ID}-{slug}`.

6. Shutdown all teammates: send `{type: "shutdown_request"}` to each.

7. Output:
   - Implementation Summary (brief)
   - Known Concerns (if any)
   - Steps for Manual Review (full list)
   - Instruction: "Walk through the manual review steps. If everything looks good — `/task-done {ID}`"

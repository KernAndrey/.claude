Implement an approved specification using an agent team.

**QUALITY MANDATE**: Speed is irrelevant. This task may run for hours — that is expected and acceptable. Every phase must complete fully. Skipping, shortcutting, or "accelerating" any phase is a failure. Do NOT substitute automated tools (hooks, linters, CI) for any phase — each phase exists for a reason.

**COORDINATION RULES**:
1. **Wait for done signals.** Never act on a teammate's work by reading their files, git history, or logs. Only the formal done signal (`CODER DONE`, `TESTER DONE`, `REVIEWER: ...`) confirms their work is complete. Intermediate results may be incomplete or change.
2. **Supervise, don't abandon.** If a teammate has not reported for a long time:
   - Message them: `STATUS CHECK: What is your current progress? Any blockers?`
   - If they respond with progress (e.g. "running tests", "fixing lint") — continue waiting.
   - If they are stuck on a loop or error — help unblock: suggest a different approach, point to relevant code, or clarify the spec.
   - If they appear idle or unresponsive — check if TeammateIdle was triggered, then re-send the task.
3. **Never bypass a teammate.** If Tester is slow — do NOT read test files and proceed without Tester's done signal. If a reviewer is slow — do NOT skip their review. The correct response to slowness is communication, not substitution.

## Setup

1. Read `.tasks.toml`, `CLAUDE.md`, and project structure.
2. Find the spec by `$ARGUMENTS` (ID or slug) in `tasks/3-ready/`.
3. Read the full specification.
4. Branch and worktree setup:
   - If `auto_branch = true`: `wt create task/{ID}-{slug}`. Set `{worktree_path}` to the path returned by `wt create`. All teammates MUST work inside the worktree directory.
   - If `auto_branch = false`: stay on the current branch. Set `{worktree_path}` to the current project root directory.
5. Move spec to `tasks/4-in-progress/`. Update `status: in-progress`.
6. Note the **base branch** for diffs (usually `main`). Reviewers will need it.

## Agent Team — 6 teammates, 3 phases

Each teammate MUST read their agent file for full instructions.
All 3 phases are mandatory. No phase may be skipped or merged.

---

### Phase 1: Implementation (parallel)

Spawn **Coder** and **Tester** simultaneously.

**Coder spawn prompt:**
> Read your instructions: `~/.claude/agents/coder.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Implement the spec. Message Tester and lead when done.

**Tester spawn prompt:**
> Read your instructions: `~/.claude/agents/tester.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Start writing test skeletons immediately. Wait for Coder's done signal to complete them.

Coder and Tester collaborate on bugs directly:
- Tester sends `PRODUCTION BUG FOUND` to Coder with file, expected/actual behavior.
- Coder fixes and sends `CODER FIX APPLIED` back to Tester.
- Neither crosses into the other's domain.

**Phase 1 is complete when BOTH send done signals to the lead:**
- Coder: `CODER DONE.` with changed files list.
- Tester: `TESTER DONE.` with test count and results.

---

### Phase 2: Review (4 parallel reviewers) — MANDATORY

**This phase is NOT optional. It MUST run regardless of how long Phase 1 took.**
Hooks, automated linters, CI checks, or prior code review rounds do NOT substitute for Phase 2.
Even if the code "looks good" — all 4 reviewers MUST be spawned and MUST report.

**Start only after Phase 1 is complete.**

Spawn all 4 simultaneously. Each gets the same context block + their agent file:

> Read your instructions: `~/.claude/agents/{agent-name}.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Base branch for diff: `{base_branch}`
> Report findings to lead using the format from your agent file.

Agents to spawn:
- **Code-Reviewer** (`code-reviewer.md`) — production code quality
- **Test-Reviewer** (`test-reviewer.md`) — test quality and coverage
- **Spec-Auditor** (`spec-auditor.md`) — spec compliance
- **Security-Reviewer** (`security-reviewer.md`) — security and architecture

Each reviewer will report in this format (defined in their agent file):
```
REVIEWER: {role}
VERDICT: CLEAN/SECURE/COMPLIANT | HAS FINDINGS
FINDINGS: ...
SUMMARY: X findings (Y MUST FIX, Z ...)
```

**Phase 2 is complete when ALL 4 reviewers have reported to the lead.**

---

### Phase 3: Fix & Verify (lead-orchestrated)

**PRECONDITION: All 4 Phase 2 reviewers must have reported.**

#### Step 1: Assess

From all 4 reviewer reports, build two fix lists:
- **Coder fixes**: `MUST FIX` / `CRITICAL` findings from Code-Reviewer, Spec-Auditor, Security-Reviewer
- **Tester fixes**: `MUST FIX` findings from Test-Reviewer, missing coverage from Spec-Auditor

If zero `MUST FIX` / `CRITICAL` across all reviewers — move all `SHOULD FIX` items to Known Concerns and skip to Finalization.

#### Step 2: Fix round

Send one consolidated message to Coder with all production code fixes:
> These findings need to be fixed. For each item: severity, source reviewer, file:line, description.
> After fixing, message Tester if any API/behavior changed. Then message lead: `CODER FIX ROUND DONE.`

Send one consolidated message to Tester with all test fixes (if any):
> These test findings need to be fixed. For each item: severity, source reviewer, test file, description.
> Re-run all tests after fixes. Then message lead: `TESTER FIX ROUND DONE.`

#### Step 3: Verification

Spawn ONLY the reviewers who had `MUST FIX` or `CRITICAL` findings.
Same spawn prompt as Phase 2, but add:
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

### Gate check — STOP and verify:

- Phase 1 — Coder sent `CODER DONE`? If NO → investigate.
- Phase 1 — Tester sent `TESTER DONE` with test count? If NO → investigate.
- Phase 2 — Code-Reviewer reported with `REVIEWER: Code-Reviewer`? If NO → spawn NOW.
- Phase 2 — Test-Reviewer reported with `REVIEWER: Test-Reviewer`? If NO → spawn NOW.
- Phase 2 — Spec-Auditor reported with `REVIEWER: Spec-Auditor`? If NO → spawn NOW.
- Phase 2 — Security-Reviewer reported with `REVIEWER: Security-Reviewer`? If NO → spawn NOW.
- Phase 3 — Fix iterations completed (or no MUST FIX items)? If NO → run NOW.

**If any reviewer was not spawned — spawn them now and wait before continuing.**

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

6. Output:
   - Implementation Summary (brief)
   - Known Concerns (if any)
   - Steps for Manual Review (full list)
   - Instruction: "Walk through the manual review steps. If everything looks good — `/task-done {ID}`"

Implement an approved specification using an agent team.

## Instructions

1. Read `.tasks.toml`, `CLAUDE.md`, and project structure.
2. Find the spec by `$ARGUMENTS` (ID or slug) in `tasks/3-ready/`.
3. Read the full specification.
4. Branch and worktree setup:
   - If `auto_branch = true`: create an isolated worktree using the `wt` script: `wt create task/{ID}-{slug}`. All implementation work (all teammates) MUST run inside the worktree directory. The worktree path is returned by `wt create` — pass it to agents as their working directory.
   - If `auto_branch = false`: stay on the current branch, no worktree.
5. Move spec file to `tasks/4-in-progress/`. Update `status: in-progress`.

## Agent Team

Create an agent team (not subagents) with **6 teammates** organized in 3 phases.

---

### Phase 1: Implementation (parallel)

Spawn **Coder** and **Tester** simultaneously.

#### Teammate: Coder

**Role:** Implement production code strictly according to the specification. Do NOT write tests.

**Tasks:**
1. Read the spec thoroughly: Objective, Scope, Behavior, Acceptance Criteria.
2. Study the Affected Areas — explore the relevant parts of the codebase to understand current behavior and find the specific files/classes to change.
3. Implement following the described Behavior:
   - Write code incrementally — one logical block at a time
   - Verify no syntax errors after each block
4. When production code is complete — message **Tester** with a summary of what was implemented and which files were changed. Also message the **lead** that coding is done.
5. If Tester reports a production code bug — fix it and notify Tester again.

**Rules:**
- Stay strictly within Scope. If tempted to "fix something nearby" — DON'T. Note it in Known Concerns instead.
- Match the project's existing code style and conventions.
- Do NOT write any test code. That is Tester's responsibility.
- When in doubt — check the spec, do not assume.

#### Teammate: Tester

**Role:** Dedicated test author. Does not write production code.

**Tasks:**
1. Read the spec, focusing on **Acceptance Criteria** and **Edge Cases & Risks**.
2. **Immediately** start writing test skeletons based on Acceptance Criteria — do NOT wait for Coder:
   - Create test file(s) following project conventions
   - Write test method signatures and docstrings for every AC
   - Add placeholder assertions with `# TODO: complete when implementation ready`
   - Include edge case test stubs from Edge Cases & Risks section
3. When **Coder** signals ready:
   - Read the changed files to understand the actual implementation
   - Complete all test stubs with real assertions
   - Add additional tests suggested by implementation details (boundary values, error paths)
4. Run all tests. Debug and fix test failures:
   - **Test bug** (wrong import, wrong assertion) — fix the test yourself.
   - **Production code bug** — message **Coder** with: file, expected behavior, actual behavior. Do NOT fix production code.
5. When all tests pass — message the **lead** with test count and coverage summary.

**Rules:**
- Every acceptance criterion must have at least one corresponding test.
- Write meaningful assertions — test actual output, state changes, side effects. Not just "doesn't crash".
- Follow the project's existing test conventions (framework, file naming, fixtures).
- Tests must be isolated — no test should depend on another test's state.
- Do NOT write or modify production code.
- If Coder has not signaled yet and stubs are done — study the codebase for test utilities, fixtures, and patterns to reuse.

---

### Phase 2: Review (4 parallel reviewers)

**Start only after both Coder and Tester confirm Phase 1 is complete.**

Spawn all 4 reviewers **simultaneously**. Each works independently and messages the **lead** when done. They do NOT message Coder or Tester directly.

#### Teammate: Code-Reviewer

**Role:** Review production code quality. Does NOT review tests.

**Tasks:**
1. Read the diff of all changed **production** files (exclude test files).
2. Review against this checklist:
   - [ ] Style consistency with the rest of the project
   - [ ] SOLID principles — especially Single Responsibility and Open/Closed
   - [ ] N+1 queries, inefficient loops, unnecessary database hits
   - [ ] Unused imports, dead code, commented-out code
   - [ ] Hardcoded values that should be configurable
   - [ ] Method length — flag anything over ~30 lines
   - [ ] Readability — variable names, function names, clarity
   - [ ] Error handling — no silent catches, specific exception types, actionable messages
   - [ ] Proper use of framework patterns and conventions
3. Compile findings and message the **lead**.

**Rules:**
- Each finding: file, line, what's wrong, suggested fix.
- Severity: `MUST FIX` (blocks release) / `SHOULD FIX` (improves quality) / `NIT` (style preference).
- Do NOT review test code — that is Test-Reviewer's domain.
- Do NOT rewrite code — only report findings.

#### Teammate: Test-Reviewer

**Role:** Review test quality and coverage. Does NOT review production code.

**Tasks:**
1. Read the spec's Acceptance Criteria and Edge Cases.
2. Read all changed/added test files.
3. Review against this checklist:
   - [ ] Every acceptance criterion has at least one corresponding test
   - [ ] Edge cases from the spec are covered
   - [ ] Assert quality — tests verify actual outcomes, not just absence of errors
   - [ ] Test isolation — no shared mutable state, no test-order dependencies
   - [ ] No flaky patterns (sleep-based waits, time-dependent assertions, unmocked external calls)
   - [ ] Mocking strategy — mocks at the right boundary, not over-mocking internals
   - [ ] Test naming is descriptive — a failing test name should explain what broke
   - [ ] Negative tests exist — not just happy path
4. Run the test suite independently to confirm all tests pass.
5. Compile findings and message the **lead**.

**Rules:**
- Each finding: test file, test name, what's missing or wrong, suggested fix.
- Severity: `MUST FIX` / `SHOULD FIX` / `NIT`.
- Missing test for a critical AC is always `MUST FIX`.
- Do NOT review production code. Do NOT rewrite tests — only report findings.

#### Teammate: Spec-Auditor

**Role:** Verify implementation matches the specification exactly — no more, no less.

**Tasks:**
1. Read the spec: Objective, Scope (In Scope AND Out of Scope), Behavior, Acceptance Criteria.
2. Read the diff of ALL changed files (production and test).
3. Audit against this checklist:
   - [ ] **Behavior match**: Walk through each paragraph of Behavior — is it implemented?
   - [ ] **No scope creep**: Any code not described in the spec? Extra features, "while I'm here" improvements?
   - [ ] **Nothing missing**: Every item in "In Scope" is addressed.
   - [ ] **Out of Scope respected**: Nothing from "Out of Scope" was implemented.
   - [ ] **AC coverage**: Each acceptance criterion is addressed by both code and tests.
   - [ ] **Edge Cases**: Edge cases from the spec are handled or documented as deferred.
4. Compile findings and message the **lead**.

**Rules:**
- This is a **compliance** review, not a quality review. You don't care about code style — only spec adherence.
- Each finding must reference the specific spec section that is violated or unaddressed.
- Severity: `MUST FIX` (spec violation) / `CONCERN` (ambiguous spec area).
- Scope creep is always `MUST FIX`.
- Do NOT rewrite code — only report findings.

#### Teammate: Security-Reviewer

**Role:** Review security, data integrity, and architectural fitness.

**Tasks:**
1. Read the diff of ALL changed files.
2. Review against this checklist:
   - [ ] **Injection**: SQL injection, XSS, command injection, template injection
   - [ ] **Access control**: Permission checks in place? Unauthorized users blocked?
   - [ ] **Data validation**: Input validated at boundaries (API endpoints, form handlers, file uploads)
   - [ ] **Auth**: No bypasses introduced, no `sudo()` without justification
   - [ ] **Sensitive data**: No secrets, tokens, or PII logged or exposed
   - [ ] **CSRF/CORS**: If web endpoints changed, protections maintained?
   - [ ] **Architecture**: Respects existing patterns? Appropriate coupling? Correct dependency direction?
   - [ ] **Regression risk**: Could this break existing functionality in affected modules?
   - [ ] **Error leakage**: Do error responses expose internal details to end users?
3. If Playwright or E2E framework is available — suggest smoke tests for affected functionality.
4. Compile findings and message the **lead**.

**Rules:**
- Each finding: file, line, vulnerability type, impact, suggested fix.
- Severity: `CRITICAL` (exploitable vulnerability) / `MUST FIX` (security weakness) / `ADVISORY` (defense-in-depth).
- `CRITICAL` findings must include an attack scenario.
- Do NOT rewrite code — only report findings.

---

### Phase 3: Fix Iterations (lead-orchestrated)

1. **Consolidate**: Collect all reviewer findings. Build two lists:
   - **Coder fixes**: production code findings from Code-Reviewer, Spec-Auditor, Security-Reviewer
   - **Tester fixes**: test findings from Test-Reviewer and Spec-Auditor (coverage gaps)

2. **Dispatch**: Send one consolidated message to Coder, one to Tester (only if they have items). Include severity and source reviewer for each item.

3. **Fix round**: Coder and Tester work in parallel.
   - If Coder's fixes change API/behavior — Coder messages Tester directly so tests can adapt.
   - Both message lead when done. Tester re-runs all tests.

4. **Targeted re-review**: Send updated diff ONLY to reviewers who had `MUST FIX` or `CRITICAL` findings. They check ONLY their own previously raised issues.
   - Response: `PASS` (all resolved) or list of remaining issues.

5. **Maximum 3 iterations.** After that:
   - `CRITICAL` security → lead attempts one more targeted fix.
   - All other remaining items → document in **Known Concerns** with full detail.

**Conflict resolution priority:** Security CRITICAL > Spec compliance > Code quality > Style nits.

---

## Finalization (Lead)

After the team finishes (run all finalization steps inside the worktree directory when `auto_branch = true`):

1. Append sections from `~/.claude/templates/sdd/implementation-sections.md` to the spec file:
   - **Implementation Summary**: what was done, key decisions, what was deferred
   - **Known Concerns**: potential issues, tech debt, unresolved review findings (include reviewer name, severity, description for each)
   - **Auto-Review Results**: test results, criteria coverage, Playwright results, regressions, summary of findings by reviewer
   - **Steps for Manual Review**: 3-7 concrete steps for human verification.
     Format: `N. [Action] → [Expected result]`
     These steps must be detailed enough to follow a week later without remembering context.

2. Update frontmatter:
   - `status: review`
   - `completed: {TODAY}`
   - `branch: task/{ID}-{slug}` (only if `auto_branch = true`; otherwise set to current branch name)
   - `updated: {TODAY}`

3. Move file from `tasks/4-in-progress/` to `tasks/5-review/`.

4. Git commit: `feat({ID}): {title}`

5. If `auto_branch = true`: remove the worktree with `wt remove task/{ID}-{slug}`.

6. Output:
   - Implementation Summary (brief)
   - Known Concerns (if any)
   - Steps for Manual Review (full list)
   - Instruction: "Walk through the manual review steps. If everything looks good — `/task-done {ID}`"

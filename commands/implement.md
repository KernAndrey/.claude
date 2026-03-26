Implement an approved specification using an agent team.

## Instructions

1. Read `.tasks.toml`, `CLAUDE.md`, and project structure.
2. Find the spec by `$ARGUMENTS` (ID or slug) in `tasks/3-ready/`.
3. Read the full specification.
4. Branch and worktree setup:
   - If `auto_branch = true`: create an isolated worktree using the `wt` script: `wt create task/{ID}-{slug}`. All implementation work (Coder and Reviewer agents) MUST run inside the worktree directory. The worktree path is returned by `wt create` — pass it to agents as their working directory.
   - If `auto_branch = false`: stay on the current branch, no worktree.
5. Move spec file to `tasks/4-in-progress/`. Update `status: in-progress`.

## Agent Team

Create an agent team (not subagents):

### Teammate: Coder

**Role:** Implement code strictly according to the specification.

**Tasks:**
1. Read the spec thoroughly: Objective, Scope, Behavior, Acceptance Criteria.
2. Study the Affected Areas described in the spec — explore the relevant parts of the codebase to understand current behavior and find the specific files/classes to change.
3. Implement following the described Behavior:
   - Write code incrementally — one logical block at a time
   - Verify no syntax errors after each block
   - Write tests for EACH acceptance criterion
4. Run tests, ensure they pass.
5. Message Reviewer that implementation is ready for review.

**Rules:**
- Stay strictly within Scope. If tempted to "fix something nearby" — DON'T. Note it in Known Concerns instead.
- Match the project's existing code style and conventions.
- Every acceptance criterion must have a corresponding test.
- When in doubt — check the spec, do not assume.

### Teammate: Reviewer

**Role:** Code reviewer and QA — verify implementation quality against the spec.

**Tasks:**
1. Wait for Coder's ready signal.
2. Conduct code review:
   - Read the diff of all changed files
   - Check each acceptance criterion — is it covered?
   - Run tests independently, verify they pass
   - Check for: N+1 queries, unused imports, hardcoded values, missing error handling
   - Check for: security issues (SQL injection, XSS, access rights)
   - If Playwright is available in the project — run smoke tests on affected functionality
   - Check for regressions in affected modules
3. If issues found:
   - Send a concrete list of problems to Coder directly (via team messaging)
   - Coder fixes → Reviewer re-checks
   - **Maximum 2 iterations.** After that — document remaining issues in Known Concerns.
4. After successful review — confirm to the lead.

**Rules:**
- You are reviewing SOMEONE ELSE's code — be objective, do not rationalize.
- Each issue must be specific: file, line, what's wrong, how to fix.
- Do NOT rewrite code yourself — only give instructions to Coder.

## Finalization (Lead)

After the team finishes (run all finalization steps inside the worktree directory when `auto_branch = true`):

1. Append sections from `~/.claude/templates/sdd/implementation-sections.md` to the spec file:
   - **Implementation Summary**: what was done, key decisions, what was deferred
   - **Known Concerns**: potential issues, tech debt, unresolved questions
   - **Auto-Review Results**: test results, criteria coverage, Playwright results, regressions
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

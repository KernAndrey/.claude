# Claude Code Review Runner

Run SDD reviewers as Claude Code teammates with full team coordination, messaging, and watchdog support.

## Launching reviewers (Phase 2)

Spawn all as teammates (`team_name: "impl-{ID}"`) and send each their task:

- **Code-Reviewer** (`name: "code-reviewer"`)
- **Test-Reviewer** (`name: "test-reviewer"`)
- **Spec-Auditor** (`name: "spec-auditor"`)
- **Security-Reviewer** (`name: "security-reviewer"`)
- **UI-Reviewer** (`name: "ui-reviewer"`) *(only if frontend files changed)*

Each reviewer message:

> Read your instructions: `~/.claude/agents/{agent-name}.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Base branch for diff: `{base_branch}`
> **Review prompts:** if `.claude/review_prompt.md` exists, read it — it contains project-specific review rules (severity overrides, design decisions to treat as intentional). Apply them during your review.
> Report findings to me using the format from your agent file.
> **Heartbeat:** every 10 min of work OR 5 items audited, send a one-line `PROGRESS: [just audited] → [auditing next]`. If blocked for more than 5 min, send `BLOCKED: [reason]`. Silent idling is not acceptable — you are watchdogged on 10-min intervals.

UI-Reviewer gets additional fields:

> Changed files: {combined changed files from all coders}
> URL hints: {any relevant URLs or pages you can identify from the spec}

## UI-Reviewer troubleshooting

If UI-Reviewer reports `VERDICT: BLOCKED` (cannot start dev server, browser unavailable):
- Kill and spawn a replacement with a troubleshooting hint (check port, install deps, try alternative start command).
- Retry up to **3 times**, each with a different hint.
- After 3 failed attempts: document reason in Known Concerns, add manual UI check to Steps for Manual Review, and continue.

## Monitoring

Track which reviewers have reported. If any goes idle without reporting — send a status check.

Phase 2 is complete when ALL spawned reviewers have reported with a valid DEPTH block.

## Re-review after fixes (Phase 3 Step 3)

Message existing reviewers who had `MUST FIX` or `CRITICAL` findings:

> This is a **re-review** after fixes.
>
> **Primary:** verify each of your previous MUST FIX / CRITICAL items is resolved.
> **Secondary (mandatory):** fixes may have introduced new issues in the modified files. Run your full audit procedure again on those files. Treat new methods, new error paths, and regressions in previously-clean code as in scope.
>
> Report `PASS` only if BOTH primary items are resolved AND the secondary pass finds nothing new. Otherwise list all outstanding issues.

If a reviewer is unresponsive after 1 status check — spawn a replacement with the same instructions (primary verification + full secondary audit).

# OpenCode Review Runner

Run SDD reviewers as `opencode run --pure` subprocesses via GitHub Copilot. Stateless — each invocation is a clean session with no plugins.

## Launching reviewers (Phase 2)

Spawn all reviewers in parallel using `Bash(run_in_background=true)`. Each reviewer gets the same command template — substitute `{agent-name}` for: `code-reviewer`, `test-reviewer`, `spec-auditor`, `security-reviewer`, and `ui-reviewer` (when frontend files changed).

```bash
opencode run --pure \
  --model github-copilot/claude-sonnet-4.6 \
  --format json \
  "Read ~/.claude/agents/{agent-name}.md — follow these instructions exactly.
Spec file: {spec_path}
Working directory: {worktree_path}
Base branch for diff: {base_branch}
Run your full audit procedure. Output your report with REVIEWER, VERDICT, DEPTH, FINDINGS, SUMMARY."
```

Timeout: 10 minutes per reviewer. On timeout or empty output — re-run once. Second failure — log as "reviewer unavailable" in Known Concerns.

## Parsing output

`--format json` emits newline-delimited JSON events. Extract review text:

```python
for line in stdout.splitlines():
    event = json.loads(line)
    if event.get("type") == "text":
        parts.append(event["part"]["text"])
review = "\n".join(parts)
```

## Validation

Same as claudecode mode: DEPTH block required, plausible audit counts. Invalid report — re-run that reviewer.

Phase 2 is complete when all reports are collected and validated.

## Re-review after fixes (Phase 3 Step 3)

Reviewers are stateless — spawn fresh instances with the same command. Append context about previous findings:

```
Read ~/.claude/agents/{agent-name}.md — follow these instructions exactly.
Spec file: {spec_path}
Working directory: {worktree_path}
Base branch for diff: {base_branch}
Run your full audit procedure.

Previous review found these MUST FIX issues:
{list of findings with file:line}
Fixes have been applied. Verify these are resolved and check for regressions.
```

Lead spot-checks fixes directly (Read/Grep affected lines) before spawning. Skip re-review for trivially confirmed fixes.

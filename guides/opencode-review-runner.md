# OpenCode Review Runner

Run SDD reviewers as `opencode run --pure` subprocesses via GitHub Copilot. Stateless — each invocation is a clean session with no plugins.

## Launching reviewers (Phase 2)

Launch all reviewers in parallel. Each reviewer writes output to a temp file so the lead can collect results after all finish.

Substitute `{agent-name}` for: `code-reviewer`, `test-reviewer`, `spec-auditor`, `security-reviewer`, and `ui-reviewer` (when frontend files changed).

For each reviewer, run via `Bash(run_in_background=true)`:

```bash
opencode run --pure \
  --model github-copilot/claude-sonnet-4.6 \
  --format json \
  "Read ~/.claude/agents/{agent-name}.md — follow these instructions exactly.
Spec file: {spec_path}
Working directory: {worktree_path}
Base branch for diff: {base_branch}
Run your full audit procedure. Output your report with REVIEWER, VERDICT, DEPTH, FINDINGS, SUMMARY." \
  > /tmp/review-{agent-name}.json 2>&1
```

Wait for all background tasks to complete. Then read each output file and parse.

Timeout: 10 minutes per reviewer. On timeout or empty output — re-run once. Second failure — log as "reviewer unavailable" in Known Concerns.

## Parsing output

Read each `/tmp/review-{agent-name}.json`. Extract review text from JSON events:

```python
for line in open(output_file):
    event = json.loads(line)
    if event.get("type") == "text":
        parts.append(event["part"]["text"])
review = "\n".join(parts)
```

## Validation

Same as claudecode mode: DEPTH block required, plausible audit counts. Invalid report — re-run that reviewer.

Phase 2 is complete when all reports are collected and validated.

## Re-review after fixes (Phase 3 Step 3)

Re-review uses **gpt-5.4** — a different model with different blind spots than the Phase 2 sonnet reviewers. Fresh eyes on fresh code.

Spawn fresh instances with the same command but switch the model. Append context about previous findings:

```bash
opencode run --pure \
  --model github-copilot/gpt-5.4 \
  --format json \
  "Read ~/.claude/agents/{agent-name}.md — follow these instructions exactly.
Spec file: {spec_path}
Working directory: {worktree_path}
Base branch for diff: {base_branch}
Run your full audit procedure.

Previous review found these MUST FIX issues:
{list of findings with file:line}
Fixes have been applied. Verify these are resolved and check for regressions." \
  > /tmp/review-{agent-name}-rereview.json 2>&1
```

Lead spot-checks fixes directly (Read/Grep affected lines) before spawning. Skip re-review for trivially confirmed fixes.

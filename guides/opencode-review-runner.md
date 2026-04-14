# OpenCode Review Runner

Run SDD reviewers as `opencode run --pure` subprocesses via GitHub Copilot. Stateless — each invocation is a clean session with no plugins.

## Launching reviewers (Phase 2)

Launch all reviewers in parallel. Each reviewer writes stdout to a unique temp file; stderr goes to a separate file to avoid corrupting JSON output.

Substitute `{agent-name}` for: `code-reviewer`, `test-reviewer`, `spec-auditor`, `security-reviewer`, `ui-reviewer` (when frontend files changed), and `spec-critic-arch` (during `/spec` runs).

**Path note:** `opencode --pure` rejects paths outside the project directory. For agents under `~/.claude/agents/`, use the project-local symlink `.claude/agents-global/{agent-name}.md` instead. If the symlink doesn't exist, read the agent file with `cat ~/.claude/agents/{agent-name}.md` and inline the full content into the prompt, replacing the `Read ...` line with the actual instructions.

### Symlink setup

Ensure symlinks exist before launching. Run once per project (or in `/task-init`):

```bash
mkdir -p .claude/agents-global
for agent in code-reviewer test-reviewer spec-auditor security-reviewer ui-reviewer spec-critic-arch spec-critic-business; do
  [ -f ~/.claude/agents/$agent.md ] && ln -sf ~/.claude/agents/$agent.md .claude/agents-global/$agent.md
done
```

Create a shared output directory before spawning:

```bash
REVIEW_DIR=$(mktemp -d /tmp/review-{task-id}-XXXXXX)
```

For each reviewer, run via `Bash(run_in_background=true)`:

```bash
opencode run --pure \
  --model github-copilot/claude-sonnet-4.6 \
  --format json \
  "Read .claude/agents-global/{agent-name}.md — follow these instructions exactly.
Spec file: {spec_path}
Working directory: {worktree_path}
Base branch for diff: {base_branch}
Run your full audit procedure. Output your report with REVIEWER, VERDICT, DEPTH, FINDINGS, SUMMARY." \
  > "$REVIEW_DIR/{agent-name}.json" 2> "$REVIEW_DIR/{agent-name}.stderr"
```

Wait for all background tasks to complete. Then read each output file and parse.

Timeout: 10 minutes per reviewer. On timeout or empty output — re-run once. Second failure — log as "reviewer unavailable" in Known Concerns.

## Parsing output

Read each `$REVIEW_DIR/{agent-name}.json`. Extract review text from JSON events, skipping malformed lines:

```python
for line in open(output_file):
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
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
  "Read .claude/agents-global/{agent-name}.md — follow these instructions exactly.
Spec file: {spec_path}
Working directory: {worktree_path}
Base branch for diff: {base_branch}
Run your full audit procedure.

Previous review found these MUST FIX issues:
{list of findings with file:line}
Fixes have been applied. Verify these are resolved and check for regressions." \
  > "$REVIEW_DIR/{agent-name}-rereview.json" 2> "$REVIEW_DIR/{agent-name}-rereview.stderr"
```

Lead spot-checks fixes directly (Read/Grep affected lines) before spawning. Skip re-review for trivially confirmed fixes.

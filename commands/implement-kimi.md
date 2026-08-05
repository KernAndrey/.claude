Implement an approved specification using Kimi CLI for code & tests, with the standard SDD reviewers.

Sibling of `/implement`. The flow is identical except: all production code and test code is written by the Kimi CLI via the kimi skill (`~/.claude/skills/kimi/SKILL.md`), not by Coder/Tester agents. Reviewers are Claude Code background agents — same agents, same fix loop as `/implement`.

Begin by saying to the user: **"I will run kimi for code and tests, then spawn the standard reviewers as background agents. I am the lead — I dispatch kimi runs, monitor them, and coordinate reviews. I do not code or review."**

<critical>
Three rules govern everything below:
1. Every kimi invocation uses the kimi skill spawn block (`~/.claude/skills/kimi/SKILL.md` §Procedure) — always background, always via `build_context.py`.
2. Every spawned kimi run is watchdogged from spawn to `=== KIMI_DONE` (see §Watchdog). Passive waiting masks silent crashes.
3. Reviewers and the fix loop follow `/implement.md` exactly — same agents, same severity rules, same iteration cap. Record the `agentId` from every reviewer spawn; a completed reviewer is reachable only by `agentId`, not by name.
</critical>

## Quality mandate

Thoroughness over speed. A single kimi run takes 20–60 minutes (longer for big scopes); the whole task may run for hours — expected. Every phase completes; no skipping.

## Setup

1. Read `.tasks.toml`, `CLAUDE.md`, and project structure. Several `.tasks.toml` in the repo (root plus `*/.tasks.toml`, `*/*/.tasks.toml`) means several SDD roots — use the one whose `id_prefix` matches the task ID. `{dir}` below is that config's `dir`, resolved relative to the config's own directory.
2. Find the spec by `$ARGUMENTS` (ID or slug) in `{dir}/3-ready/`. `$ARGUMENTS` is just the task identifier.
3. Read the full specification.
4. Branch and worktree setup:
   - If `auto_branch = true`: fetch latest `dev` (`git fetch origin dev`), then `wt create task/{ID}-{slug} --base origin/dev`. Set `{worktree_path}` to the path returned by `wt create`. All kimi runs and reviewers operate inside the worktree.
   - If `auto_branch = false`: stay on the current branch. Set `{worktree_path}` to the current project root.
5. **Review prompt setup:** if the project has `.claude/review_prompt.md`, reviewers will apply it as project-specific rules. Note its path to pass to reviewers.
6. Move spec to `{dir}/4-in-progress/`. Update `status: in-progress`.
7. Note the **base branch** for diffs: `dev` if `auto_branch = true`, otherwise the current branch. Reviewers need it.
8. **Scan project rules** (run inside `{worktree_path}`): `ls .claude/rules/*.md 2>/dev/null` and remember the filename list. You will match these against each kimi scope below. If the directory is empty/missing, note "no project rules directory" — `build_context.py` will still inject `CLAUDE.md`.
9. **Detect project type** for rule heuristics: presence of `__manifest__.py` anywhere → Odoo project; `package.json` with `react`/`vue`/`svelte` → frontend project; etc. Used in the rule-matching table below.
10. **Initialize a reviewer registry** (`name | agentId | role`). You append a row per reviewer spawn in Phase 2. Kimi runs are NOT agents — they are background bash processes tracked separately by the watchdog.

## Kimi invocation

You are the dispatcher: build the task body from the templates below, fire the spawn block in background, watchdog-poll until `=== KIMI_DONE`, read the log, verify, route the result.

Spawn block (use verbatim, substitute `{purpose}` and the task body):

```bash
LOG=/tmp/kimi-impl-{ID}-{purpose}-$$.log
PROJECT_ROOT="{worktree_path}"
CTX=$(mktemp /tmp/kimi-ctx-XXXXXX.md)
TASK=$(mktemp /tmp/kimi-task-XXXXXX.md)
~/.kimi/lib/build_context.py "$PROJECT_ROOT" > "$CTX"
cat > "$TASK" <<'PARENT_TASK_EOF'
{task body — see templates below}
PARENT_TASK_EOF
cd "$PROJECT_ROOT"
FULL=$(cat "$CTX" "$TASK")
echo "=== KIMI_START $(date -Iseconds) project=$PROJECT_ROOT log=$LOG purpose=impl-{ID}-{purpose} ==="
kimi -m kimi-code/k3 -p "$FULL" > "$LOG" 2>&1
RC=$?
rm -f "$CTX" "$TASK"
echo "=== KIMI_DONE rc=$RC log=$LOG ==="
```

`{purpose}` = semantic label like `coder1`, `tester`, `fix-r3-coder1`, `final-tests`. Never a timestamp — an hour later you need `kimi-impl-T42-coder1-1234.log` to be readable, not `kimi-1777652445.log`.

### Rule highlighting (per kimi run)

Before composing each kimi prompt, pick which rules from the scanned `.claude/rules/*.md` to highlight, based on the file extensions in the run's scope:

| File pattern in scope | Highlight rules whose filenames match |
|---|---|
| `*.py` (Odoo project) | `odoo*.md`, `python*.md` |
| `*.py` (non-Odoo) | `python*.md` |
| `*.js`, `*.jsx`, `*.ts`, `*.tsx` | `react*.md`, `frontend*.md`, `typescript*.md`, `js*.md` |
| `*.xml`, `*.qweb`, `*.html` | `view*.md`, `template*.md`, `xml*.md` |
| `*.css`, `*.scss`, `*.less` | `style*.md`, `css*.md` |
| Tester runs | `test*.md` (always) + language-specific from above |

Matched filenames go verbatim into the kimi prompt under "Project rules to apply". Unmatched rules are still loaded by `build_context.py`. If no match — say so and rely on `CLAUDE.md`.

## Watchdog & respawn protocol

Background bash gives no idle notifications: silent crashes, TTY hangs, 429s, and OS kills produce no signal on their own. The watchdog is the only safety net.

On spawn, record `{purpose, log_path, bash_shell_id, spawned_at, strikes: 0, retries: 0}`. Then loop:

1. Tick every 5 minutes. Each tick:
   - **Liveness** — `BashOutput` on the shell. If it returned `=== KIMI_DONE rc=N`, exit the watchdog.
   - **Activity** — `stat -c '%Y %s' "$LOG"`. Compare to the previous tick. Kimi streams stdout throughout, so growing size or advancing mtime = work in progress.

2. Classify the tick:
   - Log growing AND shell alive → working. Continue.
   - Log static AND shell alive → suspect stall. Add 1 strike. `tail -50 "$LOG"`.
   - Shell exited but no `KIMI_DONE` line → crash. Skip strikes, go to step 4.

3. Strike escalation (consecutive static ticks, shell alive):
   - Strike 1 (~5 min static) — log it. Kimi may be deep in a single thought.
   - Strike 2 (~10 min static) — `tail -100 "$LOG"`. If kimi is waiting on a tool, in a retry loop, or repeating a line → respawn. Otherwise one more grace tick.
   - Strike 3 (~15 min static) — force respawn regardless of log content.

4. Respawn procedure:
   - Find the kimi PID via the bash shell, `kill -TERM <pid>`; if alive after 10 s, `kill -KILL <pid>`.
   - Read full `$LOG` to extract partial work.
   - `git status` inside the worktree to see what kimi actually wrote.
   - Spawn a replacement kimi run with a sharper prompt that includes:
     - Original scope.
     - "Work already done by previous run: {modified files}" — so kimi does not redo it blindly.
     - "Previous run got stuck on: <last 10 lines of log>" — asks kimi to take a different approach.
   - Counts toward the retry budget.

5. Crash recovery (rc≠0, OR rc=0 but no `CODER DONE.`/`TESTER DONE.` marker in stdout):
   - `rc=0` no marker → read full log; if work is done, mark DONE manually; if incomplete, respawn with "complete the remaining: …".
   - `rc=429` → rate limit. Pause all kimi runs ~60 s, then resume serially (no parallelism during recovery).
   - `rc=139` (SIGSEGV) → respawn once; if it segfaults again, escalate to user with the `~/.kimi/logs/kimi.log` reference.
   - Other `rc≠0` → respawn with a sharper prompt.

6. Retry budget — max 3 respawns per scope. After 3, escalate to user: "kimi failed/stalled 3× on {scope}. (A) lead writes manually, (B) abort task back to `{dir}/3-ready/`."

7. Loopers — if successive respawns hit the same error, the prompt is wrong. Re-read the spec section, possibly split the scope, then respawn with a different framing.

For parallel kimi runs, one watchdog tick covers all active runs — do not serialize the polling.

## Phase 1a: Code (kimi runs)

### Read the Architect's Work breakdown

The spec's `## Architecture & Implementation Plan → Work breakdown → Coders` subsection is authoritative. The Architect already decided how many coder scopes there are and which files each owns. Fire one kimi run per listed Coder scope — do not re-analyze the spec for parallelization.

### Sanity check (lead, ~30 seconds)

Same as `/implement`: union of `files:` lists matches "Files to create/modify"? Paths real? Scopes coherent? If broken — stop, report, ask whether to (a) patch the breakdown manually, or (b) send the spec back to `{dir}/2-spec/`. If (b): move the spec back, reset `status: awaiting-approval`, and remove the worktree (no reviewers spawned yet at this phase).

### Fire kimi runs from the breakdown

For each Coder scope, build this task body:

```
# Task: implement {ID} — {coder name from Work breakdown}

## Spec
Read first: {spec_path}
Focus on: Objective, Behavior, Acceptance Criteria, Affected Areas.

## Your scope (from spec → Work breakdown → {coder name})
{verbatim scope text from spec}

## Files you own
{verbatim files: list from spec}

## Files you must NOT touch
Other files in the spec belong to other parallel kimi runs — do not modify them.

## Working directory
{worktree_path}

## Project rules to apply (read carefully — these override defaults)
- {matched rule filenames from heuristic table, e.g. .claude/rules/odoo.md, .claude/rules/python.md}
(Other rules from .claude/rules/ are in the prepended context; the above are the most relevant for your scope.)

## Constraints
- Match existing project code style.
- Python: full type annotations on every parameter, return, *args, **kwargs. `from __future__ import annotations`.
- Stay strictly within scope. If you spot something out of scope, mention it as `KNOWN CONCERN: …` in your final output instead of fixing it.
- Do not write tests — that is a separate kimi run.

## Acceptance
- All files in your scope updated to satisfy the spec.
- End your output with the literal line `CODER DONE.` followed by a bullet list of changed files with one-line descriptions.
```

Fire each via the spawn block. Parallelism: up to 3–4 concurrent runs (kimi skill cap — rate limit + RAM). Work breakdown guarantees disjoint file sets, so parallel writes are safe. If 5+ scopes — batch them.

Start watchdog timers for every run. Wait for all `=== KIMI_DONE` markers via `BashOutput` polling.

For each finished run: read `$LOG`, check `rc=0`, grep for `CODER DONE.`, then `git status` inside the worktree to confirm files in the scope were actually modified. Hallucinated success (DONE marker but no file changes) → respawn with "the files were not modified — please write to disk this time".

Phase 1a is complete when every kimi run has produced `CODER DONE.` and verifiable file changes. Combine all changed-file lists into `{combined_changed_files}` for Phase 1b.

## Phase 1b: Test (one kimi run)

Say: **"Coder runs done. Firing one kimi run to write tests. I will route any production bugs back as fresh kimi runs."**

Start only after Phase 1a is complete.

Always exactly one Tester kimi run — parallel testers conflict on shared test infrastructure (DBs, fixtures, ports).

Build this task body:

```
# Task: write tests for {ID}

## Spec
Read first: {spec_path}
Focus on: Acceptance Criteria, Edge Cases & Risks.

## Implementation
The following files were just changed by the coder runs:
{combined_changed_files}
Read them to understand what to test.

## Working directory
{worktree_path}

## Project rules to apply
- All `test*.md` rules from `.claude/rules/`
- {language-specific test rules matched by extension of changed files}

## Constraints
- Discover existing test conventions from the project's test directory before writing (framework, naming, fixtures, helpers).
- Every Acceptance Criterion gets ≥1 test.
- Add edge-case tests from the Edge Cases & Risks section.
- Tests must be isolated — no inter-test state dependencies.
- Test behavior, not declarative artifacts: skip static markup, declarative configuration, and pure view/template structure (UI-Reviewer's domain). If a spec AC is purely visual with no testable behavior, skip it and explain why in your output.
- All test functions and fixtures must have complete type annotations.
- Run all tests after writing them.
- If a test fails because of a production-code bug — DO NOT fix the production code. End your output with `PRODUCTION BUG FOUND.` followed by file path, function/method, expected vs actual behavior, and the failing test name.

## Acceptance
- All tests pass, OR a production bug is reported as above.
- End with `TESTER DONE.` followed by:
  - test files created/changed,
  - total test count,
  - one-line coverage summary against the ACs (which ACs covered, any gaps).
```

Fire via the spawn block. Start the watchdog. Wait for `=== KIMI_DONE`.

### If the Tester run reports `PRODUCTION BUG FOUND.`

1. From the bug report's file path, look up the owning Coder via Work breakdown's `files:` lists.
2. Fire a fresh kimi run scoped to that fix:

```
# Task: fix production bug found during testing — {ID}

## Spec
{spec_path}

## Bug report from tester
{verbatim PRODUCTION BUG FOUND block}

## Your scope
File: {path}
Fix the described bug. Do not touch unrelated code. Do not modify tests.

## Files you may modify
{owning Coder's files: list from Work breakdown}

## Working directory
{worktree_path}

## Project rules to apply
- {matched rules}

## Acceptance
- Bug fixed. End with `CODER FIX APPLIED.` and a one-line description of the change.
```

3. Wait for `=== KIMI_DONE`, verify with `git diff`.
4. Fire a re-test kimi run (scope: re-run affected tests, no new test writing). Passing → continue. Still failing → loop.
5. Maximum 7 bug-fix rounds. After 7 — lead investigates directly: read failing test, read prod code, fix manually.

Phase 1b is complete when the tester run has emitted `TESTER DONE.` with all tests passing.

## Phase 2: Review (dual-track — native Claude reviewers + Kimi mirrors)

Say: **"Kimi finished code and tests. Spawning the reviewers in parallel — each dimension gets a native Claude reviewer AND a Kimi mirror running the same procedure, for two independent passes. I will wait for all reports before proceeding."**

Phase 2 mirrors `/implement` exactly — same agents, same dual-track (native + Kimi mirror per dimension), same DEPTH-block rule, same merge/dedup, same background-agent spawning. It runs after Phase 1 regardless of time spent or perceived code quality; hooks, linters, CI, and prior review rounds do not substitute.

**Two engines per dimension.** For every native reviewer below except the UI-Reviewer, also spawn a `Kimi-Mirror` running the *same* audit procedure on the Kimi CLI. The UI-Reviewer has no Kimi mirror (Kimi cannot drive a browser).

Start only after Phase 1b is complete.

### Determine if UI review is needed

Same rules as `/implement`: if any changed file matches `.xml`/`.html`/`.css`/`.scss`/`.less`/`.js`/`.jsx`/`.ts`/`.tsx`/`.vue`/`.svelte`/`.qweb`/`.mako`/`.jinja2` — spawn UI-Reviewer. Pure-backend changes (`.py`/`.sql`/`.json` config) → skip.

### Reviewer list

Each row is a **pair**: the native Claude reviewer and its Kimi mirror (`Kimi-Mirror` subagent_type).

| Dimension | Native subagent_type | Native name | Kimi-Mirror name | Kimi `MIRROR_OF` / `PURPOSE` |
|---|---|---|---|---|
| production code quality | `Code-Reviewer` | `code-reviewer` | `kimi-code` | `~/.claude/agents/code-reviewer.md` / `review-code` |
| test quality and coverage | `Test-Reviewer` | `test-reviewer` | `kimi-test` | `~/.claude/agents/test-reviewer.md` / `review-test` |
| spec compliance | `Spec-Auditor` | `spec-auditor` | `kimi-spec-audit` | `~/.claude/agents/spec-auditor.md` / `review-spec-audit` |
| security and architecture | `Security-Reviewer` | `security-reviewer` | `kimi-security` | `~/.claude/agents/security-reviewer.md` / `review-security` |
| visual verification *(only if frontend files changed)* | `UI-Reviewer` | `ui-reviewer` | — *(no Kimi mirror — browser)* | — |

### Spawn reviewers in parallel

Spawn **all natives and all mirrors in one batch** (multiple `Agent` calls in a single response). Record each `agentId` in your reviewer registry — one row per native and one per mirror.

Each **native** spawn uses:

```
Agent(
  subagent_type: "{type}",
  name: "{name}",
  run_in_background: true,
  prompt: "Read your instructions: ~/.claude/agents/{agent-file}.md
Spec file: {spec_path}
Working directory: {worktree_path}
Base branch for diff: {base_branch}
Review prompts: if `.claude/review_prompt.md` exists, read it — project-specific review rules (severity overrides, design decisions to treat as intentional). Apply them during your review.
Report findings in the format from your agent file."
)
```

Each **Kimi mirror** spawn (one per native reviewer except UI-Reviewer) uses:

```
Agent(
  subagent_type: "Kimi-Mirror",
  name: "{kimi-name}",
  run_in_background: true,
  prompt: "Read your instructions: ~/.claude/agents/kimi-mirror.md
MIRROR_OF: {native agent file, e.g. ~/.claude/agents/security-reviewer.md}
PURPOSE: {review-code | review-test | review-spec-audit | review-security}
Working directory (WORKTREE): {worktree_path}
Base branch for diff: {base_branch}
Spec file: {spec_path}
Review prompts: if `.claude/review_prompt.md` exists, tell Kimi to read and apply it.
Mirror the native procedure exactly and relay Kimi's report verbatim, with ` (Kimi)` appended to the REVIEWER line."
)
```

**Kimi concurrency.** The mirrors each fire a background Kimi run; 4 at once may exceed Kimi's rate cap (`rc=429`). `~/.claude/templates/kimi-reviewer.md` already waits for the next quota window and re-fires, so the mirrors serialize — non-blocking, not a failure.

UI-Reviewer gets two extra lines in its prompt (no Kimi mirror for it):

> Changed files: {combined_changed_files}
> URL hints: {any relevant URLs or pages you can identify from the spec}

The completion notification is your done signal — do not poll for it. A dead reviewer sends no notification: whenever you await reviewer reports (initial pass and re-review rounds), keep a dead-man timer armed per `~/.claude/templates/liveness-protocol.md` — `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_REVIEW")`, audit on every wake-up, ping by `agentId` first, respawn as escalation. Each reviewer reports with the standard `REVIEWER:`/`VERDICT:`/`DEPTH:`/`FINDINGS:`/`SUMMARY:` block; mirror reports carry ` (Kimi)` in the `REVIEWER:` line. Reject reports without a DEPTH block — native or mirror — and re-run that reviewer (resume by `agentId` or spawn a fresh instance). Same rule if DEPTH counts look implausibly low for the diff. A Kimi mirror returning `KIMI_SUSPICIOUS` / `KIMI_FAILED` is re-run the same way.

**Merge the two passes per dimension.** You hold up to two reports per dimension (native + mirror). Dedup — same `file:line` + same issue class = one finding, keep the more specific description. Union of severity — a `MUST FIX` / `CRITICAL` from *either* engine counts and feeds a single fix round. Divergence is signal: keep whatever one engine catches that the other missed.

If UI-Reviewer reports `VERDICT: BLOCKED` (cannot start dev server, browser unavailable): spawn a replacement with a troubleshooting hint (check port, install deps, alternative start command). Retry up to 3 times. After 3 failures: document the reason in Known Concerns, add a manual UI check to Steps for Manual Review, and continue.

Phase 2 is complete when every spawned agent — each native reviewer AND each Kimi mirror — has reported with a valid DEPTH block.

## Phase 3: Fix & Verify (lead-orchestrated, kimi-driven fixes)

Say: **"All reviewers reported. I will fire kimi runs to apply MUST FIX items, then re-review."**

Precondition: All spawned Phase 2 reviewers must have reported.

### Step 1: Assess

Work from the **merged, deduped** findings (Phase 2 "Merge the two passes"). A `(Kimi)` mirror report routes to the same bucket as its native. Build two fix lists:
- **Code fixes**: `MUST FIX` / `CRITICAL` from Code-Reviewer, Spec-Auditor, Security-Reviewer, UI-Reviewer — and their Kimi mirrors.
- **Test fixes**: `MUST FIX` from Test-Reviewer + missing-coverage items from Spec-Auditor — and their Kimi mirrors.

If zero `MUST FIX` / `CRITICAL` across all reviewers (native and mirror) — skip to Finalization.

Conflict resolution priority: Security CRITICAL > Spec compliance > Code quality.

### Step 2: Fix round (kimi runs)

Group code fixes by owning Coder scope (file → coder mapping from Work breakdown). For each affected scope, fire a kimi run:

```
# Task: fix-round {N} for {ID} — {coder name}

## Spec
{spec_path}

## Findings to fix (from reviewers)
For each item: severity, source reviewer, file:line, description, suggested fix if provided.
{verbatim findings list scoped to this coder's files}

## Files you may modify
{owning Coder's files: list}

## Working directory
{worktree_path}

## Project rules to apply
- {matched rules}

## Constraints
- Address every listed finding. Do not introduce unrelated changes.
- If a finding is misguided or already addressed elsewhere — explain why instead of fixing blindly, in your output.

## Acceptance
- All listed findings addressed. End with `CODER FIX ROUND DONE.` and a checklist mapping each finding → fix description (or "skipped: <reason>").
- If any API or behavior changed in a way that affects tests, mention it explicitly.
```

Fire in parallel where scopes are disjoint. Watchdog as usual.

If any code-fix kimi run reports API/behavior changes — pass them to the test-fix kimi run prompt below.

For test fixes, fire one kimi run:

```
# Task: fix-round {N} for {ID} — tests

## Spec
{spec_path}

## Findings to fix
{verbatim test-related findings list}

## API/behavior changes from this round (if any)
{from coder fix runs}

## Working directory
{worktree_path}

## Project rules to apply
- All `test*.md` rules + language-specific test rules.

## Acceptance
- All listed findings addressed.
- Re-run the full test suite. End with `TESTER FIX ROUND DONE.` and the test results.
```

### Step 3: Verification (re-review)

Resume — by `agentId` — every reviewer **and every Kimi mirror** that had `MUST FIX` or `CRITICAL` findings (a resumed native remembers its findings via preserved context; a resumed mirror fires a fresh Kimi re-review run per `kimi-mirror.md` §Re-review):

> This is a **re-review** after fixes.
>
> **Primary:** verify each of your previous MUST FIX / CRITICAL items is resolved.
> **Secondary (mandatory):** fixes may have introduced new issues in the modified files. Run your full audit procedure again on those files. Treat new methods, new error paths, and regressions in previously-clean code as in scope.
>
> Report `PASS` only if BOTH the primary items are resolved AND the secondary pass finds nothing new. Otherwise list all outstanding issues.

The lead spot-checks fixes directly (Read/Grep affected lines) before re-review. Skip re-review for trivially confirmed fixes. If a reviewer's `agentId` is unresponsive after one status check — spawn a fresh instance with the same instructions.

### Step 4: Fix loop and escalation

If any reviewer returned non-PASS → repeat Steps 2–3. Max 7 iterations.

If the same finding persists for 2 consecutive iterations → lead investigates directly (read code, fix manually).

After 7 iterations with findings unresolved:
- Lead takes over: read, diagnose, fix directly.
- If lead cannot fix — ask user: "These findings remain after 7 fix rounds and my own attempt. Options:
  (A) Continue to manual review — remaining issues documented in Known Concerns.
  (B) Abort — return spec to `{dir}/3-ready/` with findings attached as implementation notes."
- If (B): revert worktree changes, move spec back.

## Finalization (Lead)

Say: **"Fix rounds complete. Running gate check, final test suite, then committing via the `commit` skill (must pass its AI review) and pushing."**

### Gate check

- Phase 1a — every Coder scope from Work breakdown has produced `CODER DONE.` with verified file changes? If NO → fire/respawn the missing scope NOW.
- Phase 1b — Tester kimi run has produced `TESTER DONE.` with passing tests? If NO → fire/respawn NOW.
- Phase 2 — Code-Reviewer **and** its `kimi-code` mirror reported with DEPTH block? If NO → spawn/re-run the missing one NOW.
- Phase 2 — Test-Reviewer **and** `kimi-test` reported with DEPTH block? If NO → spawn/re-run NOW.
- Phase 2 — Spec-Auditor **and** `kimi-spec-audit` reported with DEPTH block? If NO → spawn/re-run NOW.
- Phase 2 — Security-Reviewer **and** `kimi-security` reported with DEPTH block? If NO → spawn/re-run NOW.
- Phase 2 — UI-Reviewer reported with DEPTH block (if spawned; no Kimi mirror)? If NO → spawn/re-run NOW.
- Phase 3 — Fix iterations completed (or no MUST FIX items)? If NO → run NOW.

### Final test run

Run the project's full test suite directly inside the worktree (`cd {worktree_path} && {project_test_command}`) — for the final run, kimi adds no value over a direct command.

All tests pass → proceed to Steps. Tests fail → back to Phase 3 Step 2 for one more fix round.

### Steps

Run inside the worktree directory when `auto_branch = true`:

1. Append sections from `~/.claude/templates/sdd/implementation-sections.md` to the spec file:
   - **Implementation Summary**: what was done, key decisions, note that code and tests were authored by Kimi.
   - **Known Concerns**: unresolved findings (reviewer name, severity, description for each).
   - **Auto-Review Results**: test results, criteria coverage, verbatim VERDICT and SUMMARY from each spawned reviewer.
   - **Steps for Manual Review**: 3–7 concrete steps. Format: `N. [Action] → [Expected result]`.

2. Update frontmatter:
   - `status: review`, `completed: {TODAY}`, `updated: {TODAY}`
   - `branch: task/{ID}-{slug}` (if `auto_branch = true`; otherwise current branch)

3. Move file from `{dir}/4-in-progress/` to `{dir}/5-review/`.

4. Commit via the `commit` skill — mandatory. Invoke `Skill` with `skill: commit` for every commit; never call raw `git commit`. The skill owns: security scan, test-coverage preflight, pre-commit hooks (gitleaks/semgrep), AI-review handling, post-commit WARNINGs review, and the 3000-line diff cap.

   Split changes into logical commits — group cohesive units (each feature chunk with its tests, config separately, etc.). Conventional commit messages prefixed with the task ID:
   - `feat({ID}): add order model with validation and tests`
   - `feat({ID}): add order API endpoints and tests`
   - `chore({ID}): update config for order module`

   The commit skill's AI review is a hard gate — every commit must pass. If the review BLOCKS:
   - Read the review findings carefully.
   - For production-code findings → fire a fresh kimi fix run (Phase 3 Step 2 template), then re-invoke the commit skill.
   - For test findings → fresh kimi tester fix run, then re-invoke.
   - For trivial findings (typos, comments, formatting) the lead may fix directly, then re-invoke.
   - Never bypass the review (`--no-verify`, `git commit -n`, etc.). If the review keeps blocking after 3 fix attempts, escalate to user with the verbatim review output.

5. If `auto_branch = true`: `git push -u origin task/{ID}-{slug}` (inside worktree).

6. If `auto_branch = true`: `wt remove task/{ID}-{slug}`.

7. Background agents complete on their own — no shutdown step needed.

8. Output:
   - Implementation Summary (brief)
   - Known Concerns (if any)
   - Steps for Manual Review (full list)
   - Instruction: "Walk through the manual review steps. If everything looks good — `/task-done {ID}`"

## Compact instructions

This command can run for several hours. On compaction, preserve:
- `{ID}`, `{spec_path}`, `{worktree_path}`, `{base_branch}`.
- For each active kimi run: `{purpose, log_path, bash_shell_id, strikes, retries}`.
- The reviewer registry (`name | agentId | role`) — needed to resume completed reviewers in Phase 3.
- Per-phase status: which Coder scopes are DONE, whether Tester is DONE, which reviewers reported (with verbatim VERDICT/SUMMARY), current Phase 3 iteration count.
- Combined `{combined_changed_files}` once Phase 1a is complete.
- Last failing test output, if any.
- Outstanding `MUST FIX` / `CRITICAL` items not yet routed to a fix run.

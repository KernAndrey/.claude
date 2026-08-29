Implement an approved specification using addressable background agents.

All agents in this workflow are spawned via the `Agent` tool with `run_in_background: true` and a `name`. You are the lead — you coordinate, you don't code or review.

Begin by saying to the user: **"I will spawn background agents to implement this spec. I am the lead — I coordinate, I don't code or review."**

<critical>
Record the `agentId` (format `a...-...`) returned by every `Agent` spawn into your registry. A `name` addresses an agent **only while it is running**; once an agent completes, it is reachable only by `agentId`. Coders and the Tester complete before Phase 3 fix rounds — so you resume them there by `agentId`. Lose the id and the fix loop silently breaks.
</critical>

## Coordination model

- **Spawn** with `Agent(subagent_type: "...", name: "...", run_in_background: true, prompt: "...")`. The call returns immediately with an `agentId`; the agent runs asynchronously.
- **Completion** arrives as a notification carrying the agent's final message. The notification is your done signal — you do not poll for it.
- **Address a running agent** by `name`: `SendMessage(to: "coder-1", ...)`.
- **Resume a completed agent** by `agentId`: `SendMessage(to: "{agentId}", ...)` — its context is preserved, so it remembers its prior work.
- **Done conventions** (`CODER DONE.`, `TESTER DONE.`, `REVIEWER: ...`) are a *content* convention for the final message so you can parse role and result. The completion notification is the *detection* mechanism.

### Agent registry

Maintain a small table in your working context, one row per spawn:

```
name              | agentId      | role                  | files_owned
coder-1           | a1b2-...     | Coder                 | models/order.py, ...
tester            | a3c4-...     | Tester                | tests/
code-reviewer     | a5d6-...     | Code-Reviewer         | —
adaptive-rollback | a7e8-...     | Adaptive-Reviewer     | —
```

Append a row immediately after each spawn. This table is how you reach completed agents in later phases. In Phase 2 each fixed dimension gets one row, plus one row per adaptive lens.

### Liveness protocol

A dead agent (transient API error) sends no completion notification, and nothing else wakes you — the old "check after ~15 minutes" backstop never fired because your turn had already ended. Read and follow `~/.claude/templates/liveness-protocol.md`: keep a dead-man timer armed whenever you await agent messages, audit the registry on every wake-up, and recover missing agents — ping by `agentId` first (usually sufficient), fresh respawn as escalation. 3 recovery cycles per agent; no cap in AFK mode. Each phase below names its watchdog.

## Quality mandate

Thoroughness over speed. This task may run for hours — that is expected and acceptable. Every phase completes fully. Each phase exists for a reason that automated tools (hooks, linters, CI) cannot replace.

## Setup

This command does **not** use `~/.claude/templates/sdd/board-root.md`, and `{dir}` stays repo-relative throughout. The board rule inverts here: once a worktree exists, the task file travels with the code inside it, because its move into `4-in-progress` and `5-review` must be captured by the commit.

1. Read `.tasks.toml`, `CLAUDE.md`, and project structure. Several `.tasks.toml` in the repo (root plus `*/.tasks.toml`, `*/*/.tasks.toml`, skipping `node_modules/`, `.git/`, `vendor/` and plugin/cache directories) means several SDD roots — use the one whose `id_prefix` matches the task ID. `{dir}` below is that config's `dir`, resolved relative to the config's own directory.
2. Find the spec by `$ARGUMENTS` (ID or slug) in `{dir}/3-ready/`. `$ARGUMENTS` is just the task identifier.
3. Read the full specification.
4. Branch and worktree setup:
   - **Preflight, before creating anything** (`auto_branch = true` only): `git fetch origin dev`, then confirm the spec is on the base branch:

     ```
     git cat-file -e origin/dev:{dir}/3-ready/{ID}-{slug}.md
     ```

     This ref takes the **repo-relative** `{dir}`. A non-zero exit means the board change never reached `origin/dev`, so the worktree would be cut without the spec in it. Report `Spec {ID} is not on origin/dev — commit and push the board first, then re-run /implement {ID}.` and stop. Checking here, rather than after `wt create`, leaves no worktree to tear down.
   - If `auto_branch = true`: `wt create task/{ID}-{slug} --base origin/dev`. Set `{worktree_path}` to the path returned by `wt create`. All agents work inside the worktree directory.
   - If `auto_branch = false`: stay on the current branch. Set `{worktree_path}` to the current project root directory.
   - From here on every board path is `{worktree_path}/{dir}/…`, resolved inside the worktree — including the moves in step 6 and Phase 4.
5. **Review prompt setup:** if the project has `.claude/review_prompt.md`, reviewers will apply it as project-specific rules (severity overrides, design decisions to treat as intentional). Note its path to pass to reviewers.
6. Move spec to `{worktree_path}/{dir}/4-in-progress/`. Update `status: in-progress`.
7. Note the **base branch** for diffs: `dev` if `auto_branch = true`, otherwise the current branch. Reviewers will need it.
8. Initialize an empty agent registry (`name | agentId | role | files_owned`). You append a row per spawn.

## Agents — sequential phases

Each agent reads its agent file for full instructions.
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

If the breakdown is broken (gaps, overlaps, nonsense scopes): **do not silently fix it**. Stop, report the issue to the user, and ask whether to (a) patch the breakdown manually before continuing, or (b) send the spec back to `{dir}/2-spec/` for the Architect to redo. If (b): move the spec file back from `{dir}/4-in-progress/` to `{dir}/2-spec/`, reset frontmatter `status` from `in-progress` to `awaiting-approval`, remove the worktree if one was created (`wt remove task/{ID}-{slug}`), and stop any agents already spawned. The Critic should have caught this — flag it as a Critic miss too.

#### Spawn Coders from the breakdown

For each Coder listed in Work breakdown, spawn it as a background agent and record its `agentId`:

```
Agent(
  subagent_type: "Coder",
  name: "coder-N",
  run_in_background: true,
  prompt: "Read your instructions: ~/.claude/agents/coder.md
Spec file: {spec_path}
Working directory: {worktree_path}
Your scope (from spec → Work breakdown → coder-N): {scope text from spec}
Files you own: {files list from spec}
Do not touch any other files in the spec — they belong to other coders.
Implement your scope. Your final message must be `CODER DONE.` with the list of changed files."
)
```

For single-coder tasks (one entry in Work breakdown), the spawn is the same — just one agent, scope and file list copied from the spec.

Right after the spawn wave, arm the phase watchdog: `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_CODE")` (liveness protocol). `TaskStop` it when the phase completes.

**Phase 1a is complete when every Coder has sent its completion notification with `CODER DONE.`** and a changed-files list.

---

### Phase 1b: Test

Say: **"Coders are done. Spawning Tester to write tests. I will coordinate bug fixes between them if needed."**

Start only after Phase 1a is complete.

There is always exactly **one** Tester, regardless of how many Coders ran. Parallel testers are intentionally excluded — they conflict on shared test infrastructure (DBs, fixtures, ports). One Tester sees all the code and writes tests for the full implementation.

Spawn the Tester as a background agent and record its `agentId`:

```
Agent(
  subagent_type: "Tester",
  name: "tester",
  run_in_background: true,
  prompt: "Read your instructions: ~/.claude/agents/tester.md
Spec file: {spec_path}
Working directory: {worktree_path}
Coding is done. Changed files: {combined changed files from all coders}
Write tests for the implementation. Your final message must be `TESTER DONE.` with the test count and results.
If you find a production bug, message me with `PRODUCTION BUG FOUND` and details, including the affected file path so I can route the fix to the right coder."
)
```

Arm the phase watchdog: `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_TEST")`. Keep it armed through bug-fix rounds — you are awaiting agent messages the whole time.

If the Tester reports `PRODUCTION BUG FOUND`:
- Map the affected file → owning Coder via the registry's `files_owned`. Resume that Coder by `agentId` with the bug details.
- Wait for the Coder's `CODER FIX APPLIED` message.
- Resume the Tester by `agentId` to re-run affected tests.
- Repeat until all bugs resolved.
- Maximum **7 bug-fix rounds**. If bugs persist after 7 rounds — lead investigates directly: read the failing test, read the production code, diagnose and fix.

**Phase 1b is complete when the Tester sends `TESTER DONE.`** with test count and results.

---

### Phase 2: Review (fixed dimensions + adaptive lenses)

Say: **"Code and tests are done. Spawning the reviewers in parallel — one per fixed dimension, plus the adaptive lenses I designed for this diff. I will wait for all reports before proceeding."**

This phase runs after Phase 1 regardless of time spent or code quality. All reviewers must report. Hooks, automated linters, CI checks, or prior review rounds do not substitute for Phase 2.

**Plus angles only this diff needs.** The fixed dimensions are the ones every change gets. Design 2–4 more for this one (see *Adaptive lens design* below).

**Start only after Phase 1b is complete.**

#### Determine if UI review is needed

Check the changed files list from the Coders. If ANY file matches a frontend pattern — spawn the UI-Reviewer:
- `.xml`, `.html`, `.css`, `.scss`, `.less` — always
- `.js`, `.jsx`, `.ts`, `.tsx`, `.vue`, `.svelte` — always
- `.qweb`, `.mako`, `.jinja2` — template files

If all changes are purely backend (`.py`, `.sql`, config `.json`) — skip UI-Reviewer.

#### Adaptive lens design (think before you spawn)

The five dimensions below are what every change gets. What they cannot cover is the angle *this* diff needs — that depends on what the code actually does, and it is yours to work out.

1. **Read the diff and the spec's Behavior first.** You cannot name the right angle for a change you have not looked at.
2. **Choose 2–4 lenses**, scaled by the diff: 2 for a single-file change, 4 when it spans modules or touches data migration, concurrent access, permissions, or external integrations.
3. **Write each lens** as four fields — `lens-id`, `angle` (the stance in one line), `justification` (why *this* diff needs it, citing a concrete `file:line` or spec behavior), `hunt` (the failure classes it should surface).
4. **Prefer system-level angles.** Non-exhaustive seeds: attacker's eyes, existing production data, concurrent actions, operations and observability, performance at real scale, permissions and multi-tenancy, failure and rollback. Do not restate a fixed dimension — a long method belongs to Code-Reviewer, a missing test or an untested failure path to Test-Reviewer, spec drift to Spec-Auditor, a plain injection bug to Security-Reviewer.
5. **Announce** the chosen lenses with one line of rationale each, then spawn. This is your call — do not wait for approval.

#### Reviewer list

| Dimension | subagent_type | name |
|---|---|---|
| production code quality | `Code-Reviewer` | `code-reviewer` |
| AC coverage matrix and test quality | `Test-Reviewer` | `test-reviewer` |
| spec compliance | `Spec-Auditor` | `spec-auditor` |
| security and architecture | `Security-Reviewer` | `security-reviewer` |
| visual verification *(only if frontend files changed)* | `UI-Reviewer` | `ui-reviewer` |
| *this diff's angle* — one row per lens from *Adaptive lens design* | `Adaptive-Reviewer` | `adaptive-{lens-id}` |

#### Spawn reviewers in parallel

Spawn **every reviewer in one batch** (multiple `Agent` calls in a single response). Record each `agentId`.

Each **fixed dimension** spawn uses:

```
Agent(
  subagent_type: "{type}",
  name: "{name}",
  run_in_background: true,
  prompt: "Read your instructions: ~/.claude/agents/{agent-file}.md
Spec file: {spec_path}
Working directory: {worktree_path}
Base branch for diff: {base_branch}
Review prompts: if `.claude/review_prompt.md` exists, read it — it contains project-specific review rules (severity overrides, design decisions to treat as intentional). Apply them during your review.
Report findings in the format from your agent file."
)
```

Each **adaptive lens** spawn (one per lens from *Adaptive lens design*) uses:

```
Agent(
  subagent_type: "Adaptive-Reviewer",
  name: "adaptive-{lens-id}",
  run_in_background: true,
  prompt: "Read your instructions: ~/.claude/agents/adaptive-reviewer.md
LENS_ID: {lens-id}
LENS_ANGLE: {angle}
LENS_JUSTIFICATION: {why this diff needs it}
LENS_HUNT: {failure classes to surface}
Spec file: {spec_path}
Working directory: {worktree_path}
Base branch for diff: {base_branch}
Review prompts: if `.claude/review_prompt.md` exists, read it and apply its rules.
Report in the format from your agent file."
)
```

Arm the phase watchdog: `Bash(run_in_background: true, command: "sleep 1500; echo WATCHDOG_REVIEW")`. The adaptive rows push this batch past the concurrency cap, so later spawns queue for slots — a 900s timer over the whole batch fires on healthy-but-queued agents, and the respawns it triggers make the contention worse.

Do not cancel a reviewer for being slow — a queued agent is healthy; the watchdog covers genuine stalls.

UI-Reviewer gets two extra lines in its prompt:

> Changed files: {combined changed files from all coders}
> URL hints: {any relevant URLs or pages you can identify from the spec}

#### Report format

Each reviewer reports in this format (defined in its agent file):
```
REVIEWER: {role}
VERDICT: CLEAN/SECURE/COMPLIANT | HAS FINDINGS

DEPTH:
- {items audited: count, list or summary — format varies by role}
- {additional depth fields specific to the reviewer}

FINDINGS: ...
SUMMARY: X findings (Y MUST FIX, Z NIT/CONCERN)
```

**Reject reports without a DEPTH block** — this applies to fixed reviewers and adaptive lenses alike. The DEPTH counts are how you detect shallow reviews. If a reviewer reports `VERDICT` and `FINDINGS` but omits `DEPTH`, re-run that reviewer. Same rule if counts look implausibly low for the diff (e.g. "Methods audited: 2" on a 20-method diff). To re-run, resume the reviewer by `agentId` and ask for the missing DEPTH block, or spawn a fresh instance.

Test-Reviewer carries one extra requirement: its report includes the **AC coverage matrix**, one row per AC in the spec, with the success, failure, and boundary tests named. A report with no matrix, or a matrix covering fewer ACs than the spec has, is rejected the same way a missing DEPTH block is.

#### Merge the reports

You now hold one report per fixed dimension plus one per adaptive lens. Merge them all before acting:

- **Dedup**: same `file:line` + same issue class = **one** finding. Keep the more specific description. Dedup across adaptive lenses and fixed dimensions alike.
- **Union of severity**: a `MUST FIX` / `CRITICAL` raised by *any* reviewer counts — another reviewer missing it does not downgrade it. The merged MUST FIX / CRITICAL set feeds a **single** fix round in Phase 3.
- **Divergence is signal, not noise**: if one reviewer flags something the others missed, keep it. That extra catch is the whole point of running several angles.

#### UI-Reviewer troubleshooting

If UI-Reviewer reports `VERDICT: BLOCKED` (cannot start dev server, browser unavailable):
- Spawn a replacement with a troubleshooting hint (check port, install deps, try alternative start command).
- Retry up to **3 times**, each with a different hint.
- After 3 failed attempts: document the reason in Known Concerns, add a manual UI check to Steps for Manual Review, and continue.

**Phase 2 is complete when every spawned agent — each fixed reviewer and each adaptive lens — has reported with a valid DEPTH block.**

---

### Phase 3: Fix & Verify (lead-orchestrated)

Say: **"All reviewers reported. I will now orchestrate fix rounds — sending findings to Coder and Tester, then re-reviewing until all MUST FIX items are resolved."**

Precondition: All spawned Phase 2 reviewers must have reported.

#### Step 1: Assess

Work from the **merged, deduped** findings (Phase 2 "Merge the reports"). Build two fix lists:
- **Coder fixes**: `MUST FIX` / `CRITICAL` findings from Code-Reviewer, Spec-Auditor, Security-Reviewer, UI-Reviewer
- **Tester fixes**: `MUST FIX` findings from Test-Reviewer, missing coverage from Spec-Auditor. One exception: a finding the reviewer marked as a spec-flagged `Uncovered:` gap goes to Known Concerns and the user escalation path instead — the spec already recorded that the behavior is undefined, so fix rounds cannot close it and would spend all 7 iterations trying.
- **Adaptive lens findings** join whichever list matches the fix: a production-code failure goes to the Coder, an untested path to the Tester. Route by what the fix touches, not by which lens raised it.

If zero `MUST FIX` / `CRITICAL` across all reviewers — skip to Finalization.

**Conflict resolution priority:** Security CRITICAL > Spec compliance > Code quality.

#### Step 2: Fix round

Group production fixes by owning Coder (use the registry's `files_owned` to map file → coder). Resume each affected Coder **by its `agentId`** — these Coders completed in Phase 1a, so `name` no longer reaches them:

> These findings need to be fixed. For each item: severity, source reviewer, file:line, description.
> After fixing, your message must be `CODER FIX ROUND DONE.` Include a note if any API or behavior changed.

If any Coder reports API/behavior changes — forward those to the Tester.

Resume the Tester by `agentId` with all test fixes (if any):

> These test findings need to be fixed. For each item: severity, source reviewer, test file, description.
> Re-run all tests after fixes. Then your message must be `TESTER FIX ROUND DONE.`

Fix rounds await agent messages like any phase — arm `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_FIX")` per round.

#### Step 3: Verification (re-review)

Resume — by `agentId` — every reviewer **and every adaptive lens** that had `MUST FIX` or `CRITICAL` findings (a resumed reviewer remembers its findings via preserved context). An adaptive lens verifies its own findings — nobody else holds that angle:

> This is a **re-review** after fixes.
>
> **Primary:** verify each of your previous MUST FIX / CRITICAL items is resolved.
> **Secondary (mandatory):** fixes may have introduced new issues in the modified files. Run your full audit procedure again on those files. Treat new methods, new error paths, and regressions in previously-clean code as in scope.
>
> Report `PASS` only if BOTH the primary items are resolved AND the secondary pass finds nothing new. Otherwise list all outstanding issues.

The lead spot-checks fixes directly (Read/Grep affected lines) before re-review. Skip re-review for trivially confirmed fixes. If a reviewer's `agentId` is unresponsive after one status check — spawn a fresh instance with the same instructions (primary verification + full secondary audit).

#### Step 4: Fix loop and escalation

If any reviewer returned non-PASS — repeat Steps 2-3. Maximum **7 iterations**.

If the SAME finding persists unfixed for 2 consecutive iterations — lead investigates directly and fixes it.

After 7 iterations with findings still unresolved:
- Lead takes over: read the code, diagnose, and fix the remaining issues directly.
- If lead cannot fix — **ask user**: "These findings remain after 7 fix rounds and my own attempt. Options:
  (A) Continue to manual review — remaining issues documented in Known Concerns.
  (B) Abort — return spec to `{dir}/3-ready/` with findings attached as implementation notes."
- If user picks B: revert worktree changes, move spec back.

---

## Finalization (Lead)

Say: **"Fix rounds complete. Running gate check, final test suite, then committing and pushing."**

### Gate check — verify before continuing:

- Phase 1a — **every** Coder from Work breakdown sent `CODER DONE`? If NO → resume the missing one(s) by `agentId` NOW.
- Phase 1b — Tester sent `TESTER DONE` with test count? If NO → resume Tester NOW.
- Phase 2 — Code-Reviewer reported with DEPTH? If NO → spawn NOW.
- Phase 2 — Test-Reviewer reported, with a matrix row per AC? If NO → spawn NOW.
- Phase 2 — Spec-Auditor reported? If NO → spawn NOW.
- Phase 2 — Security-Reviewer reported? If NO → spawn NOW.
- Phase 2 — UI-Reviewer reported? (only if spawned) If NO → spawn NOW.
- Phase 2 — every adaptive lens reported with DEPTH? If NO → spawn NOW.
- Phase 3 — Fix iterations completed (or no MUST FIX items)? If NO → run NOW.

### Final test run

Resume the Tester by `agentId`: "Run the full test suite and report results."
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

3. Move file from `{dir}/4-in-progress/` to `{dir}/5-review/`.

4. Git commit — split changes into logical commits. Group by cohesive unit: each feature chunk together with its tests, config changes separately, etc. Each commit gets a conventional commit message prefixed with the task ID:
   - `feat({ID}): add order model with validation and tests`
   - `feat({ID}): add order API endpoints and tests`
   - `chore({ID}): update config for order module`
   Do not lump all changes into a single commit — logical splitting makes bisect and revert possible.

5. If `auto_branch = true`: `git push -u origin task/{ID}-{slug}` (inside worktree).

6. If `auto_branch = true`: `wt remove task/{ID}-{slug}`.

7. Background agents complete on their own — no shutdown step needed.

8. Output:
   - Implementation Summary (brief)
   - Known Concerns (if any)
   - Steps for Manual Review (full list)
   - Instruction: "Walk through the manual review steps. If everything looks good — `/task-done {ID}`"

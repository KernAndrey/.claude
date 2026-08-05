Implement an approved specification by handing the whole engine to a deterministic Workflow.

You are the **lead**. You do three things only: **set up**, **launch the workflow**, **report the result**. All coding, testing, reviewing, fixing, committing, and pushing happen inside the `sdd-implement-engine` workflow. You do not spawn coders or reviewers yourself, and you do not write code.

Begin by saying: **"Setting up, then handing implementation to the sdd-implement-engine workflow. It runs autonomously to delivery — code, tests, review, fixes, parallel pre-review of the commits, commits through the gate, a landed-commit integrity audit, and push."**

`$ARGUMENTS` is the task identifier (ID or slug).

<critical>
This workflow runs to completion with NO human in the loop. It never pauses to ask a question. Genuinely undecidable items are recorded in the spec's Known Concerns and surfaced to the user at the end. Do not add approval gates.
</critical>

## Phase 1: Setup (lead, with git)

Only the lead may run git here — the workflow script itself has no shell. Do this setup before launching.

1. Read `.tasks.toml`, `CLAUDE.md`, and the project structure. Several `.tasks.toml` in the repo (root plus `*/.tasks.toml`, `*/*/.tasks.toml`) means several SDD roots — use the one whose `id_prefix` matches the task ID. `{dir}` below is that config's `dir`, resolved relative to the config's own directory.
2. Find the spec for `$ARGUMENTS` in `{dir}/3-ready/`. Read it in full.
3. Branch and worktree:
   - `auto_branch = true`: `git fetch origin dev`, then `wt create task/{ID}-{slug} --base origin/dev`. Set `{worktree_path}` to the path it returns. Note `{branch} = task/{ID}-{slug}`, `{base} = dev`.
   - `auto_branch = false`: stay on the current branch. `{worktree_path}` = project root, `{base}` = current branch, `{branch}` = current branch.
4. Move the spec to `{dir}/4-in-progress/` and set frontmatter `status: in-progress`. (The workflow moves it onward to `5-review` at the end.)
5. Note the review-rules path: `{review_prompt} = .claude/review_prompt.md` if it exists, else null.

## Phase 2: Build the Coder list (lead, ~30 seconds)

The spec's `## Architecture & Implementation Plan → Work breakdown → Coders` is authoritative — it already decided how many coders and who owns which files. Translate it into a `coders` array of `{ name, scope, files }`, one entry per coder. Coders own **production files only** — drop any test path (`tests/…`, `*_test.*`, `*.spec.*`, `test_*`, and `tests/__init__.py` registrations) from every coder's `files`. The dedicated Tester writes all tests; the engine also strips test paths defensively, but keep the array production-only so the sanity-check below is meaningful.

Sanity-check before launch:
- Take the union of every coder's `files`. It must equal the **production** files in "Files to create" + "Files to modify" (test files excluded — the Tester owns those).
- File paths must be real (or marked new).

Reconcile small gaps or overlaps yourself and proceed. For anything you genuinely cannot reconcile, add a sentence to `seededConcerns` and continue — do not stop to ask. Reserve stopping for a breakdown that is outright broken (nonsense scopes, most files unaccounted for): in that case move the spec back to `{dir}/2-spec/`, reset `status: awaiting-approval`, `wt remove task/{ID}-{slug}` if created, and report it as a Critic miss.

## Phase 3: Launch the workflow

Call the Workflow tool with the saved engine and the setup values:

```
Workflow({
  name: "sdd-implement-engine",
  args: {
    specPath:        "{worktree_path}/{dir}/4-in-progress/{ID}-{slug}.md",
    worktreePath:    "{worktree_path}",
    baseBranch:      "{base}",
    branchName:      "{branch}",          // task/{ID}-{slug}, or the current branch name
    taskId:          "{ID}",
    coders:          [ { name, scope, files }, ... ],
    reviewPromptPath:"{review_prompt}",   // or null
    autoBranch:      {auto_branch},       // true / false
    seededConcerns:  [ ... ]              // [] if none
  }
})
```

The call returns when the task is delivered. Watch live progress with `/workflows`.

Capture two fields from the returned terminal result/object — you need them for Phase 5 recovery if the run dies: the `runId` and the persisted `scriptPath`. The Workflow tool returns these on the awaited call **even when the run fails**, so save both the moment the call returns, before you branch on success vs death.

## Phase 4: Report (lead)

The workflow already finalized the spec (Implementation Summary, Known Concerns, Auto-Review Results, Steps for Manual Review), moved it to `{dir}/5-review/`, landed the commits through the gate, and pushed the branch. From the returned object, show the user:

1. **Status** — `DELIVERED`, `DELIVERED_WITH_CONCERNS`, or `DELIVERED_INCOMPLETE`. The last one means the final acceptance check could not confirm the spec is fully implemented even after the engine's in-run remediation — call this out prominently and point the user at the acceptance gaps recorded in Known Concerns.
2. **Implementation Summary** (brief) and the **commit list** (subjects).
3. **Known Concerns**, verbatim — these are the items the engine decided autonomously and the user should review.
4. **Steps for Manual Review**, the full list.
5. The instruction: **"Walk through the manual review steps. If everything looks good — `/task-done {ID}`."** (For `DELIVERED_INCOMPLETE`, tell the user the task likely needs another pass before `/task-done`.)

If `status` is missing or the workflow returned an error, read the spec in `{dir}/4-in-progress/` (it may not have moved), report what landed, and tell the user which phase did not complete. When the run returned a death/error rather than a delivered object, go to **Phase 5** before reporting — Phase 5 decides whether to recover or to report-and-stop.

## Phase 5: Recovery on workflow death (lead)

The engine now holds its own long gate-waits, so a healthy run reaches delivery on its own. Phase 5 is the safety-net for a run that dies anyway (a transient agent death, a stale environment, or an engine bug). Reach it only when the Phase-4 awaited `Workflow(...)` call returned a death/error object instead of a delivered result. Recovery is **same-session only**: resume replays already-completed agents from cache and re-runs from the dead step onward, and works only while this session stays alive.

Track an **attempts counter per failure point** (keyed by the phase + step that died). The whole cycle below is bounded: at most **2 resume attempts at the same failure point**, then stop and report.

### Step 1 — Diagnose first (never blind-restart)

Diagnosing before acting prevents re-running a non-transient failure straight back into the same wall. Read, in order, and show the user what you found:

1. The returned error/object from the Phase-4 call (the death's shape and message).
2. The run and agent transcripts (use `/workflows` and the run's transcript) — find the agent and step that died.
3. The working-tree state: `git -C {worktree_path} status --porcelain`, `git -C {worktree_path} log --oneline {base}..HEAD`, and any half-written `.review/` state.
4. The last phase the run reached (from the engine's `phase(...)` log and the transcripts).

Then classify the cause into exactly one of three kinds:

- **Transient** — one agent died or a service errored (e.g. a 529), run otherwise healthy. → Step 2 (resume directly).
- **Fixable** — an environment/orchestration problem: a stuck background process, a half-written review-state file, a stale lock, a stale base needing a fresh fetch/FF-rebase, a wrong launch argument. → Step 2 (fix the cause, then resume). (If the BETA engine-self-patching block below is present, an engine bug you can fix on a well-supported, logged hypothesis is also Fixable.)
- **Unfixable or undecidable** — → Step 3 (stop and report the diagnosis + the failed phase). Also stop here if this failure point has already been attempted twice.

### Step 2 — Fix (if needed) and resume

**Blast-radius of any fix you apply:** environment/orchestration only (plus the engine itself, but ONLY via the BETA block below, and only while that block is present). Never touch the feature/product code the run is implementing — that is the engine's sole responsibility. This never-feature-code limit holds for every fix in this phase, in both regions.

Infra/orchestration fixes you may apply: kill a stuck background process, delete a half-written `.review/` state file or a stale lock, `git fetch` + fast-forward a stale base, correct a launch argument. Apply the minimal fix, then resume.

Resume same-session from the point of death by re-calling the Workflow tool with the captured ids:

```
Workflow({ scriptPath: "{captured scriptPath}", resumeFromRunId: "{captured runId}" })
```

Resume only when you have BOTH `scriptPath` and `runId` from the Phase-4 result. If either is missing from the death object, you cannot resume blind — go to Step 3 and report instead. On resume, completed `agent()` calls with an unchanged prompt+opts return cached results; the run re-executes from the first changed/new/incomplete call.

After resume, increment the attempts counter for this failure point. If the run dies again at the **same** point, re-diagnose (Step 1). Once attempts at one point reach 2 and it fails a third time, stop and report (Step 3) — make no further resume attempt at that point.

<!-- BETA: engine self-patching — remove when the engine is stable -->
### Engine self-patching (BETA — remove when the engine is stable)

This is a deliberately temporary, beta-phase capability. Deleting this single block (between the BETA markers) disables engine self-patching entirely with no other edits; the rest of Phase 5 — diagnosis, infra/orchestration fixes, the resume call, the ≤2-attempt bound, and the report — keeps working without it. It is a tracked sunset item.

You may edit the engine itself — `~/.claude/workflows/implement.js` — when your diagnosis points at an engine bug. The bar is loosened on purpose: a **well-supported, LOGGED hypothesis** is enough; you do NOT need a smoking-gun transcript line, only a stated rationale a reviewer could follow. The safety net is git history — any engine change is revertable, and over-restricting this defeats the beta-test purpose of surfacing problems no one anticipated.

These guards still hold for every engine patch:

1. **Log it verbatim.** Before patching, write your hypothesis (the reasoning) and the exact change into the final report's Known Concerns, verbatim. This is mandatory — an unlogged engine change is forbidden.
2. **Never feature/product code.** The patch touches `implement.js` (or its test) only — never the code the run is implementing.
3. **Stay within the ≤2-attempt bound** at the same failure point.
4. **Account for cache invalidation.** Editing the engine invalidates the resume cache from the first changed `agent()` call onward. Earlier completed agents stay cached; do NOT assume a post-change step is cached.

Then resume per Step 2.
<!-- /BETA -->

### Step 3 — Stop and report

When the cause is unfixable/undecidable, or the 2-attempt bound at a failure point is exhausted, or the death object lacks `scriptPath`/`runId`, stop. Report: which phase failed, your diagnosis, what landed so far (the `git log` from Step 1), and — if you patched the engine — the verbatim recovery note already recorded in Known Concerns.

## What stays with the lead vs the workflow

- **Lead:** read spec, create worktree, move spec to `4-in-progress`, parse the Coder list, launch, report. Nothing else.
- **Workflow:** code → test (+ bug loop) → review → fix (+ re-review) → finalize spec → parallel pre-review of the planned commits (fix loop until clean, ≥2 groups) → land commits through the gate (fast-path past the LLM review for pre-approved diffs; + fix loop, secrets, overrides, chunked manifest) → integrity audit that every landed commit was pre-reviewed → push. Model for every agent is the session model — choose it when you launch this command.

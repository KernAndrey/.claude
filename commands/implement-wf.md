Implement an approved specification by handing the whole engine to a deterministic Workflow.

You are the **lead**. You do three things only: **set up**, **launch the workflow**, **report the result**. All coding, testing, reviewing, fixing, committing, and pushing happen inside the `sdd-implement-engine` workflow. You do not spawn coders or reviewers yourself, and you do not write code.

Begin by saying: **"Setting up, then handing implementation to the sdd-implement-engine workflow. It runs autonomously to delivery — code, tests, review, fixes, parallel pre-review of the commits, commits through the gate, a landed-commit integrity audit, and push."**

`$ARGUMENTS` is the task identifier (ID or slug).

<critical>
This workflow runs to completion with NO human in the loop. It never pauses to ask a question. Genuinely undecidable items are recorded in the spec's Known Concerns and surfaced to the user at the end. Do not add approval gates.
</critical>

## Phase 1: Setup (lead, with git)

Only the lead may run git here — the workflow script itself has no shell. Do this setup before launching.

1. Read `.tasks.toml`, `CLAUDE.md`, and the project structure.
2. Find the spec for `$ARGUMENTS` in `tasks/3-ready/`. Read it in full.
3. Branch and worktree:
   - `auto_branch = true`: `git fetch origin dev`, then `wt create task/{ID}-{slug} --base origin/dev`. Set `{worktree_path}` to the path it returns. Note `{branch} = task/{ID}-{slug}`, `{base} = dev`.
   - `auto_branch = false`: stay on the current branch. `{worktree_path}` = project root, `{base}` = current branch, `{branch}` = current branch.
4. Move the spec to `tasks/4-in-progress/` and set frontmatter `status: in-progress`. (The workflow moves it onward to `5-review` at the end.)
5. Note the review-rules path: `{review_prompt} = .claude/review_prompt.md` if it exists, else null.

## Phase 2: Build the Coder list (lead, ~30 seconds)

The spec's `## Architecture & Implementation Plan → Work breakdown → Coders` is authoritative — it already decided how many coders and who owns which files. Translate it into a `coders` array of `{ name, scope, files }`, one entry per coder.

Sanity-check before launch:
- Take the union of every coder's `files`. It must equal "Files to create" + "Files to modify". 
- File paths must be real (or marked new).

Reconcile small gaps or overlaps yourself and proceed. For anything you genuinely cannot reconcile, add a sentence to `seededConcerns` and continue — do not stop to ask. Reserve stopping for a breakdown that is outright broken (nonsense scopes, most files unaccounted for): in that case move the spec back to `tasks/2-spec/`, reset `status: awaiting-approval`, `wt remove task/{ID}-{slug}` if created, and report it as a Critic miss.

## Phase 3: Launch the workflow

Call the Workflow tool with the saved engine and the setup values:

```
Workflow({
  name: "sdd-implement-engine",
  args: {
    specPath:        "{worktree_path}/tasks/4-in-progress/{ID}-{slug}.md",
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

## Phase 4: Report (lead)

The workflow already finalized the spec (Implementation Summary, Known Concerns, Auto-Review Results, Steps for Manual Review), moved it to `tasks/5-review/`, landed the commits through the gate, and pushed the branch. From the returned object, show the user:

1. **Status** — `DELIVERED`, `DELIVERED_WITH_CONCERNS`, or `DELIVERED_INCOMPLETE`. The last one means the final acceptance check could not confirm the spec is fully implemented even after the engine's in-run remediation — call this out prominently and point the user at the acceptance gaps recorded in Known Concerns.
2. **Implementation Summary** (brief) and the **commit list** (subjects).
3. **Known Concerns**, verbatim — these are the items the engine decided autonomously and the user should review.
4. **Steps for Manual Review**, the full list.
5. The instruction: **"Walk through the manual review steps. If everything looks good — `/task-done {ID}`."** (For `DELIVERED_INCOMPLETE`, tell the user the task likely needs another pass before `/task-done`.)

If `status` is missing or the workflow returned an error, read the spec in `tasks/4-in-progress/` (it may not have moved), report what landed, and tell the user which phase did not complete.

## What stays with the lead vs the workflow

- **Lead:** read spec, create worktree, move spec to `4-in-progress`, parse the Coder list, launch, report. Nothing else.
- **Workflow:** code → test (+ bug loop) → review → fix (+ re-review) → finalize spec → parallel pre-review of the planned commits (fix loop until clean, ≥3 groups) → land commits through the gate (fast-path past the LLM review for pre-approved diffs; + fix loop, secrets, overrides, chunked manifest) → integrity audit that every landed commit was pre-reviewed → push. Model for every agent is the session model — choose it when you launch this command.

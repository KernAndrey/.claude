Inject lightweight task-implementation requirements into the plan being drafted in plan mode.

Invoke this command from plan mode when a task is small enough to skip the full SDD flow (`/spec` → `/implement`) but still needs discipline: clarifying questions, worktree, tests, commit, and review handoff.

Assumes the project is SDD-initialized: `.tasks.toml` exists and `tasks/1-draft` … `tasks/6-done` directories are present. If `.tasks.toml` is missing — tell the user to run `/task-init` first and stop.

## Instructions

You are currently in plan mode. Structure the plan however you normally would — **this command does not dictate the plan's layout**. It only lists requirements the final plan must contain and honour.

### 1. Locate the task

If `$ARGUMENTS` is a task ID or slug — find the file under `tasks/1-draft/`, `tasks/3-ready/`, or `tasks/4-in-progress/`. Otherwise ask via `AskUserQuestion` which task to implement (list up to 4 candidates from `tasks/1-draft/` and `tasks/3-ready/`). Read the task file.

### 2. Clarify before planning

Use `AskUserQuestion` for anything unclear: scope, edge cases, affected files, expected behavior, acceptance criteria. Group 2-4 related questions per call. Do not assume silently — if something is ambiguous, ask.

### 3. Requirements the plan must cover

The plan may be written in any style, but it must include and honour all of the following. If any requirement is not applicable to the specific task, state so explicitly in the plan rather than skipping silently.

- **Worktree isolation.** Work happens in `wt create task/{ID}-{slug} --base origin/dev`; all subsequent steps run inside the returned worktree path.
- **Task lifecycle — in progress.** Move task file to `tasks/4-in-progress/`, update frontmatter `status: in-progress`, `updated: {TODAY}`, `branch: task/{ID}-{slug}` before implementation starts.
- **Tests in the same pass as code.** Every new or modified code path (function, branch, template conditional, user-facing surface) gets a test that asserts on its behavior. Follow the Test discipline section in `~/.claude/CLAUDE.md`. Run tests and paste passing output into the session before committing.
- **Commit via the `commit` skill.** Invoke the `Skill` tool with `skill: commit`. Conventional commit message prefixed with task ID, e.g. `feat({ID}): add order validation`. Split into logical commits if the change spans multiple cohesive units. Commit is mandatory — no step is "done" without it.
- **Task lifecycle — review.** After commit, move task file to `tasks/5-review/`, update frontmatter `status: review`, `updated: {TODAY}`.
- **Stop for human review.** After moving to `tasks/5-review/`, report to the user: branch name, worktree path, commit hashes, and: "Ready for review. After you approve — I will merge to `dev` (`--no-ff`), run `/task-done {ID}`, and remove the worktree." Then stop.
- **Post-review steps (documented, not executed).** The plan should note that after user approval the operator runs: `git checkout dev && git pull` → `git merge --no-ff task/{ID}-{slug} -m "Merge task/{ID}-{slug} into dev"` → `git push origin dev` → `/task-done {ID}` → `wt remove task/{ID}-{slug}`. These are NOT executed as part of this command.

### 4. Mandatory reminders — include verbatim in the plan

<critical>
- The pre-commit AI reviewer BLOCKS commits that add or modify code paths without test coverage. Write tests in the same pass as the code.
- Commit goes through the `commit` skill — not raw `git commit`. The skill handles security scan, logical splitting, and conventional messages.
- Stop after moving the task to `tasks/5-review/`. Do not merge to `dev` and do not move to `tasks/6-done/` until the user confirms review passed.
</critical>

### 5. Finish with ExitPlanMode

Once the plan is written and every requirement above is covered, call `ExitPlanMode` for user approval.

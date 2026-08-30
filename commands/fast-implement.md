Inject lightweight task-implementation requirements into the plan being drafted in plan mode.

Invoke this command from plan mode when a task is small enough to skip the full SDD flow (`/spec` → `/implement`) but still needs discipline: clarifying questions, worktree, tests, commit, auto-merge into `dev`, and a review handoff on `dev`.

Assumes the project is SDD-initialized: `.tasks.toml` exists and `{dir}/1-draft` … `{dir}/6-done` directories are present. If `.tasks.toml` is missing — tell the user to run `/task-init` first and stop.

`{dir}` = the `dir` of the applicable `.tasks.toml`. A repo may carry several SDD roots (root plus `*/.tasks.toml`, `*/*/.tasks.toml`, skipping `node_modules/`, `.git/`, `vendor/` and plugin/cache directories); pick the config whose `id_prefix` matches the task ID, or — when `$ARGUMENTS` is a bare slug — the root whose board actually holds a matching file, asking the user when several do. Resolve `dir` relative to that config's own directory.

## Instructions

You are currently in plan mode. Structure the plan however you normally would — **this command does not dictate the plan's layout**. It only lists requirements the final plan must contain and honour.

### 1. Locate the task

If `$ARGUMENTS` is a task ID or slug — find the file under `{dir}/1-draft/`, `{dir}/3-ready/`, or `{dir}/4-in-progress/`. Otherwise ask via `AskUserQuestion` which task to implement (list up to 4 candidates from `{dir}/1-draft/` and `{dir}/3-ready/`). Read the task file.

### 2. Clarify before planning

**The plan is the spec for this task — an error here is the most expensive one in the flow.** A wrong premise in the plan becomes code, tests, a commit, and a merge into `dev` before anyone looks again. Most plan mistakes trace back to research that stopped one pass too early.

- Research has no budget: read every file the change touches and every call-site that reaches it, follow imports, find ≥2 analogous features. Tool calls, files read, and follow-up passes are not capped.
- Before each question, before writing the plan, and before `ExitPlanMode`, ask yourself: **"Do I have enough context to be right?"** and **"Would one more pass change the answer?"** A "no" or "not sure" on either means research more, not proceed.

<bad_pattern>
❌ BAD THOUGHT: "It's a small task — I've seen the main file, I can plan from here."
✅ REALITY: Small tasks skip `/spec`, so this plan is the only place a wrong premise can be caught before it lands on `dev`.
⚠️ DETECTION: About to write the plan while you can name a call-site or analogous feature you have not opened? → open it first.
</bad_pattern>

**Before any question — the gate.**

0. Check yourself before checking the question. For every candidate question ask: **"Do I have enough context to ask this?"**, **"Is it built on facts or on my guesses?"** (name the `path:line` behind each presupposition — no citation means a guess), **"Would more research remove the question?"** A guess anywhere sends you back to the code until every presupposition is a cited fact or an explicit "the codebase does not cover this — greped X, Y, Z". Only then run steps 1–4.
1. Dig the code first. Find the answer where it lives — models, call-sites, existing conventions — before forming a question.
2. Resolve it yourself when you can. A purely technical point the code answers, or an obvious yes (e.g. "should I do the task at all?"), needs no question — decide and record it as context.
3. Ask only genuine decisions — ones with downstream consequences where a wrong guess causes rework. When unsure which kind it is, ask: a 30-second question beats a silent wrong default.
4. Ask as many as genuinely matter — never pad to a count.

**Language.** Run the QA session in Russian — questions, options, and the +/− trade-offs the user reads and answers. Everything that persists is English — spec sections, plan, code, commit messages, and recorded Decisions/Blockers (translate the gist of the user's Russian answer).

Use `AskUserQuestion` for the decisions that survive the gate: scope, edge cases, affected files, expected behavior, acceptance criteria. Group 2-4 related questions per call; each question in a call has passed step 0 on its own — a batch is not a way to ask several half-researched questions at once. In each `question`, give context legible to someone not living in the task — what it's about, what you found in the code, why the choice matters. In each option's `description`, state that option's trade-offs (+ upside; − downside).

### 3. Requirements the plan must cover

The plan may be written in any style, but it must include and honour all of the following. If any requirement is not applicable to the specific task, state so explicitly in the plan rather than skipping silently.

- **Worktree isolation.** Work happens in `wt create task/{ID}-{slug} --base origin/dev`; all subsequent steps run inside the returned worktree path.
- **The board lives in the worktree here.** This flow does not use `~/.claude/templates/sdd/board-root.md`, and `{dir}` stays repo-relative: once the worktree exists, every board path is `{worktree_path}/{dir}/…`, because the task file's moves must be captured by the commit that carries the code.
- **Task lifecycle — in progress.** Move task file to `{dir}/4-in-progress/`, update frontmatter `status: in-progress`, `updated: {TODAY}`, `branch: task/{ID}-{slug}` before implementation starts.
- **Tests in the same pass as code.** Every new or modified code path (function, branch, template conditional, user-facing surface) gets a test that asserts on its behavior. Follow the Test discipline section in `~/.claude/CLAUDE.md`. Run tests and paste passing output into the session before committing.
- **Task lifecycle — review (before the commit).** Once tests pass, move the task file to `{dir}/5-review/` and update frontmatter `status: review`, `updated: {TODAY}`. Do this **before** committing so the move is captured in the commit — otherwise it is lost when the worktree is removed and never reaches `dev`.
- **Commit via the `commit` skill.** Invoke the `Skill` tool with `skill: commit`. The commit captures the code, the tests, and the task-file move to `{dir}/5-review/`. Conventional commit message prefixed with task ID, e.g. `feat({ID}): add order validation`. Split into logical commits if the change spans multiple cohesive units. Commit is mandatory — no step is "done" without it.
- **Integrate into `dev` (executed automatically after a clean commit).** Run from the **main checkout**, not the worktree — you cannot `git checkout dev` from inside the task worktree, nor `wt remove` a worktree you are standing in. Resolve the main checkout path from the first entry of `git worktree list`, `cd` there, then:
  1. Verify the main checkout is clean (`git status --porcelain` is empty). If dirty — stop and report; do not touch it.
  2. `git checkout dev && git pull`
  3. `git merge --no-ff task/{ID}-{slug} -m "Merge task/{ID}-{slug} into dev"`
  4. `git push origin dev`
  5. `wt remove task/{ID}-{slug}`
  **Remove the worktree only after `git push origin dev` succeeds.** On any failure before that — main checkout dirty, `git pull` fails or diverges, merge conflicts (run `git merge --abort`), or push rejected — leave the worktree and its branch in place, report the exact failure, and stop. Until the push lands, that branch is the only copy of the work. Do **not** run `/task-done` — the task stays in `{dir}/5-review/` for human review on `dev`.
- **Report.** After a successful push and worktree removal, report to the user: commit hashes, "merged into `dev` and pushed to `origin/dev`", "worktree removed", "task is in `{dir}/5-review/`". Close with: "Review the integrated result on `dev`; when satisfied, run `/task-done {ID}`." Then stop.

### 4. Mandatory reminders — include verbatim in the plan

<critical>
- The pre-commit AI reviewer BLOCKS commits that add or modify code paths without test coverage. Write tests in the same pass as the code.
- Commit goes through the `commit` skill — not raw `git commit`. The skill handles security scan, logical splitting, and conventional messages.
- Move the task to `{dir}/5-review/` BEFORE committing — the commit must capture the move, or it is destroyed when the worktree is removed. After committing, merge into `dev` and run `wt remove` only once `git push origin dev` succeeds; on any earlier failure leave the worktree and branch intact and report. Do not run `/task-done` — the task stays in `{dir}/5-review/`.
</critical>

### 5. Finish with ExitPlanMode

Run the two self-check questions from §2 once more against the finished plan; a "not sure" sends you back to the code, not to `ExitPlanMode`.

Once the plan is written, the self-check passes, and every requirement above is covered, call `ExitPlanMode` for user approval.

Inject lightweight task-implementation requirements into the plan being drafted in plan mode — the `/fast-implement` sibling for Odoo projects that ship no tests and end at a commit.

<!-- Sibling of ~/.claude/commands/fast-implement.md. Same clarify-then-plan shape; the differences are that verification replaces tests, the work ends at a commit on a feature branch, and the board is local so nothing about it reaches a commit. Carry fast-implement edits over. -->

Invoke this from plan mode when a task is small enough to skip `/gl-spec` → `/gl-implement` but still needs discipline: clarifying questions, a worktree, verification, and a commit the user then pushes.

## The three rules that shape this plan

**Build it the way Odoo already does.** A derived value is a non-stored compute; uniqueness is a `models.UniqueIndex`; a condition on a field is `@api.constrains`; access is ACL and record rules; what the form offers is the view. Read `{worktree}/.claude/rules/native-odoo-first.md` before planning a single model change.

**Verification replaces tests.** These modules ship no suite (`no-test-suite.md`). Each rule the task states gets a shell probe whose output you paste.

**The work ends at a commit.** Push, PR and merge are the user's. Plan nothing past the commit.

Run the project's `link-tooling.sh` in the worktree, or `.claude/rules/` is absent and none of the above applies.

## 1. Locate the task

If `$ARGUMENTS` is a ticket ID or slug, find the file under `{dir}/1-draft/`, `{dir}/3-ready/` or `{dir}/4-in-progress/`. Otherwise ask via `AskUserQuestion` which task to implement, listing up to 4 candidates. Read it, and read the client ticket it came from if `tasks/context/` holds one — **the client's ticket outranks our task file wherever they differ.**

`{dir}` is the `dir` of the `.tasks.toml` whose `id_prefix` matches the ticket ID, resolved relative to that config's own directory. Missing `.tasks.toml` → tell the user to run `/task-init` and stop.

## 2. Clarify before planning

**The plan is the spec for this task.** A wrong premise here becomes code and a commit before anyone looks again.

- Research has no budget: read every file the change touches, every call-site that reaches it, and ≥2 analogous features already in the repo. That last one matters most here — the reviewer's objection on one task was that the module read as a stranger beside its siblings.
- Before each question and before `ExitPlanMode`, ask: **"Do I have enough context to be right?"** and **"Would one more pass change the answer?"** A "not sure" means research more.

**Before any question — the gate.**

0. For every candidate question ask: do I have enough context to ask this; is it built on facts or guesses (name the `path:line` behind each presupposition — no citation means a guess); would more research remove it? A guess anywhere sends you back to the code.
1. Dig the code first — models, call-sites, existing conventions.
2. Resolve it yourself when the code answers it.
3. Ask only genuine decisions, where a wrong guess causes rework.
4. Ask as many as matter; never pad to a count.

**Language.** Run the QA session in Russian. Everything that persists is English — plan, code, commit message, recorded decisions.

Group 2–4 related questions per `AskUserQuestion` call. Give context legible to someone not living in the task, and state each option's trade-offs (+ upside; − downside).

## 3. Requirements the plan must cover

Write the plan however you like, but it must include and honour all of these. State explicitly where one does not apply rather than skipping it silently.

- **Worktree isolation.** `wt create feature/{base}/{ID}-{slug} --base origin/{base}` — the branch name carries its destination as the segment after `feature/`, so `feature/stage/PROJ-101-end-date` is the branch PR'd into `stage`. `{ID}` is the ticket (`PROJ-101`); `{base}` is the project's day-to-day branch: `stage` on one project, `main` on another. Take the exact shape from the project's `CLAUDE.md`; it is `feature/…` on every one of them, never `task/…`, and the destination is never a suffix. Every later step runs inside the path `wt create` returned, and `link-tooling.sh` runs there first — a fresh worktree carries no `.claude/rules/`, because `git worktree add` brings only tracked files.
- **The board is local.** In these repositories `tasks/` and `.tasks.toml` sit in `.git/info/exclude`, so board moves reach no commit. Move the task file as you work — `4-in-progress/`, then `5-review/` — and do not plan a commit around it.
- **Native Odoo first.** For each rule the task states, name the mechanism that will hold it: compute, constraint, index, ACL, view. A `create`/`write` override, a stored writable computed field, or a Python check restating a database rule each needs a sentence in the plan saying why nothing simpler works.
- **Verification in the same pass as the code.** One probe per rule — the accepted path and every refusal the task states. Run it in a shell against the dev database ending in `env.cr.rollback()`, and paste the printed lines into the session before committing. No `tests/` directory is created; an existing suite is left alone.
- **Server and database hygiene.** Restart the dev server on the new code before probing, and re-run the project's neutralization SQL after any `-u` — a module update reactivates crons.
- **Manifest version bump.** Every module the change touches gets its `version` raised in `__manifest__.py`. odoo.sh updates a module only when the version rises; miss it and the build succeeds while the views, data and security rules in the commit are never applied.
- **Lint.** `ruff check <changed .py>` then the project's pylint wrapper. Paste the output.
- **Commit, then stop.** Subject `[{ID}] <Imperative description>` — `[PROJ-101]` — one line, no body, no attribution, no co-author trailers. The repository's coverage gate asks for tests these modules do not carry, so tell the user the commit needs the verify-skipping flag and let them run it. Split into logical commits when the change spans cohesive units.
- **Report.** Commit hashes, probe output, what you verified and what you could not, and anything you deliberately left out. Close by telling the user the branch is ready to push — and stop.

## 4. Mandatory reminders — include verbatim in the plan

<critical>
- Push, PR and merge belong to the user. The plan ends at a commit on a feature branch.
- Every rule the task states is verified by a probe whose output is pasted before the commit. No test file is written.
- Bump `__manifest__.py` version for every module touched, or the deployment is a silent no-op.
</critical>

<bad_pattern>
❌ BAD THOUGHT: "No tests here, so I'll read the code carefully and report it done."
✅ REALITY: verification moved, it did not disappear. On one task two changes that looked obviously safe by reading broke on the first probe — one raised `UniqueViolation` from two deferred UPDATEs reaching the database in the wrong order.
⚠️ DETECTION: about to finish with no command output in the session? → run the probe.
</bad_pattern>

## 5. Finish with ExitPlanMode

Run the two self-check questions from §2 against the finished plan; a "not sure" sends you back to the code, not to `ExitPlanMode`. Then call `ExitPlanMode` for approval.

Implement an approved specification for an Odoo project — the `/implement` sibling for modules that ship without a test suite and are held to the framework's own mechanisms.

<!-- Sibling of ~/.claude/commands/implement.md. The coordination model, liveness protocol and codex backend are the same; this file changes Phase 1b (no Tester), trims the reviewer set, and adds the native-Odoo gate. Carry implement.md edits over. -->

Begin by saying: **"I will spawn background agents. I am the lead — I coordinate, I don't code or review. This module ships no tests; verification is shell probes and I will show their output."**

<critical>
Record the `agentId` returned by every `Agent` spawn into your registry. A `name` reaches an agent only while it runs; a completed one is reachable only by `agentId`, and the Phase 4 fix rounds resume completed agents. Lose the id and the fix loop silently breaks.
</critical>

## The two rules this command exists to enforce

**Build it the way Odoo already does.** A derived value is a non-stored compute; uniqueness is a `UniqueIndex`; a condition on a field is `@api.constrains`; access is ACL and record rules; what the form offers is the view. Python is for the rule the ticket states. Full rule: `{worktree_path}/.claude/rules/native-odoo-first.md`.

**Verification is a shell probe whose output you paste.** No `tests/` directory is created, and no existing suite is deleted. Full rule: `{worktree_path}/.claude/rules/no-test-suite.md`.

## Coordination model

- **Spawn** with `Agent(subagent_type: "...", name: "...", run_in_background: true, prompt: "...")`. The call returns an `agentId` immediately; the agent runs asynchronously.
- **Completion** arrives as a notification carrying the agent's final message — you do not poll.
- **Address a running agent** by `name`, a completed one by `agentId`.
- **Done conventions** (`CODER DONE.`, `REVIEWER: ...`) let you parse role and result from the final message.

### Agent registry

One row per spawn, appended immediately:

```
name            | agentId    | role              | files_owned
coder-1         | a1b2-...   | Coder             | models/order.py, ...
code-reviewer   | a3c4-...   | Code-Reviewer     | —
lens-native-odoo| a5d6-...   | Adaptive-Reviewer | —
```

### Liveness protocol

Follow `~/.claude/templates/liveness-protocol.md`: keep a dead-man timer armed whenever you await agent messages, audit the registry on every wake-up, ping by `agentId` first, respawn only as escalation. Each phase below names its watchdog.

## Setup

1. Read `.tasks.toml`, `CLAUDE.md`, and `.claude/rules/`. `{dir}` is the SDD root whose `id_prefix` matches the task ID, resolved relative to its own config.
2. **Pick the reviewer backend.** Follow `~/.claude/templates/codex-reviewer.md` §1 (and §2–§4 on `codex`). Ask now — everything after this runs unattended. What remains of `$ARGUMENTS` is the task identifier; find the spec in `{dir}/3-ready/`.
3. Read the full specification, including `## Testing Strategy` — that is your verification plan.
4. **Branch and worktree.** These repositories keep the board in `.git/info/exclude`, so `/implement`'s preflight — confirming the spec is on the base branch before cutting a worktree — does not apply and must not be restored: the spec is never on any remote. Go straight to creating the branch.
   - `auto_branch = true` (the default in these projects): `wt create feature/{base}/{ID}-{slug} --base origin/{base}` — the branch name carries its destination as the segment after `feature/`, so `feature/stage/PROJ-101-price-list-model` is the branch PR'd into `stage`. `{ID}` is the ticket (`PROJ-101`); `{base}` is the project's day-to-day branch — `stage` on one project, `main` on another. Take the exact shape from the project's `CLAUDE.md`; it is `feature/…` on every one of them, never `task/…`, and the destination is never a suffix.
   - `auto_branch = false`: stay on the current branch and set `{worktree_path}` to the project root.
   - Set `{worktree_path}` to the path `wt create` returned. Everything after this runs inside it.
   - Run the project's `link-tooling.sh` there — `git worktree add` brings only tracked files, and the whole tooling pack is hidden from git, so a fresh worktree has no `.claude/rules/`.
5. Move the spec to `{worktree_path}/{dir}/4-in-progress/`, `status: in-progress`. The board is local, so this move reaches no commit — do not plan one around it.
6. Note the base branch for diffs: the `{base}` above.

## Phase 1 — Code

Read the spec's `## Architecture & Implementation Plan → Work breakdown → Coders`. The Architect already decided how many Coders and which files each owns. Do not re-derive it.

**Sanity check (~30 seconds):** the union of the coders' `files:` lists matches "Files to create" + "Files to modify"; paths are real. A broken breakdown goes back to the user — do not silently repair it.

Spawn each Coder and record its `agentId`:

> Read your instructions: `~/.claude/agents/coder.md`
> Spec file: `{spec_path}`
> Working directory: `{worktree_path}`
> Your scope: {scope text from spec}
> Files you own: {files list} — touch nothing else.
> **Read `{worktree_path}/.claude/rules/native-odoo-first.md` and `readable-syntax.md` before writing a line.** Reach for the framework's mechanism first, and write each rule as the plainest statement of it — the reader is the client's developer with the diff and no conversation. Write no `tests/` directory: this module ships without one.
> **Bump the module's `version` in `__manifest__.py`** — the host performs a module update only when it rises, and missing it makes the commit a silent no-op.
> Your final message: `CODER DONE.` with the changed files.

Arm `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_CODE")`; `TaskStop` it when the phase completes.

## Phase 2 — Verify

Say: **"Coders are done. Running the verification plan."**

This phase replaces `/implement`'s Tester. It is not optional: without it nothing distinguishes finished work from work that merely compiles.

<procedure>
1. Restart the dev server so it runs the new code, and re-run the project's neutralization SQL — a module update reactivates crons.
2. Walk `## Testing Strategy` entry by entry. For each AC write the probe: the accepted path and every refusal the entry names.
3. Run the probes in an Odoo shell against the dev database, ending with `env.cr.rollback()`.
4. Paste the actual output lines into your report — not a summary of them.
5. Name every AC whose probe you could not write, and why.
</procedure>

<example>
for label, attempt in cases:
    try:
        with env.cr.savepoint():
            attempt()
        print("%-34s ACCEPTED" % label)
    except Exception as exc:
        print("%-34s %s" % (label, str(exc)[:80]))
env.cr.rollback()
</example>

A probe that passes against code with the rule removed pins nothing. Where a refusal matters, revert the guard in your head and ask whether the probe would still print ACCEPTED.

Arm `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_VERIFY")`.

**Found a bug?** Map the file to its Coder via the registry, resume that Coder by `agentId`, wait for `CODER FIX APPLIED`, re-run the probe. Maximum 7 rounds, then diagnose it yourself.

## Phase 3 — Review

Say: **"Code and verification are done. Spawning reviewers in parallel."**

Start only after Phase 2 prints clean output. Spawn every reviewer in one batch.

| Dimension | subagent_type | name | when |
|---|---|---|---|
| production code quality | `Code-Reviewer` | `code-reviewer` | always |
| spec compliance | `Spec-Auditor` | `spec-auditor` | always |
| security and architecture | `Security-Reviewer` | `security-reviewer` | always |
| native-Odoo conformance | `Adaptive-Reviewer` | `lens-native-odoo` | always |
| visual verification | `UI-Reviewer` | `ui-reviewer` | a changed file is `.xml`, `.js`, `.css` or a template |
| *this diff's angle* | `Adaptive-Reviewer` | `adaptive-{lens-id}` | 1–2, designed by you |

Six to eight reviewers, not thirteen. These are configuration modules; a batch that large queues against the concurrency cap and buys repetition rather than coverage.

**The `lens-native-odoo` brief** — paste it into that spawn:

> LENS_ID: native-odoo
> LENS_ANGLE: Read the diff as the client's Odoo reviewer who asked for less machinery. For each guard, override and stored field, name the framework mechanism that would have done the job, or say why none would.
> LENS_JUSTIFICATION: {cite a concrete file:line in this diff}
> LENS_HUNT: a stored writable computed field plus the constraints that police it; a Python search restating a unique index; a `create`/`write` override that refuses a value no click produces; `check_access()` inside an override that core already checks; an abstract hook with `NotImplementedError` serving one purpose; a helper wrapping a single call; a lock, a busy message, or an empty `write({})`; a comment longer than four lines; a `flush_recordset` with no reproduced failure behind it.
> Rules that bind this diff: `{worktree_path}/.claude/rules/native-odoo-first.md`, `constraints-and-concurrency.md`, `code-comments.md`.
> Report in the format from your agent file.

**Standing lenses** still apply where their gate opens — check `style-conformance`, `ba-spec-compliance`, `over-engineering` and `odoo-version-compat` per `/implement`'s table, and subtract each one that fires from your adaptive count, floor of 1.

Arm `Bash(run_in_background: true, command: "sleep 1500; echo WATCHDOG_REVIEW")`. A queued reviewer is healthy — do not cancel one for being slow.

**Every report needs its DEPTH block.** A report with `VERDICT` and `FINDINGS` but no counts is rejected and re-run; that block is how a shallow review is detected.

**Merge before acting:** same `file:line` + same issue class is one finding; a `MUST FIX` from any reviewer counts even when the others missed it; divergence is signal.

## Phase 4 — Fix

Build one list from the merged `MUST FIX` / `CRITICAL` findings, route each to the Coder that owns the file, and resume that Coder by `agentId`. When the round lands, re-run the probes for every AC the fix touches and paste the output again.

Re-review only the dimensions whose findings were fixed. Maximum 4 rounds; what survives goes to Known Concerns with the reviewer's wording and the reason it stands.

A finding that says "add a test" is answered by a probe and a note in Known Concerns, never by creating a `tests/` directory.

## Finalization

<procedure>
1. □ Every MUST FIX resolved or recorded in Known Concerns — show the list.
2. □ `ruff check` and the project's pylint wrapper clean — show the output.
3. □ Verification probes re-run against the final code — paste the output.
4. □ Module installs clean from scratch — show the `Modules loaded` line.
5. □ `__manifest__.py` version bumped for every module the diff touches.
6. □ Spec moved to `{dir}/5-review/`, Change Control entry written for every deviation.
7. □ Dev server left running or stopped, as the user asked; neutralization re-applied after the last `-u`.
</procedure>

**The user commits and pushes.** Your work ends at a working tree the user can read. Give them the command; the coverage gate asks for tests this module does not carry, so it is a `--no-verify` commit.

<bad_pattern>
❌ BAD THOUGHT: "No tests ship, so I will report the work done once the code reads correctly."
✅ REALITY: verification moved, it did not disappear. On one task a probe caught a `UniqueViolation` that a one-line removal had introduced, hours after the code read fine to three reviewers.
⚠️ DETECTION: about to write a completion report with no command output in it? → run the probes first.
</bad_pattern>

## Compact instructions

Preserve: the agent registry with every `agentId`; which phase is running; the verification probe file and its last output; unresolved MUST FIX findings; modified files per module and whether each manifest version was bumped; server and database state.

# Codex reviewer backend — running review agents on Codex instead of Claude

Read this when `/spec` or `/implement` needs to pick a reviewer backend, and follow it for every codex-backed reviewer in the run. Both commands share this file so the two backends stay in step.

The point is quota, not quality: a Claude subagent burns the weekly Claude limit, a Codex run burns the Codex subscription. When one budget is tight and the other is idle, the reviewers are the cheapest fleet to move — they are the largest spawn wave and they only read.

## Scope — what moves, what stays

The Codex CLI runs its reviewers in a **read-only** sandbox. That draws the line:

| Role | Backend | Why |
|---|---|---|
| `/spec` critics — 4 fixed + every adaptive lens | switchable | read the spec and the code, report findings |
| `/implement` reviewers — Code, Test, Spec-Auditor, Security + every adaptive lens | switchable | read the diff, report findings |
| `/spec` Analyst, Architect | always Claude | they `Edit` the spec file |
| `/spec` researchers | always Claude | their reports feed the lead's own Phase 1 reading and the questions it puts to the user — the one place where a shallow pass is most expensive |
| `/implement` Coders, Tester | always Claude | they write production code and tests |
| `/implement` UI-Reviewer | always Claude | it starts a dev server and drives a browser — both blocked by a read-only sandbox |

`REVIEW_BACKEND = claude` keeps everything on the native agents. `REVIEW_BACKEND = codex` moves only the switchable rows.

## 1. Choose the backend — once, at command setup

Parse `$ARGUMENTS` first. A pre-answer skips the question entirely:

- `--reviewers=codex` → `REVIEW_BACKEND = codex`, `REVIEW_MODEL = gpt-5.6-terra`
- `--reviewers=claude` → `REVIEW_BACKEND = claude`
- `--reviewer-model=<slug>` → overrides `REVIEW_MODEL` (implies codex)

Strip these flags before resolving the task ID — what remains is the ID or slug.

With no pre-answer, ask **once** with `AskUserQuestion`, before any agent is spawned:

```
Header: Reviewers
Question: Кем прогнать ревью на этом запуске? (Claude-агенты жгут недельный лимит Claude, Codex — подписку Codex)
Options:
  1. Codex Terra (Recommended) — gpt-5.6-terra, экономит лимит Claude; ревьюверы читают те же ~/.claude/agents/*.md
  2. Claude-агенты — нативные субагенты, полный протокол сообщений и re-review по agentId
  3. Codex Sol — gpt-5.6-sol, сильнее и дороже по квоте Codex
```

Record the answer as `REVIEW_BACKEND` and `REVIEW_MODEL`, announce it in one line, and carry it through every later phase of the run — including fix rounds and re-reviews.

## 2. Resolve the companion script and the model

`${CLAUDE_PLUGIN_ROOT}` is set only inside plugin commands, so resolve the path yourself. Pick the highest installed version — a plugin update adds a new directory beside the old one:

```bash
ls -d ~/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs | sort -V | tail -1
```

<critical>
Record the absolute path this prints as `{COMPANION}` and paste it literally into every later command. Shell variables do not survive between `Bash` calls — a `$COMPANION` written into §4's launch line expands to the empty string in that new shell, and `node "" task …` fails the whole batch at once.
</critical>

Confirm `REVIEW_MODEL` is a real slug before the batch. An unknown model name is **passed through verbatim** to the API rather than rejected, so a typo fails late — after every reviewer has already launched. Paste the chosen slug in place of `gpt-5.6-terra` below:

```bash
python3 -c "
import json, io
slugs = [m['slug'] for m in json.load(io.open('/home/kern/.codex/models_cache.json'))['models']]
print('OK' if 'gpt-5.6-terra' in slugs else 'UNKNOWN MODEL — pick from: ' + ', '.join(slugs))"
```

`UNKNOWN MODEL` → tell the user the available slugs and ask which to use.

## 3. Probe the sandbox — the gate that makes the reports real

<critical>
Run this probe before the first codex reviewer of the run, and treat a failure as a hard stop for the codex backend.
</critical>

```bash
codex sandbox echo CODEX_SANDBOX_OK
```

`CODEX_SANDBOX_OK` on stdout → the sandbox starts, tool calls will really run. Anything else → announce *"Codex sandbox probe failed — falling back to Claude reviewers for this run"*, set `REVIEW_BACKEND = claude`, and continue.

The probe costs ~0.1s and no API call. It exists because of a failure this box has already hit: codex bundles a non-setuid `bwrap` that needs unprivileged user namespaces, which Ubuntu blocks by default. When the sandbox cannot start, **codex still exits 0 and still writes a plausible report** — but every `Read` and `Grep` inside it failed, so the reviewer graded the diff blind. `/etc/apparmor.d/codex-bwrap` grants `userns` to that one binary and pins its path, so an npm update that moves the binary silently reopens the hole.

<bad_pattern>
❌ BAD THOUGHT: "The report came back with a DEPTH block and zero findings — clean diff, move on."
✅ REALITY: A blind codex run produces exactly that: rc=0, a well-formed report, a fabricated DEPTH block, zero findings. Report shape cannot distinguish it from a real review — only the probe can.
⚠️ DETECTION: About to accept a codex report on a run where you never saw `CODEX_SANDBOX_OK`? → probe now, and relaunch the batch if it fails.
</bad_pattern>

## 4. Launch one reviewer

Create the run directory once, at setup, and record the path it prints as `{CODEX_DIR}` — re-reviews reuse it:

```bash
mktemp -d -t codex-reviewers-XXXXXX
```

Per reviewer, write the prompt to a file and launch it as a background Bash job. A prompt file keeps multi-paragraph review instructions out of shell quoting, and `--prompt-file` resolves against `--cwd`, so pass it as an **absolute** path. `{COMPANION}` and `{CODEX_DIR}` are the literal paths you recorded above:

```
Write({CODEX_DIR}/{name}.prompt.md, "<the same prompt text the native spawn would carry, plus §8>")

Bash(
  run_in_background: true,
  command: 'node {COMPANION} task --fresh --cwd {working_directory} --model {REVIEW_MODEL} --effort high --prompt-file {CODEX_DIR}/{name}.prompt.md > {CODEX_DIR}/{name}.report.md 2> {CODEX_DIR}/{name}.log'
)
```

- `--fresh` states "do not resume" explicitly. A new thread is already the default, but the flag is mutually exclusive with `--resume-last`, so it makes a batch of parallel reviewers fail loudly rather than quietly share one thread if that default ever changes.
- No `--write` flag — the sandbox stays read-only, which is what makes a reviewer safe to run unattended.
- `--effort high` matches the depth these agent files ask for. `xhigh` is available for a large or security-sensitive diff.
- Redirects split the two streams: the report lands in `.report.md`, the `[codex]` progress trace in `.log`.
- Launch the whole batch in **one response**, exactly like the native spawn wave.

The prompt body is the native spawn prompt, unchanged — `Read your instructions: ~/.claude/agents/{agent-file}.md` works from any cwd, so both backends read the same agent definitions.

## 5. Registry

Codex reviewers have no `agentId`. Record them in the same table with the fields that do address them:

```
name              | backend | job/report path                    | role
code-reviewer     | codex   | {CODEX_DIR}/code-reviewer.report.md | Code-Reviewer
adaptive-rollback | codex   | {CODEX_DIR}/adaptive-rollback.report.md | Adaptive-Reviewer
ui-reviewer       | claude  | a5d6-...                            | UI-Reviewer
```

A mixed run is normal — UI-Reviewer stays native while the rest are codex.

## 6. Collect the reports, and liveness

A background Bash job re-invokes you when it exits and carries its exit status, so a *crashed* codex reviewer wakes you on its own — no ping-by-`agentId` needed.

A **hung** one does not. A stalled `node` process never exits, so nothing ever wakes the lead, which is the exact failure `liveness-protocol.md` exists to catch. Keep the phase watchdog armed over the codex batch too (`sleep 1500` is the right order of magnitude for a `--effort high` review). On a WATCHDOG wake-up, audit by **file** rather than by agent: for each registry row, does `{CODEX_DIR}/{name}.report.md` exist and end with a complete report? A row with no finished report and no exit notification is hung — relaunch that one reviewer and re-arm the timer.

On each exit, read `{CODEX_DIR}/{name}.report.md`:

- **Exit 0 with a full report** → treat it exactly like a native report. The DEPTH-block rejection rule and the Test-Reviewer coverage-matrix rule apply unchanged.
- **Exit 0 with an empty or truncated report** → read `{name}.log` for the reason, then relaunch that one reviewer.
- **Non-zero exit** → read `{name}.log`. A rate-limit or quota message means the batch was too wide: relaunch that reviewer after the others finish. Any other error, or a second failure on the same reviewer → spawn the native Claude agent for that dimension instead and say so.

## 7. Re-review is self-contained

A native reviewer is resumed by `agentId` and remembers its own findings. A codex run has no such handle — `--resume-last` tracks the last thread in the workspace, which is meaningless across a batch of six.

So a codex re-review is a **fresh run whose prompt carries the prior findings verbatim**:

```
Read your instructions: ~/.claude/agents/{agent-file}.md
Spec file: {spec_path}
Working directory: {working_directory}
Base branch for diff: {base_branch}

This is a RE-REVIEW after fixes. These are the findings YOU raised on the previous pass:
{full text of that reviewer's MUST FIX / CRITICAL findings — file:line, severity, description}

Primary: verify each one is resolved.
Secondary (mandatory): re-run your full audit procedure on the modified files — fixes introduce new issues. Treat new methods, new error paths, and regressions in previously-clean code as in scope.
End your report with {the completion signal the calling command waits for}. Give it only if both the primary items are resolved AND the secondary pass finds nothing new; otherwise list every outstanding issue instead.
```

Copy the findings in full. A summary ("your 3 findings about the order model") gives the fresh run nothing to verify against.

Fill the completion signal from the command you are running, because the two differ and a lead waiting for the wrong string hangs on a report that already arrived:

- `/implement` Phase 3 → `PASS`
- `/spec` §2d → the critic's own re-check signal, e.g. `SPEC BUSINESS CRITIC RE-CHECK DONE.` / `SPEC ARCH CRITIC RE-CHECK DONE.` / `SPEC TESTING CRITIC RE-CHECK DONE.`

## 8. No mid-flight messages — say so in every prompt

A codex reviewer cannot `SendMessage` you and cannot receive one. Append this to every codex reviewer prompt:

```
You run to completion in one pass and cannot message the lead mid-run. Put every escalation, question for the user, and blocked-check note in your final report, under the heading your agent file specifies — a question you hold back is a question nobody ever answers.
```

`/spec` critics normally park emergent questions for Phase 3 anyway, so this costs nothing there. It matters most for a reviewer that would otherwise wait for an answer it will never get.

<bad_pattern>
❌ BAD THOUGHT: "Re-review time — resume the code reviewer by its id and ask it to verify."
✅ REALITY: On a codex run there is no id and no preserved context. `SendMessage` to a non-agent fails, and the fix round stalls with every finding still open.
⚠️ DETECTION: About to address a reviewer whose registry row says `backend: codex`? → launch a fresh run with the findings pasted into the prompt (§7).
</bad_pattern>

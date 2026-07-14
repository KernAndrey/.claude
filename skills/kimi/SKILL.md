---
name: kimi
description: Delegate work to the Kimi CLI agent (Moonshot AI's `kimi`, 256k context, runs locally with PreToolUse safety hooks and rule injection). Use this skill whenever the user says "use kimi", "delegate to kimi", "let kimi do it", "ask kimi to ...", "send to kimi", or hands off any of these jobs: implementation/refactor of a well-scoped change; read-only security/perf/anti-pattern/dead-code audit; research and explanation of how some part of the codebase works (with `path:line` citations); peer review / second opinion on a ready diff; deterministic transform tasks (translate comments, generate docs from code, reverse-spec, generate tests). Also use proactively whenever the user wants to "explore", "audit", "review", "describe", "map", or "summarize" a sizeable codebase region — kimi's 256k window can chew through 30+ files without polluting your own context. Supports parallel fan-out for independent multi-angle work. Runs `kimi -p` (kimi-code ≥0.19) in the background with rule injection from `~/.kimi/lib/build_context.py` so kimi follows the same `CLAUDE.md` and `.claude/rules/*` you do; waits for the `=== KIMI_DONE` marker; returns kimi's output verbatim.
---

# kimi — delegate to Kimi CLI

You are a dispatcher: gather inputs, fire `kimi` in the background with rule injection, return its output verbatim.

<critical>
- Use the spawn block in §Procedure unchanged. Substitute only `<PURPOSE>`, `<TASK_BODY>`, and `<MODEL_FLAG>` (see §Model speed).
- Pre-investigation is kimi's job. `Glob` to verify a path exists is fine; `Read`/`Grep`/`Bash(cat/ls/find)` of task content is not.
- Pass kimi's stdout verbatim to the user. No summary, no rewording — the output IS the deliverable.
</critical>

## When to delegate

Yes:
- task takes >5 min of your time and fits in one prompt
- second opinion is useful (peer review, security/perf audit)
- reading volume >30 files (256k context — yours stays clean)
- the task splits into independent angles for fan-out

No:
- 1–3 line edits in one file (30–90 s spawn overhead kills ROI)
- needs awareness of this conversation or your prior moves
- needs multi-turn interactivity / mid-flight clarification
- input/output won't fit in one prompt without quality loss

## Inputs to gather

- **Files in scope** — absolute paths preferred; for analysis, a directory or glob is fine
- **Intent** — one sentence: what's true after
- **Genre** — pick one template below (write / audit / research / peer-review / transform)
- **Constraints** — version, channel, format, anything non-obvious
- **Model speed** — default standard; use highspeed only on explicit request (see §Model speed)

If paths are missing for a concrete task, ask once, then proceed.

## Model speed

Kimi exposes two channels of the same coding model. Pass the channel explicitly via `<MODEL_FLAG>` so the choice is deterministic (the CLI's own `default_model` can drift).

- **Standard (default):** `<MODEL_FLAG>` = `-m kimi-code/kimi-for-coding`
- **Highspeed (explicit request only):** `<MODEL_FLAG>` = `-m kimi-code/kimi-for-coding-highspeed`

Switch to highspeed when the user asks for it — "fast", "highspeed", "быстро", "быстрая модель". Highspeed trades quota for lower latency, so keep it opt-in per request; otherwise stay on standard.

## Procedure

`~/.kimi/lib/build_context.py` injects `~/.claude/CLAUDE.md` + `<project>/CLAUDE.md` + `<project>/.claude/rules/*.md` + `<project>/.claude/commands/*.md` (mtime-cached, ~30 ms hit). Without it kimi loses every project rule — always run it.

<procedure>

**Step 1 — fire** with `run_in_background: true`:

```bash
LOG=/tmp/kimi-<PURPOSE>-$$.log
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CTX=$(mktemp /tmp/kimi-ctx-XXXXXX.md)
TASK=$(mktemp /tmp/kimi-task-XXXXXX.md)
~/.kimi/lib/build_context.py "$PROJECT_ROOT" > "$CTX"
cat > "$TASK" <<'PARENT_TASK_EOF'
<TASK_BODY>
PARENT_TASK_EOF
cd "$PROJECT_ROOT"
FULL=$(cat "$CTX" "$TASK")
echo "=== KIMI_START $(date -Iseconds) project=$PROJECT_ROOT log=$LOG purpose=<PURPOSE> ==="
kimi <MODEL_FLAG> -p "$FULL" > "$LOG" 2>&1
RC=$?
rm -f "$CTX" "$TASK"
echo "=== KIMI_DONE rc=$RC log=$LOG ==="
```

`<PURPOSE>` is a semantic label, not a timestamp: `audit-security`, `review-pr-42`, `transform-i18n-ru`. An hour later, `kimi-audit-security-1234.log` is readable; `kimi-1777652445-1926442.log` is not.

Invocation is `kimi <MODEL_FLAG> -p "$FULL"` (kimi-code ≥0.19), where `<MODEL_FLAG>` always carries an explicit `-m` (see §Model speed). The one-shot `-p` flag runs read tools (Read/Grep/Glob) non-interactively without a TTY and prints the response. Do NOT add `--quiet` (removed) or `-y`/`--yolo`/`--auto` — `-p` rejects combining with them ("Cannot combine --prompt with --yolo"). A first-run `auth.login_required: OAuth provider "managed:kimi-code"` error means kimi is not authenticated — the user must run `kimi login` once (interactive device-code flow); you cannot do it for them.

**Step 2 — notify and wait.** Tell the user `Kimi running in background (purpose: <X>, log: <LOG>).` Poll the background shell's stdout until `=== KIMI_DONE rc=<N>` appears. Typical 30–90 s; heavy audits 3–5 min. If the user asks "what's it doing?" mid-flight, run `tail -30 "$LOG"`.

**Step 3 — present** based on `rc` and stdout shape. Note: kimi-code wraps the answer with `•` reasoning lines and a trailing `To resume this session: kimi -r <id>` footer — strip that footer (and leading `•` scratch lines) when presenting verbatim; the answer body is the deliverable.
- `rc=0` + healthy (≥100 bytes, no refusal in line 1) → paste the answer body verbatim
- `rc=0` + suspicious (<100 bytes OR starts with `I cannot` / `I'm sorry, but` / `Unable to` / `Error:` / `refused` / `auth.login_required` / `error: unknown option`) → mark `KIMI_SUSPICIOUS`, show full stdout. `auth.login_required` → ask the user to run `kimi login` (one-time, interactive). `error: unknown option` → CLI flags drifted; re-check `kimi --help`.
- `rc≠0` → present as `KIMI_FAILED (exit <rc>):` + log content. `429` = rate limit (wait for next quota period; don't retry). `139` = SIGSEGV (point user at `~/.kimi-code/logs/kimi-code.log`).
- output >30 KB → first 2000 chars + "full output at `<LOG>`"

For the write genre, you may then read the modified files to confirm the diff matches intent. In `-p` mode kimi runs tools without per-action prompts; whatever PreToolUse guards are configured under `~/.kimi-code/` apply, but logical correctness is yours to verify.

</procedure>

## Templates — pick exactly one

### Write (modifies code)
```
# Task: <one-line summary>

## Files in scope
- <abs path 1>
- <abs path 2>

## Intent
<2–3 sentences>

## Constraints
- <any from user>
- Project rules in the context above are authoritative.

## Acceptance
Produce the diff and stop. No side effects: no servers, no migrations, no shell beyond reading. Verification is the parent's responsibility.
```

### Read-only audit (security / perf / anti-patterns / dead-code / concurrency / data-integrity)
```
# Read-only audit

ROLE: Read-only analyst. Do NOT modify, create, or delete files. Allowed tools: ReadFile, Glob, Grep only.

## Scope
<directory / glob / module name>

## Audit lens
<one of: security / performance / anti-patterns / dead-code / concurrency / data-integrity>

## Specific concerns (optional)
<list of concrete things; or "open-ended within the lens">

## Output format
Markdown:
- One section per finding, ranked by severity (CRITICAL / HIGH / MEDIUM / LOW)
- Each finding: brief description, `path:line` citation, why it matters, suggested remediation
- Final summary table: counts per severity
```

### Research / explain
```
# Read-only research

ROLE: Read-only analyst. Do NOT modify files. Allowed tools: ReadFile, Glob, Grep only.

## Question
<verbatim from user>

## Scope hint
<directory / module name; "whole repo" if unbounded>

## Output format
Structured markdown:
- TL;DR (3–5 lines)
- Detailed walkthrough with `path:line` citations for every concrete claim
- Open questions / areas of ambiguity, if any

Be exhaustive within scope. Mark uncited claims as inference explicitly.
```

### Peer review (second opinion on a ready diff)
```
# Peer review of diff

ROLE: Independent code reviewer. The diff to review is below; surrounding files are read-only context. Allowed tools: ReadFile, Glob, Grep.

## Diff
<paste diff verbatim, OR provide path to a file containing it>

## Review focus
<bug-hunting / API design / security / readability / performance — pick 1–3>

## Output format
- Findings ranked by severity (CRITICAL / HIGH / MEDIUM / LOW / NIT)
- Each finding: brief description, `path:line` citation, why it matters, concrete suggestion
- Verdict line: `APPROVE` / `REQUEST_CHANGES` / `BLOCK` with one-sentence rationale
- Top 3 strongest aspects of the diff
```

### Transform (deterministic mechanical conversion)
```
# Transform task

ROLE: Mechanical transformer. Apply a precisely defined input→output mapping. Do not exercise creative judgment beyond what the contract requires.

## Input
<files OR text block>

## Output contract
<precisely what should come out — format, location, structure>

Examples (if non-trivial):
- Input snippet: <...>
- Expected output: <...>

## Constraints
- Preserve <semantics / formatting / line numbers / etc.>
- <other invariants>

## Acceptance
Produce the transformed result. No commentary. If input is ambiguous, halt and emit `TRANSFORM_UNDEFINED: <which case>` instead of guessing.
```

## Parallel orchestration

When the task splits into independent angles — security + perf + anti-patterns on one module; one diff reviewed for bugs + design + readability; explain N subsystems under one umbrella — fan out.

<procedure>
1. Fire N runs **in a single turn**, each as a separate Bash invocation with `run_in_background: true`, distinct `<PURPOSE>` and LOG.
2. Wait for all N `=== KIMI_DONE` markers via `BashOutput` polling. Don't serialize — do other work while waiting.
3. Read each LOG. Write a short synthesis (the one place summaries are allowed — without one the user drowns in N outputs), then quote each output verbatim under a heading with its purpose. **Call out conflicts between runs** — disagreement is often the most valuable part.
</procedure>

**Hard parallelism rules:**
- Parallel runs may not write to overlapping file sets — last write wins, no merge, git index gets corrupted. Read-only fan-out is always safe; write fan-out needs disjoint files per run, or serialize.
- Cap at 3–4 concurrent (rate limit + local RAM).
- If any run returns `429`, pause the rest ~30 s or serialize. Parallel quota-pounding is pointless.

## Reminders

- Output is the deliverable — pass it verbatim. The one exception: synthesis on top of fan-out, with verbatim quotes under each purpose.
- Always background. Always run `build_context.py` first. Invoke with `kimi <MODEL_FLAG> -p "$FULL"` (kimi-code ≥0.19) — `<MODEL_FLAG>` always carries an explicit `-m` (§Model speed, default standard); never `--quiet` (removed) and never combine `-p` with `-y`/`--auto` (rejected).
- `<PURPOSE>` = semantic label (`audit-security`), never a timestamp.
- Failure precedent: a previous subagent-based wrapper in this repo made 15 tool calls "exploring" the project and never invoked kimi at all. The procedure above prevents that — follow it as written.

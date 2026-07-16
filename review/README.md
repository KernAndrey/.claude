# `review/` — pre-commit AI code review

Per-commit gate that routes the staged diff through one or more LLM
reviewers, optionally arbitrates the findings with a stronger model,
and either lets the commit proceed or BLOCKs it with concrete issues
to fix.

Wired into git via `core.hooksPath = ~/.claude/git-hooks/`. The
pre-commit script runs **gitleaks → semgrep → `python3 -B
review/hook.py`** sequentially; any of the three can block.

```
~/.claude/git-hooks/pre-commit
        │
        ▼
   gitleaks (secrets)        ─┐
        │                     │
        ▼                     │  any of these
   semgrep (--config=auto)   ─┤  blocks the commit
        │                     │
        ▼                     │
   review/hook.py            ─┘
```

## What you can change without touching `hook.py`

Everything tunable lives in two files:

| File | What it controls |
|---|---|
| `config.py` | Which CLI is the primary reviewer, the fallback, and the arbiter; their models; their timeouts. Diff-size thresholds. |
| `backends.py` | Which CLIs *can* be picked. One class per backend, registered once. |

### `config.py` — switch reviewer roles

Three slots:

```python
PRIMARIES: list[RunnerConfig]  = [RunnerConfig("opencode", "github-copilot/gpt-5.4")]
FALLBACK:  RunnerConfig | None = RunnerConfig("claude", "sonnet")
ARBITER:   RunnerConfig        = RunnerConfig("claude", "sonnet", timeout=1800)
```

Common edits:

- **Make Claude Code the sole reviewer** — `PRIMARIES = [RunnerConfig("claude", "sonnet")]`.
- **Run two reviewers in parallel and let the arbiter consolidate
  their findings** — append a second entry:

  ```python
  PRIMARIES = [
      RunnerConfig("opencode", "github-copilot/gpt-5.4"),
      RunnerConfig("claude",   "sonnet"),
  ]
  ```

  Both reviewers run concurrently against the same diff. The arbiter
  receives every `[CRITICAL]` finding (with a `<backend>-Fn` ID
  prefix), groups duplicates into clusters, and emits one
  UPHELD/OVERTURN verdict per cluster. See **Multi-backend mode**
  below for full semantics and stats.
- **Disable the fallback** — `FALLBACK = None`. Then a total-failure
  run (every primary down) goes fail-open with a warn log.
- **Run the arbiter on OpenCode** — `ARBITER = RunnerConfig("opencode", "github-copilot/gpt-5.4", timeout=1800)`.
- **Cap commit prod-surface** — `MAX_PROD_LINES = 300`. Commits with
  more added production-code lines are rejected outright. Tests, docs,
  configs, lock-files, removals, and context lines do not count — only
  added lines in `CODE_EXTS` files outside test paths.
- **Bigger / smaller fan-out cutoff** — `FANOUT_THRESHOLD = 100`. At or
  above this added-prod-line count, the review fans out into three
  parallel lens calls (bugs / architecture / tests) plus arbiter. Below
  it, a single combined call.

`backend` is a string. It must match a key in `backends.BACKENDS`.
Validated at startup by `_verify_runner_configs()` — a typo in
`config.py` produces a named warn log, not a silent skip.

### `backends.py` — add a new tool

Adding **Codex CLI**, **Kimi**, or any other CLI reviewer is **one
file, one class, one registry entry**:

```python
class CodexBackend(Backend):
    name = "codex"

    def run(self, system_prompt, user_prompt, model, timeout):
        # Build argv, invoke subprocess, parse stdout however this CLI
        # prefers. Return (review_text, stderr, returncode).
        ...

BACKENDS: dict[str, Backend] = _build_registry(
    OpencodeBackend(), ClaudeBackend(), KimiBackend(), CodexBackend(),
)
```

Then in `config.py`:

```python
PRIMARY = RunnerConfig("codex", "<model-id>")
```

`hook.py` does not change. The dispatcher (`run_reviewer`) and
validator (`_verify_runner_configs`) both read from `BACKENDS`, so a
new entry is enough.

The `Backend` ABC enforces one method (`run`). A subclass that
forgets to implement it cannot be instantiated — the test suite
catches this via `test_backend_subclass_must_implement_run`.

`Backend.run` must be **thread-safe**: `hook.run_fanout` invokes the
same Backend instance from multiple worker threads. Keep all mutable
state inside `run` locals; do not stash request-scoped data on
`self`.

The registry is built via `_build_registry(...)`, which raises
`ValueError` at import time if two backends share the same `name`
(e.g. a copy-paste that forgets to rename). A plain dict
comprehension would silently shadow.

## Pipeline

`hook.py:main()` runs in this order:

1. **`_verify_runner_configs()`** — backend names in `config.py` exist
   in `BACKENDS`. Fail loudly on typos before any LLM call.
2. **`collect_diff()`** — read `git diff --cached`. Empty diff →
   skip. Added prod lines over `MAX_PROD_LINES` → reject with split
   instruction. Diff under `MIN_LINES_TO_REVIEW` (total) → skip.
3. **Lens routing** — `applicable_lenses(files)` skips lenses with
   nothing to look at (e.g. `architecture` on a docs-only diff).
   Docs-only diffs short-circuit to skip with no LLM calls.
4. **Mode pick** — added production-code lines ≥ `FANOUT_THRESHOLD`
   chooses fan-out (three parallel lens calls + arbiter); below
   threshold uses a single-call review.
5. **Run review** —
   - Single call: one CLI invocation reading
     `prompts/combined.md` + optional `.claude/review_prompt.md`.
   - Fan-out: three parallel lens calls (`bugs`, `architecture`,
     `tests`) reading `prompts/<lens>.md` + `prompts/common.md`.
6. **Arbiter** — if any `[CRITICAL]` findings exist, `run_arbiter`
   sends them through `prompts/arbiter.md` with the full diff. The
   arbiter UPHOLDs or OVERTURNs each one. Output regex:
   `^\s*\[(UPHELD|OVERTURN)\]\s*F\d+`. Missing verdict → fail-open
   UPHOLD (a parser bug must never silently drop a finding).
7. **Verdict** — any UPHELD CRITICAL → exit 1 (BLOCK). Otherwise
   exit 0 with warnings logged. Crashes in the script also exit 0
   (`main()`'s broad `except Exception`) — never block on our own
   bug.

### Lenses

Each lens is a separate prompt + a router predicate in
`LENS_APPLICABILITY` (in `hook.py`).

| Lens | Prompt | Predicate (when does it run?) |
|---|---|---|
| `bugs` | `prompts/bugs.md` | code OR config/infra files (config-surprise scope) |
| `architecture` | `prompts/architecture.md` | executable code only |
| `tests` | `prompts/tests.md` | executable code only |

Add a new lens by:

1. Drop `prompts/<name>.md`.
2. Add `"<name>": _has_code` (or your own predicate) to
   `LENS_APPLICABILITY` in `hook.py`. `LENS_NAMES` is derived from
   the dict, so order is the dict's insertion order.

## Kimi K2 (current primary reviewer)

`KimiBackend` is the sole primary reviewer in `config.py` (both
`PRIMARIES` and `CHUNKED_BACKENDS`). The arbiter and the total-failure
fallback stay on `claude`/`sonnet`. Requirements to keep it working:

1. Install the kimi CLI per <https://www.kimi.com/code/docs/en/>.
2. `kimi login` once for OAuth (no API-key env var is documented).
3. Active Kimi membership subscription is required — Kimi Code is paywalled.

The configured model alias is `kimi-code/k3` (Kimi K3, 1M context), which
must match a `[models."…"]` key in `~/.kimi-code/config.toml` — the live
config. (`~/.kimi/config.toml` is versioned but inert; kimi-code no longer
reads it, so edits there have no effect.) An unregistered alias fails fast
with `config.invalid: Model "…" is not configured`. If kimi is ever
unreachable on a commit, the orchestrator fails open to the
`claude`/`sonnet` fallback, so a broken kimi never blocks a commit on
its own.

The backend invokes `kimi -p <instruction> --model <m> --output-format
stream-json`. kimi-code (0.24.1) dropped `--quiet` / `--agent-file` /
`--input-format` and the stdin prompt channel, so the full system+user
prompt is staged in a temp file and kimi gets a short instruction to `Read`
it — review prompts (cached block + manifest + full diff) routinely exceed
Linux `MAX_ARG_STRLEN` (~128KB per single arg), which would fail instantly
with `[Errno 7] Argument list too long` if passed as one argv value.
`stream-json` is line-delimited JSON parsed by `_parse_stream_json`; the
`text` format interleaves `•` reasoning lines and a resume footer with the
answer.

Read-only containment comes from `KIMI_REVIEW_READONLY=1`, which
`KimiBackend` sets for that run only. It arms the PreToolUse deny hook at
`~/.kimi/hooks/review_readonly.py` (registered in `~/.kimi-code/config.toml`),
denying `Write`/`Edit`/`Bash`/`FetchURL`/`WebSearch` and leaving the reviewer
only `Read`/`Grep`/`Glob`/`ReadMediaFile`. This replaces the old
`--agent-file` tool allowlist, since `-p` auto-approves tool calls. It pairs
with the write-tree/read-tree index backstop in `~/.claude/git-hooks/pre-commit`.
The YAML files under `review/agents/` are leftovers from the kimi-cli era and
are no longer read.

## Skipping the gate

- **Per-commit**, when nothing the reviewer cares about: a docs-only
  or pure-data diff makes the router skip with no LLM calls.
- **Forced**: `git commit --no-verify`. Per `~/.claude/CLAUDE.md`
  this is discouraged; the AI review hook can also be paused
  globally by editing `~/.claude/git-hooks/pre-commit` to short-
  circuit.

## Parallel pre-review fast-path

For the autonomous SDD workflow (`/implement-wf` → `workflows/implement.js`),
landing many commits sequentially is slow: each `git commit` runs the LLM gate
(~minutes) one at a time. The fast-path lets the workflow review every planned
commit **in parallel up front**, then skip the redundant per-commit LLM review.

- **`approvals.py`** — the canonical **content key** `content_hash` (`sha256` of
  each changed path's final blob sha, parsed identically on every side by
  `entries_from_raw` / `content_hash_from_raw`) + the approval-marker store
  `.review/approvals/<content_key>`. `diff_hash` (the old `sha256` of the stripped
  `git diff --cached`) is retained as a log/back-compat field only — it is
  base-dependent, so a content-identical diff reconstructed from a different base
  silently missed. The content key keys on final content, so it survives base
  drift, rename detection, and stash reconstruction.
- **`pre_review.py`** — reviews each commit group concurrently against a private
  `GIT_INDEX_FILE` (shared index/worktree untouched), reusing `run_review`. A
  CLEAN group gets a marker keyed on its content key. Also runs a whole-diff lens
  pass over the union for cross-cutting issues. `--verify-range BASE` re-derives
  each landed commit's content key for the post-Land integrity audit.
  `--validate-plan PLAN` is the LLM-free fix-A disjointness gate: it flags any
  file present in >=2 groups, lists advisory uncovered files, and returns a
  deterministic disjoint `normalized_plan` fallback.
- **`hook._maybe_fastpath`** — at commit time, *after* the deterministic
  preflight and *before* any LLM work: if `SDD_REVIEW_FASTPATH` is set **and** a
  marker exists for the **staged content key** (`get_staged_content_key`), skip
  the LLM review (exit 0).

**Fail-safe.** The bypass can only ever *skip an already-reviewed change*, never
pass an unreviewed one. No flag, no marker, or any key mismatch → the full
review runs. Ordinary (non-engine) commits never set `SDD_REVIEW_FASTPATH`, so
they never compute a content key and take the unchanged path. gitleaks/semgrep
(pre-commit wrapper) and the coverage/assert preflight (`run_gate`) always run
regardless.

**Integrity (no signing — the Workflow JS sandbox has no `crypto`).** The
workflow builds its trusted approved-set from the reviewer agent's return, the
committer is never told the marker format, and a **separate** verifier agent
(`--verify-range`) re-derives every landed commit's content key from git so the
committed set is cross-checked against the approved set. Any unapproved landed
commit is force-reviewed and surfaced as a Known Concern. Defense-in-depth: a
`hooks/guard.py` rule blocks shell writes to `.review/approvals/`.

## Tests

```
cd ~/.claude && python3 -m pytest review/test_hook.py review/test_approvals.py review/test_pre_review.py -v
```

The suite covers:

- Backend contract (`OpencodeBackend`, `ClaudeBackend`, ABC guard,
  registry invariants, custom-backend dispatch).
- Dispatcher (`run_reviewer`, `run_with_fallback` happy/timeout/
  unreachable/no-fallback paths).
- Startup validation (`_verify_runner_configs` for each role).
- Lens routing, fan-out, arbiter parsing, verdict synthesis.
- Diff sizing (production-code line counter, fan-out threshold,
  diff-size cap).

Test discipline (per top-level `~/.claude/CLAUDE.md`): every new
code path needs a same-diff test or a documented skip. The AI review
hook will block missing coverage.

## Multi-backend mode

When `PRIMARIES` has more than one entry the hook switches to a
parallel orchestration:

1. Every primary runs **concurrently** against the same staged diff.
   Each backend independently chooses single-call vs fan-out per the
   usual `FANOUT_THRESHOLD` rule. No per-primary fallback.
2. Findings get backend-prefixed IDs (`opencode-F1`, `claude-F2`) so
   downstream stages can distinguish reviewers.
3. The arbiter receives **all** findings from **all** primaries and
   reads `prompts/arbiter_multi.md`. Its job is two-fold: cluster
   duplicates (same defect found by multiple reviewers) and validate
   each cluster (UPHELD / OVERTURN). Output regex:
   `^\s*\[CLUSTER\s+(C\d+)\]` for groupings,
   `^\s*\[(UPHELD|OVERTURN)\]\s*(C\d+)` for verdicts.
4. **`FALLBACK` is now a safety-net.** It fires only when **every**
   primary failed (no usable output anywhere). The fallback result is
   appended to the run with `fallback_used=True` and recorded in the
   stats — a recurring fallback rate is itself a reliability signal
   to act on.

`N==1` is a degenerate case of the same flow: one primary, the legacy
single-backend arbiter (`prompts/arbiter.md`) is used so existing
markdown-log layout stays byte-for-byte stable, and fallback fires
the same way it used to (the only primary failing == "all" failing).

### Stats — `logs/stats.jsonl` + per-run sidecar JSON

Every orchestrator run writes two files:

- `logs/<timestamp>_<project>_<verdict>.stats.json` — full per-run
  detail (per-backend status, latency, raw + clustered finding
  counts, arbiter status + duration, fallback trigger reason).
- `logs/stats.jsonl` — append-only one-line-per-run aggregate.
  Designed for `jq` and `stats_cli.py` to slice across many commits.

Schema is versioned (`schema_version: 1`). The shape is documented at
the top of `stats.py` and exercised by
`test_hook.py::test_build_run_stats_schema_shape`.

### `stats_cli.py` — compare backends

```bash
# Default: per-backend aggregates (runs, success rate, p50/p95 latency,
# avg findings, upheld %, solo %).
python ~/.claude/review/stats_cli.py

# Head-to-head pair view (only over commits where both ran).
python ~/.claude/review/stats_cli.py --compare

# Last N runs, per-backend status + duration.
python ~/.claude/review/stats_cli.py --last 20

# Filters compose. Date filter, backend filter, project filter.
python ~/.claude/review/stats_cli.py --since 2026-04-01 --backend opencode
python ~/.claude/review/stats_cli.py --project hubcraft-tms

# Raw JSON for jq pipelines.
python ~/.claude/review/stats_cli.py --json | jq '.[].verdict'
```

Quick `jq` recipe — per-backend latency series for trend plotting:

```bash
jq -r '[.timestamp, (.backends[] | .config.backend + ":" + (.duration_seconds|tostring))] | @tsv' \
  ~/.claude/review/logs/stats.jsonl
```

## Pre-flight gate

Before any LLM call, `hook.py` runs a deterministic pre-flight gate that checks two things on the staged diff:

1. **Coverage gate** — every new line/branch must be covered by a test (`diff-cover` against the compare branch).
2. **Assert gate** — every new or modified test function must contain at least one assertion (Python `ast` + JS/TS `@typescript-eslint/parser`).

If either check fails, the commit is blocked with an actionable report and **no AI reviewer is invoked**, saving tokens and wall-clock time.

### Opt-out

The gate is **enabled by default** (opt-out). To disable it for a target repo, add to that repo's `pyproject.toml`:

```toml
[tool.code_review.coverage_gate]
enabled = false
```

### Dependencies

- **Python**: `diff-cover` (`pip install diff-cover`)
- **JS/TS assert detection**: Node ≥ 18 + `@typescript-eslint/parser` (`npm install --save-dev @typescript-eslint/parser`)

If the dependencies are missing, the gate exits with a setup-error (exit 2) and a concrete install hint.

### Environment overrides

| Variable | Effect |
|---|---|
| `CODE_REVIEW_SKIP_GATE=1` | Skip the entire gate |
| `CODE_REVIEW_SKIP_COVERAGE=1` | Skip only the coverage check |
| `CODE_REVIEW_SKIP_ASSERT=1` | Skip only the assert check |
| `CODE_REVIEW_COVERAGE_THRESHOLD=80` | Override coverage threshold |
| `CODE_REVIEW_GATE_FAIL_OPEN=1` | Convert internal crashes to warnings (exit 0) |
| `CODE_REVIEW_GATE_VERBOSE=1` | Emit extra diagnostic output |

### Rollout warning

Because the gate is opt-out, the **next commit in every target repo using this hook will be gated** until the repo either:
- generates a fresh `coverage.xml` / `lcov.info`, or
- explicitly opts out via `pyproject.toml`.

Coordinate the rollout with your team to avoid surprise commit blocks.

## Logs

Every review writes a markdown log to
`~/.claude/review/logs/<timestamp>_<project>_<verdict>.md` with the
full diff, per-backend section (multi-backend mode), per-lens output,
and arbiter rationale. Used as input for the `replay.py` smoke harness
when refactoring this package.

## Trade-off channel

The AI reviewer reads inline `# review-note: <reason>` comments as
deliberate trade-offs and honors specific, named-invariant
explanations. Examples in this tree:

- `config.py:ARBITER` — Sonnet-not-Opus is owned by the user.
- `backends.py:BACKENDS` — ABC over Callable map is owned by the
  contract (each backend owns its parser as a method, ABC enforces
  the contract).

Vague notes (`intentional`, `by design`) or three+ in one commit
are themselves flagged as CRITICAL. This is documentation for the
next reviewer and the next reader, not a hook-skip.

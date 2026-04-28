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

Three `RunnerConfig` slots:

```python
PRIMARY:  RunnerConfig         = RunnerConfig("opencode", "github-copilot/gpt-5.4")
FALLBACK: RunnerConfig | None  = RunnerConfig("claude",   "sonnet")
ARBITER:  RunnerConfig         = RunnerConfig("claude",   "sonnet", timeout=900)
```

Common edits:

- **Make Claude Code primary** — `PRIMARY = RunnerConfig("claude", "sonnet")`.
- **Disable the fallback** — `FALLBACK = None`. The hook fails open
  when the sole reviewer is unreachable; commits proceed with a warn
  log.
- **Run the arbiter on OpenCode** — `ARBITER = RunnerConfig("opencode", "github-copilot/gpt-5.4", timeout=900)`.
- **Bigger / smaller fan-out cutoff** — change `FANOUT_THRESHOLD`
  (added production lines required to switch from single-call to
  three-lens fan-out).
- **Cap diff size** — `MAX_DIFF_LINES = 3000`. Larger diffs are
  rejected outright with an instruction to split.

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

BACKENDS: dict[str, Backend] = {
    b.name: b for b in (OpencodeBackend(), ClaudeBackend(), CodexBackend())
}
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
   skip. Diff over `MAX_DIFF_LINES` → reject with split instruction.
   Diff under `MIN_LINES_TO_REVIEW` → skip.
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

## Skipping the gate

- **Per-commit**, when nothing the reviewer cares about: a docs-only
  or pure-data diff makes the router skip with no LLM calls.
- **Forced**: `git commit --no-verify`. Per `~/.claude/CLAUDE.md`
  this is discouraged; the AI review hook can also be paused
  globally by editing `~/.claude/git-hooks/pre-commit` to short-
  circuit.

## Tests

```
cd ~/.claude && python3 -m pytest review/test_hook.py -v
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

## Logs

Every review writes a markdown log to
`~/.claude/review/logs/<timestamp>_<project>_<verdict>.md` with the
full diff, per-lens output, and arbiter rationale. Used as input for
the `replay.py` smoke harness when refactoring this package.

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

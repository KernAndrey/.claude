"""Tunable settings for the pre-commit code-review hook.

Edit this file to swap models, change reviewer backends, or retune
thresholds. Only `review/hook.py` and `review/test_hook.py` import
from here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunnerConfig:
    """One backend invocation: which CLI, which model, how long to wait.

    ``backend`` must match a key in ``backends.BACKENDS``. Validated at
    ``main()`` startup by ``hook._verify_runner_configs``.
    """

    backend: str
    model: str
    timeout: int = 1800


# Reviewer roles. Available backend names live in review/backends.py
# (the BACKENDS dict). To add a new backend (e.g. "codex", "kimi"),
# see the docstring at the top of that file.
#
# PRIMARIES is the list of reviewers that run in parallel on every
# commit. Their findings are consolidated by the arbiter (which also
# clusters duplicates across backends). Configure a single backend
# for classic single-reviewer behavior, or 2+ backends to compare
# them head-to-head — see review/stats_cli.py for analytics.
#
# Examples:
#     # Single reviewer (classic, opencode + claude as fallback):
#     PRIMARIES = [RunnerConfig("opencode", "github-copilot/gpt-5.4")]
#
#     # Two reviewers in parallel (opencode + claude), no fallback
#     # unless BOTH fail:
#     PRIMARIES = [
#         RunnerConfig("opencode", "github-copilot/gpt-5.4"),
#         RunnerConfig("claude", "sonnet"),
#     ]
PRIMARIES: list[RunnerConfig] = [
    RunnerConfig(
        "kimi", "kimi-code/kimi-for-coding"
    ),  # Kimi K2 is the sole primary reviewer; requires `kimi login`. Model alias must match ~/.kimi/config.toml [models.*] key. KimiBackend pins review/agents/kimi-pre-commit-reviewer.yaml automatically. review-note: no startup capability probe by design — this hook only runs on the owner's machine (~/.claude is single-user config), the reviewer fail-opens on backend errors (hook.py:1525+, fallback to claude/sonnet on total primary failure), and a probe per commit would burn ~1-2s for no real safety on a misconfigured machine the user would notice immediately anyway.
    # RunnerConfig("claude", "sonnet"),  # uncomment to add a parallel second reviewer
]

# Safety-net reviewer used ONLY if every primary fails (timeout / rc!=0
# / empty output / unreachable CLI). With one primary this matches
# the classic fallback behavior exactly. With multiple primaries the
# fallback fires only when all of them are dead at once. Set to None
# to disable (then a total-failure run goes fail-open with a warn).
FALLBACK: RunnerConfig | None = RunnerConfig("claude", "sonnet")

# Arbiter validates [CRITICAL] findings (UPHELD/OVERTURN) and, in
# multi-backend mode, also clusters duplicates emitted by different
# primaries. Single source of arbitration regardless of how many
# primaries ran.
ARBITER: RunnerConfig = RunnerConfig(
    "claude", "sonnet", timeout=1800
)  # review-note: user explicitly swapped arbiter from sonnet to Sonnet earlier in this session (commit 31e7575); deliberate cost/latency trade-off owned by the user

# Diff-size routing. Both gates use added-prod-line count
# (count_added_production_lines) — total diff length is no longer
# considered. Tests, docs, configs, lock-files, removals, context lines
# do not consume the budget.
#
# Routing rules (after chunked-review introduction):
#   N < MAX_PROD_LINES               → small-commit path: single combined.md call
#                                        per backend (PRIMARIES). No fan-out.
#   N >= MAX_PROD_LINES, manifest    → chunked path (chunked.py). Fan-out lives
#                                        here as the whole-diff lens layer.
#   N >= MAX_PROD_LINES, no manifest → exit 1, ask writer to generate manifest.
MAX_PROD_LINES = 400  # also used as per-chunk line cap (single source of truth)
MIN_LINES_TO_REVIEW = 1  # commits smaller than this (total) skip review

# Fan-out is now scoped to chunked path only. Setting the threshold equal to
# MAX_PROD_LINES guarantees the small-commit path never triggers fan-out:
# the moment N reaches MAX_PROD_LINES the dispatch routes to chunked path
# instead. The fan-out *code* in hook.py / orchestrator.py is still used —
# chunked path reuses it for the whole-diff lens layer.
FANOUT_THRESHOLD = MAX_PROD_LINES

# Chunked-review limits.
MAX_CHUNKS = 6  # validator rejects manifests with more chunks. Effective
# upper bound on a chunked commit ≈ MAX_CHUNKS * MAX_PROD_LINES.

# Lens whitelist used by the manifest validator. Must match the prompt files
# under review/prompts/ (bugs.md, architecture.md, tests.md). Adding a lens
# means dropping a new prompt file AND updating LENS_APPLICABILITY in hook.py.
ALLOWED_LENSES: frozenset[str] = frozenset({"bugs", "architecture", "tests"})

# Backends that run inside the chunked path. Each chunk gets one reviewer per
# entry, plus the whole-diff lens layer fans out across these same backends.
# Total parallel jobs ≤ MAX_CHUNKS * len(CHUNKED_BACKENDS) +
#                       len(ALLOWED_LENSES) * len(CHUNKED_BACKENDS).
CHUNKED_BACKENDS: list[RunnerConfig] = [
    RunnerConfig("kimi", "kimi-code/kimi-for-coding"),
    # RunnerConfig("claude", "sonnet"),
]

# NOTE: LENS_NAMES is intentionally not in this file. The lens registry
# (LENS_APPLICABILITY in hook.py) owns the order; LENS_NAMES is derived
# from it via tuple(LENS_APPLICABILITY.keys()) so the two cannot drift.


@dataclass(frozen=True)
class CoverageGateConfig:
    """Master kill-switch for the pre-flight gate. Per-repo opt-out lives
    in target pyproject.toml [tool.code_review.coverage_gate].enabled."""

    enabled: bool = True


COVERAGE_GATE: CoverageGateConfig = CoverageGateConfig()

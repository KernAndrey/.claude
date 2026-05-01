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
    timeout: int = 1200


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
    RunnerConfig("opencode", "github-copilot/gpt-5.4"),
    # RunnerConfig("claude", "sonnet"),  # uncomment to enable parallel double-review
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
    "claude", "sonnet", timeout=900
)  # review-note: user explicitly swapped arbiter from Opus to Sonnet earlier in this session (commit 31e7575); deliberate cost/latency trade-off owned by the user

# Diff-size routing. Both gates use added-prod-line count
# (count_added_production_lines) — total diff length is no longer
# considered. Tests, docs, configs, lock-files, removals, context lines
# do not consume the budget.
MAX_PROD_LINES = 300  # commits with more added prod lines are rejected
MIN_LINES_TO_REVIEW = 1  # commits smaller than this (total) skip review
FANOUT_THRESHOLD = 100  # at or above this added prod line count, fan out per-lens

# NOTE: LENS_NAMES is intentionally not in this file. The lens registry
# (LENS_APPLICABILITY in hook.py) owns the order; LENS_NAMES is derived
# from it via tuple(LENS_APPLICABILITY.keys()) so the two cannot drift.

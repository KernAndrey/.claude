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
# To make Claude Code the primary reviewer:
#     PRIMARY = RunnerConfig("claude", "sonnet")
# To disable the fallback entirely:
#     FALLBACK = None
# To run the arbiter on OpenCode:
#     ARBITER = RunnerConfig("opencode", "github-copilot/gpt-5.4", timeout=900)
PRIMARY: RunnerConfig = RunnerConfig("opencode", "github-copilot/gpt-5.4")
FALLBACK: RunnerConfig | None = RunnerConfig("claude", "sonnet")
ARBITER: RunnerConfig = RunnerConfig(
    "claude", "sonnet", timeout=900
)  # review-note: user explicitly swapped arbiter from Opus to Sonnet earlier in this session (commit 31e7575); deliberate cost/latency trade-off owned by the user

# Diff-size routing.
MAX_DIFF_LINES = 3000  # commits larger than this are rejected outright
MIN_LINES_TO_REVIEW = 1  # commits smaller than this skip review
FANOUT_THRESHOLD = 150  # at or above this added-line count, fan out per-lens

# NOTE: LENS_NAMES is intentionally not in this file. The lens registry
# (LENS_APPLICABILITY in hook.py) owns the order; LENS_NAMES is derived
# from it via tuple(LENS_APPLICABILITY.keys()) so the two cannot drift.

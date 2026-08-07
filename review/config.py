"""Tunable settings for the pre-commit code-review hook.

Edit this file to swap models, change reviewer backends, or retune
thresholds. Only `review/hook.py` and `review/test_hook.py` import
from here.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- which kimi model the reviewer uses -----------------------------------
# Must match a [models."…"] key in ~/.kimi-code/config.toml — the LIVE config.
# (~/.kimi/config.toml is versioned but inert; kimi-code no longer reads it.)
# review-note: user explicitly repinned this from kimi-code/k3 to K2.7 Code
# ("kimi-for-coding") — K3 is strong but expensive and overkill for review.
# Deliberate cost trade-off owned by the user. Applies to both review paths
# (PRIMARIES = small-commit path, CHUNKED_BACKENDS = chunked path). K2.7 caps
# at 256k context vs K3's 1M, which is ample here: a chunked commit is bounded
# by MAX_CHUNKS * MAX_PROD_LINES added production lines. The kimi *delegation*
# skill (~/.claude/skills/kimi) still pins K3 — different subsystem.
_KIMI_MODEL = "kimi-code/kimi-for-coding"

# --- which codex model the reviewer would use (not currently wired) --------
# The CodexBackend is registered and tested but no RunnerConfig uses it yet —
# flipping to codex is a one-line uncomment below (PRIMARIES and/or
# CHUNKED_BACKENDS). Requires `codex login`.
#
# Model ids come from `~/.codex/models_cache.json` and are gated by the auth
# tier. As of 2026-08-07 on `auth_mode = "chatgpt"` the cache lists:
# gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4-mini. A subscription change can
# change that list — re-check the cache before trusting this constant.
_CODEX_MODEL = "gpt-5.6-terra"


@dataclass(frozen=True)
class RunnerConfig:
    """One backend invocation: which CLI, which model, how long to wait.

    ``backend`` must match a key in ``backends.BACKENDS``. Validated at
    ``main()`` startup by ``hook._verify_runner_configs``.
    """

    backend: str
    model: str
    # 2400s (40 min) per single backend invocation. Chunked-path jobs run
    # fully in parallel (chunked.py: max_workers=len(jobs)), so each kimi job
    # is independently bounded by this; the slowest observed single job is
    # ~1200-1300s and climbs under API concurrency. A timed-out chunk job
    # emits a synthetic [CRITICAL] (no fallback on the chunked path) → false
    # BLOCK, so the headroom matters. Not higher: a genuinely hung job should
    # not hold a commit hostage for an hour.
    timeout: int = 2400


# Reviewer roles. Available backend names live in review/backends.py
# (the BACKENDS dict). To add a new backend, see the docstring at the
# top of that file.
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
        "codex", _CODEX_MODEL
    ),  # Codex CLI is the sole primary reviewer; requires `codex login`. Model slug must match a `slug` in ~/.codex/models_cache.json. Read-only containment is native — CodexBackend pins `--sandbox read-only`, no external hook or agent file needed (unlike opencode/kimi). review-note: user explicitly switched here from kimi on 2026-08-07 after buying a Codex subscription; kimi stays registered and one uncomment away. Same no-startup-probe rationale as before: this hook runs only on the owner's machine and fails open to FALLBACK.
    # RunnerConfig("kimi", _KIMI_MODEL),  # uncomment to restore Kimi (requires `kimi login`; alias must match a ~/.kimi-code/config.toml [models.*] key)
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
    "claude", "sonnet", timeout=2400
)  # review-note: user explicitly swapped arbiter from sonnet to Sonnet earlier in this session (commit 31e7575); deliberate cost/latency trade-off owned by the user. timeout=2400 matches the reviewer budget (see RunnerConfig.timeout).

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

# Pre-review chunk-reviews several BIG commit groups in parallel, and each
# chunked review itself fans out uncapped (chunked.py: max_workers=len(jobs)).
# Without a ceiling the nested product (groups × jobs-per-group) can spawn
# 50+ concurrent reviewer subprocesses and rate-limit/melt the backend. This
# caps how many big groups chunk-review AT ONCE; the realistic 2-3-big-commit
# feature stays fully parallel (that's the whole point — overlap the multi-round
# convergence latency), while a pathological many-big-group plan degrades to
# this many at a time instead of all at once. Small (single-call) groups are
# unaffected. Raise it if your backend has the headroom.
PREREVIEW_MAX_CONCURRENT_CHUNKED = 4

# Lens whitelist used by the manifest validator. Must match the prompt files
# under review/prompts/ (bugs.md, architecture.md, tests.md). Adding a lens
# means dropping a new prompt file AND updating LENS_APPLICABILITY in hook.py.
ALLOWED_LENSES: frozenset[str] = frozenset({"bugs", "architecture", "tests"})

# Backends that run inside the chunked path. Each chunk gets one reviewer per
# entry, plus the whole-diff lens layer fans out across these same backends.
# Total parallel jobs ≤ MAX_CHUNKS * len(CHUNKED_BACKENDS) +
#                       len(ALLOWED_LENSES) * len(CHUNKED_BACKENDS).
CHUNKED_BACKENDS: list[RunnerConfig] = [
    RunnerConfig("kimi", _KIMI_MODEL),
    # RunnerConfig("claude", "sonnet"),
    # RunnerConfig("codex", _CODEX_MODEL),  # deliberately still kimi while codex proves itself on the small-commit path: the chunked path has NO fallback (see RunnerConfig.timeout), so a codex error/timeout here emits a synthetic [CRITICAL] → false BLOCK, whereas in PRIMARIES the same failure degrades quietly to FALLBACK. Swap once codex has a few clean chunked-size commits behind it.
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

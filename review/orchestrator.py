"""Multi-backend orchestration for the pre-commit review.

Runs every entry in ``config.PRIMARIES`` in parallel against the same
diff, captures per-backend status / latency / findings, and triggers
``config.FALLBACK`` only when **every** primary failed. The output of
this module is a list of :class:`BackendReviewResult` — consolidation
(LLM-based dedup + arbiter) lives in ``consolidation.py``.

Design notes:

- Each primary is invoked **without** its own per-call fallback. The
  fallback is a process-wide safety net, not a per-primary backstop.
  Rationale: with N>1 primaries the failure of one is partial
  redundancy — the others still produce findings. Fallback fires only
  when nothing usable came back, which preserves classic single-
  backend behavior at N==1.

- Each backend independently chooses single-call vs fan-out using the
  existing ``count_added_production_lines`` / ``FANOUT_THRESHOLD``
  rule, so adding a second backend does not change diff routing.

- Finding IDs get a backend prefix only in multi-backend mode
  (``len(PRIMARIES) > 1``). At N==1 IDs stay bare ``F1, F2, ...`` to
  preserve backwards-compatible markdown-log layout.

- All hook helpers are imported lazily to avoid an import cycle
  (``hook`` imports ``orchestrator``).
"""

from __future__ import annotations

import concurrent.futures
import subprocess
import time
from dataclasses import dataclass

from config import FALLBACK, FANOUT_THRESHOLD, PRIMARIES, RunnerConfig


@dataclass(frozen=True)
class BackendReviewResult:
    """One backend's review outcome — fed to consolidation + stats."""

    cfg: RunnerConfig
    used_backend_name: str  # cfg.backend (no fallback at this layer)
    status: str  # "ok" | "timeout" | "error" | "empty" | "skipped_no_lens"
    review_text: str  # tagged aggregated text (post assign_finding_ids); empty on failure
    raw_findings: list[dict]  # [{"id": "<prefix>-F1" or "F1", "line": "..."}]
    per_lens: list[dict] | None  # fan-out detail; None for single-call or failure
    error: str | None
    started_at: float  # time.monotonic() at backend dispatch
    ended_at: float
    fallback_used: bool  # True only when this result came from FALLBACK


def run_multi_backend(
    diff: str,
    files: str,
    is_merge: bool,
) -> list[BackendReviewResult]:
    """Run all PRIMARIES in parallel; fall back only on total failure.

    Returns results in PRIMARIES order, with the FALLBACK result (if it
    fired) appended last. Never raises — every failure mode is folded
    into a ``BackendReviewResult`` with ``status != "ok"``.
    """
    multi_mode = len(PRIMARIES) > 1

    results: list[BackendReviewResult] = []
    if PRIMARIES:
        max_workers = max(len(PRIMARIES), 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures: dict[concurrent.futures.Future, RunnerConfig] = {
                ex.submit(
                    _run_review_for_backend,
                    cfg,
                    diff,
                    files,
                    is_merge,
                    cfg.backend if multi_mode else "",
                    False,
                ): cfg
                for cfg in PRIMARIES
            }
            for fut in concurrent.futures.as_completed(futures):
                cfg = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    results.append(_failure_result(cfg, f"{type(exc).__name__}: {exc}"))

        results.sort(key=lambda r: PRIMARIES.index(r.cfg))

    if not _any_success(results) and FALLBACK is not None:
        fb_prefix = FALLBACK.backend if multi_mode else ""
        results.append(_run_review_for_backend(FALLBACK, diff, files, is_merge, fb_prefix, True))

    return results


def total_failure_reason(results: list[BackendReviewResult]) -> str | None:
    """Human-readable description of why fallback fired (or None if it didn't).

    Used by ``stats.build_run_stats`` to populate ``fallback.reason``.
    Examines only non-fallback results so a fallback that itself failed
    still reports the *primary* reasons that caused the safety net to
    fire — those are what the user wants to see when comparing backends.
    """
    primary_results = [r for r in results if not r.fallback_used]
    if not primary_results:
        return None
    if _any_success(primary_results):
        return None
    parts: list[str] = []
    for r in primary_results:
        label = f"{r.cfg.backend}/{r.cfg.model}"
        parts.append(f"{label} {r.status}: {r.error or 'unknown'}")
    n = len(primary_results)
    return f"all {n} primary/primaries failed: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# Per-backend execution
# ---------------------------------------------------------------------------


def _run_review_for_backend(
    cfg: RunnerConfig,
    diff: str,
    files: str,
    is_merge: bool,
    prefix: str,
    is_fallback: bool,
) -> BackendReviewResult:
    """Single-call or fan-out review with one backend, no per-call fallback.

    Returns a populated :class:`BackendReviewResult`. Never raises;
    all failure modes are encoded in ``status`` / ``error``.
    """
    # Late import — hook.py imports orchestrator, so module-level imports
    # here would create a cycle.
    from hook import (
        GLOBAL_PROMPT,
        applicable_lenses,
        assign_finding_ids,
        build_system_prompt,
        build_user_prompt,
        count_added_production_lines,
    )

    started = time.monotonic()

    applicable = applicable_lenses(files)
    if not applicable:
        return _terminal_result(
            cfg,
            "skipped_no_lens",
            "no applicable lens for this file set",
            started,
            is_fallback,
        )

    added = count_added_production_lines(diff)
    use_fanout = added >= FANOUT_THRESHOLD
    user_prompt = build_user_prompt(diff, files, is_merge)

    if use_fanout:
        per_lens = _run_fanout_for_backend(cfg, user_prompt, applicable)
        ok_lenses = [d for d in per_lens if d["status"] == "ok"]
        if not ok_lenses:
            return BackendReviewResult(
                cfg=cfg,
                used_backend_name=cfg.backend,
                status="error",
                review_text="",
                raw_findings=[],
                per_lens=per_lens,
                error="all lenses failed",
                started_at=started,
                ended_at=time.monotonic(),
                fallback_used=is_fallback,
            )
        from hook import _aggregate_lens_outputs  # late import (cycle)

        aggregated = _aggregate_lens_outputs(per_lens)
        tagged, findings = assign_finding_ids(aggregated, prefix=prefix)
        return BackendReviewResult(
            cfg=cfg,
            used_backend_name=cfg.backend,
            status="ok",
            review_text=tagged,
            raw_findings=findings,
            per_lens=per_lens,
            error=None,
            started_at=started,
            ended_at=time.monotonic(),
            fallback_used=is_fallback,
        )

    # Single-call path
    system_prompt = build_system_prompt()
    if not system_prompt:
        return _terminal_result(
            cfg,
            "error",
            f"no {GLOBAL_PROMPT.name}",
            started,
            is_fallback,
        )
    return _invoke_single_call(cfg, system_prompt, user_prompt, prefix, started, is_fallback)


def _invoke_single_call(
    cfg: RunnerConfig,
    system_prompt: str,
    user_prompt: str,
    prefix: str,
    started: float,
    is_fallback: bool,
) -> BackendReviewResult:
    """One subprocess call to ``cfg`` backend, wrap the outcome."""
    from hook import assign_finding_ids, run_reviewer

    try:
        review, stderr, rc = run_reviewer(cfg, system_prompt, user_prompt)
    except subprocess.TimeoutExpired:
        return _terminal_result(
            cfg,
            "timeout",
            f"{cfg.backend} timeout after {cfg.timeout}s",
            started,
            is_fallback,
        )
    except (FileNotFoundError, OSError) as exc:
        return _terminal_result(
            cfg,
            "error",
            f"{cfg.backend} unreachable: {exc}",
            started,
            is_fallback,
        )

    if rc != 0:
        return _terminal_result(
            cfg,
            "error",
            f"{cfg.backend} rc={rc} stderr={stderr[:200] if stderr else ''}",
            started,
            is_fallback,
        )
    if not review or not review.strip():
        return _terminal_result(
            cfg,
            "empty",
            f"{cfg.backend} empty output",
            started,
            is_fallback,
        )

    tagged, findings = assign_finding_ids(review, prefix=prefix)
    return BackendReviewResult(
        cfg=cfg,
        used_backend_name=cfg.backend,
        status="ok",
        review_text=tagged,
        raw_findings=findings,
        per_lens=None,
        error=None,
        started_at=started,
        ended_at=time.monotonic(),
        fallback_used=is_fallback,
    )


def _run_fanout_for_backend(
    cfg: RunnerConfig,
    user_prompt: str,
    applicable: list[str],
) -> list[dict]:
    """Run every applicable lens through one backend (no per-lens fallback).

    Mirrors ``hook.run_fanout``'s parallelism + ``per_lens`` shape, but
    pinned to a single backend. Adds ``started_at`` / ``ended_at`` to
    each lens dict so downstream stats can report per-lens latency.
    """
    from hook import LENS_NAMES, build_lens_system_prompt, run_reviewer

    skipped = [n for n in LENS_NAMES if n not in applicable]
    per_lens: list[dict] = [
        {
            "name": name,
            "status": "skipped_by_router",
            "review": "",
            "reviewer": None,
            "error": "no applicable files for this lens",
            "started_at": 0.0,
            "ended_at": 0.0,
        }
        for name in skipped
    ]

    if not applicable:
        return per_lens

    def _run_lens(lens_name: str) -> dict:
        lens_started = time.monotonic()
        system_prompt = build_lens_system_prompt(lens_name)
        if not system_prompt:
            return _lens_result(lens_name, "error", "", cfg.backend, f"prompts/{lens_name}.md missing", lens_started)
        try:
            review, stderr, rc = run_reviewer(cfg, system_prompt, user_prompt)
        except subprocess.TimeoutExpired:
            return _lens_result(lens_name, "timeout", "", cfg.backend, f"{cfg.backend} timeout", lens_started)
        except (FileNotFoundError, OSError) as exc:
            return _lens_result(lens_name, "error", "", cfg.backend, f"unreachable: {exc}", lens_started)
        if rc != 0 or not review or not review.strip():
            return _lens_result(
                lens_name,
                "error",
                review,
                cfg.backend,
                f"rc={rc} stderr={stderr[:200] if stderr else ''}",
                lens_started,
            )
        return _lens_result(lens_name, "ok", review, cfg.backend, "", lens_started)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(applicable)) as ex:
        futures = {ex.submit(_run_lens, name): name for name in applicable}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                per_lens.append(fut.result())
            except Exception as exc:
                per_lens.append(
                    _lens_result(
                        name,
                        "error",
                        "",
                        cfg.backend,
                        f"{type(exc).__name__}: {exc}",
                        time.monotonic(),
                    )
                )

    per_lens.sort(key=lambda d: LENS_NAMES.index(d["name"]))
    return per_lens


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def _any_success(results: list[BackendReviewResult]) -> bool:
    """Has at least one backend produced a non-empty review?"""
    return any(r.status == "ok" and r.review_text and r.review_text.strip() for r in results)


def _terminal_result(
    cfg: RunnerConfig,
    status: str,
    error: str,
    started: float,
    is_fallback: bool,
) -> BackendReviewResult:
    """Build a non-success :class:`BackendReviewResult` with timing."""
    return BackendReviewResult(
        cfg=cfg,
        used_backend_name=cfg.backend,
        status=status,
        review_text="",
        raw_findings=[],
        per_lens=None,
        error=error,
        started_at=started,
        ended_at=time.monotonic(),
        fallback_used=is_fallback,
    )


def _failure_result(cfg: RunnerConfig, error: str) -> BackendReviewResult:
    """Synthetic result for unexpected exceptions in the worker pool."""
    now = time.monotonic()
    return BackendReviewResult(
        cfg=cfg,
        used_backend_name=cfg.backend,
        status="error",
        review_text="",
        raw_findings=[],
        per_lens=None,
        error=error,
        started_at=now,
        ended_at=now,
        fallback_used=False,
    )


def _lens_result(
    name: str,
    status: str,
    review: str,
    reviewer: str | None,
    error: str,
    started_at: float,
) -> dict:
    """Per-lens dict shape used inside :class:`BackendReviewResult.per_lens`."""
    return {
        "name": name,
        "status": status,
        "review": review,
        "reviewer": reviewer,
        "error": error,
        "started_at": started_at,
        "ended_at": time.monotonic(),
    }

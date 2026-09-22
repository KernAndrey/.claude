"""Per-run review statistics — JSON sidecar + rolling JSONL aggregate.

Two outputs are written for every commit that reaches the multi-backend
orchestrator (i.e. the gate did not short-circuit on diff size):

- ``logs/{timestamp}_{project}_{verdict}.stats.json`` — full per-run
  detail. Sits next to the existing markdown log so the two are easy
  to correlate.
- ``logs/stats.jsonl`` — append-only one-line-per-run aggregate. Lets
  the user run ``jq`` / ``stats_cli.py`` analytics across runs to
  compare backends on latency, reliability, find-rate, upheld-rate.

The schema is versioned (``schema_version`` field) so future-me can
tell whether a stats.jsonl row matches the current parser.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from consolidation import ConsolidationResult, FindingCluster
from orchestrator import BackendReviewResult, total_failure_reason

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DiffStats:
    """Numeric facts about the diff being reviewed."""

    total_lines: int
    added_prod_lines: int
    files_count: int


# ---------------------------------------------------------------------------
# Build the stats object
# ---------------------------------------------------------------------------


def build_run_stats(
    results: list[BackendReviewResult],
    consolidation: ConsolidationResult,
    diff_stats: DiffStats,
    verdict: str,
    project: str,
    timestamp: str,
) -> dict[str, Any]:
    """Assemble the per-run stats object matching schema_version 1.

    Pure function: no I/O, no clocks. Output is JSON-serialisable.
    """
    backends = [_backend_block(r, consolidation.clusters) for r in results]
    cons_block = _consolidation_block(consolidation)
    fb_block = _fallback_block(results)

    mode = _detect_mode(results)

    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp,
        "project": project,
        "verdict": verdict,
        "mode": mode,
        "diff": {
            "total_lines": diff_stats.total_lines,
            "added_prod_lines": diff_stats.added_prod_lines,
            "files_count": diff_stats.files_count,
        },
        "backends": backends,
        "consolidation": cons_block,
        "fallback": fb_block,
    }


# ---------------------------------------------------------------------------
# Save: sidecar JSON + rolling JSONL append
# ---------------------------------------------------------------------------


def save(
    stats: dict[str, Any],
    log_path_md: Path,
    aggregate_path: Path,
) -> None:
    """Write the sidecar JSON and append one row to the rolling JSONL.

    Failures are swallowed (mirrors ``hook.save_log`` behavior — stats
    must never break a commit). Any partial write is acceptable: jq /
    stats_cli treat malformed lines as skipped.
    """
    sidecar_path = log_path_md.with_suffix(".stats.json")
    serialized = json.dumps(stats, indent=2, sort_keys=True, ensure_ascii=False)
    try:
        sidecar_path.write_text(serialized, encoding="utf-8")
    except OSError:
        pass
    try:
        # O_APPEND on POSIX is atomic for writes < PIPE_BUF (~4 KB).
        # One stats line is ~1-2 KB, so concurrent appenders cannot
        # corrupt each other. Pre-commit is single-process anyway.
        aggregate_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(stats, ensure_ascii=False) + "\n"
        with open(aggregate_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Per-backend block
# ---------------------------------------------------------------------------


def _backend_block(
    r: BackendReviewResult,
    clusters: list[FindingCluster],
) -> dict[str, Any]:
    """One entry of the ``backends`` array."""
    own_finding_ids = {f["id"] for f in r.raw_findings}

    # Map clusters that include this backend's findings.
    own_clusters = [c for c in clusters if any(mid in own_finding_ids for mid in c.member_ids)]
    consensus = sum(1 for c in own_clusters if len(set(c.contributors) - {""}) > 1)
    solo = len(own_clusters) - consensus
    upheld = sum(1 for c in own_clusters if c.upheld)
    overturned = len(own_clusters) - upheld

    raw_critical = len(r.raw_findings)
    raw_warning = _count_warning_lines(r.review_text)

    return {
        "config": {
            "backend": r.cfg.backend,
            "model": r.cfg.model,
            "timeout": r.cfg.timeout,
        },
        "used_backend": r.used_backend_name,
        "status": r.status,
        "fallback_used": r.fallback_used,
        "duration_seconds": round(r.ended_at - r.started_at, 3),
        "raw_findings": {"critical": raw_critical, "warning": raw_warning},
        "unique_findings_in_clusters": len(own_clusters),
        "consensus_findings": consensus,
        "solo_findings": solo,
        "upheld_findings": upheld,
        "overturned_findings": overturned,
        "error": r.error,
        "per_lens": _per_lens_block(r.per_lens),
    }


def _per_lens_block(per_lens: list[dict] | None) -> list[dict] | None:
    """Minimal lens detail for stats — name / status / duration only."""
    if per_lens is None:
        return None
    out: list[dict] = []
    for lens in per_lens:
        started = lens.get("started_at", 0.0) or 0.0
        ended = lens.get("ended_at", 0.0) or 0.0
        out.append(
            {
                "lens": lens["name"],
                "status": lens["status"],
                "duration_seconds": round(max(ended - started, 0.0), 3),
            }
        )
    return out


def _count_warning_lines(review_text: str) -> int:
    """Count [WARNING] finding lines in the (already-tagged) review text."""
    if not review_text:
        return 0
    # Late import keeps the cycle tame (hook -> stats -> hook would
    # otherwise need to import on module load).
    from hook import extract_warning_lines

    return len(extract_warning_lines(review_text))


# ---------------------------------------------------------------------------
# Consolidation block
# ---------------------------------------------------------------------------


def _consolidation_block(c: ConsolidationResult) -> dict[str, Any]:
    """The ``consolidation`` field of the stats object."""
    total = len(c.clusters)
    upheld = len(c.upheld_clusters)
    overturned = total - upheld
    consensus_clusters = sum(1 for cl in c.clusters if len({x for x in cl.contributors if x}) > 1)
    consensus_rate = (consensus_clusters / total) if total else 0.0
    return {
        "total_clusters": total,
        "upheld_clusters": upheld,
        "overturned_clusters": overturned,
        "consensus_rate": round(consensus_rate, 3),
        "arbiter": {
            "status": c.arbiter_status,
            "duration_seconds": round(
                max(c.arbiter_ended_at - c.arbiter_started_at, 0.0),
                3,
            ),
            "error": c.arbiter_error,
        },
    }


# ---------------------------------------------------------------------------
# Fallback block
# ---------------------------------------------------------------------------


def _fallback_block(results: list[BackendReviewResult]) -> dict[str, Any]:
    """Top-level ``fallback`` block — was the safety net needed?"""
    triggered = any(r.fallback_used for r in results)
    return {
        "triggered": triggered,
        "reason": total_failure_reason(results) if triggered else None,
    }


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------


def _detect_mode(results: list[BackendReviewResult]) -> str:
    """Tag the run as 'single-call' / 'fanout' / 'mixed' / 'no-op'.

    Mostly informational for stats_cli filters. ``mixed`` is an oddity
    — it implies different backends took different paths, which can
    only happen if FANOUT_THRESHOLD changes mid-run (it doesn't).
    """
    primary_results = [r for r in results if not r.fallback_used]
    if not primary_results:
        primary_results = results
    modes = Counter(
        "fanout" if r.per_lens is not None else "single-call"
        for r in primary_results
        if r.status in {"ok", "error"}  # exclude purely terminal states
    )
    if not modes:
        return "no-op"
    if len(modes) == 1:
        return next(iter(modes))
    return "mixed"


# ---------------------------------------------------------------------------
# Aggregation across rolling JSONL — used by stats_cli
# ---------------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a stats.jsonl file, skipping malformed lines.

    Returns rows in file order (chronological). Missing file → empty
    list (callers print a friendlier message themselves).
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def aggregate_per_backend(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Roll up across runs into per-backend metrics.

    Key shape: ``"opencode/github-copilot/gpt-5.4"`` so the same
    backend with two different models stays separate.
    """
    bucket: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for b in row.get("backends", []):
            cfg = b.get("config", {})
            key = f"{cfg.get('backend', '?')}/{cfg.get('model', '?')}"
            bucket.setdefault(key, []).append(b)

    out: dict[str, dict[str, Any]] = {}
    for key, entries in bucket.items():
        durations = [float(e.get("duration_seconds", 0.0)) for e in entries if e.get("status") in {"ok", "timeout"}]
        ok_count = sum(1 for e in entries if e.get("status") == "ok")
        success_rate = (ok_count / len(entries)) if entries else 0.0
        upheld_total = sum(int(e.get("upheld_findings", 0)) for e in entries)
        overturned_total = sum(int(e.get("overturned_findings", 0)) for e in entries)
        verdict_total = upheld_total + overturned_total
        upheld_rate = (upheld_total / verdict_total) if verdict_total else 0.0
        consensus_total = sum(int(e.get("consensus_findings", 0)) for e in entries)
        solo_total = sum(int(e.get("solo_findings", 0)) for e in entries)
        clusters_total = consensus_total + solo_total
        solo_rate = (solo_total / clusters_total) if clusters_total else 0.0
        avg_findings = (
            sum(int(e.get("raw_findings", {}).get("critical", 0)) for e in entries) / len(entries) if entries else 0.0
        )

        out[key] = {
            "runs": len(entries),
            "ok_count": ok_count,
            "success_rate": round(success_rate, 4),
            "p50_latency_seconds": round(median(durations), 3) if durations else 0.0,
            "p95_latency_seconds": round(_percentile(durations, 95), 3) if durations else 0.0,
            "avg_critical_findings": round(avg_findings, 3),
            "upheld_rate": round(upheld_rate, 4),
            "solo_rate": round(solo_rate, 4),
            "consensus_findings_total": consensus_total,
            "solo_findings_total": solo_total,
            "upheld_total": upheld_total,
            "overturned_total": overturned_total,
        }
    return out


def _percentile(values: list[float], percent: float) -> float:
    """Simple linear-interp percentile; values do not need to be sorted."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (percent / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


# ---------------------------------------------------------------------------
# Default paths (overridable for tests)
# ---------------------------------------------------------------------------


def default_aggregate_path() -> Path:
    """Path to logs/stats.jsonl in the canonical review/logs/ directory."""
    return Path(os.path.expanduser("~/.claude/review/logs")) / "stats.jsonl"

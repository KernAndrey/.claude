"""Consolidation: dedup findings across backends + arbiter validation.

In **multi-backend mode** (``len(PRIMARIES) > 1``) the arbiter receives
all findings from every backend (with backend-prefixed IDs like
``opencode-F1`` / ``claude-F2``) and emits two passes of output:

1. ``[CLUSTER C1] opencode-F1, claude-F2`` — groups duplicates.
2. ``[UPHELD] C1`` / ``[OVERTURN] C2`` — verdicts per cluster.

In **single-backend mode** we shortcut to the legacy arbiter
(``arbiter.md``, capturing bare ``F\\d+`` IDs) so existing behavior
and tests stay byte-for-byte stable. The result is wrapped into the
same :class:`ConsolidationResult` shape via singleton clusters.

The output of this module feeds two consumers:

- ``hook.main()`` — uses ``upheld_clusters`` to decide BLOCK vs OK
- ``stats.build_run_stats()`` — uses ``clusters`` + per-finding
  membership to compute consensus / solo counts per backend.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from config import ARBITER, PRIMARIES
from orchestrator import BackendReviewResult


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FindingCluster:
    """One equivalence class of findings (one or more contributors)."""

    cluster_id: str  # "C1", "C2", ...
    member_ids: tuple[str, ...]  # finding IDs that belong here, in input order
    contributors: tuple[str, ...]  # unique backend names (alphabetical)
    canonical_line: str  # the chosen "best" finding line for display
    upheld: bool


@dataclass(frozen=True)
class ConsolidationResult:
    clusters: list[FindingCluster]
    upheld_clusters: list[FindingCluster]
    arbiter_status: str  # "ran" | "skipped_no_findings" | "error" | "timeout" | "unavailable"
    arbiter_error: str | None
    arbiter_raw_output: str
    arbiter_started_at: float  # 0.0 if arbiter was skipped
    arbiter_ended_at: float


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def consolidate(
    results: list[BackendReviewResult],
    diff: str,
) -> ConsolidationResult:
    """Cluster duplicates across backends, then validate each cluster.

    For ``len(PRIMARIES) == 1`` the legacy single-backend arbiter is
    used (no prefixed IDs, ``arbiter.md`` prompt). Otherwise the
    multi-backend arbiter (``arbiter_multi.md``) does both clustering
    and verdicts.
    """
    all_findings = _gather_findings(results)
    if not all_findings:
        return ConsolidationResult(
            clusters=[],
            upheld_clusters=[],
            arbiter_status="skipped_no_findings",
            arbiter_error=None,
            arbiter_raw_output="",
            arbiter_started_at=0.0,
            arbiter_ended_at=0.0,
        )

    if len(PRIMARIES) <= 1:
        return _consolidate_single_backend(diff, all_findings)
    return _consolidate_multi_backend(diff, all_findings)


# ---------------------------------------------------------------------------
# Single-backend path (delegates to legacy run_arbiter)
# ---------------------------------------------------------------------------


def _consolidate_single_backend(
    diff: str,
    findings: list[dict],
) -> ConsolidationResult:
    """Run the existing single-backend arbiter; wrap result in clusters."""
    from hook import run_arbiter

    started = time.monotonic()
    arb = run_arbiter(diff, findings)
    ended = time.monotonic()

    upheld_ids: set[str] = arb["upheld_ids"]
    clusters: list[FindingCluster] = []
    for idx, finding in enumerate(findings, start=1):
        fid = finding["id"]
        upheld = fid in upheld_ids
        clusters.append(
            FindingCluster(
                cluster_id=f"C{idx}",
                member_ids=(fid,),
                contributors=(_backend_of(fid),),
                canonical_line=finding["line"],
                upheld=upheld,
            )
        )

    raw_status = arb.get("status", "ok")
    if raw_status == "ok":
        arbiter_status = "ran"
    elif raw_status == "skipped":
        arbiter_status = "skipped_no_findings"
    elif "timeout" in (arb.get("error") or "").lower():
        arbiter_status = "timeout"
    else:
        arbiter_status = "error" if raw_status == "unavailable" else raw_status

    return ConsolidationResult(
        clusters=clusters,
        upheld_clusters=[c for c in clusters if c.upheld],
        arbiter_status=arbiter_status,
        arbiter_error=arb.get("error") or None,
        arbiter_raw_output=arb.get("raw", ""),
        arbiter_started_at=started,
        arbiter_ended_at=ended,
    )


# ---------------------------------------------------------------------------
# Multi-backend arbiter
# ---------------------------------------------------------------------------


_ARBITER_MULTI_PROMPT_PATH = Path.home() / ".claude" / "review" / "prompts" / "arbiter_multi.md"
_CLUSTER_RE = re.compile(
    r"^\s*\[CLUSTER\s+(C\d+)\]\s*(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"^\s*\[(UPHELD|OVERTURN)\]\s*(C\d+)\b",
    re.MULTILINE | re.IGNORECASE,
)
_FINDING_ID_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*-?F\d+")


def _consolidate_multi_backend(
    diff: str,
    findings: list[dict],
) -> ConsolidationResult:
    """Drive the multi-backend arbiter and parse its CLUSTER + verdict output."""
    from hook import info, read_file, run_reviewer, warn

    system_prompt = read_file(_ARBITER_MULTI_PROMPT_PATH)
    started = time.monotonic()
    if not system_prompt:
        warn(f"Arbiter prompt {_ARBITER_MULTI_PROMPT_PATH} missing — upholding all findings")
        ended = time.monotonic()
        return _fail_open(findings, "unavailable", f"{_ARBITER_MULTI_PROMPT_PATH} not found", "", started, ended)

    user_prompt = (
        "## Full staged diff\n\n"
        "```diff\n" + diff + "\n```\n\n"
        "## Findings to cluster + arbitrate (in order):\n\n" + "\n".join(f["line"] for f in findings) + "\n\n"
        "Output cluster lines first (`[CLUSTER C1] <ids>`), then one "
        "verdict line per cluster (`[UPHELD] C1` or `[OVERTURN] C1`), "
        "then the `Summary:` terminator. No other content."
    )

    info(
        f"Multi-backend arbiter: clustering+validating {len(findings)} finding(s) "
        f"with {ARBITER.backend} {ARBITER.model} (may take 30-180s)..."
    )
    try:
        raw, stderr, rc = run_reviewer(ARBITER, system_prompt, user_prompt)
    except subprocess.TimeoutExpired:
        warn(f"Multi-backend arbiter timed out after {ARBITER.timeout}s — upholding all findings")
        return _fail_open(findings, "timeout", "timeout", "", started, time.monotonic())
    except (FileNotFoundError, OSError) as exc:
        warn(f"Multi-backend arbiter unreachable ({exc}) — upholding all findings")
        return _fail_open(findings, "unavailable", f"unreachable: {exc}", "", started, time.monotonic())

    ended = time.monotonic()

    if rc != 0 or not raw or not raw.strip():
        warn(f"Multi-backend arbiter failed (rc={rc}) — upholding all findings")
        return _fail_open(
            findings, "error", f"rc={rc} stderr={stderr[:200] if stderr else ''}", raw or "", started, ended
        )

    clusters = parse_multi_arbiter_output(raw, findings)
    info(
        f"Multi-backend arbiter: {sum(1 for c in clusters if c.upheld)} UPHELD, "
        f"{sum(1 for c in clusters if not c.upheld)} OVERTURN, "
        f"{len(clusters)} cluster(s) total."
    )
    return ConsolidationResult(
        clusters=clusters,
        upheld_clusters=[c for c in clusters if c.upheld],
        arbiter_status="ran",
        arbiter_error=None,
        arbiter_raw_output=raw,
        arbiter_started_at=started,
        arbiter_ended_at=ended,
    )


# ---------------------------------------------------------------------------
# Parser — `[CLUSTER C1] ...` + `[UPHELD] C1` lines
# ---------------------------------------------------------------------------


def parse_multi_arbiter_output(
    raw: str,
    findings: list[dict],
) -> list[FindingCluster]:
    """Parse the multi-backend arbiter output into ``FindingCluster`` objects.

    Robust to malformed output:

    - A finding ID that the arbiter omitted from every cluster gets its
      own singleton cluster with ``upheld=True`` (fail-open: better to
      block than to silently drop a flagged defect).
    - A cluster with no verdict line is treated as ``UPHELD`` (same
      stance as the legacy single-backend arbiter parser).
    - Cluster IDs the arbiter invented but didn't ground in any
      finding are dropped silently — they would have no member to
      uphold.
    """
    finding_index: dict[str, dict] = {f["id"]: f for f in findings}
    cluster_members: dict[str, list[str]] = {}
    seen_in_some_cluster: set[str] = set()

    for m in _CLUSTER_RE.finditer(raw):
        cid = m.group(1).upper()
        ids_part = m.group(2)
        ids: list[str] = []
        for token in _FINDING_ID_TOKEN_RE.findall(ids_part):
            if token in finding_index and token not in seen_in_some_cluster:
                ids.append(token)
                seen_in_some_cluster.add(token)
        if ids:
            # Last writer wins on duplicate cluster IDs — pathological
            # but harmless; we just take the latest grouping.
            cluster_members[cid] = ids

    verdicts: dict[str, bool] = {}
    for m in _VERDICT_RE.finditer(raw):
        verdict = m.group(1).upper()
        cid = m.group(2).upper()
        verdicts[cid] = verdict == "UPHELD"

    clusters: list[FindingCluster] = []
    for cid, member_ids in cluster_members.items():
        upheld = verdicts.get(cid, True)  # fail-open: missing verdict → UPHELD
        clusters.append(_make_cluster(cid, member_ids, finding_index, upheld))

    # Fail-open singleton for each finding the arbiter forgot to cluster.
    next_index = _next_cluster_index(cluster_members.keys())
    for finding in findings:
        if finding["id"] not in seen_in_some_cluster:
            cid = f"C{next_index}"
            next_index += 1
            clusters.append(_make_cluster(cid, [finding["id"]], finding_index, upheld=True))

    return clusters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gather_findings(results: list[BackendReviewResult]) -> list[dict]:
    """Flatten findings from successful backends in PRIMARIES + fallback order."""
    out: list[dict] = []
    for r in results:
        if r.status == "ok":
            out.extend(r.raw_findings)
    return out


def _backend_of(finding_id: str) -> str:
    """Extract the backend prefix from a finding ID (or '' if unprefixed)."""
    if "-F" in finding_id:
        return finding_id.rsplit("-F", 1)[0]
    return ""


def _make_cluster(
    cluster_id: str,
    member_ids: list[str],
    finding_index: dict[str, dict],
    upheld: bool,
) -> FindingCluster:
    """Build a :class:`FindingCluster` from member IDs.

    ``canonical_line`` picks the longest finding line — a rough proxy
    for "most informative" without an LLM second-pass. The contributors
    tuple is alphabetical and deduped so equality / hashing is stable.
    """
    members = [finding_index[mid] for mid in member_ids if mid in finding_index]
    contributors = tuple(sorted({_backend_of(mid) for mid in member_ids}))
    canonical = ""
    if members:
        canonical = max(members, key=lambda f: len(f["line"]))["line"]
    return FindingCluster(
        cluster_id=cluster_id,
        member_ids=tuple(member_ids),
        contributors=contributors,
        canonical_line=canonical,
        upheld=upheld,
    )


def _next_cluster_index(existing_ids: object) -> int:
    """Compute the next free `C<n>` index, given a set of existing IDs."""
    nums: list[int] = []
    for cid in existing_ids:
        match = re.match(r"C(\d+)$", cid)
        if match:
            nums.append(int(match.group(1)))
    return (max(nums) + 1) if nums else 1


def _fail_open(
    findings: list[dict],
    status: str,
    error: str,
    raw: str,
    started: float,
    ended: float,
) -> ConsolidationResult:
    """Build a ConsolidationResult that upholds every finding (singleton)."""
    finding_index = {f["id"]: f for f in findings}
    clusters: list[FindingCluster] = []
    for idx, finding in enumerate(findings, start=1):
        clusters.append(_make_cluster(f"C{idx}", [finding["id"]], finding_index, upheld=True))
    return ConsolidationResult(
        clusters=clusters,
        upheld_clusters=clusters,
        arbiter_status=status,
        arbiter_error=error,
        arbiter_raw_output=raw,
        arbiter_started_at=started,
        arbiter_ended_at=ended,
    )

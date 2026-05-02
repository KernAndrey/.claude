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
    """One equivalence class of findings (one or more contributors).

    ``chunk_id`` is the chunk that owns every member (per-chunk reviewer
    output) or ``None`` when the cluster mixes chunks, mixes a chunk with
    a whole-diff lens finding, or comes from the legacy non-chunked path.

    ``invariant_id`` is set only for synthetic clusters the arbiter raises
    against a manifest's ``cross_chunk_invariants`` entry, in the form
    ``arbiter-INV<n>``.
    """

    cluster_id: str  # "C1", "C2", ...
    member_ids: tuple[str, ...]  # finding IDs that belong here, in input order
    contributors: tuple[str, ...]  # unique backend names (alphabetical)
    canonical_line: str  # the chosen "best" finding line for display
    upheld: bool
    chunk_id: str | None = None
    invariant_id: str | None = None


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
# Matches finding IDs in any of these grammars:
#   <backend>-F<n>                       legacy / single-backend         (opencode-F1)
#   <chunk>-<backend>-F<n>               chunked per-chunk reviewer      (models-opencode-F1)
#   wholediff-<lens>-<backend>-F<n>      chunked whole-diff lens layer   (wholediff-bugs-claude-F2)
#   arbiter-INV<n>                       synthetic invariant violation   (arbiter-INV1)
_FINDING_ID_TOKEN_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_-]*-)?F\d+"
    r"|arbiter-INV\d+"
)


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


_VERDICT_LINE_RE = re.compile(
    r"^\s*\[(UPHELD|OVERTURN)\]\s*(C\d+)\s*[—\-:]?\s*(.*?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _ingest_cluster_token(
    token: str,
    finding_index: dict[str, dict],
    synthetic_findings: dict[str, dict],
    seen: set[str],
) -> str | None:
    """Decide whether ``token`` is a usable cluster member; register synthetic
    invariant IDs on the fly. Returns the token when accepted, else None."""
    if token in seen:
        return None
    if token in finding_index:
        seen.add(token)
        return token
    if _INVARIANT_ID_RE.match(token):
        synthetic_findings.setdefault(token, {"id": token, "line": f"[{token}] cross-chunk invariant violation"})
        seen.add(token)
        return token
    return None


def _parse_cluster_lines(
    raw: str,
    finding_index: dict[str, dict],
    synthetic_findings: dict[str, dict],
) -> tuple[dict[str, list[str]], set[str]]:
    """Walk every ``[CLUSTER C<n>] ...`` line and group accepted member IDs.

    Returns (cluster_members, seen_ids). Last-writer wins on duplicate
    cluster IDs.
    """
    cluster_members: dict[str, list[str]] = {}
    seen: set[str] = set()
    for m in _CLUSTER_RE.finditer(raw):
        cid = m.group(1).upper()
        # Un-see previous members when the arbiter re-emits a cluster so
        # corrected / extended lists don't silently drop IDs.
        if cid in cluster_members:
            for old_id in cluster_members[cid]:
                seen.discard(old_id)
        ids: list[str] = []
        for token in _FINDING_ID_TOKEN_RE.findall(m.group(2)):
            accepted = _ingest_cluster_token(token, finding_index, synthetic_findings, seen)
            if accepted:
                ids.append(accepted)
        if ids:
            cluster_members[cid] = ids
    return cluster_members, seen


def _parse_verdict_lines(raw: str) -> dict[str, tuple[bool, str]]:
    """Capture verdict + rationale for every ``[UPHELD|OVERTURN] C<n>`` line."""
    verdicts: dict[str, tuple[bool, str]] = {}
    for m in _VERDICT_LINE_RE.finditer(raw):
        cid = m.group(2).upper()
        verdicts[cid] = (m.group(1).upper() == "UPHELD", m.group(3).strip())
    return verdicts


def _enrich_synthetic_lines(
    synthetic_findings: dict[str, dict],
    cluster_members: dict[str, list[str]],
    verdicts: dict[str, tuple[bool, str]],
) -> None:
    """Pull richer rationale text into synthetic placeholders so an invariant
    cluster has an informative canonical line."""
    for cid, member_ids in cluster_members.items():
        rationale = verdicts.get(cid, (True, ""))[1]
        if not rationale:
            continue
        for mid in member_ids:
            if mid in synthetic_findings and len(rationale) > len(synthetic_findings[mid]["line"]):
                synthetic_findings[mid]["line"] = f"[{mid}] {rationale}"


def parse_multi_arbiter_output(
    raw: str,
    findings: list[dict],
) -> list[FindingCluster]:
    """Parse the multi-backend arbiter output into ``FindingCluster`` objects.

    Robust to malformed output:

    - A finding ID that the arbiter omitted from every cluster gets its
      own singleton cluster with ``upheld=True`` (fail-open).
    - A cluster with no verdict line is treated as ``UPHELD`` (same
      stance as the legacy single-backend arbiter parser).
    - Cluster IDs the arbiter invented but didn't ground in any
      finding are dropped silently.

    Synthetic ``arbiter-INV<n>`` IDs (cross-chunk-invariant violations
    raised by the arbiter itself in chunked mode) are accepted even
    though they are not in the input findings list. A placeholder
    finding entry is materialized so the cluster has a canonical line
    sourced from the verdict's rationale.
    """
    finding_index: dict[str, dict] = {f["id"]: f for f in findings}
    synthetic_findings: dict[str, dict] = {}

    cluster_members, seen = _parse_cluster_lines(raw, finding_index, synthetic_findings)
    verdicts = _parse_verdict_lines(raw)
    _enrich_synthetic_lines(synthetic_findings, cluster_members, verdicts)

    merged_index = {**finding_index, **synthetic_findings}
    clusters: list[FindingCluster] = [
        _make_cluster(cid, member_ids, merged_index, upheld=verdicts.get(cid, (True, ""))[0])
        for cid, member_ids in cluster_members.items()
    ]

    next_index = _next_cluster_index(cluster_members.keys())
    for finding in findings:
        if finding["id"] not in seen:
            clusters.append(_make_cluster(f"C{next_index}", [finding["id"]], merged_index, upheld=True))
            next_index += 1
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


_INVARIANT_ID_RE = re.compile(r"^arbiter-INV(\d+)$")
_WHOLEDIFF_ID_RE = re.compile(r"^wholediff-(?P<lens>[A-Za-z][A-Za-z0-9_]*)-(?P<backend>[A-Za-z][A-Za-z0-9_-]*)-F\d+$")
_CHUNKED_ID_RE = re.compile(r"^(?P<chunk>[A-Za-z][A-Za-z0-9_]*)-(?P<backend>[A-Za-z][A-Za-z0-9_-]*)-F\d+$")
_LEGACY_ID_RE = re.compile(r"^(?P<backend>[A-Za-z][A-Za-z0-9_-]*)-F\d+$")


def _backend_of(finding_id: str) -> str:
    """Extract the backend prefix from a finding ID (or '' if unprefixed).

    Handles all four ID grammars: legacy ``<backend>-F<n>``, chunked
    ``<chunk>-<backend>-F<n>``, whole-diff lens
    ``wholediff-<lens>-<backend>-F<n>``, and synthetic
    ``arbiter-INV<n>`` (returns ``"arbiter"``).
    """
    if _INVARIANT_ID_RE.match(finding_id):
        return "arbiter"
    m = _WHOLEDIFF_ID_RE.match(finding_id)
    if m:
        return m.group("backend")
    m = _CHUNKED_ID_RE.match(finding_id)
    if m:
        return m.group("backend")
    m = _LEGACY_ID_RE.match(finding_id)
    if m:
        return m.group("backend")
    return ""


def _chunk_of(finding_id: str) -> str | None:
    """Return the chunk id for chunked per-chunk findings, else None.

    Whole-diff lens findings, legacy IDs, and synthetic invariant IDs
    return None — they don't belong to any single chunk.
    """
    if _WHOLEDIFF_ID_RE.match(finding_id) or _INVARIANT_ID_RE.match(finding_id):
        return None
    m = _CHUNKED_ID_RE.match(finding_id)
    if m:
        return m.group("chunk")
    return None


def _invariant_of(finding_id: str) -> str | None:
    """Return ``arbiter-INV<n>`` for synthetic invariant IDs, else None."""
    if _INVARIANT_ID_RE.match(finding_id):
        return finding_id
    return None


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

    ``chunk_id`` is set when every member belongs to the same chunk;
    mixing chunks (or mixing chunked / wholediff findings) leaves it
    None. ``invariant_id`` is set when at least one member is a
    synthetic ``arbiter-INV<n>`` ID.
    """
    members = [finding_index[mid] for mid in member_ids if mid in finding_index]
    contributors = tuple(sorted({_backend_of(mid) for mid in member_ids if _backend_of(mid)}))
    canonical = ""
    if members:
        canonical = max(members, key=lambda f: len(f["line"]))["line"]

    chunk_ids = {_chunk_of(mid) for mid in member_ids}
    chunk_id = next(iter(chunk_ids)) if len(chunk_ids) == 1 and None not in chunk_ids else None

    invariant_id = next(
        (inv for mid in member_ids if (inv := _invariant_of(mid)) is not None),
        None,
    )

    return FindingCluster(
        cluster_id=cluster_id,
        member_ids=tuple(member_ids),
        contributors=contributors,
        canonical_line=canonical,
        upheld=upheld,
        chunk_id=chunk_id,
        invariant_id=invariant_id,
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

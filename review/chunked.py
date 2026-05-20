"""Chunked review pipeline — additive layer for large commits.

Activates only when ``count_added_production_lines(diff) >= MAX_PROD_LINES``
**and** ``.review/manifest.yaml`` exists. The legacy single-call path for
small commits (``N < MAX_PROD_LINES``) is untouched: it keeps using
``combined.md`` per backend with the existing arbiter.

Composition (per ``run_chunked_review``):

- One per-chunk reviewer per ``chunk × backend`` (≤ ``MAX_CHUNKS *
  len(CHUNKED_BACKENDS)`` jobs).
- One whole-diff reviewer per ``lens × backend`` for each lens in
  ``ALLOWED_LENSES`` (≤ ``len(ALLOWED_LENSES) * len(CHUNKED_BACKENDS)``
  jobs).

All jobs run in parallel through a single ``ThreadPoolExecutor`` —
no concurrency cap (per user). Findings from both layers feed a single
arbiter call whose prompt carries the manifest text so the arbiter can
check ``cross_chunk_invariants`` and merge cross-chunk false positives.
"""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import subprocess
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import config
from config import ARBITER, CHUNKED_BACKENDS, RunnerConfig
from consolidation import FindingCluster, parse_multi_arbiter_output
from validators.manifest import ValidationResult
from validators.manifest import validate as validate_manifest


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewerJobResult:
    """One reviewer subprocess outcome (per-chunk or whole-diff lens)."""

    job_id: str  # e.g. "chunk:models:opencode" or "wholediff:bugs:claude"
    layer: str  # "chunk" or "wholediff"
    chunk_id: str | None  # set for layer="chunk", None for "wholediff"
    lens: str | None  # set for layer="wholediff", None for "chunk"
    backend: str
    status: str  # "ok", "timeout", "unreachable", "error", "empty_stdout"
    raw_text: str
    tagged_text: str
    findings: list[dict]  # {"id": str, "line": str}
    rc: int
    stderr: str
    duration: float
    attempts: int = 1


@dataclass(frozen=True)
class ChunkedResult:
    """Aggregate outcome of the chunked-path run, returned to ``hook.py``."""

    status: str  # "ok" | "manifest_invalid" | "infrastructure_failure"
    validation: ValidationResult
    job_results: list[ReviewerJobResult]
    arbiter_raw: str
    arbiter_status: str  # "ran" | "timeout" | "unreachable" | "error" | "skipped_no_findings"
    arbiter_error: str | None
    clusters: list[FindingCluster]
    upheld_clusters: list[FindingCluster]
    blocking_text: str
    findings_json_text: str
    metrics: dict
    started_at: float
    ended_at: float


# ---------------------------------------------------------------------------
# Per-stage persistence + retry helpers
#
# Every reviewer's raw output is flushed to ``.review/raw/`` immediately on
# return so a subsequent crash (in the arbiter, in the consolidator, in any
# wrapping code) does not lose 5–20 minutes of reviewer work. State-file
# checkpoints capture each pipeline stage so a writer can `cat .review/state/*`
# after a crash and see exactly how far the pipeline got and what was found.
# ---------------------------------------------------------------------------


def _safe_filename(s: str) -> str:
    """Map an arbitrary job_id ('chunk:models:opencode') into a safe filename."""
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in s)


class _RunPersister:
    """Atomic per-stage writer for the chunked-review run.

    Constructed once per ``run_chunked_review`` call. If ``repo_root`` is
    ``None`` the persister is disabled (every method becomes a no-op) — used
    by tests that don't care about disk artifacts and by legacy code paths
    that compose the pipeline without on-disk forensics.
    """

    def __init__(self, repo_root: Path | None = None) -> None:
        self._enabled = repo_root is not None
        if self._enabled:
            assert repo_root is not None  # type narrowing for mypy
            self.review_dir = repo_root / ".review"
            self.state_dir = self.review_dir / "state"
            self.raw_dir = self.review_dir / "raw"
        self._stage = "init"

    def _write_atomic(self, path: Path, text: str) -> None:
        if not self._enabled:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def reset_state_dirs(self) -> None:
        """Wipe ``state/`` and ``raw/`` from any prior run; keep ``manifest.yaml``."""
        if not self._enabled:
            return
        for d in (self.state_dir, self.raw_dir):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)

    def record_validation(self, validation: ValidationResult) -> None:
        self._stage = "validation"
        path = self.state_dir / "00_validation.json" if self._enabled else Path()
        payload = {
            "ok": validation.ok,
            "errors": [
                {
                    "code": e.code,
                    "message": e.message,
                    "chunk_id": e.chunk_id,
                    "file_path": e.file_path,
                }
                for e in validation.errors
            ],
        }
        self._write_atomic(path, json.dumps(payload, indent=2))

    def record_jobs_plan(self, jobs_meta: list[dict[str, Any]]) -> None:
        self._stage = "jobs_plan"
        path = self.state_dir / "01_jobs_plan.json" if self._enabled else Path()
        self._write_atomic(path, json.dumps(jobs_meta, indent=2))

    def record_job_complete(self, jr: ReviewerJobResult) -> None:
        if not self._enabled:
            return
        stem = _safe_filename(jr.job_id)
        text = jr.tagged_text or jr.raw_text or ""
        self._write_atomic(self.raw_dir / f"{stem}.txt", text)
        payload = {
            "job_id": jr.job_id,
            "layer": jr.layer,
            "chunk_id": jr.chunk_id,
            "lens": jr.lens,
            "backend": jr.backend,
            "status": jr.status,
            "rc": jr.rc,
            "duration": round(jr.duration, 3),
            "attempts": jr.attempts,
            "stderr_head": (jr.stderr or "")[:512],
            "findings": jr.findings,
        }
        self._write_atomic(self.raw_dir / f"{stem}.json", json.dumps(payload, indent=2))

    def record_jobs_complete(self, job_results: list[ReviewerJobResult]) -> None:
        self._stage = "jobs_complete"
        path = self.state_dir / "02_jobs_complete.json" if self._enabled else Path()
        payload = [
            {
                "job_id": jr.job_id,
                "layer": jr.layer,
                "chunk_id": jr.chunk_id,
                "lens": jr.lens,
                "backend": jr.backend,
                "status": jr.status,
                "rc": jr.rc,
                "duration": round(jr.duration, 3),
                "attempts": jr.attempts,
                "findings_count": len(jr.findings),
            }
            for jr in job_results
        ]
        self._write_atomic(path, json.dumps(payload, indent=2))

    def record_arbiter_input(self, prompt: str) -> None:
        self._stage = "arbiter_input"
        path = self.state_dir / "03_arbiter_input.txt" if self._enabled else Path()
        self._write_atomic(path, prompt)

    def record_arbiter_output(
        self,
        raw: str,
        status: str,
        error: str | None,
        attempts: int,
    ) -> None:
        self._stage = "arbiter_output"
        meta = {
            "status": status,
            "error": error,
            "attempts": attempts,
            "raw_length": len(raw or ""),
        }
        path = self.state_dir / "04_arbiter_meta.json" if self._enabled else Path()
        self._write_atomic(path, json.dumps(meta, indent=2))

    def record_clusters(self, clusters: list[FindingCluster]) -> None:
        self._stage = "clusters"
        path = self.state_dir / "05_clusters.json" if self._enabled else Path()
        payload = [
            {
                "cluster_id": c.cluster_id,
                "member_ids": list(c.member_ids),
                "contributors": list(c.contributors),
                "canonical_line": c.canonical_line,
                "upheld": c.upheld,
                "chunk_id": c.chunk_id,
                "invariant_id": c.invariant_id,
            }
            for c in clusters
        ]
        self._write_atomic(path, json.dumps(payload, indent=2))

    def record_retry_attempt(
        self,
        job_id: str,
        backend: str,
        attempt: int,
        outcome: str,
        duration_s: float,
        stderr_head: str,
    ) -> None:
        if not self._enabled:
            return
        path = self.state_dir / "retries.jsonl"
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "job_id": job_id,
                "backend": backend,
                "attempt": attempt,
                "outcome": outcome,
                "duration_s": round(duration_s, 3),
                "stderr_head": (stderr_head or "")[:200],
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def record_crash(self, traceback_text: str) -> None:
        if not self._enabled:
            return
        path = self.state_dir / "crash.log"
        body = f"# Pipeline crashed at stage: {self._stage}\n\n{traceback_text}"
        self._write_atomic(path, body)


_NULL_PERSISTER = _RunPersister()


def _run_with_retry(
    invoke: Callable[[], tuple[str, str, int, str]],
    job_id: str,
    backend: str,
    persister: _RunPersister,
) -> tuple[str, str, int, str, int]:
    """Wrap one reviewer/arbiter invocation with up to one retry on infra-style failures.

    Each attempt is logged to ``state/retries.jsonl`` (so the writer can
    later analyze which backends fail most often and reduce the failure
    rate). ``invoke`` must return ``(output, stderr, rc, status)``.

    Retries on:
      - ``status == "timeout"`` (subprocess.TimeoutExpired upstream)
      - ``status == "unreachable"`` (FileNotFoundError / OSError upstream)
      - ``status == "empty_stdout"`` (rc=0 but no usable output)

    Does NOT retry rc!=0 with non-empty stdout — that is a real review
    failure, retrying just burns tokens.
    """
    retry_eligible = {"timeout", "unreachable", "empty_stdout"}
    review, stderr, rc, status = "", "", -1, "unreachable"
    for attempt in (1, 2):
        started = time.monotonic()
        review, stderr, rc, status = invoke()
        duration = time.monotonic() - started
        persister.record_retry_attempt(job_id, backend, attempt, status, duration, stderr)
        if status not in retry_eligible:
            return review, stderr, rc, status, attempt
        if attempt == 2:
            return review, stderr, rc, status, attempt
    # unreachable in practice; defensive fall-through for type checker
    return review, stderr, rc, status, 2


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------


REVIEW_ROOT = Path.home() / ".claude" / "review"
PROMPTS_DIR = REVIEW_ROOT / "prompts"
CHUNK_REVIEW_PROMPT = PROMPTS_DIR / "chunk_review.md"
ARBITER_MULTI_PROMPT = PROMPTS_DIR / "arbiter_multi.md"


def _manifest_path(repo_root: Path) -> Path:
    return repo_root / ".review" / "manifest.yaml"


def manifest_present(repo_root: Path) -> bool:
    """Cheap predicate the hook calls before it commits to chunked dispatch."""
    return _manifest_path(repo_root).is_file()


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""


def _build_cached_block(diff: str, manifest_text: str, repo_root: Path, default_related: list[str]) -> str:
    """Concatenate the project context, default-related files, full diff and
    manifest into the prompt prefix that's identical across every reviewer.
    Anthropic-side prompt caching can be wired against this block in v2."""
    parts: list[str] = []

    project_md = _read_text(repo_root / "CLAUDE.md")
    if project_md:
        parts.append("## Project context (CLAUDE.md)\n\n" + project_md)

    if default_related:
        related_parts = ["## Default related files"]
        for rel in default_related:
            content = _read_text(repo_root / rel)
            if content:
                related_parts.append(f"### {rel}\n\n{content}")
        if len(related_parts) > 1:
            parts.append("\n\n".join(related_parts))

    parts.append("## Full staged diff\n\n```diff\n" + diff + "\n```")
    parts.append("## Manifest\n\n```yaml\n" + manifest_text + "\n```")
    return "\n\n".join(parts) + "\n\n---\n\n"


def _build_chunk_variable_block(chunk: dict, repo_root: Path) -> str:
    cid = chunk["id"]
    parts = [f"## Your scope\nchunk_id: {cid}"]
    related = chunk.get("related_files") or []
    if related:
        rel_parts = ["## Chunk-specific related files"]
        for rel in related:
            content = _read_text(repo_root / rel)
            if content:
                rel_parts.append(f"### {rel}\n\n{content}")
        if len(rel_parts) > 1:
            parts.append("\n\n".join(rel_parts))
    rationale = chunk.get("rationale")
    if rationale:
        parts.append(f"## Rationale (from manifest)\n\n{rationale}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Reviewer invocation (sync; jobs are wrapped in a thread-pool below)
# ---------------------------------------------------------------------------


def _run_one_reviewer(
    backend_cfg: RunnerConfig,
    system_prompt: str,
    user_prompt: str,
    job_id: str,
) -> tuple[str, str, int, str]:
    """Wrap ``hook.run_reviewer`` so timeout/unreachable failures become
    one-line synthetic CRITICAL findings instead of crashing the executor."""
    from hook import run_reviewer  # late import — avoids hook ↔ chunked cycle

    try:
        if not system_prompt.strip():
            raise FileNotFoundError("missing or empty system prompt")
        review, stderr, rc = run_reviewer(backend_cfg, system_prompt, user_prompt)
        if rc == 0 and not review.strip():
            return review, stderr, rc, "empty_stdout"
        return review, stderr, rc, "ok"
    except subprocess.TimeoutExpired:
        synthetic = (
            f"- [CRITICAL] {job_id}:0 — `<reviewer timeout>` — reviewer did not return within {backend_cfg.timeout}s"
        )
        return synthetic, "", -1, "timeout"
    except (FileNotFoundError, OSError) as exc:
        synthetic = (
            f"- [CRITICAL] {job_id}:0 — `<reviewer unreachable>` — backend {backend_cfg.backend!r} unavailable: {exc}"
        )
        return synthetic, "", -1, "unreachable"


def _run_chunk_job(
    chunk: dict,
    backend_cfg: RunnerConfig,
    system_prompt: str,
    cached_block: str,
    repo_root: Path,
    persister: _RunPersister = _NULL_PERSISTER,
) -> ReviewerJobResult:
    cid = chunk["id"]
    job_id = f"chunk:{cid}:{backend_cfg.backend}"
    user_prompt = cached_block + _build_chunk_variable_block(chunk, repo_root)
    started = time.monotonic()
    review, stderr, rc, status, attempts = _run_with_retry(
        lambda: _run_one_reviewer(backend_cfg, system_prompt, user_prompt, job_id),
        job_id=job_id,
        backend=backend_cfg.backend,
        persister=persister,
    )
    duration = time.monotonic() - started

    from hook import assign_finding_ids

    prefix = f"{cid}-{backend_cfg.backend}"
    tagged, findings = assign_finding_ids(review, prefix=prefix)
    jr = ReviewerJobResult(
        job_id=job_id,
        layer="chunk",
        chunk_id=cid,
        lens=None,
        backend=backend_cfg.backend,
        status=status if rc == 0 or status != "ok" else "error",
        raw_text=review,
        tagged_text=tagged,
        findings=findings,
        rc=rc,
        stderr=stderr,
        duration=duration,
        attempts=attempts,
    )
    persister.record_job_complete(jr)
    return jr


def _run_wholediff_job(
    lens: str,
    backend_cfg: RunnerConfig,
    diff: str,
    files: str,
    is_merge: bool,
    persister: _RunPersister = _NULL_PERSISTER,
) -> ReviewerJobResult:
    from hook import assign_finding_ids, build_lens_system_prompt, build_user_prompt

    job_id = f"wholediff:{lens}:{backend_cfg.backend}"
    system_prompt = build_lens_system_prompt(lens) or ""
    user_prompt = build_user_prompt(diff, files, is_merge)
    started = time.monotonic()
    review, stderr, rc, status, attempts = _run_with_retry(
        lambda: _run_one_reviewer(backend_cfg, system_prompt, user_prompt, job_id),
        job_id=job_id,
        backend=backend_cfg.backend,
        persister=persister,
    )
    duration = time.monotonic() - started

    prefix = f"wholediff-{lens}-{backend_cfg.backend}"
    tagged, findings = assign_finding_ids(review, prefix=prefix)
    jr = ReviewerJobResult(
        job_id=job_id,
        layer="wholediff",
        chunk_id=None,
        lens=lens,
        backend=backend_cfg.backend,
        status=status if rc == 0 or status != "ok" else "error",
        raw_text=review,
        tagged_text=tagged,
        findings=findings,
        rc=rc,
        stderr=stderr,
        duration=duration,
        attempts=attempts,
    )
    persister.record_job_complete(jr)
    return jr


def _run_jobs_parallel(jobs: list[Callable[[], ReviewerJobResult]]) -> list[ReviewerJobResult]:
    """Run every job concurrently. No semaphore (per user)."""
    if not jobs:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(job) for job in jobs]
        return [f.result() for f in futures]


# ---------------------------------------------------------------------------
# Chunked arbiter — parallel to consolidation._consolidate_multi_backend but
# augments the user prompt with the manifest so the arbiter can check
# cross_chunk_invariants and merge cross-chunk false positives.
# ---------------------------------------------------------------------------


def _build_arbiter_user_prompt(diff: str, manifest_text: str, findings: list[dict]) -> str:
    return (
        "## Full staged diff\n\n```diff\n"
        + diff
        + "\n```\n\n"
        + "## Findings to cluster + arbitrate (in order)\n\n"
        + "\n".join(f["line"] for f in findings)
        + "\n\n"
        + "## Manifest\n\n```yaml\n"
        + manifest_text
        + "\n```\n\n"
        + "Output cluster lines first (`[CLUSTER C1] <ids>`), then one "
        "verdict line per cluster (`[UPHELD] C1` or `[OVERTURN] C1`), "
        "then the `Summary:` and `Chunked:` terminators. No other content."
    )


def _manifest_has_invariants(manifest_text: str) -> bool:
    """Return True when *manifest_text* declares non-empty cross_chunk_invariants."""
    if not manifest_text:
        return False
    try:
        parsed = yaml.safe_load(manifest_text)
        if isinstance(parsed, dict):
            invariants = parsed.get("cross_chunk_invariants")
            return bool(invariants)
    except yaml.YAMLError:
        pass
    return False


def _run_chunked_arbiter(
    diff: str,
    manifest_text: str,
    findings: list[dict],
    persister: _RunPersister = _NULL_PERSISTER,
) -> tuple[str, str, str | None, int]:
    """Run the chunked-mode arbiter. Returns (raw_output, status, error, attempts).

    Status values match ``ConsolidationResult.arbiter_status``:
    ``"ran"``, ``"timeout"``, ``"unreachable"``, ``"error"``,
    ``"skipped_no_findings"``.

    The arbiter is retried once on infra failures (timeout / unreachable /
    rc=0+empty stdout) via :func:`_run_with_retry`. Each attempt is logged
    to ``state/retries.jsonl``.
    """
    from hook import info, run_reviewer, warn

    if not findings and not _manifest_has_invariants(manifest_text):
        return "", "skipped_no_findings", None, 0

    system_prompt = _read_text(ARBITER_MULTI_PROMPT)
    if not system_prompt:
        warn(f"chunked: arbiter prompt {ARBITER_MULTI_PROMPT} missing — fail-open")
        return "", "unreachable", f"{ARBITER_MULTI_PROMPT} not found", 0

    user_prompt = _build_arbiter_user_prompt(diff, manifest_text, findings)
    persister.record_arbiter_input(user_prompt)
    info(f"chunked: clustering+validating {len(findings)} finding(s) via {ARBITER.backend} {ARBITER.model}...")

    def _invoke() -> tuple[str, str, int, str]:
        try:
            raw, stderr, rc = run_reviewer(ARBITER, system_prompt, user_prompt)
        except subprocess.TimeoutExpired:
            return "", "", -1, "timeout"
        except (FileNotFoundError, OSError) as exc:
            return "", str(exc), -1, "unreachable"
        if rc == 0 and (not raw or not raw.strip()):
            return raw or "", stderr, rc, "empty_stdout"
        if rc != 0:
            return raw or "", stderr, rc, "error_rc"
        return raw, stderr, rc, "ran"

    raw, stderr, rc, status, attempts = _run_with_retry(
        _invoke,
        job_id=f"arbiter:{ARBITER.backend}",
        backend=ARBITER.backend,
        persister=persister,
    )

    if status == "ran":
        return raw, "ran", None, attempts
    if status == "timeout":
        warn(f"chunked: arbiter timed out after {ARBITER.timeout}s — fail-open")
        return "", "timeout", "timeout", attempts
    if status == "unreachable":
        warn("chunked: arbiter unreachable — fail-open")
        return "", "unreachable", stderr or "unreachable", attempts
    # empty_stdout / error_rc → "error" arbiter_status (fail-open singletons)
    return raw or "", "error", f"rc={rc} stderr={stderr[:200] if stderr else ''}", attempts


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_blocking_text(upheld: list[FindingCluster], total: int) -> str:
    if not upheld:
        return f"chunked review: 0 BLOCKING (out of {total} cluster(s))."
    lines = [f"chunked review: {len(upheld)} BLOCKING (out of {total} cluster(s)):"]
    for cluster in upheld:
        tag = cluster.cluster_id
        if cluster.chunk_id:
            tag += f" [chunk={cluster.chunk_id}]"
        if cluster.invariant_id:
            tag += f" [{cluster.invariant_id}]"
        lines.append(f"  - {tag}: {cluster.canonical_line}")
    return "\n".join(lines)


def _format_findings_json(
    clusters: list[FindingCluster],
    job_results: list[ReviewerJobResult],
    metrics: dict,
) -> str:
    payload = {
        "metrics": metrics,
        "jobs": [
            {
                "job_id": jr.job_id,
                "layer": jr.layer,
                "chunk_id": jr.chunk_id,
                "lens": jr.lens,
                "backend": jr.backend,
                "status": jr.status,
                "rc": jr.rc,
                "duration_seconds": round(jr.duration, 3),
                "finding_count": len(jr.findings),
            }
            for jr in job_results
        ],
        "clusters": [
            {
                "cluster_id": c.cluster_id,
                "member_ids": list(c.member_ids),
                "contributors": list(c.contributors),
                "canonical_line": c.canonical_line,
                "upheld": c.upheld,
                "chunk_id": c.chunk_id,
                "invariant_id": c.invariant_id,
            }
            for c in clusters
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------


@dataclass
class _RunInputs:
    """Inputs derived from the staged diff + manifest for one run.

    Splitting this out keeps ``run_chunked_review`` short and makes it
    easy to unit-test the validate-only failure path."""

    manifest_text: str
    manifest: dict = field(default_factory=dict)
    validation: ValidationResult = field(default_factory=ValidationResult)


def _load_and_validate(diff: str, repo_root: Path) -> _RunInputs:
    text = _manifest_path(repo_root).read_text(encoding="utf-8")
    inputs = _RunInputs(manifest_text=text)
    inputs.validation = validate_manifest(text, diff, repo_root)
    if inputs.validation.ok:
        loaded = yaml.safe_load(text)
        if isinstance(loaded, dict):
            inputs.manifest = loaded
    return inputs


def _build_jobs(
    inputs: _RunInputs,
    diff: str,
    files: str,
    is_merge: bool,
    repo_root: Path,
    persister: _RunPersister = _NULL_PERSISTER,
) -> list[Callable[[], ReviewerJobResult]]:
    chunk_review_prompt = _read_text(CHUNK_REVIEW_PROMPT)
    default_related = inputs.manifest.get("default_related_files") or []
    cached_block = _build_cached_block(diff, inputs.manifest_text, repo_root, list(default_related))

    jobs: list[Callable[[], ReviewerJobResult]] = []
    for chunk in inputs.manifest.get("chunks", []):
        for backend_cfg in CHUNKED_BACKENDS:
            jobs.append(
                lambda c=chunk, b=backend_cfg: _run_chunk_job(
                    c, b, chunk_review_prompt, cached_block, repo_root, persister
                )
            )

    for lens in sorted(config.ALLOWED_LENSES):
        for backend_cfg in CHUNKED_BACKENDS:
            jobs.append(lambda L=lens, b=backend_cfg: _run_wholediff_job(L, b, diff, files, is_merge, persister))
    return jobs


def _consolidate(
    job_results: list[ReviewerJobResult],
    diff: str,
    manifest_text: str,
    persister: _RunPersister = _NULL_PERSISTER,
) -> tuple[str, str, str | None, list[FindingCluster]]:
    all_findings: list[dict] = []
    for jr in job_results:
        all_findings.extend(jr.findings)

    arbiter_raw, arbiter_status, arbiter_error, attempts = _run_chunked_arbiter(
        diff, manifest_text, all_findings, persister=persister
    )
    persister.record_arbiter_output(arbiter_raw, arbiter_status, arbiter_error, attempts)

    if arbiter_status == "ran":
        clusters = parse_multi_arbiter_output(arbiter_raw, all_findings)
    elif arbiter_status == "skipped_no_findings":
        clusters = []
    else:
        # Fail-open: every finding becomes a singleton UPHELD cluster.
        clusters = parse_multi_arbiter_output("", all_findings)
    persister.record_clusters(clusters)
    return arbiter_raw, arbiter_status, arbiter_error, clusters


def _build_infrastructure_failure_result(
    *,
    validation: ValidationResult,
    job_results: list[ReviewerJobResult],
    jobs_total: int,
    started: float,
) -> ChunkedResult:
    """Synthesize a BLOCKING ``ChunkedResult`` when every reviewer failed.

    Skipping the arbiter is deliberate: it has no real findings to compare
    against and cannot judge whether the staged code is safe.
    """
    all_findings = [f for jr in job_results for f in jr.findings]
    member_ids = tuple(f["id"] for f in all_findings)
    by_status: dict[str, int] = {}
    for jr in job_results:
        by_status[jr.status] = by_status.get(jr.status, 0) + 1
    breakdown = ", ".join(f"{n}×{s}" for s, n in sorted(by_status.items()))
    if jobs_total == 0:
        canonical = (
            "[CRITICAL] chunked-review infrastructure failure — "
            "no reviewer jobs were built (manifest produced 0 jobs or CHUNKED_BACKENDS is empty); "
            "no code was actually reviewed, so the commit is blocked."
        )
    else:
        canonical = (
            f"[CRITICAL] chunked-review infrastructure failure — "
            f"all {jobs_total} reviewer subprocess(es) failed ({breakdown}); "
            f"no code was actually reviewed, so the commit is blocked. "
            f"Inspect job stderr in .review/raw/ before retrying."
        )
    cluster = FindingCluster(
        cluster_id="INFRA1",
        member_ids=member_ids,
        contributors=tuple(sorted({jr.backend for jr in job_results})),
        canonical_line=canonical,
        upheld=True,
        chunk_id=None,
        invariant_id="infra-failure",
    )
    ended = time.monotonic()
    metrics = {
        "phase": "infrastructure_failure",
        "wall_clock_seconds": round(ended - started, 3),
        "jobs": jobs_total,
        "findings_total": len(all_findings),
        "clusters_total": 1,
        "clusters_upheld": 1,
        "arbiter_status": "skipped_infrastructure_failure",
    }
    findings_payload = {
        "status": "infrastructure_failure",
        "metrics": metrics,
        "jobs": [
            {
                "job_id": jr.job_id,
                "status": jr.status,
                "rc": jr.rc,
                "stderr": jr.stderr,
            }
            for jr in job_results
        ],
    }
    return ChunkedResult(
        status="infrastructure_failure",
        validation=validation,
        job_results=job_results,
        arbiter_raw="",
        arbiter_status="skipped_infrastructure_failure",
        arbiter_error=None,
        clusters=[cluster],
        upheld_clusters=[cluster],
        blocking_text=canonical,
        findings_json_text=json.dumps(findings_payload, indent=2),
        metrics=metrics,
        started_at=started,
        ended_at=ended,
    )


def run_chunked_review(
    diff: str,
    files: str,
    repo_root: Path,
    *,
    is_merge: bool = False,
) -> ChunkedResult:
    """Run the chunked pipeline end-to-end on the current staged diff.

    Caller must have already verified ``manifest_present(repo_root)``.

    Per-stage state is flushed to ``.review/state/`` and per-job raw
    output to ``.review/raw/`` as soon as each stage completes, so a
    crash anywhere in the pipeline still leaves a complete forensic
    trail for the writer.
    """
    started = time.monotonic()
    persister = _RunPersister(repo_root)
    persister.reset_state_dirs()
    try:
        return _run_chunked_review_impl(
            diff=diff,
            files=files,
            repo_root=repo_root,
            is_merge=is_merge,
            started=started,
            persister=persister,
        )
    except Exception:
        persister.record_crash(traceback.format_exc())
        raise


def _run_chunked_review_impl(
    *,
    diff: str,
    files: str,
    repo_root: Path,
    is_merge: bool,
    started: float,
    persister: _RunPersister,
) -> ChunkedResult:
    inputs = _load_and_validate(diff, repo_root)
    persister.record_validation(inputs.validation)
    if not inputs.validation.ok:
        ended = time.monotonic()
        text = inputs.validation.to_text()
        return ChunkedResult(
            status="manifest_invalid",
            validation=inputs.validation,
            job_results=[],
            arbiter_raw="",
            arbiter_status="skipped_no_findings",
            arbiter_error=None,
            clusters=[],
            upheld_clusters=[],
            blocking_text=text,
            findings_json_text=json.dumps(
                {"errors": [{"code": e.code, "message": e.message} for e in inputs.validation.errors]},
                indent=2,
            ),
            metrics={"phase": "validation_failed", "wall_clock_seconds": ended - started},
            started_at=started,
            ended_at=ended,
        )

    jobs = _build_jobs(inputs, diff, files, is_merge, repo_root, persister)
    from hook import info

    info(
        f"chunked: spawning {len(jobs)} reviewer(s): "
        f"{len(inputs.manifest.get('chunks', []))} chunk(s) + "
        f"{len(config.ALLOWED_LENSES)} lens(es) × {len(CHUNKED_BACKENDS)} backend(s)"
    )
    persister.record_jobs_plan(
        [
            {
                "index": idx,
                "kind": "chunk"
                if idx < len(inputs.manifest.get("chunks", [])) * len(CHUNKED_BACKENDS)
                else "wholediff",
            }
            for idx in range(len(jobs))
        ]
    )
    job_results = _run_jobs_parallel(jobs)
    persister.record_jobs_complete(job_results)

    # Safety: if every reviewer subprocess failed (timeout / unreachable /
    # non-zero rc), block immediately. Without this the arbiter sees only
    # synthetic ``<reviewer unreachable>`` findings and may overturn them
    # ("no real review = no real defect"), silently allowing commits when
    # the pipeline itself is broken.
    if not jobs:
        return _build_infrastructure_failure_result(
            validation=inputs.validation,
            job_results=[],
            jobs_total=0,
            started=started,
        )

    # C3: every reviewer failed and produced zero findings → block.
    if job_results and all(jr.status != "ok" for jr in job_results) and all(not jr.findings for jr in job_results):
        return _build_infrastructure_failure_result(
            validation=inputs.validation,
            job_results=job_results,
            jobs_total=len(jobs),
            started=started,
        )

    infra_only_statuses = {"timeout", "unreachable", "empty_stdout"}
    if job_results and all(jr.status in infra_only_statuses for jr in job_results):
        return _build_infrastructure_failure_result(
            validation=inputs.validation,
            job_results=job_results,
            jobs_total=len(jobs),
            started=started,
        )

    arbiter_raw, arbiter_status, arbiter_error, clusters = _consolidate(
        job_results, diff, inputs.manifest_text, persister=persister
    )

    upheld = [c for c in clusters if c.upheld]
    ended = time.monotonic()

    metrics = {
        "phase": "complete",
        "wall_clock_seconds": round(ended - started, 3),
        "jobs": len(jobs),
        "findings_total": sum(len(jr.findings) for jr in job_results),
        "clusters_total": len(clusters),
        "clusters_upheld": len(upheld),
        "arbiter_status": arbiter_status,
    }

    return ChunkedResult(
        status="ok",
        validation=inputs.validation,
        job_results=job_results,
        arbiter_raw=arbiter_raw,
        arbiter_status=arbiter_status,
        arbiter_error=arbiter_error,
        clusters=clusters,
        upheld_clusters=upheld,
        blocking_text=_format_blocking_text(upheld, len(clusters)),
        findings_json_text=_format_findings_json(clusters, job_results, metrics),
        metrics=metrics,
        started_at=started,
        ended_at=ended,
    )


# ---------------------------------------------------------------------------
# Artifacts — written into <repo>/.review/ so the post-commit hook can
# atomically move them to <git_common>/review-archive/<sha>/ on success.
# ---------------------------------------------------------------------------


def _safe_filename(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in s)


def write_artifacts(result: ChunkedResult, repo_root: Path) -> Path:
    """Persist run outputs to ``<repo>/.review/`` for forensics + archive.

    Layout:
      .review/findings.json   — consolidated clusters + per-job stats
      .review/blocking.txt    — human-readable blocking summary
      .review/metrics.json    — timing + counts
      .review/arbiter_raw.txt — full arbiter output (optional)
      .review/raw/<job>.txt   — raw reviewer output per job
      .review/raw/<job>.json  — tagged findings per job
    """
    review_dir = repo_root / ".review"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "findings.json").write_text(result.findings_json_text, encoding="utf-8")
    (review_dir / "blocking.txt").write_text(result.blocking_text, encoding="utf-8")
    (review_dir / "metrics.json").write_text(json.dumps(result.metrics, indent=2), encoding="utf-8")
    arbiter_raw_path = review_dir / "arbiter_raw.txt"
    if arbiter_raw_path.exists():
        arbiter_raw_path.unlink()
    if result.arbiter_raw:
        arbiter_raw_path.write_text(result.arbiter_raw, encoding="utf-8")

    # raw/ entries are normally written incrementally by ``_RunPersister``
    # as each reviewer subprocess returns. Mirror through the same persister
    # here so ``write_artifacts`` works as a standalone post-pipeline call
    # (tests, recovery from a broken state) and so the on-disk schema stays
    # identical between paths (attempts + stderr_head are preserved).
    persister = _RunPersister(repo_root)
    persister.raw_dir.mkdir(parents=True, exist_ok=True)
    for jr in result.job_results:
        persister.record_job_complete(jr)
    return review_dir

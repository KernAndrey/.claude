"""Parallel pre-review orchestrator for the SDD Land phase.

The commit gate (`hook.py`) reviews one commit at a time and is slow (an LLM
call per commit). For a large task that lands ~10 logical commits, running the
gate sequentially can take hours. This module reviews every planned commit
group **in parallel** *before* the real commits, using the identical review
pipeline against a private git index per group (`GIT_INDEX_FILE`) so nothing
touches the shared working index. A group that passes review gets an approval
marker (`approvals.write_approval`); at commit time `hook._maybe_fastpath`
then skips the redundant LLM review for that exact diff.

Diff identity is the load-bearing invariant: the diff this module hashes for a
group must be byte-identical to what `git diff --cached` produces when the real
commit stages the same files — even after earlier groups have been committed
and HEAD has moved. That holds because the commit planner makes groups
file-disjoint, so a group's diff never depends on another group's commit. See
`test_pre_review.py` for the proof (real intervening commit).

Parallelism lives here (a thread pool), not at the agent layer — mirroring
`chunked.py`. The workflow (`implement.js`) invokes this through a single agent
and consumes the JSON report on stdout.

There is NO signing here. Integrity is enforced by the workflow: it builds its
trusted approved-set from this report (via the reviewer agent), the committer
never produces audit evidence, and a separate post-Land verifier agent
re-derives every landed commit's hash from git and demands it was approved.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import approvals
from config import MAX_PROD_LINES
from consolidation import consolidate
from hook import applicable_lenses, count_added_production_lines
from orchestrator import run_multi_backend

# Verdicts that mean "the live gate would allow this commit" → safe to approve.
# BLOCK = upheld critical(s); EMPTY = every reviewer failed (don't approve —
# let the live gate retry); SKIP = no applicable lens (gate would exit 0 too).
_APPROVE_VERDICTS = frozenset({"OK", "SKIP"})


def _gate_verdict(diff: str, names: str) -> tuple[str, list[str]]:
    """Reproduce the commit gate's verdict EXACTLY, then return (verdict, blockers).

    Mirrors ``hook._run_multi_backend_pipeline`` (run_multi_backend →
    consolidate → BLOCK iff upheld clusters) plus ``main()``'s
    docs-only short-circuit, so a CLEAN pre-review means the live gate would
    pass *by construction* — NOT the legacy single-backend ``run_review`` path,
    which would silently diverge the moment a second PRIMARY is configured.
    ``blockers`` are the upheld findings' canonical lines (same
    ``[id] [CRITICAL] path:line — desc`` format the committer already parses).
    """
    if not applicable_lenses(names):
        return "SKIP", []
    results = run_multi_backend(diff, names, is_merge=False)
    if not any(r.status == "ok" and r.review_text and r.review_text.strip() for r in results):
        return "EMPTY", []
    cons = consolidate(results, diff)
    if cons.upheld_clusters:
        return "BLOCK", [c.canonical_line for c in cons.upheld_clusters]
    return "OK", []


def _git(args: list[str], repo_root: Path, index_file: str | None = None) -> str:
    """Run a git command and return stripped stdout (raises on non-zero)."""
    env = None
    if index_file is not None:
        env = {**os.environ, "GIT_INDEX_FILE": index_file}
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def build_group_diff(repo_root: Path, files: list[str]) -> tuple[str, str]:
    """Stage ``files`` into a throwaway private index and return (diff, names).

    Uses ``GIT_INDEX_FILE`` so the shared index/working tree is untouched and
    many groups can run concurrently. ``git add -- <files>`` mirrors exactly
    how the committer stages, so the resulting ``git diff --cached`` is the
    same text the real commit will be hashed against.
    """
    fd, idx_path = tempfile.mkstemp(prefix="prereview-idx-")
    os.close(fd)
    try:
        _git(["read-tree", "HEAD"], repo_root, idx_path)
        _git(["add", "--", *files], repo_root, idx_path)
        diff = _git(["diff", "--cached"], repo_root, idx_path)
        names = _git(["diff", "--cached", "--name-only"], repo_root, idx_path)
        return diff, names
    finally:
        os.unlink(idx_path)


def review_group(repo_root: Path, index: int, group: dict) -> dict:
    """Review one commit group in isolation; write its marker if it passes.

    Returns a record (no exception escapes — a failure becomes verdict ERROR
    so one bad group never sinks the whole round).
    """
    message = group.get("message", "")
    files = group.get("files", [])
    # Worker isolation: this runs in a thread pool, so ANY failure (git staging,
    # a reviewer/arbiter crash, etc.) must become an ERROR record rather than
    # propagate and sink the whole round. Broad by design — the error text is
    # surfaced in the record, never silently swallowed.
    try:
        diff, names = build_group_diff(repo_root, files)
        if not diff:
            # Nothing to review (files unchanged vs HEAD). Treat as clean/no-op.
            return _record(index, message, approvals.diff_hash(diff), "SKIP", False, True, [])
        if count_added_production_lines(diff) >= MAX_PROD_LINES:
            # Rare (planner targets <=300). Don't pre-approve — let the live gate
            # run its chunked/manifest path at commit time.
            return _record(index, message, approvals.diff_hash(diff), "TOO_BIG", True, False, [])
        verdict, blockers = _gate_verdict(diff, names)
        approved = verdict in _APPROVE_VERDICTS
        h = approvals.diff_hash(diff)
        if approved:
            approvals.write_approval(repo_root, h)
        return _record(index, message, h, verdict, False, approved, blockers)
    except Exception as exc:
        return _record(index, message, "", "ERROR", False, False, [f"review failed: {type(exc).__name__}: {exc}"])


def _record(
    index: int, message: str, diff_hash: str, verdict: str, too_big: bool, approved: bool, blockers: list[str]
) -> dict:
    return {
        "index": index,
        "message": message,
        "diff_hash": diff_hash,
        "verdict": verdict,
        "too_big": too_big,
        "approved": approved,
        "blockers": blockers,
    }


def review_wholediff(repo_root: Path, all_files: list[str]) -> dict:
    """Cross-cutting review over the union of every group's files.

    Catches issues that span commit boundaries (architecture, an invariant a
    single group's slice can't reveal). Advisory: it gates the round (the
    workflow loops until it is clean too) but never writes a per-group marker.
    Uses the same gate verdict (``_gate_verdict``) over the union diff.
    """
    if not all_files:
        return {"verdict": "SKIP", "blockers": []}
    # Stage the union into a private index (same as a group) so NEW files are
    # captured — `git diff HEAD` alone omits untracked files.
    diff, names = build_group_diff(repo_root, all_files)
    if not diff:
        return {"verdict": "SKIP", "blockers": []}
    verdict, blockers = _gate_verdict(diff, names)
    return {"verdict": verdict, "blockers": blockers}


def run_pre_review(
    repo_root: Path,
    plan: dict,
    pending: list[int] | None,
    reset: bool,
    max_workers: int | None,
) -> dict:
    """Review the pending groups in parallel + a whole-diff pass. Returns report."""
    groups: list[dict] = plan.get("groups", [])
    if reset:
        approvals.clear_approvals(repo_root)

    indices = list(range(len(groups))) if pending is None else [i for i in pending if 0 <= i < len(groups)]
    workers = max_workers or max(1, min(len(indices), 8))

    records: list[dict] = []
    if indices:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(review_group, repo_root, i, groups[i]): i for i in indices}
            for fut in concurrent.futures.as_completed(futures):
                records.append(fut.result())
        records.sort(key=lambda r: r["index"])

    all_group_files = sorted({f for g in groups for f in g.get("files", [])})
    wholediff = review_wholediff(repo_root, all_group_files)

    groups_clean = all(r["approved"] for r in records) if records else True
    wholediff_clean = wholediff["verdict"] in _APPROVE_VERDICTS
    return {
        "base": _safe_head(repo_root),
        "groups": records,
        "wholediff": wholediff,
        "all_clean": groups_clean and wholediff_clean,
    }


def _safe_head(repo_root: Path) -> str:
    try:
        return _git(["rev-parse", "HEAD"], repo_root)
    except subprocess.CalledProcessError:
        return ""


def hash_commit(repo_root: Path, sha: str) -> str:
    """Canonical diff_hash of a *landed* commit (vs its parent).

    ``git diff <sha>~1 <sha>`` reproduces, byte-for-byte, the ``git diff
    --cached`` text the commit was staged from (same blob pair + paths), so
    this matches the approval hash written at pre-review time. Computed in
    Python through ``approvals.diff_hash`` so the audit cannot drift from the
    write side. Commits in ``base..HEAD`` always have a parent (``base``), so
    there is no root-commit edge case.
    """
    diff = _git(["diff", f"{sha}~1", sha], repo_root)
    return approvals.diff_hash(diff)


def verify_range(repo_root: Path, base: str) -> dict:
    """Re-derive every ``base..HEAD`` commit's canonical diff_hash from git.

    The post-Land integrity audit: the workflow checks each returned hash is
    in its trusted approved-set. Run by a verifier agent that is NOT the
    committer (role separation), reading the immutable git history.
    """
    shas = [s for s in _git(["log", "--reverse", "--format=%H", f"{base}..HEAD"], repo_root).splitlines() if s]
    return {"commits": [{"sha": s, "diff_hash": hash_commit(repo_root, s)} for s in shas]}


def _parse_pending(raw: str | None) -> list[int] | None:
    """Parse ``--pending`` ("1,3,5") into ints. Raises ValueError on a malformed
    token so the CLI can report it cleanly instead of crashing with a traceback."""
    if not raw:
        return None
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.lstrip("-").isdigit():
            raise ValueError(f"--pending expects comma-separated integers, got {tok!r}")
        out.append(int(tok))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parallel pre-review of planned commit groups.")
    parser.add_argument("--plan", default=None, help="path to JSON: {groups:[{message,files}]}")
    parser.add_argument(
        "--verify-range",
        default=None,
        metavar="BASE",
        help="audit mode: emit canonical diff_hash for each BASE..HEAD commit",
    )
    parser.add_argument("--repo-root", default=None, help="repo root (default: cwd)")
    parser.add_argument("--reset", action="store_true", help="clear all approval markers first (round 1)")
    parser.add_argument("--pending", default=None, help="comma-separated group indices to (re)review; default all")
    parser.add_argument("--max-workers", type=int, default=None)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()

    if args.verify_range:
        json.dump(verify_range(repo_root, args.verify_range), sys.stdout)
        sys.stdout.write("\n")
        return 0

    if not args.plan:
        parser.error("one of --plan or --verify-range is required")

    try:
        pending = _parse_pending(args.pending)
    except ValueError as exc:
        parser.error(str(exc))

    plan = json.loads(Path(args.plan).read_text())
    report = run_pre_review(
        repo_root=repo_root,
        plan=plan,
        pending=pending,
        reset=args.reset,
        max_workers=args.max_workers,
    )
    json.dump(report, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

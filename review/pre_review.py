"""Parallel pre-review orchestrator for the SDD Land phase.

The commit gate (`hook.py`) reviews one commit at a time and is slow (an LLM
call per commit). For a large task that lands ~10 logical commits, running the
gate sequentially can take hours. This module reviews every planned commit
group **in parallel** *before* the real commits, using the identical review
pipeline against a private git index per group (`GIT_INDEX_FILE`) so nothing
touches the shared working index. A group that passes review gets an approval
marker (`approvals.write_approval`) keyed on its **content key**; at commit time
`hook._maybe_fastpath` recomputes the same key from the staged blobs and skips
the redundant LLM review.

Content identity is the load-bearing invariant: the content key
(`approvals.content_hash` over each changed path's final blob sha — see
`_content_key`) must be identical on all three sides — this module's private
index, the real `git diff --cached` the committer stages, and the post-Land
audit of the landed commit (`commit_content_key`). Because the key depends only
on final content (not diff text or base), it survives base drift, rename
detection, and stash reconstruction — the exact cases where the old textual
diff hash silently missed. Group file-disjointness (validated by
`validate_plan`) keeps each group's base stable so its changed-path set does
not shift either. See `test_pre_review.py` for the proofs.

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
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import yaml

import approvals
from config import MAX_CHUNKS, MAX_PROD_LINES, PREREVIEW_MAX_CONCURRENT_CHUNKED
from consolidation import consolidate
from hook import applicable_lenses, count_added_production_lines
from orchestrator import run_multi_backend

# Verdicts that mean "the live gate would allow this commit" → safe to approve.
# BLOCK = upheld critical(s); EMPTY = every reviewer failed (don't approve —
# let the live gate retry); SKIP = no applicable lens (gate would exit 0 too).
_APPROVE_VERDICTS = frozenset({"OK", "SKIP"})

# Bound how many BIG (chunked) groups review AT ONCE. Each chunked review fans
# out uncapped internally (chunked.py: max_workers=len(jobs)), so the nested
# product groups×jobs is what melts the backend — not any single review. The
# realistic 2-3-big-commit feature still runs fully in parallel (the whole point:
# overlap the multi-round convergence latency). Small single-call groups never
# touch this. See config.PREREVIEW_MAX_CONCURRENT_CHUNKED.
_CHUNKED_SEM = threading.BoundedSemaphore(PREREVIEW_MAX_CONCURRENT_CHUNKED)


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


def _stage_into(repo_root: Path, files: list[str], index_file: str) -> None:
    """Stage ``files`` (vs HEAD) into the private index at ``index_file``.

    ``git add -- <files>`` mirrors exactly how the committer stages, so the
    resulting ``git diff --cached`` is the same text the real commit produces.
    """
    _git(["read-tree", "HEAD"], repo_root, index_file)
    _git(["add", "--", *files], repo_root, index_file)


def build_group(repo_root: Path, files: list[str]) -> tuple[str, str, str]:
    """Stage ``files`` into a throwaway private index; return (diff, names, content_key).

    Uses ``GIT_INDEX_FILE`` so the shared index/working tree is untouched and
    many groups can run concurrently. The diff/names drive the review and the
    too-big check; ``content_key`` is the base-independent fast-path key
    (:func:`approvals.content_hash`), computed from the *same* index so it
    matches what the real commit produces. Staged once — no double work.
    """
    fd, idx_path = tempfile.mkstemp(prefix="prereview-idx-")
    os.close(fd)
    try:
        _stage_into(repo_root, files, idx_path)
        diff = _git(["diff", "--cached"], repo_root, idx_path)
        names = _git(["diff", "--cached", "--name-only"], repo_root, idx_path)
        content_key = _content_key(repo_root, ["--cached"], idx_path)
        return diff, names, content_key
    finally:
        os.unlink(idx_path)


def build_group_diff(repo_root: Path, files: list[str]) -> tuple[str, str]:
    """(diff, names) for a group — the 2-tuple shim over :func:`build_group`.

    Kept for the whole-diff pass and call sites that don't need the content key.
    """
    diff, names, _ = build_group(repo_root, files)
    return diff, names


def _content_key(repo_root: Path, diff_args: list[str], index_file: str | None = None) -> str:
    """Content key for the given diff selector, via :func:`approvals.content_hash_from_raw`.

    ``diff_args`` selects the comparison: ``["--cached"]`` (index vs HEAD) for
    the staged sides, or ``["<sha>~1", "<sha>"]`` for a landed commit. The git
    invocation matches every other side (raw, full-index, NUL, no-renames) so
    the key cannot drift.
    """
    raw = _git(
        ["diff", "--raw", "--full-index", "-z", "--no-renames", *diff_args],
        repo_root,
        index_file,
    )
    return approvals.content_hash_from_raw(raw)


def commit_content_key(repo_root: Path, sha: str) -> str:
    """Content key of a *landed* commit (vs its parent) — the audit side.

    Computed from the commit's post-image blobs, so it equals the pre-review
    ``content_key`` of the group that produced it whenever the final bytes match
    — independent of base drift, diff formatting, or how it was staged. Commits
    in ``base..HEAD`` always have a parent, so there is no root-commit edge case.
    """
    return _content_key(repo_root, [f"{sha}~1", sha])


def _prereview_dir(repo_root: Path, index: int) -> Path:
    """Private per-group manifest + artifacts dir for a big group's chunked review.

    Each big group gets its OWN ``.review/prereview/group-<i>/`` so parallel
    chunk-reviews never clobber a sibling's manifest/state, and so none of it
    collides with the gate's top-level ``.review/manifest.yaml`` (left untouched,
    so a fall-through commit still auto-scaffolds normally). ``.review/`` is
    gitignored, so nothing here is ever committed.
    """
    return repo_root / ".review" / "prereview" / f"group-{index}"


def _git_raw(args: list[str], repo_root: Path, index_file: str) -> str:
    """Like :func:`_git` but returns UNSTRIPPED stdout, byte-for-byte matching
    ``validators.manifest._default_git_runner``.

    The chunked validator reads name-status/numstat through an injected runner;
    feeding it this (pointed at the group's private ``GIT_INDEX_FILE``) makes the
    pre-review read its private index identically to how the gate reads the real
    one — so the validation that passes here also passes at the gate.
    """
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        env={**os.environ, "GIT_INDEX_FILE": index_file},
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _scaffold_group_manifest(diff: str, names: str, repo_root: Path) -> str:
    """Empty per-group manifest for the pre-review agent to fill (mirrors the
    gate's auto-scaffold).

    Carries the correct ``diff_hash`` + a file checklist; ``chunks: []`` makes the
    validator report ``no_chunks`` until the agent supplies the split, which is how
    an un-filled scaffold reads as ``NEEDS_MANIFEST`` rather than passing review.
    """
    file_list = [f for f in names.splitlines() if f]
    related = (
        "default_related_files:\n  - CLAUDE.md" if (repo_root / "CLAUDE.md").exists() else "default_related_files: []"
    )
    header = [
        "# Big cohesive commit group — fill in `chunks:` so the chunked reviewers run.",
        f"#   <= {MAX_CHUNKS} chunks; each <= {MAX_PROD_LINES} added PROD lines",
        "#   (tests/docs/config do NOT count); every file in EXACTLY one chunk;",
        "#   prefer `line_ranges: all` for whole files. Do NOT edit diff_hash.",
        "#   This group is one commit of a consistent multi-commit branch: the",
        "#   working tree (Read/Grep) is authoritative — do not flag a symbol or",
        "#   test that lives in a sibling commit; record such cross-commit",
        "#   assumptions under cross_chunk_invariants instead.",
        "# Files in this group (each must appear in exactly one chunk):",
        *[f"#   - {f}" for f in file_list],
    ]
    return "\n".join(
        [
            "version: 1",
            f"diff_hash: {approvals.diff_hash(diff)}",
            "",
            *header,
            "",
            related,
            "cross_chunk_invariants: []",
            "chunks: []",
            "",
        ]
    )


def _manifest_claimed_files(manifest: dict) -> set[str]:
    """Every ``path`` the manifest's chunks claim — used to tell a stale-hash
    refresh (same files, line counts moved) from a real re-author (files added or
    removed)."""
    out: set[str] = set()
    for chunk in manifest.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        for entry in chunk.get("files") or []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                out.add(entry["path"])
    return out


def _refresh_manifest_diff_hash(mpath: Path, diff_hash: str) -> None:
    """Rewrite ONLY the ``diff_hash:`` line, preserving the agent's chunks/comments."""
    text = mpath.read_text(encoding="utf-8")
    new_text, n = re.subn(r"(?m)^diff_hash:.*$", f"diff_hash: {diff_hash}", text)
    mpath.write_text(new_text if n else f"diff_hash: {diff_hash}\n{text}", encoding="utf-8")


def _ensure_group_manifest(mpath: Path, diff: str, names: str, repo_root: Path) -> str:
    """Make ``mpath`` ready for a chunked review, or signal the agent to author it.

    Returns ``"ready"`` when a filled manifest already claims the current file-set
    (its ``diff_hash`` is refreshed in place so a fix round that only moved line
    counts does NOT bounce back to the agent — that keeps the LLM off the
    steady-state critical path, which is where the parallel-convergence win lives).
    Returns ``"needs_manifest"`` (writing a fresh empty scaffold) when there is no
    manifest yet, it is un-filled, or the changed file-set shifted so the split
    must be (re)authored.
    """
    current_files = {f for f in names.splitlines() if f}
    if mpath.exists():
        try:
            loaded = yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            loaded = {}
        if isinstance(loaded, dict) and _manifest_claimed_files(loaded) == current_files and current_files:
            _refresh_manifest_diff_hash(mpath, approvals.diff_hash(diff))
            return "ready"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(_scaffold_group_manifest(diff, names, repo_root), encoding="utf-8")
    return "needs_manifest"


def _review_big_group(repo_root: Path, index: int, group: dict, diff: str, names: str, content_key: str) -> dict:
    """Chunk-review a big cohesive group through the SAME pipeline the gate uses.

    Replaces the old ``TOO_BIG`` bail. Bailing left the group for the live gate,
    which reviewed it once AND then the post-Land audit force-reviewed it again
    (it was never pre-approved) — the exact double review SDD-002 kills. Instead
    pre-review chunk-reviews it now, in parallel with sibling groups (bounded by
    ``_CHUNKED_SEM``), against a per-group manifest + private index. CLEAN → write
    the marker so the commit fast-paths and is reviewed exactly once.

    When the per-group manifest is missing/un-filled/stale-by-file-set, returns
    ``NEEDS_MANIFEST`` so the pre-review agent authors the chunk split (as the
    committer already does at the gate) and re-runs.
    """
    message = group.get("message", "")
    dh = approvals.diff_hash(diff)
    review_dir = _prereview_dir(repo_root, index)
    mpath = review_dir / "manifest.yaml"
    if _ensure_group_manifest(mpath, diff, names, repo_root) == "needs_manifest":
        return _record(index, message, dh, content_key, "NEEDS_MANIFEST", True, False, [])

    from chunked import run_chunked_review  # lazy: heavy import, mirrors hook.py

    fd, idx_path = tempfile.mkstemp(prefix=f"prereview-chunk-{index}-")
    os.close(fd)
    try:
        # A PERSISTENT private index (build_group's is already unlinked): the
        # chunked validator reads it through `runner` for the whole call.
        _stage_into(repo_root, group.get("files", []), idx_path)
        with _CHUNKED_SEM:
            result = run_chunked_review(
                diff,
                names,
                repo_root,
                manifest_path=mpath,
                review_dir=review_dir,
                runner=lambda a: _git_raw(a, repo_root, idx_path),
            )
    finally:
        os.unlink(idx_path)

    if result.status == "manifest_invalid":
        # The agent's chunk split is invalid (coverage/size/stale ranges). Bounce
        # back with the validator's errors so it fixes the SAME manifest.
        return _record(index, message, dh, content_key, "NEEDS_MANIFEST", True, False, [result.blocking_text])
    if result.upheld_clusters:
        return _record(
            index, message, dh, content_key, "BLOCK", True, False, [c.canonical_line for c in result.upheld_clusters]
        )
    approvals.write_approval(repo_root, content_key)
    return _record(index, message, dh, content_key, "OK", True, True, [])


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
        diff, names, content_key = build_group(repo_root, files)
        if not diff:
            # Nothing to review (files unchanged vs HEAD). Treat as clean/no-op;
            # no marker (there is no commit to fast-path).
            return _record(index, message, approvals.diff_hash(diff), content_key, "SKIP", False, True, [])
        if count_added_production_lines(diff) >= MAX_PROD_LINES:
            # Big cohesive commit: chunk-review it through the SAME pipeline the
            # gate uses (per-group manifest + private index), in parallel with
            # sibling groups, instead of bailing TOO_BIG. CLEAN → marker →
            # fast-path → reviewed exactly once (no gate re-review, no audit
            # double-review). May return NEEDS_MANIFEST for the agent to author.
            return _review_big_group(repo_root, index, group, diff, names, content_key)
        verdict, blockers = _gate_verdict(diff, names)
        approved = verdict in _APPROVE_VERDICTS
        if approved:
            # Marker keyed on the base-independent content key — the commit-time
            # fast-path (hook._maybe_fastpath) recomputes the same key from the
            # staged blobs and matches it here.
            approvals.write_approval(repo_root, content_key)
        return _record(index, message, approvals.diff_hash(diff), content_key, verdict, False, approved, blockers)
    except Exception as exc:
        return _record(index, message, "", "", "ERROR", False, False, [f"review failed: {type(exc).__name__}: {exc}"])


def _record(
    index: int,
    message: str,
    diff_hash: str,
    content_key: str,
    verdict: str,
    too_big: bool,
    approved: bool,
    blockers: list[str],
) -> dict:
    # diff_hash is log/back-compat only; content_key is the load-bearing
    # fast-path key (written as the marker and matched by the gate + audit).
    return {
        "index": index,
        "message": message,
        "diff_hash": diff_hash,
        "content_key": content_key,
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


def verify_range(repo_root: Path, base: str) -> dict:
    """Re-derive every ``base..HEAD`` commit's content key from git.

    The post-Land integrity audit: the workflow checks each returned key is in
    its trusted approved-set (built from the pre-review markers). Run by a
    verifier agent that is NOT the committer (role separation), reading the
    immutable git history through :func:`commit_content_key`.
    """
    shas = [s for s in _git(["log", "--reverse", "--format=%H", f"{base}..HEAD"], repo_root).splitlines() if s]
    return {"commits": [{"sha": s, "content_key": commit_content_key(repo_root, s)} for s in shas]}


def _changed_paths(repo_root: Path) -> set[str]:
    """Working-tree paths that differ from HEAD: modified/deleted tracked + untracked.

    Used for the advisory completeness check in :func:`validate_plan`. Avoids
    porcelain rename parsing by unioning ``git diff --name-only HEAD`` (tracked
    changes, incl. deletions) with the untracked set.
    """
    tracked = _git(["diff", "--name-only", "HEAD"], repo_root).splitlines()
    untracked = _git(["ls-files", "--others", "--exclude-standard"], repo_root).splitlines()
    return {p for p in (*tracked, *untracked) if p}


def _normalize_plan(groups: list[dict]) -> dict:
    """Deterministically merge groups that share a path into disjoint groups.

    Connected-components over the "groups linked by a shared file" graph
    (union-find). Files are unioned (dedup, order-preserving) and messages
    joined. Output order follows the lowest original group index in each
    component, so the result is stable. This is the engine's guaranteed-disjoint
    fallback when the planner cannot produce a disjoint plan on its own.
    """
    n = len(groups)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    seen: dict[str, int] = {}
    for i, g in enumerate(groups):
        for f in g.get("files", []):
            if f in seen:
                union(seen[f], i)
            else:
                seen[f] = i

    merged: dict[int, dict] = {}
    for i, g in enumerate(groups):
        m = merged.setdefault(find(i), {"message": "", "files": []})
        for f in g.get("files", []):
            if f not in m["files"]:
                m["files"].append(f)
        msg = g.get("message", "")
        m["message"] = msg if not m["message"] else (f"{m['message']}; {msg}" if msg else m["message"])
    return {"groups": [merged[r] for r in sorted(merged)]}


def validate_plan(repo_root: Path, plan: dict) -> dict:
    """Deterministically check a commit plan is file-disjoint (fix A).

    The dominant fast-path failure was a single file split across groups
    (``tests/__init__.py`` across 5 groups): a group's isolated diff then never
    matches the whole-file diff, so the fast-path is guaranteed to miss, and
    committing one group changes another's base. This is a pure-data check (no
    LLM), which is what makes vertical-slice grouping reliable despite the
    planner being an LLM step.

    Returns:
      * ``overlaps`` — every path present in >=2 groups, with the group indices.
      * ``valid`` — True iff there are no overlaps (the hard, reliable signal).
      * ``uncovered`` — working-tree changed files in no group (ADVISORY only;
        does not flip ``valid``, since porcelain edge cases could false-positive).
      * ``normalized_plan`` — a guaranteed-disjoint plan (overlapping groups
        merged) the engine can fall back to if a re-prompt still overlaps.
    """
    groups: list[dict] = plan.get("groups", [])
    path_groups: dict[str, list[int]] = {}
    for i, g in enumerate(groups):
        for f in g.get("files", []):
            path_groups.setdefault(f, []).append(i)
    overlaps = [{"path": p, "groups": idxs} for p, idxs in sorted(path_groups.items()) if len(idxs) > 1]
    uncovered = sorted(_changed_paths(repo_root) - set(path_groups))
    return {
        "valid": not overlaps,
        "overlaps": overlaps,
        "uncovered": uncovered,
        "normalized_plan": _normalize_plan(groups),
    }


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
        help="audit mode: emit canonical content_key for each BASE..HEAD commit",
    )
    parser.add_argument(
        "--validate-plan",
        default=None,
        metavar="PLAN_JSON",
        help="validate mode: emit {valid,overlaps,uncovered,normalized_plan} for a commit plan",
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

    if args.validate_plan:
        plan = json.loads(Path(args.validate_plan).read_text())
        json.dump(validate_plan(repo_root, plan), sys.stdout)
        sys.stdout.write("\n")
        return 0

    if not args.plan:
        parser.error("one of --plan, --verify-range, or --validate-plan is required")

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

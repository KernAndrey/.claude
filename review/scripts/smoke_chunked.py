"""End-to-end smoke for the chunked-review pipeline.

What it exercises:
  1. Synthetic large commit (>300 prod lines) in a throwaway repo.
  2. A hand-written manifest (the writer's job in real flows; the
     scaffolder produces an empty template, not auto-classification).
  3. ``validators.manifest.validate`` accepts it.
  4. ``chunked.run_chunked_review`` runs end-to-end with the reviewer
     subprocess mocked — verifies dispatch, ID re-tagging, artifact
     writing, arbiter prompt assembly, and consolidation glue.
  5. ``archive.archive_review_dir`` moves ``.review/`` to
     ``.git/review-archive/<sha>/`` after a real ``git commit
     --no-verify`` (real backends are NOT invoked).
  6. Worktree case: a second worktree shares the same archive dir
     because ``--git-common-dir`` resolves to the parent.

Run: ``python3 -B ~/.claude/review/scripts/smoke_chunked.py``
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import chunked  # noqa: E402
from archive import archive_review_dir  # noqa: E402
from validators.manifest import validate as validate_manifest  # noqa: E402


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(cmd, cwd=cwd, text=True, env=env)


def _populate_repo(repo: Path) -> None:
    """Create a 320-prod-line commit across 3 layers."""
    for layer in ("models", "security", "views"):
        (repo / "addons" / "foo" / layer).mkdir(parents=True, exist_ok=True)

    # 200 lines of model code → models chunk
    (repo / "addons" / "foo" / "models" / "sale_order.py").write_text(
        "class SaleOrder:\n" + "".join(f"    field_{i} = None\n" for i in range(200))
    )
    # 80 lines of security CSV → security chunk (CSV is not "production code"
    # under is_production_code — so the chunk-size cap won't trip here)
    (repo / "addons" / "foo" / "security" / "ir.model.access.csv").write_text(
        "id,name,model_id,perm_read,perm_write\n" + "".join(f"row_{i},name_{i},sale.order,1,0\n" for i in range(80))
    )
    # 50 lines of view XML → views chunk
    (repo / "addons" / "foo" / "views" / "sale_order.xml").write_text(
        "<view>\n" + "".join(f"  <field name='f{i}'/>\n" for i in range(50)) + "</view>\n"
    )


def _fake_reviewer(_cfg: object, _sys: str, _user: str) -> tuple[str, str, int]:
    return (
        "Section 1 — File audit:\n"
        "- addons/foo/models/sale_order.py — REVIEWED\n\n"
        "Section 2 — Findings:\n"
        "- [CRITICAL] addons/foo/models/sale_order.py:42 — `field_42 = None` — missing default\n"
        "- [WARNING] addons/foo/models/sale_order.py:43 — `field_43 = None` — soft hint\n\n"
        "Summary: 1 CRITICAL, 1 WARNING across 1 file.\n",
        "",
        0,
    )


def _fake_arbiter(cfg: object, _sys: str, user: str) -> tuple[str, str, int]:
    # Capture how many incoming finding IDs we have so we cluster all of them.
    import re as _re

    ids = _re.findall(r"\[([a-z][a-z0-9_-]*-F\d+)\]", user)
    if not ids:
        return ("Summary: 0 UPHELD, 0 OVERTURN, 0 clusters total.\n", "", 0)
    cluster_line = f"[CLUSTER C1] {', '.join(ids)}"
    return (
        f"{cluster_line}\n[OVERTURN] C1 — synthetic test, theoretical\n"
        f"Summary: 0 UPHELD, 1 OVERTURN, 1 clusters total.\n"
        f"Chunked: 0 invariant-violations, 0 cross-chunk-overturns.\n",
        "",
        0,
    )


def _init_repo(repo: Path) -> str:
    """Init repo, populate, stage. Returns the staged diff text."""
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Smoke Test"], repo)
    _populate_repo(repo)
    _run(["git", "add", "-A"], repo)
    return _run(["git", "diff", "--cached"], repo)


def _hand_written_manifest(diff_text: str) -> str:
    """Inline the manifest the writer would produce by hand. Mirrors the
    test-repo's three-file layout (models/security/views)."""
    diff_hash = hashlib.sha256(diff_text.encode()).hexdigest()
    return textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {diff_hash}
        default_related_files: []
        cross_chunk_invariants: []
        chunks:
          - id: models
            rationale: "new sale order model"
            files:
              - path: addons/foo/models/sale_order.py
                line_ranges: all
            review_lenses: [bugs, architecture, tests]
          - id: security
            rationale: "ACL row for the new model"
            files:
              - path: addons/foo/security/ir.model.access.csv
                line_ranges: all
            review_lenses: [bugs, architecture]
          - id: views
            rationale: "form / tree view"
            files:
              - path: addons/foo/views/sale_order.xml
                line_ranges: all
            review_lenses: [architecture]
        """
    )


def _write_and_validate(repo: Path, diff_text: str) -> bool:
    os.chdir(repo)
    review_dir = repo / ".review"
    review_dir.mkdir(parents=True, exist_ok=True)
    manifest_text = _hand_written_manifest(diff_text)
    (review_dir / "manifest.yaml").write_text(manifest_text)
    print("[2] manifest: 3 chunk(s) → ['models', 'security', 'views']")
    validation = validate_manifest(manifest_text, diff_text, repo)
    if not validation.ok:
        print("[3] VALIDATION FAILED:\n" + validation.to_text())
        return False
    print("[3] manifest validates clean")
    return True


def _run_chunked_with_mocks(repo: Path, diff_text: str) -> chunked.ChunkedResult:
    counts = {"reviewer": 0, "arbiter": 0}

    def dispatch(cfg: object, s: str, u: str) -> tuple[str, str, int]:
        if "Findings to cluster" in u:
            counts["arbiter"] += 1
            return _fake_arbiter(cfg, s, u)
        counts["reviewer"] += 1
        return _fake_reviewer(cfg, s, u)

    with patch("hook.run_reviewer", side_effect=dispatch):
        result = chunked.run_chunked_review(diff_text, "addons/foo/...", repo)
    print(
        f"[4] chunked run: status={result.status} "
        f"jobs={counts['reviewer']} arbiter_calls={counts['arbiter']} "
        f"clusters={len(result.clusters)} upheld={len(result.upheld_clusters)} "
        f"wall_clock={result.metrics.get('wall_clock_seconds')}s"
    )
    return result


def _verify_archive(repo: Path, sha: str) -> bool:
    archive_dest = repo / ".git" / "review-archive" / sha
    if not archive_dest.is_dir():
        print(f"[7] FAIL: post-commit hook did not create {archive_dest}")
        print("    Confirm ~/.claude/git-hooks/post-commit is executable and core.hooksPath is set.")
        return False
    if (repo / ".review").exists():
        print("[7] FAIL: .review/ not wiped after post-commit archive")
        return False
    if not (archive_dest / "findings.json").is_file() or not (archive_dest / "raw").is_dir():
        print("[7] FAIL: archive missing expected entries")
        return False
    assert archive_review_dir(repo) is None  # idempotent on empty .review/
    print(f"[7] post-commit hook archived → .git/review-archive/{sha[:8]}/")
    return True


def _verify_worktree_archive_shared(repo: Path, tmp: str) -> bool:
    wt_dir = Path(tmp) / "worktree"
    _run(["git", "worktree", "add", "-q", str(wt_dir), "HEAD"], repo)
    wt_common = Path(_run(["git", "rev-parse", "--git-common-dir"], wt_dir).strip())
    if not wt_common.is_absolute():
        wt_common = (wt_dir / wt_common).resolve()
    expected = (repo / ".git" / "review-archive").resolve()
    if wt_common.resolve() / "review-archive" != expected:
        print(f"[8] worktree common-dir mismatch: wt={wt_common} expected={expected}")
        return False
    print(f"[8] worktree shares archive dir {expected}")
    return True


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "smoke-repo"
        repo.mkdir()
        diff_text = _init_repo(repo)
        print(f"[1] staged diff: {len(diff_text.splitlines())} lines")
        if not _write_and_validate(repo, diff_text):
            return 1
        result = _run_chunked_with_mocks(repo, diff_text)
        if result.status != "ok":
            print(f"[4] FAIL: {result.blocking_text}")
            return 1
        chunked.write_artifacts(result, repo)
        artifacts = sorted((repo / ".review").rglob("*"))
        print(f"[5] .review/ artifacts: {len(artifacts)} entries")
        for p in artifacts[:8]:
            print(f"    - {p.relative_to(repo)}")

        _run(
            ["git", "commit", "-m", "smoke"],
            repo,
            env={**os.environ, "_CLAUDE_HOOK_RUNNING": "1"},
        )
        sha = _run(["git", "rev-parse", "HEAD"], repo).strip()
        print(f"[6] commit: {sha[:8]}")

        if not _verify_archive(repo, sha):
            return 1
        if not _verify_worktree_archive_shared(repo, tmp):
            return 1

    print("\n✅ chunked-review smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

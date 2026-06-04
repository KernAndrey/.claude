"""Tests for ``pre_review`` — the parallel pre-review orchestrator.

The marquee test is ``test_diff_hash_survives_intervening_commit``: it proves
the load-bearing invariant that a group's pre-review diff is byte-identical to
the real staged diff even after another group has been committed and HEAD has
moved. Everything else (fast-path, approvals) is worthless if that drifts.

``_gate_verdict`` (the LLM review) is patched everywhere except where we
exercise git plumbing directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import approvals
import pre_review


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, msg: str) -> None:
    # `-c core.hooksPath=<empty>` (highest precedence) disables the inherited
    # global AI-review hook for test commits — a local `git config` override
    # does not win here, and the hook would otherwise run (slow) and fail the
    # preflight gate (no coverage.xml in a scratch repo).
    _git(repo, "-c", f"core.hooksPath={repo}/.nohooks", "commit", "-q", "-m", msg)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    (repo / ".nohooks").mkdir()
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _commit(repo, "base")


def _write(repo: Path, rel: str, content: str) -> None:
    (repo / rel).write_text(content)


# ---------------------------------------------------------------------------
# Diff identity — the invariant the whole feature rests on
# ---------------------------------------------------------------------------


def test_diff_hash_survives_intervening_commit(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    # Two file-disjoint groups changing different files.
    _write(repo, "a.py", "def a():\n    return 1\n")
    _write(repo, "b.py", "def b():\n    return 2\n")

    # Pre-review hashes group B against the current HEAD (base).
    pre_diff, _ = pre_review.build_group_diff(repo, ["b.py"])
    pre_hash = approvals.diff_hash(pre_diff)
    assert "index " in pre_diff  # blob-sha header present — the likely drift point

    # Now group A actually lands as a real commit → HEAD moves.
    _git(repo, "add", "a.py")
    _commit(repo, "group A")

    # The committer stages group B in the REAL index against the moved HEAD.
    _git(repo, "add", "b.py")
    real_diff = _git(repo, "diff", "--cached")
    real_hash = approvals.diff_hash(real_diff)

    assert real_diff == pre_diff  # byte-identical, including the `index` line
    assert real_hash == pre_hash


def test_commit_content_key_matches_staged_key(tmp_path: Path) -> None:
    """The audit content key of a landed commit == the pre-review content key.

    The verifier recomputes via ``git diff C~1 C``; pre-review computed it from
    the private staged index. They must agree (across an intervening commit that
    moves HEAD) or the integrity cross-check throws false positives — or, worse,
    false negatives.
    """
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "def a():\n    return 1\n")
    _write(repo, "b.py", "def b():\n    return 2\n")

    pre_b_key = pre_review.build_group(repo, ["b.py"])[2]

    # Land group A (intervening), then land group B as its own commit.
    _git(repo, "add", "a.py")
    _commit(repo, "group A")
    _git(repo, "add", "b.py")
    _commit(repo, "group B")
    sha_b = _git(repo, "rev-parse", "HEAD")

    assert pre_review.commit_content_key(repo, sha_b) == pre_b_key


def test_verify_range_lists_each_commit_content_key(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "a.py", "x = 1\n")
    pre_a_key = pre_review.build_group(repo, ["a.py"])[2]
    _git(repo, "add", "a.py")
    _commit(repo, "group A")

    report = pre_review.verify_range(repo, base)
    assert len(report["commits"]) == 1
    assert report["commits"][0]["content_key"] == pre_a_key


def test_build_group_diff_stages_only_group_files(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    _write(repo, "b.py", "y = 2\n")
    diff, names = pre_review.build_group_diff(repo, ["a.py"])
    assert "a.py" in names and "b.py" not in names
    assert "a.py" in diff and "b.py" not in diff
    # Private index must not have touched the shared index/working tree.
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_build_group_diff_does_not_mutate_shared_index(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    before = _git(repo, "status", "--porcelain")
    pre_review.build_group_diff(repo, ["a.py"])
    assert _git(repo, "status", "--porcelain") == before


# ---------------------------------------------------------------------------
# Content key (fix B) — the base-independent fast-path identity. These prove the
# key is identical on all three sides AND matches where the old textual hash
# missed (the concrete HCC-097 concern #58 failure).
# ---------------------------------------------------------------------------


def test_content_key_identical_on_all_three_sides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pre-review private index == real staged index == landed commit.

    Drift between any two of these sides is exactly the silent fast-path miss
    this feature kills, so the key must agree across all three — including after
    an intervening commit moves HEAD. Side 2 deliberately uses the *commit-time*
    function ``hook.get_staged_content_key`` (a separate function/subprocess from
    pre-review), since that is what actually matches the marker.
    """
    import hook

    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "def a():\n    return 1\n")
    _write(repo, "b.py", "def b():\n    return 2\n")

    # Side 1 — pre-review, against a private index.
    key_prereview = pre_review.build_group(repo, ["b.py"])[2]

    # Intervening commit (group A) moves HEAD.
    _git(repo, "add", "a.py")
    _commit(repo, "group A")

    # Side 2 — the REAL commit-time match side (hook), against the real index.
    _git(repo, "add", "b.py")
    monkeypatch.chdir(repo)
    key_staged = hook.get_staged_content_key()

    # Side 3 — the landed commit, re-derived by the post-Land audit.
    _commit(repo, "group B")
    key_audit = pre_review.commit_content_key(repo, _git(repo, "rev-parse", "HEAD"))

    assert key_prereview == key_staged == key_audit


def test_content_key_matches_across_base_drift_where_diff_hash_misses(tmp_path: Path) -> None:
    """The decisive #58 test: identical final bytes, DIFFERENT textual diff.

    Stage the same final content from two different base contents. The diff
    text differs (different pre-image) → the OLD textual ``diff_hash`` misses;
    the content key MATCHES because it depends only on the final blob. This is
    the lived HCC-097 failure (byte-identical files, fast-path still missed).
    """
    repo = tmp_path / "r"
    _init_repo(repo)

    # Base A: a.py = C0. Stage final content X.
    _write(repo, "a.py", "C0\n")
    _git(repo, "add", "a.py")
    _commit(repo, "base C0")
    _write(repo, "a.py", "FINAL\n")
    diff_a, _names_a, key_a = pre_review.build_group(repo, ["a.py"])

    # Drift the base: a.py = C1 (different), committed. Re-stage the SAME X.
    _write(repo, "a.py", "C1-different\n")
    _git(repo, "add", "a.py")
    _commit(repo, "base C1")
    _write(repo, "a.py", "FINAL\n")
    diff_b, _names_b, key_b = pre_review.build_group(repo, ["a.py"])

    # The textual diff (and its hash) differ — the old keying would have missed.
    assert diff_a != diff_b
    assert approvals.diff_hash(diff_a) != approvals.diff_hash(diff_b)
    # The content key matches — same final bytes.
    assert key_a == key_b


def test_content_key_handles_deletion_across_sides(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "doomed.py", "x = 1\n")
    _git(repo, "add", "doomed.py")
    _commit(repo, "add doomed")

    (repo / "doomed.py").unlink()
    key_pre = pre_review.build_group(repo, ["doomed.py"])[2]
    _git(repo, "add", "--", "doomed.py")  # stage the deletion, as the committer would
    key_staged = pre_review._content_key(repo, ["--cached"])
    _commit(repo, "delete doomed")
    key_audit = pre_review.commit_content_key(repo, _git(repo, "rev-parse", "HEAD"))

    assert key_pre == key_staged == key_audit


def test_content_key_treats_rename_as_delete_plus_add(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "old.py", "shared = 1\n")
    _git(repo, "add", "old.py")
    _commit(repo, "add old")

    _git(repo, "mv", "old.py", "new.py")  # stages the rename in the real index
    key_pre = pre_review.build_group(repo, ["old.py", "new.py"])[2]
    key_staged = pre_review._content_key(repo, ["--cached"])
    _commit(repo, "rename old->new")
    key_audit = pre_review.commit_content_key(repo, _git(repo, "rev-parse", "HEAD"))

    # --no-renames everywhere → consistent (old deleted + new added) on all sides.
    assert key_pre == key_staged == key_audit


def test_hook_staged_key_agrees_with_prereview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit-time match side computes the SAME key as the pre-review side.

    ``hook.get_staged_content_key`` and ``pre_review._content_key`` are distinct
    functions with distinct subprocess calls (one strips stdout, one does not).
    Their agreement — across modify, delete, AND rename in one staged set — is
    the cross-side invariant the whole fast-path rests on; without this the two
    could silently diverge and the suite would stay green.
    """
    import hook

    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "mod.py", "v = 1\n")
    _write(repo, "doomed.py", "d = 1\n")
    _write(repo, "old.py", "o = 1\n")
    _git(repo, "add", "mod.py", "doomed.py", "old.py")
    _commit(repo, "seed")

    _write(repo, "mod.py", "v = 2\n")  # modify
    (repo / "doomed.py").unlink()  # delete
    _git(repo, "mv", "old.py", "new.py")  # rename (git mv already stages both sides)
    _git(repo, "add", "--", "mod.py", "doomed.py")  # stage the modify + the deletion

    monkeypatch.chdir(repo)
    assert hook.get_staged_content_key() == pre_review._content_key(repo, ["--cached"])


# ---------------------------------------------------------------------------
# review_group — verdict → marker behavior
# ---------------------------------------------------------------------------


def test_review_group_writes_marker_on_ok(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    with patch("pre_review._gate_verdict", return_value=("OK", [])):
        rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["a.py"]})
    assert rec["verdict"] == "OK"
    assert rec["approved"] is True
    assert approvals.approval_exists(repo, rec["content_key"]) is True


def test_review_group_no_marker_on_block(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    with patch("pre_review._gate_verdict", return_value=("BLOCK", ["[F1] [CRITICAL] a.py:1 — bad"])):
        rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["a.py"]})
    assert rec["verdict"] == "BLOCK"
    assert rec["approved"] is False
    assert approvals.approval_exists(repo, rec["content_key"]) is False


# ---------------------------------------------------------------------------
# Big cohesive groups — SDD-002: chunk-review in pre-review (no TOO_BIG bail)
# ---------------------------------------------------------------------------


def _big_body(prefix: str = "v", n: int = 500) -> str:
    """>= MAX_PROD_LINES added production lines so the group takes the big path."""
    return "".join(f"{prefix}{i} = {i}\n" for i in range(n))


def _chunked_result(status: str = "ok", upheld: tuple = (), blocking_text: str = "") -> SimpleNamespace:
    """Mimic the fields ``_review_big_group`` reads off a ``ChunkedResult``."""
    clusters = [SimpleNamespace(canonical_line=line) for line in upheld]
    return SimpleNamespace(status=status, upheld_clusters=clusters, blocking_text=blocking_text)


def _manifest_all(diff: str, files: list[str]) -> str:
    """A filled manifest (one whole-file chunk per file) — enough for
    ``_ensure_group_manifest`` to read it as authored. Used where
    ``run_chunked_review`` is mocked, so chunk SIZE is not validated."""
    chunks = "".join(
        f"  - id: chunk_{i}\n    rationale: r\n    files:\n      - path: {f}\n        line_ranges: all\n"
        for i, f in enumerate(files, 1)
    )
    return (
        f"version: 1\ndiff_hash: {approvals.diff_hash(diff)}\n"
        "default_related_files: []\ncross_chunk_invariants: []\nchunks:\n" + chunks
    )


def _author_manifest(repo: Path, index: int, diff: str, files: list[str]) -> Path:
    mdir = repo / ".review" / "prereview" / f"group-{index}"
    mdir.mkdir(parents=True, exist_ok=True)
    mpath = mdir / "manifest.yaml"
    mpath.write_text(_manifest_all(diff, files))
    return mpath


def test_review_group_big_scaffolds_manifest_then_needs_author(tmp_path: Path) -> None:
    """No manifest yet → NEEDS_MANIFEST + a scaffold the agent fills; review is
    NOT run (would be reviewing an unauthored split)."""
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "big.py", _big_body())
    with patch("chunked.run_chunked_review", side_effect=AssertionError("must not review without a manifest")):
        rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["big.py"]})
    assert rec["verdict"] == "NEEDS_MANIFEST"
    assert rec["too_big"] is True
    assert rec["approved"] is False
    assert approvals.approval_exists(repo, rec["content_key"]) is False
    mpath = repo / ".review" / "prereview" / "group-0" / "manifest.yaml"
    assert mpath.exists()
    text = mpath.read_text()
    diff, _names, _ck = pre_review.build_group(repo, ["big.py"])
    assert f"diff_hash: {approvals.diff_hash(diff)}" in text  # correct hash for the agent
    assert "big.py" in text  # file checklist
    assert "chunks: []" in text  # empty → reads as NEEDS_MANIFEST until filled


def test_review_group_big_clean_chunked_writes_marker(tmp_path: Path) -> None:
    """Authored manifest + CLEAN chunked review → marker written, verdict OK, and
    the call was driven with the group's OWN manifest/artifacts dir + a runner."""
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "big.py", _big_body())
    diff, _names, content_key = pre_review.build_group(repo, ["big.py"])
    mpath = _author_manifest(repo, 0, diff, ["big.py"])
    with patch("chunked.run_chunked_review", return_value=_chunked_result("ok", ())) as m:
        rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["big.py"]})
    assert m.called
    _args, kw = m.call_args
    assert kw["manifest_path"] == mpath
    assert kw["review_dir"] == mpath.parent
    assert callable(kw["runner"])
    assert rec["verdict"] == "OK"
    assert rec["approved"] is True
    assert approvals.approval_exists(repo, content_key) is True


def test_review_group_big_block_writes_no_marker(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "big.py", _big_body())
    diff, _names, content_key = pre_review.build_group(repo, ["big.py"])
    _author_manifest(repo, 0, diff, ["big.py"])
    upheld = ("[F1] [CRITICAL] big.py:1 — real bug",)
    with patch("chunked.run_chunked_review", return_value=_chunked_result("ok", upheld)):
        rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["big.py"]})
    assert rec["verdict"] == "BLOCK"
    assert rec["approved"] is False
    assert any("real bug" in b for b in rec["blockers"])
    assert approvals.approval_exists(repo, content_key) is False


def test_review_group_big_invalid_manifest_bounces_to_author(tmp_path: Path) -> None:
    """A bad chunk split (validator failed) → NEEDS_MANIFEST carrying the errors,
    no marker."""
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "big.py", _big_body())
    diff, _names, content_key = pre_review.build_group(repo, ["big.py"])
    _author_manifest(repo, 0, diff, ["big.py"])
    bad = _chunked_result("manifest_invalid", (), blocking_text="chunk 'chunk_1': 700 added prod lines, max is 400")
    with patch("chunked.run_chunked_review", return_value=bad):
        rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["big.py"]})
    assert rec["verdict"] == "NEEDS_MANIFEST"
    assert rec["approved"] is False
    assert any("max is 400" in b for b in rec["blockers"])
    assert approvals.approval_exists(repo, content_key) is False


def test_review_big_group_real_pipeline_clean_writes_marker(tmp_path: Path) -> None:
    """Integration: a big group through the REAL run_chunked_review (reviewer
    subprocesses stubbed CLEAN) writes the approval marker and returns OK.

    Exercises the success path the mocked tests skip: validate against the private
    index → spawn the real chunk + whole-diff jobs → arbiter skipped (no findings)
    → _review_big_group writes the marker. This is the path a real /implement-wf
    run depends on, proven here without LLM calls."""
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "big.py", _big_body(n=500))
    diff, _names, content_key = pre_review.build_group(repo, ["big.py"])
    # A VALID 2-chunk split (one whole-file chunk would be oversized at 500 prod).
    mdir = repo / ".review" / "prereview" / "group-0"
    mdir.mkdir(parents=True)
    (mdir / "manifest.yaml").write_text(
        f"version: 1\ndiff_hash: {approvals.diff_hash(diff)}\n"
        "default_related_files: []\ncross_chunk_invariants: []\nchunks:\n"
        "  - id: chunk_1\n    rationale: r\n    files:\n      - path: big.py\n        line_ranges: [[1, 250]]\n"
        "  - id: chunk_2\n    rationale: r\n    files:\n      - path: big.py\n        line_ranges: [[251, 500]]\n"
    )
    spawned = {"n": 0}

    def clean_reviewer(*args: object, **kwargs: object) -> tuple[str, str, int]:
        spawned["n"] += 1
        # Non-empty output that parses to ZERO findings = a real CLEAN review.
        # (Empty stdout is treated as a reviewer FAILURE, not a pass.)
        return ("No issues found — reviewed clean.\n", "", 0)

    with patch("hook.run_reviewer", side_effect=clean_reviewer):
        rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["big.py"]})
    assert spawned["n"] > 0  # the real pipeline actually spawned reviewer jobs
    assert rec["verdict"] == "OK"
    assert rec["approved"] is True
    assert approvals.approval_exists(repo, content_key) is True


def test_ensure_group_manifest_refreshes_hash_when_fileset_unchanged(tmp_path: Path) -> None:
    """A fix round that only moved line counts must NOT bounce to the agent:
    same file-set → refresh diff_hash in place, keep the authored chunks."""
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "big.py", _big_body())
    diff, names, _ck = pre_review.build_group(repo, ["big.py"])
    mdir = repo / ".review" / "prereview" / "group-0"
    mdir.mkdir(parents=True)
    mpath = mdir / "manifest.yaml"
    # Author with a STALE (wrong) diff_hash but the correct file-set.
    mpath.write_text(_manifest_all("+totally different\n", ["big.py"]))
    status = pre_review._ensure_group_manifest(mpath, diff, names, repo)
    assert status == "ready"
    text = mpath.read_text()
    assert f"diff_hash: {approvals.diff_hash(diff)}" in text  # refreshed in place
    assert "chunk_1" in text  # authored chunks preserved (not re-scaffolded)


def test_ensure_group_manifest_rescaffolds_when_fileset_changes(tmp_path: Path) -> None:
    """Files added/removed → the split must be re-authored (fresh empty scaffold)."""
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "big.py", _big_body())
    _write(repo, "extra.py", "x = 1\n")
    diff, names, _ck = pre_review.build_group(repo, ["big.py", "extra.py"])
    mdir = repo / ".review" / "prereview" / "group-0"
    mdir.mkdir(parents=True)
    mpath = mdir / "manifest.yaml"
    # Manifest claims only big.py, but the group now also has extra.py.
    mpath.write_text(_manifest_all(diff, ["big.py"]))
    status = pre_review._ensure_group_manifest(mpath, diff, names, repo)
    assert status == "needs_manifest"
    assert "chunks: []" in mpath.read_text()  # re-scaffolded empty for the agent


def test_concurrent_big_groups_use_isolated_dirs_and_private_index(tmp_path: Path) -> None:
    """The load-bearing test: two big groups reviewed in parallel get DISTINCT
    per-group manifest + artifacts dirs (no collision), and the chunked review of
    each reads its OWN private index (not the empty real one)."""
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", _big_body("a"))
    _write(repo, "b.py", _big_body("b"))
    for idx, f in ((0, "a.py"), (1, "b.py")):
        diff, _n, _ck = pre_review.build_group(repo, [f])
        _author_manifest(repo, idx, diff, [f])

    seen: list[tuple] = []
    lock = threading.Lock()

    def fake(diff: str, files: str, repo_root: Path, **kw: object) -> SimpleNamespace:
        with lock:
            seen.append((kw["manifest_path"], kw["review_dir"]))
        # Prove the runner reads the group's PRIVATE index — the real index is
        # empty during pre-review, so a missing/wrong runner would yield "" here
        # and the chunked validator would falsely report 0 changed files.
        staged = kw["runner"](["diff", "--cached", "--name-only"])
        assert staged.strip(), "runner must read the private index with staged files"
        return _chunked_result("ok", ())

    plan = {"groups": [{"message": "a", "files": ["a.py"]}, {"message": "b", "files": ["b.py"]}]}
    # Mock the whole-diff pass too (run_pre_review reviews the union via _gate_verdict)
    # so the test exercises only the big-group chunked path, deterministically.
    with (
        patch("chunked.run_chunked_review", side_effect=fake),
        patch("pre_review._gate_verdict", return_value=("OK", [])),
    ):
        report = pre_review.run_pre_review(repo, plan, pending=None, reset=True, max_workers=4)

    assert report["all_clean"] is True
    assert len({m for m, _ in seen}) == 2  # distinct per-group manifests
    assert len({r for _, r in seen}) == 2  # distinct per-group artifacts dirs
    for f in ("a.py", "b.py"):
        _d, _n, ck = pre_review.build_group(repo, [f])
        assert approvals.approval_exists(repo, ck) is True


def test_chunked_validation_uses_injected_runner_for_private_index(tmp_path: Path) -> None:
    """Directly proves the runner is what makes the chunked validator see the
    staged change-set: with the private-index runner the manifest validates; with
    the default runner (real, empty index) it does not."""
    from validators.manifest import validate

    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "big.py", _big_body(n=500))
    diff, _names, _ck = pre_review.build_group(repo, ["big.py"])
    # Split the 500-line file into two <=400-prod-line chunks so the manifest is
    # genuinely valid (one whole-file chunk would be oversized).
    manifest = (
        f"version: 1\ndiff_hash: {approvals.diff_hash(diff)}\n"
        "default_related_files: []\ncross_chunk_invariants: []\nchunks:\n"
        "  - id: chunk_1\n    rationale: r\n    files:\n      - path: big.py\n        line_ranges: [[1, 250]]\n"
        "  - id: chunk_2\n    rationale: r\n    files:\n      - path: big.py\n        line_ranges: [[251, 500]]\n"
    )
    fd, idx = tempfile.mkstemp(prefix="prereview-chunk-test-")
    os.close(fd)
    pre_review._stage_into(repo, ["big.py"], idx)
    try:
        ok = validate(manifest, diff, repo, runner=lambda a: pre_review._git_raw(a, repo, idx))
        bad = validate(manifest, diff, repo)  # default runner → real (empty) index
    finally:
        os.unlink(idx)
    assert ok.ok is True  # private index → manifest covers the staged files
    assert bad.ok is False  # real index empty → validation fails → the runner mattered


def test_run_chunked_review_threads_per_group_paths(tmp_path: Path) -> None:
    """End-to-end (no mock): a custom manifest_path + review_dir + runner drive
    the real ``run_chunked_review``. An invalid manifest makes it short-circuit at
    validation (before any reviewer spawns), proving the runner is consulted and
    artifacts land under the per-group review_dir — the chunked.py plumbing."""
    import chunked

    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "big.py", _big_body())
    diff, names, _ck = pre_review.build_group(repo, ["big.py"])
    mdir = tmp_path / "g0"
    mdir.mkdir()
    mpath = mdir / "manifest.yaml"
    # Stale hash + one oversized whole-file chunk → validation fails (no reviewers).
    mpath.write_text(_manifest_all("+stale\n", ["big.py"]))
    runner_calls: list[list[str]] = []
    fd, idx = tempfile.mkstemp(prefix="prereview-chunk-test-")
    os.close(fd)
    pre_review._stage_into(repo, ["big.py"], idx)

    def runner(args: list[str]) -> str:
        runner_calls.append(args[:2])
        return pre_review._git_raw(args, repo, idx)

    try:
        result = chunked.run_chunked_review(diff, names, repo, manifest_path=mpath, review_dir=mdir, runner=runner)
    finally:
        os.unlink(idx)
    assert result.status == "manifest_invalid"
    assert runner_calls  # the injected runner drove the validate path
    assert (mdir / "state" / "00_validation.json").exists()  # per-group review_dir used for artifacts


def test_manifest_helpers_tolerate_malformed_input(tmp_path: Path) -> None:
    """Defensive paths: non-dict chunks/entries are ignored, and an unparseable
    manifest re-scaffolds rather than crashing the worker thread."""
    # Non-dict chunk ("oops") skipped; non-dict entry ("x") and a path-less entry
    # skipped; only the well-formed {"path": ...} is claimed.
    claimed = pre_review._manifest_claimed_files(
        {"chunks": ["oops", {"files": ["x", {"no": "path"}, {"path": "a.py"}]}]}
    )
    assert claimed == {"a.py"}
    assert pre_review._manifest_claimed_files({}) == set()

    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "big.py", _big_body())
    diff, names, _ck = pre_review.build_group(repo, ["big.py"])
    mdir = repo / ".review" / "prereview" / "group-0"
    mdir.mkdir(parents=True)
    mpath = mdir / "manifest.yaml"
    mpath.write_text("[unclosed flow")  # invalid YAML → yaml.YAMLError
    assert pre_review._ensure_group_manifest(mpath, diff, names, repo) == "needs_manifest"
    assert "chunks: []" in mpath.read_text()  # re-scaffolded for the agent


# ---------------------------------------------------------------------------
# run_pre_review — reset, aggregation, all_clean
# ---------------------------------------------------------------------------


def test_reset_clears_prior_markers(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    stale = approvals.diff_hash("+stale\n")
    approvals.write_approval(repo, stale)
    _write(repo, "a.py", "x = 1\n")
    plan = {"groups": [{"message": "m", "files": ["a.py"]}]}
    with patch("pre_review._gate_verdict", return_value=("OK", [])):
        pre_review.run_pre_review(repo, plan, pending=None, reset=True, max_workers=2)
    assert approvals.approval_exists(repo, stale) is False  # reset pruned it


def test_no_reset_preserves_prior_markers(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    kept = approvals.diff_hash("+kept\n")
    approvals.write_approval(repo, kept)
    plan = {"groups": [{"message": "m", "files": ["base.txt"]}]}
    with patch("pre_review._gate_verdict", return_value=("OK", [])):
        pre_review.run_pre_review(repo, plan, pending=[], reset=False, max_workers=2)
    assert approvals.approval_exists(repo, kept) is True


def test_all_clean_true_when_every_group_ok(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    _write(repo, "b.py", "y = 2\n")
    plan = {"groups": [{"message": "a", "files": ["a.py"]}, {"message": "b", "files": ["b.py"]}]}
    with patch("pre_review._gate_verdict", return_value=("OK", [])):
        report = pre_review.run_pre_review(repo, plan, pending=None, reset=True, max_workers=4)
    assert report["all_clean"] is True
    assert {r["index"] for r in report["groups"]} == {0, 1}


def test_all_clean_false_when_a_group_blocks(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")

    def fake_verdict(diff: str, names: str) -> tuple[str, list[str]]:
        return ("BLOCK", ["[F1] [CRITICAL] a.py:1 — x"]) if "a.py" in names else ("OK", [])

    plan = {"groups": [{"message": "a", "files": ["a.py"]}]}
    with patch("pre_review._gate_verdict", side_effect=fake_verdict):
        report = pre_review.run_pre_review(repo, plan, pending=None, reset=True, max_workers=2)
    assert report["all_clean"] is False


def test_real_staged_diff_fastpaths_via_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: a real staged change + a real marker → hook.main() skips the LLM.

    Uses NO mock for the key path — proves the pre-review write side
    (``_content_key`` over the staged index) and the commit-time match side
    (``hook.get_staged_content_key``) compute the SAME content key, so a marker
    written by pre-review is found by the fast-path.
    """
    import hook

    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    _git(repo, "add", "a.py")
    approvals.write_approval(repo, pre_review._content_key(repo, ["--cached"]))

    monkeypatch.chdir(repo)
    monkeypatch.setenv("SDD_REVIEW_FASTPATH", "1")
    with (
        patch("hook._verify_runner_configs"),
        patch("hook._check_staged_review_guard"),
        patch("hook.COVERAGE_GATE", MagicMock(enabled=True)),
        patch("hook.run_gate", return_value=0),
        patch("hook._maybe_dispatch_chunked", side_effect=AssertionError("must fast-path before chunked")),
        patch("hook._run_multi_backend_pipeline", side_effect=AssertionError("LLM must not run")),
        patch("hook.save_log"),
    ):
        with pytest.raises(SystemExit) as exc:
            hook.main()
    assert exc.value.code == 0


def test_real_staged_diff_without_marker_runs_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-safe end-to-end: no marker → the full review pipeline runs."""
    import hook

    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    _git(repo, "add", "a.py")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("SDD_REVIEW_FASTPATH", "1")
    with (
        patch("hook._verify_runner_configs"),
        patch("hook._check_staged_review_guard"),
        patch("hook.COVERAGE_GATE", MagicMock(enabled=True)),
        patch("hook.run_gate", return_value=0),
        patch("hook._maybe_dispatch_chunked"),
        patch("hook.applicable_lenses", return_value=["bugs"]),
        patch("hook._run_multi_backend_pipeline", return_value="OK") as m_pipeline,
    ):
        with pytest.raises(SystemExit) as exc:
            hook.main()
    assert exc.value.code == 0
    m_pipeline.assert_called_once()


def test_wholediff_block_gates_round_even_if_groups_clean(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")

    # Per-group review of a.py is OK; the union (whole-diff) review BLOCKs.
    # The per-group pass completes first (in the pool), then the whole-diff pass
    # runs — so key off call order using a mutable closure.
    def fake_verdict(diff: str, names: str) -> tuple[str, list[str]]:
        fake_verdict.calls += 1  # type: ignore[attr-defined]
        if fake_verdict.calls == 1:
            return ("OK", [])
        return ("BLOCK", ["[F1] [CRITICAL] a.py:1 — cross"])  # type: ignore[attr-defined]

    fake_verdict.calls = 0  # type: ignore[attr-defined]
    plan = {"groups": [{"message": "a", "files": ["a.py"]}]}
    with patch("pre_review._gate_verdict", side_effect=fake_verdict):
        report = pre_review.run_pre_review(repo, plan, pending=None, reset=True, max_workers=1)
    assert report["groups"][0]["approved"] is True
    assert report["wholediff"]["verdict"] == "BLOCK"
    assert report["all_clean"] is False


# ---------------------------------------------------------------------------
# Branch coverage: _gate_verdict, error/skip paths, CLI, parsing
# ---------------------------------------------------------------------------


def test_gate_verdict_skip_when_no_lens() -> None:
    with patch("pre_review.applicable_lenses", return_value=[]):
        assert pre_review._gate_verdict("diff", "x.md") == ("SKIP", [])


def test_gate_verdict_empty_when_all_reviewers_fail() -> None:
    bad = MagicMock(status="error", review_text="")
    with (
        patch("pre_review.applicable_lenses", return_value=["bugs"]),
        patch("pre_review.run_multi_backend", return_value=[bad]),
    ):
        assert pre_review._gate_verdict("diff", "x.py") == ("EMPTY", [])


def test_gate_verdict_block_on_upheld_clusters() -> None:
    ok = MagicMock(status="ok", review_text="finding text")
    cons = MagicMock(upheld_clusters=[MagicMock(canonical_line="[F1] [CRITICAL] x.py:1 — bad")])
    with (
        patch("pre_review.applicable_lenses", return_value=["bugs"]),
        patch("pre_review.run_multi_backend", return_value=[ok]),
        patch("pre_review.consolidate", return_value=cons),
    ):
        verdict, blockers = pre_review._gate_verdict("diff", "x.py")
    assert verdict == "BLOCK"
    assert blockers == ["[F1] [CRITICAL] x.py:1 — bad"]


def test_gate_verdict_ok_when_no_upheld() -> None:
    ok = MagicMock(status="ok", review_text="text")
    cons = MagicMock(upheld_clusters=[])
    with (
        patch("pre_review.applicable_lenses", return_value=["bugs"]),
        patch("pre_review.run_multi_backend", return_value=[ok]),
        patch("pre_review.consolidate", return_value=cons),
    ):
        assert pre_review._gate_verdict("diff", "x.py") == ("OK", [])


def test_review_group_error_on_any_failure(tmp_path: Path) -> None:
    # The docstring promises no exception escapes — a non-CalledProcessError
    # (e.g. a reviewer crash) must still become an ERROR record.
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    with patch("pre_review.build_group", side_effect=RuntimeError("boom")):
        rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["a.py"]})
    assert rec["verdict"] == "ERROR"
    assert rec["approved"] is False
    assert "boom" in rec["blockers"][0]


def test_review_group_skip_on_empty_diff(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)  # base.txt is committed and unchanged → empty diff
    rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["base.txt"]})
    assert rec["verdict"] == "SKIP"
    assert rec["approved"] is True


def test_review_wholediff_skip_on_no_files(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    assert pre_review.review_wholediff(repo, [])["verdict"] == "SKIP"


def test_run_pre_review_ignores_out_of_range_pending(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    plan = {"groups": [{"message": "m", "files": ["a.py"]}]}
    with patch("pre_review._gate_verdict", return_value=("OK", [])):
        report = pre_review.run_pre_review(repo, plan, pending=[99], reset=True, max_workers=1)
    assert report["groups"] == []  # index 99 filtered out, no crash


def test_parse_pending_valid_and_empty() -> None:
    assert pre_review._parse_pending("0,2, 3") == [0, 2, 3]
    assert pre_review._parse_pending("0,,2") == [0, 2]  # empty token between commas is skipped
    assert pre_review._parse_pending(None) is None
    assert pre_review._parse_pending("") is None


def test_safe_head_returns_empty_outside_git(tmp_path: Path) -> None:
    # rev-parse fails in a non-git dir → _safe_head swallows it and returns "".
    assert pre_review._safe_head(tmp_path) == ""


def test_parse_pending_rejects_non_integer() -> None:
    with pytest.raises(ValueError):
        pre_review._parse_pending("abc")


def test_main_verify_range_emits_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "a.py", "x = 1\n")
    _git(repo, "add", "a.py")
    _commit(repo, "A")
    rc = pre_review.main(["--verify-range", base, "--repo-root", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["commits"]) == 1


def test_main_plan_mode_emits_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"groups": [{"message": "m", "files": ["a.py"]}]}))
    with patch("pre_review._gate_verdict", return_value=("OK", [])):
        rc = pre_review.main(["--plan", str(plan_path), "--repo-root", str(repo), "--reset"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["all_clean"] is True


def test_main_requires_plan_or_verify_range(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        pre_review.main(["--repo-root", str(tmp_path)])


# ---------------------------------------------------------------------------
# validate_plan (fix A) — deterministic file-disjointness gate
# ---------------------------------------------------------------------------


def test_validate_plan_flags_overlap_and_merges(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    plan = {
        "groups": [
            {"message": "g0", "files": ["a.py", "shared.py"]},
            {"message": "g1", "files": ["b.py", "shared.py"]},
        ]
    }
    v = pre_review.validate_plan(repo, plan)
    assert v["valid"] is False
    assert v["overlaps"] == [{"path": "shared.py", "groups": [0, 1]}]
    # normalized_plan merges the two groups linked by shared.py into one disjoint group.
    assert len(v["normalized_plan"]["groups"]) == 1
    assert sorted(v["normalized_plan"]["groups"][0]["files"]) == ["a.py", "b.py", "shared.py"]


def test_normalize_plan_merges_multi_file_overlap(tmp_path: Path) -> None:
    # Two groups share BOTH files: union() is called twice on the same pair, so
    # the second call exercises the already-merged (ra == rb) branch.
    repo = tmp_path / "r"
    _init_repo(repo)
    plan = {
        "groups": [
            {"message": "g0", "files": ["x.py", "y.py"]},
            {"message": "g1", "files": ["x.py", "y.py"]},
        ]
    }
    v = pre_review.validate_plan(repo, plan)
    assert v["valid"] is False
    assert len(v["normalized_plan"]["groups"]) == 1
    assert sorted(v["normalized_plan"]["groups"][0]["files"]) == ["x.py", "y.py"]


def test_validate_plan_disjoint_passes(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    plan = {"groups": [{"message": "g0", "files": ["a.py"]}, {"message": "g1", "files": ["b.py"]}]}
    v = pre_review.validate_plan(repo, plan)
    assert v["valid"] is True
    assert v["overlaps"] == []
    assert len(v["normalized_plan"]["groups"]) == 2  # already disjoint → shape preserved


def test_validate_plan_uncovered_is_advisory(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "covered.py", "x = 1\n")
    _write(repo, "forgotten.py", "y = 2\n")
    plan = {"groups": [{"message": "g", "files": ["covered.py"]}]}
    v = pre_review.validate_plan(repo, plan)
    assert v["valid"] is True  # uncovered does NOT flip valid
    assert "forgotten.py" in v["uncovered"]
    assert "covered.py" not in v["uncovered"]


def test_main_validate_plan_emits_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "groups": [
                    {"message": "g0", "files": ["a.py", "shared.py"]},
                    {"message": "g1", "files": ["shared.py"]},
                ]
            }
        )
    )
    rc = pre_review.main(["--validate-plan", str(plan_path), "--repo-root", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["valid"] is False
    assert out["overlaps"][0]["path"] == "shared.py"
    assert "normalized_plan" in out


def test_main_bad_pending_exits_cleanly(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"groups": [{"message": "m", "files": ["base.txt"]}]}))
    with pytest.raises(SystemExit):
        pre_review.main(["--plan", str(plan_path), "--repo-root", str(repo), "--pending", "abc"])

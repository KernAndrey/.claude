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
import subprocess
from pathlib import Path
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


def test_hash_commit_matches_staged_hash(tmp_path: Path) -> None:
    """The audit hash of a landed commit == the pre-review hash of that group.

    The verifier recomputes via ``git diff C~1 C``; pre-review hashed ``git diff
    --cached``. They must agree or the integrity cross-check throws false
    positives (or, worse, false negatives).
    """
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "def a():\n    return 1\n")
    _write(repo, "b.py", "def b():\n    return 2\n")

    pre_b_hash = approvals.diff_hash(pre_review.build_group_diff(repo, ["b.py"])[0])

    # Land group A (intervening), then land group B as its own commit.
    _git(repo, "add", "a.py")
    _commit(repo, "group A")
    _git(repo, "add", "b.py")
    _commit(repo, "group B")
    sha_b = _git(repo, "rev-parse", "HEAD")

    assert pre_review.hash_commit(repo, sha_b) == pre_b_hash


def test_verify_range_lists_each_commit_hash(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "a.py", "x = 1\n")
    pre_a_hash = approvals.diff_hash(pre_review.build_group_diff(repo, ["a.py"])[0])
    _git(repo, "add", "a.py")
    _commit(repo, "group A")

    report = pre_review.verify_range(repo, base)
    assert len(report["commits"]) == 1
    assert report["commits"][0]["diff_hash"] == pre_a_hash


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
    assert approvals.approval_exists(repo, rec["diff_hash"]) is True


def test_review_group_no_marker_on_block(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    with patch("pre_review._gate_verdict", return_value=("BLOCK", ["[F1] [CRITICAL] a.py:1 — bad"])):
        rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["a.py"]})
    assert rec["verdict"] == "BLOCK"
    assert rec["approved"] is False
    assert approvals.approval_exists(repo, rec["diff_hash"]) is False


def test_review_group_too_big_left_to_live_gate(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    body = "".join(f"v{i} = {i}\n" for i in range(500))  # >= MAX_PROD_LINES added prod lines
    _write(repo, "big.py", body)
    # _gate_verdict must NOT be consulted for an over-limit group.
    with patch("pre_review._gate_verdict", side_effect=AssertionError("must not review")):
        rec = pre_review.review_group(repo, 0, {"message": "m", "files": ["big.py"]})
    assert rec["too_big"] is True
    assert rec["approved"] is False
    assert approvals.approval_exists(repo, rec["diff_hash"]) is False


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
    """End-to-end: a real staged diff + a real marker → hook.main() skips the LLM.

    Uses NO mock for the hash path — proves get_staged_diff's normalization
    matches approvals.diff_hash, so a marker written by pre-review is found by
    the commit-time fast-path.
    """
    import hook

    repo = tmp_path / "r"
    _init_repo(repo)
    _write(repo, "a.py", "x = 1\n")
    _git(repo, "add", "a.py")
    staged = _git(repo, "diff", "--cached")
    approvals.write_approval(repo, approvals.diff_hash(staged))

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
    with patch("pre_review.build_group_diff", side_effect=RuntimeError("boom")):
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


def test_main_bad_pending_exits_cleanly(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"groups": [{"message": "m", "files": ["base.txt"]}]}))
    with pytest.raises(SystemExit):
        pre_review.main(["--plan", str(plan_path), "--repo-root", str(repo), "--pending", "abc"])

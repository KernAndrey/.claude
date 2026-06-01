"""Unit tests for ``approvals`` — the canonical diff hash + approval markers.

The approval markers are the on-disk channel the commit-time fast-path reads:
presence of ``.review/approvals/<diff_hash>`` means "this exact staged diff was
pre-reviewed CLEAN, skip the LLM review". There is deliberately no crypto here —
integrity is enforced by the workflow (agent role separation + an independent
git-audit), not by signatures (the Workflow JS sandbox cannot verify them).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import approvals


def test_diff_hash_matches_canonical_sha256() -> None:
    diff = "diff --git a/x.py b/x.py\n@@ -0,0 +1 @@\n+print('hi')\n"
    assert approvals.diff_hash(diff) == hashlib.sha256(diff.encode()).hexdigest()


def test_diff_hash_is_deterministic() -> None:
    diff = "diff --git a/a b/a\n+one\n"
    assert approvals.diff_hash(diff) == approvals.diff_hash(diff)


def test_diff_hash_distinguishes_content() -> None:
    assert approvals.diff_hash("+one\n") != approvals.diff_hash("+two\n")


def test_approvals_dir_is_under_review(tmp_path: Path) -> None:
    assert approvals.approvals_dir(tmp_path) == tmp_path / ".review" / "approvals"


def test_write_then_exists(tmp_path: Path) -> None:
    h = approvals.diff_hash("+payload\n")
    assert approvals.approval_exists(tmp_path, h) is False
    approvals.write_approval(tmp_path, h)
    assert approvals.approval_exists(tmp_path, h) is True


def test_write_approval_is_idempotent(tmp_path: Path) -> None:
    h = approvals.diff_hash("+payload\n")
    approvals.write_approval(tmp_path, h)
    approvals.write_approval(tmp_path, h)  # must not raise
    assert approvals.approval_exists(tmp_path, h) is True


def test_exists_false_for_unknown_hash(tmp_path: Path) -> None:
    approvals.write_approval(tmp_path, approvals.diff_hash("+a\n"))
    assert approvals.approval_exists(tmp_path, approvals.diff_hash("+b\n")) is False


def test_clear_removes_all_markers(tmp_path: Path) -> None:
    h1 = approvals.diff_hash("+a\n")
    h2 = approvals.diff_hash("+b\n")
    approvals.write_approval(tmp_path, h1)
    approvals.write_approval(tmp_path, h2)
    approvals.clear_approvals(tmp_path)
    assert approvals.approval_exists(tmp_path, h1) is False
    assert approvals.approval_exists(tmp_path, h2) is False


def test_clear_on_missing_dir_is_noop(tmp_path: Path) -> None:
    approvals.clear_approvals(tmp_path)  # must not raise when nothing exists
    assert approvals.approvals_dir(tmp_path).exists() is False


def test_exists_rejects_path_traversal(tmp_path: Path) -> None:
    # A non-hex / path-bearing key must never resolve outside the approvals dir.
    assert approvals.approval_exists(tmp_path, "../../etc/passwd") is False


def test_write_approval_rejects_non_canonical_hash(tmp_path: Path) -> None:
    # Only genuine sha256 digests may become markers — a path/garbage key raises
    # rather than creating a file outside (or with a hostile name in) the dir.
    for bad in ["nothex", "../escape", "abc123", "g" * 64]:
        with pytest.raises(ValueError):
            approvals.write_approval(tmp_path, bad)

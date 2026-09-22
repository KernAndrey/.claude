"""Unit tests for scaffold_manifest.py."""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import scaffold_manifest

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


def _git_stub(
    diff_text: str,
    name_status: str,
    numstat: str,
) -> Callable[[list[str]], str]:
    def _git(args: list[str]) -> str:
        if args == ["diff", "--cached"]:
            return diff_text
        if args == ["diff", "--cached", "--name-status", "-M"]:
            return name_status
        if args == ["diff", "--cached", "--numstat"]:
            return numstat
        raise AssertionError(f"unexpected git args: {args}")

    return _git


def test_build_scaffold_diff_hash_determinism() -> None:
    diff = "diff --git a/f.py b/f.py\n+line\n"
    stub = _git_stub(diff, "M\tf.py\n", "1\t0\tf.py\n")

    with patch("scaffold_manifest._git", side_effect=stub):
        out1 = scaffold_manifest.build_scaffold(1, 8, 300)
        out2 = scaffold_manifest.build_scaffold(1, 8, 300)

    match1 = re.search(r"diff_hash: (\w+)", out1)
    match2 = re.search(r"diff_hash: (\w+)", out2)
    assert match1 is not None
    assert match2 is not None
    assert match1.group(1) == match2.group(1)
    assert match1.group(1) == hashlib.sha256(diff.strip().encode()).hexdigest()


def test_build_scaffold_staged_file_comments() -> None:
    diff = "diff --git a/f.py b/f.py\n+line\n"
    name_status = "A\tnew.py\nM\tmod.py\nD\tdel.py\nR100\told.py\trenamed.py\nC100\tbase.py\tcopied.py\n"
    numstat = "5\t0\tnew.py\n3\t1\tmod.py\n0\t4\tdel.py\n2\t0\trenamed.py\n1\t0\tcopied.py\n"
    stub = _git_stub(diff, name_status, numstat)

    with patch("scaffold_manifest._git", side_effect=stub):
        out = scaffold_manifest.build_scaffold(1, 8, 300)

    assert "new.py  (new, +5 lines)" in out
    assert "mod.py  (+3 -1)" in out
    assert "del.py  (deleted (-4))" in out
    assert "renamed.py  (renamed from old.py (+2 -0))" in out
    assert "copied.py  (copied from base.py (+1 -0))" in out


def test_build_scaffold_binary_file() -> None:
    diff = "diff --git a/img.png b/img.png\n"
    name_status = "M\timg.png\n"
    numstat = "-\t-\timg.png\n"
    stub = _git_stub(diff, name_status, numstat)

    with patch("scaffold_manifest._git", side_effect=stub):
        out = scaffold_manifest.build_scaffold(1, 8, 300)

    assert "img.png  (binary)" in out


def test_build_scaffold_chunk_count() -> None:
    diff = "diff --git a/f.py b/f.py\n+line\n"
    stub = _git_stub(diff, "M\tf.py\n", "1\t0\tf.py\n")

    with patch("scaffold_manifest._git", side_effect=stub):
        out3 = scaffold_manifest.build_scaffold(3, 8, 300)
        out5 = scaffold_manifest.build_scaffold(5, 8, 300)

    assert out3.count("id: chunk_") == 3
    assert out5.count("id: chunk_") == 5


def test_build_scaffold_no_staged_changes() -> None:
    stub = _git_stub("", "", "")

    with patch("scaffold_manifest._git", side_effect=stub):
        with pytest.raises(SystemExit) as exc_info:
            scaffold_manifest.build_scaffold(1, 8, 300)

    assert "no staged changes" in str(exc_info.value)


def test_write_scaffold_creates_file_and_dir(tmp_path: Path) -> None:
    text = "version: 1\n"
    path = scaffold_manifest.write_scaffold(text, tmp_path)
    assert path == tmp_path / ".review" / "manifest.yaml"
    assert path.read_text() == text


def test_main_print(capsys: pytest.CaptureFixture[str]) -> None:
    diff = "diff --git a/f.py b/f.py\n+line\n"
    stub = _git_stub(diff, "M\tf.py\n", "1\t0\tf.py\n")

    with patch("scaffold_manifest._git", side_effect=stub):
        rc = scaffold_manifest.main(["--print"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "diff_hash:" in captured.out


def test_main_repo_root(tmp_path: Path) -> None:
    diff = "diff --git a/f.py b/f.py\n+line\n"
    stub = _git_stub(diff, "M\tf.py\n", "1\t0\tf.py\n")

    with patch("scaffold_manifest._git", side_effect=stub):
        rc = scaffold_manifest.main(["--repo-root", str(tmp_path)])

    assert rc == 0
    path = tmp_path / ".review" / "manifest.yaml"
    assert path.exists()
    assert "diff_hash:" in path.read_text()


def test_main_chunks_out_of_range() -> None:
    with pytest.raises(SystemExit) as exc_info:
        scaffold_manifest.main(["--chunks", "0"])
    assert exc_info.value.code == 2


def test_main_chunks_too_large() -> None:
    with pytest.raises(SystemExit) as exc_info:
        scaffold_manifest.main(["--chunks", "99"])
    assert exc_info.value.code == 2


def test_main_chunks_at_max_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    """C26 boundary: args.chunks == config.MAX_CHUNKS must be accepted."""
    diff = "diff --git a/f.py b/f.py\n+line\n"
    stub = _git_stub(diff, "M\tf.py\n", "1\t0\tf.py\n")

    with patch("scaffold_manifest._git", side_effect=stub):
        rc = scaffold_manifest.main(["--chunks", str(config.MAX_CHUNKS), "--print"])

    assert rc == 0


def test_main_chunks_one_over_max_rejected() -> None:
    """C26 boundary: args.chunks == config.MAX_CHUNKS + 1 must be rejected."""
    with pytest.raises(SystemExit) as exc_info:
        scaffold_manifest.main(["--chunks", str(config.MAX_CHUNKS + 1)])
    assert exc_info.value.code == 2


def test_extract_new_path_brace_form() -> None:
    """Git rename numstat brace form: prefix/{old => new}suffix → prefix/newsuffix."""
    assert scaffold_manifest._extract_new_path("src/{old.py => new.py}") == "src/new.py"
    assert scaffold_manifest._extract_new_path("a/{b => c}/d.py") == "a/c/d.py"


def test_extract_new_path_simple_arrow_form() -> None:
    assert scaffold_manifest._extract_new_path("old.py => new.py") == "new.py"
    assert scaffold_manifest._extract_new_path("old.py -> new.py") == "new.py"


def test_extract_new_path_no_rename() -> None:
    assert scaffold_manifest._extract_new_path("src/file.py") == "src/file.py"


def test_parse_numstat_brace_rename_keyed_by_new_path() -> None:
    text = "5\t2\tsrc/{old.py => new.py}\n"
    parsed = scaffold_manifest._parse_numstat(text)
    assert parsed == {"src/new.py": (5, 2, False)}


def test_build_scaffold_includes_claude_md_when_present(tmp_path: Path) -> None:
    diff = "diff --git a/f.py b/f.py\n+line\n"
    stub = _git_stub(diff, "M\tf.py\n", "1\t0\tf.py\n")
    (tmp_path / "CLAUDE.md").write_text("# Guide\n")

    with patch("scaffold_manifest._git", side_effect=stub):
        out = scaffold_manifest.build_scaffold(1, 8, 300, repo_root=tmp_path)

    assert "- CLAUDE.md" in out


def test_build_scaffold_empty_related_files_when_claude_md_missing(tmp_path: Path) -> None:
    diff = "diff --git a/f.py b/f.py\n+line\n"
    stub = _git_stub(diff, "M\tf.py\n", "1\t0\tf.py\n")

    with patch("scaffold_manifest._git", side_effect=stub):
        out = scaffold_manifest.build_scaffold(1, 8, 300, repo_root=tmp_path)

    assert "default_related_files: []" in out
    assert "- CLAUDE.md" not in out

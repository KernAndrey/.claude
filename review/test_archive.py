"""Unit tests for archive.py."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

import archive


def test_archive_review_dir_no_review_dir(tmp_path: Path) -> None:
    assert archive.archive_review_dir(tmp_path) is None


def test_archive_review_dir_success(tmp_path: Path) -> None:
    review_dir = tmp_path / ".review"
    review_dir.mkdir()
    (review_dir / "manifest.yaml").write_text("version: 1\n")
    git_common = tmp_path / ".git"
    git_common.mkdir()

    def fake_check_output(cmd: list[str], **kwargs: object) -> str:
        if cmd == ["git", "rev-parse", "HEAD"]:
            return "abc123\n"
        if cmd == ["git", "rev-parse", "--git-common-dir"]:
            return str(git_common.relative_to(tmp_path)) + "\n"
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("archive.subprocess.check_output", side_effect=fake_check_output):
        dest = archive.archive_review_dir(tmp_path)

    assert dest is not None
    assert dest == git_common / "review-archive" / "abc123"
    assert (dest / "manifest.yaml").read_text() == "version: 1\n"
    assert not review_dir.exists()


def test_archive_review_dir_absolute_git_common_dir(tmp_path: Path) -> None:
    review_dir = tmp_path / ".review"
    review_dir.mkdir()
    (review_dir / "manifest.yaml").write_text("version: 1\n")
    git_common = tmp_path / "absolute" / ".git"
    git_common.mkdir(parents=True)

    def fake_check_output(cmd: list[str], **kwargs: object) -> str:
        if cmd == ["git", "rev-parse", "HEAD"]:
            return "abc123\n"
        if cmd == ["git", "rev-parse", "--git-common-dir"]:
            return str(git_common.resolve()) + "\n"
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("archive.subprocess.check_output", side_effect=fake_check_output):
        dest = archive.archive_review_dir(tmp_path)

    assert dest is not None
    assert dest == git_common / "review-archive" / "abc123"
    assert (dest / "manifest.yaml").read_text() == "version: 1\n"
    assert not review_dir.exists()


def test_archive_review_dir_git_failure(tmp_path: Path) -> None:
    review_dir = tmp_path / ".review"
    review_dir.mkdir()

    def fake_check_output(cmd: list[str], **kwargs: object) -> str:
        raise FileNotFoundError("git not found")

    with patch("archive.subprocess.check_output", side_effect=fake_check_output):
        result = archive.archive_review_dir(tmp_path)

    assert result is None
    assert review_dir.exists()


def test_archive_review_dir_replaces_existing_destination(tmp_path: Path) -> None:
    review_dir = tmp_path / ".review"
    review_dir.mkdir()
    (review_dir / "manifest.yaml").write_text("version: 2\n")
    git_common = tmp_path / ".git"
    git_common.mkdir()

    dest = git_common / "review-archive" / "abc123"
    dest.mkdir(parents=True)
    (dest / "stale.txt").write_text("old")

    def fake_check_output(cmd: list[str], **kwargs: object) -> str:
        if cmd == ["git", "rev-parse", "HEAD"]:
            return "abc123\n"
        if cmd == ["git", "rev-parse", "--git-common-dir"]:
            return str(git_common.relative_to(tmp_path)) + "\n"
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("archive.subprocess.check_output", side_effect=fake_check_output):
        result = archive.archive_review_dir(tmp_path)

    assert result is not None
    assert result == dest
    assert (dest / "manifest.yaml").read_text() == "version: 2\n"
    assert not (dest / "stale.txt").exists()
    assert not review_dir.exists()
    assert not dest.with_name(dest.name + ".tmp").exists()
    assert not dest.with_name(dest.name + ".old").exists()


def test_archive_review_dir_preserves_old_on_copy_failure(tmp_path: Path) -> None:
    review_dir = tmp_path / ".review"
    review_dir.mkdir()
    (review_dir / "manifest.yaml").write_text("version: 2\n")
    git_common = tmp_path / ".git"
    git_common.mkdir()

    dest = git_common / "review-archive" / "abc123"
    dest.mkdir(parents=True)
    (dest / "stale.txt").write_text("old")

    def fake_check_output(cmd: list[str], **kwargs: object) -> str:
        if cmd == ["git", "rev-parse", "HEAD"]:
            return "abc123\n"
        if cmd == ["git", "rev-parse", "--git-common-dir"]:
            return str(git_common.relative_to(tmp_path)) + "\n"
        raise AssertionError(f"unexpected command: {cmd}")

    with (
        patch("archive.subprocess.check_output", side_effect=fake_check_output),
        patch("archive.shutil.copytree", side_effect=shutil.Error("simulated copy failure")),
    ):
        result = archive.archive_review_dir(tmp_path)

    assert result is None
    assert review_dir.exists()
    assert dest.is_dir()
    assert (dest / "stale.txt").read_text() == "old"
    assert not dest.with_name(dest.name + ".tmp").exists()


def test_main_no_review_dir(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("archive.archive_review_dir", return_value=None):
        rc = archive.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_success(capsys: pytest.CaptureFixture[str]) -> None:
    dest = Path("/fake/archive/abc123")
    with patch("archive.archive_review_dir", return_value=dest):
        rc = archive.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "chunked review archived" in captured.out
    assert str(dest) in captured.out


# ---------------------------------------------------------------------------
# _atomic_copytree — direct unit tests
# ---------------------------------------------------------------------------


def test_atomic_copytree_creates_fresh_destination(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "file.txt").write_text("content")
    dest = tmp_path / "dest"

    archive._atomic_copytree(src, dest)

    assert (dest / "file.txt").read_text() == "content"
    assert not dest.with_name(dest.name + ".tmp").exists()
    assert not dest.with_name(dest.name + ".old").exists()


def test_atomic_copytree_replaces_existing_destination(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "file.txt").write_text("new content")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "file.txt").write_text("old content")
    (dest / "stale.txt").write_text("stale")

    archive._atomic_copytree(src, dest)

    assert (dest / "file.txt").read_text() == "new content"
    assert not (dest / "stale.txt").exists()
    assert not dest.with_name(dest.name + ".tmp").exists()
    assert not dest.with_name(dest.name + ".old").exists()


def test_atomic_copytree_cleans_up_tmp_on_copy_failure(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"

    with patch("archive.shutil.copytree", side_effect=shutil.Error("fake")):
        with pytest.raises(shutil.Error, match="fake"):
            archive._atomic_copytree(src, dest)

    assert not dest.exists()
    assert not dest.with_name(dest.name + ".tmp").exists()
    assert not dest.with_name(dest.name + ".old").exists()


def test_atomic_copytree_preserves_old_on_failed_post_rename_step(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "file.txt").write_text("new content")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "file.txt").write_text("old content")

    original_rename = Path.rename

    class FakeRename:
        def __init__(self) -> None:
            self.calls = 0

        def __get__(self, obj: Path | None, objtype: type | None = None) -> object:
            if obj is None:
                return self

            def wrapper(target: Path) -> Path:
                self.calls += 1
                if self.calls == 2:
                    raise OSError("simulated cross-device link")
                return original_rename(obj, target)

            return wrapper

    with patch.object(Path, "rename", FakeRename()):
        with pytest.raises(OSError, match="simulated cross-device link"):
            archive._atomic_copytree(src, dest)

    assert dest.is_dir()
    assert (dest / "file.txt").read_text() == "old content"
    assert not dest.with_name(dest.name + ".tmp").exists()
    assert not dest.with_name(dest.name + ".old").exists()

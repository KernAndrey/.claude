"""Archive ``.review/`` working artifacts after a successful commit.

Invoked from ``~/.claude/git-hooks/post-commit`` (set up via
``scripts/install_chunked.sh``). For every commit that produced
``.review/`` content (chunked path), atomically copy it to
``<git_common_dir>/review-archive/<sha>/`` and wipe ``.review/`` so the
next commit starts clean.

Worktree-safe: ``git rev-parse --git-common-dir`` resolves to the
shared parent ``.git/``, so all worktrees archive into one place
keyed by sha.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _atomic_copytree(src: Path, dest: Path) -> None:
    """Copy ``src`` to ``dest`` atomically using sibling temp + rename.

    On failure any partial temp is cleaned up and the exception propagates.
    """
    dest_tmp = dest.with_name(dest.name + ".tmp")
    dest_old = dest.with_name(dest.name + ".old")
    moved_old = False
    try:
        shutil.copytree(src, dest_tmp)
        if dest.exists():
            dest.rename(dest_old)
            moved_old = True
        try:
            dest_tmp.rename(dest)
        except OSError:
            # Roll back so the invariant holds: dest is either the original
            # content or the new content, but never absent.
            if moved_old and dest_old.exists() and not dest.exists():
                dest_old.rename(dest)
            raise
        if dest_old.exists():
            shutil.rmtree(dest_old)
    except (OSError, shutil.Error):
        if dest_tmp.exists():
            try:
                shutil.rmtree(dest_tmp)
            except OSError:
                pass
        raise


def archive_review_dir(repo_root: Path | None = None) -> Path | None:
    """Move ``.review/`` to ``<git_common>/review-archive/<HEAD-sha>/``.

    Returns the destination path on success, ``None`` when there is
    nothing to archive (no ``.review/``). Atomic: on copy failure,
    leaves ``.review/`` in place so the developer can debug.
    """
    repo_root = Path.cwd() if repo_root is None else repo_root
    review_dir = repo_root / ".review"
    if not review_dir.is_dir():
        return None

    try:
        sha = _run_git(["rev-parse", "HEAD"])
        git_common = Path(_run_git(["rev-parse", "--git-common-dir"]))
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"archive: cannot resolve git refs ({exc}) — leaving .review/ in place", file=sys.stderr)
        return None

    if not git_common.is_absolute():
        git_common = (repo_root / git_common).resolve()

    dest = git_common / "review-archive" / sha
    try:
        _atomic_copytree(review_dir, dest)
    except (OSError, shutil.Error) as exc:
        print(f"archive: copytree failed ({exc}) — leaving .review/ in place", file=sys.stderr)
        return None

    try:
        shutil.rmtree(review_dir)
    except OSError as exc:
        print(
            f"archive: archived to {dest} but failed to wipe .review/ ({exc})",
            file=sys.stderr,
        )
    return dest


def main() -> int:
    dest = archive_review_dir()
    if dest is None:
        return 0  # nothing to do; not an error
    print(f"chunked review archived → {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

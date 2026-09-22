"""Make it impossible for this suite to touch the user's real account files.

A test once overwrote the live `~/.claude-profiles/vlad.json` with fixture
data, destroying a working login. The suite's own isolation is per-test and
therefore only as good as the setUp that applies it; this is the backstop that
holds even when a test forgets, or when an older revision is run.

It works at the filesystem call level: every write aimed at a real credential
path fails loudly instead of succeeding silently — judged on the resolved
path, so an alias cannot smuggle one past under a temp-directory name.
Emptying a file counts as writing to it, and so does changing its mode: both
leave the account unusable just as thoroughly as replacing its contents.
"""

from __future__ import annotations

import builtins
import io
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

HOME = Path.home()

#: Real files and directories no test may ever write to.
PROTECTED = (
    HOME / ".claude-profiles",
    HOME / ".claude" / ".credentials.json",
    HOME / ".claude.json",
)

#: The same locations with every symlink resolved — what candidates are
#: compared against, so an alias cannot name a protected file by another path.
PROTECTED_REAL = tuple(Path(os.path.realpath(p)) for p in PROTECTED)

#: Captured before any patching, so the guard's own tests can build the alias
#: they then prove is refused.
REAL_SYMLINK = os.symlink


def read_real_profiles() -> dict[str, bytes]:
    """Byte-for-byte contents of the real profiles as they are right now."""
    root = HOME / ".claude-profiles"
    if not root.exists():
        return {}
    return {p.name: p.read_bytes() for p in sorted(root.glob("*.json"))}


#: Taken while this file is imported — before the first test runs. Captured
#: per test instead, the "before" bytes would be read after any damage an
#: earlier test had already done, and every survival check would pass on a
#: profile that was destroyed hours ago.
BASELINE_CAPTURED_AT = time.monotonic()
ORIGINAL_PROFILES = read_real_profiles()


class RealAccountWriteError(AssertionError):
    """A test tried to write to the user's real credentials."""


def _base_of(dir_fd: int) -> str | None:
    """The directory an open fd refers to, or None if it cannot be named."""
    try:
        return os.readlink(f"/proc/self/fd/{dir_fd}")
    except OSError:
        return None


def _is_protected(path: object, dir_fd: int | None = None) -> bool:
    """True for anything that ends up inside a real credential location.

    The comparison is made on the resolved path, never on the name given: a
    symlink in a temp directory pointing at the real credentials writes
    straight through to them, and its own path says nothing but `tmp/alias`.

    A relative name resolves against `dir_fd` when one is supplied and
    against the cwd otherwise — `shutil.rmtree` walks a tree with bare entry
    names and a directory fd, so reading those as cwd-relative would refuse
    to clean up a temp directory whenever pytest runs from ~/.claude.
    """
    try:
        name = os.fspath(path)
    except TypeError:
        return False
    if dir_fd is not None and not os.path.isabs(name):
        base = _base_of(dir_fd)
        if base is None:
            # A closed or unnameable fd resolves to nothing. Falling back to
            # the cwd would be worse than useless: pytest runs from ~/.claude,
            # so every bare name would read as a real credential path.
            return False
        name = os.path.join(base, name)
    try:
        candidate = Path(os.path.realpath(name))
    except (TypeError, ValueError, OSError):
        return False
    return any(candidate == p or candidate.is_relative_to(p) for p in PROTECTED_REAL)


def _refuse(path: object) -> None:
    raise RealAccountWriteError(
        f"test attempted to write to the real account file {path!r}. "
        "Patch the cc_switch path constants to a temp directory instead."
    )


@pytest.fixture
def guard_api() -> dict[str, Any]:
    """The live guard's own objects, handed to the tests that check it.

    A fixture rather than an import: pytest may load this file under a
    path-qualified module name, and importing it again would define a second
    RealAccountWriteError that `pytest.raises` would not match.
    """
    return {
        "PROTECTED": PROTECTED,
        "RealAccountWriteError": RealAccountWriteError,
        "is_protected": _is_protected,
        "real_symlink": REAL_SYMLINK,
        "original_profiles": ORIGINAL_PROFILES,
        "read_real_profiles": read_real_profiles,
        "baseline_captured_at": BASELINE_CAPTURED_AT,
    }


def _writes(mode: str) -> bool:
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _guarded_open(real_open: Any) -> Any:  # noqa: ANN401 — wraps arbitrary stdlib callables
    def _inner(file: object, mode: str = "r", *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if _writes(mode) and _is_protected(file):
            _refuse(file)
        return real_open(file, mode, *args, **kwargs)

    return _inner


def _guarded_path_open(real_path_open: Any) -> Any:  # noqa: ANN401 — wraps arbitrary stdlib callables
    """`Path.open` goes through `io.open`, not the patched builtin.

    Without this the whole backstop has a hole exactly the width of the most
    idiomatic way to write a file in this codebase.
    """

    def _inner(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if _writes(mode) and _is_protected(self):
            _refuse(self)
        return real_path_open(self, mode, *args, **kwargs)

    return _inner


def _guarded_os_open(real_os_open: Any) -> Any:  # noqa: ANN401 — wraps arbitrary stdlib callables
    writing = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def _inner(path: object, flags: int, *args: Any, **kwargs: Any) -> int:  # noqa: ANN401
        if flags & writing and _is_protected(path, kwargs.get("dir_fd")):
            _refuse(path)
        return real_os_open(path, flags, *args, **kwargs)

    return _inner


def _guarded_two_path(fn: Any) -> Any:  # noqa: ANN401 — wraps arbitrary stdlib callables
    """`os.replace`/`rename`/`link`/`symlink` name each end against its own fd.

    Both ends are refused. For `os.link` that is the only defence there is: a
    hardlink is a second name for the same inode, and no amount of resolving
    a path afterwards can lead back to the protected one. `Path.symlink_to`
    and `Path.hardlink_to` need no wrapper of their own — they call these.
    """

    def _inner(src: object, dst: object, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        dst_fd = kwargs.get("dst_dir_fd", kwargs.get("dir_fd"))
        if _is_protected(dst, dst_fd) or _is_protected(src, kwargs.get("src_dir_fd")):
            _refuse(dst)
        return fn(src, dst, *args, **kwargs)

    return _inner


def _guarded_one_path(fn: Any) -> Any:  # noqa: ANN401 — wraps arbitrary stdlib callables
    def _inner(path: object, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if _is_protected(path, kwargs.get("dir_fd")):
            _refuse(path)
        return fn(path, *args, **kwargs)

    return _inner


def _guarded_method(fn: Any) -> Any:  # noqa: ANN401 — wraps arbitrary stdlib callables
    def _inner(self: Path, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if _is_protected(self):
            _refuse(self)
        return fn(self, *args, **kwargs)

    return _inner


@pytest.fixture(autouse=True)
def _forbid_real_account_writes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail any write to a real credential path, from anywhere in the suite."""
    monkeypatch.setattr(builtins, "open", _guarded_open(builtins.open))
    monkeypatch.setattr(io, "open", _guarded_open(io.open))
    monkeypatch.setattr(Path, "open", _guarded_path_open(Path.open))
    monkeypatch.setattr(os, "open", _guarded_os_open(os.open))
    monkeypatch.setattr(os, "replace", _guarded_two_path(os.replace))
    monkeypatch.setattr(os, "rename", _guarded_two_path(os.rename))
    monkeypatch.setattr(os, "unlink", _guarded_one_path(os.unlink))
    monkeypatch.setattr(os, "remove", _guarded_one_path(os.remove))
    monkeypatch.setattr(os, "rmdir", _guarded_one_path(os.rmdir))
    monkeypatch.setattr(os, "truncate", _guarded_one_path(os.truncate))
    monkeypatch.setattr(os, "chmod", _guarded_one_path(os.chmod))
    monkeypatch.setattr(os, "chown", _guarded_one_path(os.chown))
    monkeypatch.setattr(os, "utime", _guarded_one_path(os.utime))
    # `copy2` and `copyfile` are the two that reach a destination on their
    # own; `shutil.copy` and `shutil.move` are refused through them and
    # through `os.rename`, which is why they are not wrapped again here.
    monkeypatch.setattr(shutil, "copy2", _guarded_two_path(shutil.copy2))
    monkeypatch.setattr(shutil, "copyfile", _guarded_two_path(shutil.copyfile))
    monkeypatch.setattr(os, "mkdir", _guarded_one_path(os.mkdir))
    monkeypatch.setattr(os, "makedirs", _guarded_one_path(os.makedirs))
    monkeypatch.setattr(Path, "mkdir", _guarded_method(Path.mkdir))
    monkeypatch.setattr(Path, "write_text", _guarded_method(Path.write_text))
    monkeypatch.setattr(Path, "write_bytes", _guarded_method(Path.write_bytes))
    monkeypatch.setattr(Path, "unlink", _guarded_method(Path.unlink))
    monkeypatch.setattr(Path, "rmdir", _guarded_method(Path.rmdir))
    monkeypatch.setattr(os, "symlink", _guarded_two_path(os.symlink))
    monkeypatch.setattr(os, "link", _guarded_two_path(os.link))
    yield

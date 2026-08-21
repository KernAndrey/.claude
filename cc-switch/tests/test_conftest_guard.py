"""The backstop that stops this suite writing to real account files.

These exercise the guard itself. It exists because a test run once replaced
the live `~/.claude-profiles/vlad.json` with fixture data and destroyed a
working login, so a guard that quietly failed open would stay unnoticed until
it happened again.

Every probe below aims at a `guard-probe*` name that does not exist. That is
deliberate: if the guard ever fails open, the fallout is a stray file these
tests then catch, never an overwritten login.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import cc_switch as cc  # noqa: E402

REAL_PROFILES = Path.home() / ".claude-profiles"
REAL_CREDS = Path.home() / ".claude" / ".credentials.json"
REAL_MAIN = Path.home() / ".claude.json"

#: Read at import. conftest.py is imported before this module, so the
#: guard's baseline must carry a smaller stamp than this one.
_THIS_MODULE_IMPORTED_AT = time.monotonic()

PROBE = REAL_PROFILES / "guard-probe.json"
PROBE_DIR = REAL_PROFILES / "guard-probe-dir"
PROBE_VIA_DIR = "guard-probe-via-dir.json"


GuardApi = dict[str, object]


class TestProtectedPaths:
    def test_the_three_real_locations_are_covered(self, guard_api: GuardApi) -> None:
        assert set(guard_api["PROTECTED"]) == {REAL_PROFILES, REAL_CREDS, REAL_MAIN}

    def test_a_profile_inside_the_real_directory_is_protected(self, guard_api: GuardApi) -> None:
        assert guard_api["is_protected"](PROBE)

    def test_the_real_credentials_are_protected(self, guard_api: GuardApi) -> None:
        assert guard_api["is_protected"](REAL_CREDS)

    def test_the_real_config_is_protected(self, guard_api: GuardApi) -> None:
        assert guard_api["is_protected"](REAL_MAIN)

    def test_a_temp_path_is_not_protected(self, guard_api: GuardApi, tmp_path: Path) -> None:
        assert not guard_api["is_protected"](tmp_path / "vlad.json")

    def test_a_non_path_value_is_not_protected(self, guard_api: GuardApi) -> None:
        assert not guard_api["is_protected"](object())


class TestWritesAreRefused:
    """Each call the module actually uses to write credentials."""

    def test_save_profile_into_the_real_directory(self, guard_api: GuardApi) -> None:
        """This is the call that destroyed the real profile."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            cc.save_profile(
                "guard-probe-not-a-real-account",
                {"claudeAiOauth": {"accessToken": "fixture"}},
                {"emailAddress": "x@y"},
            )

    def test_write_text(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            REAL_CREDS.write_text("{}")

    def test_write_bytes(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            REAL_MAIN.write_bytes(b"{}")

    def test_builtin_open_for_writing(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]), open(PROBE, "w") as f:
            f.write("{}")

    def test_path_open_for_writing(self, guard_api: GuardApi) -> None:
        """`Path.open` bypasses the patched builtin — the idiomatic hole."""
        with pytest.raises(guard_api["RealAccountWriteError"]), PROBE.open("w") as f:
            f.write("{}")

    def test_path_open_for_appending(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]), REAL_CREDS.open("a") as f:
            f.write("{}")

    def test_path_open_for_reading_is_allowed(self, guard_api: GuardApi) -> None:
        if not REAL_MAIN.exists():
            pytest.skip("no real config on this machine")
        with REAL_MAIN.open() as f:
            assert f.read(1) == "{"

    def test_os_open_for_writing(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.open(str(REAL_CREDS), os.O_WRONLY | os.O_CREAT, 0o600)

    def test_os_replace_onto_a_real_path(self, guard_api: GuardApi) -> None:
        """The last step of every atomic write in the module."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.replace("/tmp/guard-probe-source", str(PROBE))

    def test_unlink(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            PROBE.unlink()

    def test_rmdir(self, guard_api: GuardApi) -> None:
        """Removing the real profiles directory is a write too."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            PROBE_DIR.rmdir()

    def test_os_rmdir(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.rmdir(str(REAL_PROFILES))

    def test_os_mkdir(self, guard_api: GuardApi) -> None:
        """`Path.mkdir` was wrapped, the os-level call was not."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.mkdir(str(PROBE_DIR))

    def test_os_makedirs(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.makedirs(str(PROBE_DIR / "deeper"))

    def test_mkdir(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            PROBE_DIR.mkdir()

    def test_copy2_onto_a_real_path(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            shutil.copy2("/tmp/guard-probe-source", str(REAL_MAIN))

    def test_the_error_names_the_offending_path(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"], match="guard-probe.json"):
            PROBE.write_text("{}")


class TestReadsAndTempWritesStillWork:
    """The guard blocks writes only — everything else keeps working."""

    def test_reading_the_real_profiles_is_allowed(self, guard_api: GuardApi) -> None:
        if not REAL_PROFILES.exists():
            pytest.skip("no real profiles on this machine")
        assert isinstance(list(REAL_PROFILES.glob("*.json")), list)

    def test_reading_a_real_file_is_allowed(self, guard_api: GuardApi) -> None:
        if not REAL_MAIN.exists():
            pytest.skip("no real config on this machine")
        with open(REAL_MAIN) as f:
            assert f.read(1) == "{"

    def test_writing_to_a_temp_path_is_allowed(self, guard_api: GuardApi, tmp_path: Path) -> None:
        target = tmp_path / "vlad.json"
        target.write_text(json.dumps({"ok": True}))
        assert json.loads(target.read_text())["ok"] is True

    def test_the_module_writes_happily_into_a_temp_dir(self, guard_api: GuardApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cc, "PROFILES_DIR", tmp_path)
        cc.save_profile("vlad", {"claudeAiOauth": {"accessToken": "fixture"}}, {"emailAddress": "x@y"})
        saved = json.loads((tmp_path / "vlad.json").read_text())
        assert saved["oauthAccount"]["emailAddress"] == "x@y"



class TestAliasesCannotSlipPast:
    """A second name for a real file is the way past a name-based guard.

    Every alias below points at a `guard-probe*` name, never at a real
    profile: if the guard failed open the fallout is a stray file the
    snapshot tests catch, not an overwritten login.
    """

    @staticmethod
    def _alias(guard_api: GuardApi, link: Path, target: Path) -> Path:
        """Build the alias with the unpatched call — creating one is refused."""
        guard_api["real_symlink"](target, link)
        return link

    def test_a_symlink_to_a_protected_file_is_protected(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        alias = self._alias(guard_api, tmp_path / "alias.json", PROBE)
        assert guard_api["is_protected"](alias)

    def test_writing_through_a_symlink_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        alias = self._alias(guard_api, tmp_path / "alias.json", PROBE)
        with pytest.raises(guard_api["RealAccountWriteError"]):
            alias.write_text("{}")

    def test_opening_a_symlink_for_writing_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        alias = self._alias(guard_api, tmp_path / "alias.json", PROBE)
        with pytest.raises(guard_api["RealAccountWriteError"]), open(alias, "w") as f:
            f.write("{}")

    def test_writing_inside_a_symlinked_directory_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        """The directory is protected, so everything reached through it is."""
        alias = self._alias(guard_api, tmp_path / "dir-alias", REAL_PROFILES)
        with pytest.raises(guard_api["RealAccountWriteError"]):
            (alias / PROBE_VIA_DIR).write_text("{}")

    def test_a_symlink_that_leads_nowhere_real_still_writes(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        """Resolving must not turn every symlink into a refusal."""
        alias = self._alias(guard_api, tmp_path / "plain", tmp_path / "target.json")
        alias.write_text(json.dumps({"ok": True}))
        assert json.loads((tmp_path / "target.json").read_text())["ok"] is True

    def test_creating_a_symlink_to_a_protected_path_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.symlink(str(PROBE), str(tmp_path / "alias.json"))

    def test_creating_a_hardlink_to_a_protected_path_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        """A hardlink shares the inode; no resolving can see it afterwards."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.link(str(PROBE), str(tmp_path / "alias.json"))

    def test_creating_a_link_inside_a_protected_directory_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.symlink(str(tmp_path / "source.json"), str(PROBE))

    def test_path_symlink_to_a_protected_path_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        """`Path.symlink_to` calls `os.symlink`, so the os wrapper covers it."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            (tmp_path / "alias.json").symlink_to(PROBE)

    def test_path_hardlink_to_a_protected_path_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            (tmp_path / "alias.json").hardlink_to(PROBE)

    def test_path_symlink_placed_inside_a_protected_directory_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            PROBE.symlink_to(tmp_path / "source.json")

    def test_a_symlink_between_two_temp_paths_is_allowed(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        os.symlink(str(tmp_path / "source.json"), str(tmp_path / "alias.json"))
        assert (tmp_path / "alias.json").is_symlink()


class TestRelativeNamesResolveAgainstTheCwd:
    """pytest runs from ~/.claude, where a bare name is the real file."""

    def test_a_bare_credentials_name_from_the_repo_root_is_protected(
        self, guard_api: GuardApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(REAL_CREDS.parent)
        assert guard_api["is_protected"](".credentials.json")

    def test_the_same_name_from_a_temp_cwd_is_not_protected(
        self, guard_api: GuardApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert not guard_api["is_protected"](".credentials.json")

    def test_writing_a_bare_name_from_a_temp_cwd_still_works(
        self, guard_api: GuardApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        Path(".credentials.json").write_text(json.dumps({"ok": True}))
        assert json.loads((tmp_path / ".credentials.json").read_text())["ok"] is True



class TestDirectoryFileDescriptors:
    """`shutil.rmtree` walks a tree as bare names against an open directory.

    Reading those as cwd-relative refused to clean up any temp directory,
    because pytest runs from ~/.claude where `.credentials.json` is real.
    """

    @staticmethod
    def _dir_fd(path: Path) -> int:
        """Read-only; the guard blocks writes, so this is allowed."""
        return os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)

    def test_a_bare_name_under_a_protected_dir_fd_is_protected(self, guard_api: GuardApi) -> None:
        if not REAL_PROFILES.exists():
            pytest.skip("no real profiles on this machine")
        fd = self._dir_fd(REAL_PROFILES)
        try:
            assert guard_api["is_protected"]("guard-probe.json", fd)
        finally:
            os.close(fd)

    def test_a_bare_name_under_a_temp_dir_fd_is_not_protected(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        fd = self._dir_fd(tmp_path)
        try:
            assert not guard_api["is_protected"](".credentials.json", fd)
        finally:
            os.close(fd)

    def test_an_absolute_name_ignores_the_dir_fd(self, guard_api: GuardApi, tmp_path: Path) -> None:
        """That is what the OS does with one, so the guard must agree."""
        fd = self._dir_fd(tmp_path)
        try:
            assert guard_api["is_protected"](str(REAL_CREDS), fd)
        finally:
            os.close(fd)

    def test_a_dir_fd_that_cannot_be_named_is_not_protected(
        self, guard_api: GuardApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to resolve against: the cwd is not a stand-in for it.

        Run from the directory that holds the real credentials, so falling
        back to it would report the bare name as protected.
        """
        monkeypatch.chdir(REAL_CREDS.parent)
        assert not guard_api["is_protected"](".credentials.json", 9999)

    def test_os_open_for_writing_under_a_protected_dir_fd_is_refused(
        self, guard_api: GuardApi
    ) -> None:
        if not REAL_PROFILES.exists():
            pytest.skip("no real profiles on this machine")
        fd = self._dir_fd(REAL_PROFILES)
        try:
            with pytest.raises(guard_api["RealAccountWriteError"]):
                os.open("guard-probe.json", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
        finally:
            os.close(fd)

    def test_os_open_for_writing_under_a_temp_dir_fd_is_allowed(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        fd = self._dir_fd(tmp_path)
        try:
            handle = os.open("written.json", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
            os.close(handle)
        finally:
            os.close(fd)
        assert (tmp_path / "written.json").exists()

    def test_a_rename_into_a_protected_dir_fd_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        """`os.replace` names its destination against `dst_dir_fd`."""
        if not REAL_PROFILES.exists():
            pytest.skip("no real profiles on this machine")
        source = tmp_path / "source.json"
        source.write_text("{}")
        fd = self._dir_fd(REAL_PROFILES)
        try:
            with pytest.raises(guard_api["RealAccountWriteError"]):
                os.replace(str(source), "guard-probe.json", dst_dir_fd=fd)
        finally:
            os.close(fd)

    def test_a_rename_out_of_a_protected_dir_fd_is_refused(
        self, guard_api: GuardApi, tmp_path: Path
    ) -> None:
        """Moving a real profile away is a write to it too."""
        if not REAL_PROFILES.exists():
            pytest.skip("no real profiles on this machine")
        fd = self._dir_fd(REAL_PROFILES)
        try:
            with pytest.raises(guard_api["RealAccountWriteError"]):
                os.replace("guard-probe.json", str(tmp_path / "taken.json"), src_dir_fd=fd)
        finally:
            os.close(fd)

    def test_removing_a_temp_tree_still_works(self, guard_api: GuardApi, tmp_path: Path) -> None:
        tree = tmp_path / "tree" / "deeper"
        tree.mkdir(parents=True)
        (tree / ".credentials.json").write_text("{}")
        shutil.rmtree(tmp_path / "tree")
        assert not (tmp_path / "tree").exists()


class TestUnnameablePaths:
    def test_a_name_with_a_null_byte_is_not_protected(self, guard_api: GuardApi) -> None:
        """`realpath` raises ValueError on it; the guard must not propagate that."""
        assert not guard_api["is_protected"]("bad\0name.json")


class TestEmptyingAndRelabellingAreWritesToo:
    """Nothing has to be replaced for a login to be destroyed."""

    def test_truncate(self, guard_api: GuardApi) -> None:
        """`os.truncate` reaches the file without opening it through the guard."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.truncate(str(REAL_CREDS), 0)

    def test_truncate_through_a_symlink(self, guard_api: GuardApi, tmp_path: Path) -> None:
        alias = tmp_path / "alias.json"
        guard_api["real_symlink"](PROBE, alias)
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.truncate(str(alias), 0)

    def test_truncating_a_temp_file_is_allowed(self, guard_api: GuardApi, tmp_path: Path) -> None:
        target = tmp_path / "big.json"
        target.write_text("{}" + " " * 100)
        os.truncate(str(target), 2)
        assert target.read_text() == "{}"

    def test_chmod(self, guard_api: GuardApi) -> None:
        """0o000 on the credentials locks the account out as surely as erasing it."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.chmod(str(REAL_CREDS), 0o000)

    def test_chown(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.chown(str(REAL_CREDS), 0, 0)

    def test_utime(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            os.utime(str(REAL_MAIN), (0, 0))

    def test_copyfile_onto_a_real_path(self, guard_api: GuardApi) -> None:
        with pytest.raises(guard_api["RealAccountWriteError"]):
            shutil.copyfile("/tmp/guard-probe-source", str(PROBE))

    def test_copy_onto_a_real_path(self, guard_api: GuardApi) -> None:
        """Refused through `copyfile`, which is what it calls."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            shutil.copy("/tmp/guard-probe-source", str(PROBE))

    def test_move_onto_a_real_path(self, guard_api: GuardApi) -> None:
        """Refused through `os.rename`, which it tries first."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            shutil.move("/tmp/guard-probe-source", str(PROBE))

    def test_moving_a_real_profile_away(self, guard_api: GuardApi, tmp_path: Path) -> None:
        """Taking one is a write to it, whatever the destination."""
        with pytest.raises(guard_api["RealAccountWriteError"]):
            shutil.move(str(PROBE), str(tmp_path / "taken.json"))

    def test_copying_between_temp_paths_is_allowed(self, guard_api: GuardApi, tmp_path: Path) -> None:
        source = tmp_path / "a.json"
        source.write_text("{}")
        shutil.copyfile(str(source), str(tmp_path / "b.json"))
        assert (tmp_path / "b.json").read_text() == "{}"


class TestTheRealProfilesSurvive:
    """Belt and braces: the refusals above must not have written anything.

    Compared against the baseline the guard captured as it loaded, never
    against assumed properties of the user's data — a profile shape we did
    not anticipate would otherwise fail the suite while nothing had written
    to it. The baseline has to predate the whole run: read per test, its
    "before" bytes would already include damage an earlier test had done.
    """

    def test_no_probe_file_was_created(self, guard_api: GuardApi) -> None:
        if not REAL_PROFILES.exists():
            pytest.skip("no real profiles on this machine")
        strays = sorted(p.name for p in REAL_PROFILES.glob("guard-probe*"))
        assert strays == [], f"the guard let a write through: {strays}"

    def test_no_probe_file_arrived_through_an_alias(self, guard_api: GuardApi) -> None:
        """Named separately: a directory alias writes a name the glob shares."""
        if not REAL_PROFILES.exists():
            pytest.skip("no real profiles on this machine")
        assert not (REAL_PROFILES / PROBE_VIA_DIR).exists()

    def test_every_real_profile_is_byte_identical(self, guard_api: GuardApi) -> None:
        baseline: dict[str, bytes] = guard_api["original_profiles"]
        if not baseline:
            pytest.skip("no real profiles on this machine")
        for name, before in baseline.items():
            after = (REAL_PROFILES / name).read_bytes()
            assert after == before, f"{name} was modified during the test run"

    def test_no_real_profile_disappeared(self, guard_api: GuardApi) -> None:
        baseline: dict[str, bytes] = guard_api["original_profiles"]
        if not baseline:
            pytest.skip("no real profiles on this machine")
        for name in baseline:
            assert (REAL_PROFILES / name).exists(), f"{name} was deleted during the test run"

    def test_the_baseline_predates_the_first_test(self, guard_api: GuardApi) -> None:
        """Captured as the guard loaded, not when an assertion asks for it.

        A per-test capture reads its "before" bytes after any damage an
        earlier test has already done, so the file it calls unchanged may
        have been destroyed long before this test started.
        """
        assert guard_api["baseline_captured_at"] < _THIS_MODULE_IMPORTED_AT

    def test_the_baseline_is_a_stored_copy_not_a_live_read(self, guard_api: GuardApi) -> None:
        """A live read agrees with the disk by construction and proves nothing."""
        fresh = guard_api["read_real_profiles"]()
        assert guard_api["original_profiles"] is not fresh
        assert guard_api["original_profiles"] == fresh

    def test_the_baseline_would_notice_a_change(self, guard_api: GuardApi, tmp_path: Path) -> None:
        """Guards the guard's guard: prove the comparison is not vacuous."""
        baseline: dict[str, bytes] = guard_api["original_profiles"]
        if not baseline:
            pytest.skip("no real profiles on this machine")
        name, before = next(iter(baseline.items()))
        copy = tmp_path / name
        copy.write_bytes(before + b" ")
        assert copy.read_bytes() != before

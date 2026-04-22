#!/usr/bin/env python3
"""Tests for cc_switch.py."""

from __future__ import annotations

import contextlib
import datetime as _dt
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

# Make the sibling cc_switch module importable regardless of where the
# suite is executed from.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import cc_switch as cc  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _perms(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@contextlib.contextmanager
def _silence() -> Iterator[None]:
    """Suppress stdout and stderr."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


SAMPLE_CREDS: dict = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-TOKEN",
        "refreshToken": "sk-ant-ort01-REFRESH",
        "expiresAt": 9999999999000,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
        "rateLimitTier": "default",
    }
}

SAMPLE_OAUTH: dict = {
    "accountUuid": "aaaa-1111",
    "emailAddress": "alice@example.com",
    "organizationUuid": "bbbb-2222",
    "displayName": "Alice",
    "organizationRole": "admin",
    "billingType": "stripe_subscription",
    "hasExtraUsageEnabled": False,
}

SAMPLE_CREDS_B: dict = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-TOKENB",
        "refreshToken": "sk-ant-ort01-REFRESHB",
        "expiresAt": 8888888888000,
        "scopes": ["user:inference"],
        "subscriptionType": "pro",
    }
}

SAMPLE_OAUTH_B: dict = {
    "accountUuid": "cccc-3333",
    "emailAddress": "bob@example.com",
    "organizationUuid": "dddd-4444",
    "displayName": "Bob",
    "organizationRole": "member",
    "billingType": "stripe_subscription",
    "hasExtraUsageEnabled": False,
}


class BaseTest(unittest.TestCase):
    """Set up isolated temp directories and patch module globals."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.claude_dir = self.tmp / "dot_claude"
        self.claude_dir.mkdir()
        self.creds_file = self.claude_dir / ".credentials.json"
        self.main_file = self.tmp / ".claude.json"
        self.profiles_dir = self.tmp / "profiles"
        self.profiles_dir.mkdir()
        self.active_file = self.profiles_dir / ".active"
        self.backups_dir = self.profiles_dir / ".backups"
        self.backups_dir.mkdir()

        self._patches = [
            patch.object(cc, "CLAUDE_DIR", self.claude_dir),
            patch.object(cc, "CREDS_FILE", self.creds_file),
            patch.object(cc, "MAIN_FILE", self.main_file),
            patch.object(cc, "PROFILES_DIR", self.profiles_dir),
            patch.object(cc, "ACTIVE_FILE", self.active_file),
            patch.object(cc, "BACKUPS_DIR", self.backups_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- convenience writers -------------------------------------------------

    def _write_creds(self, data: dict | None = None) -> None:
        d = data if data is not None else SAMPLE_CREDS
        self.creds_file.write_text(json.dumps(d, indent=2))
        os.chmod(self.creds_file, 0o600)

    def _write_main(self, oauth: dict | None = None, extra_keys: dict | None = None) -> None:
        data: dict = {
            "oauthAccount": oauth if oauth is not None else SAMPLE_OAUTH,
            "preserveMe": "untouched",
            "numericKey": 42,
            "nestedKey": {"a": 1},
        }
        if extra_keys:
            data.update(extra_keys)
        self.main_file.write_text(json.dumps(data, indent=2))
        os.chmod(self.main_file, 0o600)

    def _profile_path(self, name: str) -> Path:
        return self.profiles_dir / f"{name}.json"


# ===========================================================================
# Unit tests for pure helpers
# ===========================================================================


class TestDetectIndent(unittest.TestCase):
    def test_two_spaces(self) -> None:
        raw = '{\n  "k": 1\n}'
        self.assertEqual(cc.detect_indent(raw), 2)

    def test_four_spaces(self) -> None:
        raw = '{\n    "k": 1\n}'
        self.assertEqual(cc.detect_indent(raw), 4)

    def test_one_space(self) -> None:
        raw = '{\n "k": 1\n}'
        self.assertEqual(cc.detect_indent(raw), 1)

    def test_no_indent_fallback(self) -> None:
        self.assertEqual(cc.detect_indent('{"k":1}'), 2)


class TestProfilePath(BaseTest):
    def test_valid_name(self) -> None:
        p = cc.profile_path("work")
        self.assertEqual(p, self.profiles_dir / "work.json")

    def test_empty_name_dies(self) -> None:
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.profile_path("")
        self.assertEqual(cm.exception.code, cc.EXIT_USER)

    def test_slash_in_name_dies(self) -> None:
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.profile_path("a/b")
        self.assertEqual(cm.exception.code, cc.EXIT_USER)

    def test_backslash_in_name_dies(self) -> None:
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.profile_path("a\\b")
        self.assertEqual(cm.exception.code, cc.EXIT_USER)

    def test_dot_prefix_dies(self) -> None:
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.profile_path(".hidden")
        self.assertEqual(cm.exception.code, cc.EXIT_USER)


class TestEnsureDirs(BaseTest):
    def test_creates_missing_dirs(self) -> None:
        # Re-point to fresh dirs that don't yet exist.
        new_profiles = self.tmp / "profiles2"
        new_backups = new_profiles / ".backups"
        with patch.object(cc, "PROFILES_DIR", new_profiles), patch.object(cc, "BACKUPS_DIR", new_backups):
            cc.ensure_dirs()
            self.assertTrue(new_profiles.is_dir())
            self.assertTrue(new_backups.is_dir())

    def test_idempotent_when_dirs_exist(self) -> None:
        cc.ensure_dirs()
        cc.ensure_dirs()  # must not raise

    def test_mkdir_failure_dies(self) -> None:
        with (
            patch.object(Path, "mkdir", side_effect=OSError("disk full")),
            _silence(),
            self.assertRaises(SystemExit) as cm,
        ):
            cc.ensure_dirs()
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)


class TestConfirm(unittest.TestCase):
    def test_yes_variants(self) -> None:
        for ans in ("y", "Y", "yes", "YES", "  Yes  "):
            with patch("builtins.input", return_value=ans):
                self.assertTrue(cc.confirm("ok?"))

    def test_no_variants(self) -> None:
        for ans in ("", "n", "N", "no", "maybe"):
            with patch("builtins.input", return_value=ans):
                self.assertFalse(cc.confirm("ok?"))

    def test_eof_returns_false(self) -> None:
        with patch("builtins.input", side_effect=EOFError), _silence():
            self.assertFalse(cc.confirm("ok?"))

    def test_keyboard_interrupt_returns_false(self) -> None:
        with patch("builtins.input", side_effect=KeyboardInterrupt), _silence():
            self.assertFalse(cc.confirm("ok?"))


# ===========================================================================
# I/O helpers
# ===========================================================================


class TestReadJsonFile(BaseTest):
    def test_happy_path(self) -> None:
        self._write_creds()
        data, raw, tnl = cc.read_json_file(self.creds_file)
        self.assertIn("claudeAiOauth", data)
        self.assertIsInstance(raw, str)
        self.assertIsInstance(tnl, bool)

    def test_file_not_found_dies(self) -> None:
        with _silence(), self.assertRaises(SystemExit):
            cc.read_json_file(self.profiles_dir / "nonexistent.json")

    def test_invalid_json_dies(self) -> None:
        bad = self.tmp / "bad.json"
        bad.write_text("not json{")
        with _silence(), self.assertRaises(SystemExit):
            cc.read_json_file(bad)

    def test_json_array_dies(self) -> None:
        arr = self.tmp / "arr.json"
        arr.write_text("[1, 2, 3]")
        with _silence(), self.assertRaises(SystemExit):
            cc.read_json_file(arr)

    def test_oserror_permission_denied_dies(self) -> None:
        p = self.tmp / "perm.json"
        p.write_text("{}")
        with (
            patch.object(Path, "read_text", side_effect=PermissionError("denied")),
            _silence(),
            self.assertRaises(SystemExit) as cm,
        ):
            cc.read_json_file(p)
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)

    def test_trailing_newline_detected(self) -> None:
        p = self.tmp / "nl.json"
        p.write_text('{"k": 1}\n')
        _, _, tnl = cc.read_json_file(p)
        self.assertTrue(tnl)

    def test_no_trailing_newline_detected(self) -> None:
        p = self.tmp / "nonl.json"
        p.write_text('{"k": 1}')
        _, _, tnl = cc.read_json_file(p)
        self.assertFalse(tnl)


class TestAtomicWriteJson(BaseTest):
    def test_writes_correct_content(self) -> None:
        dest = self.tmp / "out.json"
        data = {"hello": "world", "n": 7}
        cc.atomic_write_json(dest, data, indent=2, trailing_nl=False)
        self.assertEqual(json.loads(dest.read_text()), data)

    def test_correct_permissions(self) -> None:
        dest = self.tmp / "out.json"
        cc.atomic_write_json(dest, {"x": 1}, indent=2, trailing_nl=False)
        self.assertEqual(_perms(dest), 0o600)

    def test_trailing_newline_written(self) -> None:
        dest = self.tmp / "nl.json"
        cc.atomic_write_json(dest, {"x": 1}, indent=2, trailing_nl=True)
        self.assertTrue(dest.read_text().endswith("\n"))

    def test_no_trailing_newline(self) -> None:
        dest = self.tmp / "nonl.json"
        cc.atomic_write_json(dest, {"x": 1}, indent=2, trailing_nl=False)
        self.assertFalse(dest.read_text().endswith("\n"))

    def test_no_tmp_left_on_success(self) -> None:
        dest = self.tmp / "out.json"
        cc.atomic_write_json(dest, {}, indent=2, trailing_nl=False)
        self.assertFalse((self.tmp / "out.json.tmp").exists())

    def test_indent_preserved(self) -> None:
        dest = self.tmp / "out4.json"
        cc.atomic_write_json(dest, {"k": "v"}, indent=4, trailing_nl=False)
        raw = dest.read_text()
        self.assertEqual(cc.detect_indent(raw), 4)

    def test_write_failure_dies_and_cleans_tmp(self) -> None:
        dest = self.tmp / "fail.json"
        with (
            patch.object(cc, "_open_write_0600", side_effect=OSError("ENOSPC")),
            _silence(),
            self.assertRaises(SystemExit) as cm,
        ):
            cc.atomic_write_json(dest, {"x": 1}, indent=2, trailing_nl=False)
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)
        self.assertFalse(dest.exists())
        self.assertFalse((self.tmp / "fail.json.tmp").exists())


class TestSaveReadProfile(BaseTest):
    def test_round_trip(self) -> None:
        cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        data = cc.read_profile("alice")
        self.assertEqual(data["credentials"], SAMPLE_CREDS)
        self.assertEqual(data["oauthAccount"], SAMPLE_OAUTH)
        self.assertIn("savedAt", data)

    def test_permissions_0600(self) -> None:
        cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        self.assertEqual(_perms(self._profile_path("alice")), 0o600)

    def test_save_profile_write_failure_dies(self) -> None:
        with (
            patch.object(cc, "_open_write_0600", side_effect=OSError("disk full")),
            _silence(),
            self.assertRaises(SystemExit) as cm,
        ):
            cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)

    def test_read_nonexistent_dies(self) -> None:
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.read_profile("ghost")
        self.assertEqual(cm.exception.code, cc.EXIT_USER)

    def test_read_corrupted_dies(self) -> None:
        p = self._profile_path("bad")
        p.write_text("not json")
        with _silence(), self.assertRaises(SystemExit):
            cc.read_profile("bad")

    def test_read_non_dict_json_dies(self) -> None:
        p = self._profile_path("listprof")
        p.write_text("[1, 2, 3]")
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.read_profile("listprof")
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)

    def test_read_profile_oserror_dies(self) -> None:
        p = self._profile_path("denied")
        p.write_text("{}")
        with (
            patch.object(Path, "read_text", side_effect=PermissionError("denied")),
            _silence(),
            self.assertRaises(SystemExit) as cm,
        ):
            cc.read_profile("denied")
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)

    def test_overwrite_existing(self) -> None:
        cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        cc.save_profile("alice", SAMPLE_CREDS_B, SAMPLE_OAUTH_B)
        data = cc.read_profile("alice")
        self.assertEqual(data["credentials"], SAMPLE_CREDS_B)


class TestActiveFile(BaseTest):
    def test_write_then_read(self) -> None:
        cc.write_active("work")
        self.assertEqual(cc.read_active(), "work")

    def test_read_missing_returns_none(self) -> None:
        self.assertIsNone(cc.read_active())

    def test_active_file_permissions(self) -> None:
        cc.write_active("x")
        self.assertEqual(_perms(self.active_file), 0o600)

    def test_read_active_oserror_returns_none(self) -> None:
        self.active_file.write_text("work\n")
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertIsNone(cc.read_active())

    def test_read_active_empty_file_returns_none(self) -> None:
        self.active_file.write_text("   \n")
        self.assertIsNone(cc.read_active())

    def test_write_active_failure_dies(self) -> None:
        with (
            patch.object(cc, "_open_write_0600", side_effect=OSError("disk full")),
            _silence(),
            self.assertRaises(SystemExit) as cm,
        ):
            cc.write_active("x")
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)


class TestRotateBackups(BaseTest):
    def _make_backup(self, name: str) -> None:
        p = self.backups_dir / name
        p.write_text("{}")

    def test_no_rotation_under_limit(self) -> None:
        for i in range(5):
            self._make_backup(f"claude.json.2026010{i}-120000")
        cc.rotate_backups()
        files = [p for p in self.backups_dir.iterdir() if p.name.startswith("claude.json.")]
        self.assertEqual(len(files), 5)

    def test_removes_oldest_when_over_limit(self) -> None:
        names = [f"claude.json.2026010{i}-120000" for i in range(7)]
        for n in names:
            self._make_backup(n)
        cc.rotate_backups()
        remaining = sorted(p.name for p in self.backups_dir.iterdir() if p.name.startswith("claude.json."))
        self.assertEqual(len(remaining), cc.BACKUP_KEEP)
        # oldest two must be gone
        self.assertNotIn(names[0], remaining)
        self.assertNotIn(names[1], remaining)
        # newest must survive
        self.assertIn(names[-1], remaining)

    def test_iterdir_failure_is_silent_noop(self) -> None:
        with patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            cc.rotate_backups()  # must not raise

    def test_unlink_failure_is_tolerated(self) -> None:
        names = [f"claude.json.2026010{i}-120000" for i in range(7)]
        for n in names:
            self._make_backup(n)
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            cc.rotate_backups()  # must not raise even if unlink fails


class TestBackupMain(BaseTest):
    def test_creates_backup(self) -> None:
        self._write_main()
        cc.backup_main()
        backups = list(self.backups_dir.glob("claude.json.*"))
        self.assertEqual(len(backups), 1)

    def test_backup_permissions(self) -> None:
        self._write_main()
        cc.backup_main()
        b = next(self.backups_dir.glob("claude.json.*"))
        self.assertEqual(_perms(b), 0o600)

    def test_no_main_file_is_noop(self) -> None:
        cc.backup_main()  # must not raise
        self.assertEqual(list(self.backups_dir.iterdir()), [])

    def test_copy_failure_dies(self) -> None:
        self._write_main()
        with (
            patch.object(shutil, "copy2", side_effect=OSError("disk full")),
            _silence(),
            self.assertRaises(SystemExit) as cm,
        ):
            cc.backup_main()
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)

    def test_rapid_backups_in_same_second_do_not_overwrite(self) -> None:
        """Regression: two backups within one second must not collide.

        Before the fix, the backup filename used `%Y%m%d-%H%M%S` resolution,
        so two calls to `backup_main()` inside the same second targeted the
        same path and the second `copy2` silently overwrote the first,
        losing a recovery point.
        """
        self._write_main()
        same_second = _dt.datetime(2026, 4, 22, 12, 0, 0)
        with patch.object(cc._dt, "datetime") as mock_dt:
            mock_dt.now.side_effect = [
                same_second.replace(microsecond=100_000),
                same_second.replace(microsecond=200_000),
            ]
            cc.backup_main()
            cc.backup_main()
        backups = sorted(self.backups_dir.glob("claude.json.*"))
        self.assertEqual(len(backups), 2, f"expected 2 distinct backups, got {backups}")


class TestReadCurrentState(BaseTest):
    def test_happy_path(self) -> None:
        self._write_creds()
        self._write_main()
        result = cc.read_current_state()
        self.assertIsNotNone(result)
        creds, oauth = result  # type: ignore[misc]
        self.assertEqual(creds, SAMPLE_CREDS)
        self.assertEqual(oauth["emailAddress"], "alice@example.com")

    def test_missing_creds_strict_dies(self) -> None:
        self._write_main()
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.read_current_state(strict=True)
        self.assertEqual(cm.exception.code, cc.EXIT_USER)

    def test_missing_creds_nonstrict_returns_none(self) -> None:
        self._write_main()
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_missing_claudeaioauth_strict_dies(self) -> None:
        self.creds_file.write_text(json.dumps({"wrongKey": {}}))
        self._write_main()
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.read_current_state(strict=True)
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)

    def test_missing_claudeaioauth_nonstrict_returns_none(self) -> None:
        self.creds_file.write_text(json.dumps({"wrongKey": {}}))
        self._write_main()
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_missing_oauth_key_strict_dies(self) -> None:
        self._write_creds()
        self.main_file.write_text(json.dumps({"otherKey": 1}))
        with _silence(), self.assertRaises(SystemExit):
            cc.read_current_state(strict=True)

    def test_missing_oauth_key_nonstrict_returns_none(self) -> None:
        self._write_creds()
        self.main_file.write_text(json.dumps({"otherKey": 1}))
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_missing_main_file_strict_dies(self) -> None:
        self._write_creds()
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.read_current_state(strict=True)
        self.assertEqual(cm.exception.code, cc.EXIT_USER)

    def test_missing_main_file_nonstrict_returns_none(self) -> None:
        self._write_creds()
        self.assertIsNone(cc.read_current_state(strict=False))


# ===========================================================================
# Command tests
# ===========================================================================


class TestCmdAdd(BaseTest):
    def _run_add(self, name: str) -> int:
        parser = cc.build_parser()
        args = parser.parse_args(["add", name])
        with _silence():
            return cc.cmd_add(args)

    def test_creates_profile(self) -> None:
        self._write_creds()
        self._write_main()
        self._run_add("work")
        self.assertTrue(self._profile_path("work").exists())

    def test_profile_content(self) -> None:
        self._write_creds()
        self._write_main()
        self._run_add("work")
        data = cc.read_profile("work")
        self.assertEqual(data["credentials"], SAMPLE_CREDS)
        self.assertEqual(data["oauthAccount"]["emailAddress"], "alice@example.com")

    def test_profile_permissions(self) -> None:
        self._write_creds()
        self._write_main()
        self._run_add("work")
        self.assertEqual(_perms(self._profile_path("work")), 0o600)

    def test_overwrite_confirmed(self) -> None:
        self._write_creds()
        self._write_main()
        self._run_add("work")
        new_creds = {**SAMPLE_CREDS, "claudeAiOauth": {**SAMPLE_CREDS["claudeAiOauth"], "subscriptionType": "pro"}}
        self._write_creds(new_creds)
        with patch("builtins.input", return_value="y"), _silence():
            cc.cmd_add(cc.build_parser().parse_args(["add", "work"]))
        data = cc.read_profile("work")
        self.assertEqual(data["credentials"]["claudeAiOauth"]["subscriptionType"], "pro")

    def test_overwrite_cancelled(self) -> None:
        self._write_creds()
        self._write_main()
        self._run_add("work")
        original_data = cc.read_profile("work")
        with patch("builtins.input", return_value="n"), _silence():
            cc.cmd_add(cc.build_parser().parse_args(["add", "work"]))
        self.assertEqual(cc.read_profile("work"), original_data)


class TestCmdUse(BaseTest):
    def _setup_two_profiles(self) -> None:
        self._write_creds(SAMPLE_CREDS)
        self._write_main(SAMPLE_OAUTH)
        cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        cc.save_profile("bob", SAMPLE_CREDS_B, SAMPLE_OAUTH_B)

    def test_switches_credentials(self) -> None:
        self._setup_two_profiles()
        with _silence():
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        creds, _, _ = cc.read_json_file(self.creds_file)
        self.assertEqual(creds["claudeAiOauth"]["accessToken"], "sk-ant-oat01-TOKENB")

    def test_switches_oauth_account(self) -> None:
        self._setup_two_profiles()
        with _silence():
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        main_data, _, _ = cc.read_json_file(self.main_file)
        self.assertEqual(main_data["oauthAccount"]["emailAddress"], "bob@example.com")

    def test_preserves_other_keys_in_main(self) -> None:
        self._setup_two_profiles()
        with _silence():
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        main_data, _, _ = cc.read_json_file(self.main_file)
        self.assertEqual(main_data["preserveMe"], "untouched")
        self.assertEqual(main_data["numericKey"], 42)
        self.assertEqual(main_data["nestedKey"], {"a": 1})

    def test_writes_active(self) -> None:
        self._setup_two_profiles()
        with _silence():
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        self.assertEqual(cc.read_active(), "bob")

    def test_creds_permissions(self) -> None:
        self._setup_two_profiles()
        with _silence():
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        self.assertEqual(_perms(self.creds_file), 0o600)

    def test_creates_main_backup(self) -> None:
        self._setup_two_profiles()
        with _silence():
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        backups = list(self.backups_dir.glob("claude.json.*"))
        self.assertEqual(len(backups), 1)

    def test_auto_backup_active_profile(self) -> None:
        self._setup_two_profiles()
        cc.write_active("alice")
        refreshed = {"claudeAiOauth": {**SAMPLE_CREDS["claudeAiOauth"], "accessToken": "sk-ant-oat01-REFRESHED"}}
        self._write_creds(refreshed)
        with _silence():
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        alice_data = cc.read_profile("alice")
        self.assertEqual(alice_data["credentials"]["claudeAiOauth"]["accessToken"], "sk-ant-oat01-REFRESHED")

    def test_profile_not_found_dies(self) -> None:
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.cmd_use(cc.build_parser().parse_args(["use", "ghost"]))
        self.assertEqual(cm.exception.code, cc.EXIT_USER)

    def test_preserves_main_indent(self) -> None:
        self._setup_two_profiles()
        with _silence():
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        raw = self.main_file.read_text()
        self.assertEqual(cc.detect_indent(raw), 2)

    def test_corrupted_profile_missing_keys_dies(self) -> None:
        p = self._profile_path("broken")
        p.write_text(json.dumps({"savedAt": "2026-01-01", "notes": "oops"}))
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.cmd_use(cc.build_parser().parse_args(["use", "broken"]))
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)

    def test_corrupted_profile_non_dict_fields_dies(self) -> None:
        p = self._profile_path("weird")
        p.write_text(json.dumps({"credentials": "str", "oauthAccount": 42}))
        with _silence(), self.assertRaises(SystemExit):
            cc.cmd_use(cc.build_parser().parse_args(["use", "weird"]))

    def test_missing_main_file_dies(self) -> None:
        cc.save_profile("bob", SAMPLE_CREDS_B, SAMPLE_OAUTH_B)
        self._write_creds(SAMPLE_CREDS)
        # main file absent
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        self.assertEqual(cm.exception.code, cc.EXIT_USER)

    def test_first_write_no_creds_but_claude_dir_present(self) -> None:
        """When .credentials.json doesn't exist but CLAUDE_DIR does, the switch still succeeds."""
        cc.save_profile("bob", SAMPLE_CREDS_B, SAMPLE_OAUTH_B)
        self._write_main(SAMPLE_OAUTH)
        # no creds file, but CLAUDE_DIR exists (set up in setUp)
        with _silence():
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        self.assertTrue(self.creds_file.exists())
        creds, _, _ = cc.read_json_file(self.creds_file)
        self.assertEqual(creds["claudeAiOauth"]["accessToken"], "sk-ant-oat01-TOKENB")

    def test_missing_claude_dir_and_creds_dies(self) -> None:
        cc.save_profile("bob", SAMPLE_CREDS_B, SAMPLE_OAUTH_B)
        self._write_main(SAMPLE_OAUTH)
        shutil.rmtree(self.claude_dir)
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))
        self.assertEqual(cm.exception.code, cc.EXIT_USER)

    def test_rollback_on_main_write_failure(self) -> None:
        """If writing MAIN_FILE fails after creds were swapped, creds are rolled back."""
        self._setup_two_profiles()
        original_token = SAMPLE_CREDS["claudeAiOauth"]["accessToken"]

        real_atomic = cc.atomic_write_json
        calls: list[Path] = []

        def flaky(path: Path, data: dict, indent: int, trailing_nl: bool) -> None:
            calls.append(path)
            # First MAIN_FILE write (after creds write) fails; allow rollback + others
            if path == self.main_file and calls.count(self.main_file) == 1:
                cc.die(f"simulated failure writing {path}")
            real_atomic(path, data, indent, trailing_nl)

        with patch.object(cc, "atomic_write_json", side_effect=flaky), _silence(), self.assertRaises(SystemExit):
            cc.cmd_use(cc.build_parser().parse_args(["use", "bob"]))

        # After failure, credentials must still show Alice's token (rollback succeeded).
        creds, _, _ = cc.read_json_file(self.creds_file)
        self.assertEqual(creds["claudeAiOauth"]["accessToken"], original_token)


class TestCmdList(BaseTest):
    def test_empty_shows_message(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.cmd_list(cc.build_parser().parse_args(["list"]))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("No profiles", out.getvalue())

    def test_lists_one_profile(self) -> None:
        cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(cc.build_parser().parse_args(["list"]))
        text = out.getvalue()
        self.assertIn("alice", text)
        self.assertIn("alice@example.com", text)
        self.assertIn("max", text)

    def test_marks_active_profile(self) -> None:
        cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        cc.save_profile("bob", SAMPLE_CREDS_B, SAMPLE_OAUTH_B)
        cc.write_active("alice")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(cc.build_parser().parse_args(["list"]))
        lines = out.getvalue().splitlines()
        alice_line = next(line for line in lines if "alice" in line)
        bob_line = next(line for line in lines if "bob" in line)
        self.assertTrue(alice_line.startswith("*"))
        self.assertFalse(bob_line.startswith("*"))

    def test_corrupted_profile_shown_safely(self) -> None:
        p = self.profiles_dir / "bad.json"
        p.write_text("not json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.cmd_list(cc.build_parser().parse_args(["list"]))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("corrupted", out.getvalue())

    def test_non_dict_json_profile_shown_safely(self) -> None:
        """A profile whose JSON is valid but not an object must not crash list."""
        p = self.profiles_dir / "arr.json"
        p.write_text("[1, 2, 3]")
        cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.cmd_list(cc.build_parser().parse_args(["list"]))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("corrupted", out.getvalue())
        self.assertIn("alice", out.getvalue())

    def test_profile_with_non_dict_oauth_field_shown_safely(self) -> None:
        p = self.profiles_dir / "weird.json"
        p.write_text(json.dumps({"oauthAccount": "str", "credentials": 42}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.cmd_list(cc.build_parser().parse_args(["list"]))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("weird", out.getvalue())


class TestCmdCurrent(BaseTest):
    def test_returns_active_from_file(self) -> None:
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        cc.write_active("work")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_current(cc.build_parser().parse_args(["current"]))
        self.assertEqual(out.getvalue().strip(), "work")

    def test_falls_back_to_email_match(self) -> None:
        self._write_creds()
        self._write_main(SAMPLE_OAUTH)
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        # no .active file
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_current(cc.build_parser().parse_args(["current"]))
        self.assertEqual(out.getvalue().strip(), "work")

    def test_stale_active_falls_back(self) -> None:
        """If .active names a removed profile, fall back to email scan."""
        self._write_creds()
        self._write_main(SAMPLE_OAUTH)
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        cc.write_active("removed")  # points at nonexistent profile
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_current(cc.build_parser().parse_args(["current"]))
        self.assertEqual(out.getvalue().strip(), "work")

    def test_no_match_says_undetermined(self) -> None:
        self._write_creds()
        self._write_main(SAMPLE_OAUTH)
        cc.save_profile("other", SAMPLE_CREDS_B, SAMPLE_OAUTH_B)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_current(cc.build_parser().parse_args(["current"]))
        self.assertIn("No active profile", out.getvalue())

    def test_no_creds_file(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.cmd_current(cc.build_parser().parse_args(["current"]))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("No active profile", out.getvalue())

    def test_skips_corrupted_profiles_in_email_scan(self) -> None:
        """cmd_current must not crash when one profile file is corrupted."""
        self._write_creds()
        self._write_main(SAMPLE_OAUTH)
        (self.profiles_dir / "broken.json").write_text("not json")
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.cmd_current(cc.build_parser().parse_args(["current"]))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(out.getvalue().strip(), "work")

    def test_skips_non_dict_json_profiles_in_email_scan(self) -> None:
        self._write_creds()
        self._write_main(SAMPLE_OAUTH)
        (self.profiles_dir / "array.json").write_text("[1, 2]")
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.cmd_current(cc.build_parser().parse_args(["current"]))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(out.getvalue().strip(), "work")


class TestCmdRemove(BaseTest):
    def test_removes_profile(self) -> None:
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        with patch("builtins.input", return_value="y"), _silence():
            cc.cmd_remove(cc.build_parser().parse_args(["remove", "work"]))
        self.assertFalse(self._profile_path("work").exists())

    def test_cancelled_keeps_profile(self) -> None:
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        with patch("builtins.input", return_value="n"), _silence():
            cc.cmd_remove(cc.build_parser().parse_args(["remove", "work"]))
        self.assertTrue(self._profile_path("work").exists())

    def test_clears_active_when_removing_active(self) -> None:
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        cc.write_active("work")
        with patch("builtins.input", return_value="y"), _silence():
            cc.cmd_remove(cc.build_parser().parse_args(["remove", "work"]))
        self.assertIsNone(cc.read_active())

    def test_does_not_clear_active_for_other_profile(self) -> None:
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        cc.save_profile("home", SAMPLE_CREDS_B, SAMPLE_OAUTH_B)
        cc.write_active("home")
        with patch("builtins.input", return_value="y"), _silence():
            cc.cmd_remove(cc.build_parser().parse_args(["remove", "work"]))
        self.assertEqual(cc.read_active(), "home")

    def test_not_found_dies(self) -> None:
        with _silence(), self.assertRaises(SystemExit) as cm:
            cc.cmd_remove(cc.build_parser().parse_args(["remove", "ghost"]))
        self.assertEqual(cm.exception.code, cc.EXIT_USER)

    def test_unlink_failure_dies(self) -> None:
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        with (
            patch.object(Path, "unlink", side_effect=OSError("busy")),
            patch("builtins.input", return_value="y"),
            _silence(),
            self.assertRaises(SystemExit) as cm,
        ):
            cc.cmd_remove(cc.build_parser().parse_args(["remove", "work"]))
        self.assertEqual(cm.exception.code, cc.EXIT_SYS)

    def test_active_unlink_failure_is_tolerated(self) -> None:
        """Failing to clear .active after removing the profile must not crash."""
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        cc.write_active("work")
        profile_file = self._profile_path("work")
        active_file = self.active_file
        real_unlink = Path.unlink

        def unlink_fail_on_active(p: Path, *a: object, **kw: object) -> None:
            if p == active_file:
                raise OSError("busy")
            real_unlink(p, *a, **kw)

        with (
            patch.object(Path, "unlink", autospec=True, side_effect=unlink_fail_on_active),
            patch("builtins.input", return_value="y"),
            _silence(),
        ):
            cc.cmd_remove(cc.build_parser().parse_args(["remove", "work"]))

        self.assertFalse(profile_file.exists())
        self.assertTrue(active_file.exists())


class TestMainDispatch(BaseTest):
    def test_no_args_runs_list_and_help(self) -> None:
        cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.main([])
        self.assertEqual(rc, cc.EXIT_OK)
        text = out.getvalue()
        self.assertIn("alice", text)
        self.assertIn("usage:", text)

    def test_main_dispatches_list(self) -> None:
        cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.main(["list"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("alice", out.getvalue())

    def test_main_dispatches_current(self) -> None:
        cc.save_profile("alice", SAMPLE_CREDS, SAMPLE_OAUTH)
        cc.write_active("alice")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.main(["current"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(out.getvalue().strip(), "alice")

    def test_main_dispatches_add(self) -> None:
        self._write_creds()
        self._write_main()
        with _silence():
            rc = cc.main(["add", "new"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertTrue(self._profile_path("new").exists())

    def test_main_dispatches_use(self) -> None:
        self._write_creds(SAMPLE_CREDS)
        self._write_main(SAMPLE_OAUTH)
        cc.save_profile("bob", SAMPLE_CREDS_B, SAMPLE_OAUTH_B)
        with _silence():
            rc = cc.main(["use", "bob"])
        self.assertEqual(rc, cc.EXIT_OK)
        creds, _, _ = cc.read_json_file(self.creds_file)
        self.assertEqual(creds["claudeAiOauth"]["accessToken"], "sk-ant-oat01-TOKENB")

    def test_main_dispatches_remove(self) -> None:
        cc.save_profile("gone", SAMPLE_CREDS, SAMPLE_OAUTH)
        with patch("builtins.input", return_value="y"), _silence():
            rc = cc.main(["remove", "gone"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertFalse(self._profile_path("gone").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

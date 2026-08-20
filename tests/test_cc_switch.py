#!/usr/bin/env python3
"""Tests for cc_switch.py."""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import io
import json
import math
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
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


# ===========================================================================
# Auto-switching: snapshots, ranking, gate, tick
# ===========================================================================


NOW = 1_800_000_000.0  # fixed clock, so window arithmetic is deterministic
HOUR = 3600.0
DAY = 86400.0


def _iso(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.UTC).isoformat().replace("+00:00", "Z")


def _creds(token: str, expires: int = 9999999999000, refresh_expires: int | None = None) -> dict:
    oauth: dict = {
        "accessToken": token,
        "refreshToken": f"refresh-{token}",
        "expiresAt": expires,
        "subscriptionType": "max",
    }
    if refresh_expires is not None:
        oauth["refreshTokenExpiresAt"] = refresh_expires
    return {"claudeAiOauth": oauth}


def _account(email: str) -> dict:
    return {"accountUuid": f"uuid-{email}", "emailAddress": email, "organizationUuid": "org"}


def _api_usage(five: float, seven: float, r5: str | None = None, r7: str | None = None) -> dict:
    return {
        "five_hour": {"utilization": five, "resets_at": r5},
        "seven_day": {"utilization": seven, "resets_at": r7},
    }


class AutoBaseTest(BaseTest):
    """BaseTest plus the auto-switch state files, which are module globals.

    Patching PROFILES_DIR alone is not enough: the state paths are derived
    from it once at import time.
    """

    def setUp(self) -> None:
        super().setUp()
        self.auto_file = self.profiles_dir / ".auto"
        self.gate_file = self.profiles_dir / ".gate"
        self.exhausted_file = self.profiles_dir / ".exhausted"
        self.settle_file = self.profiles_dir / ".settle"
        self.log_file = self.profiles_dir / ".auto.log"
        self.lock_file = self.profiles_dir / ".lock"
        self._auto_patches = [
            patch.object(cc, "AUTO_FILE", self.auto_file),
            patch.object(cc, "GATE_FILE", self.gate_file),
            patch.object(cc, "EXHAUSTED_FILE", self.exhausted_file),
            patch.object(cc, "SETTLE_FILE", self.settle_file),
            patch.object(cc, "LOG_FILE", self.log_file),
            patch.object(cc, "LOCK_FILE", self.lock_file),
            patch.object(cc, "_now", lambda: NOW),
            # Any un-mocked network call must fail loudly rather than go out.
            patch.object(cc.urllib.request, "urlopen", side_effect=AssertionError("network in tests")),
        ]
        for p in self._auto_patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._auto_patches:
            p.stop()
        super().tearDown()

    # -- convenience writers -------------------------------------------------

    def _save_with_usage(
        self,
        name: str,
        creds: dict,
        account: dict,
        five: float | None = None,
        seven: float | None = None,
        r5: str | None = None,
        r7: str | None = None,
    ) -> None:
        cc.save_profile(name, creds, account)
        if five is None and seven is None:
            return
        path = self._profile_path(name)
        data = json.loads(path.read_text())
        data["usage"] = {
            "five_hour": {"utilization": five or 0.0, "resets_at": r5},
            "seven_day": {"utilization": seven or 0.0, "resets_at": r7},
            "observedAt": _iso(NOW),
        }
        path.write_text(json.dumps(data, indent=2))

    def _enable_auto(self) -> None:
        self.auto_file.write_text("on\n")

    def _tick_args(
        self,
        five: float,
        seven: float,
        r5: str | None = None,
        r7: str | None = None,
    ) -> argparse.Namespace:
        return argparse.Namespace(five_hour=five, seven_day=seven, resets_5h=r5, resets_7d=r7)

    def _stored_usage(self, name: str) -> dict:
        return json.loads(self._profile_path(name).read_text())["usage"]

    def _log_text(self) -> str:
        return self.log_file.read_text() if self.log_file.exists() else ""


class TestEffectiveUsage(unittest.TestCase):
    def test_no_snapshot_is_zero(self) -> None:
        self.assertEqual(cc.effective_usage(None, NOW), (0.0, 0.0))

    def test_non_dict_snapshot_is_zero(self) -> None:
        self.assertEqual(cc.effective_usage("nonsense", NOW), (0.0, 0.0))

    def test_fresh_windows_are_kept(self) -> None:
        snap = cc.make_snapshot(30.0, 60.0, _iso(NOW + HOUR), _iso(NOW + DAY))
        self.assertEqual(cc.effective_usage(snap, NOW), (30.0, 60.0))

    def test_five_hour_zeroed_past_its_reset(self) -> None:
        snap = cc.make_snapshot(97.0, 60.0, _iso(NOW - 1), _iso(NOW + DAY))
        self.assertEqual(cc.effective_usage(snap, NOW), (0.0, 60.0))

    def test_seven_day_zeroed_past_its_reset(self) -> None:
        """The weekly window must zero on its own reset, not the 5h one.

        Zeroing only the 5h component would strand an account whose stored
        weekly figure is high but whose weekly window rolled over long ago.
        """
        snap = cc.make_snapshot(30.0, 96.0, _iso(NOW + HOUR), _iso(NOW - 1))
        self.assertEqual(cc.effective_usage(snap, NOW), (30.0, 0.0))

    def test_both_windows_zeroed(self) -> None:
        snap = cc.make_snapshot(97.0, 96.0, _iso(NOW - DAY), _iso(NOW - 1))
        self.assertEqual(cc.effective_usage(snap, NOW), (0.0, 0.0))

    def test_null_resets_keeps_the_value(self) -> None:
        snap = cc.make_snapshot(12.0, 34.0, None, None)
        self.assertEqual(cc.effective_usage(snap, NOW), (12.0, 34.0))

    def test_unparsable_reset_keeps_the_value(self) -> None:
        snap = cc.make_snapshot(12.0, 34.0, "not-a-date", "also-not")
        self.assertEqual(cc.effective_usage(snap, NOW), (12.0, 34.0))


class TestUsageFromApi(unittest.TestCase):
    def test_reads_both_windows(self) -> None:
        snap = cc.usage_from_api(_api_usage(23.0, 40.0, _iso(NOW), _iso(NOW + DAY)))
        self.assertEqual(snap["five_hour"]["utilization"], 23.0)
        self.assertEqual(snap["seven_day"]["resets_at"], _iso(NOW + DAY))

    def test_null_resets_survive(self) -> None:
        """A window with no usage at all comes back with resets_at null."""
        snap = cc.usage_from_api(_api_usage(0.0, 0.0))
        self.assertIsNone(snap["five_hour"]["resets_at"])
        self.assertEqual(snap["seven_day"]["utilization"], 0.0)

    def test_missing_blocks_default_to_zero(self) -> None:
        snap = cc.usage_from_api({})
        self.assertEqual(snap["five_hour"]["utilization"], 0.0)
        self.assertEqual(snap["seven_day"]["utilization"], 0.0)


class TestProfileUnusableReason(unittest.TestCase):
    def test_healthy_profile(self) -> None:
        data = {"credentials": _creds("t"), "oauthAccount": _account("a@example.com")}
        self.assertIsNone(cc.profile_unusable_reason(data, NOW))

    def test_corrupted(self) -> None:
        self.assertEqual(cc.profile_unusable_reason(None, NOW), "corrupted")

    def test_missing_credentials(self) -> None:
        data = {"oauthAccount": _account("a@example.com")}
        self.assertEqual(cc.profile_unusable_reason(data, NOW), "no credentials")

    def test_missing_refresh_token(self) -> None:
        data = {"credentials": {"claudeAiOauth": {"accessToken": "t"}}, "oauthAccount": _account("a@x")}
        self.assertEqual(cc.profile_unusable_reason(data, NOW), "no credentials")

    def test_missing_account(self) -> None:
        data = {"credentials": _creds("t")}
        self.assertEqual(cc.profile_unusable_reason(data, NOW), "no account")

    def test_refresh_token_expired(self) -> None:
        """A refresh does not extend this deadline, so past it the account is dead."""
        data = {
            "credentials": _creds("t", refresh_expires=int((NOW - 1) * 1000)),
            "oauthAccount": _account("a@x"),
        }
        self.assertEqual(cc.profile_unusable_reason(data, NOW), "refresh token expired")

    def test_refresh_token_still_valid(self) -> None:
        data = {
            "credentials": _creds("t", refresh_expires=int((NOW + DAY) * 1000)),
            "oauthAccount": _account("a@x"),
        }
        self.assertIsNone(cc.profile_unusable_reason(data, NOW))

    def test_auth_error_retires_the_profile(self) -> None:
        data = {
            "credentials": _creds("t"),
            "oauthAccount": _account("a@x"),
            "authError": {"reason": "refresh rejected (401)", "at": _iso(NOW)},
        }
        self.assertEqual(cc.profile_unusable_reason(data, NOW), "auth error (refresh rejected (401))")


class TestRankCandidates(AutoBaseTest):
    def test_lowest_weekly_first(self) -> None:
        self._save_with_usage("high", _creds("a"), _account("a@x"), 10.0, 60.0)
        self._save_with_usage("low", _creds("b"), _account("b@x"), 80.0, 20.0)
        ranked = cc.rank_candidates(NOW, None)
        self.assertEqual([c.name for c in ranked], ["low", "high"])

    def test_tie_broken_by_five_hour(self) -> None:
        self._save_with_usage("busy", _creds("a"), _account("a@x"), 70.0, 40.0)
        self._save_with_usage("idle", _creds("b"), _account("b@x"), 10.0, 40.0)
        ranked = cc.rank_candidates(NOW, None)
        self.assertEqual([c.name for c in ranked], ["idle", "busy"])

    def test_entry_bar_excludes_weekly_full_account(self) -> None:
        self._save_with_usage("full", _creds("a"), _account("a@x"), 10.0, cc.ENTER_7D + 1)
        self.assertEqual(cc.rank_candidates(NOW, None), [])

    def test_entry_bar_excludes_five_hour_full_account(self) -> None:
        self._save_with_usage("full", _creds("a"), _account("a@x"), cc.ENTER_5H + 1, 10.0)
        self.assertEqual(cc.rank_candidates(NOW, None), [])

    def test_entry_bar_is_inclusive(self) -> None:
        self._save_with_usage("edge", _creds("a"), _account("a@x"), cc.ENTER_5H, cc.ENTER_7D)
        self.assertEqual([c.name for c in cc.rank_candidates(NOW, None)], ["edge"])

    def test_active_profile_excluded(self) -> None:
        self._save_with_usage("me", _creds("a"), _account("a@x"), 1.0, 1.0)
        self._save_with_usage("other", _creds("b"), _account("b@x"), 2.0, 2.0)
        self.assertEqual([c.name for c in cc.rank_candidates(NOW, "me")], ["other"])

    def test_unusable_profile_excluded(self) -> None:
        self._save_with_usage("dead", _creds("a", refresh_expires=int((NOW - 1) * 1000)), _account("a@x"), 1.0, 1.0)
        self._save_with_usage("ok", _creds("b"), _account("b@x"), 2.0, 2.0)
        self.assertEqual([c.name for c in cc.rank_candidates(NOW, None)], ["ok"])

    def test_corrupted_profile_excluded(self) -> None:
        self._profile_path("broken").write_text("{not json")
        self._save_with_usage("ok", _creds("b"), _account("b@x"), 2.0, 2.0)
        self.assertEqual([c.name for c in cc.rank_candidates(NOW, None)], ["ok"])

    def test_missing_snapshot_is_treated_as_free(self) -> None:
        """An unknown account is worth trying — the live check settles it."""
        cc.save_profile("fresh", _creds("a"), _account("a@x"))
        ranked = cc.rank_candidates(NOW, None)
        self.assertEqual([(c.name, c.seven_day) for c in ranked], [("fresh", 0.0)])

    def test_rolled_over_window_returns_account_to_the_pool(self) -> None:
        self._save_with_usage("stale", _creds("a"), _account("a@x"), 99.0, 96.0, _iso(NOW - 1), _iso(NOW - 1))
        self.assertEqual([c.name for c in cc.rank_candidates(NOW, None)], ["stale"])

    def test_empty_when_nothing_usable(self) -> None:
        self.assertEqual(cc.rank_candidates(NOW, None), [])

    def test_excluding_the_first_profile_still_ranks_the_rest(self) -> None:
        """`aaa` sorts first: stopping at the exclusion would hide `zzz`."""
        self._save_with_usage("aaa", _creds("a"), _account("a@x"), 1.0, 1.0)
        self._save_with_usage("zzz", _creds("b"), _account("b@x"), 2.0, 2.0)
        self.assertEqual([c.name for c in cc.rank_candidates(NOW, "aaa")], ["zzz"])


class TestCandidate(unittest.TestCase):
    """The ranking result is a named tuple — the field order is the contract."""

    def test_fields_are_named_and_ordered(self) -> None:
        candidate = cc.Candidate("vlad", 12.0, 34.0)
        self.assertEqual(candidate.name, "vlad")
        self.assertEqual(candidate.five_hour, 12.0)
        self.assertEqual(candidate.seven_day, 34.0)
        self.assertEqual(tuple(candidate), ("vlad", 12.0, 34.0))

    def test_sorts_by_weekly_then_five_hour(self) -> None:
        low_week = cc.Candidate("a", 90.0, 10.0)
        high_week = cc.Candidate("b", 1.0, 50.0)
        self.assertLess(sorted([high_week, low_week], key=lambda c: (c.seven_day, c.five_hour))[0], high_week)


class TestSwitchReason(unittest.TestCase):
    def test_no_reason_below_thresholds(self) -> None:
        self.assertIsNone(cc.switch_reason(10.0, 40.0, 38.0))

    def test_five_hour_limit(self) -> None:
        self.assertEqual(cc.switch_reason(cc.EXIT_5H, 10.0, 5.0), "limit")

    def test_seven_day_limit(self) -> None:
        self.assertEqual(cc.switch_reason(10.0, cc.EXIT_7D, 5.0), "limit")

    def test_balance_gap_reached(self) -> None:
        self.assertEqual(cc.switch_reason(10.0, 40.0, 40.0 - cc.BALANCE_GAP_7D), "balance")

    def test_balance_gap_one_short(self) -> None:
        self.assertIsNone(cc.switch_reason(10.0, 40.0, 40.0 - cc.BALANCE_GAP_7D + 1))

    def test_limit_wins_over_balance(self) -> None:
        self.assertEqual(cc.switch_reason(cc.EXIT_5H, 40.0, 10.0), "limit")


class TestGate(AutoBaseTest):
    def _gate(self) -> list[int]:
        return [int(x) for x in self.gate_file.read_text().split()]

    def test_line_is_four_integers(self) -> None:
        """The statusline parses this from bash — the shape is the contract."""
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        cc.recompute_gate("me", NOW)
        self.assertEqual(len(self._gate()), 4)

    def test_trigger_uses_the_balance_gap(self) -> None:
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], int(20 + cc.BALANCE_GAP_7D))

    def test_trigger_capped_by_exit_threshold(self) -> None:
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, cc.ENTER_7D)
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], int(cc.EXIT_7D))

    def test_trigger_is_exit_threshold_without_candidates(self) -> None:
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[2:], [int(cc.EXIT_5H), int(cc.EXIT_7D)])

    def test_recheck_after_is_earliest_other_reset(self) -> None:
        self._save_with_usage("a", _creds("a"), _account("a@x"), 1.0, 1.0, _iso(NOW + 2 * HOUR), _iso(NOW + DAY))
        self._save_with_usage("b", _creds("b"), _account("b@x"), 1.0, 1.0, _iso(NOW + HOUR), _iso(NOW + DAY))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + HOUR))

    def test_recheck_ignores_past_resets(self) -> None:
        self._save_with_usage("a", _creds("a"), _account("a@x"), 1.0, 1.0, _iso(NOW - HOUR), _iso(NOW + DAY))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + DAY))

    def test_recheck_zero_when_no_reset_is_known(self) -> None:
        """resets_at is null for an untouched window, so 'never' must be expressible."""
        self._save_with_usage("a", _creds("a"), _account("a@x"), 0.0, 0.0, None, None)
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], 0)

    def test_recheck_skips_the_active_profile(self) -> None:
        self._save_with_usage("me", _creds("a"), _account("a@x"), 1.0, 1.0, _iso(NOW + HOUR), _iso(NOW + HOUR))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], 0)

    def test_not_before_is_max_of_settle_and_exhausted(self) -> None:
        cc.write_epoch_file(self.settle_file, NOW + 60)
        cc.write_epoch_file(self.exhausted_file, NOW + 600)
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[0], int(NOW + 600))


class TestAutoCommand(AutoBaseTest):
    def test_on_writes_the_flag_and_the_gate(self) -> None:
        with _silence():
            cc.cmd_auto(argparse.Namespace(action="on"))
        self.assertTrue(cc.auto_enabled())
        self.assertTrue(self.gate_file.exists())

    def test_on_clears_a_stale_exhausted_deadline(self) -> None:
        cc.write_epoch_file(self.exhausted_file, NOW + DAY)
        with _silence():
            cc.cmd_auto(argparse.Namespace(action="on"))
        self.assertFalse(self.exhausted_file.exists())

    def test_off_removes_flag_and_gate(self) -> None:
        self._enable_auto()
        self.gate_file.write_text("0 0 95 99\n")
        with _silence():
            cc.cmd_auto(argparse.Namespace(action="off"))
        self.assertFalse(cc.auto_enabled())
        self.assertFalse(self.gate_file.exists())

    def test_status_reports_disabled_by_default(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_auto(argparse.Namespace(action="status"))
        self.assertIn("disabled", out.getvalue())

    def test_status_reports_exhaustion(self) -> None:
        self._enable_auto()
        cc.write_epoch_file(self.exhausted_file, NOW + HOUR)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_auto(argparse.Namespace(action="status"))
        self.assertIn("exhausted", out.getvalue())


class TickTestCase(AutoBaseTest):
    """Two accounts, `me` active, auto on — the shape every tick test needs."""

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")
        self._enable_auto()

    def _patch_usage(self, five: float, seven: float, r5: str | None = None, r7: str | None = None) -> object:
        return patch.object(cc, "fetch_usage", return_value=(200, _api_usage(five, seven, r5, r7)))


class TestTick(TickTestCase):
    def test_auto_off_does_not_switch(self) -> None:
        self.auto_file.unlink()
        with self._patch_usage(1.0, 1.0):
            cc.cmd_tick(self._tick_args(99.0, 99.0))
        self.assertEqual(cc.read_active(), "me")

    def test_settle_window_blocks_the_decision(self) -> None:
        """Right after a switch the statusline still reports the old account."""
        cc.write_epoch_file(self.settle_file, NOW + 30)
        with self._patch_usage(1.0, 1.0):
            cc.cmd_tick(self._tick_args(99.0, 99.0))
        self.assertEqual(cc.read_active(), "me")

    def test_exhausted_window_blocks_the_decision(self) -> None:
        cc.write_epoch_file(self.exhausted_file, NOW + HOUR)
        with self._patch_usage(1.0, 1.0):
            cc.cmd_tick(self._tick_args(99.0, 99.0))
        self.assertEqual(cc.read_active(), "me")

    def test_records_the_active_snapshot(self) -> None:
        with self._patch_usage(1.0, 1.0):
            cc.cmd_tick(self._tick_args(11.0, 22.0, _iso(NOW + HOUR), _iso(NOW + DAY)))
        stored = self._stored_usage("me")
        self.assertEqual(stored["five_hour"]["utilization"], 11.0)
        self.assertEqual(stored["seven_day"]["resets_at"], _iso(NOW + DAY))

    def test_no_reason_leaves_everything_alone(self) -> None:
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 5.0, 38.0)
        with self._patch_usage(5.0, 38.0):
            cc.cmd_tick(self._tick_args(10.0, 40.0))
        self.assertEqual(cc.read_active(), "me")
        self.assertFalse(self.exhausted_file.exists())
        self.assertTrue(self.gate_file.exists())

    def test_switch_on_five_hour_limit(self) -> None:
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "vlad")
        creds, _, _ = cc.read_json_file(self.creds_file)
        self.assertEqual(creds["claudeAiOauth"]["accessToken"], "token-vlad")

    def test_switch_on_weekly_limit(self) -> None:
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(10.0, cc.EXIT_7D))
        self.assertEqual(cc.read_active(), "vlad")

    def test_switch_on_balance(self) -> None:
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(10.0, 40.0))
        self.assertEqual(cc.read_active(), "vlad")
        self.assertIn("(balance)", self._log_text())

    def test_switch_arms_settle_and_clears_exhausted(self) -> None:
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.SETTLE_SECONDS))
        self.assertFalse(self.exhausted_file.exists())
        self.assertTrue(self.gate_file.exists())

    def test_switch_is_logged(self) -> None:
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertIn("switch me -> vlad (limit)", self._log_text())

    def test_switch_preserves_the_departing_snapshot(self) -> None:
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 44.0))
        self.assertEqual(self._stored_usage("me")["seven_day"]["utilization"], 44.0)

    def test_balance_failure_does_not_write_a_deadline(self) -> None:
        """A weekly deadline from a balance miss would silence the limit path for days.

        The resets below are what makes this bite: without a known future
        reset there would be no deadline to write either way.
        """
        with self._patch_usage(5.0, 39.0, _iso(NOW + HOUR), _iso(NOW + DAY)):
            cc.cmd_tick(self._tick_args(10.0, 40.0, _iso(NOW + HOUR), _iso(NOW + DAY)))
        self.assertEqual(cc.read_active(), "me")
        self.assertGreater(cc.earliest_future_reset(["me", "vlad"], NOW), 0)
        self.assertFalse(self.exhausted_file.exists())

    def test_healthy_pair_writes_no_deadline(self) -> None:
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 5.0, 38.0, _iso(NOW + HOUR))
        with self._patch_usage(5.0, 38.0, _iso(NOW + HOUR), _iso(NOW + DAY)):
            cc.cmd_tick(self._tick_args(10.0, 40.0, _iso(NOW + HOUR), _iso(NOW + DAY)))
        self.assertFalse(self.exhausted_file.exists())

    def test_exhausted_written_when_limit_finds_no_one(self) -> None:
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 10.0, cc.ENTER_7D + 5)
        cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0, _iso(NOW + HOUR), _iso(NOW + DAY)))
        self.assertEqual(cc.read_active(), "me")
        self.assertEqual(cc.read_epoch_file(self.exhausted_file), int(NOW + HOUR))
        self.assertIn("exhausted", self._log_text())

    def test_no_deadline_written_when_no_reset_is_known(self) -> None:
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 10.0, cc.ENTER_7D + 5)
        cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertFalse(self.exhausted_file.exists())

    def test_lock_held_elsewhere_skips_the_tick(self) -> None:
        fd = os.open(str(self.lock_file), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self._patch_usage(5.0, 20.0):
                cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        finally:
            os.close(fd)
        self.assertEqual(cc.read_active(), "me")

    def test_stale_marker_is_resolved_from_the_live_email(self) -> None:
        """A marker naming a deleted profile must not strand auto-switching."""
        cc.write_active("ghost")
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_unknown_live_account_is_a_noop(self) -> None:
        cc.write_active("ghost")
        self._write_main(_account("stranger@example.com"))
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "ghost")


class TestTickConfirmation(TickTestCase):
    def test_live_check_can_veto_a_stale_snapshot(self) -> None:
        with self._patch_usage(10.0, cc.ENTER_7D + 5):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0, _iso(NOW + HOUR)))
        self.assertEqual(cc.read_active(), "me")
        self.assertIn("skip vlad: live", self._log_text())

    def test_veto_updates_the_snapshot(self) -> None:
        """The refuted guess is replaced by fact, which is why this converges."""
        with self._patch_usage(10.0, cc.ENTER_7D + 5):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(self._stored_usage("vlad")["seven_day"]["utilization"], cc.ENTER_7D + 5)

    def test_invalid_grant_marks_the_profile_and_skips_it(self) -> None:
        self._save_with_usage("vlad", _creds("token-vlad", expires=1000), _account("v@x"), 5.0, 20.0)
        with patch.object(cc, "oauth_refresh", return_value=(400, {"error": "invalid_grant"})):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "me")
        stored = json.loads(self._profile_path("vlad").read_text())
        self.assertIn("authError", stored)
        self.assertIn("unauthorized", self._log_text())

    def test_marked_profile_stays_out_of_the_pool(self) -> None:
        self._save_with_usage("vlad", _creds("token-vlad", expires=1000), _account("v@x"), 5.0, 20.0)
        with patch.object(cc, "oauth_refresh", return_value=(401, {"error": "nope"})):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.rank_candidates(NOW, "me"), [])

    def test_throttled_refresh_does_not_retire_the_profile(self) -> None:
        """429 is transient — retiring an account over it would be a real loss."""
        self._save_with_usage("vlad", _creds("token-vlad", expires=1000), _account("v@x"), 5.0, 20.0)
        with patch.object(cc, "oauth_refresh", return_value=(429, {"error": "rate limited"})) as refresh:
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(cc.read_active(), "me")
        self.assertNotIn("authError", json.loads(self._profile_path("vlad").read_text()))

    def test_unauthorized_usage_call_retires_the_profile(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(401, {"error": "expired"})):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertIn("authError", json.loads(self._profile_path("vlad").read_text()))

    def test_convergence_each_candidate_is_confirmed_once(self) -> None:
        """Every snapshot promises headroom; every live check refutes it.

        This is the case that actually exercises the candidate loop — with
        snapshots already over the bar, ranking returns nothing and the loop
        never runs.
        """
        for name in ("a", "b", "c"):
            self._save_with_usage(name, _creds(f"token-{name}"), _account(f"{name}@x"), 5.0, 20.0)
        self._profile_path("vlad").unlink()
        seen: list[str] = []

        def _fake_usage(token: str) -> tuple[int, dict]:
            seen.append(token)
            return 200, _api_usage(10.0, cc.ENTER_7D + 5)

        with patch.object(cc, "fetch_usage", side_effect=_fake_usage):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0, _iso(NOW + HOUR)))

        self.assertEqual(cc.read_active(), "me")
        self.assertEqual(sorted(seen), ["token-a", "token-b", "token-c"])
        self.assertEqual(cc.read_epoch_file(self.exhausted_file), int(NOW + HOUR))


class TestRefreshStorage(AutoBaseTest):
    def setUp(self) -> None:
        super().setUp()
        self._save_with_usage("idle", _creds("old-token", expires=1000), _account("i@x"))
        self.payload = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 28800,
            "refresh_token_expires_in": 596169,
        }

    def test_tokens_are_stored_before_they_are_used(self) -> None:
        """The old refresh token dies on the server's answer — persist first."""
        order: list[str] = []

        def _refresh(_token: str) -> tuple[int, dict]:
            order.append("refresh")
            return 200, self.payload

        def _usage(_token: str) -> tuple[int, dict]:
            order.append("usage")
            stored = json.loads(self._profile_path("idle").read_text())
            order.append(stored["credentials"]["claudeAiOauth"]["refreshToken"])
            return 200, _api_usage(1.0, 2.0)

        with (
            patch.object(cc, "oauth_refresh", side_effect=_refresh),
            patch.object(cc, "fetch_usage", side_effect=_usage),
        ):
            cc.confirm_candidate("idle", None)
        self.assertEqual(order, ["refresh", "usage", "new-refresh"])

    def test_expiry_fields_are_updated(self) -> None:
        with patch.object(cc, "oauth_refresh", return_value=(200, self.payload)):
            token, error, transient = cc.access_token_for("idle", None)
        self.assertEqual((token, error, transient), ("new-access", None, False))
        oauth = json.loads(self._profile_path("idle").read_text())["credentials"]["claudeAiOauth"]
        self.assertGreater(oauth["expiresAt"] / 1000, time.time())
        self.assertGreater(oauth["refreshTokenExpiresAt"] / 1000, time.time())

    def test_write_failure_leaves_a_recovery_file(self) -> None:
        with (
            patch.object(cc, "oauth_refresh", return_value=(200, self.payload)),
            patch.object(cc, "atomic_write_json", side_effect=SystemExit(2)),
            self.assertRaises(SystemExit),
            _silence(),
        ):
            cc.access_token_for("idle", None)
        recovery = self.profiles_dir / ".recovery-idle.json"
        self.assertTrue(recovery.exists())
        self.assertEqual(stat.S_IMODE(os.stat(recovery).st_mode), 0o600)
        self.assertEqual(json.loads(recovery.read_text())["refresh_token"], "new-refresh")

    def test_partial_payload_keeps_the_existing_fields(self) -> None:
        """A response carrying only an access token must not blank the rest."""
        before = json.loads(self._profile_path("idle").read_text())["credentials"]["claudeAiOauth"]
        with patch.object(cc, "oauth_refresh", return_value=(200, {"access_token": "only-access"})):
            token, error, _ = cc.access_token_for("idle", None)
        self.assertEqual((token, error), ("only-access", None))
        after = json.loads(self._profile_path("idle").read_text())["credentials"]["claudeAiOauth"]
        self.assertEqual(after["refreshToken"], before["refreshToken"])
        self.assertEqual(after["expiresAt"], before["expiresAt"])

    def test_active_profile_is_never_refreshed(self) -> None:
        """Rotating under the running Claude Code process would break it."""
        self._write_creds(_creds("live-token"))
        self._write_main(_account("i@x"))
        with patch.object(cc, "oauth_refresh", side_effect=AssertionError("must not refresh")) as refresh:
            token, error, _ = cc.access_token_for("idle", "idle")
        self.assertEqual((token, error), ("live-token", None))
        refresh.assert_not_called()

    def test_valid_access_token_skips_the_refresh(self) -> None:
        self._save_with_usage("fresh", _creds("still-good"), _account("f@x"))
        with patch.object(cc, "oauth_refresh", side_effect=AssertionError("must not refresh")):
            token, error, _ = cc.access_token_for("fresh", None)
        self.assertEqual((token, error), ("still-good", None))

    def test_missing_live_credentials_reported(self) -> None:
        token, error, _ = cc.access_token_for("idle", "idle")
        self.assertIsNone(token)
        self.assertEqual(error, "no live credentials")


class TestUsageCommand(AutoBaseTest):
    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"))
        cc.write_active("me")

    def test_refreshes_every_snapshot(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(23.0, 40.0, _iso(NOW + HOUR)))), _silence():
            rc = cc.cmd_usage(argparse.Namespace(json=False))
        self.assertEqual(rc, cc.EXIT_OK)
        for name in ("me", "vlad"):
            self.assertEqual(self._stored_usage(name)["five_hour"]["utilization"], 23.0)

    def test_one_failure_does_not_hide_the_others(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            if token == "token-vlad":
                return 0, {"error": {"message": "offline"}}
            return 200, _api_usage(23.0, 40.0)

        out = io.StringIO()
        with patch.object(cc, "fetch_usage", side_effect=_usage), contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=False))
        text = out.getvalue()
        self.assertIn("23%", text)
        self.assertIn("failed", text)

    def test_unusable_profile_is_reported_without_a_request(self) -> None:
        self._save_with_usage("dead", _creds("t", refresh_expires=int((NOW - 1) * 1000)), _account("d@x"), 1.0, 2.0)
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(0.0, 0.0))), contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=False))
        self.assertIn("refresh token expired", out.getvalue())

    def test_json_output_is_machine_readable(self) -> None:
        out = io.StringIO()
        with (
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(23.0, 40.0))),
            contextlib.redirect_stdout(out),
        ):
            cc.cmd_usage(argparse.Namespace(json=True))
        rows = json.loads(out.getvalue())
        self.assertEqual({r["profile"] for r in rows}, {"me", "vlad"})
        self.assertEqual(rows[0]["seven_day"], 40.0)

    def test_no_profiles_message(self) -> None:
        for name in ("me", "vlad"):
            self._profile_path(name).unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=False))
        self.assertIn("No profiles yet", out.getvalue())


class TestPickCommand(AutoBaseTest):
    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 60.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")

    def test_moves_to_the_lowest_weekly_account(self) -> None:
        def _usage(token: str) -> tuple[int, dict]:
            return (200, _api_usage(10.0, 60.0)) if token == "token-me" else (200, _api_usage(5.0, 20.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "vlad")

    def test_stays_when_nothing_is_lower(self) -> None:
        def _usage(token: str) -> tuple[int, dict]:
            return (200, _api_usage(10.0, 10.0)) if token == "token-me" else (200, _api_usage(5.0, 20.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "me")

    def test_skips_an_account_over_the_entry_bar(self) -> None:
        def _usage(token: str) -> tuple[int, dict]:
            if token == "token-me":
                return 200, _api_usage(10.0, 60.0)
            return 200, _api_usage(10.0, cc.ENTER_7D + 5)

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "me")

    def test_runs_without_an_active_account(self) -> None:
        """First run after a fresh login: no marker, no live credentials yet."""
        self.active_file.unlink()
        self.creds_file.unlink()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertIn(cc.read_active(), ("me", "vlad"))

    def test_unreachable_active_account_does_not_block_the_move(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            return (0, {"error": "offline"}) if token == "token-me" else (200, _api_usage(5.0, 20.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "vlad")

    def test_refuses_to_run_concurrently(self) -> None:
        fd = os.open(str(self.lock_file), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(SystemExit) as ctx, _silence():
                cc.cmd_pick(argparse.Namespace())
        finally:
            os.close(fd)
        self.assertEqual(ctx.exception.code, cc.EXIT_USER)


class TestListNotes(AutoBaseTest):
    def test_flags_an_unusable_profile(self) -> None:
        self._save_with_usage("dead", _creds("t", refresh_expires=int((NOW - 1) * 1000)), _account("d@x"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        self.assertIn("! refresh token expired", out.getvalue())

    def test_warns_about_an_expiring_login(self) -> None:
        self._save_with_usage("soon", _creds("t", refresh_expires=int((NOW + HOUR) * 1000)), _account("s@x"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        self.assertIn("login expires in", out.getvalue())

    def test_healthy_profile_has_no_note(self) -> None:
        self._save_with_usage("ok", _creds("t", refresh_expires=int((NOW + 10 * DAY) * 1000)), _account("o@x"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        self.assertNotIn("!", out.getvalue())


class TestStateFiles(AutoBaseTest):
    def test_epoch_round_trip(self) -> None:
        cc.write_epoch_file(self.settle_file, NOW + 5)
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + 5))

    def test_missing_epoch_file_is_zero(self) -> None:
        self.assertEqual(cc.read_epoch_file(self.settle_file), 0.0)

    def test_garbage_epoch_file_is_zero(self) -> None:
        self.settle_file.write_text("soon\n")
        self.assertEqual(cc.read_epoch_file(self.settle_file), 0.0)

    def test_epoch_file_permissions(self) -> None:
        cc.write_epoch_file(self.settle_file, NOW)
        self.assertEqual(stat.S_IMODE(os.stat(self.settle_file).st_mode), 0o600)

    def test_auto_flag_requires_the_exact_word(self) -> None:
        self.auto_file.write_text("yes\n")
        self.assertFalse(cc.auto_enabled())

    def test_clear_state_file_removes_an_existing_deadline(self) -> None:
        cc.write_epoch_file(self.settle_file, NOW)
        cc.clear_state_file(self.settle_file)
        self.assertFalse(self.settle_file.exists())

    def test_clear_state_file_is_forgiving_when_already_absent(self) -> None:
        self.assertFalse(self.settle_file.exists())
        cc.clear_state_file(self.settle_file)
        self.assertFalse(self.settle_file.exists())

    def test_log_is_appended_with_0600(self) -> None:
        cc.log_decision("first")
        cc.log_decision("second")
        self.assertEqual(self._log_text().count("\n"), 2)
        self.assertEqual(stat.S_IMODE(os.stat(self.log_file).st_mode), 0o600)


class TestManualSwitchInteraction(TickTestCase):
    """A deliberate `cc-switch use` must survive the next statusline render."""

    def test_manual_use_arms_the_settle_window(self) -> None:
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.SETTLE_SECONDS))

    def test_manual_use_is_not_undone_by_the_next_tick(self) -> None:
        """The statusline still reports the old account for a moment after a swap."""
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(10.0, 40.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_manual_use_refreshes_the_gate(self) -> None:
        self.gate_file.write_text("0 0 95 47\n")
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        not_before = int(self.gate_file.read_text().split()[0])
        self.assertEqual(not_before, int(NOW + cc.SETTLE_SECONDS))


class TestResolveActive(AutoBaseTest):
    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))

    def test_marker_wins_when_present(self) -> None:
        cc.write_active("me")
        self.assertEqual(cc.resolve_active(), "me")

    def test_falls_back_to_the_live_email(self) -> None:
        """Without this, usage would refresh the LIVE account and break the session."""
        self.assertIsNone(cc.read_active())
        self.assertEqual(cc.resolve_active(), "me")

    def test_stale_marker_falls_back_to_the_email(self) -> None:
        cc.write_active("ghost")
        self.assertEqual(cc.resolve_active(), "me")

    def test_none_when_nothing_matches(self) -> None:
        self._write_main(_account("stranger@example.com"))
        self.assertIsNone(cc.resolve_active())

    def test_usage_never_refreshes_the_live_account(self) -> None:
        """With no marker, only the email fallback keeps this off the live token.

        The stored access token is deliberately expired, so anything that
        fails to recognise this profile as active would rotate the very
        credentials the running session is using.
        """
        self._save_with_usage("me", _creds("token-me", expires=1000), _account("me@example.com"))
        self.assertIsNone(cc.read_active())
        with (
            patch.object(cc, "oauth_refresh", side_effect=AssertionError("must not refresh")),
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))) as usage,
            _silence(),
        ):
            cc.cmd_usage(argparse.Namespace(json=False))
        # The live credentials file, not the profile's expired copy.
        usage.assert_called_once_with("token-me")

    def test_tick_without_a_marker_still_decides(self) -> None:
        self._enable_auto()
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "vlad")


class _FakeResponse:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class TestHttpLayer(AutoBaseTest):
    """The transport itself — everywhere else it is mocked away."""

    def test_successful_response_is_parsed(self) -> None:
        with patch.object(cc.urllib.request, "urlopen", return_value=_FakeResponse(200, '{"ok": true}')):
            status, payload = cc.fetch_usage("token")
        self.assertEqual((status, payload), (200, {"ok": True}))

    def test_usage_request_carries_the_bearer_token(self) -> None:
        captured: list[cc.urllib.request.Request] = []

        def _urlopen(req: cc.urllib.request.Request, timeout: float = 0) -> _FakeResponse:
            captured.append(req)
            return _FakeResponse(200, "{}")

        with patch.object(cc.urllib.request, "urlopen", side_effect=_urlopen):
            cc.fetch_usage("secret-token")
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(captured[0].full_url, cc.USAGE_URL)

    def test_refresh_posts_the_documented_body(self) -> None:
        captured: list[cc.urllib.request.Request] = []

        def _urlopen(req: cc.urllib.request.Request, timeout: float = 0) -> _FakeResponse:
            captured.append(req)
            return _FakeResponse(200, '{"access_token": "a"}')

        with patch.object(cc.urllib.request, "urlopen", side_effect=_urlopen):
            status, payload = cc.oauth_refresh("my-refresh")
        body = json.loads(captured[0].data.decode())
        self.assertEqual(body["grant_type"], "refresh_token")
        self.assertEqual(body["refresh_token"], "my-refresh")
        self.assertEqual(body["client_id"], cc.OAUTH_CLIENT_ID)
        self.assertEqual(captured[0].get_method(), "POST")
        self.assertEqual((status, payload), (200, {"access_token": "a"}))

    def test_http_error_body_is_returned(self) -> None:
        err = cc.urllib.error.HTTPError(cc.TOKEN_URL, 400, "Bad", {}, io.BytesIO(b'{"error": "invalid_grant"}'))
        with patch.object(cc.urllib.request, "urlopen", side_effect=err):
            status, payload = cc.oauth_refresh("dead")
        self.assertEqual((status, payload), (400, {"error": "invalid_grant"}))

    def test_unparsable_http_error_body_yields_none(self) -> None:
        err = cc.urllib.error.HTTPError(cc.TOKEN_URL, 429, "Too Many", {}, io.BytesIO(b"<html>nope"))
        with patch.object(cc.urllib.request, "urlopen", side_effect=err):
            status, payload = cc.oauth_refresh("throttled")
        self.assertEqual((status, payload), (429, None))

    def test_network_failure_is_status_zero(self) -> None:
        """Status 0 keeps a transport failure distinct from a real rejection."""
        with patch.object(cc.urllib.request, "urlopen", side_effect=cc.urllib.error.URLError("offline")):
            status, payload = cc.fetch_usage("token")
        self.assertEqual(status, 0)
        self.assertIn("offline", json.dumps(payload))

    def test_timeout_is_status_zero(self) -> None:
        with patch.object(cc.urllib.request, "urlopen", side_effect=TimeoutError("slow")):
            status, _ = cc.fetch_usage("token")
        self.assertEqual(status, 0)

    def test_an_unexpected_url_is_refused(self) -> None:
        """urllib honours file://, so the endpoint allow-list is enforced."""
        req = cc.urllib.request.Request("file:///etc/passwd")
        with patch.object(cc.urllib.request, "urlopen", side_effect=AssertionError("must not open")):
            with self.assertRaises(SystemExit) as ctx, _silence():
                cc._http_json(req)
        self.assertEqual(ctx.exception.code, cc.EXIT_SYS)

    def test_both_known_endpoints_are_allowed(self) -> None:
        for url in (cc.USAGE_URL, cc.TOKEN_URL):
            with patch.object(cc.urllib.request, "urlopen", return_value=_FakeResponse(200, "{}")):
                status, _ = cc._http_json(cc.urllib.request.Request(url))
            self.assertEqual(status, 200)

    def test_malformed_success_body_is_status_zero(self) -> None:
        with patch.object(cc.urllib.request, "urlopen", return_value=_FakeResponse(200, "not json")):
            status, _ = cc.fetch_usage("token")
        self.assertEqual(status, 0)


class TestEnvThresholds(unittest.TestCase):
    def test_default_is_used_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cc._env_float("CC_SWITCH_TEST_VALUE", 7.5), 7.5)

    def test_environment_overrides_the_default(self) -> None:
        with patch.dict(os.environ, {"CC_SWITCH_TEST_VALUE": "12"}):
            self.assertEqual(cc._env_float("CC_SWITCH_TEST_VALUE", 7.5), 12.0)

    def test_garbage_value_exits_with_a_user_error(self) -> None:
        with patch.dict(os.environ, {"CC_SWITCH_TEST_VALUE": "high"}), self.assertRaises(SystemExit) as ctx, _silence():
            cc._env_float("CC_SWITCH_TEST_VALUE", 7.5)
        self.assertEqual(ctx.exception.code, cc.EXIT_USER)


class TestFailureHandling(AutoBaseTest):
    """Degraded paths: unreadable files, unwritable state, dead profiles."""

    def test_non_fatal_write_failure_returns_false(self) -> None:
        target = self.profiles_dir / "nested" / "deep.txt"  # parent missing
        self.assertFalse(cc.write_text_file(target, "x", fatal=False))

    def test_fatal_write_failure_exits(self) -> None:
        target = self.profiles_dir / "nested" / "deep.txt"
        with self.assertRaises(SystemExit), _silence():
            cc.write_text_file(target, "x")

    def test_gate_write_failure_is_survivable(self) -> None:
        """A gate that cannot be written must not take the tick down with it."""
        unwritable = self.profiles_dir / "missing" / ".gate"
        with patch.object(cc, "GATE_FILE", unwritable):
            cc.recompute_gate(None, NOW)
        self.assertFalse(unwritable.exists())
        self.assertFalse(self.gate_file.exists())

    def test_lock_open_failure_yields_unlocked(self) -> None:
        with patch.object(cc, "LOCK_FILE", self.profiles_dir / "missing" / ".lock"), cc.profile_lock() as ok:
            self.assertFalse(ok)

    def test_log_write_failure_is_swallowed(self) -> None:
        """Losing the audit log must never abort a switch that is otherwise fine."""
        unwritable = self.profiles_dir / "missing" / ".log"
        with patch.object(cc, "LOG_FILE", unwritable):
            cc.log_decision("nowhere")
        self.assertFalse(unwritable.exists())
        self.assertEqual(self._log_text(), "")

    def test_snapshot_recording_skips_a_corrupted_profile(self) -> None:
        self._profile_path("broken").write_text("{not json")
        cc.record_usage_snapshot("broken", cc.make_snapshot(1.0, 2.0, None, None))
        self.assertEqual(self._profile_path("broken").read_text(), "{not json")

    def test_auth_marking_skips_a_corrupted_profile(self) -> None:
        self._profile_path("broken").write_text("{not json")
        cc.mark_auth_error("broken", "nope")
        self.assertEqual(self._profile_path("broken").read_text(), "{not json")

    def test_window_value_ignores_a_non_dict(self) -> None:
        self.assertEqual(cc._window_value("nonsense", NOW), 0.0)

    def test_access_token_for_corrupted_profile(self) -> None:
        self._profile_path("broken").write_text("{not json")
        token, error, _ = cc.access_token_for("broken", None)
        self.assertEqual((token, error), (None, "corrupted"))

    def test_resolve_active_without_credentials(self) -> None:
        self.assertIsNone(cc.resolve_active())

    def test_resolve_active_without_an_email(self) -> None:
        self._write_creds(_creds("t"))
        self._write_main({"accountUuid": "no-email"})
        self.assertIsNone(cc.resolve_active())

    def test_emergency_dump_falls_back_to_stderr(self) -> None:
        err = io.StringIO()
        with (
            patch.object(cc, "PROFILES_DIR", self.profiles_dir / "missing"),
            contextlib.redirect_stderr(err),
        ):
            cc._emergency_token_dump("idle", {"refresh_token": "would-be-lost"})
        self.assertIn("would-be-lost", err.getvalue())

    def test_unreadable_profile_during_token_storage_still_dumps(self) -> None:
        """The rotated token must survive even when the profile vanishes."""
        err = io.StringIO()
        with (
            patch.object(cc, "load_profile_data", return_value=None),
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cc._store_refreshed_tokens("gone", {"access_token": "a", "refresh_token": "r"})
        self.assertTrue((self.profiles_dir / ".recovery-gone.json").exists())

    def test_pick_reports_an_unreachable_candidate(self) -> None:
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 60.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 5.0, 20.0)
        cc.write_active("me")

        def _usage(token: str) -> tuple[int, object]:
            return (200, _api_usage(10.0, 60.0)) if token == "token-me" else (0, {"error": "offline"})

        out = io.StringIO()
        with patch.object(cc, "fetch_usage", side_effect=_usage), contextlib.redirect_stdout(out):
            cc.cmd_pick(argparse.Namespace())
        self.assertIn("Skipping 'vlad'", out.getvalue())
        self.assertEqual(cc.read_active(), "me")

    def test_pick_with_no_usable_account(self) -> None:
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 60.0)
        cc.write_active("me")
        out = io.StringIO()
        with (
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(10.0, 60.0))),
            contextlib.redirect_stdout(out),
        ):
            cc.cmd_pick(argparse.Namespace())
        self.assertIn("No usable account", out.getvalue())


class TestCandidateLoopSkipsRatherThanStops(TickTestCase):
    """A rejected candidate must be skipped, not end the search.

    `continue` turning into `break` is invisible to any test whose first
    candidate is the one that wins — these all reject the first and expect
    the second to be reached.
    """

    def setUp(self) -> None:
        super().setUp()
        self._profile_path("vlad").unlink()
        # "aaa" sorts and ranks first; "zzz" is the one that must be reached.
        self._save_with_usage("aaa", _creds("token-aaa"), _account("a@x"), 5.0, 10.0)
        self._save_with_usage("zzz", _creds("token-zzz"), _account("z@x"), 5.0, 20.0)

    def test_unreachable_candidate_does_not_end_the_search(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            return (0, {"error": "offline"}) if token == "token-aaa" else (200, _api_usage(5.0, 20.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "zzz")

    def test_over_bar_candidate_does_not_end_the_search(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            if token == "token-aaa":
                return 200, _api_usage(10.0, cc.ENTER_7D + 5)
            return 200, _api_usage(5.0, 20.0)

        with patch.object(cc, "fetch_usage", side_effect=_usage):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "zzz")

    def test_reason_evaporating_does_not_end_the_search(self) -> None:
        """A candidate too close to balance on must not hide a better one."""
        self._save_with_usage("aaa", _creds("token-aaa"), _account("a@x"), 5.0, 10.0)

        def _usage(token: str) -> tuple[int, object]:
            # 39 leaves no balance reason against an active 40; 20 does.
            return (200, _api_usage(5.0, 39.0)) if token == "token-aaa" else (200, _api_usage(5.0, 20.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage):
            cc.cmd_tick(self._tick_args(10.0, 40.0))
        self.assertEqual(cc.read_active(), "zzz")

    def test_unusable_profile_does_not_end_the_ranking(self) -> None:
        self._save_with_usage(
            "aaa", _creds("token-aaa", refresh_expires=int((NOW - 1) * 1000)), _account("a@x"), 1.0, 1.0
        )
        self.assertEqual([c.name for c in cc.rank_candidates(NOW, "me")], ["zzz"])

    def test_pick_skips_an_unreachable_candidate_and_continues(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            if token == "token-me":
                return 200, _api_usage(10.0, 60.0)
            return (0, {"error": "offline"}) if token == "token-aaa" else (200, _api_usage(5.0, 20.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "zzz")

    def test_pick_skips_an_over_bar_candidate_and_continues(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            if token == "token-me":
                return 200, _api_usage(10.0, 60.0)
            if token == "token-aaa":
                return 200, _api_usage(10.0, cc.ENTER_7D + 5)
            return 200, _api_usage(5.0, 20.0)

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "zzz")

    def test_reset_scan_covers_every_profile(self) -> None:
        """Stopping at the first profile without usage would miss the earliest reset."""
        cc.save_profile("aaa", _creds("token-aaa"), _account("a@x"))  # no usage block
        self._save_with_usage("zzz", _creds("token-zzz"), _account("z@x"), 1.0, 1.0, _iso(NOW + HOUR))
        self.assertEqual(cc.earliest_future_reset(["aaa", "zzz"], NOW), NOW + HOUR)


class TestBoundaries(TickTestCase):
    """Exact-equality cases on every threshold comparison."""

    def test_live_five_hour_exactly_at_the_bar_is_accepted(self) -> None:
        with self._patch_usage(cc.ENTER_5H, 20.0):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_live_weekly_exactly_at_the_bar_is_accepted(self) -> None:
        with self._patch_usage(5.0, cc.ENTER_7D):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_settle_expiring_exactly_now_no_longer_blocks(self) -> None:
        cc.write_epoch_file(self.settle_file, NOW)
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_exhausted_expiring_exactly_now_no_longer_blocks(self) -> None:
        cc.write_epoch_file(self.exhausted_file, NOW)
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_window_resetting_exactly_now_is_not_yet_zeroed(self) -> None:
        snap = cc.make_snapshot(80.0, 70.0, _iso(NOW), _iso(NOW))
        self.assertEqual(cc.effective_usage(snap, NOW), (80.0, 70.0))

    def test_reset_exactly_now_is_not_in_the_future(self) -> None:
        self._save_with_usage("vlad", _creds("t"), _account("v@x"), 1.0, 1.0, _iso(NOW), _iso(NOW))
        self.assertEqual(cc.earliest_future_reset(["vlad"], NOW), 0.0)

    def test_refresh_token_expiring_exactly_now_is_dead(self) -> None:
        data = {"credentials": _creds("t", refresh_expires=int(NOW * 1000)), "oauthAccount": _account("a@x")}
        self.assertEqual(cc.profile_unusable_reason(data, NOW), "refresh token expired")

    def test_exhausted_deadline_exactly_now_is_not_reported(self) -> None:
        self._enable_auto()
        cc.write_epoch_file(self.exhausted_file, NOW)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_auto(argparse.Namespace(action="status"))
        self.assertNotIn("exhausted", out.getvalue())

    def test_pick_stays_when_the_candidate_ties_the_active_account(self) -> None:
        """Equal weekly usage is no reason to move — the swap would be churn."""
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 40.0))), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "me")

    def test_pick_moves_when_the_candidate_is_lower_by_one_point(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            return (200, _api_usage(5.0, 40.0)) if token == "token-me" else (200, _api_usage(5.0, 39.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "vlad")

    def test_epoch_zero_renders_as_unknown(self) -> None:
        self.assertEqual(cc._fmt_epoch(0), "unknown")
        self.assertEqual(cc._fmt_epoch(-1), "unknown")
        self.assertNotEqual(cc._fmt_epoch(NOW), "unknown")


class TestExactTimeArithmetic(AutoBaseTest):
    """Millisecond conversions — an inverted factor still looks plausible."""

    def setUp(self) -> None:
        super().setUp()
        self._save_with_usage("idle", _creds("old", expires=1000), _account("i@x"))

    def test_expiry_is_stored_in_milliseconds(self) -> None:
        payload = {"access_token": "a", "expires_in": 28800, "refresh_token_expires_in": 600000}
        with patch.object(cc, "time") as fake_time, patch.object(cc, "oauth_refresh", return_value=(200, payload)):
            fake_time.time.return_value = NOW
            cc.access_token_for("idle", None)
        oauth = json.loads(self._profile_path("idle").read_text())["credentials"]["claudeAiOauth"]
        self.assertEqual(oauth["expiresAt"], int(NOW * 1000) + 28800 * 1000)
        self.assertEqual(oauth["refreshTokenExpiresAt"], int(NOW * 1000) + 600000 * 1000)

    def test_a_token_expiring_within_the_minute_is_refreshed(self) -> None:
        """Anything closer than the safety margin must be replaced, not used."""
        self._save_with_usage("edge", _creds("old", expires=int((NOW + 30) * 1000)), _account("e@x"))
        payload = {"access_token": "fresh", "expires_in": 28800}
        with patch.object(cc, "time") as fake_time, patch.object(cc, "oauth_refresh", return_value=(200, payload)):
            fake_time.time.return_value = NOW
            token, _, _t = cc.access_token_for("edge", None)
        self.assertEqual(token, "fresh")

    def test_a_token_beyond_the_margin_is_reused(self) -> None:
        self._save_with_usage("edge", _creds("still-good", expires=int((NOW + 300) * 1000)), _account("e@x"))
        with patch.object(cc, "time") as fake_time, patch.object(cc, "oauth_refresh", side_effect=AssertionError("no")):
            fake_time.time.return_value = NOW
            token, _, _t = cc.access_token_for("edge", None)
        self.assertEqual(token, "still-good")

    def test_login_warning_counts_real_hours(self) -> None:
        self._save_with_usage("soon", _creds("t", refresh_expires=int((NOW + 6 * HOUR) * 1000)), _account("s@x"))
        self.assertIn("6h", cc._profile_note("soon", NOW))

    def test_no_warning_just_past_the_window(self) -> None:
        self._save_with_usage("fine", _creds("t", refresh_expires=int((NOW + 3 * DAY) * 1000)), _account("f@x"))
        self.assertEqual(cc._profile_note("fine", NOW), "")


class TestRemainingBoundaries(TickTestCase):
    def test_pick_five_hour_exactly_at_the_bar_is_accepted(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            if token == "token-me":
                return 200, _api_usage(10.0, 60.0)
            return 200, _api_usage(cc.ENTER_5H, 20.0)

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "vlad")

    def test_pick_weekly_exactly_at_the_bar_is_accepted(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            if token == "token-me":
                return 200, _api_usage(10.0, 99.0)
            return 200, _api_usage(5.0, cc.ENTER_7D)

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "vlad")

    def test_login_warning_at_exactly_two_days_is_silent(self) -> None:
        """Two days out is comfortable; the warning is for the last stretch."""
        self._save_with_usage("edge", _creds("t", refresh_expires=int((NOW + 2 * DAY) * 1000)), _account("e@x"))
        self.assertEqual(cc._profile_note("edge", NOW), "")

    def test_login_warning_just_under_two_days_fires(self) -> None:
        self._save_with_usage("edge", _creds("t", refresh_expires=int((NOW + 2 * DAY - HOUR) * 1000)), _account("e@x"))
        self.assertIn("login expires in", cc._profile_note("edge", NOW))

    def test_quiet_switch_prints_nothing(self) -> None:
        """The statusline-driven path must stay silent; `use` must not."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc._auto_backup_active("vlad", quiet=True)
        self.assertEqual(out.getvalue(), "")

    def test_loud_switch_announces_the_auto_save(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc._auto_backup_active("vlad", quiet=False)
        self.assertIn("Auto-saved active profile 'me'", out.getvalue())

    def test_backup_recovers_from_a_marker_naming_a_deleted_profile(self) -> None:
        """A stale marker must not stop the save — the live email still resolves."""
        cc.write_active("ghost")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc._auto_backup_active("vlad", quiet=False)
        self.assertIn("Auto-saved active profile 'me'", out.getvalue())

    def test_backup_does_nothing_without_a_resolvable_account(self) -> None:
        cc.write_active("ghost")
        self._write_main(_account("stranger@example.com"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc._auto_backup_active("vlad", quiet=False)
        self.assertEqual(out.getvalue(), "")

    def test_refresh_failure_without_a_payload_is_reported(self) -> None:
        """A 200 with an unparsable body must not be mistaken for success."""
        self._save_with_usage("idle", _creds("old", expires=1000), _account("i@x"))
        with patch.object(cc, "oauth_refresh", return_value=(200, None)):
            token, error, transient = cc.access_token_for("idle", None)
        self.assertIsNone(token)
        self.assertIn("refresh failed", error or "")
        self.assertTrue(transient)

    def test_refresh_payload_without_an_access_token_is_reported(self) -> None:
        self._save_with_usage("idle", _creds("old", expires=1000), _account("i@x"))
        with patch.object(cc, "oauth_refresh", return_value=(200, {"token_type": "Bearer"})):
            token, error, transient = cc.access_token_for("idle", None)
        self.assertIsNone(token)
        self.assertIn("refresh failed", error or "")
        self.assertTrue(transient)

    def test_missing_refresh_token_is_sent_as_empty(self) -> None:
        """An empty string reaches the server and is rejected there, cleanly."""
        cc.save_profile("bare", {"claudeAiOauth": {"accessToken": "a", "expiresAt": 1000}}, _account("b@x"))
        with patch.object(cc, "oauth_refresh", return_value=(400, {"error": "invalid_grant"})) as refresh:
            cc.access_token_for("bare", None)
        refresh.assert_called_once_with("")


class TestParseIso(unittest.TestCase):
    def test_none_is_rejected(self) -> None:
        self.assertIsNone(cc.parse_iso(None))

    def test_empty_string_is_rejected(self) -> None:
        """An empty timestamp must not be read as the epoch."""
        self.assertIsNone(cc.parse_iso(""))

    def test_non_string_is_rejected(self) -> None:
        self.assertIsNone(cc.parse_iso(12345))

    def test_zulu_suffix_is_understood(self) -> None:
        self.assertEqual(cc.parse_iso(_iso(NOW)), NOW)

    def test_offset_form_is_understood(self) -> None:
        self.assertEqual(cc.parse_iso("2026-08-20T19:49:59+00:00"), cc.parse_iso("2026-08-20T19:49:59Z"))

    def test_garbage_is_rejected(self) -> None:
        self.assertIsNone(cc.parse_iso("tomorrow"))


class TestUsageRowActiveFlag(AutoBaseTest):
    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"))
        cc.write_active("me")

    def _rows(self) -> dict[str, dict[str, object]]:
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))), contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=True))
        return {r["profile"]: r for r in json.loads(out.getvalue())}

    def test_only_the_active_profile_is_flagged(self) -> None:
        rows = self._rows()
        self.assertTrue(rows["me"]["active"])
        self.assertFalse(rows["vlad"]["active"])

    def test_status_names_the_active_profile(self) -> None:
        rows = self._rows()
        self.assertEqual(rows["me"]["status"], "active")
        self.assertEqual(rows["vlad"]["status"], "ok")

    def test_marker_column_marks_exactly_one_row(self) -> None:
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))), contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=False))
        starred = [ln for ln in out.getvalue().splitlines() if ln.startswith("*")]
        self.assertEqual(len(starred), 1)
        self.assertIn("me", starred[0])


class TestTransientFailureIsNotExhaustion(TickTestCase):
    """An unreachable candidate is no evidence that accounts are spent.

    Recording a reset deadline on a network blip would suppress switching
    until that reset — hours, or days for a weekly window — while the account
    actually had room all along.
    """

    def test_network_failure_writes_a_short_retry_not_a_deadline(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(0, {"error": "offline"})):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0, _iso(NOW + HOUR), _iso(NOW + DAY)))
        self.assertFalse(self.exhausted_file.exists())
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.RETRY_SECONDS))
        self.assertIn("retrying", self._log_text())

    def test_throttled_refresh_writes_a_short_retry_not_a_deadline(self) -> None:
        self._save_with_usage("vlad", _creds("token-vlad", expires=1000), _account("v@x"), 5.0, 20.0)
        with patch.object(cc, "oauth_refresh", return_value=(429, {"error": "rate limited"})):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0, _iso(NOW + HOUR), _iso(NOW + DAY)))
        self.assertFalse(self.exhausted_file.exists())
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.RETRY_SECONDS))

    def test_the_retry_pause_is_far_shorter_than_a_reset(self) -> None:
        self.assertLess(cc.RETRY_SECONDS, HOUR)

    def test_genuine_exhaustion_still_writes_the_deadline(self) -> None:
        """The transient path must not swallow the real exhaustion case."""
        with self._patch_usage(10.0, cc.ENTER_7D + 5):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0, _iso(NOW + HOUR), _iso(NOW + DAY)))
        self.assertEqual(cc.read_epoch_file(self.exhausted_file), int(NOW + HOUR))
        self.assertNotIn("retrying", self._log_text())

    def test_unauthorized_is_not_transient(self) -> None:
        """A rejected login is real evidence — it must not be retried as a blip."""
        with patch.object(cc, "fetch_usage", return_value=(401, {"error": "expired"})):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0, _iso(NOW + HOUR), _iso(NOW + DAY)))
        self.assertEqual(cc.read_epoch_file(self.exhausted_file), int(NOW + HOUR))

    def test_a_reachable_full_account_beside_an_unreachable_one_still_retries(self) -> None:
        """One unreachable candidate is enough to make the conclusion unsafe."""
        self._save_with_usage("aaa", _creds("token-aaa"), _account("a@x"), 5.0, 10.0)

        def _usage(token: str) -> tuple[int, object]:
            if token == "token-aaa":
                return 0, {"error": "offline"}
            return 200, _api_usage(10.0, cc.ENTER_7D + 5)

        with patch.object(cc, "fetch_usage", side_effect=_usage):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0, _iso(NOW + HOUR), _iso(NOW + DAY)))
        self.assertFalse(self.exhausted_file.exists())
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.RETRY_SECONDS))

    def test_balance_reason_never_writes_a_retry_pause(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(0, {"error": "offline"})):
            cc.cmd_tick(self._tick_args(10.0, 40.0, _iso(NOW + HOUR), _iso(NOW + DAY)))
        self.assertFalse(self.exhausted_file.exists())
        self.assertFalse(self.settle_file.exists())


class TestConfirmationShape(unittest.TestCase):
    """The live-check result carries its own transient flag.

    Squeezing this into a plain (usage, error) pair is what let a network
    blip read as exhaustion — the caller could not tell the two apart.
    """

    def test_fields_are_named_and_ordered(self) -> None:
        snapshot = cc.make_snapshot(1.0, 2.0, None, None)
        result = cc.Confirmation(snapshot, None)
        self.assertIs(result.usage, snapshot)
        self.assertIsNone(result.error)
        self.assertFalse(result.transient)

    def test_transient_defaults_to_false(self) -> None:
        """Only an explicitly transient failure may suppress the deadline."""
        self.assertFalse(cc.Confirmation(None, "unauthorized (401)").transient)

    def test_transient_can_be_set(self) -> None:
        result = cc.Confirmation(None, "usage request failed (0)", transient=True)
        self.assertTrue(result.transient)
        self.assertIsNone(result.usage)


class TestConfirmationTransientFlag(AutoBaseTest):
    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        self._save_with_usage("idle", _creds("token-idle"), _account("i@x"))

    def test_success_is_not_transient(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))):
            result = cc.confirm_candidate("idle", "me")
        self.assertIsNotNone(result.usage)
        self.assertFalse(result.transient)

    def test_server_error_is_transient(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(500, {"error": "boom"})):
            result = cc.confirm_candidate("idle", "me")
        self.assertIsNone(result.usage)
        self.assertTrue(result.transient)

    def test_unauthorized_is_not_transient(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(403, {"error": "no"})):
            result = cc.confirm_candidate("idle", "me")
        self.assertIsNone(result.usage)
        self.assertFalse(result.transient)

    def test_corrupted_profile_is_not_transient(self) -> None:
        self._profile_path("broken").write_text("{not json")
        result = cc.confirm_candidate("broken", "me")
        self.assertIsNone(result.usage)
        self.assertFalse(result.transient)

    def test_refresh_rejection_is_not_transient(self) -> None:
        self._save_with_usage("idle", _creds("token-idle", expires=1000), _account("i@x"))
        with patch.object(cc, "oauth_refresh", return_value=(400, {"error": "invalid_grant"})):
            result = cc.confirm_candidate("idle", "me")
        self.assertFalse(result.transient)

    def test_refresh_throttling_is_transient(self) -> None:
        self._save_with_usage("idle", _creds("token-idle", expires=1000), _account("i@x"))
        with patch.object(cc, "oauth_refresh", return_value=(429, {"error": "slow down"})):
            result = cc.confirm_candidate("idle", "me")
        self.assertTrue(result.transient)


class TestWritePathsAreSerialized(AutoBaseTest):
    """Every command that writes credentials or profiles takes the same lock.

    They all run the identical read-decide-write sequence, so a manual command
    racing a statusline tick could overwrite the other's `.active`,
    credentials, and saved profiles — or double-refresh one expired token and
    retire a perfectly good account on the loser's invalid_grant.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")
        self._lock_fd = os.open(str(self.lock_file), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def tearDown(self) -> None:
        os.close(self._lock_fd)
        super().tearDown()

    def _assert_refuses(self, command: Callable[[], object]) -> None:
        with self.assertRaises(SystemExit) as ctx, _silence():
            command()
        self.assertEqual(ctx.exception.code, cc.EXIT_USER)

    def test_use_refuses_while_another_operation_holds_the_lock(self) -> None:
        self._assert_refuses(lambda: cc.cmd_use(argparse.Namespace(name="vlad")))
        self.assertEqual(cc.read_active(), "me")

    def test_usage_refuses_rather_than_double_refreshing(self) -> None:
        before = self._profile_path("vlad").read_text()
        with patch.object(cc, "oauth_refresh", side_effect=AssertionError("must not refresh")) as refresh:
            self._assert_refuses(lambda: cc.cmd_usage(argparse.Namespace(json=False)))
        refresh.assert_not_called()
        self.assertEqual(self._profile_path("vlad").read_text(), before)

    def test_add_refuses_and_writes_nothing(self) -> None:
        self._assert_refuses(lambda: cc.cmd_add(argparse.Namespace(name="fresh")))
        self.assertFalse(self._profile_path("fresh").exists())

    def test_remove_refuses_and_keeps_the_profile(self) -> None:
        with patch("builtins.input", return_value="y"):
            self._assert_refuses(lambda: cc.cmd_remove(argparse.Namespace(name="vlad")))
        self.assertTrue(self._profile_path("vlad").exists())

    def test_pick_refuses(self) -> None:
        with patch.object(cc, "fetch_usage", side_effect=AssertionError("must not run")) as usage:
            self._assert_refuses(lambda: cc.cmd_pick(argparse.Namespace()))
        usage.assert_not_called()
        self.assertEqual(cc.read_active(), "me")

    def test_tick_yields_silently_rather_than_failing(self) -> None:
        """The statusline path must never surface an error — it just waits."""
        self._enable_auto()
        with patch.object(cc, "fetch_usage", side_effect=AssertionError("must not run")):
            rc = cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(cc.read_active(), "me")


class TestWritePathsSucceedWhenFree(AutoBaseTest):
    """The same commands still work when nothing holds the lock."""

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")

    def test_use_switches(self) -> None:
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(cc.read_active(), "vlad")

    def test_add_saves(self) -> None:
        with _silence():
            cc.cmd_add(argparse.Namespace(name="fresh"))
        self.assertTrue(self._profile_path("fresh").exists())

    def test_remove_deletes_and_refreshes_the_gate(self) -> None:
        with patch("builtins.input", return_value="y"), _silence():
            cc.cmd_remove(argparse.Namespace(name="vlad"))
        self.assertFalse(self._profile_path("vlad").exists())
        self.assertTrue(self.gate_file.exists())

    def test_usage_runs(self) -> None:
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))), contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=False))
        self.assertIn("vlad", out.getvalue())

    def test_lock_is_released_after_each_command(self) -> None:
        """A leaked lock would deadlock every later switch."""
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        with cc.profile_lock() as acquired:
            self.assertTrue(acquired)


class TestThresholdValidation(unittest.TestCase):
    """Overrides that would only explode later must be rejected at read time.

    A NaN threshold parses fine and then raises inside `math.floor()` while
    writing the gate — after `.auto` is on, so every statusline render would
    spawn a crashing tick.
    """

    def _reject(self, name: str, value: str, reader: object = None) -> tuple[int, str]:
        """Run a reader with the override set; return (exit code, stderr)."""
        fn = reader or cc._env_float
        err = io.StringIO()
        with (
            patch.dict(os.environ, {name: value}),
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                fn(name, 50.0)
            except SystemExit as exc:
                return int(exc.code or 0), err.getvalue()
        raise AssertionError(f"{name}={value!r} was accepted")

    def test_nan_is_rejected(self) -> None:
        code, message = self._reject("CC_SWITCH_TEST_VALUE", "nan")
        self.assertEqual(code, cc.EXIT_USER)
        self.assertIn("finite", message)

    def test_infinity_is_rejected(self) -> None:
        code, message = self._reject("CC_SWITCH_TEST_VALUE", "inf")
        self.assertEqual(code, cc.EXIT_USER)
        self.assertIn("finite", message)

    def test_negative_percentage_is_rejected(self) -> None:
        code, message = self._reject("CC_SWITCH_TEST_VALUE", "-1")
        self.assertEqual(code, cc.EXIT_USER)
        self.assertIn("between 0 and 100", message)

    def test_percentage_above_one_hundred_is_rejected(self) -> None:
        code, message = self._reject("CC_SWITCH_TEST_VALUE", "101")
        self.assertEqual(code, cc.EXIT_USER)
        self.assertIn("between 0 and 100", message)

    def test_percentage_bounds_are_inclusive(self) -> None:
        for value in ("0", "100"):
            with patch.dict(os.environ, {"CC_SWITCH_TEST_VALUE": value}):
                self.assertEqual(cc._env_float("CC_SWITCH_TEST_VALUE", 50.0), float(value))

    def test_seconds_reject_nan(self) -> None:
        code, message = self._reject("CC_SWITCH_TEST_SECONDS", "nan", cc._env_seconds)
        self.assertEqual(code, cc.EXIT_USER)
        self.assertIn("non-negative finite", message)

    def test_seconds_reject_negative(self) -> None:
        code, message = self._reject("CC_SWITCH_TEST_SECONDS", "-5", cc._env_seconds)
        self.assertEqual(code, cc.EXIT_USER)
        self.assertIn("non-negative finite", message)

    def test_seconds_reject_non_numeric(self) -> None:
        code, message = self._reject("CC_SWITCH_TEST_SECONDS", "five minutes", cc._env_seconds)
        self.assertEqual(code, cc.EXIT_USER)
        self.assertIn("expected a number", message)

    def test_seconds_allow_values_above_one_hundred(self) -> None:
        """Durations are not percentages — 300s must not hit the 0..100 bound."""
        with patch.dict(os.environ, {"CC_SWITCH_TEST_SECONDS": "300"}):
            self.assertEqual(cc._env_seconds("CC_SWITCH_TEST_SECONDS", 60.0), 300.0)

    def test_seconds_allow_zero(self) -> None:
        with patch.dict(os.environ, {"CC_SWITCH_TEST_SECONDS": "0"}):
            self.assertEqual(cc._env_seconds("CC_SWITCH_TEST_SECONDS", 60.0), 0.0)

    def test_configured_thresholds_are_floor_safe(self) -> None:
        """The gate writes these through math.floor on every recompute."""
        for value in (cc.EXIT_5H, cc.EXIT_7D, cc.ENTER_5H, cc.ENTER_7D, cc.BALANCE_GAP_7D):
            self.assertTrue(math.isfinite(value))
            self.assertIsInstance(math.floor(value), int)

    def test_entry_bars_stay_below_their_exit_thresholds(self) -> None:
        """Equal bars would destroy the hysteresis and let accounts flap."""
        self.assertLessEqual(cc.ENTER_5H, cc.EXIT_5H)
        self.assertLessEqual(cc.ENTER_7D, cc.EXIT_7D)

    def test_a_five_hour_entry_bar_above_its_exit_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as ctx, _silence():
            cc.check_threshold_consistency(96.0, 95.0, 94.0, 99.0, 5.0)
        self.assertEqual(ctx.exception.code, cc.EXIT_USER)

    def test_a_weekly_entry_bar_above_its_exit_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as ctx, _silence():
            cc.check_threshold_consistency(90.0, 95.0, 100.0, 99.0, 5.0)
        self.assertEqual(ctx.exception.code, cc.EXIT_USER)

    def test_equal_bars_are_allowed(self) -> None:
        """Equal is the degenerate-but-valid end of the range; above it is not."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cc.check_threshold_consistency(95.0, 95.0, 99.0, 99.0, 5.0)
        self.assertEqual(err.getvalue(), "")

    def test_the_shipped_defaults_are_consistent(self) -> None:
        self.assertLessEqual(cc.DEFAULT_ENTER_5H, cc.DEFAULT_EXIT_5H)
        self.assertLessEqual(cc.DEFAULT_ENTER_7D, cc.DEFAULT_EXIT_7D)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cc.check_threshold_consistency(
                cc.DEFAULT_ENTER_5H,
                cc.DEFAULT_EXIT_5H,
                cc.DEFAULT_ENTER_7D,
                cc.DEFAULT_EXIT_7D,
                cc.DEFAULT_BALANCE_GAP_7D,
            )
        self.assertEqual(err.getvalue(), "")

    def test_non_finite_is_rejected_before_the_range_check(self) -> None:
        """NaN slips through `0 <= x <= 100` (every comparison is False)."""
        with patch.dict(os.environ, {"CC_SWITCH_TEST_VALUE": "nan"}), _silence():
            with self.assertRaises(SystemExit):
                cc._env_float("CC_SWITCH_TEST_VALUE", 50.0)
        # Prove the range check alone would NOT have caught it.
        self.assertFalse(0 <= float("nan") <= 100)
        self.assertFalse(float("nan") > 100)


class TestExpiryParsing(unittest.TestCase):
    """A malformed lifetime must not raise — it must leave the stored one alone."""

    def test_valid_seconds_become_an_absolute_deadline(self) -> None:
        self.assertEqual(cc._expiry_ms(28800, 1_000), 1_000 + 28800 * 1000)

    def test_numeric_string_is_accepted(self) -> None:
        self.assertEqual(cc._expiry_ms("600", 0), 600_000)

    def test_non_numeric_is_rejected_without_raising(self) -> None:
        self.assertIsNone(cc._expiry_ms("soon", 0))

    def test_none_is_rejected(self) -> None:
        self.assertIsNone(cc._expiry_ms(None, 0))

    def test_zero_and_negative_are_rejected(self) -> None:
        self.assertIsNone(cc._expiry_ms(0, 0))
        self.assertIsNone(cc._expiry_ms(-5, 0))


class TestRefreshNeverLosesTokens(AutoBaseTest):
    """The old refresh token dies on the server's answer.

    Between that answer and the profile write there is exactly one chance to
    keep the account. Every failure in that window must still put the rotated
    pair somewhere recoverable.
    """

    def setUp(self) -> None:
        super().setUp()
        self._save_with_usage("idle", _creds("old", expires=1000), _account("i@x"))
        self.recovery = self.profiles_dir / ".recovery-idle.json"

    def test_malformed_expiry_does_not_lose_the_tokens(self) -> None:
        payload = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": "soon"}
        with patch.object(cc, "oauth_refresh", return_value=(200, payload)):
            token, error, _ = cc.access_token_for("idle", None)
        self.assertEqual((token, error), ("new-access", None))
        oauth = json.loads(self._profile_path("idle").read_text())["credentials"]["claudeAiOauth"]
        self.assertEqual(oauth["refreshToken"], "new-refresh")
        self.assertEqual(oauth["expiresAt"], 1000)  # untouched, not guessed

    def test_malformed_refresh_expiry_does_not_lose_the_tokens(self) -> None:
        payload = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "refresh_token_expires_in": {"weird": True},
        }
        with patch.object(cc, "oauth_refresh", return_value=(200, payload)):
            token, _, _t = cc.access_token_for("idle", None)
        self.assertEqual(token, "new-access")
        oauth = json.loads(self._profile_path("idle").read_text())["credentials"]["claudeAiOauth"]
        self.assertEqual(oauth["refreshToken"], "new-refresh")

    def test_an_unexpected_error_still_dumps_the_rotated_pair(self) -> None:
        payload = {"access_token": "new-access", "refresh_token": "new-refresh"}
        with (
            patch.object(cc, "oauth_refresh", return_value=(200, payload)),
            patch.object(cc, "atomic_write_json", side_effect=RuntimeError("disk gremlin")),
            self.assertRaises(RuntimeError),
            _silence(),
        ):
            cc.access_token_for("idle", None)
        self.assertTrue(self.recovery.exists())
        self.assertEqual(json.loads(self.recovery.read_text())["refresh_token"], "new-refresh")

    def test_a_fatal_write_still_dumps_the_rotated_pair(self) -> None:
        payload = {"access_token": "new-access", "refresh_token": "new-refresh"}
        with (
            patch.object(cc, "oauth_refresh", return_value=(200, payload)),
            patch.object(cc, "atomic_write_json", side_effect=SystemExit(2)),
            self.assertRaises(SystemExit),
            _silence(),
        ):
            cc.access_token_for("idle", None)
        self.assertTrue(self.recovery.exists())

    def test_a_vanished_profile_still_dumps_the_rotated_pair(self) -> None:
        """The profile can disappear between the refresh call and the write."""
        payload = {"access_token": "new-access", "refresh_token": "new-refresh"}
        with (
            patch.object(cc, "load_profile_data", return_value=None),
            self.assertRaises(SystemExit),
            _silence(),
        ):
            cc._store_refreshed_tokens("idle", payload)
        self.assertTrue(self.recovery.exists())
        self.assertEqual(json.loads(self.recovery.read_text())["refresh_token"], "new-refresh")


class TestResolveActiveTrustsLiveCredentials(AutoBaseTest):
    """The live credentials decide, not the marker.

    Claude Code can log into a different saved account on its own, which
    leaves `.active` naming a profile that is no longer live. Trusting it
    would make `usage` refresh the running session's own token — the exact
    thing the never-refresh-the-active rule exists to prevent.
    """

    def setUp(self) -> None:
        super().setUp()
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"))
        self._write_creds(_creds("token-vlad"))
        self._write_main(_account("vlad@example.com"))
        cc.write_active("me")  # stale: Claude Code logged into vlad

    def test_a_stale_marker_loses_to_the_live_account(self) -> None:
        self.assertEqual(cc.resolve_active(), "vlad")

    def test_usage_never_refreshes_the_live_account_behind_a_stale_marker(self) -> None:
        """The saved copy has an expired token; the live one is what counts."""
        self._save_with_usage("vlad", _creds("token-vlad", expires=1000), _account("vlad@example.com"))
        before = self._profile_path("vlad").read_text()
        with (
            patch.object(cc, "oauth_refresh", return_value=(200, {"access_token": "rotated"})) as refresh,
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))) as usage,
            _silence(),
        ):
            cc.cmd_usage(argparse.Namespace(json=False))
        refresh.assert_not_called()
        # It read the live token from the credentials file, not the stale copy.
        self.assertIn("token-vlad", [call.args[0] for call in usage.call_args_list])
        self.assertEqual(
            json.loads(self._profile_path("vlad").read_text())["credentials"],
            json.loads(before)["credentials"],
        )

    def test_pick_never_refreshes_the_live_account_behind_a_stale_marker(self) -> None:
        self._save_with_usage("vlad", _creds("token-vlad", expires=1000), _account("vlad@example.com"))
        with (
            patch.object(cc, "oauth_refresh", return_value=(200, {"access_token": "rotated"})) as refresh,
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))),
            _silence(),
        ):
            cc.cmd_pick(argparse.Namespace())
        refresh.assert_not_called()
        oauth = json.loads(self._profile_path("vlad").read_text())["credentials"]["claudeAiOauth"]
        self.assertEqual(oauth["accessToken"], "token-vlad")

    def test_an_agreeing_marker_is_used_as_is(self) -> None:
        cc.write_active("vlad")
        self.assertEqual(cc.resolve_active(), "vlad")

    def test_a_live_account_with_no_saved_profile_resolves_to_nothing(self) -> None:
        self._write_main(_account("stranger@example.com"))
        self.assertIsNone(cc.resolve_active())

    def test_without_a_live_email_the_marker_is_all_we_have(self) -> None:
        self._write_main({"accountUuid": "no-email"})
        self.assertEqual(cc.resolve_active(), "me")

    def test_without_credentials_the_marker_is_all_we_have(self) -> None:
        self.creds_file.unlink()
        self.assertEqual(cc.resolve_active(), "me")


class TestPickTakesTheLowestAccount(AutoBaseTest):
    """Live numbers decide, and every candidate is checked before deciding."""

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 60.0)
        # Snapshots order the checks: `first` looks best but live-checks worse.
        self._save_with_usage("first", _creds("token-first"), _account("f@x"), 5.0, 10.0)
        self._save_with_usage("second", _creds("token-second"), _account("s@x"), 5.0, 30.0)
        cc.write_active("me")

    def test_a_worse_first_candidate_does_not_hide_a_better_later_one(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            return {
                "token-me": (200, _api_usage(10.0, 60.0)),
                "token-first": (200, _api_usage(10.0, 70.0)),
                "token-second": (200, _api_usage(5.0, 20.0)),
            }[token]

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "second")

    def test_the_lowest_of_several_eligible_candidates_wins(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            return {
                "token-me": (200, _api_usage(10.0, 60.0)),
                "token-first": (200, _api_usage(5.0, 40.0)),
                "token-second": (200, _api_usage(5.0, 15.0)),
            }[token]

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "second")

    def test_every_candidate_is_checked_before_deciding(self) -> None:
        seen: list[str] = []

        def _usage(token: str) -> tuple[int, object]:
            seen.append(token)
            return 200, _api_usage(10.0, 70.0) if token != "token-me" else _api_usage(10.0, 60.0)

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertIn("token-first", seen)
        self.assertIn("token-second", seen)
        self.assertEqual(cc.read_active(), "me")

    def test_staying_put_when_no_candidate_beats_the_active_account(self) -> None:
        out = io.StringIO()
        with (
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(10.0, 60.0))),
            contextlib.redirect_stdout(out),
        ):
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "me")
        self.assertIn("nothing lower available", out.getvalue())

    def test_active_is_resolved_under_the_lock(self) -> None:
        """Resolving before the lock would let a completed switch mislead us."""
        order: list[str] = []
        real_lock = cc.profile_lock

        @contextlib.contextmanager
        def _tracking_lock() -> Iterator[bool]:
            order.append("lock")
            with real_lock() as acquired:
                yield acquired

        def _resolve() -> str:
            order.append("resolve")
            return "me"

        with (
            patch.object(cc, "profile_lock", _tracking_lock),
            patch.object(cc, "resolve_active", _resolve),
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(10.0, 60.0))),
            _silence(),
        ):
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(order[:2], ["lock", "resolve"])


class TestBackupNeverOverwritesTheWrongProfile(AutoBaseTest):
    """The credentials being saved decide which profile receives them.

    When Claude Code logs into B on its own, `.active` still names A. Saving
    the live credentials into A would overwrite A's account irrecoverably —
    the profile would then hold B's tokens under A's name.
    """

    def setUp(self) -> None:
        super().setUp()
        self._save_with_usage("a", _creds("token-a"), _account("a@example.com"), 10.0, 40.0)
        self._save_with_usage("b", _creds("token-b"), _account("b@example.com"), 5.0, 20.0)
        self._save_with_usage("c", _creds("token-c"), _account("c@example.com"), 1.0, 1.0)
        # Live session is on b, but the marker still says a.
        self._write_creds(_creds("token-b-live"))
        self._write_main(_account("b@example.com"))
        cc.write_active("a")

    def test_a_stale_marker_does_not_corrupt_the_named_profile(self) -> None:
        before = json.loads(self._profile_path("a").read_text())
        with _silence():
            cc._auto_backup_active("c")
        after = json.loads(self._profile_path("a").read_text())
        self.assertEqual(after["credentials"], before["credentials"])
        self.assertEqual(after["oauthAccount"]["emailAddress"], "a@example.com")

    def test_the_live_credentials_land_in_their_own_profile(self) -> None:
        with _silence():
            cc._auto_backup_active("c")
        saved = json.loads(self._profile_path("b").read_text())
        self.assertEqual(saved["credentials"]["claudeAiOauth"]["accessToken"], "token-b-live")
        self.assertEqual(saved["oauthAccount"]["emailAddress"], "b@example.com")

    def test_an_automatic_switch_preserves_every_other_profile(self) -> None:
        self._enable_auto()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 1.0))):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "c")
        a = json.loads(self._profile_path("a").read_text())
        self.assertEqual(a["oauthAccount"]["emailAddress"], "a@example.com")
        self.assertEqual(a["credentials"]["claudeAiOauth"]["accessToken"], "token-a")

    def test_backup_is_skipped_when_the_target_is_already_active(self) -> None:
        before = json.loads(self._profile_path("b").read_text())
        with _silence():
            cc._auto_backup_active("b")
        self.assertEqual(json.loads(self._profile_path("b").read_text()), before)


class TestListShowsTheLiveAccount(AutoBaseTest):
    """`list` marks whichever account is actually live, not what the marker says."""

    def setUp(self) -> None:
        super().setUp()
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"))
        self._write_creds(_creds("token-vlad"))
        self._write_main(_account("vlad@example.com"))
        cc.write_active("me")  # stale

    def _marked(self) -> list[str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        return [ln for ln in out.getvalue().splitlines() if ln.startswith("*")]

    def test_the_live_account_is_marked(self) -> None:
        marked = self._marked()
        self.assertEqual(len(marked), 1)
        self.assertIn("vlad", marked[0])

    def test_the_stale_marker_is_not_marked(self) -> None:
        self.assertNotIn("me", self._marked()[0])

    def test_an_agreeing_marker_still_marks_correctly(self) -> None:
        cc.write_active("vlad")
        self.assertIn("vlad", self._marked()[0])


class TestBalanceGapMustBePositive(unittest.TestCase):
    """A zero gap makes equal usage a reason to move — an endless ping-pong."""

    def test_zero_gap_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as ctx, _silence():
            cc.check_threshold_consistency(90.0, 95.0, 94.0, 99.0, 0.0)
        self.assertEqual(ctx.exception.code, cc.EXIT_USER)

    def test_negative_gap_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as ctx, _silence():
            cc.check_threshold_consistency(90.0, 95.0, 94.0, 99.0, -1.0)
        self.assertEqual(ctx.exception.code, cc.EXIT_USER)

    def test_the_smallest_positive_gap_is_allowed(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cc.check_threshold_consistency(90.0, 95.0, 94.0, 99.0, 1.0)
        self.assertEqual(err.getvalue(), "")

    def test_the_configured_gap_is_positive(self) -> None:
        self.assertGreater(cc.BALANCE_GAP_7D, 0)

    def test_equal_usage_is_never_a_balance_reason(self) -> None:
        """With a positive gap enforced, this can no longer flap."""
        self.assertIsNone(cc.switch_reason(10.0, 40.0, 40.0))


class TestMainDispatchesEveryCommand(AutoBaseTest):
    """Each subcommand reaches its handler through `main`.

    Parsing alone proves nothing: a missing `set_defaults(func=...)` still
    parses, then raises AttributeError on `args.func` at runtime.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")

    def test_auto_on_dispatches(self) -> None:
        with _silence():
            rc = cc.main(["auto", "on"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertTrue(cc.auto_enabled())

    def test_auto_off_dispatches(self) -> None:
        self._enable_auto()
        with _silence():
            rc = cc.main(["auto", "off"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertFalse(cc.auto_enabled())

    def test_auto_status_dispatches(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.main(["auto"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("Auto-switching", out.getvalue())

    def test_tick_dispatches(self) -> None:
        self._enable_auto()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))):
            rc = cc.main(["tick", "--5h", str(cc.EXIT_5H), "--7d", "40"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(cc.read_active(), "vlad")

    def test_tick_dispatches_with_reset_arguments(self) -> None:
        self._enable_auto()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))):
            rc = cc.main(["tick", "--5h", "10", "--7d", "40", "--resets-5h", _iso(NOW + HOUR), "--resets-7d", ""])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(self._stored_usage("me")["five_hour"]["resets_at"], _iso(NOW + HOUR))

    def test_pick_dispatches(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            return (200, _api_usage(10.0, 60.0)) if token == "token-me" else (200, _api_usage(5.0, 20.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            rc = cc.main(["pick"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(cc.read_active(), "vlad")

    def test_usage_dispatches(self) -> None:
        out = io.StringIO()
        with (
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(23.0, 40.0))),
            contextlib.redirect_stdout(out),
        ):
            rc = cc.main(["usage"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("23%", out.getvalue())

    def test_usage_json_dispatches(self) -> None:
        out = io.StringIO()
        with (
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(23.0, 40.0))),
            contextlib.redirect_stdout(out),
        ):
            rc = cc.main(["usage", "--json"])
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual({r["profile"] for r in json.loads(out.getvalue())}, {"me", "vlad"})

    def test_every_subcommand_has_a_handler(self) -> None:
        """A binding lost on any command would only show up at runtime."""
        for argv in (["list"], ["current"], ["auto"], ["pick"], ["usage"]):
            args = cc.build_parser().parse_args(argv)
            self.assertTrue(callable(getattr(args, "func", None)), argv)


class TestLiveAccountIsProtectedByIdentity(AutoBaseTest):
    """The account is protected, not the profile name.

    The same account saved twice under two names shares one refresh token, so
    refreshing the duplicate rotates the live session's token too.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-live"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-live"), _account("me@example.com"), 10.0, 40.0)
        # Same account, different profile name, expired stored token.
        self._save_with_usage("me-backup", _creds("token-live", expires=1000), _account("me@example.com"), 10.0, 40.0)
        cc.write_active("me")

    def test_a_duplicate_of_the_live_account_is_not_refreshed(self) -> None:
        with patch.object(cc, "oauth_refresh", return_value=(200, {"access_token": "rotated"})) as refresh:
            token, error, _ = cc.access_token_for("me-backup", "me")
        refresh.assert_not_called()
        self.assertEqual((token, error), ("token-live", None))

    def test_usage_does_not_refresh_the_duplicate(self) -> None:
        with (
            patch.object(cc, "oauth_refresh", return_value=(200, {"access_token": "rotated"})) as refresh,
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))),
            _silence(),
        ):
            cc.cmd_usage(argparse.Namespace(json=False))
        refresh.assert_not_called()

    def test_a_different_account_is_still_refreshed(self) -> None:
        """The guard must not block genuine candidates."""
        self._save_with_usage("other", _creds("token-other", expires=1000), _account("other@example.com"))
        with patch.object(cc, "oauth_refresh", return_value=(200, {"access_token": "rotated"})) as refresh:
            token, error, _ = cc.access_token_for("other", "me")
        refresh.assert_called_once()
        self.assertEqual((token, error), ("rotated", None))

    def test_the_live_check_needs_a_live_email(self) -> None:
        self._write_main({"accountUuid": "no-email"})
        self.assertFalse(cc._is_live_account("me-backup"))


class TestTickRecheckedUnderTheLock(TickTestCase):
    """Everything read before the lock is re-read after acquiring it.

    A tick can pass its pre-checks, then wait behind another operation's
    lock while the user runs `auto off` or a manual `use` lands.
    """

    def test_auto_turned_off_while_waiting_stops_the_switch(self) -> None:
        real_lock = cc.profile_lock

        @contextlib.contextmanager
        def _lock_then_disable() -> Iterator[bool]:
            with real_lock() as acquired:
                self.auto_file.unlink()  # `auto off` won the race
                yield acquired

        with (
            patch.object(cc, "profile_lock", _lock_then_disable),
            patch.object(cc, "fetch_usage", side_effect=AssertionError("must not run")),
        ):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "me")

    def test_a_settle_window_armed_while_waiting_stops_the_switch(self) -> None:
        real_lock = cc.profile_lock

        @contextlib.contextmanager
        def _lock_then_settle() -> Iterator[bool]:
            with real_lock() as acquired:
                cc.write_epoch_file(self.settle_file, NOW + 60)  # a manual `use` landed
                yield acquired

        with (
            patch.object(cc, "profile_lock", _lock_then_settle),
            patch.object(cc, "fetch_usage", side_effect=AssertionError("must not run")),
        ):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "me")

    def test_an_exhausted_deadline_armed_while_waiting_stops_the_switch(self) -> None:
        real_lock = cc.profile_lock

        @contextlib.contextmanager
        def _lock_then_exhaust() -> Iterator[bool]:
            with real_lock() as acquired:
                cc.write_epoch_file(self.exhausted_file, NOW + HOUR)
                yield acquired

        with (
            patch.object(cc, "profile_lock", _lock_then_exhaust),
            patch.object(cc, "fetch_usage", side_effect=AssertionError("must not run")),
        ):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "me")

    def test_nothing_changing_still_switches(self) -> None:
        """The recheck must not block the ordinary path."""
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_auto_off_takes_the_lock(self) -> None:
        fd = os.open(str(self.lock_file), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(SystemExit) as ctx, _silence():
                cc.cmd_auto(argparse.Namespace(action="off"))
        finally:
            os.close(fd)
        self.assertEqual(ctx.exception.code, cc.EXIT_USER)
        self.assertTrue(cc.auto_enabled())  # unchanged

    def test_auto_on_takes_the_lock(self) -> None:
        self.auto_file.unlink()
        fd = os.open(str(self.lock_file), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(SystemExit) as ctx, _silence():
                cc.cmd_auto(argparse.Namespace(action="on"))
        finally:
            os.close(fd)
        self.assertEqual(ctx.exception.code, cc.EXIT_USER)
        self.assertFalse(cc.auto_enabled())

    def test_auto_status_does_not_need_the_lock(self) -> None:
        """Reading state must never fail because something else is running."""
        fd = os.open(str(self.lock_file), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cc.cmd_auto(argparse.Namespace(action="status"))
        finally:
            os.close(fd)
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("Auto-switching", out.getvalue())


class TestFractionalThresholds(TickTestCase):
    """Fractional overrides decide in Python, on exact values."""

    def test_just_below_a_fractional_exit_does_not_switch(self) -> None:
        """The candidate is level on the week, so only the 5h limit could fire."""
        with patch.object(cc, "EXIT_5H", 95.9), self._patch_usage(5.0, 40.0):
            cc.cmd_tick(self._tick_args(95.5, 40.0))
        self.assertEqual(cc.read_active(), "me")

    def test_at_a_fractional_exit_switches(self) -> None:
        with patch.object(cc, "EXIT_5H", 95.9), self._patch_usage(5.0, 40.0):
            cc.cmd_tick(self._tick_args(95.9, 40.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_rounding_up_would_have_fired_early(self) -> None:
        """Pins the C7 defect: 95.5 rounds to 96 and would clear a 95.9 bar."""
        self.assertGreaterEqual(round(95.5), 95.9)
        self.assertLess(95.5, 95.9)

    def test_fractional_percentages_are_stored_unrounded(self) -> None:
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(12.4, 33.6))
        stored = self._stored_usage("me")
        self.assertEqual(stored["five_hour"]["utilization"], 12.4)
        self.assertEqual(stored["seven_day"]["utilization"], 33.6)

    def test_the_gate_trigger_is_floored_so_it_can_only_fire_early(self) -> None:
        """A floored trigger wakes the tick sooner; the tick then re-decides."""
        with patch.object(cc, "EXIT_5H", 95.9):
            cc.recompute_gate("me", NOW)
        trigger_5h = int(self.gate_file.read_text().split()[2])
        self.assertEqual(trigger_5h, 95)
        self.assertLessEqual(trigger_5h, 95.9)


class TestStatuslinePassesExactValues(unittest.TestCase):
    """The shell must not pre-round what the decision depends on."""

    def setUp(self) -> None:
        self.script = (Path(cc.__file__).parent / "statusline-command.sh").read_text()

    def test_tick_receives_the_unrounded_percentages(self) -> None:
        self.assertIn('tick --5h "$FIVE_H" --7d "$WEEK"', self.script)

    def test_rounded_values_are_only_used_for_the_gate_comparison(self) -> None:
        self.assertNotIn('--5h "$FIVE_I"', self.script)
        self.assertIn('"${FIVE_I:-0}" -ge', self.script)

    def test_the_gate_comparison_floors_rather_than_rounds(self) -> None:
        """Rounding up would make the sieve miss a tick it should have spawned."""
        self.assertIn("FIVE_I=${FIVE_H%%.*}", self.script)
        self.assertIn("WEEK_I=${WEEK%%.*}", self.script)


class TestParser(AutoBaseTest):
    def test_tick_arguments(self) -> None:
        args = cc.build_parser().parse_args(["tick", "--5h", "12.5", "--7d", "30", "--resets-5h", "x"])
        self.assertEqual((args.five_hour, args.seven_day, args.resets_5h), (12.5, 30.0, "x"))

    def test_auto_defaults_to_status(self) -> None:
        self.assertEqual(cc.build_parser().parse_args(["auto"]).action, "status")

    def test_usage_json_flag(self) -> None:
        self.assertTrue(cc.build_parser().parse_args(["usage", "--json"]).json)


if __name__ == "__main__":
    unittest.main(verbosity=2)

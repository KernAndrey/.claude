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
import subprocess
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

#: The statusline lives in ~/.claude, one level above this package: it is Claude
#: Code's status line, which settings.json points at, not a cc-switch component.
STATUSLINE_SCRIPT = _ROOT.parent / "statusline-command.sh"

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

        # EVERY module-level path is redirected here, not just the ones a
        # given test touches. A path left unpatched silently writes into the
        # real ~/.claude-profiles — which is how a test profile once ended up
        # named as the user's live account.
        self._patches = [
            patch.object(cc, "CLAUDE_DIR", self.claude_dir),
            patch.object(cc, "CREDS_FILE", self.creds_file),
            patch.object(cc, "MAIN_FILE", self.main_file),
            patch.object(cc, "PROFILES_DIR", self.profiles_dir),
            patch.object(cc, "ACTIVE_FILE", self.active_file),
            patch.object(cc, "BACKUPS_DIR", self.backups_dir),
            patch.object(cc, "LIVE_FILE", self.profiles_dir / ".live"),
            patch.object(cc, "AUTO_FILE", self.profiles_dir / ".auto"),
            patch.object(cc, "GATE_FILE", self.profiles_dir / ".gate"),
            patch.object(cc, "EXHAUSTED_FILE", self.profiles_dir / ".exhausted"),
            patch.object(cc, "SETTLE_FILE", self.profiles_dir / ".settle"),
            patch.object(cc, "LOG_FILE", self.profiles_dir / ".auto.log"),
            patch.object(cc, "LOCK_FILE", self.profiles_dir / ".lock"),
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
        self.assertEqual(out.getvalue().split()[0], "work")

    def test_falls_back_to_email_match(self) -> None:
        self._write_creds()
        self._write_main(SAMPLE_OAUTH)
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        # no .active file
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_current(cc.build_parser().parse_args(["current"]))
        self.assertEqual(out.getvalue().split()[0], "work")

    def test_stale_active_falls_back(self) -> None:
        """If .active names a removed profile, fall back to email scan."""
        self._write_creds()
        self._write_main(SAMPLE_OAUTH)
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        cc.write_active("removed")  # points at nonexistent profile
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_current(cc.build_parser().parse_args(["current"]))
        self.assertEqual(out.getvalue().split()[0], "work")

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
        self.assertEqual(out.getvalue().split()[0], "work")

    def test_skips_non_dict_json_profiles_in_email_scan(self) -> None:
        self._write_creds()
        self._write_main(SAMPLE_OAUTH)
        (self.profiles_dir / "array.json").write_text("[1, 2]")
        cc.save_profile("work", SAMPLE_CREDS, SAMPLE_OAUTH)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.cmd_current(cc.build_parser().parse_args(["current"]))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(out.getvalue().split()[0], "work")


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
        self.assertEqual(out.getvalue().split()[0], "alice")

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
        self.live_file = self.profiles_dir / ".live"
        self.lock_file = self.profiles_dir / ".lock"
        self._auto_patches = [
            patch.object(cc, "AUTO_FILE", self.auto_file),
            patch.object(cc, "GATE_FILE", self.gate_file),
            patch.object(cc, "EXHAUSTED_FILE", self.exhausted_file),
            patch.object(cc, "SETTLE_FILE", self.settle_file),
            patch.object(cc, "LOG_FILE", self.log_file),
            patch.object(cc, "LIVE_FILE", self.live_file),
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


class TestHoursUntil(unittest.TestCase):
    """Hours of weekly window left — the denominator of every decision."""

    def test_a_known_future_reset(self) -> None:
        self.assertAlmostEqual(cc.hours_until(NOW + 12 * HOUR, NOW), 12.0)

    def test_an_absent_reset_gets_a_nominal_week(self) -> None:
        """A window we know nothing about has not started: least urgent, and
        the only answer that cannot invent urgency out of missing data."""
        self.assertEqual(cc.hours_until(None, NOW), cc.WINDOW_HOURS)

    def test_a_past_reset_gets_a_nominal_week(self) -> None:
        self.assertEqual(cc.hours_until(NOW - HOUR, NOW), cc.WINDOW_HOURS)

    def test_a_reset_exactly_now_gets_a_nominal_week(self) -> None:
        self.assertEqual(cc.hours_until(NOW, NOW), cc.WINDOW_HOURS)

    def test_an_imminent_reset_is_floored(self) -> None:
        """Without the floor the required burn rate runs away to infinity."""
        self.assertEqual(cc.hours_until(NOW + 60, NOW), cc.MIN_TTR_HOURS)

    def test_a_far_future_reset_is_capped(self) -> None:
        """A hand-edited date must not make an account look free forever."""
        self.assertEqual(cc.hours_until(NOW + 400 * HOUR, NOW), cc.WINDOW_HOURS)

    def test_a_non_finite_reset_gets_a_nominal_week(self) -> None:
        self.assertEqual(cc.hours_until(float("inf"), NOW), cc.WINDOW_HOURS)


class TestWeeklyPressure(unittest.TestCase):
    def test_pp_per_hour_at_alpha_one(self) -> None:
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertAlmostEqual(cc.weekly_pressure(75.0, 12.0), 25.0 / 12.0)

    def test_alpha_zero_is_plain_headroom(self) -> None:
        """The escape hatch: `ttr ** 0` is exactly 1.0 at any deadline."""
        with patch.object(cc, "URGENCY_ALPHA", 0.0):
            self.assertEqual(cc.weekly_pressure(75.0, 12.0), 25.0)
            self.assertEqual(cc.weekly_pressure(75.0, 168.0), 25.0)

    def test_the_neutral_baseline_is_a_full_fresh_week(self) -> None:
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertAlmostEqual(cc.weekly_pressure(0.0, cc.WINDOW_HOURS), 100.0 / 168.0)

    def test_an_earlier_deadline_outranks_more_headroom(self) -> None:
        """The whole change: 25pp in twelve hours beats 80pp in six days."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertGreater(cc.weekly_pressure(75.0, 12.0), cc.weekly_pressure(20.0, 144.0))

    def test_a_non_finite_utilization_is_no_headroom(self) -> None:
        """`max(0.0, nan)` is 0.0, so a NaN reaching the gate writes a trigger
        of zero — which the statusline satisfies on every render."""
        self.assertEqual(cc.weekly_pressure(float("nan"), 12.0), 0.0)


class TestScoreUsage(unittest.TestCase):
    def test_it_carries_the_deadline_effective_usage_drops(self) -> None:
        snap = cc.make_snapshot(30.0, 60.0, None, _iso(NOW + 12 * HOUR))
        score = cc.score_usage(snap, NOW)
        self.assertEqual((score.five_hour, score.seven_day), (30.0, 60.0))
        self.assertAlmostEqual(score.ttr_hours, 12.0)

    def test_a_rolled_over_weekly_window_is_a_fresh_week(self) -> None:
        """Zeroed usage and a nominal week must arrive together, or a 0%
        account would look like it had to burn 100pp in the last minute."""
        snap = cc.make_snapshot(30.0, 96.0, None, _iso(NOW - 1))
        score = cc.score_usage(snap, NOW)
        self.assertEqual(score.seven_day, 0.0)
        self.assertEqual(score.ttr_hours, cc.WINDOW_HOURS)

    def test_no_snapshot_scores_as_a_free_week(self) -> None:
        score = cc.score_usage(None, NOW)
        self.assertEqual((score.five_hour, score.seven_day, score.ttr_hours), (0.0, 0.0, cc.WINDOW_HOURS))

    def test_a_non_dict_weekly_window_does_not_raise(self) -> None:
        """A hand-edited profile can hold a string here, and this runs inside
        `recompute_gate` — raising would take down every tick."""
        self.assertEqual(cc.effective_ttr_hours({"seven_day": "nonsense"}, NOW), cc.WINDOW_HOURS)

    def test_a_nan_utilization_reads_as_zero(self) -> None:
        """`json.loads` accepts a bare NaN literal and `usage_from_api` does
        an unchecked `float()`."""
        self.assertEqual(cc.effective_usage(cc.make_snapshot(1.0, float("nan"), None, None), NOW), (1.0, 0.0))

    def test_an_infinite_utilization_reads_as_zero(self) -> None:
        self.assertEqual(cc.effective_usage(cc.make_snapshot(float("inf"), 1.0, None, None), NOW), (0.0, 1.0))

    def test_a_boolean_weekly_utilization_reads_as_zero(self) -> None:
        """`bool` is an `int` in Python, so a hand-edited `true` used to reach
        `float()` and read as 1% — a real percentage, and very nearly the most
        attractive account on the board. It is not a measurement, so it reads
        as unknown like every other unusable value here.
        """
        self.assertEqual(cc.effective_usage(cc.make_snapshot(1.0, True, None, None), NOW), (1.0, 0.0))

    def test_a_boolean_five_hour_utilization_reads_as_zero(self) -> None:
        """Both windows go through the same reader; both had the same hole."""
        self.assertEqual(cc.effective_usage(cc.make_snapshot(True, 1.0, None, None), NOW), (0.0, 1.0))

    def test_false_is_not_a_zero_percent_measurement_either(self) -> None:
        """`False` already read as 0.0 by accident. It must keep doing so by
        rule, so the two booleans cannot diverge."""
        self.assertEqual(cc.effective_usage(cc.make_snapshot(1.0, False, None, None), NOW), (1.0, 0.0))

    def test_a_string_utilization_reads_as_zero(self) -> None:
        """The pre-existing shape in the same class: not a number at all."""
        self.assertEqual(cc.effective_usage(cc.make_snapshot(1.0, "nonsense", None, None), NOW), (1.0, 0.0))

    def test_a_boolean_utilization_scores_as_a_free_week(self) -> None:
        """What the gate and the ranking actually read, end of the chain."""
        score = cc.score_usage(cc.make_snapshot(True, True, None, None), NOW)
        self.assertEqual((score.five_hour, score.seven_day), (0.0, 0.0))
        self.assertAlmostEqual(score.pressure, cc.weekly_pressure(0.0, cc.WINDOW_HOURS))


class TestUsefulBalanceTarget(unittest.TestCase):
    """Whether an account is worth *rebalancing* onto — never whether it is
    somewhere to escape a limit, where any account beats none."""

    def _cand(self, seven: float, ttr: float) -> cc.Candidate:
        return cc.Candidate("c", 0.0, seven, ttr, cc.weekly_pressure(seven, ttr))

    def test_a_roomy_account_with_time_left_qualifies(self) -> None:
        self.assertTrue(cc.useful_balance_target(self._cand(20.0, 144.0)))

    def test_a_window_closing_within_the_hour_does_not(self) -> None:
        """Its headroom cannot be spent before the window rolls over, and the
        reset itself wakes a tick that reconsiders it holding a full week."""
        self.assertFalse(cc.useful_balance_target(self._cand(20.0, cc.MIN_TTR_HOURS)))

    def test_too_little_headroom_does_not(self) -> None:
        """Not worth the refresh-token rotation a switch costs."""
        self.assertFalse(cc.useful_balance_target(self._cand(100.0 - cc.MIN_HEADROOM_7D + 1, 144.0)))

    def test_the_headroom_bar_is_inclusive(self) -> None:
        self.assertTrue(cc.useful_balance_target(self._cand(100.0 - cc.MIN_HEADROOM_7D, 144.0)))

    def test_the_deadline_bar_is_inclusive(self) -> None:
        self.assertTrue(cc.useful_balance_target(self._cand(20.0, cc.MIN_USEFUL_TTR_HOURS)))

    def test_the_deadline_bar_is_inert_at_alpha_zero(self) -> None:
        """It withholds an urgency bonus that alpha 0 never grants."""
        with patch.object(cc, "URGENCY_ALPHA", 0.0):
            self.assertTrue(cc.useful_balance_target(self._cand(20.0, cc.MIN_TTR_HOURS)))

    def test_the_headroom_bar_still_applies_at_alpha_zero(self) -> None:
        with patch.object(cc, "URGENCY_ALPHA", 0.0):
            self.assertFalse(cc.useful_balance_target(self._cand(99.0, 144.0)))

    def test_it_accepts_a_bare_score_too(self) -> None:
        """`_try_candidates` passes a live Score, not a named Candidate."""
        self.assertTrue(cc.useful_balance_target(cc.score_usage(cc.make_snapshot(0.0, 20.0, None, None), NOW)))


class TestCandidateEquivalent7d(unittest.TestCase):
    """A candidate's worth, restated in the active account's percentage points."""

    def _cand(self, seven: float, ttr: float) -> cc.Candidate:
        return cc.Candidate("c", 0.0, seven, ttr, cc.weekly_pressure(seven, ttr))

    def test_alpha_zero_returns_the_real_percentage(self) -> None:
        """The escape hatch: every caller then reproduces the old rule."""
        with patch.object(cc, "URGENCY_ALPHA", 0.0):
            self.assertEqual(cc.candidate_equivalent_7d(self._cand(34.0, 12.0), 168.0), 34.0)

    def test_equal_windows_return_the_real_percentage(self) -> None:
        """Why most of this suite is unaffected: null resets on both sides."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            cand = self._cand(34.0, cc.WINDOW_HOURS)
            self.assertAlmostEqual(cc.candidate_equivalent_7d(cand, cc.WINDOW_HOURS), 34.0)

    def test_a_roomy_candidate_looks_worse_against_an_urgent_active(self) -> None:
        """75% resetting in twelve hours must not be abandoned for a safe 20%."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertGreater(cc.candidate_equivalent_7d(self._cand(20.0, 144.0), 12.0), 75.0)

    def test_an_urgent_candidate_looks_better_against_a_roomy_active(self) -> None:
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertLess(cc.candidate_equivalent_7d(self._cand(75.0, 12.0), 144.0), 20.0)

    def test_a_candidate_about_to_reset_gets_no_urgency_credit(self) -> None:
        """Its headroom cannot be spent before the window rolls over, and the
        reset itself wakes a tick that reconsiders it holding a full week."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertEqual(cc.candidate_equivalent_7d(self._cand(90.0, cc.MIN_TTR_HOURS), 168.0), 90.0)

    def test_a_nearly_spent_candidate_gets_no_urgency_credit(self) -> None:
        """Too little left to be worth the refresh-token rotation a switch costs."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertEqual(cc.candidate_equivalent_7d(self._cand(95.0, 2.0), 168.0), 95.0)

    def test_withholding_the_credit_still_lets_pick_escape(self) -> None:
        """Ruling a candidate out entirely looked equivalent and was not:
        `pick` compares against the active account with no limit branch, so
        an unusable answer left it on 99% with 94% available."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertLess(cc.candidate_equivalent_7d(self._cand(94.0, 168.0), 168.0), 99.0)

    def test_the_deadline_bar_is_inert_at_alpha_zero(self) -> None:
        """It exists to withhold an urgency bonus that alpha 0 never grants."""
        with patch.object(cc, "URGENCY_ALPHA", 0.0):
            self.assertEqual(cc.candidate_equivalent_7d(self._cand(50.0, cc.MIN_TTR_HOURS), 168.0), 50.0)

    def test_a_non_finite_result_is_never_a_reason(self) -> None:
        self.assertEqual(cc.candidate_equivalent_7d(cc.Candidate("c", 0.0, 0.0, 168.0, float("nan")), 168.0), 100.0)


class TestANonUnitExponent(AutoBaseTest):
    """Alpha 1 hides a whole class of mistake: `ttr ** 1` is `ttr`, so every
    place the exponent is applied looks identical to forgetting it. Alpha 2 is
    a supported setting (the bound is 0..10) and separates the two.
    """

    def _cand(self, seven: float, ttr: float) -> cc.Candidate:
        return cc.Candidate("c", 0.0, seven, ttr, cc.weekly_pressure(seven, ttr))

    def test_pressure_squares_the_window(self) -> None:
        with patch.object(cc, "URGENCY_ALPHA", 2.0):
            self.assertAlmostEqual(cc.weekly_pressure(75.0, 12.0), 25.0 / 144.0)

    def test_the_equivalent_squares_the_active_window(self) -> None:
        with patch.object(cc, "URGENCY_ALPHA", 2.0):
            # 40pp over 48h against an active account with 24h left.
            expected = 100.0 - (40.0 / 48.0**2) * 24.0**2
            self.assertAlmostEqual(cc.candidate_equivalent_7d(self._cand(60.0, 48.0), 24.0), expected)

    def test_equal_windows_still_cancel_at_alpha_two(self) -> None:
        """The identity that keeps BALANCE_GAP_7D meaning five points has to
        hold at every exponent, not just the default."""
        with patch.object(cc, "URGENCY_ALPHA", 2.0):
            self.assertAlmostEqual(cc.candidate_equivalent_7d(self._cand(34.0, 90.0), 90.0), 34.0)

    def test_the_exponent_changes_the_ranking(self) -> None:
        """60% resetting in 48h loses to 15% in 96h at alpha 1 and wins at
        alpha 2, where the deadline dominates."""
        self._save_with_usage("sooner", _creds("a"), _account("a@x"), 0.0, 60.0, None, _iso(NOW + 48 * HOUR))
        self._save_with_usage("later", _creds("b"), _account("b@x"), 0.0, 15.0, None, _iso(NOW + 96 * HOUR))
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertEqual([c.name for c in cc.rank_candidates(NOW, None)], ["later", "sooner"])
        with patch.object(cc, "URGENCY_ALPHA", 2.0):
            self.assertEqual([c.name for c in cc.rank_candidates(NOW, None)], ["sooner", "later"])

    def test_the_gate_trigger_follows_the_exponent(self) -> None:
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 40.0, None, _iso(NOW + 24 * HOUR))
        self._save_with_usage("other", _creds("a"), _account("a@x"), 0.0, 60.0, None, _iso(NOW + 48 * HOUR))
        with patch.object(cc, "URGENCY_ALPHA", 2.0):
            cc.recompute_gate("me", NOW)
            expected = min(cc.EXIT_7D, max(0.0, (100.0 - (40.0 / 48.0**2) * 24.0**2) + cc.BALANCE_GAP_7D))
        trigger = int(self.gate_file.read_text().split()[3])
        self.assertEqual(trigger, cc._gate_tenths(expected))


class TestBestBalanceEquivalent(unittest.TestCase):
    def test_no_candidates_is_a_hundred(self) -> None:
        """100 + the gap clamps to EXIT_7D and can never beat the active account."""
        self.assertEqual(cc.best_balance_equivalent([], 168.0), 100.0)

    def test_it_takes_the_minimum_not_the_head(self) -> None:
        """The order demotes candidates that are not worth rebalancing onto,
        so the head of the list is not always the most attractive one."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            doomed = cc.Candidate("doomed", 0.0, 90.0, cc.MIN_TTR_HOURS, cc.weekly_pressure(90.0, cc.MIN_TTR_HOURS))
            real = cc.Candidate("real", 0.0, 10.0, 168.0, cc.weekly_pressure(10.0, 168.0))
            self.assertAlmostEqual(cc.best_balance_equivalent([doomed, real], 168.0), 10.0)


class TestBurnCell(unittest.TestCase):
    """The number every decision turns on belongs beside the percentages."""

    def test_it_reports_pp_per_hour(self) -> None:
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            row = {"seven_day": 75.0, "resets_7d": _iso(NOW + 12 * HOUR)}
            self.assertEqual(cc._burn_cell(row, NOW), "2.08")

    def test_an_unknown_deadline_uses_the_nominal_week(self) -> None:
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertEqual(cc._burn_cell({"seven_day": 0.0, "resets_7d": None}, NOW), "0.60")

    def test_an_unusable_percentage_reads_as_unknown(self) -> None:
        """One hand-edited profile must not stop `usage` reporting the others."""
        self.assertEqual(cc._burn_cell({"seven_day": None, "resets_7d": None}, NOW), "?")

    def test_a_bool_is_not_a_percentage(self) -> None:
        self.assertEqual(cc._burn_cell({"seven_day": True, "resets_7d": None}, NOW), "?")


class TestEveryKnobIsActuallyWired(unittest.TestCase):
    """Each documented variable reaches the global that acts on it.

    The validators are tested through a throwaway name and the behaviour is
    tested by patching the global, so between the two nothing ever read the
    *documented* spelling. A typo in one of these strings, or a binding left
    off after adding a knob, is invisible to every other test here and to the
    README — the setting simply does nothing.

    Run in a subprocess because these are read once at import.
    """

    #: (environment variable, module global, value to set, expected float)
    KNOBS = (
        ("CC_SWITCH_EXIT_5H", "EXIT_5H", "96", 96.0),
        ("CC_SWITCH_EXIT_7D", "EXIT_7D", "98", 98.0),
        ("CC_SWITCH_ENTER_5H", "ENTER_5H", "80", 80.0),
        ("CC_SWITCH_ENTER_7D", "ENTER_7D", "85", 85.0),
        ("CC_SWITCH_BALANCE_GAP_7D", "BALANCE_GAP_7D", "7", 7.0),
        ("CC_SWITCH_SETTLE_SECONDS", "SETTLE_SECONDS", "30", 30.0),
        ("CC_SWITCH_RETRY_SECONDS", "RETRY_SECONDS", "120", 120.0),
        ("CC_SWITCH_MIN_HEADROOM_7D", "MIN_HEADROOM_7D", "25", 25.0),
        ("CC_SWITCH_URGENCY_ALPHA", "URGENCY_ALPHA", "2", 2.0),
        ("CC_SWITCH_CROSSOVER_POLL_SECONDS", "CROSSOVER_POLL_SECONDS", "600", 600.0),
    )

    def _read_global(self, env_var: str, attr: str, value: str) -> float:
        env = dict(os.environ, **{env_var: value})
        env.pop("PYTHONPATH", None)
        proc = subprocess.run(
            [sys.executable, "-c", f"import cc_switch; print(cc_switch.{attr})"],
            capture_output=True,
            text=True,
            cwd=str(Path(cc.__file__).parent),
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return float(proc.stdout.strip())

    def test_every_documented_knob_reaches_its_global(self) -> None:
        for env_var, attr, value, expected in self.KNOBS:
            with self.subTest(env_var=env_var):
                self.assertEqual(self._read_global(env_var, attr, value), expected)

    def test_the_readme_lists_exactly_these_knobs(self) -> None:
        """A knob nobody can discover is a knob nobody can use."""
        readme = (Path(cc.__file__).parent / "README.md").read_text()
        for env_var, _, _, _ in self.KNOBS:
            self.assertIn(env_var, readme)


class TestAlphaZeroReproducesTheOldRule(AutoBaseTest):
    """The compatibility claim the README makes, pinned rather than asserted.

    At ALPHA 0 the ranking must be the pre-change one exactly — weekly usage
    ascending, then 5h, then name. It survives the demotion tier only because
    that tier is monotone in usage: headroom >= MIN_HEADROOM_7D is the same as
    usage <= 100 - MIN_HEADROOM_7D, so demoting a candidate can never move it
    past one with more usage. That is not obvious, and a change to either bar
    could break it silently.

    These drive the real `rank_candidates` rather than a copy of its sort key,
    so the production ordering is what is being compared.
    """

    def _save(self, name: str, seven: float, five: float = 0.0, reset: float | None = None) -> None:
        self._save_with_usage(name, _creds(name), _account(f"{name}@x"), five, seven,
                              None, _iso(reset) if reset else None)

    def _ranked(self) -> list[str]:
        with patch.object(cc, "URGENCY_ALPHA", 0.0):
            return [c.name for c in cc.rank_candidates(NOW, None)]

    def _old_rule(self) -> list[str]:
        """The pre-change key, computed from the same stored snapshots."""
        rows = []
        for path in cc.list_profiles():
            five, seven = cc.effective_usage((cc.load_profile_data(path.stem) or {}).get("usage"), NOW)
            rows.append((seven, five, path.stem))
        return [name for _, _, name in sorted(rows)]

    def test_a_demoted_candidate_does_not_jump_a_roomier_one(self) -> None:
        """The case raised in review: 92% has only 8pp of headroom and is
        demoted, but it sorted behind 50% under the old rule anyway."""
        self._save("spent", 92.0)
        self._save("roomy", 50.0)
        self.assertEqual(self._ranked(), ["roomy", "spent"])
        self.assertEqual(self._ranked(), self._old_rule())

    def test_two_demoted_candidates_keep_their_relative_order(self) -> None:
        self._save("a", 92.0)
        self._save("b", 93.0)
        self.assertEqual(self._ranked(), self._old_rule())

    def test_the_tier_boundary_does_not_reorder(self) -> None:
        """Either side of `100 - MIN_HEADROOM_7D`, where the tier flips."""
        edge = 100.0 - cc.MIN_HEADROOM_7D
        self._save("just_under", edge - 0.1)
        self._save("at_edge", edge)
        self._save("just_over", edge + 0.1)
        self.assertEqual(self._ranked(), ["just_under", "at_edge", "just_over"])
        self.assertEqual(self._ranked(), self._old_rule())

    def test_the_five_hour_tiebreak_survives(self) -> None:
        self._save("busy", 40.0, five=80.0)
        self._save("idle", 40.0, five=1.0)
        self.assertEqual(self._ranked(), ["idle", "busy"])
        self.assertEqual(self._ranked(), self._old_rule())

    def test_deadlines_do_not_reorder_anything(self) -> None:
        """The whole point of ALPHA 0: the window leaves the comparison."""
        self._save("spent_but_urgent", 92.0, reset=NOW + 0.25 * HOUR)
        self._save("roomy_but_distant", 50.0, reset=NOW + 6 * DAY)
        self.assertEqual(self._ranked(), ["roomy_but_distant", "spent_but_urgent"])
        self.assertEqual(self._ranked(), self._old_rule())

    def test_alpha_one_does_reorder_the_same_board(self) -> None:
        """Proves the tests above pin ALPHA 0 and not a board that happens to
        sort the same way whatever the exponent.

        The deadline is two hours rather than minutes: inside
        MIN_USEFUL_TTR_HOURS the account is demoted for being about to reset,
        which would hide the reordering this is checking for.
        """
        self._save("spent_but_urgent", 92.0, reset=NOW + 2 * HOUR)
        self._save("roomy_but_distant", 50.0, reset=NOW + 6 * DAY)
        self.assertEqual(self._ranked(), ["roomy_but_distant", "spent_but_urgent"])
        with patch.object(cc, "URGENCY_ALPHA", 1.0), patch.object(cc, "MIN_HEADROOM_7D", 0.0):
            self.assertEqual([c.name for c in cc.rank_candidates(NOW, None)],
                             ["spent_but_urgent", "roomy_but_distant"])

    def test_the_equivalent_is_the_candidates_own_percentage(self) -> None:
        """Which is what makes switch_reason, the gate and pick reduce too."""
        with patch.object(cc, "URGENCY_ALPHA", 0.0):
            for seven, ttr_c, ttr_a in ((34.0, 12.0, 168.0), (0.0, 0.5, 3.0), (94.0, 168.0, 0.5)):
                cand = cc.Candidate("c", 0.0, seven, ttr_c, cc.weekly_pressure(seven, ttr_c))
                self.assertAlmostEqual(cc.candidate_equivalent_7d(cand, ttr_a), seven)


class TestUrgencyKnobValidation(unittest.TestCase):
    """The exponent is not a percentage, so `_env_float`'s bound is wrong."""

    def _reject(self, value: str) -> tuple[int, str]:
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"CC_SWITCH_TEST_ALPHA": value}),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as caught,
        ):
            cc._env_alpha("CC_SWITCH_TEST_ALPHA", 1.0)
        return int(caught.exception.code or 0), err.getvalue()

    def test_nan_is_rejected(self) -> None:
        code, message = self._reject("nan")
        self.assertEqual(code, cc.EXIT_USER)
        self.assertIn("finite", message)

    def test_non_numeric_is_rejected(self) -> None:
        code, message = self._reject("soon")
        self.assertEqual(code, cc.EXIT_USER)
        self.assertIn("expected a number", message)

    def test_negative_is_rejected(self) -> None:
        """A negative exponent would rank the *least* urgent account first."""
        self.assertEqual(self._reject("-1")[0], cc.EXIT_USER)

    def test_an_exponent_that_would_overflow_is_rejected(self) -> None:
        code, message = self._reject("200")
        self.assertEqual(code, cc.EXIT_USER)
        self.assertIn("0 and 10", message)

    def test_zero_is_allowed(self) -> None:
        with patch.dict(os.environ, {"CC_SWITCH_TEST_ALPHA": "0"}):
            self.assertEqual(cc._env_alpha("CC_SWITCH_TEST_ALPHA", 1.0), 0.0)

    def test_the_upper_bound_is_inclusive(self) -> None:
        with patch.dict(os.environ, {"CC_SWITCH_TEST_ALPHA": "10"}):
            self.assertEqual(cc._env_alpha("CC_SWITCH_TEST_ALPHA", 1.0), 10.0)

    def test_the_default_is_used_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CC_SWITCH_TEST_ALPHA", None)
            self.assertEqual(cc._env_alpha("CC_SWITCH_TEST_ALPHA", 1.0), 1.0)

    def test_the_bound_keeps_the_arithmetic_finite(self) -> None:
        """The binding term is the window raised to the exponent."""
        self.assertTrue(math.isfinite(cc.WINDOW_HOURS**cc.MAX_URGENCY_ALPHA))
        self.assertTrue(math.isfinite((cc.WINDOW_HOURS / cc.MIN_TTR_HOURS) ** cc.MAX_URGENCY_ALPHA))

    def test_a_zero_poll_turns_polling_off(self) -> None:
        """`_env_seconds` permits 0, and 0 here would mean a process per render."""
        with patch.dict(os.environ, {"CC_SWITCH_TEST_POLL": "0"}):
            self.assertEqual(cc._env_poll_seconds("CC_SWITCH_TEST_POLL", 900.0), 0.0)

    def test_a_short_poll_is_floored(self) -> None:
        """Faster than the settle window cannot change any answer."""
        with patch.dict(os.environ, {"CC_SWITCH_TEST_POLL": "5"}):
            self.assertEqual(cc._env_poll_seconds("CC_SWITCH_TEST_POLL", 900.0), cc.MIN_CROSSOVER_POLL_SECONDS)

    def test_a_long_poll_is_kept(self) -> None:
        with patch.dict(os.environ, {"CC_SWITCH_TEST_POLL": "1800"}):
            self.assertEqual(cc._env_poll_seconds("CC_SWITCH_TEST_POLL", 900.0), 1800.0)


class TestTimestampArgToIso(unittest.TestCase):
    """The two producers of a reset deadline disagree about its shape.

    The OAuth usage endpoint returns an ISO 8601 string. The Claude Code
    statusline payload documents `resets_at` as a *number* — Unix epoch
    seconds — and argparse hands every value over as text. Converting once
    here keeps one shape on disk, so every reader below stays single-format.
    """

    def test_epoch_seconds_become_iso(self) -> None:
        self.assertEqual(cc.parse_iso(cc.timestamp_arg_to_iso("1787803200")), 1787803200.0)

    def test_a_fractional_epoch_is_accepted(self) -> None:
        """The payload says number, not integer."""
        self.assertEqual(cc.parse_iso(cc.timestamp_arg_to_iso("1787803200.5")), 1787803200.5)

    def test_an_iso_string_passes_through_unchanged(self) -> None:
        self.assertEqual(cc.timestamp_arg_to_iso("2026-08-27T04:00:00Z"), "2026-08-27T04:00:00Z")

    def test_a_date_is_read_as_a_date_not_an_epoch(self) -> None:
        """`float("20260827")` is August 1970 and would zero a live window."""
        self.assertGreater(cc.parse_iso(cc.timestamp_arg_to_iso("20260827")), 1_600_000_000.0)

    def test_the_literal_null_is_rejected(self) -> None:
        """New input shape: the shell forwards `null` rather than an empty
        string now that it reads unquoted values too."""
        self.assertIsNone(cc.timestamp_arg_to_iso("null"))

    def test_none_is_rejected(self) -> None:
        self.assertIsNone(cc.timestamp_arg_to_iso(None))

    def test_an_empty_string_is_rejected(self) -> None:
        self.assertIsNone(cc.timestamp_arg_to_iso(""))

    def test_garbage_is_rejected(self) -> None:
        self.assertIsNone(cc.timestamp_arg_to_iso("not-a-date"))

    def test_nan_is_rejected(self) -> None:
        """`float()` accepts it and every later comparison would be false."""
        self.assertIsNone(cc.timestamp_arg_to_iso("nan"))

    def test_infinity_is_rejected(self) -> None:
        self.assertIsNone(cc.timestamp_arg_to_iso("inf"))

    def test_an_absurd_epoch_is_rejected(self) -> None:
        """1e300 passes every numeric check and then raises in fromtimestamp."""
        self.assertIsNone(cc.timestamp_arg_to_iso("1e300"))

    def test_a_negative_epoch_is_rejected(self) -> None:
        self.assertIsNone(cc.timestamp_arg_to_iso("-1"))

    def test_zero_is_rejected(self) -> None:
        """0 is how every other epoch field in this file spells 'no deadline'."""
        self.assertIsNone(cc.timestamp_arg_to_iso("0"))


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


class TestRankingWeighsDeadlines(AutoBaseTest):
    """The change itself: which account the ranking puts first."""

    def _pair(self, a_seven: float, a_reset: float | None, b_seven: float, b_reset: float | None) -> list[str]:
        self._save_with_usage("urgent", _creds("a"), _account("a@x"), 0.0, a_seven, None,
                              _iso(a_reset) if a_reset else None)
        self._save_with_usage("roomy", _creds("b"), _account("b@x"), 0.0, b_seven, None,
                              _iso(b_reset) if b_reset else None)
        return [c.name for c in cc.rank_candidates(NOW, None)]

    def test_an_earlier_reset_outranks_lower_usage(self) -> None:
        """25pp about to be destroyed beats 80pp that is safe for six days."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertEqual(self._pair(75.0, NOW + 12 * HOUR, 20.0, NOW + 6 * DAY), ["urgent", "roomy"])

    def test_alpha_zero_restores_lowest_weekly_first(self) -> None:
        with patch.object(cc, "URGENCY_ALPHA", 0.0):
            self.assertEqual(self._pair(75.0, NOW + 12 * HOUR, 20.0, NOW + 6 * DAY), ["roomy", "urgent"])

    def test_equal_deadlines_fall_back_to_lowest_weekly(self) -> None:
        """With one deadline on both sides the rule is what it always was."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertEqual(self._pair(75.0, NOW + 6 * DAY, 20.0, NOW + 6 * DAY), ["roomy", "urgent"])

    def test_unknown_deadlines_fall_back_to_lowest_weekly(self) -> None:
        """The shape most of this suite stores."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertEqual(self._pair(75.0, None, 20.0, None), ["roomy", "urgent"])

    def test_equal_pressure_prefers_the_larger_budget(self) -> None:
        """50pp over half a week and 100pp over a week burn at the same rate."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertEqual(self._pair(50.0, NOW + 84 * HOUR, 0.0, NOW + 168 * HOUR), ["roomy", "urgent"])

    def test_the_candidate_carries_its_deadline(self) -> None:
        self._save_with_usage("a", _creds("a"), _account("a@x"), 0.0, 40.0, None, _iso(NOW + 24 * HOUR))
        self.assertAlmostEqual(cc.rank_candidates(NOW, None)[0].ttr_hours, 24.0)

    def test_an_account_about_to_reset_is_demoted_not_dropped(self) -> None:
        """It has the highest burn rate on the board and would head the list,
        so a forced evacuation would take it and hit the exit threshold again
        minutes later. Demoted, because an evacuation must still land."""
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            order = self._pair(93.0, NOW + 300, 20.0, NOW + 6 * DAY)
        self.assertEqual(order, ["roomy", "urgent"])

    def test_a_demoted_account_is_still_available_alone(self) -> None:
        self._save_with_usage("doomed", _creds("a"), _account("a@x"), 0.0, 93.0, None, _iso(NOW + 300))
        with patch.object(cc, "URGENCY_ALPHA", 1.0):
            self.assertEqual([c.name for c in cc.rank_candidates(NOW, None)], ["doomed"])


class TestCandidate(unittest.TestCase):
    """The ranking result is a named tuple — the field order is the contract."""

    def test_fields_are_named_and_ordered(self) -> None:
        candidate = cc.Candidate("vlad", 12.0, 34.0, 48.0, 1.375)
        self.assertEqual(candidate.name, "vlad")
        self.assertEqual(candidate.five_hour, 12.0)
        self.assertEqual(candidate.seven_day, 34.0)
        self.assertEqual(candidate.ttr_hours, 48.0)
        self.assertEqual(candidate.pressure, 1.375)
        self.assertEqual(tuple(candidate), ("vlad", 12.0, 34.0, 48.0, 1.375))

    def test_score_fields_line_up_with_candidate_fields(self) -> None:
        """`scored_candidate` copies them across positionally."""
        self.assertEqual(cc.Candidate._fields[1:], cc.Score._fields)

    def test_headroom_is_what_is_left_of_the_week(self) -> None:
        self.assertEqual(cc.Candidate("a", 0.0, 34.0, 48.0, 1.375).headroom, 66.0)

    def test_sorts_by_pressure_descending(self) -> None:
        """25pp about to be destroyed outranks 80pp that is safe for six days."""
        urgent = cc.Candidate("a", 90.0, 75.0, 12.0, 25.0 / 12.0)
        roomy = cc.Candidate("b", 1.0, 20.0, 144.0, 80.0 / 144.0)
        self.assertEqual(sorted([roomy, urgent], key=lambda c: -c.pressure)[0], urgent)


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

    def test_line_is_six_integers(self) -> None:
        """The statusline parses this from bash — the shape is the contract."""
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        cc.recompute_gate("me", NOW)
        self.assertEqual(len(self._gate()), 6)

    def test_the_sixth_field_is_the_settle_window_alone(self) -> None:
        """`not_before` also carries the exhaustion deadline; this must not."""
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        cc.write_epoch_file(self.settle_file, NOW + 60)
        cc.write_epoch_file(self.exhausted_file, NOW + DAY)
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[0], int(NOW + DAY))
        self.assertEqual(self._gate()[5], int(NOW + 60))

    def test_a_fractional_threshold_never_triggers_below_itself(self) -> None:
        """A trigger under the threshold is a tick per render that declines.

        The statusline compares tenths, so `EXIT_5H=95.15` cannot be one. It
        has to land on 95.2: at 95.1 the shell would wake a tick, Python
        would decline, and the same trigger would be written back.
        """
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        with patch.object(cc, "EXIT_5H", 95.15):
            cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[2], 952)

    def test_a_threshold_halfway_between_tenths_still_rounds_up(self) -> None:
        """Nearest-half would send `95.05` down to 950 — banker's, to even.

        The shell would then wake a tick at 95.0%, Python would decline
        because 95.0 is under the threshold, and the same trigger would be
        written back: a process per render for the whole tenth in between.
        """
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        with patch.object(cc, "EXIT_5H", 95.05):
            cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[2], 951)

    def test_a_tenth_threshold_lands_exactly_on_itself(self) -> None:
        """`95.1 * 10` is 950.9999999999999 — a bare ceiling is not enough."""
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        with patch.object(cc, "EXIT_5H", 95.1):
            cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[2], 951)

    def test_a_sum_landing_just_above_a_tenth_is_not_pushed_past_it(self) -> None:
        """`16.1 + 0.1` is 16.200000000000003, and a bare ceiling reads 163.

        One decimal times ten is exact in binary; a sum of two is not, and
        `trigger_7d` is exactly such a sum. Overshooting delays the tick that
        would have balanced the accounts by a whole tenth of a percent.
        """
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 16.1)
        with patch.object(cc, "BALANCE_GAP_7D", 0.1):
            cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], 162)

    def test_the_balance_trigger_is_rounded_the_same_way_as_the_limit(self) -> None:
        """Its precision comes from the API, which nothing here validates."""
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        with patch.object(cc, "BALANCE_GAP_7D", 5.15):
            cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], 252)

    def test_trigger_uses_the_balance_gap(self) -> None:
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], int((20 + cc.BALANCE_GAP_7D) * 10))

    def test_trigger_capped_by_exit_threshold(self) -> None:
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, cc.ENTER_7D)
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], int(cc.EXIT_7D * 10))

    def test_trigger_is_exit_threshold_without_candidates(self) -> None:
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[2:4], [int(cc.EXIT_5H * 10), int(cc.EXIT_7D * 10)])

    def test_recheck_after_is_earliest_reset(self) -> None:
        """The poll is switched off here so this pins one thing: a reset is a
        deadline the gate has to wake for. Its own tests are below."""
        self._save_with_usage("a", _creds("a"), _account("a@x"), 1.0, 1.0, _iso(NOW + 2 * HOUR), _iso(NOW + DAY))
        self._save_with_usage("b", _creds("b"), _account("b@x"), 1.0, 1.0, _iso(NOW + HOUR), _iso(NOW + DAY))
        with patch.object(cc, "CROSSOVER_POLL_SECONDS", 0.0):
            cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + HOUR))

    def test_recheck_ignores_past_resets(self) -> None:
        self._save_with_usage("a", _creds("a"), _account("a@x"), 1.0, 1.0, _iso(NOW - HOUR), _iso(NOW + DAY))
        with patch.object(cc, "CROSSOVER_POLL_SECONDS", 0.0):
            cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + DAY))

    def test_recheck_zero_when_no_reset_is_known(self) -> None:
        """resets_at is null for an untouched window, so 'never' must be expressible."""
        self._save_with_usage("a", _creds("a"), _account("a@x"), 0.0, 0.0, None, None)
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], 0)

    def test_recheck_includes_the_active_profile(self) -> None:
        """Its own reset used to be irrelevant and now is not.

        Under "lowest weekly usage wins" the active account's reset only
        lowered its own figure, and low usage was never a reason to leave.
        Under a burn rate it drops the account from "25pp in twelve hours" to
        "100pp in a week", which can hand a candidate the lead — and nothing
        else would wake a tick, because usage *falls* at a reset, so no
        percentage trigger can fire.
        """
        self._save_with_usage("me", _creds("a"), _account("a@x"), 1.0, 1.0, _iso(NOW + HOUR), _iso(NOW + HOUR))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + HOUR))

    def test_an_earlier_candidate_reset_arms_the_crossover_poll(self) -> None:
        """Pressure grows as one over the hours left, so an account whose
        window closes sooner gains it faster and overtakes with nothing else
        moving. The reset boundary is days away and usage need not change, so
        neither of the gate's other wake-ups can catch it."""
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 90.0, None, _iso(NOW + 6 * DAY))
        self._save_with_usage("soon", _creds("a"), _account("a@x"), 0.0, 60.0, None, _iso(NOW + 3 * DAY))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + cc.CROSSOVER_POLL_SECONDS))

    def test_a_later_candidate_reset_does_not_arm_the_poll(self) -> None:
        """Provably unnecessary: the trigger's drift has the sign of
        (candidate ttr - active ttr), so here it drifts the safe way."""
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 90.0, None, _iso(NOW + 2 * DAY))
        self._save_with_usage("later", _creds("a"), _account("a@x"), 0.0, 60.0, None, _iso(NOW + 6 * DAY))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + 2 * DAY))

    def test_alpha_zero_never_polls(self) -> None:
        """Pressure does not depend on time there, so the hole does not exist
        and closing it must cost nothing."""
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 90.0, None, _iso(NOW + 6 * DAY))
        self._save_with_usage("soon", _creds("a"), _account("a@x"), 0.0, 60.0, None, _iso(NOW + 3 * DAY))
        with patch.object(cc, "URGENCY_ALPHA", 0.0):
            cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + 3 * DAY))

    def test_a_nearer_reset_wins_over_the_poll(self) -> None:
        """The poll is a ceiling on lateness, not a replacement deadline."""
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 90.0, None, _iso(NOW + 6 * DAY))
        self._save_with_usage("soon", _creds("a"), _account("a@x"), 0.0, 60.0, None, _iso(NOW + 300))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + 300))

    def test_the_poll_can_be_turned_off(self) -> None:
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 90.0, None, _iso(NOW + 6 * DAY))
        self._save_with_usage("soon", _creds("a"), _account("a@x"), 0.0, 60.0, None, None)
        with patch.object(cc, "CROSSOVER_POLL_SECONDS", 0.0):
            cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + 6 * DAY))

    def test_the_poll_is_armed_even_with_no_reset_known_anywhere(self) -> None:
        """`earliest_future_reset` returns 0 for 'never'; the poll still needs
        to be expressible on top of that."""
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 90.0, None, None)
        self._save_with_usage("soon", _creds("a"), _account("a@x"), 0.0, 60.0, None, _iso(NOW + 3 * DAY))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[1], int(NOW + cc.CROSSOVER_POLL_SECONDS))

    def test_the_trigger_is_unchanged_when_both_windows_are_unknown(self) -> None:
        """Null resets on both sides: the inversion has to be a no-op."""
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], int((20 + cc.BALANCE_GAP_7D) * 10))

    def test_an_urgent_active_account_raises_the_trigger(self) -> None:
        """75% resetting in twelve hours must not be abandoned for a safe 20%,
        so the wake-up point has to sit above where we already are.

        The candidate's 80pp over six days is worth 93.3% on this account's
        twelve-hour scale, so the trigger lands at 98.3% — 23pp above the 75%
        we are at, where the old rule would have woken a tick immediately.
        """
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 75.0, None, _iso(NOW + 12 * HOUR))
        self._save_with_usage("roomy", _creds("a"), _account("a@x"), 0.0, 20.0, None, _iso(NOW + 6 * DAY))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], 984)
        self.assertGreater(self._gate()[3], 750)

    def test_an_urgent_candidate_lowers_the_trigger_to_zero(self) -> None:
        """A candidate that beats us from any usage at all: wake immediately,
        and do not write a negative number the shell cannot compare."""
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 10.0, None, _iso(NOW + 6 * DAY))
        self._save_with_usage("urgent", _creds("a"), _account("a@x"), 0.0, 40.0, None, _iso(NOW + 2 * HOUR))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], 0)

    def test_alpha_zero_reproduces_the_old_trigger_exactly(self) -> None:
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 75.0, None, _iso(NOW + 12 * HOUR))
        self._save_with_usage("roomy", _creds("a"), _account("a@x"), 0.0, 20.0, None, _iso(NOW + 6 * DAY))
        with patch.object(cc, "URGENCY_ALPHA", 0.0):
            cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], int((20 + cc.BALANCE_GAP_7D) * 10))

    def test_a_candidate_about_to_reset_does_not_move_the_trigger(self) -> None:
        """It gets no urgency credit, so it is worth its plain 40% here."""
        self._save_with_usage("me", _creds("m"), _account("m@x"), 0.0, 10.0, None, None)
        self._save_with_usage("doomed", _creds("a"), _account("a@x"), 0.0, 40.0, None, _iso(NOW + 300))
        cc.recompute_gate("me", NOW)
        self.assertEqual(self._gate()[3], int((40 + cc.BALANCE_GAP_7D) * 10))

    def test_a_nan_utilization_does_not_crash_the_gate(self) -> None:
        """A NaN reaching `math.ceil` is a crashing tick on every render, and
        `max(0.0, nan)` would otherwise write a trigger of zero — which the
        statusline satisfies on every render, spawning one either way.

        It reads as 0% instead, the same as any other unusable utilization has
        always read here, so the trigger is an ordinary number and the live
        fetch on the next tick replaces the damaged snapshot.
        """
        self._save_with_usage("other", _creds("a"), _account("a@x"), 10.0, 20.0)
        path = self._profile_path("other")
        data = json.loads(path.read_text())
        data["usage"]["seven_day"]["utilization"] = float("nan")
        path.write_text(json.dumps(data))
        cc.recompute_gate("me", NOW)
        self.assertEqual(len(self._gate()), 6)
        self.assertEqual(self._gate()[3], int(cc.BALANCE_GAP_7D * 10))
        self.assertGreater(self._gate()[3], 0)

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

    def test_status_reports_the_urgency_exponent(self) -> None:
        """Under a burn-rate rule the thresholds alone do not explain a switch."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_auto(argparse.Namespace(action="status"))
        self.assertIn("urgency exponent", out.getvalue())
        self.assertIn("headroom per hour", out.getvalue())

    def test_status_names_the_old_rule_at_alpha_zero(self) -> None:
        out = io.StringIO()
        with patch.object(cc, "URGENCY_ALPHA", 0.0), contextlib.redirect_stdout(out):
            cc.cmd_auto(argparse.Namespace(action="status"))
        self.assertIn("lowest weekly usage", out.getvalue())

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


class TestTickStoresTheActiveDeadline(AutoBaseTest):
    """The active account's own weekly deadline, end to end through cmd_tick.

    The statusline sends epoch seconds and the extractor only matched quoted
    strings, so this field was null on every snapshot the tick ever wrote —
    for the one account whose numbers actually drive a decision. Every other
    profile got a real deadline from the API, so nothing looked wrong.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 0.0, 0.0)
        cc.write_active("me")
        self._enable_auto()

    def test_epoch_seconds_are_stored_as_a_real_deadline(self) -> None:
        cc.cmd_tick(self._tick_args(1.0, 2.0, str(int(NOW + HOUR)), str(int(NOW + DAY))))
        stored = self._stored_usage("me")
        self.assertEqual(cc.parse_iso(stored["seven_day"]["resets_at"]), NOW + DAY)
        self.assertEqual(cc.parse_iso(stored["five_hour"]["resets_at"]), NOW + HOUR)

    def test_an_iso_deadline_is_stored_too(self) -> None:
        """The API's shape must keep working through the same path."""
        cc.cmd_tick(self._tick_args(1.0, 2.0, None, _iso(NOW + DAY)))
        self.assertEqual(cc.parse_iso(self._stored_usage("me")["seven_day"]["resets_at"]), NOW + DAY)

    def test_a_null_deadline_stores_nothing(self) -> None:
        """The shell forwards the literal `null` now that it reads numbers."""
        cc.cmd_tick(self._tick_args(1.0, 2.0, "null", "null"))
        self.assertIsNone(self._stored_usage("me")["seven_day"]["resets_at"])

    def test_the_stored_deadline_reaches_the_exhaustion_check(self) -> None:
        """`.exhausted` scans every profile, the active one included, and
        could never see its windows while this field was null."""
        cc.cmd_tick(self._tick_args(1.0, 2.0, None, str(int(NOW + DAY))))
        self.assertEqual(cc.earliest_future_reset(["me"], NOW), NOW + DAY)

    def test_a_rolled_over_weekly_window_now_zeroes_itself(self) -> None:
        """`_window_value` zeroes a window past its reset. With the deadline
        missing it never could, so a stale weekly figure kept the active
        account looking spent long after its window had rolled over."""
        cc.cmd_tick(self._tick_args(1.0, 90.0, None, str(int(NOW + HOUR))))
        stored = self._stored_usage("me")
        self.assertEqual(cc.effective_usage(stored, NOW)[1], 90.0)
        self.assertEqual(cc.effective_usage(stored, NOW + 2 * HOUR)[1], 0.0)


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


class TestTickWeighsDeadlines(TickTestCase):
    """The change, end to end through cmd_tick."""

    def test_it_stays_on_an_urgent_account_a_roomy_one_would_have_won(self) -> None:
        """The reported failure. Active 75% resetting in twelve hours against
        a candidate at 20% resetting in six days: the old rule moved and
        destroyed 25pp, this one stays and burns them."""
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"),
                              0.0, 20.0, None, _iso(NOW + 6 * DAY))
        with self._patch_usage(0.0, 20.0, None, _iso(NOW + 6 * DAY)):
            cc.cmd_tick(self._tick_args(0.0, 75.0, None, str(int(NOW + 12 * HOUR))))
        self.assertEqual(cc.read_active(), "me")

    def test_alpha_zero_still_moves_to_the_lower_account(self) -> None:
        """The same inputs under the old rule, to show the difference is the
        exponent and nothing else."""
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"),
                              0.0, 20.0, None, _iso(NOW + 6 * DAY))
        with patch.object(cc, "URGENCY_ALPHA", 0.0), self._patch_usage(0.0, 20.0, None, _iso(NOW + 6 * DAY)):
            cc.cmd_tick(self._tick_args(0.0, 75.0, None, str(int(NOW + 12 * HOUR))))
        self.assertEqual(cc.read_active(), "vlad")

    def test_it_moves_to_an_urgent_account_from_a_roomy_one(self) -> None:
        """The mirror image: the 25pp about to expire are the ones to spend."""
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"),
                              0.0, 75.0, None, _iso(NOW + 12 * HOUR))
        with self._patch_usage(0.0, 75.0, None, _iso(NOW + 12 * HOUR)):
            cc.cmd_tick(self._tick_args(0.0, 20.0, None, str(int(NOW + 6 * DAY))))
        self.assertEqual(cc.read_active(), "vlad")
        self.assertIn("(balance)", self._log_text())

    def test_equal_deadlines_behave_exactly_as_before(self) -> None:
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"),
                              0.0, 20.0, None, _iso(NOW + 6 * DAY))
        with self._patch_usage(0.0, 20.0, None, _iso(NOW + 6 * DAY)):
            cc.cmd_tick(self._tick_args(0.0, 75.0, None, str(int(NOW + 6 * DAY))))
        self.assertEqual(cc.read_active(), "vlad")

    def test_an_account_about_to_reset_is_not_a_balance_reason(self) -> None:
        """Two token rotations for headroom that cannot be spent."""
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"),
                              0.0, 90.0, None, _iso(NOW + 300))
        with self._patch_usage(0.0, 90.0, None, _iso(NOW + 300)):
            cc.cmd_tick(self._tick_args(0.0, 20.0, None, str(int(NOW + 6 * DAY))))
        self.assertEqual(cc.read_active(), "me")

    def test_an_account_about_to_reset_still_rescues_a_limit(self) -> None:
        """Any account beats none, and it resets into a full week shortly."""
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"),
                              0.0, 10.0, None, _iso(NOW + 300))
        with self._patch_usage(0.0, 10.0, None, _iso(NOW + 300)):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 20.0, None, str(int(NOW + 6 * DAY))))
        self.assertEqual(cc.read_active(), "vlad")

    def test_the_live_recheck_uses_the_live_deadline(self) -> None:
        """The stored snapshot says the candidate resets soon and is worth
        moving to; the live fetch says its window rolled over into a fresh
        week. Weighing the fresh percentage against the stale deadline would
        switch on a reason that no longer exists."""
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"),
                              0.0, 60.0, None, _iso(NOW + 2 * HOUR))
        with self._patch_usage(0.0, 60.0, None, _iso(NOW + 6 * DAY)):
            cc.cmd_tick(self._tick_args(0.0, 20.0, None, str(int(NOW + 6 * DAY))))
        self.assertEqual(cc.read_active(), "me")
        self.assertIn("no reason to move", self._log_text())

    def test_an_unreachable_candidate_backs_off_on_a_balance_reason_too(self) -> None:
        """Nothing was recorded, so the identical trigger is written back and
        the next render fires again. The retry pause is the only bound, and it
        used to be armed for limits alone — survivable while the balance
        trigger sat a gap above zero, and not since it can be inverted to it.
        """
        with patch.object(cc, "fetch_usage", return_value=(0, {"error": {"message": "boom"}})):
            cc.cmd_tick(self._tick_args(10.0, 40.0))
        self.assertEqual(cc.read_active(), "me")
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.RETRY_SECONDS))
        self.assertFalse(self.exhausted_file.exists())

    def test_a_reachable_candidate_that_declines_does_not_back_off(self) -> None:
        """That path recorded a fresh snapshot, so the next gate is different
        and it converges without a pause."""
        with self._patch_usage(5.0, 39.0):
            cc.cmd_tick(self._tick_args(10.0, 40.0))
        self.assertEqual(cc.read_active(), "me")
        self.assertFalse(self.settle_file.exists())


class TestTickAtANonUnitExponent(TickTestCase):
    """The exponent has to reach the live decision, not just the scorer."""

    def test_alpha_two_moves_where_alpha_one_stays(self) -> None:
        """Active 15% resetting in 96h, candidate 60% resetting in 48h. At
        alpha 1 the active account is the more urgent of the two and we stay;
        at alpha 2 the nearer deadline wins and we move."""
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"),
                              0.0, 60.0, None, _iso(NOW + 48 * HOUR))
        args = self._tick_args(0.0, 15.0, None, str(int(NOW + 96 * HOUR)))
        with patch.object(cc, "URGENCY_ALPHA", 1.0), self._patch_usage(0.0, 60.0, None, _iso(NOW + 48 * HOUR)):
            cc.cmd_tick(args)
        self.assertEqual(cc.read_active(), "me")
        with patch.object(cc, "URGENCY_ALPHA", 2.0), self._patch_usage(0.0, 60.0, None, _iso(NOW + 48 * HOUR)):
            cc.cmd_tick(self._tick_args(0.0, 15.0, None, str(int(NOW + 96 * HOUR))))
        self.assertEqual(cc.read_active(), "vlad")


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

    def test_the_table_carries_the_burn_column(self) -> None:
        """The rate is what every decision now turns on, so `usage` has to
        show it. `_burn_cell` passing in isolation proves nothing if the
        column is never wired into the header or the rows."""
        out = io.StringIO()
        with (
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(0.0, 75.0, None, _iso(NOW + 12 * HOUR)))),
            patch.object(cc, "URGENCY_ALPHA", 1.0),
            contextlib.redirect_stdout(out),
        ):
            cc.cmd_usage(argparse.Namespace(json=False))
        text = out.getvalue()
        self.assertIn("BURN", text.splitlines()[0])
        # 25pp of headroom over twelve hours.
        self.assertIn("2.08", text)
        self.assertIn("percentage points per hour", text)

    def test_the_burn_column_follows_the_exponent(self) -> None:
        """At alpha 0 the rate is plain headroom, so the same board prints a
        different number — the column reads the live knob, not a constant."""
        out = io.StringIO()
        with (
            patch.object(cc, "fetch_usage", return_value=(200, _api_usage(0.0, 75.0, None, _iso(NOW + 12 * HOUR)))),
            patch.object(cc, "URGENCY_ALPHA", 0.0),
            contextlib.redirect_stdout(out),
        ):
            cc.cmd_usage(argparse.Namespace(json=False))
        self.assertIn("25.00", out.getvalue())

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
        self.assertIn("6h", _note("soon"))

    def test_no_warning_just_past_the_window(self) -> None:
        self._save_with_usage("fine", _creds("t", refresh_expires=int((NOW + 3 * DAY) * 1000)), _account("f@x"))
        self.assertEqual(_note("fine"), "")



def _note(name: str, active: str | None = None, now: float = NOW) -> str:
    """The `list` note for one account, from the verdict its date comes from."""
    reason, expiry = cc.login_status_for(name, active, now)
    return cc._login_note(reason, expiry, now)


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
        self.assertEqual(_note("edge"), "")

    def test_login_warning_just_under_two_days_fires(self) -> None:
        self._save_with_usage("edge", _creds("t", refresh_expires=int((NOW + 2 * DAY - HOUR) * 1000)), _account("e@x"))
        self.assertIn("login expires in", _note("edge"))

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
        """The gate writes these through `_gate_tenths` on every recompute."""
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


class TestPickRescuesADeadLoginToo(AutoBaseTest):
    """`pick` runs the same decision as the tick and needs the same rule.

    Fixing the automatic rescue alone left the manual command refusing to
    look at the profile the marker names — the one place a user reaches for
    when the session has stopped working.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 60.0)
        cc.write_active("me")

    def test_the_marker_profile_is_restored_when_it_is_all_there_is(self) -> None:
        self.creds_file.unlink()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(10.0, 60.0))), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "me")
        live = json.loads(self.creds_file.read_text())
        self.assertEqual(live["claudeAiOauth"]["accessToken"], "token-me")

    def test_it_is_confirmed_with_its_own_stored_token(self) -> None:
        """There is no live token to confirm with — that is the whole problem."""
        self.creds_file.unlink()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(10.0, 60.0))) as fetch, _silence():
            cc.cmd_pick(argparse.Namespace())
        fetch.assert_called_once_with("token-me")

    def test_a_lower_account_still_wins_over_the_marker(self) -> None:
        self.creds_file.unlink()
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 5.0, 20.0)

        def _usage(token: str) -> tuple[int, dict]:
            return (200, _api_usage(10.0, 60.0)) if token == "token-me" else (200, _api_usage(5.0, 20.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "vlad")

    def test_a_healthy_login_still_excludes_the_active_account(self) -> None:
        """The rule is for a dead login only; otherwise nothing changes."""
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 5.0, 20.0)

        def _usage(token: str) -> tuple[int, dict]:
            return (200, _api_usage(10.0, 10.0)) if token == "token-me" else (200, _api_usage(5.0, 20.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "me")

    def test_nothing_usable_still_says_so(self) -> None:
        self.creds_file.unlink()
        self._save_with_usage(
            "me", _creds("token-me", refresh_expires=int((NOW - DAY) * 1000)), _account("me@example.com")
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_pick(argparse.Namespace())
        self.assertIn("No usable account with headroom", out.getvalue())


class TestPickWeighsDeadlines(AutoBaseTest):
    """`pick` and the tick must answer the same question the same way.

    Two decision sites that disagree is the failure this shares a scorer to
    avoid: auto moves to the urgent account and the next `pick` hands it
    straight back.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 0.0, 75.0)
        self._save_with_usage("urgent", _creds("token-urgent"), _account("u@x"), 0.0, 60.0)
        self._save_with_usage("roomy", _creds("token-roomy"), _account("r@x"), 0.0, 20.0)
        cc.write_active("me")

    def _usage(self, me_r7: float, urgent_r7: float, roomy_r7: float) -> object:
        table = {
            "token-me": (200, _api_usage(0.0, 75.0, None, _iso(me_r7))),
            "token-urgent": (200, _api_usage(0.0, 60.0, None, _iso(urgent_r7))),
            "token-roomy": (200, _api_usage(0.0, 20.0, None, _iso(roomy_r7))),
        }
        return patch.object(cc, "fetch_usage", side_effect=lambda token: table[token])

    def test_it_takes_the_most_urgent_not_the_lowest(self) -> None:
        """60% resetting in six hours outranks 20% resetting in six days."""
        with self._usage(NOW + 6 * DAY, NOW + 6 * HOUR, NOW + 6 * DAY), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "urgent")

    def test_alpha_zero_takes_the_lowest_weekly_account(self) -> None:
        with patch.object(cc, "URGENCY_ALPHA", 0.0), self._usage(NOW + 6 * DAY, NOW + 6 * HOUR, NOW + 6 * DAY), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "roomy")

    def test_equal_deadlines_take_the_lowest_weekly_account(self) -> None:
        with self._usage(NOW + 6 * DAY, NOW + 6 * DAY, NOW + 6 * DAY), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "roomy")

    def test_it_stays_on_a_more_urgent_active_account(self) -> None:
        with self._usage(NOW + 6 * HOUR, NOW + 6 * DAY, NOW + 6 * DAY), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "me")

    def test_the_exponent_reaches_pick_too(self) -> None:
        """`pick` shares the scorer, so it has to move with the knob."""
        with patch.object(cc, "URGENCY_ALPHA", 2.0), self._usage(NOW + 6 * DAY, NOW + 6 * HOUR, NOW + 6 * DAY), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "urgent")

    def test_it_still_escapes_a_nearly_spent_active_account(self) -> None:
        """The churn guard must not leak into the escape path: sitting on 99%
        with 94% available and refusing to move is not a defensible answer.
        """
        table = {
            "token-me": (200, _api_usage(10.0, 99.0)),
            "token-urgent": (200, _api_usage(5.0, cc.ENTER_7D)),
            "token-roomy": (200, _api_usage(5.0, cc.ENTER_7D)),
        }
        with patch.object(cc, "fetch_usage", side_effect=lambda t: table[t]), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(cc.read_active(), "roomy")


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
        self.assertIn("nothing better available", out.getvalue())

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


class TestADeadLoginBelongsToNobody(AutoBaseTest):
    """Identity by email protects a running session. There is none here.

    Answering "this profile is the live account" for a login that no longer
    works handed out the unusable live token in place of the profile's own.
    The API rejected it, and `mark_auth_error` stamped the rejection on the
    one profile the rescue was reaching for — locking it out of every later
    ranking.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-live", refresh_expires=int((NOW - 1) * 1000)))
        self._write_main(_account("me@example.com"))
        self._save_with_usage(
            "me",
            _creds("token-saved", refresh_expires=int((NOW + 30 * DAY) * 1000)),
            _account("me@example.com"),
            10.0,
            40.0,
        )
        cc.write_active("me")
        self._enable_auto()

    def test_the_profile_is_no_longer_called_the_live_account(self) -> None:
        self.assertFalse(cc._is_live_account("me"))

    def test_its_own_stored_token_is_used(self) -> None:
        token, error, _ = cc.access_token_for("me", None)
        self.assertEqual((token, error), ("token-saved", None))

    def test_the_rescue_confirms_with_that_token(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))) as fetch:
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        fetch.assert_called_once_with("token-saved")

    def test_the_rescue_restores_the_account(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        live = json.loads(self.creds_file.read_text())
        self.assertEqual(live["claudeAiOauth"]["accessToken"], "token-saved")

    def test_no_auth_error_is_stamped_on_the_recovery_profile(self) -> None:
        """That mark would keep it out of every ranking from then on."""
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertNotIn("authError", json.loads(self._profile_path("me").read_text()))

    def test_pick_reaches_it_the_same_way(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))) as fetch, _silence():
            cc.cmd_pick(argparse.Namespace())
        fetch.assert_called_once_with("token-saved")

    def test_a_rejection_of_its_own_token_is_still_recorded(self) -> None:
        """The mark is honest when it is the profile's own credentials."""
        with patch.object(cc, "fetch_usage", return_value=(401, {})):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertIn("authError", json.loads(self._profile_path("me").read_text()))


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

    def test_the_gate_trigger_is_written_in_tenths(self) -> None:
        """Whole numbers cannot express a fractional threshold either way.

        Rounded down, the gate fires below the real threshold and the tick
        rewrites the same trigger on every render. Rounded up, 95.1 became 96
        and 95.2% usage never reached it at all.
        """
        with patch.object(cc, "EXIT_5H", 95.1):
            cc.recompute_gate("me", NOW)
        trigger_5h = int(self.gate_file.read_text().split()[2])
        self.assertEqual(trigger_5h, 951)

    def test_a_fractional_threshold_is_reachable(self) -> None:
        """95.2% usage must clear a 95.1% threshold, in the shell's units."""
        with patch.object(cc, "EXIT_5H", 95.1):
            cc.recompute_gate("me", NOW)
        trigger_5h = int(self.gate_file.read_text().split()[2])
        self.assertGreaterEqual(int(95.2 * 10), trigger_5h)
        self.assertLess(int(95.0 * 10), trigger_5h)

    def test_a_fractional_balance_trigger_does_not_spawn_on_every_render(self) -> None:
        """The other half: candidate 20.9, active 25.1, gap 5 — no reason."""
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 5.0, 20.9)
        cc.recompute_gate("me", NOW)
        trigger_7d = int(self.gate_file.read_text().split()[3])
        self.assertLess(int(25.1 * 10), trigger_7d)
        self.assertIsNone(cc.switch_reason(10.0, 25.1, 20.9))


class TestStatuslinePassesExactValues(unittest.TestCase):
    """The shell must not pre-round what the decision depends on."""

    def setUp(self) -> None:
        self.script = STATUSLINE_SCRIPT.read_text()

    def test_tick_receives_the_unrounded_percentages(self) -> None:
        self.assertIn('tick --5h "$FIVE_H" --7d "$WEEK"', self.script)

    def test_rounded_values_are_only_used_for_the_gate_comparison(self) -> None:
        self.assertNotIn('--5h "$FIVE_I"', self.script)
        self.assertIn('"${FIVE_I:-0}" -ge', self.script)

    def test_the_gate_comparison_works_in_tenths(self) -> None:
        """Both sides scale identically, so a fractional threshold is exact."""
        self.assertIn("FIVE_I=$(( ${FIVE_INT:-0} * 10 + ${FIVE_FRAC:0:1} ))", self.script)
        self.assertIn("WEEK_I=$(( ${WEEK_INT:-0} * 10 + ${WEEK_FRAC:0:1} ))", self.script)


class TestMalformedProfilesAreSkippedNotFatal(AutoBaseTest):
    """One hand-edited profile must never take a command down with it."""

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        cc.write_active("me")

    def _corrupt_expiry(self, name: str, value: object) -> None:
        cc.save_profile(name, _creds(f"token-{name}"), _account(f"{name}@x"))
        path = self._profile_path(name)
        data = json.loads(path.read_text())
        data["credentials"]["claudeAiOauth"]["expiresAt"] = value
        path.write_text(json.dumps(data, indent=2))

    def test_a_string_expiry_skips_the_profile(self) -> None:
        self._corrupt_expiry("broken", "soon")
        token, error, transient = cc.access_token_for("broken", "me")
        self.assertIsNone(token)
        self.assertEqual(error, "corrupted expiry")
        self.assertFalse(transient)

    def test_an_object_expiry_skips_the_profile(self) -> None:
        self._corrupt_expiry("broken", {"at": "later"})
        token, error, _ = cc.access_token_for("broken", "me")
        self.assertIsNone(token)
        self.assertEqual(error, "corrupted expiry")

    def test_usage_still_reports_the_healthy_accounts(self) -> None:
        self._corrupt_expiry("broken", "soon")
        self._save_with_usage("good", _creds("token-good"), _account("g@x"), 1.0, 2.0)
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(7.0, 8.0))), contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=False))
        text = out.getvalue()
        self.assertIn("good", text)
        self.assertIn("corrupted expiry", text)

    def test_a_tick_switches_past_the_broken_profile(self) -> None:
        self._corrupt_expiry("broken", "soon")
        self._save_with_usage("good", _creds("token-good"), _account("g@x"), 1.0, 2.0)
        self._enable_auto()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "good")

    def test_a_malformed_stored_snapshot_renders_as_unknown(self) -> None:
        cc.save_profile("odd", _creds("token-odd"), _account("o@x"))
        path = self._profile_path("odd")
        data = json.loads(path.read_text())
        data["usage"] = {"five_hour": "nonsense", "seven_day": [1, 2]}
        path.write_text(json.dumps(data, indent=2))
        fields = cc._usage_fields(data["usage"])
        self.assertIsNone(fields["five_hour"])
        self.assertIsNone(fields["resets_7d"])

    def test_usage_survives_a_malformed_snapshot_on_an_unusable_profile(self) -> None:
        cc.save_profile("odd", _creds("token-odd", refresh_expires=int((NOW - 1) * 1000)), _account("o@x"))
        path = self._profile_path("odd")
        data = json.loads(path.read_text())
        data["usage"] = {"five_hour": "nonsense", "seven_day": [1, 2]}
        path.write_text(json.dumps(data, indent=2))
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(7.0, 8.0))), contextlib.redirect_stdout(out):
            rc = cc.cmd_usage(argparse.Namespace(json=False))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("me", out.getvalue())


class TestTickRejectsGarbledUsage(TickTestCase):
    """A garbled statusline payload must not be persisted or acted on."""

    def test_a_nan_percentage_is_ignored(self) -> None:
        with patch.object(cc, "fetch_usage", side_effect=AssertionError("must not run")):
            rc = cc.cmd_tick(self._tick_args(float("nan"), float("nan")))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(cc.read_active(), "me")
        self.assertIn("non-finite", self._log_text())

    def test_a_nan_percentage_does_not_poison_the_snapshot(self) -> None:
        before = self._stored_usage("me")
        with patch.object(cc, "fetch_usage", side_effect=AssertionError("must not run")):
            cc.cmd_tick(self._tick_args(float("nan"), 40.0))
        self.assertEqual(self._stored_usage("me"), before)

    def test_an_infinite_percentage_is_ignored(self) -> None:
        with patch.object(cc, "fetch_usage", side_effect=AssertionError("must not run")):
            cc.cmd_tick(self._tick_args(float("inf"), 40.0))
        self.assertEqual(cc.read_active(), "me")

    def test_an_out_of_range_percentage_is_clamped(self) -> None:
        """Clamped rather than dropped: 101% still means 'full'."""
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(120.0, -5.0))
        stored = self._stored_usage("me")
        self.assertEqual(stored["five_hour"]["utilization"], 100.0)
        self.assertEqual(stored["seven_day"]["utilization"], 0.0)

    def test_a_clamped_full_account_still_evacuates(self) -> None:
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(120.0, 40.0))
        self.assertEqual(cc.read_active(), "vlad")


class TestARefusedSwitchIsInert(AutoBaseTest):
    """A switch that cannot start must leave nothing behind.

    The settle barrier is armed for a switch that is about to happen. Arming
    it for one that never starts would silence automatic switching for the
    whole window, for nothing.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")

    def test_a_malformed_target_leaves_no_barrier_behind(self) -> None:
        """A refused switch must not suppress auto-switching afterwards.

        The barrier is armed for a switch that is about to happen; arming it
        for one that never starts would silence the automatic path for the
        whole settle window, for nothing.
        """
        self._profile_path("vlad").write_text("{not json")
        with contextlib.suppress(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertFalse(self.settle_file.exists())
        self.assertEqual(cc.read_active(), "me")

    def test_a_profile_missing_its_credentials_leaves_no_barrier(self) -> None:
        self._profile_path("vlad").write_text(json.dumps({"oauthAccount": _account("v@x")}))
        with contextlib.suppress(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertFalse(self.settle_file.exists())
        self.assertEqual(cc.read_active(), "me")

    def test_an_unreadable_main_config_leaves_no_barrier(self) -> None:
        self.main_file.write_text("{not json")
        with contextlib.suppress(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertFalse(self.settle_file.exists())

    def test_a_failing_backup_releases_the_barrier(self) -> None:
        """The barrier guards a switch; if the switch aborts it must go."""
        with (
            patch.object(cc, "_auto_backup_active", side_effect=SystemExit(2)),
            self.assertRaises(SystemExit),
            _silence(),
        ):
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertFalse(self.settle_file.exists())
        self.assertEqual(cc.read_active(), "me")

    def test_a_failing_main_backup_releases_the_barrier(self) -> None:
        with (
            patch.object(cc, "backup_main", side_effect=SystemExit(2)),
            self.assertRaises(SystemExit),
            _silence(),
        ):
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertFalse(self.settle_file.exists())

    def test_an_unexpected_error_also_releases_it(self) -> None:
        with (
            patch.object(cc, "backup_main", side_effect=RuntimeError("disk gremlin")),
            self.assertRaises(RuntimeError),
            _silence(),
        ):
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertFalse(self.settle_file.exists())

    def test_a_failing_atomic_write_releases_the_barrier(self) -> None:
        """The swap rolls itself back, so the barrier must go with it."""
        with (
            patch.object(cc, "_apply_switch_atomic", side_effect=SystemExit(2)),
            self.assertRaises(SystemExit),
            _silence(),
        ):
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertFalse(self.settle_file.exists())
        self.assertEqual(cc.read_active(), "me")

    def test_an_unexpected_error_in_the_swap_releases_it_too(self) -> None:
        with (
            patch.object(cc, "_apply_switch_atomic", side_effect=RuntimeError("disk full")),
            self.assertRaises(RuntimeError),
            _silence(),
        ):
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertFalse(self.settle_file.exists())

    def test_a_successful_switch_keeps_the_barrier(self) -> None:
        """The release must not fire on the happy path."""
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(cc.read_active(), "vlad")
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.SETTLE_SECONDS))

    def test_a_refused_switch_does_not_touch_the_profiles(self) -> None:
        before = self._profile_path("me").read_text()
        self._profile_path("vlad").write_text("{not json")
        with contextlib.suppress(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(self._profile_path("me").read_text(), before)


class TestUnwritableSettleFileAbortsTheSwitch(AutoBaseTest):
    """Without the settle barrier a switch is not safe to perform.

    The statusline keeps reporting the outgoing account's percentages for a
    moment; a tick reading those with no barrier in place switches straight
    back, silently undoing what the user asked for. Since the barrier is
    armed before the swap, failing to write it leaves everything untouched.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")
        self.settle_file.mkdir()  # a directory cannot be replaced by a file

    def test_the_switch_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as ctx, _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertNotEqual(ctx.exception.code, cc.EXIT_OK)

    def test_the_credentials_are_untouched(self) -> None:
        before = self.creds_file.read_text()
        with contextlib.suppress(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(self.creds_file.read_text(), before)
        self.assertEqual(cc.read_active(), "me")

    def test_no_half_applied_switch_is_left_behind(self) -> None:
        """The whole point: nothing partial for a later tick to act on."""
        self._enable_auto()
        with contextlib.suppress(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(cc.read_active(), "me")
        self.assertEqual(
            json.loads(self.creds_file.read_text())["claudeAiOauth"]["accessToken"], "token-me"
        )
        self.assertEqual(json.loads(self.main_file.read_text())["oauthAccount"]["emailAddress"], "me@example.com")

    def test_the_saved_profiles_are_intact(self) -> None:
        before = {n: self._profile_path(n).read_text() for n in ("me", "vlad")}
        with contextlib.suppress(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        for name, text in before.items():
            self.assertEqual(self._profile_path(name).read_text(), text, name)


class TestSettleBarrierProtectsAManualSwitch(AutoBaseTest):
    """A manual switch survives the very next statusline render."""

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 95.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")
        self._enable_auto()

    def test_stale_usage_right_after_a_switch_does_not_reverse_it(self) -> None:
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(cc.read_active(), "vlad")
        # The render still describes the account we just left.
        with patch.object(cc, "fetch_usage", side_effect=AssertionError("must not evaluate")):
            cc.cmd_tick(self._tick_args(10.0, 95.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_the_barrier_is_armed_by_the_switch(self) -> None:
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.SETTLE_SECONDS))

    def test_an_automatic_switch_arms_it_too(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 95.0))
        self.assertEqual(cc.read_active(), "vlad")
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.SETTLE_SECONDS))


class TestStatuslineNamesTheLiveAccount(AutoBaseTest):
    """`.live` is written by cc-switch; `.active` is only a fallback."""

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-vlad"))
        self._write_main(_account("vlad@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")  # stale marker

    def test_resolving_records_the_live_account(self) -> None:
        self.assertEqual(cc.resolve_active(), "vlad")
        self.assertEqual(self.live_file.read_text().strip(), "vlad")

    def test_an_unresolvable_account_writes_an_empty_name(self) -> None:
        """A cleared cache is right: showing a name we cannot confirm misleads."""
        self._write_main(_account("stranger@example.com"))
        self.assertIsNone(cc.resolve_active())
        self.assertEqual(self.live_file.read_text().strip(), "")

    def test_the_cache_refreshes_without_the_gate(self) -> None:
        """The WARNING case: auto off, no tick — `current` still corrects it."""
        self.live_file.write_text("me\n")
        with contextlib.redirect_stdout(io.StringIO()):
            cc.cmd_current(argparse.Namespace())
        self.assertEqual(self.live_file.read_text().strip(), "vlad")

    def test_an_unchanged_name_is_not_rewritten(self) -> None:
        cc.resolve_active()
        before = self.live_file.stat().st_mtime_ns
        cc.resolve_active()
        self.assertEqual(self.live_file.stat().st_mtime_ns, before)

    def test_the_statusline_prefers_the_live_file(self) -> None:
        script = STATUSLINE_SCRIPT.read_text()
        self.assertIn(".live", script)
        live_pos = script.index("$PROFILES_DIR/.live")
        active_pos = script.index("$PROFILES_DIR/.active")
        self.assertLess(live_pos, active_pos)

    def test_the_statusline_still_falls_back_to_the_marker(self) -> None:
        """A profile set saved before this change has no `.live` yet."""
        script = STATUSLINE_SCRIPT.read_text()
        self.assertIn('[ -z "$ACCOUNT" ] && [ -r "$PROFILES_DIR/.active" ]', script)


class TestJsonSpecialFloatsAreRejected(AutoBaseTest):
    """"NaN" and "Infinity" are valid JSON floats to Python.

    They pass `float()` without raising, so a try/except alone is not the
    guard it looks like: infinity would treat a dead token as valid forever.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))

    def _profile_with_expiry(self, value: object) -> None:
        cc.save_profile("odd", _creds("token-odd"), _account("odd@x"))
        path = self._profile_path("odd")
        data = json.loads(path.read_text())
        data["credentials"]["claudeAiOauth"]["expiresAt"] = value
        path.write_text(json.dumps(data, indent=2))

    def test_infinity_does_not_make_a_dead_token_valid(self) -> None:
        self._profile_with_expiry(float("inf"))
        with patch.object(cc, "oauth_refresh", side_effect=AssertionError("must not refresh")):
            token, error, _ = cc.access_token_for("odd", "me")
        self.assertIsNone(token)
        self.assertEqual(error, "corrupted expiry")

    def test_nan_does_not_trigger_a_refresh(self) -> None:
        self._profile_with_expiry(float("nan"))
        with patch.object(cc, "oauth_refresh", side_effect=AssertionError("must not refresh")):
            token, error, _ = cc.access_token_for("odd", "me")
        self.assertIsNone(token)
        self.assertEqual(error, "corrupted expiry")

    def test_the_json_text_forms_are_rejected_too(self) -> None:
        """json.loads turns bare NaN/Infinity into the float, not a string."""
        path = self._profile_path("odd")
        cc.save_profile("odd", _creds("token-odd"), _account("odd@x"))
        raw = path.read_text().replace('"expiresAt": 9999999999000', '"expiresAt": Infinity')
        path.write_text(raw)
        token, error, _ = cc.access_token_for("odd", "me")
        self.assertIsNone(token)
        self.assertEqual(error, "corrupted expiry")

    def test_an_ordinary_expiry_still_works(self) -> None:
        self._profile_with_expiry(int((NOW + DAY) * 1000))
        with patch.object(cc, "time") as fake_time:
            fake_time.time.return_value = NOW
            token, error, _ = cc.access_token_for("odd", "me")
        self.assertEqual((token, error), ("token-odd", None))


class TestMalformedSnapshotsDoNotBreakTheGate(AutoBaseTest):
    """The decision path needs the same tolerance the display path got.

    `recompute_gate` runs on `auto on`, on every switch, and on a no-switch
    tick — raising there takes all three down.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        cc.save_profile("odd", _creds("token-odd"), _account("odd@x"))
        path = self._profile_path("odd")
        data = json.loads(path.read_text())
        data["usage"] = {"five_hour": "nonsense", "seven_day": [1, 2]}
        path.write_text(json.dumps(data, indent=2))
        cc.write_active("me")

    def test_the_reset_scan_skips_the_malformed_windows(self) -> None:
        self.assertEqual(cc.earliest_future_reset(["odd"], NOW), 0.0)

    def test_a_healthy_profile_beside_it_is_still_read(self) -> None:
        self._save_with_usage("good", _creds("token-good"), _account("g@x"), 1.0, 1.0, _iso(NOW + HOUR))
        self.assertEqual(cc.earliest_future_reset(["odd", "good"], NOW), NOW + HOUR)

    def test_recompute_gate_survives(self) -> None:
        cc.recompute_gate("me", NOW)
        self.assertTrue(self.gate_file.exists())

    def test_auto_on_survives(self) -> None:
        with _silence():
            rc = cc.cmd_auto(argparse.Namespace(action="on"))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertTrue(cc.auto_enabled())

    def test_a_no_switch_tick_survives(self) -> None:
        self._enable_auto()
        self._save_with_usage("good", _creds("token-good"), _account("g@x"), 1.0, 39.0)
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 39.0))):
            rc = cc.cmd_tick(self._tick_args(10.0, 40.0))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(cc.read_active(), "me")

    def test_a_switch_survives(self) -> None:
        """The malformed profile is the only candidate, so it must be usable.

        Its unreadable snapshot reads as "usage unknown", which ranks it as
        free — the same treatment a profile with no snapshot gets, and the
        live check is what actually decides.
        """
        self._enable_auto()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 1.0))):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "odd")

    def test_a_malformed_snapshot_is_replaced_by_the_live_reading(self) -> None:
        """Switching to it repairs the profile rather than leaving it broken."""
        self._enable_auto()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(7.0, 8.0))):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        repaired = json.loads(self._profile_path("odd").read_text())["usage"]
        self.assertEqual(repaired["five_hour"]["utilization"], 7.0)
        self.assertEqual(repaired["seven_day"]["utilization"], 8.0)


class TestSwitchingRefreshesTheDisplayedAccount(AutoBaseTest):
    """A switch must update `.live` itself, with nothing else running.

    `_auto_backup_active` resolves (and caches) the account being left, so a
    switch that only writes `.active` leaves the statusline showing the old
    name. These read the file directly — calling `resolve_active()` first
    would repair the cache and hide the defect.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")
        cc.resolve_active()  # prime the cache with the pre-switch account
        self.assertEqual(self.live_file.read_text().strip(), "me")

    def test_manual_use_updates_the_cache(self) -> None:
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(self.live_file.read_text().strip(), "vlad")

    def test_an_automatic_switch_updates_the_cache(self) -> None:
        self._enable_auto()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))):
            cc.cmd_tick(self._tick_args(cc.EXIT_5H, 40.0))
        self.assertEqual(cc.read_active(), "vlad")
        self.assertEqual(self.live_file.read_text().strip(), "vlad")

    def test_pick_updates_the_cache(self) -> None:
        def _usage(token: str) -> tuple[int, object]:
            return (200, _api_usage(10.0, 60.0)) if token == "token-me" else (200, _api_usage(5.0, 20.0))

        with patch.object(cc, "fetch_usage", side_effect=_usage), _silence():
            cc.cmd_pick(argparse.Namespace())
        self.assertEqual(self.live_file.read_text().strip(), "vlad")

    def test_the_statusline_would_name_the_new_account(self) -> None:
        """End to end: the file the shell actually reads holds the new name."""
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        displayed = self.live_file.read_text().strip()
        self.assertEqual(displayed, "vlad")
        self.assertNotEqual(displayed, "me")


class TestUnwritableLiveCacheIsNotSilentlyStale(AutoBaseTest):
    """A cache that cannot be updated must not keep showing the old name.

    `.live` wins over `.active` in the statusline, so a failed update would
    leave the wrong account on display indefinitely. Removing it is the
    honest outcome: the statusline falls back to `.active`, and the very next
    successful resolve repopulates it.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-vlad"))
        self._write_main(_account("vlad@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"))
        cc.write_active("vlad")
        self.live_file.write_text("me\n")  # a stale cached name

    def test_a_failed_update_removes_the_stale_name(self) -> None:
        with patch.object(cc, "write_text_file", return_value=False):
            cc.resolve_active()
        self.assertFalse(self.live_file.exists())

    def test_the_statusline_then_falls_back_to_the_marker(self) -> None:
        with patch.object(cc, "write_text_file", return_value=False):
            cc.resolve_active()
        # `.active` is what the shell reads next, and it names the right one.
        self.assertEqual(cc.read_active(), "vlad")

    def test_a_successful_update_repopulates_it(self) -> None:
        with patch.object(cc, "write_text_file", return_value=False):
            cc.resolve_active()
        self.assertEqual(cc.resolve_active(), "vlad")
        self.assertEqual(self.live_file.read_text().strip(), "vlad")

    def test_an_already_absent_cache_is_not_an_error(self) -> None:
        """Two processes can hit the same failure; the second finds it gone."""
        self.live_file.unlink()
        with patch.object(cc, "write_text_file", return_value=False):
            cc.resolve_active()
        self.assertFalse(self.live_file.exists())
        self.assertNotIn("could not be cleared", self._log_text())

    def test_an_unremovable_stale_cache_is_reported(self) -> None:
        """Nothing else can be done, but it must not pass unnoticed."""
        with (
            patch.object(cc, "write_text_file", return_value=False),
            patch.object(cc.Path, "unlink", side_effect=OSError("read-only")),
        ):
            cc.resolve_active()
        self.assertIn("stale", self._log_text())


class TestLoginExpiryIsVisible(AutoBaseTest):
    """Every command that names an account says when its login runs out.

    Knowing the date is what turns a surprise "Not logged in" into a planned
    `claude` sign-in.
    """

    def setUp(self) -> None:
        super().setUp()
        # The live credentials carry the same expiry as the saved copy, as
        # they do in reality; the active row reads the live one.
        self._write_creds(_creds("token-me", refresh_expires=int((NOW + 20 * DAY) * 1000)))
        self._write_main(_account("me@example.com"))
        self._save_with_usage(
            "me", _creds("token-me", refresh_expires=int((NOW + 20 * DAY) * 1000)), _account("me@example.com")
        )
        self._save_with_usage(
            "vlad", _creds("token-vlad", refresh_expires=int((NOW + 6 * HOUR) * 1000)), _account("vlad@example.com")
        )
        cc.write_active("me")

    def _run(self, fn: object, args: argparse.Namespace) -> str:
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))), contextlib.redirect_stdout(out):
            fn(args)
        return out.getvalue()

    def test_current_shows_the_expiry(self) -> None:
        text = self._run(cc.cmd_current, argparse.Namespace())
        self.assertIn("me", text)
        self.assertIn("login until", text)
        self.assertIn(_dt.datetime.fromtimestamp(NOW + 20 * DAY).strftime("%d %b"), text)

    def test_list_shows_it_for_every_profile(self) -> None:
        text = self._run(cc.cmd_list, argparse.Namespace())
        self.assertEqual(text.count("login until"), 2)

    def test_usage_has_a_login_column(self) -> None:
        text = self._run(cc.cmd_usage, argparse.Namespace(json=False))
        self.assertIn("LOGIN UNTIL", text)
        self.assertIn(_dt.datetime.fromtimestamp(NOW + 6 * HOUR).strftime("%d %b %H:%M"), text)

    def test_usage_json_carries_the_raw_epoch(self) -> None:
        """Machine-readable output gives the timestamp, not a rendered string."""
        rows = {r["profile"]: r for r in json.loads(self._run(cc.cmd_usage, argparse.Namespace(json=True)))}
        self.assertEqual(rows["me"]["login_expires_at"], NOW + 20 * DAY)
        self.assertEqual(rows["vlad"]["login_expires_at"], NOW + 6 * HOUR)

    def test_a_near_expiry_is_shown_in_hours(self) -> None:
        text = self._run(cc.cmd_list, argparse.Namespace())
        self.assertIn("(in 6h)", text)

    def test_a_distant_expiry_is_shown_in_days(self) -> None:
        text = self._run(cc.cmd_list, argparse.Namespace())
        self.assertIn("(in 20d)", text)

    def test_an_expired_login_says_so(self) -> None:
        self._save_with_usage(
            "dead", _creds("token-dead", refresh_expires=int((NOW - DAY) * 1000)), _account("d@x")
        )
        self.assertIn("EXPIRED", self._run(cc.cmd_list, argparse.Namespace()))

    def test_an_unknown_expiry_says_unknown(self) -> None:
        cc.save_profile("old", _creds("token-old"), _account("o@x"))  # no expiry field
        self.assertIn("unknown", self._run(cc.cmd_list, argparse.Namespace()))


class TestLenientStateReads(AutoBaseTest):
    """`strict=False` has to survive damaged files, not just missing ones.

    A half-written credentials or config file is exactly the situation the
    non-strict callers exist to handle; exiting there would take down a tick
    that was trying to escape that very account.
    """

    def test_damaged_credentials_return_none(self) -> None:
        self.creds_file.write_text("{not json")
        self._write_main(_account("me@example.com"))
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_damaged_main_config_returns_none(self) -> None:
        self._write_creds(_creds("token-me"))
        self.main_file.write_text("{not json")
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_a_non_object_main_config_returns_none(self) -> None:
        self._write_creds(_creds("token-me"))
        self.main_file.write_text("[1, 2, 3]")
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_strict_still_exits_on_damage(self) -> None:
        """The manual commands keep their loud failure."""
        self._write_creds(_creds("token-me"))
        self.main_file.write_text("{not json")
        with self.assertRaises(SystemExit), _silence():
            cc.read_current_state(strict=True)

    def test_healthy_files_still_read(self) -> None:
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        state = cc.read_current_state(strict=False)
        self.assertIsNotNone(state)
        self.assertEqual(state[1]["emailAddress"], "me@example.com")


class TestLoginExpiryHelpers(unittest.TestCase):
    def test_reads_the_refresh_expiry(self) -> None:
        data = {"credentials": _creds("t", refresh_expires=int((NOW + DAY) * 1000)), "oauthAccount": {}}
        self.assertEqual(cc.profile_login_expiry(data), NOW + DAY)

    def test_a_missing_field_is_unknown(self) -> None:
        self.assertIsNone(cc.profile_login_expiry({"credentials": _creds("t")}))

    def test_a_non_dict_profile_is_unknown(self) -> None:
        self.assertIsNone(cc.profile_login_expiry("nonsense"))

    def test_a_non_dict_credentials_block_is_unknown(self) -> None:
        """A hand-edited profile must not break the listing for the others."""
        self.assertIsNone(cc.profile_login_expiry({"credentials": "nonsense"}))

    def test_a_non_dict_oauth_block_is_unknown(self) -> None:
        self.assertIsNone(cc.profile_login_expiry({"credentials": {"claudeAiOauth": [1, 2]}}))

    def test_a_non_finite_expiry_is_unknown(self) -> None:
        data = {"credentials": {"claudeAiOauth": {"refreshTokenExpiresAt": float("inf")}}}
        self.assertIsNone(cc.profile_login_expiry(data))

    def test_formatting_unknown(self) -> None:
        self.assertEqual(cc._fmt_login_expiry(None, NOW), "unknown")

    def test_formatting_expired(self) -> None:
        self.assertIn("EXPIRED", cc._fmt_login_expiry(NOW - 1, NOW))

    def test_formatting_hours_under_two_days(self) -> None:
        self.assertIn("(in 47h)", cc._fmt_login_expiry(NOW + 47 * HOUR, NOW))

    def test_formatting_days_at_two_days_and_beyond(self) -> None:
        self.assertIn("(in 2d)", cc._fmt_login_expiry(NOW + 48 * HOUR, NOW))

    def test_the_date_itself_is_always_present(self) -> None:
        rendered = cc._fmt_login_expiry(NOW + DAY, NOW)
        self.assertIn(_dt.datetime.fromtimestamp(NOW + DAY).strftime("%d %b %H:%M"), rendered)


class TestDeadLoginTriggersAnEvacuation(TickTestCase):
    """An expired login is a reason to switch in its own right.

    It is worse than an exhausted one: the session stops with "Not logged
    in", and no usage figures are reported at all, so nothing in the limit
    path would ever notice.
    """

    def _kill_the_live_login(self) -> None:
        creds = _creds("token-me", refresh_expires=int((NOW - 1) * 1000))
        self._write_creds(creds)

    def test_an_expired_login_evacuates_without_any_usage(self) -> None:
        self._kill_the_live_login()
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "vlad")
        self.assertIn("live login unusable", self._log_text())

    def test_missing_credentials_evacuate_too(self) -> None:
        self.creds_file.unlink()
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_unreadable_credentials_evacuate_too(self) -> None:
        self.creds_file.write_text("{not json")
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_credentials_without_a_refresh_token_evacuate(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a"}}))
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_a_healthy_login_is_left_alone(self) -> None:
        """Level usage, healthy login: no reason of any kind to move."""
        self._write_creds(_creds("token-me", refresh_expires=int((NOW + DAY) * 1000)))
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 5.0, 40.0)
        with self._patch_usage(5.0, 40.0):
            cc.cmd_tick(self._tick_args(10.0, 40.0))
        self.assertEqual(cc.read_active(), "me")

    def test_an_unknown_expiry_is_not_treated_as_dead(self) -> None:
        """Older saved logins have no expiry field; that is not evidence."""
        self.assertIsNone(cc.live_credentials_dead(NOW))

    def test_the_marker_profile_is_not_excluded_from_the_rescue(self) -> None:
        """`.active` is a guess once there is no live email to match it against.

        Excluding it hid the one profile whose saved copy still held a
        working login: the tick reported no working account and backed off
        while the fix sat in `me.json`.
        """
        self.creds_file.unlink()
        self._profile_path("vlad").unlink()
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "me")
        # "?" and not "me": with the credentials gone, nothing knew who was
        # signed in — the marker is a guess the log must not launder as fact.
        self.assertIn("switch ? -> me (expired)", self._log_text())

    def test_the_rescue_restores_the_credentials_file(self) -> None:
        self.creds_file.unlink()
        self._profile_path("vlad").unlink()
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        live = json.loads(self.creds_file.read_text())
        self.assertEqual(live["claudeAiOauth"]["accessToken"], "token-me")

    def test_the_rescue_does_not_overwrite_the_profile_it_restores(self) -> None:
        """There are no live credentials to back up; saving them would erase it."""
        self.creds_file.unlink()
        self._profile_path("vlad").unlink()
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        saved = json.loads(self._profile_path("me").read_text())
        self.assertEqual(saved["credentials"]["claudeAiOauth"]["accessToken"], "token-me")

    def test_the_marker_profile_is_confirmed_with_its_own_token(self) -> None:
        """There is no live token to confirm with — that is the whole problem."""
        self.creds_file.unlink()
        self._profile_path("vlad").unlink()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))) as fetch:
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        fetch.assert_called_once_with("token-me")

    def test_the_live_account_is_still_never_refreshed(self) -> None:
        """Email identity, not the marker, is what protects a running token."""
        self._kill_the_live_login()
        self._save_with_usage(
            "me",
            _creds("token-me", refresh_expires=int((NOW + DAY) * 1000)),
            _account("me@example.com"),
            1.0,
            1.0,
        )
        with patch.object(cc, "oauth_refresh", side_effect=AssertionError("must not refresh")), \
                self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "me")

    def test_the_entry_bar_still_applies(self) -> None:
        """A dead login is not a reason to move onto an exhausted account."""
        self._kill_the_live_login()
        with self._patch_usage(10.0, cc.ENTER_7D + 5):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "me")

    def test_an_expired_login_beats_a_balance_veto(self) -> None:
        """Level usage gives no balance reason — the dead login must still win."""
        self._kill_the_live_login()
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 5.0, 40.0)
        with self._patch_usage(5.0, 40.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_the_reason_is_recorded(self) -> None:
        self._kill_the_live_login()
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertIn("(expired)", self._log_text())


class TestRefreshExpiryValidation(unittest.TestCase):
    """One definition of a usable expiry, because three had drifted apart."""

    def _oauth(self, value: object) -> dict[str, object]:
        return {"refreshToken": "r", "refreshTokenExpiresAt": value}

    def test_an_ordinary_millisecond_timestamp(self) -> None:
        self.assertEqual(cc.refresh_expiry_epoch(self._oauth(int(NOW * 1000))), NOW)

    def test_a_float_timestamp(self) -> None:
        self.assertEqual(cc.refresh_expiry_epoch(self._oauth(NOW * 1000)), NOW)

    def test_true_is_rejected(self) -> None:
        """JSON true is a Python int: it would read as epoch 0.001."""
        self.assertIsNone(cc.refresh_expiry_epoch(self._oauth(True)))

    def test_false_is_rejected(self) -> None:
        self.assertIsNone(cc.refresh_expiry_epoch(self._oauth(False)))

    def test_nan_is_rejected(self) -> None:
        self.assertIsNone(cc.refresh_expiry_epoch(self._oauth(float("nan"))))

    def test_infinity_is_rejected(self) -> None:
        self.assertIsNone(cc.refresh_expiry_epoch(self._oauth(float("inf"))))

    def test_a_huge_but_finite_value_is_rejected(self) -> None:
        """1e300 passes every numeric check, then crashes fromtimestamp."""
        self.assertIsNone(cc.refresh_expiry_epoch(self._oauth(1e300)))

    def test_a_negative_value_is_rejected(self) -> None:
        self.assertIsNone(cc.refresh_expiry_epoch(self._oauth(-1)))

    def test_a_string_is_rejected(self) -> None:
        self.assertIsNone(cc.refresh_expiry_epoch(self._oauth("soon")))

    def test_a_missing_field_is_rejected(self) -> None:
        self.assertIsNone(cc.refresh_expiry_epoch({"refreshToken": "r"}))

    def test_a_non_dict_is_rejected(self) -> None:
        self.assertIsNone(cc.refresh_expiry_epoch("nonsense"))

    def test_the_upper_bound_is_formattable(self) -> None:
        """Whatever passes must survive the display path."""
        rendered = cc._fmt_login_expiry(cc.MAX_EPOCH, NOW)
        self.assertIn("(in", rendered)


class TestUnreadableBytesAreSurvivable(AutoBaseTest):
    """Invalid UTF-8 raises UnicodeDecodeError, not a JSON error.

    It is a ValueError, so catching only OSError/JSONDecodeError/TypeError
    let it escape and crash the very callers that exist to tolerate damage.
    """

    def setUp(self) -> None:
        super().setUp()
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        cc.write_active("me")

    def test_resolve_active_survives_invalid_bytes(self) -> None:
        """No live email to match against, so the marker is all there is."""
        self.creds_file.write_bytes(b"\xff\xfe not utf-8")
        self._write_main(_account("me@example.com"))
        self.assertEqual(cc.resolve_active(), "me")

    def test_resolve_active_returns_nothing_without_a_marker(self) -> None:
        self.creds_file.write_bytes(b"\xff\xfe not utf-8")
        self._write_main(_account("me@example.com"))
        self.active_file.unlink()
        self.assertIsNone(cc.resolve_active())

    def test_read_current_state_survives_invalid_bytes(self) -> None:
        self.creds_file.write_bytes(b"\xff\xfe")
        self._write_main(_account("me@example.com"))
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_the_main_config_too(self) -> None:
        self._write_creds(_creds("token-me"))
        self.main_file.write_bytes(b"\xff\xfe")
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_a_profile_with_invalid_bytes_is_skipped(self) -> None:
        self._profile_path("odd").write_bytes(b"\xff\xfe")
        self.assertIsNone(cc.load_profile_data("odd"))

    def test_the_credentials_status_reports_it(self) -> None:
        self.creds_file.write_bytes(b"\xff\xfe")
        reason, deadline = cc.live_credentials_status(NOW)
        self.assertEqual(reason, "unreadable credentials")
        self.assertEqual(deadline, cc.DEADLINE_NOW)

    def test_list_still_renders(self) -> None:
        self._profile_path("odd").write_bytes(b"\xff\xfe")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        self.assertIn("me", out.getvalue())


class TestStructureIsCheckedNotJustPresence(AutoBaseTest):
    """A key being present says nothing about what is under it.

    `{"claudeAiOauth": []}` passed a key check and then crashed the first
    `.get` downstream — in `usage`, on the active account.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        cc.write_active("me")

    def test_a_list_under_the_oauth_key_is_rejected(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": []}))
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_a_string_under_the_oauth_key_is_rejected(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": "nonsense"}))
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_null_under_the_oauth_key_is_rejected(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": None}))
        self.assertIsNone(cc.read_current_state(strict=False))

    def test_usage_survives_it(self) -> None:
        """The crash the reviewer traced: `usage` on the active account."""
        self.creds_file.write_text(json.dumps({"claudeAiOauth": []}))
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))), contextlib.redirect_stdout(out):
            rc = cc.cmd_usage(argparse.Namespace(json=False))
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertIn("me", out.getvalue())

    def test_the_access_token_lookup_reports_it(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": []}))
        token, error, _ = cc.access_token_for("me", "me")
        self.assertIsNone(token)
        self.assertEqual(error, "no live credentials")

    def test_it_counts_as_a_dead_login(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": []}))
        self.assertEqual(cc.live_credentials_dead(NOW), "no refresh token")

    def test_strict_mode_still_exits(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": []}))
        with self.assertRaises(SystemExit), _silence():
            cc.read_current_state(strict=True)

    def test_a_healthy_object_still_reads(self) -> None:
        self._write_creds(_creds("token-me"))
        state = cc.read_current_state(strict=False)
        self.assertIsNotNone(state)


class TestTheEmailScanSkipsDamagedProfiles(AutoBaseTest):
    """Resolving by email reads every profile; one bad file must not stop it."""

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-vlad"))
        self._write_main(_account("vlad@example.com"))
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"))

    def test_invalid_utf8_in_another_profile_is_skipped(self) -> None:
        self._profile_path("aaa").write_bytes(b"\xff\xfe")  # sorts before vlad
        self.assertEqual(cc.resolve_active(), "vlad")

    def test_the_email_reader_returns_none_for_it(self) -> None:
        self._profile_path("aaa").write_bytes(b"\xff\xfe")
        self.assertIsNone(cc._profile_email(self._profile_path("aaa")))

    def test_list_still_renders_every_other_profile(self) -> None:
        self._profile_path("aaa").write_bytes(b"\xff\xfe")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        self.assertIn("vlad", out.getvalue())

    def test_current_still_answers(self) -> None:
        self._profile_path("aaa").write_bytes(b"\xff\xfe")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_current(argparse.Namespace())
        self.assertIn("vlad", out.getvalue())

    def test_ranking_skips_it(self) -> None:
        self._profile_path("aaa").write_bytes(b"\xff\xfe")
        self.assertEqual([c.name for c in cc.rank_candidates(NOW, "vlad")], [])


class TestOversizedIntegersAreMalformed(AutoBaseTest):
    """An int beyond float range raises OverflowError, not ValueError."""

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        cc.write_active("me")

    def test_the_expiry_validator_rejects_it(self) -> None:
        self.assertIsNone(cc.refresh_expiry_epoch({"refreshTokenExpiresAt": 10**400}))

    def test_candidate_confirmation_skips_the_profile(self) -> None:
        cc.save_profile("odd", _creds("token-odd"), _account("odd@x"))
        path = self._profile_path("odd")
        path.write_text(path.read_text().replace('"expiresAt": 9999999999000', f'"expiresAt": {10**400}'))
        token, error, _ = cc.access_token_for("odd", "me")
        self.assertIsNone(token)
        self.assertEqual(error, "corrupted expiry")

    def test_usage_still_reports_the_other_accounts(self) -> None:
        cc.save_profile("odd", _creds("token-odd"), _account("odd@x"))
        path = self._profile_path("odd")
        path.write_text(path.read_text().replace('"expiresAt": 9999999999000', f'"expiresAt": {10**400}'))
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))), contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=False))
        self.assertIn("me", out.getvalue())

    def test_list_renders_it_as_unknown(self) -> None:
        cc.save_profile("odd", _creds("token-odd"), _account("odd@x"))
        path = self._profile_path("odd")
        data = path.read_text().replace('"expiresAt": 9999999999000', '"expiresAt": 9999999999000, "refreshTokenExpiresAt": ' + str(10**400))
        path.write_text(data)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        self.assertIn("unknown", out.getvalue())


class TestAccessTokenExpiryIsBounded(AutoBaseTest):
    """An implausible `expiresAt` made a dead token look valid.

    It was then sent to the API, and the 401 retired a profile that only
    needed refreshing.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        cc.write_active("me")

    def _profile_with_access_expiry(self, value: object) -> None:
        cc.save_profile("odd", _creds("token-odd"), _account("odd@x"))
        path = self._profile_path("odd")
        data = json.loads(path.read_text())
        data["credentials"]["claudeAiOauth"]["expiresAt"] = value
        path.write_text(json.dumps(data, indent=2))

    def test_a_huge_finite_expiry_is_rejected(self) -> None:
        self._profile_with_access_expiry(1e300)
        token, error, _ = cc.access_token_for("odd", "me")
        self.assertIsNone(token)
        self.assertEqual(error, "corrupted expiry")

    def test_the_stale_token_is_never_sent(self) -> None:
        self._profile_with_access_expiry(1e300)
        with patch.object(cc, "fetch_usage", side_effect=AssertionError("must not send")) as usage:
            cc.confirm_candidate("odd", "me")
        usage.assert_not_called()

    def test_the_profile_is_not_retired(self) -> None:
        """The damage this caused: a refreshable account marked authError."""
        self._profile_with_access_expiry(1e300)
        cc.confirm_candidate("odd", "me")
        self.assertNotIn("authError", json.loads(self._profile_path("odd").read_text()))

    def test_a_negative_expiry_is_rejected(self) -> None:
        self._profile_with_access_expiry(-1)
        token, error, _ = cc.access_token_for("odd", "me")
        self.assertEqual((token, error), (None, "corrupted expiry"))

    def test_an_ordinary_expiry_still_works(self) -> None:
        self._profile_with_access_expiry(int((NOW + DAY) * 1000))
        with patch.object(cc, "time") as fake_time:
            fake_time.time.return_value = NOW
            token, error, _ = cc.access_token_for("odd", "me")
        self.assertEqual((token, error), ("token-odd", None))


class TestActiveRowReportsTheLiveLogin(AutoBaseTest):
    """Signing in again renews the live login but not the saved copy."""

    def setUp(self) -> None:
        super().setUp()
        # Live credentials: renewed. Saved profile: the old, expired copy.
        self._write_creds(_creds("token-me", refresh_expires=int((NOW + 30 * DAY) * 1000)))
        self._write_main(_account("me@example.com"))
        self._save_with_usage(
            "me", _creds("token-me", refresh_expires=int((NOW - DAY) * 1000)), _account("me@example.com")
        )
        cc.write_active("me")

    def _rows(self) -> dict[str, dict[str, object]]:
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))), contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=True))
        return {r["profile"]: r for r in json.loads(out.getvalue())}

    def test_the_active_row_uses_the_live_expiry(self) -> None:
        self.assertEqual(self._rows()["me"]["login_expires_at"], NOW + 30 * DAY)

    def test_it_does_not_report_the_stale_saved_expiry(self) -> None:
        """Otherwise `usage` tells the user a working login is expired."""
        self.assertNotEqual(self._rows()["me"]["login_expires_at"], NOW - DAY)

    def test_an_inactive_profile_still_uses_its_own(self) -> None:
        self._save_with_usage(
            "vlad", _creds("token-vlad", refresh_expires=int((NOW + DAY) * 1000)), _account("v@x")
        )
        self.assertEqual(self._rows()["vlad"]["login_expires_at"], NOW + DAY)

    def test_the_table_shows_the_live_date(self) -> None:
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))), contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=False))
        self.assertIn(_dt.datetime.fromtimestamp(NOW + 30 * DAY).strftime("%d %b"), out.getvalue())
        self.assertNotIn("EXPIRED", out.getvalue())


class TestPythonAndTheShellAgreeOnAToken:
    """Both sides must accept exactly the same values.

    When Python called `refreshToken: 123` healthy and the shell called it
    dead, every render woke a tick that then took the ordinary path and never
    armed the retry pause — one background process per render, forever.
    """

    def test_a_real_token_is_usable(self) -> None:
        assert cc.has_usable_refresh_token({"refreshToken": "r"})

    def test_a_number_is_not_usable(self) -> None:
        assert not cc.has_usable_refresh_token({"refreshToken": 123})

    def test_true_is_not_usable(self) -> None:
        assert not cc.has_usable_refresh_token({"refreshToken": True})

    def test_an_empty_string_is_not_usable(self) -> None:
        assert not cc.has_usable_refresh_token({"refreshToken": ""})

    def test_null_is_not_usable(self) -> None:
        assert not cc.has_usable_refresh_token({"refreshToken": None})

    def test_a_missing_field_is_not_usable(self) -> None:
        assert not cc.has_usable_refresh_token({})

    def test_a_non_dict_is_not_usable(self) -> None:
        assert not cc.has_usable_refresh_token("nonsense")


class TestANumericTokenIsDeadEverywhere(TickTestCase):
    """The divergence in its end-to-end form."""

    def setUp(self) -> None:
        super().setUp()
        self.creds_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": 123}}))

    def test_python_calls_it_dead(self) -> None:
        self.assertEqual(cc.live_credentials_dead(NOW), "no refresh token")

    def test_the_tick_evacuates_rather_than_looping(self) -> None:
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_with_nowhere_to_go_it_arms_the_pause(self) -> None:
        """Without this the shell would wake a tick on every render.

        Every saved copy has to be dead too — the marker's own profile is a
        place to go, and the rescue takes it.
        """
        self._profile_path("vlad").unlink()
        self._save_with_usage(
            "me", _creds("token-me", refresh_expires=int((NOW - DAY) * 1000)), _account("me@example.com")
        )
        cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.RETRY_SECONDS))

    def test_the_last_saved_copy_is_still_a_place_to_go(self) -> None:
        """The marker names it, but the marker is not evidence of anything."""
        self._profile_path("vlad").unlink()
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "me")
        # The settle window after a switch, not the retry pause of a failure.
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.SETTLE_SECONDS))


class TestEveryCommandReportsTheLiveExpiry(AutoBaseTest):
    """Signing in again renews the live login but not the saved copy.

    The rule has to hold in `current`, `list` and `usage` alike — applying it
    to one of them is what this class exists to prevent.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me", refresh_expires=int((NOW + 30 * DAY) * 1000)))
        self._write_main(_account("me@example.com"))
        self._save_with_usage(
            "me", _creds("token-me", refresh_expires=int((NOW - DAY) * 1000)), _account("me@example.com"), 10.0, 40.0
        )
        self._save_with_usage(
            "vlad", _creds("token-vlad", refresh_expires=int((NOW + DAY) * 1000)), _account("v@x"), 5.0, 20.0
        )
        cc.write_active("me")
        self.live_date = _dt.datetime.fromtimestamp(NOW + 30 * DAY).strftime("%d %b")

    def _out(self, fn: object, args: argparse.Namespace) -> str:
        buf = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))), contextlib.redirect_stdout(buf):
            fn(args)
        return buf.getvalue()

    @staticmethod
    def _row(text: str, name: str) -> str:
        """The line for one profile; the active marker shifts the columns."""
        return next(ln for ln in text.splitlines() if name in ln.split()[:2])

    def test_current_reports_the_live_expiry(self) -> None:
        text = self._out(cc.cmd_current, argparse.Namespace())
        self.assertIn(self.live_date, text)
        self.assertNotIn("EXPIRED", text)

    def test_list_reports_the_live_expiry(self) -> None:
        text = self._out(cc.cmd_list, argparse.Namespace())
        self.assertIn(self.live_date, text)
        self.assertNotIn("EXPIRED", text)

    def test_usage_reports_the_live_expiry(self) -> None:
        text = self._out(cc.cmd_usage, argparse.Namespace(json=False))
        self.assertIn(self.live_date, text)

    def test_usage_fetches_live_limits_for_the_active_account(self) -> None:
        """A stale saved expiry must not downgrade it to stored numbers."""
        text = self._out(cc.cmd_usage, argparse.Namespace(json=False))
        me_row = next(ln for ln in text.splitlines() if " me " in ln)
        self.assertIn("active", me_row)
        self.assertNotIn("(stored)", me_row)
        self.assertNotIn("refresh token expired", me_row)

    def test_the_helper_agrees_for_the_active_account(self) -> None:
        self.assertEqual(cc.login_status_for("me", "me", NOW)[1], NOW + 30 * DAY)

    def test_the_helper_uses_the_saved_copy_for_the_others(self) -> None:
        self.assertEqual(cc.login_status_for("vlad", "me", NOW)[1], NOW + DAY)

    def test_list_does_not_call_the_live_login_dead(self) -> None:
        """The date and the verdict have to come from the same copy.

        Reading one from the live credentials and the other from the saved
        profile printed a future "login until" beside `! refresh token
        expired` — the exact state of anyone who has just signed in again.
        """
        me_row = self._row(self._out(cc.cmd_list, argparse.Namespace()), "me")
        self.assertIn(self.live_date, me_row)
        self.assertNotIn("refresh token expired", me_row)

    def test_current_does_not_call_the_live_login_dead(self) -> None:
        text = self._out(cc.cmd_current, argparse.Namespace())
        self.assertIn(self.live_date, text)
        self.assertNotIn("refresh token expired", text)

    def test_usage_does_not_call_the_live_login_dead(self) -> None:
        me_row = self._row(self._out(cc.cmd_usage, argparse.Namespace(json=False)), "me")
        self.assertIn("active", me_row)
        self.assertNotIn("refresh token expired", me_row)

    def test_the_others_are_still_judged_by_their_saved_copy(self) -> None:
        """The rule is "use the live copy for the live account", not "excuse all"."""
        vlad_row = self._row(self._out(cc.cmd_list, argparse.Namespace()), "vlad")
        self.assertIn("login expires in 24h", vlad_row)

    def test_an_inactive_dead_profile_is_named_not_just_dated(self) -> None:
        self._save_with_usage(
            "dead", _creds("token-dead", refresh_expires=int((NOW - DAY) * 1000)), _account("d@x")
        )
        dead_row = self._row(self._out(cc.cmd_list, argparse.Namespace()), "dead")
        self.assertIn("! refresh token expired", dead_row)

    def test_an_inactive_expired_profile_is_still_flagged(self) -> None:
        """The rule must not blanket-excuse every profile."""
        self._save_with_usage(
            "old", _creds("token-old", refresh_expires=int((NOW - DAY) * 1000)), _account("o@x")
        )
        self.assertIn("EXPIRED", self._out(cc.cmd_list, argparse.Namespace()))


class TestTheWarningWindowFollowsTheLiveLogin(AutoBaseTest):
    """"Expires soon" is the same question as "expired", one step earlier."""

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me", refresh_expires=int((NOW + 6 * HOUR) * 1000)))
        self._write_main(_account("me@example.com"))
        self._save_with_usage(
            "me",
            _creds("token-me", refresh_expires=int((NOW + 30 * DAY) * 1000)),
            _account("me@example.com"),
            10.0,
            40.0,
        )
        cc.write_active("me")

    def test_the_live_login_raises_the_warning(self) -> None:
        """The saved copy says a month; only the live one is running out."""
        self.assertIn("login expires in 6h", _note("me", "me"))

    def test_the_saved_copy_alone_stays_quiet(self) -> None:
        self.assertEqual(_note("me", None), "")

    def test_list_shows_the_warning(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        self.assertIn("login expires in 6h", out.getvalue())


class TestUnknownLoginExpiryIsNotInvented(AutoBaseTest):
    """The sentinel deadline is a signal to the shell, not a date.

    Formatting it produced "01 Jan 00:00 (EXPIRED)" — a fabricated moment
    presented to the user as fact.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        cc.write_active("me")

    def test_missing_credentials_report_unknown(self) -> None:
        self.assertIsNone(cc.login_status_for("me", "me", NOW)[1])

    def test_damaged_credentials_report_unknown(self) -> None:
        self.creds_file.write_text("{not json")
        self.assertIsNone(cc.login_status_for("me", "me", NOW)[1])

    def test_credentials_without_a_token_report_unknown(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a"}}))
        self.assertIsNone(cc.login_status_for("me", "me", NOW)[1])

    def test_current_says_unknown_rather_than_a_date(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_current(argparse.Namespace())
        self.assertIn("unknown", out.getvalue())
        self.assertNotIn("01 Jan", out.getvalue())

    def test_list_says_unknown_rather_than_a_date(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        self.assertIn("unknown", out.getvalue())
        self.assertNotIn("01 Jan", out.getvalue())

    def test_list_names_why_the_live_login_is_unusable(self) -> None:
        """"unknown" alone leaves the user with no idea what to do."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        self.assertIn("! no credentials", out.getvalue())

    def test_current_names_why_the_live_login_is_unusable(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_current(argparse.Namespace())
        self.assertIn("! no credentials", out.getvalue())

    def test_usage_reports_the_live_reason_without_fetching(self) -> None:
        """Asking the API with a dead login only turns it into an HTTP error."""
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", side_effect=AssertionError("must not fetch")):
            with contextlib.redirect_stdout(out):
                cc.cmd_usage(argparse.Namespace(json=False))
        self.assertIn("no credentials", out.getvalue())

    def test_the_sentinel_never_reaches_formatting(self) -> None:
        """Whatever the status reports, no caller may render the sentinel."""
        self.assertIn("EXPIRED", cc._fmt_login_expiry(cc.DEADLINE_NOW, NOW))
        self.assertIsNone(cc.login_status_for("me", "me", NOW)[1])

    def test_a_healthy_login_still_reports_its_date(self) -> None:
        self._write_creds(_creds("token-me", refresh_expires=int((NOW + DAY) * 1000)))
        self.assertEqual(cc.login_status_for("me", "me", NOW)[1], NOW + DAY)


class TestTheTwoAccountFilesNeverDisagree(AutoBaseTest):
    """A half-applied switch is worse than a refused one.

    The credentials and the account object must describe the same login; a
    mismatch is a state nothing in the tool knows how to interpret.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"), 5.0, 20.0)
        cc.write_active("me")

    def _fail_the_second_write(self) -> object:
        real = cc.atomic_write_json

        def _inner(path: Path, data: dict, indent: int, trailing_nl: bool) -> None:
            if path == self.main_file:
                raise SystemExit(2)
            real(path, data, indent, trailing_nl)

        return patch.object(cc, "atomic_write_json", _inner)

    def test_the_credentials_are_rolled_back(self) -> None:
        with self._fail_the_second_write(), self.assertRaises(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        creds = json.loads(self.creds_file.read_text())
        self.assertEqual(creds["claudeAiOauth"]["accessToken"], "token-me")

    def test_the_two_files_still_agree(self) -> None:
        with self._fail_the_second_write(), self.assertRaises(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(json.loads(self.main_file.read_text())["oauthAccount"]["emailAddress"], "me@example.com")
        self.assertEqual(cc.resolve_active(), "me")

    def test_damaged_outgoing_credentials_are_removed_not_mismatched(self) -> None:
        """With nothing to restore, the coherent outcome is no credentials.

        That is a dead login, which the tool already escapes from — unlike a
        credentials file paired with somebody else's account object.
        """
        self.creds_file.write_text("{not json")
        with self._fail_the_second_write(), self.assertRaises(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertFalse(self.creds_file.exists())
        self.assertEqual(json.loads(self.main_file.read_text())["oauthAccount"]["emailAddress"], "me@example.com")

    def test_that_state_is_recognised_as_a_dead_login(self) -> None:
        self.creds_file.write_text("{not json")
        with self._fail_the_second_write(), self.assertRaises(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(cc.live_credentials_dead(NOW), "no credentials")

    def _fail_every_credentials_write(self) -> object:
        """The disk that broke the first write can break the rollback too."""
        real = cc.atomic_write_json

        def _inner(path: Path, data: dict, indent: int, trailing_nl: bool) -> None:
            if path == self.main_file:
                raise SystemExit(2)
            if path == self.creds_file and data.get("claudeAiOauth", {}).get("accessToken") == "token-me":
                raise OSError(28, "No space left on device")
            real(path, data, indent, trailing_nl)

        return patch.object(cc, "atomic_write_json", _inner)

    def test_a_failed_rollback_removes_the_credentials(self) -> None:
        """Restoring can fail in its own right; a mismatched pair may not stand."""
        with self._fail_every_credentials_write(), self.assertRaises(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertFalse(self.creds_file.exists())
        self.assertEqual(json.loads(self.main_file.read_text())["oauthAccount"]["emailAddress"], "me@example.com")

    def test_a_failed_rollback_leaves_a_state_the_tool_escapes_from(self) -> None:
        with self._fail_every_credentials_write(), self.assertRaises(SystemExit), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(cc.live_credentials_dead(NOW), "no credentials")

    def test_a_removal_that_also_fails_is_surfaced(self) -> None:
        """Silence here would leave exactly the pair this all exists to prevent."""
        with self._fail_every_credentials_write(), patch.object(
            Path, "unlink", side_effect=OSError(30, "Read-only file system")
        ), self.assertRaises(OSError), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))

    def test_an_unexpected_error_rolls_back_too(self) -> None:
        real = cc.atomic_write_json

        def _inner(path: Path, data: dict, indent: int, trailing_nl: bool) -> None:
            if path == self.main_file:
                raise RuntimeError("disk gremlin")
            real(path, data, indent, trailing_nl)

        with patch.object(cc, "atomic_write_json", _inner), self.assertRaises(RuntimeError), _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(json.loads(self.creds_file.read_text())["claudeAiOauth"]["accessToken"], "token-me")

    def test_a_successful_switch_writes_both(self) -> None:
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(json.loads(self.creds_file.read_text())["claudeAiOauth"]["accessToken"], "token-vlad")
        self.assertEqual(json.loads(self.main_file.read_text())["oauthAccount"]["emailAddress"], "vlad@example.com")


class TestRankingTreatsUnusableExpiriesConsistently(AutoBaseTest):
    """`profile_unusable_reason` was a fourth copy of the same check.

    It read `refreshTokenExpiresAt: true` as epoch 0.001 and dropped an
    otherwise usable account from the pool as "expired".
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"), 10.0, 40.0)
        cc.write_active("me")

    def _candidate_with_expiry(self, value: object) -> None:
        self._save_with_usage("vlad", _creds("token-vlad"), _account("v@x"), 5.0, 20.0)
        path = self._profile_path("vlad")
        data = json.loads(path.read_text())
        data["credentials"]["claudeAiOauth"]["refreshTokenExpiresAt"] = value
        path.write_text(json.dumps(data, indent=2))

    def test_a_boolean_expiry_does_not_retire_the_profile(self) -> None:
        self._candidate_with_expiry(True)
        data = cc.load_profile_data("vlad")
        self.assertIsNone(cc.profile_unusable_reason(data, NOW))

    def test_a_huge_expiry_does_not_retire_the_profile(self) -> None:
        self._candidate_with_expiry(1e300)
        self.assertIsNone(cc.profile_unusable_reason(cc.load_profile_data("vlad"), NOW))

    def test_such_a_profile_is_still_rankable(self) -> None:
        self._candidate_with_expiry(True)
        self.assertEqual([c.name for c in cc.rank_candidates(NOW, "me")], ["vlad"])

    def test_a_genuinely_expired_profile_is_still_retired(self) -> None:
        self._candidate_with_expiry(int((NOW - 1) * 1000))
        self.assertEqual(cc.profile_unusable_reason(cc.load_profile_data("vlad"), NOW), "refresh token expired")


class TestMalformedExpiryNeverCrashesTheDisplay(AutoBaseTest):
    """`list` and `usage` must survive any value a profile can hold."""

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        cc.write_active("me")

    def _profile_with(self, value: object) -> None:
        cc.save_profile("odd", _creds("token-odd"), _account("odd@x"))
        path = self._profile_path("odd")
        data = json.loads(path.read_text())
        data["credentials"]["claudeAiOauth"]["refreshTokenExpiresAt"] = value
        path.write_text(json.dumps(data, indent=2))

    def _list_output(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_list(argparse.Namespace())
        return out.getvalue()

    def _usage_output(self) -> str:
        out = io.StringIO()
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(1.0, 2.0))), contextlib.redirect_stdout(out):
            cc.cmd_usage(argparse.Namespace(json=False))
        return out.getvalue()

    def test_list_survives_a_huge_expiry(self) -> None:
        self._profile_with(1e300)
        self.assertIn("unknown", self._list_output())

    def test_usage_survives_a_huge_expiry(self) -> None:
        self._profile_with(1e300)
        self.assertIn("unknown", self._usage_output())

    def test_list_survives_a_boolean_expiry(self) -> None:
        self._profile_with(True)
        self.assertIn("unknown", self._list_output())

    def test_list_survives_a_negative_expiry(self) -> None:
        self._profile_with(-5)
        self.assertIn("unknown", self._list_output())

    def test_current_survives_a_huge_expiry(self) -> None:
        cc.save_profile("me", _creds("token-me"), _account("me@example.com"))
        path = self._profile_path("me")
        data = json.loads(path.read_text())
        data["credentials"]["claudeAiOauth"]["refreshTokenExpiresAt"] = 1e300
        path.write_text(json.dumps(data, indent=2))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.cmd_current(argparse.Namespace())
        self.assertIn("unknown", out.getvalue())

    def test_a_boolean_expiry_does_not_evacuate_a_working_login(self) -> None:
        """The worst outcome: a healthy account abandoned over a typo."""
        self.creds_file.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "refreshTokenExpiresAt": True}})
        )
        self.assertIsNone(cc.live_credentials_dead(NOW))

    def test_a_huge_expiry_does_not_evacuate_a_working_login(self) -> None:
        self.creds_file.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "refreshTokenExpiresAt": 1e300}})
        )
        self.assertIsNone(cc.live_credentials_dead(NOW))


class TestLiveCredentialsDeadEdgeCases(AutoBaseTest):
    """`live_credentials_dead` decides whether a session can run at all.

    A wrong "dead" here evacuates a perfectly good account; a wrong "alive"
    strands the session. Both directions are pinned, including the JSON
    special floats that parse without raising.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_main(_account("me@example.com"))

    def _write_expiry(self, value: object) -> None:
        self.creds_file.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "refreshTokenExpiresAt": value}})
        )

    def test_a_nan_expiry_is_not_treated_as_dead(self) -> None:
        """NaN loses every comparison, so it must not decide anything."""
        self._write_expiry(float("nan"))
        self.assertIsNone(cc.live_credentials_dead(NOW))

    def test_an_infinite_expiry_is_not_treated_as_dead(self) -> None:
        self._write_expiry(float("inf"))
        self.assertIsNone(cc.live_credentials_dead(NOW))

    def test_a_negative_infinite_expiry_is_not_treated_as_dead(self) -> None:
        self._write_expiry(float("-inf"))
        self.assertIsNone(cc.live_credentials_dead(NOW))

    def test_a_string_expiry_is_not_treated_as_dead(self) -> None:
        self._write_expiry("soon")
        self.assertIsNone(cc.live_credentials_dead(NOW))

    def test_an_expiry_exactly_now_is_dead(self) -> None:
        self._write_expiry(int(NOW * 1000))
        self.assertEqual(cc.live_credentials_dead(NOW), "login expired")

    def test_an_expiry_one_second_ahead_is_alive(self) -> None:
        self._write_expiry(int((NOW + 1) * 1000))
        self.assertIsNone(cc.live_credentials_dead(NOW))

    def test_a_missing_file_is_dead(self) -> None:
        self.assertEqual(cc.live_credentials_dead(NOW), "no credentials")

    def test_a_damaged_file_is_dead(self) -> None:
        self.creds_file.write_text("{not json")
        self.assertEqual(cc.live_credentials_dead(NOW), "unreadable credentials")

    def test_a_non_object_file_is_dead(self) -> None:
        self.creds_file.write_text("[1, 2]")
        self.assertEqual(cc.live_credentials_dead(NOW), "unreadable credentials")

    def test_credentials_without_a_refresh_token_are_dead(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a"}}))
        self.assertEqual(cc.live_credentials_dead(NOW), "no refresh token")

    def test_a_non_dict_oauth_block_is_dead(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": "nonsense"}))
        self.assertEqual(cc.live_credentials_dead(NOW), "no refresh token")


class TestAnExhaustionDeadlineDoesNotTrapADeadLogin(TickTestCase):
    """Being at a limit is no reason to stay logged out.

    `.exhausted` records when the accounts were all full, and it can be days
    out. A dead login outranks limits everywhere else in the decision, but
    the pre-lock deadline check ran first and returned before that ordering
    was ever consulted.
    """

    def setUp(self) -> None:
        super().setUp()
        cc.write_epoch_file(self.exhausted_file, NOW + 3 * DAY)

    def test_a_dead_login_still_evacuates(self) -> None:
        self._write_creds(_creds("token-me", refresh_expires=int((NOW - 1) * 1000)))
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_missing_credentials_still_evacuate(self) -> None:
        self.creds_file.unlink()
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "vlad")

    def test_a_healthy_login_is_still_held_back(self) -> None:
        """The deadline keeps doing its job for the case it was written for."""
        with self._patch_usage(1.0, 1.0):
            cc.cmd_tick(self._tick_args(99.0, 99.0))
        self.assertEqual(cc.read_active(), "me")

    def test_the_settle_window_still_holds_a_dead_login(self) -> None:
        """That barrier stops a switch undoing itself; it is not a limit."""
        self._write_creds(_creds("token-me", refresh_expires=int((NOW - 1) * 1000)))
        cc.write_epoch_file(self.settle_file, NOW + 30)
        with self._patch_usage(5.0, 20.0):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "me")

    def test_with_nowhere_to_go_it_still_backs_off(self) -> None:
        """No spin: the retry pause replaces the deadline that was bypassed."""
        self.creds_file.unlink()
        self._profile_path("vlad").unlink()
        self._save_with_usage(
            "me", _creds("token-me", refresh_expires=int((NOW - DAY) * 1000)), _account("me@example.com")
        )
        cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.RETRY_SECONDS))


class TestDeadLoginBacksOffWhenTrapped(TickTestCase):
    """A dead login with nowhere to go must not spawn a tick per render.

    The login deadline is already in the past, so without a pause the
    statusline would relaunch cc-switch on every single render — network
    checks and all — for as long as the situation lasts.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me", refresh_expires=int((NOW - 1) * 1000)))

    def test_a_pause_is_armed_when_no_candidate_works(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(0, {"error": "offline"})):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.RETRY_SECONDS))

    def test_a_pause_is_armed_when_every_candidate_is_full(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(10.0, cc.ENTER_7D + 5))):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "me")
        self.assertEqual(cc.read_epoch_file(self.settle_file), int(NOW + cc.RETRY_SECONDS))

    def test_the_reason_is_logged(self) -> None:
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(10.0, cc.ENTER_7D + 5))):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertIn("no working account to escape to", self._log_text())

    def test_no_exhaustion_deadline_is_written(self) -> None:
        """A dead login is not evidence that the other accounts are spent."""
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(10.0, cc.ENTER_7D + 5))):
            cc.cmd_tick(self._tick_args(0.0, 0.0, _iso(NOW + DAY), _iso(NOW + DAY)))
        self.assertFalse(self.exhausted_file.exists())

    def test_the_pause_blocks_the_immediate_next_tick(self) -> None:
        """The point of the back-off: the next render costs nothing."""
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(10.0, cc.ENTER_7D + 5))):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        armed = cc.read_epoch_file(self.settle_file)
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))) as usage:
            rc = cc.cmd_tick(self._tick_args(0.0, 0.0))
        usage.assert_not_called()
        self.assertEqual(rc, cc.EXIT_OK)
        self.assertEqual(cc.read_active(), "me")
        self.assertEqual(cc.read_epoch_file(self.settle_file), armed)

    def test_a_working_candidate_still_wins_immediately(self) -> None:
        """The back-off must not delay a rescue that is actually available."""
        with patch.object(cc, "fetch_usage", return_value=(200, _api_usage(5.0, 20.0))):
            cc.cmd_tick(self._tick_args(0.0, 0.0))
        self.assertEqual(cc.read_active(), "vlad")


class TestSwitchReasonRanking(unittest.TestCase):
    def test_expired_outranks_everything(self) -> None:
        self.assertEqual(cc.switch_reason(1.0, 1.0, 1.0, expired=True), "expired")

    def test_expired_outranks_a_limit(self) -> None:
        self.assertEqual(cc.switch_reason(cc.EXIT_5H, 10.0, 5.0, expired=True), "expired")

    def test_without_the_flag_the_old_order_holds(self) -> None:
        self.assertEqual(cc.switch_reason(cc.EXIT_5H, 10.0, 5.0), "limit")
        self.assertIsNone(cc.switch_reason(1.0, 1.0, 1.0))


class TestGateCarriesTheLoginDeadline(AutoBaseTest):
    """The only trigger that can fire once no usage is reported."""

    def setUp(self) -> None:
        super().setUp()
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        cc.write_active("me")

    def test_the_deadline_is_written(self) -> None:
        self._write_creds(_creds("token-me", refresh_expires=int((NOW + DAY) * 1000)))
        cc.recompute_gate("me", NOW)
        self.assertEqual(int(self.gate_file.read_text().split()[4]), int(NOW + DAY))

    def test_an_unknown_deadline_is_zero(self) -> None:
        """A login saved before expiries were recorded: nothing to go on."""
        self._write_creds(_creds("token-me"))  # no refreshTokenExpiresAt
        cc.recompute_gate("me", NOW)
        self.assertEqual(int(self.gate_file.read_text().split()[4]), 0)

    def test_missing_credentials_give_a_past_deadline(self) -> None:
        """Damaged is not unknown: it is a reason to act, not to wait."""
        cc.recompute_gate("me", NOW)
        deadline = int(self.gate_file.read_text().split()[4])
        self.assertEqual(deadline, int(cc.DEADLINE_NOW))
        self.assertLess(deadline, NOW)

    def test_unreadable_credentials_give_a_past_deadline(self) -> None:
        """The shell cannot parse JSON — this is how it learns to wake a tick."""
        self.creds_file.write_text("{not json")
        self.assertEqual(cc.live_login_deadline(), cc.DEADLINE_NOW)

    def test_credentials_without_a_refresh_token_give_a_past_deadline(self) -> None:
        self.creds_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a"}}))
        self.assertEqual(cc.live_login_deadline(), cc.DEADLINE_NOW)

    def test_a_non_finite_deadline_is_zero(self) -> None:
        """Readable credentials with a nonsense expiry: unknown, not damaged."""
        self.creds_file.write_text(
            json.dumps({"claudeAiOauth": {"refreshToken": "r", "refreshTokenExpiresAt": float("inf")}})
        )
        self.assertEqual(cc.live_login_deadline(), 0.0)

    def test_an_out_of_range_deadline_is_zero(self) -> None:
        self.creds_file.write_text(
            json.dumps({"claudeAiOauth": {"refreshToken": "r", "refreshTokenExpiresAt": 1e300}})
        )
        self.assertEqual(cc.live_login_deadline(), 0.0)

    def test_a_boolean_deadline_is_zero(self) -> None:
        """JSON true is a Python int; epoch 0.001 would look long expired."""
        self.creds_file.write_text(
            json.dumps({"claudeAiOauth": {"refreshToken": "r", "refreshTokenExpiresAt": True}})
        )
        self.assertEqual(cc.live_login_deadline(), 0.0)

    def test_the_statusline_wakes_a_tick_past_the_deadline(self) -> None:
        script = STATUSLINE_SCRIPT.read_text()
        self.assertIn("LOGIN_DEADLINE", script)
        self.assertIn('tick --5h 0 --7d 0', script)

    def test_the_statusline_reads_the_fifth_field(self) -> None:
        """Behaviour lives in tests/test_statusline.py; this pins the shape.

        Both reads must name a trailing variable, because `read` folds every
        unconsumed field into the last one — which is how adding this fifth
        field silently broke every percentage comparison.
        """
        script = STATUSLINE_SCRIPT.read_text()
        self.assertIn("LOGIN_DEADLINE GATE_SETTLE _REST < \"$PROFILES_DIR/.gate\"", script)
        self.assertIn("T7D _REST < \"$PROFILES_DIR/.gate\"", script)

    def test_the_statusline_also_notices_missing_credentials(self) -> None:
        """A deleted credentials file has no deadline to wait for."""
        script = STATUSLINE_SCRIPT.read_text()
        self.assertIn('if [ ! -r "$CREDS_FILE" ]; then', script)
        self.assertIn("DEAD=1", script)

    def test_the_statusline_notices_damaged_credentials(self) -> None:
        """Corrupted after the deadline was recorded: neither test would fire.

        The shell cannot parse JSON, but the field that decides everything is
        a plain substring, and both `read` and `case` are builtins.
        """
        script = STATUSLINE_SCRIPT.read_text()
        self.assertIn('IFS= read -r -d "" CREDS_BLOB < "$CREDS_FILE"', script)
        # The quote and colon are the point: `refreshTokenExpiresAt` contains
        # `refreshToken`, so the looser pattern called a dead file alive.
        self.assertIn("*'\"refreshToken\":\"'*", script)
        self.assertIn("*'\"refreshToken\":\"\"'*", script)
        # Matched against the compacted copy, so one spelling covers every
        # amount of whitespace JSON allows around the colon.
        self.assertIn("COMPACT=${CREDS_BLOB//[[:space:]]/}", script)
        self.assertNotIn('*\'"refreshToken": "\'*', script)

    def test_only_one_tick_is_spawned_per_render(self) -> None:
        """A dead login with a stale usage payload once matched both gates."""
        script = STATUSLINE_SCRIPT.read_text()
        self.assertIn('[ -z "$SPAWNED_DEAD" ]', script)
        self.assertIn("SPAWNED_DEAD=1", script)

    def test_the_dead_login_spawn_respects_the_settle_window(self) -> None:
        """Otherwise a dead login with nowhere to go spawns on every render."""
        script = STATUSLINE_SCRIPT.read_text()
        self.assertIn('[ "$NOW" -ge "${GATE_SETTLE:-0}" ]', script)


class TestCacheWriteLosesToAConcurrentSwitch(AutoBaseTest):
    """An unlocked reader must not overwrite the cache a switch just wrote.

    `current` and `list` resolve without the lock. If a switch completes in
    between, the reader's answer is already obsolete — writing it would leave
    the statusline naming the old account until something resolved again.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_creds(_creds("token-me"))
        self._write_main(_account("me@example.com"))
        self._save_with_usage("me", _creds("token-me"), _account("me@example.com"))
        self._save_with_usage("vlad", _creds("token-vlad"), _account("vlad@example.com"))
        cc.write_active("me")

    def _switch_happens_now(self) -> None:
        """Simulate another process completing a switch to vlad."""
        self._write_creds(_creds("token-vlad"))
        self._write_main(_account("vlad@example.com"))
        cc.write_active("vlad")
        self.live_file.write_text("vlad\n")

    def test_a_stale_answer_is_not_written(self) -> None:
        original = cc._resolve_active_uncached

        def _resolve_then_switch() -> str | None:
            answer = original()
            self._switch_happens_now()
            return answer

        with patch.object(cc, "_resolve_active_uncached", _resolve_then_switch):
            cc.resolve_active()
        self.assertEqual(self.live_file.read_text().strip(), "vlad")

    def test_an_uncontended_answer_is_written(self) -> None:
        """The guard must not block the ordinary path."""
        cc.resolve_active()
        self.assertEqual(self.live_file.read_text().strip(), "me")

    def test_a_switch_of_its_own_still_updates_the_cache(self) -> None:
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(self.live_file.read_text().strip(), "vlad")

    def test_a_switch_landing_mid_write_is_repaired(self) -> None:
        """The pre-write check narrows the race; this closes what is left.

        The switch lands *during* the reader's write, so the reader's stale
        value ("me") is what hits disk last. Only the post-write check can
        put the live account back into a file the statusline prefers over
        `.active`.
        """
        original = cc.write_text_file
        switched: list[bool] = []

        def _switch_mid_write(path: object, text: str, fatal: bool = True) -> bool:
            if path == self.live_file and not switched:
                switched.append(True)
                self._switch_happens_now()  # writes "vlad"
            return original(path, text, fatal=fatal)  # then the stale "me" lands

        with patch.object(cc, "write_text_file", _switch_mid_write):
            cc.resolve_active()
        self.assertTrue(switched, "the race was never triggered")
        self.assertEqual(self.live_file.read_text().strip(), "vlad")

    def test_invalid_bytes_in_the_cache_do_not_crash_a_switch(self) -> None:
        """UnicodeDecodeError is a ValueError, not an OSError.

        Crashing here would report failure after the credentials had already
        been swapped — the switch happened, the command says it did not.
        """
        self.live_file.write_bytes(b"\xff\xfe")
        with _silence():
            cc.cmd_use(argparse.Namespace(name="vlad"))
        self.assertEqual(cc.read_active(), "vlad")
        self.assertEqual(self.live_file.read_text().strip(), "vlad")

    def test_invalid_bytes_in_the_cache_do_not_crash_a_read(self) -> None:
        self.live_file.write_bytes(b"\xff\xfe")
        self.assertEqual(cc.resolve_active(), "me")
        self.assertEqual(self.live_file.read_text().strip(), "me")

    def test_the_guard_is_skipped_when_no_email_was_captured(self) -> None:
        """Direct calls without a captured email still write — used by switches."""
        cc._cache_live_name("vlad")
        self.assertEqual(self.live_file.read_text().strip(), "vlad")


# `HOME` is the base the others are derived from; only the derived paths are
# redirected, and no test writes to `HOME` itself. Captured at import, before
# any test patches them.
_NOT_REDIRECTED = frozenset({"HOME"})
_REAL_PATHS = {
    name: Path(str(value))
    for name, value in vars(cc).items()
    if name.isupper() and isinstance(value, Path) and name not in _NOT_REDIRECTED
}


class TestSuiteNeverTouchesTheRealHome(unittest.TestCase):
    """Every module path must be redirected away from its real location.

    A path the base class forgets writes into the user's real files during an
    ordinary test run — silently, until it corrupts live credentials.
    """

    NOT_REDIRECTED = _NOT_REDIRECTED
    REAL_PATHS = _REAL_PATHS

    def test_the_real_paths_are_what_we_think_they_are(self) -> None:
        """Guards the guard: a renamed constant must not silently drop out."""
        self.assertGreaterEqual(len(self.REAL_PATHS), 12)
        self.assertIn("CREDS_FILE", self.REAL_PATHS)
        self.assertIn("LIVE_FILE", self.REAL_PATHS)
        self.assertNotIn("HOME", self.REAL_PATHS)

    def test_only_the_base_directory_is_exempt(self) -> None:
        """Every exemption must be one that cannot be written to."""
        self.assertEqual(self.NOT_REDIRECTED, frozenset({"HOME"}))
        self.assertEqual(Path(str(cc.HOME)), Path.home())

    def test_the_base_class_redirects_every_path_constant(self) -> None:
        """Compared against each path's REAL value, not against one directory.

        Checking only `~/.claude-profiles` would miss CREDS_FILE, MAIN_FILE
        and CLAUDE_DIR — they live elsewhere under $HOME, so a dropped patch
        on those would let tests overwrite real credentials and still pass.
        """

        class _Probe(BaseTest):
            def runTest(self) -> None:  # noqa: N802 - unittest API
                pass

        probe = _Probe()
        probe.setUp()
        try:
            for name, real in self.REAL_PATHS.items():
                current = Path(str(getattr(cc, name)))
                self.assertNotEqual(current, real, f"{name} still points at the real {real}")
                self.assertFalse(
                    current.is_relative_to(Path.home()),
                    f"{name} points inside the real home: {current}",
                )
        finally:
            probe.tearDown()

    def test_dropping_any_single_patch_would_be_caught(self) -> None:
        """The failure mode this class exists for, exercised directly."""

        class _Probe(BaseTest):
            def runTest(self) -> None:  # noqa: N802 - unittest API
                pass

        probe = _Probe()
        probe.setUp()
        try:
            for name in ("CREDS_FILE", "MAIN_FILE", "CLAUDE_DIR", "LIVE_FILE", "PROFILES_DIR"):
                real = self.REAL_PATHS[name]
                with patch.object(cc, name, real):
                    current = Path(str(getattr(cc, name)))
                    self.assertEqual(current, real)
                    self.assertTrue(current.is_relative_to(Path.home()))
        finally:
            probe.tearDown()


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

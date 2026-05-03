from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


import ast

from scripts.preflight_gate import (
    AssertFinding,
    AssertResult,
    CoverageFinding,
    CoverageResult,
    GateConfig,
    _attr_name,
    _count_assertions,
    _count_assertions_in_node,
    _diff_has_production_python,
    _format_report,
    _func_in_diff,
    _git_run,
    _cyan,
    _green,
    _has_skip_decorator,
    _load_config,
    _red,
    _run_assert_check,
    _run_assert_check_js,
    _run_coverage_check,
    _tag,
    _yellow,
    parse_added_lines,
    run_gate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _write_diff(repo: Path, diff: str) -> None:
    patch(
        "scripts.preflight_gate._git_run",
        side_effect=lambda args, cwd=None: diff if args[0] == "diff" else "",
    ).start()


def _write_pyproject(repo: Path, content: str) -> None:
    (repo / "pyproject.toml").write_text(content)


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------


def test_parse_added_lines_basic() -> None:
    diff = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,3 @@\n line1\n+line2\n line3\n"
    result = parse_added_lines(diff)
    assert result == {"foo.py": {2}}


def test_parse_added_lines_empty() -> None:
    assert parse_added_lines("") == {}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_config_defaults_when_missing_pyproject(tmp_path: Path) -> None:
    cfg = _load_config(tmp_path)
    assert cfg.enabled is True
    assert cfg.coverage_threshold == 100.0


def test_load_config_opt_out(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "[tool.code_review.coverage_gate]\nenabled = false\n")
    cfg = _load_config(tmp_path)
    assert cfg.enabled is False


def test_load_config_env_threshold_override(tmp_path: Path) -> None:
    os.environ["CODE_REVIEW_COVERAGE_THRESHOLD"] = "80"
    try:
        cfg = _load_config(tmp_path)
        assert cfg.coverage_threshold == 80.0
    finally:
        del os.environ["CODE_REVIEW_COVERAGE_THRESHOLD"]


def test_load_config_coverage_exclude_paths_as_string(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code_review.coverage_gate.coverage_check]\nexclude_paths = "*/tests/*"\n'
    )
    cfg = _load_config(tmp_path)
    assert cfg.coverage_exclude_paths == ("*/tests/*",)


def test_load_config_coverage_exclude_paths_as_list(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code_review.coverage_gate.coverage_check]\nexclude_paths = ["*/tests/*", "test_*.py"]\n'
    )
    cfg = _load_config(tmp_path)
    assert cfg.coverage_exclude_paths == ("*/tests/*", "test_*.py")


def test_load_config_coverage_exclude_legacy_alias(tmp_path: Path) -> None:
    """The `exclude` key is the legacy/concise alias for `exclude_paths`."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code_review.coverage_gate.coverage_check]\nexclude = ["*/tests/*"]\n'
    )
    cfg = _load_config(tmp_path)
    assert cfg.coverage_exclude_paths == ("*/tests/*",)


def test_resolve_compare_branch_env_override(tmp_path: Path) -> None:
    from scripts.preflight_gate import _resolve_compare_branch

    os.environ["CODE_REVIEW_COMPARE_BRANCH"] = "feature/x"
    try:
        cfg = _load_config(tmp_path)
        assert _resolve_compare_branch(cfg, tmp_path) == "feature/x"
    finally:
        del os.environ["CODE_REVIEW_COMPARE_BRANCH"]


def test_resolve_compare_branch_explicit_config_non_default(tmp_path: Path) -> None:
    """Explicit config value (not the legacy `origin/main` default) is trusted."""
    from scripts.preflight_gate import GateConfig as GC
    from scripts.preflight_gate import _resolve_compare_branch

    cfg = GC(compare_branch="develop")
    assert _resolve_compare_branch(cfg, tmp_path) == "develop"


# ---------------------------------------------------------------------------
# Coverage check
# ---------------------------------------------------------------------------


def test_coverage_check_missing_report(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    cfg = GateConfig(coverage_check_enabled=True, coverage_reports=("coverage.xml",))
    result = _run_coverage_check(cfg, repo)
    assert result.setup_error is not None
    assert "missing coverage report" in result.setup_error


def test_coverage_check_stale_report(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text("<coverage></coverage>")
    # Make report very old
    import time

    old_time = time.time() - 3600
    os.utime(report, (old_time, old_time))

    # staged file is newer
    staged = repo / "src.py"
    staged.write_text("x = 1")
    os.utime(staged, (time.time(), time.time()))

    with patch(
        "scripts.preflight_gate._git_run",
        side_effect=lambda args, cwd=None: "src.py\n" if "--name-only" in args else "",
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.setup_error is not None
    assert "stale" in result.setup_error


def test_coverage_check_no_branch_data(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage line-rate="1.0"></coverage>')

    with patch(
        "scripts.preflight_gate._git_run",
        side_effect=lambda args, cwd=None: "" if "--name-only" in args else "",
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.setup_error is not None
    assert "branch" in result.setup_error


def test_coverage_check_all_covered(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = {"src.py": {"violation_lines": []}}
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.findings == []


def test_coverage_check_one_uncovered_line(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = {"src.py": {"violation_lines": [7]}}
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert len(result.findings) == 1
    assert result.findings[0] == CoverageFinding("src.py", 7, "line")


def test_coverage_check_branch_missed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="0.5"></coverage>')

    json_out = {"src.py": {"violations": [{"line": 5, "kind": "branch"}]}}
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert any(f.kind == "branch" for f in result.findings)


def test_coverage_check_diff_cover_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.subprocess.run", side_effect=FileNotFoundError("diff-cover")),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.setup_error is not None
    assert "diff-cover not on PATH" in result.setup_error


def test_coverage_check_diff_cover_crash(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    class FakeProc:
        returncode = 2
        stderr = "ambiguous ref"

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.subprocess.run", return_value=FakeProc()),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.internal_error is not None
    assert "git fetch" in result.internal_error


# ---------------------------------------------------------------------------
# Assert check — Python
# ---------------------------------------------------------------------------


def test_assert_check_new_test_with_assertion() -> None:
    # Use `assert x` (Name) not `assert True` (Constant) — the latter is now
    # caught by trivial_assertion_check and would count as zero assertions.
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    assert x\n"
    )
    with patch(
        "scripts.preflight_gate._git_run",
        side_effect=lambda args, cwd=None: diff if args[0] == "diff" else "def test_x():\n    assert x\n",
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_assert_check_new_test_no_assertion() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    pass\n"
    )
    with patch(
        "scripts.preflight_gate._git_run",
        side_effect=lambda args, cwd=None: diff if args[0] == "diff" else "def test_x():\n    pass\n",
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert len(result.findings) == 1
    assert result.findings[0].function_name == "test_x"


def test_assert_check_self_assert_equal() -> None:
    # `assertEqual(x, 1)` not `assertEqual(1, 1)` — the latter is now caught
    # by trivial_assertion_check (both args identical Constants).
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x(self):\n"
        "+    self.assertEqual(x, 1)\n"
    )
    src = "def test_x(self):\n    self.assertEqual(x, 1)\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_assert_check_mock_assert_called_with() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    mock.assert_called_with(1)\n"
    )
    src = "def test_x():\n    mock.assert_called_with(1)\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_assert_check_pytest_raises() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+def test_x():\n"
        "+    with pytest.raises(ValueError):\n"
        "+        foo()\n"
    )
    src = "def test_x():\n    with pytest.raises(ValueError):\n        foo()\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(count_pytest_raises_as_assertion=True), Path("/tmp"))
    assert result.findings == []


def test_assert_check_pytest_fail() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    pytest.fail('not impl')\n"
    )
    src = "def test_x():\n    pytest.fail('not impl')\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_assert_check_self_fail() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x(self):\n"
        "+    self.fail('foo')\n"
    )
    src = "def test_x(self):\n    self.fail('foo')\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_assert_check_custom_helper_whitelisted() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    _verify_state(obj)\n"
    )
    src = "def test_x():\n    _verify_state(obj)\n"
    cfg = GateConfig(custom_assertion_helpers=frozenset({"_verify_state"}))
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(cfg, Path("/tmp"))
    assert result.findings == []


def test_assert_check_custom_helper_not_whitelisted() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    _verify_state(obj)\n"
    )
    src = "def test_x():\n    _verify_state(obj)\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert len(result.findings) == 1


def test_assert_check_async_test() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+async def test_x():\n"
        "+    assert x\n"
    )
    src = "async def test_x():\n    assert x\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_assert_check_class_test_method() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+class TestFoo:\n"
        "+    def test_y(self):\n"
        "+        self.assertTrue(x)\n"
    )
    src = "class TestFoo:\n    def test_y(self):\n        self.assertTrue(x)\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_assert_check_pytest_mark_skip() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+@pytest.mark.skip\n"
        "+def test_x():\n"
        "+    pass\n"
    )
    src = "@pytest.mark.skip\ndef test_x():\n    pass\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_assert_check_unittest_skip() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+@unittest.skip('reason')\n"
        "+def test_x():\n"
        "+    pass\n"
    )
    src = "@unittest.skip('reason')\ndef test_x():\n    pass\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_assert_check_pytest_xfail_scanned() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+@pytest.mark.xfail\n"
        "+def test_x():\n"
        "+    pass\n"
    )
    src = "@pytest.mark.xfail\ndef test_x():\n    pass\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert len(result.findings) == 1


def test_assert_check_helper_not_scanned() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+def _verify():\n"
        "+    assert x\n"
        "+def test_x(): pass\n"
    )
    src = "def _verify():\n    assert x\ndef test_x(): pass\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert len(result.findings) == 1  # test_x has 0 assertions; _verify is not scanned


def test_assert_check_syntax_error() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x(:\n"
        "+    pass\n"
    )
    src = "def test_x(:\n    pass\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.setup_error is not None
    assert "SyntaxError" in result.setup_error


def test_assert_check_deleted_file_skipped() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    pass\n"
    )

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff":
            return diff
        raise subprocess.CalledProcessError(128, ["git", "show"])

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_run_gate_both_ok() -> None:
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._run_coverage_check", return_value=CoverageResult()),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        assert run_gate() == 0


def test_run_gate_coverage_fails_assert_ok() -> None:
    cov = CoverageResult(findings=[CoverageFinding("a.py", 1, "line")])
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        assert run_gate() == 1


def test_run_gate_assert_fails_coverage_ok() -> None:
    ass = AssertResult(findings=[AssertFinding("t.py", "test_x", 1)])
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._run_coverage_check", return_value=CoverageResult()),
        patch("scripts.preflight_gate._run_assert_check", return_value=ass),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        assert run_gate() == 1


def test_run_gate_setup_error() -> None:
    cov = CoverageResult(setup_error="missing report")
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        assert run_gate() == 2


def test_run_gate_internal_error_fail_open() -> None:
    cov = CoverageResult(internal_error="boom")
    cfg = GateConfig(fail_open_on_error=True)
    with (
        patch("scripts.preflight_gate._load_config", return_value=cfg),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        assert run_gate() == 0


def test_run_gate_internal_error_fail_closed() -> None:
    cov = CoverageResult(internal_error="boom")
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig(fail_open_on_error=False)),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        assert run_gate() == 3


# ---------------------------------------------------------------------------
# Bypass
# ---------------------------------------------------------------------------


def test_run_gate_skip_gate_env() -> None:
    os.environ["CODE_REVIEW_SKIP_GATE"] = "1"
    try:
        assert run_gate() == 0
    finally:
        del os.environ["CODE_REVIEW_SKIP_GATE"]


def test_run_gate_skip_coverage_env() -> None:
    os.environ["CODE_REVIEW_SKIP_COVERAGE"] = "1"
    try:
        with (
            patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
            patch("scripts.preflight_gate._run_coverage_check") as m_cov,
            patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
            patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
        ):
            rc = run_gate()
            assert rc == 0
            m_cov.assert_not_called()
    finally:
        del os.environ["CODE_REVIEW_SKIP_COVERAGE"]


def test_run_gate_skip_assert_env() -> None:
    os.environ["CODE_REVIEW_SKIP_ASSERT"] = "1"
    try:
        with (
            patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
            patch("scripts.preflight_gate._run_coverage_check", return_value=CoverageResult()),
            patch("scripts.preflight_gate._run_assert_check") as m_ass,
            patch("scripts.preflight_gate._run_assert_check_js") as m_js,
        ):
            rc = run_gate()
            assert rc == 0
            m_ass.assert_not_called()
            m_js.assert_not_called()
    finally:
        del os.environ["CODE_REVIEW_SKIP_ASSERT"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_run_gate_empty_diff() -> None:
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate._run_coverage_check", return_value=CoverageResult()),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        assert run_gate() == 0


def test_run_gate_diff_only_deletions() -> None:
    diff = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,1 @@\n-line1\n line2\n"
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._git_run", return_value=diff),
        patch("scripts.preflight_gate._run_coverage_check", return_value=CoverageResult()),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        assert run_gate() == 0


def test_run_gate_diff_only_md_files() -> None:
    diff = "diff --git a/readme.md b/readme.md\n--- a/readme.md\n+++ b/readme.md\n@@ -1,1 +1,2 @@\n+hello\n"
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._git_run", return_value=diff),
        patch("scripts.preflight_gate._run_coverage_check", return_value=CoverageResult()),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        assert run_gate() == 0


# ---------------------------------------------------------------------------
# Formatting / internal helpers
# ---------------------------------------------------------------------------


def test_format_report_findings() -> None:
    cov = CoverageResult(findings=[CoverageFinding("a.py", 1, "line")])
    ass = AssertResult(findings=[AssertFinding("t.py", "test_x", 2)])
    report = _format_report(cov, ass, GateConfig(output_format="plain"))
    assert "1 uncovered" in report
    assert "Assert gate — 1 finding" in report
    assert "[missing assertion]" in report


def test_format_report_empty() -> None:
    report = _format_report(CoverageResult(), AssertResult(), GateConfig(output_format="plain"))
    assert "OK" in report


def test_func_in_diff_intersects() -> None:
    import ast

    tree = ast.parse("def test_x():\n    pass\n")
    func = tree.body[0]
    assert _func_in_diff(func, {1, 2})


def test_func_in_diff_no_intersection() -> None:
    import ast

    tree = ast.parse("def test_x():\n    pass\n")
    func = tree.body[0]
    assert not _func_in_diff(func, {5, 6})


def test_has_skip_decorator_pytest_mark_skip() -> None:
    import ast

    tree = ast.parse("@pytest.mark.skip\ndef test_x(): pass\n")
    func = tree.body[0]
    assert _has_skip_decorator(func)


def test_has_skip_decorator_no_skip() -> None:
    import ast

    tree = ast.parse("def test_x(): pass\n")
    func = tree.body[0]
    assert not _has_skip_decorator(func)


def test_count_assertions_simple() -> None:
    import ast

    # `assert x` (Name) — not caught by trivial_assertion_check.
    tree = ast.parse("def test_x():\n    assert x\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 1


def test_count_assertions_nested_def_ignored() -> None:
    import ast

    # The nested helper's assertion (even a non-trivial one) must be ignored.
    tree = ast.parse("def test_x():\n    def helper():\n        assert x\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


# ---------------------------------------------------------------------------
# C5 — non-numeric env threshold
# ---------------------------------------------------------------------------


def test_load_config_non_numeric_env_threshold(tmp_path: Path) -> None:
    os.environ["CODE_REVIEW_COVERAGE_THRESHOLD"] = "abc"
    try:
        cfg = _load_config(tmp_path)
        assert cfg.threshold_setup_error is not None
        assert "must be a number" in cfg.threshold_setup_error
    finally:
        del os.environ["CODE_REVIEW_COVERAGE_THRESHOLD"]


# ---------------------------------------------------------------------------
# C6 — env overrides
# ---------------------------------------------------------------------------


def test_load_config_fail_open_env_override(tmp_path: Path) -> None:
    os.environ["CODE_REVIEW_GATE_FAIL_OPEN"] = "1"
    try:
        cfg = _load_config(tmp_path)
        assert cfg.fail_open_on_error is True
    finally:
        del os.environ["CODE_REVIEW_GATE_FAIL_OPEN"]


def test_run_gate_verbose_env_emits_diagnostic(capsys: pytest.CaptureFixture[str]) -> None:
    os.environ["CODE_REVIEW_GATE_VERBOSE"] = "1"
    try:
        with (
            patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
            patch("scripts.preflight_gate._git_run", return_value=""),
            patch("scripts.preflight_gate._run_coverage_check", return_value=CoverageResult()),
            patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
            patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
        ):
            rc = run_gate()
        assert rc == 0
        captured = capsys.readouterr()
        assert "verbose:" in captured.err
    finally:
        del os.environ["CODE_REVIEW_GATE_VERBOSE"]


# ---------------------------------------------------------------------------
# C7 — staleness uses git log, not worktree mtime
# ---------------------------------------------------------------------------


def test_coverage_check_stale_when_worktree_mtime_newer_than_report(tmp_path: Path) -> None:
    """Working-tree mtime is the staleness source-of-truth.

    Pre-C2-fix this used `git log -1 --format=%ct`, which returned the
    last *committed* timestamp — a coverage.xml regenerated after the
    last commit but before a fresh edit was wrongly treated as fresh.
    Post-fix: any working-tree edit newer than the report marks it stale.
    """
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    staged = repo / "src.py"
    staged.write_text("x = 1\n")
    import time

    # Report generated 10s ago.
    report_mtime = time.time() - 10
    os.utime(report, (report_mtime, report_mtime))

    # Working-tree edit happens NOW (newer than report).
    now = time.time()
    os.utime(staged, (now, now))

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--name-only" in args:
            return "src.py\n"
        if args[0] == "log":
            # Stale committed timestamp — irrelevant after fix.
            return str(int(report_mtime - 5))
        return ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        result = _run_coverage_check(GateConfig(), repo)

    # Working-tree mtime > report mtime → setup_error.
    assert result.setup_error is not None
    assert "stale" in result.setup_error


# ---------------------------------------------------------------------------
# C8, C9, C10 — diff-cover JSON schema variations
# ---------------------------------------------------------------------------


def test_coverage_check_diff_cover_json_src_stats_schema(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = {"src_stats": {"a.py": {"violation_lines": [3]}}}
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert len(result.findings) == 1
    assert result.findings[0] == CoverageFinding("a.py", 3, "line")


def test_coverage_check_diff_cover_json_per_file_list_violations(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = {"a.py": {"violations": [2, 5]}}
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert len(result.findings) == 2
    assert result.findings[0] == CoverageFinding("a.py", 2, "line")
    assert result.findings[1] == CoverageFinding("a.py", 5, "line")


def test_coverage_check_diff_cover_json_top_level_list(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = [{"filename": "b.py", "violations": [{"line": 4, "kind": "branch"}]}]
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert len(result.findings) == 1
    assert result.findings[0] == CoverageFinding("b.py", 4, "branch")


# ---------------------------------------------------------------------------
# C11 — skipif decorators
# ---------------------------------------------------------------------------


def test_assert_check_pytest_skipif_not_flagged() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        '+@pytest.mark.skipif(True, reason="x")\n'
        "+def test_x(): pass\n"
    )
    src = '@pytest.mark.skipif(True, reason="x")\ndef test_x(): pass\n'
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


# ---------------------------------------------------------------------------
# C12 — self.assertRaises context manager
# ---------------------------------------------------------------------------


def test_assert_check_self_assertRaises_context_manager() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+def test_x(self):\n"
        "+    with self.assertRaises(ValueError):\n"
        "+        foo()\n"
    )
    src = "def test_x(self):\n    with self.assertRaises(ValueError):\n        foo()\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


# ---------------------------------------------------------------------------
# C13 — bare raises context manager
# ---------------------------------------------------------------------------


def test_assert_check_bare_raises_context_manager() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+from pytest import raises\n"
        "+def test_x():\n"
        "+    with raises(ValueError):\n"
        "+        foo()\n"
    )
    src = "from pytest import raises\ndef test_x():\n    with raises(ValueError):\n        foo()\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


# ---------------------------------------------------------------------------
# C15 — opt-out short circuit
# ---------------------------------------------------------------------------


def test_run_gate_opt_out_short_circuit(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "[tool.code_review.coverage_gate]\nenabled = false\n")
    with (
        patch("scripts.preflight_gate._run_coverage_check") as m_cov,
        patch("scripts.preflight_gate._run_assert_check") as m_ass,
        patch("scripts.preflight_gate._run_assert_check_js") as m_js,
    ):
        rc = run_gate(tmp_path)
    assert rc == 0
    m_cov.assert_not_called()
    m_ass.assert_not_called()
    m_js.assert_not_called()


# ---------------------------------------------------------------------------
# C16 — all subchecks disabled returns zero
# ---------------------------------------------------------------------------


def test_run_gate_all_subchecks_disabled_returns_zero(tmp_path: Path) -> None:
    _write_pyproject(
        tmp_path, "[tool.code_review.coverage_gate]\ncoverage_check.enabled = false\nassert_counter.enabled = false\n"
    )
    with (
        patch("scripts.preflight_gate._run_coverage_check") as m_cov,
        patch("scripts.preflight_gate._run_assert_check") as m_ass,
        patch("scripts.preflight_gate._run_assert_check_js") as m_js,
    ):
        rc = run_gate(tmp_path)
    assert rc == 0
    m_cov.assert_not_called()
    m_ass.assert_not_called()
    m_js.assert_not_called()


# ---------------------------------------------------------------------------
# C17 — skip JS when disabled
# ---------------------------------------------------------------------------


def test_run_gate_skips_js_when_disabled(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "[tool.code_review.coverage_gate]\nassert_counter.javascript.enabled = false\n")
    diff = (
        "diff --git a/src.test.js b/src.test.js\n--- a/src.test.js\n+++ b/src.test.js\n@@ -0,0 +1 @@\n+it('x',()=>{})\n"
    )
    with (
        patch("scripts.preflight_gate._git_run", return_value=diff),
        patch("scripts.preflight_gate._run_coverage_check", return_value=CoverageResult()),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js") as m_js,
    ):
        rc = run_gate(tmp_path)
    assert rc == 0
    m_js.assert_not_called()


# ---------------------------------------------------------------------------
# Coverage gap tests — ANSI helpers
# ---------------------------------------------------------------------------


def test_red_wraps_with_escape() -> None:
    assert _red("hi") == "\033[31mhi\033[0m"


def test_yellow_wraps_with_escape() -> None:
    assert _yellow("hi") == "\033[33mhi\033[0m"


def test_cyan_wraps_with_escape() -> None:
    assert _cyan("hi") == "\033[36mhi\033[0m"


def test_green_wraps_with_escape() -> None:
    assert _green("hi") == "\033[32mhi\033[0m"


def test_tag_prefixes_with_color() -> None:
    assert _tag("\033[31m", "label", "msg") == "\033[31m[preflight] msg\033[0m"


# ---------------------------------------------------------------------------
# Coverage gap tests — Config edge cases
# ---------------------------------------------------------------------------


def test_load_config_reports_field_as_string(tmp_path: Path) -> None:
    _write_pyproject(
        tmp_path,
        '[tool.code_review.coverage_gate]\ncoverage_check.reports = "coverage.xml"\n',
    )
    cfg = _load_config(tmp_path)
    assert cfg.coverage_reports == ("coverage.xml",)


# ---------------------------------------------------------------------------
# Coverage gap tests — _git_run
# ---------------------------------------------------------------------------


def test_git_run_raises_on_error() -> None:
    with patch("scripts.preflight_gate.subprocess.run") as m:
        m.return_value = subprocess.CompletedProcess(args=["git", "status"], returncode=1, stdout="", stderr="fail")
        with pytest.raises(subprocess.CalledProcessError):
            _git_run(["status"])


# ---------------------------------------------------------------------------
# Coverage gap tests — Coverage check branches
# ---------------------------------------------------------------------------


def test_coverage_check_git_name_only_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')
    with patch("scripts.preflight_gate._git_run", side_effect=subprocess.CalledProcessError(128, ["git"])):
        result = _run_coverage_check(GateConfig(), repo)
    assert result.internal_error is not None
    assert "git diff --cached --name-only failed" in result.internal_error


def test_coverage_check_staleness_fallback_worktree_mtime(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')
    staged = repo / "new.py"
    staged.write_text("x = 1")
    import time

    old_time = time.time() - 3600
    os.utime(report, (old_time, old_time))
    os.utime(staged, (time.time(), time.time()))

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if "--name-only" in args:
            return "new.py\n"
        if "log" in args:
            return ""  # no commit history
        return ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        result = _run_coverage_check(GateConfig(), repo)
    assert result.setup_error is not None
    assert "stale" in result.setup_error


def test_coverage_check_staleness_valueerror(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if "--name-only" in args:
            return "src.py\n"
        if "log" in args:
            return "bad"
        return ""

    with (
        patch("scripts.preflight_gate._git_run", side_effect=fake_git),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)
    assert result.setup_error is None


def test_coverage_check_xml_read_error(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.mkdir()  # directory, cannot read as file

    with patch("scripts.preflight_gate._git_run", return_value=""):
        result = _run_coverage_check(GateConfig(), repo)
    assert result.setup_error is not None
    assert "cannot read" in result.setup_error


def test_coverage_check_diff_cover_timeout(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch(
            "scripts.preflight_gate.subprocess.run",
            side_effect=subprocess.TimeoutExpired("diff-cover", 120),
        ),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)
    assert result.internal_error is not None
    assert "timed out" in result.internal_error


def test_coverage_check_diff_cover_crash_no_hint(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    class FakeProc:
        returncode = 2
        stderr = "some random error"

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.subprocess.run", return_value=FakeProc()),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.internal_error is not None
    assert "diff-cover crashed" in result.internal_error
    assert "git fetch" not in result.internal_error


def test_coverage_check_json_path_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    class FakeProc:
        returncode = 0
        stderr = ""

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.subprocess.run", return_value=FakeProc()),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.internal_error is not None
    assert "did not produce JSON output" in result.internal_error


def test_coverage_check_json_decode_error(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text("not json")
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.internal_error is not None
    assert "JSON parse error" in result.internal_error


def test_coverage_check_violation_list_of_pairs(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = {"src.py": {"violations": [[5, "branch"], [7]]}}
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert len(result.findings) == 2
    assert result.findings[0] == CoverageFinding("src.py", 5, "branch")
    assert result.findings[1] == CoverageFinding("src.py", 7, "line")


def test_coverage_check_stats_as_list(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = {"src.py": [{"line": 3, "kind": "branch"}, 8]}
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert len(result.findings) == 2
    assert result.findings[0] == CoverageFinding("src.py", 3, "branch")
    assert result.findings[1] == CoverageFinding("src.py", 8, "line")


def test_coverage_check_srcs_neither_dict_nor_list(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = "unexpected string"
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.findings == []


def test_coverage_check_top_level_list_two_entries(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = [
        {"filename": "a.py", "violations": [1]},
        {"filename": "b.py", "violations": [2]},
    ]
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert len(result.findings) == 2


def test_coverage_check_stats_neither_dict_nor_list(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = {"src.py": "unexpected"}
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.findings == []


def test_coverage_check_top_level_list_non_dict_entry(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = [{"filename": "a.py", "violations": [1]}, "not a dict"]
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert len(result.findings) == 1
    assert result.findings[0] == CoverageFinding("a.py", 1, "line")


def test_coverage_check_staleness_fallback_worktree_mtime_loop(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')
    staged1 = repo / "new1.py"
    staged1.write_text("x = 1")
    staged2 = repo / "new2.py"
    staged2.write_text("y = 2")
    import time

    old_time = time.time() - 3600
    os.utime(report, (old_time, old_time))
    now = time.time()
    os.utime(staged1, (now, now))
    os.utime(staged2, (now - 30, now - 30))

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if "--name-only" in args:
            return "new1.py\nnew2.py\n"
        if "log" in args:
            return ""
        return ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        result = _run_coverage_check(GateConfig(), repo)
    assert result.setup_error is not None
    assert "stale" in result.setup_error


# ---------------------------------------------------------------------------
# Coverage gap tests — _format_report branches
# ---------------------------------------------------------------------------


def test_format_report_coverage_none_assert_findings() -> None:
    ass = AssertResult(findings=[AssertFinding("t.py", "test_x", 2)])
    report = _format_report(None, ass, GateConfig(output_format="plain"))
    assert "[1/2]" not in report
    assert "Assert gate — 1 finding" in report


def test_format_report_coverage_findings_truncation() -> None:
    findings = [CoverageFinding("a.py", i, "line") for i in range(1, 10)]
    cov = CoverageResult(findings=findings)
    report = _format_report(cov, None, GateConfig(output_format="plain", max_report_lines_in_output=3))
    assert "... and 6 more" in report


def test_format_report_assert_none() -> None:
    cov = CoverageResult(findings=[CoverageFinding("a.py", 1, "line")])
    report = _format_report(cov, None, GateConfig(output_format="plain"))
    assert "[1/2]" in report
    assert "[2/2]" not in report


def test_format_report_assert_findings_truncation() -> None:
    findings = [AssertFinding("t.py", f"test_{i}", i) for i in range(1, 10)]
    ass = AssertResult(findings=findings)
    report = _format_report(None, ass, GateConfig(output_format="plain", max_report_lines_in_output=3))
    assert "... and 6 more" in report


# ---------------------------------------------------------------------------
# Coverage gap tests — Skip decorator branches
# ---------------------------------------------------------------------------


def test_has_skip_decorator_bare_skip() -> None:
    tree = ast.parse("@skip\ndef test_x(): pass\n")
    func = tree.body[0]
    assert _has_skip_decorator(func)


def test_has_skip_decorator_subscript() -> None:
    tree = ast.parse("@decorators['skip']\ndef test_x(): pass\n")
    func = tree.body[0]
    assert not _has_skip_decorator(func)


# ---------------------------------------------------------------------------
# Coverage gap tests — Assert counter branches
# ---------------------------------------------------------------------------


def test_attr_name_returns_none_for_subscript() -> None:
    tree = ast.parse("foo[0](1)\n")
    call = tree.body[0].value
    assert _attr_name(call.func) is None


def test_count_assertions_with_multiple_context_managers() -> None:
    src = "def test_x():\n    with pytest.raises(ValueError), some_context, open('f'):\n        pass\n"
    tree = ast.parse(src)
    func = tree.body[0]
    count = _count_assertions_in_node(func.body[0], GateConfig(count_pytest_raises_as_assertion=False))
    assert count == 0


def test_count_assertions_self_assertRaises_with_loop() -> None:
    src = "def test_x(self):\n    with self.assertRaises(ValueError), self.assertWarns(Warning):\n        pass\n"
    tree = ast.parse(src)
    func = tree.body[0]
    count = _count_assertions_in_node(func.body[0], GateConfig())
    assert count == 2


def test_count_assertions_keyword_args() -> None:
    src = "def test_x():\n    helper(a=assert_true())\n"
    tree = ast.parse(src)
    func = tree.body[0]
    count = _count_assertions_in_node(func.body[0], GateConfig())
    assert count == 1


# ---------------------------------------------------------------------------
# Coverage gap tests — _run_assert_check branches
# ---------------------------------------------------------------------------


def test_assert_check_git_diff_fails() -> None:
    with patch("scripts.preflight_gate._git_run", side_effect=subprocess.CalledProcessError(128, ["git"])):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.internal_error is not None
    assert "git diff --cached failed" in result.internal_error


def test_assert_check_no_added_lines() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -1,2 +1,2 @@\n"
        " line1\n"
        " line2\n"
    )
    with patch("scripts.preflight_gate._git_run", return_value=diff):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_glob_match_bare_double_star() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    assert x\n"
    )
    cfg = GateConfig(test_file_patterns=("**test_*.py",))
    with patch(
        "scripts.preflight_gate._git_run",
        side_effect=lambda args, cwd=None: diff if args[0] == "diff" else "def test_x():\n    assert x\n",
    ):
        result = _run_assert_check(cfg, Path("/tmp"))
    assert result.findings == []


def test_glob_match_question_mark() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    assert x\n"
    )
    cfg = GateConfig(test_file_patterns=("tests/?est_*.py",))
    with patch(
        "scripts.preflight_gate._git_run",
        side_effect=lambda args, cwd=None: diff if args[0] == "diff" else "def test_x():\n    assert x\n",
    ):
        result = _run_assert_check(cfg, Path("/tmp"))
    assert result.findings == []


def test_is_test_file_no_match() -> None:
    diff = "diff --git a/src.py b/src.py\n--- a/src.py\n+++ b/src.py\n@@ -0,0 +1 @@\n+x = 1\n"
    with patch("scripts.preflight_gate._git_run", return_value=diff):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_assert_check_func_not_in_diff() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    pass\n"
    )
    src = "def test_x():\n    pass\ndef test_y():\n    pass\n"
    with patch(
        "scripts.preflight_gate._git_run",
        side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src,
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert len(result.findings) == 1
    assert result.findings[0].function_name == "test_x"


# ---------------------------------------------------------------------------
# Coverage gap tests — _diff_has_production_python
# ---------------------------------------------------------------------------


def test_diff_has_production_python_with_test_file() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    pass\n"
    )
    assert not _diff_has_production_python(diff, GateConfig())


def test_diff_has_production_python_mixed() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    pass\n"
        "diff --git a/src.py b/src.py\n"
        "--- a/src.py\n"
        "+++ b/src.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    assert _diff_has_production_python(diff, GateConfig())


def test_diff_has_production_python_custom_pattern_bare_star_star() -> None:
    diff = "diff --git a/src.py b/src.py\n--- a/src.py\n+++ b/src.py\n@@ -0,0 +1 @@\n+x = 1\n"
    cfg = GateConfig(test_file_patterns=("**src.py",))
    assert not _diff_has_production_python(diff, cfg)


def test_diff_has_production_python_custom_pattern_question_mark() -> None:
    diff = "diff --git a/src.py b/src.py\n--- a/src.py\n+++ b/src.py\n@@ -0,0 +1 @@\n+x = 1\n"
    cfg = GateConfig(test_file_patterns=("?rc.py",))
    assert not _diff_has_production_python(diff, cfg)


# ---------------------------------------------------------------------------
# Coverage gap tests — run_gate orchestrator branches
# ---------------------------------------------------------------------------


def test_run_gate_skip_gate_env_tty(capsys: pytest.CaptureFixture[str]) -> None:
    os.environ["CODE_REVIEW_SKIP_GATE"] = "1"
    try:
        with patch("sys.stderr.isatty", return_value=True):
            rc = run_gate()
        assert rc == 0
        captured = capsys.readouterr()
        assert "\033[33m" in captured.err
    finally:
        del os.environ["CODE_REVIEW_SKIP_GATE"]


def test_run_gate_threshold_setup_error_returns_two() -> None:
    cfg = GateConfig(threshold_setup_error="bad threshold")
    with patch("scripts.preflight_gate._load_config", return_value=cfg):
        rc = run_gate()
    assert rc == 2


def test_run_gate_threshold_setup_error_tty(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = GateConfig(threshold_setup_error="bad threshold")
    with (
        patch("scripts.preflight_gate._load_config", return_value=cfg),
        patch("sys.stderr.isatty", return_value=True),
    ):
        rc = run_gate()
    assert rc == 2
    captured = capsys.readouterr()
    assert "\033[31m" in captured.err


def test_run_gate_git_diff_fails_coverage() -> None:
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch(
            "scripts.preflight_gate._git_run",
            side_effect=subprocess.CalledProcessError(128, ["git"]),
        ),
        patch("scripts.preflight_gate._run_coverage_check") as m_cov,
        patch("scripts.preflight_gate._run_assert_check") as m_ass,
    ):
        rc = run_gate()
    assert rc == 3
    m_cov.assert_not_called()
    m_ass.assert_not_called()


def test_run_gate_assert_disabled_coverage_enabled() -> None:
    cfg = GateConfig(coverage_check_enabled=True, assert_check_enabled=False)
    with (
        patch("scripts.preflight_gate._load_config", return_value=cfg),
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate._run_coverage_check", return_value=CoverageResult()),
    ):
        rc = run_gate()
    assert rc == 0


def test_run_gate_setup_error_from_assert_only() -> None:
    cov = CoverageResult(findings=[])
    ass = AssertResult(setup_error="missing pytest")
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._git_run", return_value="diff"),
        patch("scripts.preflight_gate._diff_has_production_python", return_value=True),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=ass),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        rc = run_gate()
    assert rc == 2


def test_run_gate_setup_error_tty(capsys: pytest.CaptureFixture[str]) -> None:
    cov = CoverageResult(setup_error="missing report")
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
        patch("sys.stderr.isatty", return_value=True),
    ):
        rc = run_gate()
    assert rc == 2
    captured = capsys.readouterr()
    assert "\033[31m" in captured.err


def test_run_gate_assert_internal_error() -> None:
    cov = CoverageResult(findings=[])
    ass = AssertResult(internal_error="boom")
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._git_run", return_value="diff"),
        patch("scripts.preflight_gate._diff_has_production_python", return_value=True),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=ass),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        rc = run_gate()
    assert rc == 3


def test_run_gate_internal_error_fail_open_tty(capsys: pytest.CaptureFixture[str]) -> None:
    cov = CoverageResult(internal_error="boom")
    cfg = GateConfig(fail_open_on_error=True)
    with (
        patch("scripts.preflight_gate._load_config", return_value=cfg),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
        patch("sys.stderr.isatty", return_value=True),
    ):
        rc = run_gate()
    assert rc == 0
    captured = capsys.readouterr()
    assert "\033[33m" in captured.err


def test_run_gate_internal_error_fail_closed_tty(capsys: pytest.CaptureFixture[str]) -> None:
    cov = CoverageResult(internal_error="boom")
    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig(fail_open_on_error=False)),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=AssertResult()),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
        patch("sys.stderr.isatty", return_value=True),
    ):
        rc = run_gate()
    assert rc == 3
    captured = capsys.readouterr()
    assert "\033[31m" in captured.err


# ---------------------------------------------------------------------------
# Coverage gap tests — JS wrapper branches (mocked)
# ---------------------------------------------------------------------------


def test_run_assert_check_js_disabled() -> None:
    cfg = GateConfig(javascript_assert_enabled=False)
    result = _run_assert_check_js(cfg, Path("/tmp"))
    assert result.findings == []
    assert result.setup_error is None


def test_run_assert_check_js_git_diff_fails() -> None:
    with patch("scripts.preflight_gate._git_run", side_effect=subprocess.CalledProcessError(128, ["git"])):
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))
    assert result.internal_error is not None
    assert "git diff --cached failed" in result.internal_error


def test_run_assert_check_js_no_added_lines() -> None:
    with patch("scripts.preflight_gate._git_run", return_value=""):
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_run_assert_check_js_non_js_file() -> None:
    diff = "diff --git a/src.py b/src.py\n--- a/src.py\n+++ b/src.py\n@@ -0,0 +1 @@\n+x = 1\n"
    with patch("scripts.preflight_gate._git_run", return_value=diff):
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_run_assert_check_js_git_show_fails() -> None:
    diff = (
        "diff --git a/src.test.js b/src.test.js\n--- a/src.test.js\n+++ b/src.test.js\n@@ -0,0 +1 @@\n+it('x',()=>{})\n"
    )

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff":
            return diff
        raise subprocess.CalledProcessError(128, ["git"])

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))
    assert result.findings == []


def test_run_assert_check_js_helper_crash() -> None:
    diff = (
        "diff --git a/src.test.js b/src.test.js\n--- a/src.test.js\n+++ b/src.test.js\n@@ -0,0 +1 @@\n+it('x',()=>{})\n"
    )
    fake_proc = subprocess.CompletedProcess(args=["node"], returncode=1, stderr="helper crashed")
    with (
        patch("scripts.preflight_gate._git_run", return_value=diff),
        patch("scripts.preflight_gate.subprocess.run", return_value=fake_proc),
    ):
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))
    assert result.internal_error is not None
    assert "JS assert helper failed" in result.internal_error


def test_coverage_check_top_level_list_violation_lines(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = [{"filename": "a.py", "violation_lines": [3, 4]}]
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert len(result.findings) == 2
    assert result.findings[0] == CoverageFinding("a.py", 3, "line")


# ---------------------------------------------------------------------------
# C1 — malformed pyproject.toml
# ---------------------------------------------------------------------------


def test_load_config_malformed_pyproject_toml(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool\n")
    cfg = _load_config(tmp_path)
    assert cfg.config_setup_error is not None
    assert "malformed pyproject.toml" in cfg.config_setup_error


def test_run_gate_config_setup_error_returns_two() -> None:
    cfg = GateConfig(config_setup_error="malformed pyproject.toml")
    with patch("scripts.preflight_gate._load_config", return_value=cfg):
        rc = run_gate()
    assert rc == 2


# ---------------------------------------------------------------------------
# C2 — tuple-typed config fields normalise single strings
# ---------------------------------------------------------------------------


def test_load_config_test_file_patterns_as_string(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code_review.coverage_gate.assert_counter.python]\ntest_file_patterns = "*/test_*.py"\n'
    )
    cfg = _load_config(tmp_path)
    assert cfg.test_file_patterns == ("*/test_*.py",)


def test_load_config_js_test_function_names_as_string(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code_review.coverage_gate.assert_counter.javascript]\ntest_function_names = "it.only"\n'
    )
    cfg = _load_config(tmp_path)
    assert cfg.javascript_test_function_names == ("it.only",)


# ---------------------------------------------------------------------------
# C5 — HEAD fallback on fresh git repo
# ---------------------------------------------------------------------------


def test_resolve_compare_branch_returns_none_when_head_unresolved(tmp_path: Path) -> None:
    from scripts.preflight_gate import _resolve_compare_branch

    with patch("scripts.preflight_gate._ref_exists", return_value=False):
        result = _resolve_compare_branch(GateConfig(), tmp_path)
    assert result is None


def test_run_coverage_check_setup_error_when_no_compare_branch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value=None),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.setup_error is not None
    assert "no resolvable compare-branch" in result.setup_error


def test_resolve_compare_branch_auto_origin_head(tmp_path: Path) -> None:
    from scripts.preflight_gate import _resolve_compare_branch

    fake_proc = subprocess.CompletedProcess(
        args=["git", "symbolic-ref"],
        returncode=0,
        stdout="refs/remotes/origin/develop\n",
        stderr="",
    )
    with patch("scripts.preflight_gate.subprocess.run", return_value=fake_proc):
        result = _resolve_compare_branch(GateConfig(), tmp_path)
    assert result == "origin/develop"


def test_resolve_compare_branch_ladder_origin_master(tmp_path: Path) -> None:
    from scripts.preflight_gate import _resolve_compare_branch

    def fake_ref_exists(ref: str, repo_root: Path) -> bool:
        return ref == "origin/master"

    fake_proc = subprocess.CompletedProcess(
        args=["git", "symbolic-ref"],
        returncode=1,
        stdout="",
        stderr="",
    )
    with (
        patch("scripts.preflight_gate.subprocess.run", return_value=fake_proc),
        patch("scripts.preflight_gate._ref_exists", side_effect=fake_ref_exists),
    ):
        result = _resolve_compare_branch(GateConfig(), tmp_path)
    assert result == "origin/master"


# ---------------------------------------------------------------------------
# C6 — nested helper functions not collected as tests
# ---------------------------------------------------------------------------


def test_assert_check_nested_helper_not_collected(tmp_path: Path) -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+def outer():\n"
        "+    def test_helper():\n"
        "+        pass\n"
    )
    src = "def outer():\n    def test_helper():\n        pass\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


# ---------------------------------------------------------------------------
# C7 — assert methods on Call/Subscript receivers
# ---------------------------------------------------------------------------


def test_count_assertions_subscript_or_call_receiver() -> None:
    tree = ast.parse("def test_x():\n    factory().assert_called_once_with(x)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) >= 1


# ---------------------------------------------------------------------------
# C12 — _diff_has_production_python skips non-.py files
# ---------------------------------------------------------------------------


def test_diff_has_production_python_skips_js_files() -> None:
    diff = (
        "diff --git a/src.js b/src.js\n"
        "--- a/src.js\n"
        "+++ b/src.js\n"
        "@@ -0,0 +1 @@\n"
        "+console.log(1)\n"
        "diff --git a/src.ts b/src.ts\n"
        "--- a/src.ts\n"
        "+++ b/src.ts\n"
        "@@ -0,0 +1 @@\n"
        "+const x = 1\n"
        "diff --git a/src.tsx b/src.tsx\n"
        "--- a/src.tsx\n"
        "+++ b/src.tsx\n"
        "@@ -0,0 +1 @@\n"
        "+const y = 2\n"
    )
    assert not _diff_has_production_python(diff, GateConfig())


# ---------------------------------------------------------------------------
# C15 — default pattern includes **_test.py
# ---------------------------------------------------------------------------


def test_assert_check_underscore_test_suffix_pattern() -> None:
    diff = (
        "diff --git a/pkg/foo_test.py b/pkg/foo_test.py\n"
        "--- a/pkg/foo_test.py\n"
        "+++ b/pkg/foo_test.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_x():\n"
        "+    pass\n"
    )
    src = "def test_x():\n    pass\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert len(result.findings) == 1
    assert result.findings[0].function_name == "test_x"


# ---------------------------------------------------------------------------
# Coverage gap — empty coverage_exclude_paths
# ---------------------------------------------------------------------------


def test_coverage_check_no_exclude_paths(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = {"src.py": {"violation_lines": []}}
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(coverage_exclude_paths=()), repo)

    assert result.findings == []


# ---------------------------------------------------------------------------
# Coverage gap — remaining branches
# ---------------------------------------------------------------------------


def test_load_config_test_file_patterns_as_list(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code_review.coverage_gate.assert_counter.python]\ntest_file_patterns = ["*/test_*.py", "*_test.py"]\n'
    )
    cfg = _load_config(tmp_path)
    assert cfg.test_file_patterns == ("*/test_*.py", "*_test.py")


def test_load_config_js_test_function_names_as_list(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code_review.coverage_gate.assert_counter.javascript]\ntest_function_names = ["it", "test"]\n'
    )
    cfg = _load_config(tmp_path)
    assert cfg.javascript_test_function_names == ("it", "test")


def test_ref_exists_handles_subprocess_error(tmp_path: Path) -> None:
    from scripts.preflight_gate import _ref_exists

    with patch("scripts.preflight_gate.subprocess.run", side_effect=FileNotFoundError("git")):
        assert _ref_exists("HEAD", tmp_path) is False


def test_resolve_compare_branch_symbolic_ref_subprocess_error(tmp_path: Path) -> None:
    from scripts.preflight_gate import _resolve_compare_branch

    with patch("scripts.preflight_gate.subprocess.run", side_effect=FileNotFoundError("git")):
        result = _resolve_compare_branch(GateConfig(), tmp_path)
    assert result is None


def test_resolve_compare_branch_head_resolves(tmp_path: Path) -> None:
    from scripts.preflight_gate import _resolve_compare_branch

    def fake_ref_exists(ref: str, repo_root: Path) -> bool:
        return ref == "HEAD"

    fake_proc = subprocess.CompletedProcess(
        args=["git", "symbolic-ref"],
        returncode=1,
        stdout="",
        stderr="",
    )
    with (
        patch("scripts.preflight_gate.subprocess.run", return_value=fake_proc),
        patch("scripts.preflight_gate._ref_exists", side_effect=fake_ref_exists),
    ):
        result = _resolve_compare_branch(GateConfig(), tmp_path)
    assert result == "HEAD"


def test_iter_test_funcs_non_module_returns_empty() -> None:
    from scripts.preflight_gate import _iter_test_funcs

    expr = ast.parse("1 + 1").body[0]
    assert list(_iter_test_funcs(expr)) == []


def test_count_assertions_attribute_not_assert() -> None:
    tree = ast.parse("def test_x():\n    factory().called_once_with(x)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_assert_check_class_with_non_test_method_not_collected() -> None:
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+class TestFoo:\n"
        "+    def helper(self):\n"
        "+        pass\n"
    )
    src = "class TestFoo:\n    def helper(self):\n        pass\n"
    with patch(
        "scripts.preflight_gate._git_run", side_effect=lambda args, cwd=None: diff if args[0] == "diff" else src
    ):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert result.findings == []


# ---------------------------------------------------------------------------
# Coverage gap — empty stats list
# ---------------------------------------------------------------------------


def test_coverage_check_empty_stats_list(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    json_out = {"src.py": []}
    td = tmp_path / "cov_td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = ""
        return fake_proc

    with (
        patch("scripts.preflight_gate._git_run", return_value=""),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.findings == []


# ---------------------------------------------------------------------------
# C1 — single-string custom_assertion_helpers must NOT be split into chars
# ---------------------------------------------------------------------------


def test_load_config_custom_assertion_helpers_string_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code_review.coverage_gate.assert_counter.python]\ncustom_assertion_helpers = "assert_json"\n'
    )
    cfg = _load_config(tmp_path)
    assert cfg.custom_assertion_helpers == frozenset({"assert_json"})


def test_load_config_custom_assertion_helpers_string_javascript(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code_review.coverage_gate.assert_counter.javascript]\ncustom_assertion_helpers = "expectShape"\n'
    )
    cfg = _load_config(tmp_path)
    assert cfg.javascript_custom_assertion_helpers == frozenset({"expectShape"})


# ---------------------------------------------------------------------------
# C3 — stale `origin/HEAD -> origin/<missing>` symref must fall through ladder
# ---------------------------------------------------------------------------


def test_resolve_compare_branch_stale_origin_head_falls_back(tmp_path: Path) -> None:
    """If `git symbolic-ref refs/remotes/origin/HEAD` returns a ref that no
    longer exists (pruned/missing), the ladder must fall through to
    origin/main / origin/master / etc. — not blindly trust the symref.
    """
    from scripts.preflight_gate import _resolve_compare_branch

    fake_proc = subprocess.CompletedProcess(
        args=["git", "symbolic-ref"],
        returncode=0,
        stdout="refs/remotes/origin/missing-branch\n",
        stderr="",
    )

    def fake_ref_exists(ref: str, repo_root: Path) -> bool:
        # The symref's target is gone; only `origin/main` resolves.
        return ref == "origin/main"

    with (
        patch("scripts.preflight_gate.subprocess.run", return_value=fake_proc),
        patch("scripts.preflight_gate._ref_exists", side_effect=fake_ref_exists),
    ):
        result = _resolve_compare_branch(GateConfig(), tmp_path)
    assert result == "origin/main"


# ---------------------------------------------------------------------------
# C2 — staged-but-deleted file falls back to git-log committed timestamp
# ---------------------------------------------------------------------------


def test_coverage_check_staleness_falls_back_to_git_log_for_deleted_file(tmp_path: Path) -> None:
    """When a staged file is absent from working tree (e.g. `git rm` staged),
    `os.stat` raises OSError and we fall back to the last-commit timestamp.
    """
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')

    import time

    # Report old.
    report_mtime = time.time() - 100
    os.utime(report, (report_mtime, report_mtime))

    # File staged for deletion: `git diff --cached --name-only` lists it
    # but it's not on disk anymore. Last commit timestamp is NEWER than
    # report → must mark report stale via git-log fallback.
    deleted_commit_ts = int(report_mtime + 50)

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--name-only" in args:
            return "removed.py\n"
        if args[0] == "log":
            return f"{deleted_commit_ts}\n"
        return ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.setup_error is not None
    assert "stale" in result.setup_error


# ---------------------------------------------------------------------------
# C7 — PREFLIGHT_TARGET_CWD must override an inherited shell export
# ---------------------------------------------------------------------------


def test_run_assert_check_js_target_cwd_wins_over_inherited_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the parent shell exports PREFLIGHT_TARGET_CWD=/some/other/path,
    the gate's own value (str(repo_root)) must still win — otherwise the
    Node helper resolves @typescript-eslint/parser from the wrong checkout.
    """
    from scripts.preflight_gate import _run_assert_check_js

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    test_file = repo / "tests" / "a.test.ts"
    test_file.write_text("it('x', () => {});\n")

    monkeypatch.setenv("PREFLIGHT_TARGET_CWD", "/totally/wrong/path")

    captured: dict[str, dict[str, str]] = {}

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        captured["env"] = kwargs.get("env", {})
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = '{"findings": []}'
        proc.stderr = ""
        return proc

    cfg = GateConfig(javascript_assert_enabled=True)

    with (
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._git_run", return_value="tests/a.test.ts\n"),
        patch(
            "scripts.preflight_gate.parse_added_lines",
            return_value={"tests/a.test.ts": {1}},
        ),
    ):
        _run_assert_check_js(cfg, repo)

    assert captured["env"].get("PREFLIGHT_TARGET_CWD") == str(repo)


# ---------------------------------------------------------------------------
# C12 — _format_report uses sys.stderr.isatty() (report is printed to stderr)
# ---------------------------------------------------------------------------


def test_format_report_color_keyed_off_stderr_tty() -> None:
    """ANSI color must be enabled iff stderr is a TTY. _format_report's
    output goes to stderr (run_gate), so keying off stdout.isatty() would
    color-leak when stdout is redirected but stderr is the terminal.
    """
    from scripts.preflight_gate import (
        AssertFinding,
        AssertResult,
        _format_report,
    )

    cfg = GateConfig(output_format="ansi")
    res = AssertResult(findings=[AssertFinding("a.py", "test_x", 1)])

    fake_stderr = MagicMock()
    fake_stderr.isatty.return_value = True
    fake_stdout = MagicMock()
    fake_stdout.isatty.return_value = False

    with (
        patch("scripts.preflight_gate.sys.stderr", fake_stderr),
        patch("scripts.preflight_gate.sys.stdout", fake_stdout),
    ):
        report = _format_report(None, res, cfg)

    # ANSI color codes present (red prefix) -> keyed off stderr, not stdout.
    assert "\x1b[" in report


def test_format_report_no_color_when_stderr_not_tty() -> None:
    from scripts.preflight_gate import (
        AssertFinding,
        AssertResult,
        _format_report,
    )

    cfg = GateConfig(output_format="ansi")
    res = AssertResult(findings=[AssertFinding("a.py", "test_x", 1)])

    fake_stderr = MagicMock()
    fake_stderr.isatty.return_value = False

    with patch("scripts.preflight_gate.sys.stderr", fake_stderr):
        report = _format_report(None, res, cfg)

    assert "\x1b[" not in report


# ---------------------------------------------------------------------------
# C13 — async with pytest.raises(...) counts as an assertion
# ---------------------------------------------------------------------------


def test_assert_check_async_with_raises_counts_as_assertion(tmp_path: Path) -> None:
    """`ast.AsyncWith` must be treated like `ast.With` — async tests using
    `async with pytest.raises(...)` should count as having an assertion.
    """
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,4 @@\n"
        "+import pytest\n"
        "+async def test_async_raises():\n"
        "+    async with pytest.raises(ValueError):\n"
        "+        await some_async_op()\n"
    )
    path = tmp_path / "tests" / "test_x.py"
    path.parent.mkdir()
    path.write_text(
        "import pytest\n"
        "async def test_async_raises():\n"
        "    async with pytest.raises(ValueError):\n"
        "        await some_async_op()\n"
    )

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--cached" in args and "--unified=0" in args:
            return diff
        if args[0] == "show":
            return path.read_text()
        return ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        result = _run_assert_check(GateConfig(), tmp_path)

    assert result.findings == []


# ---------------------------------------------------------------------------
# C17 / C18 — combined coverage + assert errors aggregate both messages
# ---------------------------------------------------------------------------


def test_run_gate_combined_setup_error_emits_both(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When BOTH coverage and assert phases hit setup_error, run_gate must
    emit both messages joined with `; ` — losing one side silently makes
    the operator chase a phantom problem.
    """
    from scripts.preflight_gate import (
        AssertResult,
        CoverageResult,
        run_gate,
    )

    cov = CoverageResult(setup_error="missing coverage.xml")
    asr = AssertResult(setup_error="parser unavailable")

    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._git_run", return_value="src.py\n"),
        patch("scripts.preflight_gate._diff_has_production_python", return_value=True),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=asr),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        rc = run_gate(tmp_path)

    assert rc == 2
    err = capsys.readouterr().err
    assert "missing coverage.xml" in err
    assert "parser unavailable" in err


def test_run_gate_combined_internal_error_emits_both(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.preflight_gate import (
        AssertResult,
        CoverageResult,
        run_gate,
    )

    cov = CoverageResult(internal_error="diff-cover crashed")
    asr = AssertResult(internal_error="helper crashed")

    with (
        patch("scripts.preflight_gate._load_config", return_value=GateConfig()),
        patch("scripts.preflight_gate._git_run", return_value="src.py\n"),
        patch("scripts.preflight_gate._diff_has_production_python", return_value=True),
        patch("scripts.preflight_gate._run_coverage_check", return_value=cov),
        patch("scripts.preflight_gate._run_assert_check", return_value=asr),
        patch("scripts.preflight_gate._run_assert_check_js", return_value=AssertResult()),
    ):
        rc = run_gate(tmp_path)

    assert rc == 3
    err = capsys.readouterr().err
    assert "diff-cover crashed" in err
    assert "helper crashed" in err


def test_coverage_check_staleness_git_log_empty_for_deleted_file(tmp_path: Path) -> None:
    """Deleted-from-worktree file with no commit history (git log returns
    empty) — fallback ladder ends without bumping mtime, but the loop
    must still continue cleanly to the next staged file.
    """
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')
    import time

    report_mtime = time.time()
    os.utime(report, (report_mtime, report_mtime))

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--name-only" in args:
            return "ghost.py\n"  # deleted, no history
        if args[0] == "log":
            return ""  # no commit history
        return ""

    json_out: dict[str, Any] = {}
    td = tmp_path / "td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        return proc

    with (
        patch("scripts.preflight_gate._git_run", side_effect=fake_git),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(GateConfig(), repo)

    assert result.setup_error is None


# ---------------------------------------------------------------------------
# Follow-up #1 — trivial-assertion detector
# ---------------------------------------------------------------------------


def test_trivial_assert_constant_true_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x():\n    assert True\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_trivial_assert_constant_one_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x():\n    assert 1\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_trivial_assert_constant_string_not_counted() -> None:
    import ast

    tree = ast.parse('def test_x():\n    assert "ok"\n')
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_trivial_assert_empty_list_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x():\n    assert []\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_trivial_assert_empty_dict_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x():\n    assert {}\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_trivial_assert_empty_tuple_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x():\n    assert ()\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_trivial_assert_empty_set_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x():\n    assert {1}\n")
    # ast.Set with content — still flagged as collection literal.
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_real_assert_name_counted() -> None:
    import ast

    tree = ast.parse("def test_x():\n    assert x\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 1


def test_real_assert_compare_counted() -> None:
    import ast

    tree = ast.parse("def test_x():\n    assert x == 1\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 1


def test_trivial_assert_true_call_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x(self):\n    self.assertTrue(True)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_trivial_assert_false_call_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x(self):\n    self.assertFalse(False)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_assert_true_with_name_counted() -> None:
    import ast

    tree = ast.parse("def test_x(self):\n    self.assertTrue(x)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 1


def test_trivial_assert_equal_same_constants_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x(self):\n    self.assertEqual(1, 1)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_trivial_assert_equal_same_name_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x(self):\n    self.assertEqual(x, x)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_real_assert_equal_different_args_counted() -> None:
    import ast

    tree = ast.parse("def test_x(self):\n    self.assertEqual(x, 1)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 1


def test_trivial_assertion_check_opt_out() -> None:
    """When trivial_assertion_check is disabled, `assert True` counts again."""
    import ast

    tree = ast.parse("def test_x():\n    assert True\n")
    func = tree.body[0]
    cfg = GateConfig(trivial_assertion_check=False)
    assert _count_assertions(func, cfg) == 1


def test_trivial_assertion_check_loaded_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.code_review.coverage_gate.assert_counter.python]\ntrivial_assertion_check = false\n"
    )
    cfg = _load_config(tmp_path)
    assert cfg.trivial_assertion_check is False


def test_trivial_assert_is_none_call_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x(self):\n    self.assertIsNone(None)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_trivial_assert_is_not_none_call_with_constant_not_counted() -> None:
    import ast

    tree = ast.parse('def test_x(self):\n    self.assertIsNotNone("x")\n')
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_trivial_assert_is_same_name_not_counted() -> None:
    import ast

    tree = ast.parse("def test_x(self):\n    self.assertIs(x, x)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 0


def test_real_assertx_call_with_three_args_counted() -> None:
    """assertEqual with len(args) >= 2 picks first two; extra args ignored."""
    import ast

    tree = ast.parse('def test_x(self):\n    self.assertEqual(x, 1, "msg")\n')
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 1


def test_assert_x_call_unrelated_no_special_handling() -> None:
    """assertOther / non-recognized assert* helpers fall through to the
    generic 'starts with assert' branch and count as 1.
    """
    import ast

    tree = ast.parse("def test_x(self):\n    self.assertGreaterThan(x, 1)\n")
    func = tree.body[0]
    assert _count_assertions(func, GateConfig()) == 1


def test_is_trivial_assertion_non_assert_node_returns_false() -> None:
    """Defensive: _is_trivial_assertion on a non-assert/non-call node
    returns False (for completeness in the AST walker).
    """
    import ast

    from scripts.preflight_gate import _is_trivial_assertion

    tree = ast.parse("x = 1\n")
    assert _is_trivial_assertion(tree.body[0]) is False


def test_is_trivial_assertion_call_with_no_attr_returns_false() -> None:
    """A `Call` whose callee is neither Name nor Attribute (e.g. nested
    Subscript) gets `attr=""` and short-circuits to False."""
    import ast

    from scripts.preflight_gate import _is_trivial_assertion

    tree = ast.parse("def test_x():\n    obj[0](1)\n")
    call = tree.body[0].body[0].value
    assert _is_trivial_assertion(call) is False


def test_is_trivial_assertion_call_non_assert_prefix_returns_false() -> None:
    """A Call whose callee starts with something other than 'assert' (e.g.
    `verify`) returns False — only assert-prefixed helpers are screened."""
    import ast

    from scripts.preflight_gate import _is_trivial_assertion

    tree = ast.parse("def test_x():\n    verify(True)\n")
    call = tree.body[0].body[0].value
    assert _is_trivial_assertion(call) is False


# ---------------------------------------------------------------------------
# Follow-up #2 — mock-of-unit-under-test detector
# ---------------------------------------------------------------------------


def _make_mock_uut_diff(prod_src: str, test_src: str) -> tuple[str, dict[str, str]]:
    """Build a diff covering one new prod file + one new test file.

    Returns (diff_text, sources_by_path).
    """
    prod_lines = prod_src.count("\n") or 1
    test_lines = test_src.count("\n") or 1
    diff = (
        f"diff --git a/src/mod.py b/src/mod.py\n"
        f"--- a/src/mod.py\n"
        f"+++ b/src/mod.py\n"
        f"@@ -0,0 +1,{prod_lines} @@\n"
        + "".join(f"+{ln}\n" for ln in prod_src.splitlines())
        + f"diff --git a/tests/test_mod.py b/tests/test_mod.py\n"
        f"--- a/tests/test_mod.py\n"
        f"+++ b/tests/test_mod.py\n"
        f"@@ -0,0 +1,{test_lines} @@\n" + "".join(f"+{ln}\n" for ln in test_src.splitlines())
    )
    return diff, {"src/mod.py": prod_src, "tests/test_mod.py": test_src}


def _fake_git_factory(diff: str, sources: dict[str, str]) -> Any:  # noqa: ANN401
    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--unified=0" in args:
            return diff
        if args[0] == "show":
            target = args[1].lstrip(":")
            return sources.get(target, "")
        return ""

    return fake_git


def test_mock_of_uut_decorator_patch() -> None:
    """`@patch("src.mod.process")` on a test where `process` is a new
    public def in the same diff → finding (test asserts on something
    OTHER than the mock itself).
    """
    prod = "def process(x):\n    return x\n"
    test = "from unittest.mock import patch\n@patch('src.mod.process')\ndef test_process(mock_p):\n    assert x == 1\n"
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    kinds = {f.kind for f in result.findings}
    assert "mocks_unit_under_test" in kinds


def test_mock_of_uut_with_block() -> None:
    """`with patch("src.mod.process"):` inside a test body → finding."""
    prod = "def process(x):\n    return x\n"
    test = (
        "from unittest.mock import patch\n"
        "def test_process():\n"
        "    with patch('src.mod.process'):\n"
        "        assert x == 1\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_mocker_patch() -> None:
    """`mocker.patch("src.mod.process")` (pytest-mock idiom) → finding."""
    prod = "def process(x):\n    return x\n"
    test = "def test_process(mocker):\n    mocker.patch('src.mod.process')\n    assert x == 1\n"
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_patch_object() -> None:
    """`@patch.object(M, "process")` form → finding when "process" is a UUT."""
    prod = "def process(x):\n    return x\n"
    test = (
        "from unittest.mock import patch\n@patch.object(some_mod, 'process')\ndef test_process(p):\n    assert x == 1\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_exempt_when_asserting_on_mock() -> None:
    """A test that mocks the UUT but ALSO asserts on the mock object
    (assert_called, called, call_count) is using the mock as assertion
    target — not silent UUT bypass. Exempted from finding.
    """
    prod = "def process(x):\n    return x\n"
    test = (
        "from unittest.mock import patch\n"
        "@patch('src.mod.process')\n"
        "def test_process(mock_p):\n"
        "    do_thing()\n"
        "    mock_p.assert_called_once_with(1)\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_exempt_called_attr() -> None:
    prod = "def process(x):\n    return x\n"
    test = (
        "from unittest.mock import patch\n"
        "def test_process():\n"
        "    with patch('src.mod.process') as m:\n"
        "        do_thing()\n"
        "        assert m.called\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_exempt_call_count_attr() -> None:
    prod = "def process(x):\n    return x\n"
    test = (
        "from unittest.mock import patch\n"
        "def test_process():\n"
        "    with patch('src.mod.process') as m:\n"
        "        do_thing()\n"
        "        assert m.call_count == 2\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_substring_filter_test_name_unrelated() -> None:
    """A test whose name does NOT contain the UUT symbol as substring
    is presumed to test the *caller* — patches there are dependency
    mocks, not UUT bypass.
    """
    prod = "def process(x):\n    return x\n"
    test = (
        "from unittest.mock import patch\n"
        "@patch('src.mod.process')\n"
        "def test_some_other_thing(_mock):\n"
        "    assert x == 1\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    # `process` not a substring of `test_some_other_thing` → no finding.
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_has_mock_behavioral_assertion_assert_not_called() -> None:
    """Direct unit test for the helper covering all the mock-assert
    attribute prefixes (assert_called*, assert_not_called*, called,
    call_count, call_args, call_args_list).
    """
    import ast

    from scripts.preflight_gate import _has_mock_behavioral_assertion

    for snippet in (
        "def test_x():\n    m.assert_called()\n",
        "def test_x():\n    m.assert_called_once()\n",
        "def test_x():\n    m.assert_called_with(1)\n",
        "def test_x():\n    m.assert_not_called()\n",
        "def test_x():\n    assert m.called\n",
        "def test_x():\n    assert m.call_count == 0\n",
        "def test_x():\n    args = m.call_args\n",
        "def test_x():\n    args = m.call_args_list\n",
    ):
        tree = ast.parse(snippet)
        assert _has_mock_behavioral_assertion(tree.body[0]) is True


def test_has_mock_behavioral_assertion_negative() -> None:
    import ast

    from scripts.preflight_gate import _has_mock_behavioral_assertion

    tree = ast.parse("def test_x():\n    assert x == 1\n")
    assert _has_mock_behavioral_assertion(tree.body[0]) is False


def test_mock_of_uut_unrelated_patch_no_finding() -> None:
    """Patching a symbol NOT in the added prod set → no mock-uut finding."""
    prod = "def process(x):\n    return x\n"
    test = (
        "from unittest.mock import patch\n"
        "@patch('os.path.exists')\n"  # unrelated stdlib
        "def test_process(_):\n"
        "    assert process(1)\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_check_opt_out() -> None:
    """When mock_of_uut_check is False, no mock-uut findings emitted."""
    prod = "def process(x):\n    return x\n"
    test = "from unittest.mock import patch\n@patch('src.mod.process')\ndef test_process(p):\n    assert process(1)\n"
    diff, src = _make_mock_uut_diff(prod, test)
    cfg = GateConfig(mock_of_uut_check=False)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(cfg, Path("/tmp"))
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_check_loaded_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.code_review.coverage_gate.assert_counter.python]\nmock_of_uut_check = false\n"
    )
    cfg = _load_config(tmp_path)
    assert cfg.mock_of_uut_check is False


def test_extract_patch_target_dynamic_arg_returns_none() -> None:
    """`patch(some_var)` (non-literal arg) cannot be resolved statically."""
    import ast

    from scripts.preflight_gate import _extract_patch_target

    tree = ast.parse("patch(some_var)")
    call = tree.body[0].value
    assert _extract_patch_target(call) is None


def test_extract_patch_target_non_call_returns_none() -> None:
    import ast

    from scripts.preflight_gate import _extract_patch_target

    tree = ast.parse("x = 1")
    assert _extract_patch_target(tree.body[0]) is None


def test_extract_patch_target_unrelated_callee_returns_none() -> None:
    """Call to something other than patch/patch.object → None."""
    import ast

    from scripts.preflight_gate import _extract_patch_target

    tree = ast.parse('foo("bar")')
    call = tree.body[0].value
    assert _extract_patch_target(call) is None


def test_extract_patch_target_patch_object_first_arg_string_skipped() -> None:
    """`patch.object("foo", "bar")` — first arg is string but second arg
    is the patched attribute name; helper takes args[1].
    """
    import ast

    from scripts.preflight_gate import _extract_patch_target

    tree = ast.parse('patch.object(SomeClass, "method")')
    call = tree.body[0].value
    assert _extract_patch_target(call) == "method"


def test_extract_patch_target_patch_object_no_second_arg_returns_none() -> None:
    """`patch.object(M)` (only one arg) is malformed → None."""
    import ast

    from scripts.preflight_gate import _extract_patch_target

    tree = ast.parse("patch.object(SomeClass)")
    call = tree.body[0].value
    assert _extract_patch_target(call) is None


def test_extract_patch_target_patch_object_non_string_second_arg() -> None:
    """`patch.object(M, x)` where x is a Name → can't statically resolve."""
    import ast

    from scripts.preflight_gate import _extract_patch_target

    tree = ast.parse("patch.object(SomeClass, attr)")
    call = tree.body[0].value
    assert _extract_patch_target(call) is None


def test_extract_patch_target_patch_no_args_returns_none() -> None:
    import ast

    from scripts.preflight_gate import _extract_patch_target

    tree = ast.parse("patch()")
    call = tree.body[0].value
    assert _extract_patch_target(call) is None


# ---------------------------------------------------------------------------
# Follow-up #3 — missing-test-reference detector
# ---------------------------------------------------------------------------


def test_missing_test_reference_finds_unreferenced_symbol() -> None:
    """A new public def whose name doesn't appear in any test source in
    the diff → missing_test_reference finding.
    """
    prod = "def alpha():\n    return 1\ndef beta():\n    return 2\n"
    # Test only references `alpha`; `beta` is unreferenced.
    test = "from src.mod import alpha\ndef test_alpha():\n    assert alpha() == 1\n"
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    miss = [f for f in result.findings if f.kind == "missing_test_reference"]
    assert any(f.function_name == "beta" for f in miss)
    assert not any(f.function_name == "alpha" for f in miss)


def test_missing_test_reference_class_method() -> None:
    """A new method on a non-Test class is a unit and must be referenced."""
    prod = "class Service:\n    def handle(self, x):\n        return x\n"
    test = "def test_other():\n    assert x == 1\n"  # no reference to handle
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    miss = {f.function_name for f in result.findings if f.kind == "missing_test_reference"}
    assert "handle" in miss


def test_missing_test_reference_skips_test_class_methods() -> None:
    """Methods of `class TestFoo` are NOT units under test; they're tests."""
    prod = "class TestThing:\n    def test_something(self):\n        assert x == 1\n"
    test = "def test_x():\n    assert x == 1\n"
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    # `test_something` is inside `TestThing` → skipped from prod symbols.
    assert not any(f.function_name == "test_something" for f in result.findings)


def test_missing_test_reference_word_boundary() -> None:
    """`process` referenced as `processed` (substring) does NOT count;
    word-boundary match avoids false negatives.
    """
    prod = "def process():\n    return 1\n"
    test = "def test_x():\n    processed = 1\n    assert processed == 1\n"
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    miss = {f.function_name for f in result.findings if f.kind == "missing_test_reference"}
    assert "process" in miss


def test_missing_test_reference_skips_underscore_prefixed() -> None:
    """`_helper` is private — not counted as a unit under test."""
    prod = "def _helper():\n    return 1\n"
    test = "def test_x():\n    assert x == 1\n"
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert not any(f.function_name == "_helper" for f in result.findings)


def test_missing_test_reference_check_opt_out() -> None:
    """When missing_test_reference_check is False, no findings emitted."""
    prod = "def alpha():\n    return 1\n"
    test = "def test_x():\n    assert x == 1\n"
    diff, src = _make_mock_uut_diff(prod, test)
    cfg = GateConfig(missing_test_reference_check=False)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(cfg, Path("/tmp"))
    assert not any(f.kind == "missing_test_reference" for f in result.findings)


def test_missing_test_reference_check_loaded_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.code_review.coverage_gate.assert_counter.python]\nmissing_test_reference_check = false\n"
    )
    cfg = _load_config(tmp_path)
    assert cfg.missing_test_reference_check is False


def test_missing_test_reference_async_function() -> None:
    """`async def` units are also collected."""
    prod = "async def fetch():\n    return 1\n"
    test = "def test_x():\n    assert x == 1\n"
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    miss = {f.function_name for f in result.findings if f.kind == "missing_test_reference"}
    assert "fetch" in miss


def test_collect_added_prod_symbols_skips_unparseable_file(tmp_path: Path) -> None:
    """Syntactically broken prod file is skipped silently — its symbols
    don't enter the added set, but other prod files are still processed.
    """
    from scripts.preflight_gate import _collect_added_prod_symbols

    sources = {"a.py": "def good():\n    pass\n", "b.py": "def : invalid"}

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "show":
            return sources.get(args[1].lstrip(":"), "")
        return ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        symbols = _collect_added_prod_symbols(
            ["a.py", "b.py"],
            {"a.py": {1, 2}, "b.py": {1}},
            tmp_path,
        )
    assert "good" in symbols
    assert len(symbols) == 1


def test_collect_added_prod_symbols_subprocess_error(tmp_path: Path) -> None:
    """`git show` failure on a file is non-fatal — that file is skipped."""
    from scripts.preflight_gate import _collect_added_prod_symbols

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        raise subprocess.CalledProcessError(1, args, "", "fatal: not found")

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        symbols = _collect_added_prod_symbols(["ghost.py"], {"ghost.py": {1}}, tmp_path)
    assert symbols == {}


def test_collect_added_prod_symbols_def_not_in_added_lines(tmp_path: Path) -> None:
    """A pre-existing def whose body is unchanged → def line not in
    added_by_file → not collected.
    """
    from scripts.preflight_gate import _collect_added_prod_symbols

    src = "def existing():\n    return 1\ndef new_one():\n    return 2\n"

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        return src if args[0] == "show" else ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        # Only line 3 (the `def new_one:`) is added.
        symbols = _collect_added_prod_symbols(["m.py"], {"m.py": {3}}, tmp_path)
    assert "new_one" in symbols
    assert "existing" not in symbols


def test_collect_added_prod_symbols_class_def_at_top_level(tmp_path: Path) -> None:
    """ClassDef at top level whose name is in added lines is collected."""
    from scripts.preflight_gate import _collect_added_prod_symbols

    src = "class Engine:\n    pass\n"

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        return src if args[0] == "show" else ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        symbols = _collect_added_prod_symbols(["e.py"], {"e.py": {1, 2}}, tmp_path)
    assert "Engine" in symbols


def test_mock_of_uut_dynamic_patch_arg_no_finding() -> None:
    """A dynamic patch arg (variable, not literal) cannot be resolved →
    no false-positive finding.
    """
    prod = "def process():\n    return 1\n"
    test = (
        "from unittest.mock import patch\n"
        "TARGET = 'src.mod.process'\n"
        "@patch(TARGET)\n"
        "def test_process(_):\n"
        "    assert x == 1\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_skipped_test_not_flagged() -> None:
    """A test marked @pytest.mark.skip is not scanned for any check."""
    prod = "def process():\n    return 1\n"
    test = (
        "import pytest\n"
        "from unittest.mock import patch\n"
        "@pytest.mark.skip\n"
        "@patch('src.mod.process')\n"
        "def test_process(_):\n"
        "    assert x == 1\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_collect_added_prod_symbols_underscore_class_skipped(tmp_path: Path) -> None:
    """`class _PrivateBase` is private — class itself not collected, but
    its public methods are still walked (since the `Test` filter is the
    only short-circuit, and `_PrivateBase` doesn't start with `Test`).
    """
    from scripts.preflight_gate import _collect_added_prod_symbols

    src = "class _PrivateBase:\n    def helper(self):\n        return 1\n"

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        return src if args[0] == "show" else ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        symbols = _collect_added_prod_symbols(["m.py"], {"m.py": {1, 2, 3}}, tmp_path)
    # Class itself (underscore-prefixed) skipped from public-symbol set.
    assert "_PrivateBase" not in symbols
    # Public methods on it still collected (they're public units).
    assert "helper" in symbols


def test_extract_patch_target_callee_no_attr_name_returns_none() -> None:
    """A Call whose callee yields `_attr_name(node.func) is None` — e.g.
    `obj["patch"](...)` (Subscript callee) — short-circuits to None.
    """
    import ast

    from scripts.preflight_gate import _extract_patch_target

    tree = ast.parse('obj["patch"]("x")')
    call = tree.body[0].value
    assert _extract_patch_target(call) is None


def test_mock_of_uut_with_block_non_call_context_expr() -> None:
    """`with x as y:` (Name, not Call) inside a test must not crash the
    mock-uut walker — context_expr falls through.
    """
    prod = "def process():\n    return 1\n"
    test = "def test_x():\n    cm = open_cm()\n    with cm:\n        assert x == 1\n"
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_with_block_unrelated_patch() -> None:
    """`with patch('os.path.exists'):` — patches something NOT in the
    added prod set; mock-uut walker continues to next item without firing.
    """
    prod = "def process():\n    return 1\n"
    test = (
        "from unittest.mock import patch\n"
        "def test_x():\n"
        "    with patch('os.path.exists'):\n"
        "        assert process(1) == 1\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_call_unrelated_patch_in_body() -> None:
    """`mocker.patch('unrelated.X')` inside body — last_seg X is not in
    added prod set; walker continues.
    """
    prod = "def process():\n    return 1\n"
    test = "def test_x(mocker):\n    mocker.patch('unrelated.thing')\n    assert process(1) == 1\n"
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_collect_added_prod_symbols_two_classes_in_one_file(tmp_path: Path) -> None:
    """Multiple top-level ClassDefs in one file — loop must continue
    after the first class's body is processed.
    """
    from scripts.preflight_gate import _collect_added_prod_symbols

    src = "class First:\n    def alpha(self):\n        return 1\nclass Second:\n    def beta(self):\n        return 2\n"

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        return src if args[0] == "show" else ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        symbols = _collect_added_prod_symbols(["m.py"], {"m.py": {1, 2, 3, 4, 5, 6}}, tmp_path)
    assert "First" in symbols
    assert "Second" in symbols
    assert "alpha" in symbols
    assert "beta" in symbols


def test_collect_added_prod_symbols_class_method_not_in_added(tmp_path: Path) -> None:
    """A class method whose def line is NOT in this commit's added lines
    is not collected (it's a pre-existing method).
    """
    from scripts.preflight_gate import _collect_added_prod_symbols

    src = (
        "class Service:\n"
        "    def existing(self):\n"  # pre-existing
        "        return 1\n"
        "    def fresh(self):\n"  # newly added (line 4 in this src)
        "        return 2\n"
    )

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        return src if args[0] == "show" else ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        # Only line 4 (the `def fresh`) is in added lines.
        symbols = _collect_added_prod_symbols(["m.py"], {"m.py": {4, 5}}, tmp_path)
    assert "fresh" in symbols
    assert "existing" not in symbols
    assert "Service" not in symbols  # class def line (1) not in added either


def test_collect_added_prod_symbols_private_class_method(tmp_path: Path) -> None:
    """`_helper` method on a public class is private — not collected."""
    from scripts.preflight_gate import _collect_added_prod_symbols

    src = "class Service:\n    def _helper(self):\n        return 1\n"

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        return src if args[0] == "show" else ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        symbols = _collect_added_prod_symbols(["m.py"], {"m.py": {1, 2, 3}}, tmp_path)
    assert "Service" in symbols
    assert "_helper" not in symbols


def test_collect_added_prod_symbols_skips_non_def_top_level(tmp_path: Path) -> None:
    """Top-level statements that are neither FunctionDef nor ClassDef
    (imports, assigns, expressions) are skipped silently — only public
    def/class declarations enter the symbol set.
    """
    from scripts.preflight_gate import _collect_added_prod_symbols

    src = (
        "import os\n"  # line 1: Import
        "X = 1\n"  # line 2: Assign
        "def public_fn():\n"  # line 3: FunctionDef
        "    return X\n"
    )

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        return src if args[0] == "show" else ""

    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        symbols = _collect_added_prod_symbols(["m.py"], {"m.py": {1, 2, 3, 4}}, tmp_path)
    assert "public_fn" in symbols
    assert len(symbols) == 1


# ---------------------------------------------------------------------------
# Follow-up #4 — strict coverage-freshness check (opt-in)
# ---------------------------------------------------------------------------


def test_strict_freshness_default_off(tmp_path: Path) -> None:
    """Default GateConfig has coverage_freshness_strict=False; no
    additional setup_error from freshness even if file is missing.
    """
    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text(
        '<coverage branch-rate="1.0">'
        "<packages><package><classes>"
        '<class filename="other.py"></class>'
        "</classes></package></packages></coverage>"
    )

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--name-only" in args:
            return "new_file.py\n"
        return ""

    json_out: dict[str, Any] = {}
    td = tmp_path / "td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        return proc

    with (
        patch("scripts.preflight_gate._git_run", side_effect=fake_git),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        # Default: freshness_strict=False — no setup_error from freshness.
        result = _run_coverage_check(GateConfig(), repo)

    assert result.setup_error is None


def test_strict_freshness_on_flags_missing_file(tmp_path: Path) -> None:
    """With coverage_freshness_strict=True, an added prod file missing
    from coverage.xml's `<class filename=>` list → setup_error.
    """
    repo = _make_repo(tmp_path)
    # coverage.xml mentions only "other.py", NOT "new_file.py".
    report = repo / "coverage.xml"
    report.write_text(
        '<coverage branch-rate="1.0">'
        "<packages><package><classes>"
        '<class filename="other.py"></class>'
        "</classes></package></packages></coverage>"
    )
    (repo / "new_file.py").write_text("def f(): pass\n")
    (repo / "other.py").write_text("def g(): pass\n")
    # Bump report mtime so basic staleness check passes; we want the
    # missing-file freshness branch to be the failure.
    import time

    future = time.time() + 60
    os.utime(report, (future, future))

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--name-only" in args:
            return "new_file.py\n"
        return ""

    cfg = GateConfig(coverage_freshness_strict=True)
    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        result = _run_coverage_check(cfg, repo)

    assert result.setup_error is not None
    assert "missing 1 added file" in result.setup_error
    assert "new_file.py" in result.setup_error


def test_strict_freshness_on_skips_excluded_files(tmp_path: Path) -> None:
    """Files matching coverage_exclude_paths don't need to be in xml."""
    import time

    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text(
        '<coverage branch-rate="1.0">'
        "<packages><package><classes>"
        '<class filename="src/main.py"></class>'
        "</classes></package></packages></coverage>"
    )
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("def f(): pass\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "tool.py").write_text("def g(): pass\n")
    future = time.time() + 60
    os.utime(report, (future, future))

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--name-only" in args:
            # Excluded path — must NOT trigger missing-file error.
            return "scripts/tool.py\nsrc/main.py\n"
        return ""

    json_out: dict[str, Any] = {}
    td = tmp_path / "td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        return proc

    cfg = GateConfig(
        coverage_freshness_strict=True,
        coverage_exclude_paths=("scripts/*",),
    )
    with (
        patch("scripts.preflight_gate._git_run", side_effect=fake_git),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(cfg, repo)

    assert result.setup_error is None


def test_strict_freshness_on_skips_test_files(tmp_path: Path) -> None:
    """Test files don't need to appear in coverage.xml."""
    import time

    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text(
        '<coverage branch-rate="1.0">'
        "<packages><package><classes>"
        '<class filename="src/main.py"></class>'
        "</classes></package></packages></coverage>"
    )
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("def f(): pass\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_main.py").write_text("def test_f(): assert x == 1\n")
    # Bump report mtime so basic staleness check passes; the test focuses
    # on freshness_strict skipping test files.
    future = time.time() + 60
    os.utime(report, (future, future))

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--name-only" in args:
            return "src/main.py\ntests/test_main.py\n"
        return ""

    json_out: dict[str, Any] = {}
    td = tmp_path / "td"
    td.mkdir()

    class FakeTempDir:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> str:
            return self.path

        def __exit__(self, *args: Any) -> None:  # noqa: ANN401
            pass

    def fake_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        for arg in args[0]:
            if arg.startswith("--format=json:"):
                p = Path(arg[len("--format=json:") :])
                p.write_text(__import__("json").dumps(json_out))
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        return proc

    cfg = GateConfig(coverage_freshness_strict=True)
    with (
        patch("scripts.preflight_gate._git_run", side_effect=fake_git),
        patch("scripts.preflight_gate.tempfile.TemporaryDirectory", return_value=FakeTempDir(str(td))),
        patch("scripts.preflight_gate.subprocess.run", side_effect=fake_run),
        patch("scripts.preflight_gate._resolve_compare_branch", return_value="HEAD"),
    ):
        result = _run_coverage_check(cfg, repo)

    assert result.setup_error is None


def test_strict_freshness_skips_non_python_files() -> None:
    """The freshness helper only inspects .py files; non-py staged files
    (e.g. .yaml, .md) don't count toward the missing list.
    """
    from scripts.preflight_gate import _strict_freshness_missing_files

    cfg = GateConfig()
    missing = _strict_freshness_missing_files(
        cfg,
        ["docs/readme.md", "config.yaml", "src/main.py"],
        {Path("coverage.xml"): '<class filename="src/main.py"></class>'},
    )
    assert missing == []


def test_strict_freshness_no_python_short_circuits() -> None:
    """When no staged file is .py, freshness check returns empty list."""
    from scripts.preflight_gate import _strict_freshness_missing_files

    cfg = GateConfig()
    missing = _strict_freshness_missing_files(
        cfg,
        ["readme.md", "data.json"],
        {Path("coverage.xml"): "<coverage></coverage>"},
    )
    assert missing == []


def test_strict_freshness_many_missing_files_truncated_in_message(tmp_path: Path) -> None:
    """When >3 files are missing, the error message shows first 3 + "(+N more)"."""
    import time

    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"></coverage>')
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
        (repo / name).write_text("def f(): pass\n")
    future = time.time() + 60
    os.utime(report, (future, future))

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--name-only" in args:
            return "a.py\nb.py\nc.py\nd.py\ne.py\n"
        return ""

    cfg = GateConfig(coverage_freshness_strict=True)
    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        result = _run_coverage_check(cfg, repo)

    assert result.setup_error is not None
    assert "missing 5 added file" in result.setup_error
    assert "+2 more" in result.setup_error


def test_strict_freshness_loaded_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.code_review.coverage_gate.coverage_check]\nfreshness_strict = true\n"
    )
    cfg = _load_config(tmp_path)
    assert cfg.coverage_freshness_strict is True


def test_glob_match_simple_question_mark() -> None:
    from scripts.preflight_gate import _glob_match_simple

    assert _glob_match_simple("ab.py", "a?.py") is True
    assert _glob_match_simple("abc.py", "a?.py") is False


def test_glob_match_simple_double_star_recursive() -> None:
    from scripts.preflight_gate import _glob_match_simple

    assert _glob_match_simple("a/b/c.py", "**/c.py") is True
    assert _glob_match_simple("a/b/c.py", "a/**/c.py") is True


def test_glob_match_simple_bare_double_star() -> None:
    """Bare `**` (no trailing slash) matches any chars including slashes."""
    from scripts.preflight_gate import _glob_match_simple

    assert _glob_match_simple("a/b.py", "**b.py") is True


def test_strict_freshness_xml_with_no_class_entries(tmp_path: Path) -> None:
    """If coverage.xml exists but has no `<class filename=>` entries
    (empty packages), every staged .py file appears as missing.
    """
    import time

    repo = _make_repo(tmp_path)
    report = repo / "coverage.xml"
    report.write_text('<coverage branch-rate="1.0"><packages></packages></coverage>')
    (repo / "lonely.py").write_text("def f(): pass\n")
    future = time.time() + 60
    os.utime(report, (future, future))

    def fake_git(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "diff" and "--name-only" in args:
            return "lonely.py\n"
        return ""

    cfg = GateConfig(coverage_freshness_strict=True)
    with patch("scripts.preflight_gate._git_run", side_effect=fake_git):
        result = _run_coverage_check(cfg, repo)

    assert result.setup_error is not None
    assert "lonely.py" in result.setup_error


def test_strict_freshness_test_file_pattern_path_with_no_excludes() -> None:
    """When coverage_exclude_paths is empty, is_test() runs and short-
    circuits on a test-file pattern match — covers the test-file branch.
    """
    from scripts.preflight_gate import _strict_freshness_missing_files

    cfg = GateConfig(
        coverage_exclude_paths=(),  # nothing excluded
        test_file_patterns=("**/test_*.py",),
    )
    missing = _strict_freshness_missing_files(
        cfg,
        ["src/main.py", "tests/test_thing.py"],
        {Path("coverage.xml"): '<class filename="src/main.py"></class>'},
    )
    # tests/test_thing.py matches **/test_*.py via is_test → not in missing.
    assert missing == []


def test_mock_of_uut_with_block_unrelated_target_in_relevant_test() -> None:
    """Test name contains the UUT symbol (so substring filter passes),
    but the patched target inside `with` is unrelated — last_seg not in
    relevant → walker advances past the item without returning.
    """
    prod = "def process():\n    return 1\n"
    test = (
        "from unittest.mock import patch\n"
        "def test_process():\n"
        "    with patch('os.path.exists'):\n"
        "        assert x == 1\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    # Substring filter passes (process in test_process), but `exists` not
    # in relevant set → walker exits without finding.
    assert not any(f.kind == "mocks_unit_under_test" for f in result.findings)


def test_mock_of_uut_with_block_two_items_first_unrelated() -> None:
    """`with patch('a'), patch('b'):` — two items in one with stmt.
    First item not relevant; loop must advance to second item.
    """
    prod = "def process():\n    return 1\n"
    test = (
        "from unittest.mock import patch\n"
        "def test_process():\n"
        "    with patch('os.path.exists'), patch('src.mod.process'):\n"
        "        assert x == 1\n"
    )
    diff, src = _make_mock_uut_diff(prod, test)
    with patch("scripts.preflight_gate._git_run", side_effect=_fake_git_factory(diff, src)):
        result = _run_assert_check(GateConfig(), Path("/tmp"))
    # Second item DOES match → finding emitted.
    assert any(f.kind == "mocks_unit_under_test" for f in result.findings)

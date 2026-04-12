from __future__ import annotations

from unittest.mock import MagicMock, patch

from hooks.pre_commit_review import MAX_DIFF_LINES, check_diff_size, run_opencode


def test_under_limit_returns_none() -> None:
    diff = "\n".join(f"line {i}" for i in range(MAX_DIFF_LINES))
    assert check_diff_size(diff) is None


def test_over_limit_returns_message() -> None:
    diff = "\n".join(f"line {i}" for i in range(MAX_DIFF_LINES + 1))
    result = check_diff_size(diff)
    assert result is not None
    assert "Split" in result
    assert str(MAX_DIFF_LINES) in result


def test_run_opencode_builds_correct_command() -> None:
    mock_result = MagicMock()
    mock_result.stdout = "OK\n"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("hooks.pre_commit_review.subprocess.run", return_value=mock_result) as mock_run:
        stdout, stderr, rc = run_opencode("sys", "user")

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "opencode"
    assert "--model" in cmd
    assert "github-copilot/claude-sonnet-4.6" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "run" in cmd
    assert cmd[-1] == "sys\n\nuser"
    assert stdout == "OK"
    assert rc == 0


def test_run_opencode_empty_system_prompt() -> None:
    mock_result = MagicMock()
    mock_result.stdout = "OK"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("hooks.pre_commit_review.subprocess.run", return_value=mock_result) as mock_run:
        run_opencode("", "user only")

    cmd = mock_run.call_args[0][0]
    assert cmd[-1] == "user only"

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hooks.pre_commit_review import (
    MAX_DIFF_LINES,
    _parse_opencode_json,
    check_diff_size,
    run_opencode,
)


def test_under_limit_returns_none() -> None:
    diff = "\n".join(f"line {i}" for i in range(MAX_DIFF_LINES))
    assert check_diff_size(diff) is None


def test_over_limit_returns_message() -> None:
    diff = "\n".join(f"line {i}" for i in range(MAX_DIFF_LINES + 1))
    result = check_diff_size(diff)
    assert result is not None
    assert "Split" in result
    assert str(MAX_DIFF_LINES) in result


def test_parse_opencode_json_extracts_last_text() -> None:
    raw = (
        '{"type":"step_start","timestamp":1}\n'
        '{"type":"text","timestamp":2,"part":{"type":"text","text":"partial"}}\n'
        '{"type":"text","timestamp":3,"part":{"type":"text","text":"[WARNING] foo\\n\\nOK"}}\n'
        '{"type":"step_finish","timestamp":4}\n'
    )
    assert _parse_opencode_json(raw) == "[WARNING] foo\n\nOK"


def test_parse_opencode_json_empty_output() -> None:
    assert _parse_opencode_json("") == ""
    assert _parse_opencode_json('{"type":"step_start"}\n') == ""


def test_run_opencode_builds_correct_command() -> None:
    json_output = '{"type":"text","part":{"type":"text","text":"OK"}}\n'
    mock_result = MagicMock()
    mock_result.stdout = json_output
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("hooks.pre_commit_review.subprocess.run", return_value=mock_result) as mock_run:
        stdout, stderr, rc = run_opencode("sys", "user")

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "opencode"
    assert "run" in cmd
    assert "--pure" in cmd
    assert "--format" in cmd
    assert "json" in cmd
    assert "--model" in cmd
    assert "github-copilot/claude-sonnet-4.6" in cmd
    assert cmd[-1] == "sys\n\nuser"
    assert stdout == "OK"
    assert rc == 0


def test_run_opencode_empty_system_prompt() -> None:
    json_output = '{"type":"text","part":{"type":"text","text":"OK"}}\n'
    mock_result = MagicMock()
    mock_result.stdout = json_output
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("hooks.pre_commit_review.subprocess.run", return_value=mock_result) as mock_run:
        run_opencode("", "user only")

    cmd = mock_run.call_args[0][0]
    assert cmd[-1] == "user only"

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from hooks.conftest import StdinSetter
from hooks.lint import main


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


# ---------------------------------------------------------------------------
# Non-.py files and malformed input are no-ops
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"tool_input": {"file_path": "foo.md"}, "cwd": "/tmp"}, id="non-py-extension"),
        pytest.param({"tool_input": {}, "cwd": "/tmp"}, id="missing-file-path"),
        pytest.param("not json at all", id="bad-json"),
    ],
)
def test_main_no_op_on_invalid_input(stdin_payload: StdinSetter, payload: object) -> None:
    stdin_payload(payload)
    with patch("hooks.lint.subprocess.run") as mock_run:
        main()
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# .py files run the full ruff pipeline in a specific order
# ---------------------------------------------------------------------------


def test_main_runs_full_pipeline_in_order(stdin_payload: StdinSetter) -> None:
    """Pipeline contract: `ruff check --fix` runs BEFORE `ruff format`
    because `--fix` can rewrite imports/code that then need reformatting;
    if the order inverted, the file would land in a non-formatted state
    and the project pre-commit would rewrite it, reproducing the stale-
    file / "files were modified by this hook" abort this hook prevents.
    Reporting steps follow."""
    stdin_payload({"tool_input": {"file_path": "a.py"}, "cwd": "/w"})
    with patch("hooks.lint.subprocess.run", return_value=_completed()) as mock_run:
        main()

    commands = [call.args[0] for call in mock_run.call_args_list]
    assert commands == [
        ["ruff", "check", "--fix", "a.py"],
        ["ruff", "format", "a.py"],
        ["ruff", "check", "a.py"],
        ["ruff", "check", "--select", "ANN", "a.py"],
    ]


@pytest.mark.parametrize(
    ("cwd_input", "expected_cwd"),
    [
        pytest.param("/work/repo", "/work/repo", id="explicit-cwd"),
        pytest.param(None, ".", id="default-cwd"),
    ],
)
def test_main_passes_cwd_to_subprocess(
    stdin_payload: StdinSetter,
    cwd_input: str | None,
    expected_cwd: str,
) -> None:
    payload: dict[str, object] = {"tool_input": {"file_path": "x.py"}}
    if cwd_input is not None:
        payload["cwd"] = cwd_input
    stdin_payload(payload)
    with patch("hooks.lint.subprocess.run", return_value=_completed()) as mock_run:
        main()

    for call in mock_run.call_args_list:
        assert call.kwargs["cwd"] == expected_cwd


# ---------------------------------------------------------------------------
# Reporting behavior
# ---------------------------------------------------------------------------


def test_main_prints_check_stdout_to_stderr(
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload({"tool_input": {"file_path": "x.py"}, "cwd": "."})

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["ruff", "check", "x.py"]:
            return _completed(stdout="E999 some error")
        if cmd[:2] == ["ruff", "check"] and "--select" in cmd:
            return _completed(stdout="ANN201 missing annotation")
        return _completed()

    with patch("hooks.lint.subprocess.run", side_effect=fake_run):
        main()

    captured = capsys.readouterr()
    assert "E999 some error" in captured.err
    assert "ANN201 missing annotation" in captured.err


def test_main_silent_when_ruff_missing(stdin_payload: StdinSetter) -> None:
    stdin_payload({"tool_input": {"file_path": "x.py"}, "cwd": "."})
    with patch("hooks.lint.subprocess.run", side_effect=FileNotFoundError):
        main()  # must not raise

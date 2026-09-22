"""Tests for git-hooks/pre-push, the layer that reads no command text.

Everything else in this directory tests the guard, which refuses a spelling.
The hook refuses the act, and until now nothing exercised it: deleting either
of its checks left the whole suite green while a Claude-originated push went
through.

Two of its three inputs can be supplied here. The environment is ordinary, and
a controlling terminal comes from a pty. Process ancestry cannot be faked from
inside a Claude-spawned process, so the tests that need "no `claude` ancestor"
run against a copy with that one function stubbed, and a separate test pins the
real function's behaviour from where it actually runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path("/home/kern/.claude/git-hooks/pre-push")
REMOTE_URL = "git@github.com:some-org/some-repo.git"

# The hook exits 1 to refuse and 0 to allow; git turns the former into a
# failed push.
REFUSED = 1
ALLOWED = 0


@pytest.fixture
def hook_without_ancestry(tmp_path: Path) -> Path:
    """The hook, with the `claude` ancestry check forced to find nothing.

    A test process here always has that ancestor, so the only way to exercise
    the rest of the hook is to stub the one input that cannot be arranged.
    """
    stubbed = tmp_path / "pre-push"
    source = HOOK.read_text().replace(
        "claude_process_ancestor() {",
        "claude_process_ancestor() { return 1; ",
        1,
    )
    stubbed.write_text(source)
    stubbed.chmod(0o755)
    return stubbed


def run_hook(
    hook: Path,
    *,
    env: dict[str, str] | None = None,
    remote: str = REMOTE_URL,
    tty: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke the hook the way git does: `pre-push <remote-name> <remote-url>`."""
    command = ["bash", str(hook), "origin", remote]
    environment = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    environment.update(env or {})
    if tty:
        # `script` allocates a real pty, which is the only way to give the hook
        # a controlling terminal from here.
        inner = " ".join(f"'{part}'" for part in command)
        command = ["script", "-qec", inner, "/dev/null"]
    return subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


# ---------- ancestry: the check that holds in both directions ----------


def test_a_claude_descendant_is_refused() -> None:
    """This test process has `claude` among its ancestors, so the unstubbed
    hook must refuse it -- and must say ancestry, not the environment.
    """
    result = run_hook(HOOK, env={"CLAUDECODE": "1"})
    assert result.returncode == REFUSED
    assert "PUSH DENIED" in result.stderr
    assert "ancestry" in result.stderr


def test_ancestry_outranks_having_a_terminal() -> None:
    """A pty does not make a Claude-spawned process into a person."""
    result = run_hook(HOOK, tty=True)
    assert "PUSH DENIED" in result.stdout + result.stderr
    assert "ancestry" in result.stdout + result.stderr


# ---------- the environment fallback, and the terminal that gates it ----------


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("CLAUDECODE", id="claudecode"),
        pytest.param("CLAUDE_CODE_ENTRYPOINT", id="entrypoint"),
        pytest.param("CLAUDE_CODE_SESSION_ID", id="session-id"),
        pytest.param("AI_AGENT", id="ai-agent"),
    ],
)
def test_a_detached_process_carrying_the_environment_is_refused(
    hook_without_ancestry: Path,
    marker: str,
) -> None:
    """No ancestor and no terminal: whatever this is, it is not a person, and
    the environment it kept is the only thing left to go on.
    """
    result = run_hook(hook_without_ancestry, env={marker: "1"})
    assert result.returncode == REFUSED
    assert "PUSH DENIED" in result.stderr
    assert marker in result.stderr


def test_the_users_terminal_is_allowed_despite_the_extension_variable(
    hook_without_ancestry: Path,
) -> None:
    """The regression this gate exists for.

    The VS Code extension exports CLAUDE_CODE_SSE_PORT into every integrated
    terminal -- it advertises a reachable IDE, it does not identify the caller
    -- and treating it as proof refused the user's own push.
    """
    result = run_hook(
        hook_without_ancestry,
        env={"CLAUDE_CODE_SSE_PORT": "41337"},
        tty=True,
    )
    assert result.returncode == ALLOWED
    assert "PUSH DENIED" not in result.stdout + result.stderr


def test_a_terminal_with_the_whole_family_of_variables_is_still_allowed(
    hook_without_ancestry: Path,
) -> None:
    result = run_hook(
        hook_without_ancestry,
        env={
            "CLAUDE_CODE_SSE_PORT": "41337",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "AI_AGENT": "1",
        },
        tty=True,
    )
    assert result.returncode == ALLOWED


def test_a_plain_shell_with_no_markers_is_allowed(hook_without_ancestry: Path) -> None:
    assert run_hook(hook_without_ancestry).returncode == ALLOWED


# ---------- no-push.list ----------


@pytest.fixture
def hook_with_blocklist(tmp_path: Path, hook_without_ancestry: Path) -> Path:
    """The stubbed hook beside a blocklist of our own.

    The hook reads the list from its own directory, so the copy needs one.
    """
    (tmp_path / "no-push.list").write_text(
        "# a comment\n\n  some-org/read-only-repo  \nanother-org/Mixed-Case  # trailing comment\n",
    )
    return hook_without_ancestry


@pytest.mark.parametrize(
    "remote",
    [
        pytest.param("git@github.com:some-org/read-only-repo.git", id="ssh-with-suffix"),
        pytest.param("git@github.com:some-org/read-only-repo", id="ssh-without-suffix"),
        pytest.param("git@gh-alias:some-org/read-only-repo.git", id="ssh-host-alias"),
        pytest.param("https://github.com/some-org/read-only-repo.git", id="https"),
        pytest.param("git@github.com:Some-Org/Read-Only-Repo.git", id="different-case"),
        pytest.param("git@github.com:another-org/mixed-case.git", id="entry-was-mixed-case"),
    ],
)
def test_a_listed_repository_is_refused(hook_with_blocklist: Path, remote: str) -> None:
    """One entry has to match every URL shape the same repository comes in as,
    which is what the lowercasing and the `.git` trim are for.
    """
    result = run_hook(hook_with_blocklist, remote=remote)
    assert result.returncode == REFUSED
    assert "read-only" in result.stderr.lower() or "no-push.list" in result.stderr


@pytest.mark.parametrize(
    "remote",
    [
        pytest.param("git@github.com:some-org/a-different-repo.git", id="same-org"),
        pytest.param("git@github.com:other/read-only-repo-extra.git", id="longer-name"),
        pytest.param("https://github.com/unrelated/project.git", id="unrelated"),
    ],
)
def test_an_unlisted_repository_is_allowed(hook_with_blocklist: Path, remote: str) -> None:
    assert run_hook(hook_with_blocklist, remote=remote).returncode == ALLOWED


def test_a_comment_only_list_blocks_nothing(
    tmp_path: Path,
    hook_without_ancestry: Path,
) -> None:
    (tmp_path / "no-push.list").write_text("# nothing here\n\n   \n")
    assert run_hook(hook_without_ancestry).returncode == ALLOWED


def test_a_missing_list_is_not_an_error(
    tmp_path: Path,
    hook_without_ancestry: Path,
) -> None:
    """The blocklist is optional; its absence must not refuse every push."""
    missing = tmp_path / "no-push.list"
    if missing.exists():
        missing.unlink()
    assert run_hook(hook_without_ancestry).returncode == ALLOWED


# ---------- the registration that makes the guard run at all ----------


def test_the_guard_is_registered_for_every_tool() -> None:
    """Every guard test calls main() directly, so the registration itself was
    unpinned: putting the matcher back to "Bash" would leave them all green
    while Write, Edit and the code tool walked past the guard.
    """
    import json

    settings = json.loads(Path("/home/kern/.claude/settings.json").read_text())
    entries = settings["hooks"]["PreToolUse"]
    matchers = {entry["matcher"] for entry in entries}
    assert "*" in matchers, f"guard must run for every tool, got matchers {matchers}"

    commands = {hook["command"] for entry in entries for hook in entry["hooks"]}
    assert any("guard.py" in command for command in commands), commands


def test_the_hook_script_is_executable() -> None:
    assert shutil.which("bash") is not None
    assert os.access(HOOK, os.X_OK), f"{HOOK} must be executable for git to run it"

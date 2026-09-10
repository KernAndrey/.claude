from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from hooks.conftest import StdinSetter
from hooks.guard import (
    _cd_destination,
    _expand_locals,
    _local_variables,
    _split_segments,
    _substitutions,
    _write_targets,
    block,
    check_command,
    check_path,
    check_relative_target,
    main,
    normalize_code,
    normalize_paths,
)


@pytest.mark.parametrize(
    ("command", "expected_rule"),
    [
        # ---------- git-no-verify ----------
        pytest.param(
            "git commit --no-verify -m 'x'",
            "git-no-verify",
            id="no-verify-long",
        ),
        pytest.param(
            "foo && git commit --no-verify -m 'x'",
            "git-no-verify",
            id="no-verify-chained",
        ),
        pytest.param("git commit -m 'x'", None, id="plain-commit-allowed"),
        pytest.param(
            "git push -n origin main",
            "git-push-denied",
            id="push-dry-run-now-denied",
        ),
        pytest.param("ls -la", None, id="unrelated-allowed"),
        pytest.param("echo --no-verify", None, id="no-verify-without-git-allowed"),
        # ---------- review-approvals-write ----------
        pytest.param("echo x > .review/approvals/abc123", "review-approvals-write", id="approvals-redirect"),
        pytest.param("echo x >> .review/approvals/abc123", "review-approvals-write", id="approvals-append"),
        pytest.param("touch .review/approvals/deadbeef", "review-approvals-write", id="approvals-touch"),
        pytest.param("cp /tmp/x .review/approvals/h", "review-approvals-write", id="approvals-cp"),
        pytest.param("printf x | tee .review/approvals/h", "review-approvals-write", id="approvals-tee"),
        pytest.param("cat .review/approvals/abc123", None, id="approvals-read-allowed"),
        pytest.param("ls .review/approvals", None, id="approvals-ls-allowed"),
        pytest.param(
            "python3 ~/.claude/review/pre_review.py --plan .review/prereview-plan.json --reset",
            None,
            id="prereview-script-allowed",
        ),
        pytest.param("echo '{}' > .review/prereview-plan.json", None, id="prereview-plan-write-allowed"),
        # ---------- git-force-push ----------
        pytest.param("git push --force", "git-force-push", id="push-force-long"),
        pytest.param(
            "git push -f origin main",
            "git-force-push",
            id="push-f-short",
        ),
        pytest.param(
            "git push origin main -f",
            "git-force-push",
            id="push-f-trailing",
        ),
        pytest.param(
            "git push --force-with-lease feature",
            "git-force-push",
            id="push-force-with-lease",
        ),
        pytest.param(
            "git push -fv origin main",
            "git-force-push",
            id="push-fv-combined",
        ),
        pytest.param(
            "git push -vf origin main",
            "git-force-push",
            id="push-vf-combined",
        ),
        pytest.param(
            "git push -fu upstream feature",
            "git-force-push",
            id="push-fu-combined",
        ),
        # The cases below still pin git-force-push precision: because it is
        # checked before git-push-denied, a git-push-denied verdict proves the
        # force regex did not match the branch name.
        pytest.param(
            "git push origin main",
            "git-push-denied",
            id="push-plain-now-denied",
        ),
        pytest.param(
            "git push origin feature",
            "git-push-denied",
            id="push-feature-now-denied",
        ),
        pytest.param(
            "git push origin feature-fix",
            "git-push-denied",
            id="push-branch-with-dash-f-now-denied",
        ),
        pytest.param(
            "git push origin some-foo",
            "git-push-denied",
            id="push-branch-some-foo-now-denied",
        ),
        # Regression: numeric prefix before `-fix` must NOT trigger force-push.
        pytest.param(
            "git push origin task/USKO-032-fix-remaining-test-failures",
            "git-push-denied",
            id="push-branch-numeric-dash-fix-now-denied",
        ),
        pytest.param(
            "git push origin 123-fu-branch",
            "git-push-denied",
            id="push-branch-numeric-dash-fu-now-denied",
        ),
        pytest.param(
            "git push origin task/USKO-01-feature",
            "git-push-denied",
            id="push-branch-numeric-dash-feature-now-denied",
        ),
        pytest.param(
            "git push origin release/2025-fast-hotfix",
            "git-push-denied",
            id="push-branch-numeric-dash-fast-now-denied",
        ),
        pytest.param(
            "git push origin v1.2-fun",
            "git-push-denied",
            id="push-branch-dotted-dash-fun-now-denied",
        ),
        # ---------- git-branch-force-delete ----------
        pytest.param(
            "git branch -D feature",
            "git-branch-force-delete",
            id="branch-D",
        ),
        pytest.param(
            "git branch --delete --force feature",
            "git-branch-force-delete",
            id="branch-delete-force",
        ),
        pytest.param(
            "git branch -df feature",
            "git-branch-force-delete",
            id="branch-df-combined",
        ),
        pytest.param(
            "git branch -fd feature",
            "git-branch-force-delete",
            id="branch-fd-combined",
        ),
        pytest.param(
            "git branch -dfv feature",
            "git-branch-force-delete",
            id="branch-dfv-with-verbose",
        ),
        pytest.param("git branch -d feature", None, id="branch-d-merged-allowed"),
        pytest.param(
            "git branch -dv feature",
            None,
            id="branch-dv-non-force-allowed",
        ),
        pytest.param(
            "git branch -d fix-draft",
            None,
            id="branch-d-fix-draft-allowed",
        ),
        pytest.param(
            "git branch -d add-pdf-export",
            None,
            id="branch-d-add-pdf-export-allowed",
        ),
        pytest.param(
            "git branch -dv fix-draft",
            None,
            id="branch-dv-fix-draft-allowed",
        ),
        # Regression: numeric prefix before a `-dfx`-like embedded token must
        # NOT trigger force-delete — the short-flag alternative requires a
        # whitespace/SOL boundary before the leading dash.
        pytest.param(
            "git branch -d task/USKO-032-dfx-feature",
            None,
            id="branch-d-numeric-dash-dfx-allowed",
        ),
        pytest.param(
            "git branch -d release/2025-fdx-hotfix",
            None,
            id="branch-d-numeric-dash-fdx-allowed",
        ),
        pytest.param("git branch", None, id="branch-list-allowed"),
        pytest.param(
            "git branch --list 'feature/*'",
            None,
            id="branch-list-glob-allowed",
        ),
        # ---------- git-rebase-protected ----------
        pytest.param("git rebase main", "git-rebase-protected", id="rebase-main"),
        pytest.param(
            "git rebase origin/master",
            "git-rebase-protected",
            id="rebase-origin-master",
        ),
        pytest.param(
            "git rebase upstream/dev",
            "git-rebase-protected",
            id="rebase-upstream-dev",
        ),
        pytest.param(
            "git rebase -i main",
            "git-rebase-protected",
            id="rebase-interactive-main",
        ),
        pytest.param(
            "git rebase --onto main feature~5 feature",
            "git-rebase-protected",
            id="rebase-onto-main",
        ),
        pytest.param(
            "git rebase feature-branch",
            None,
            id="rebase-feature-allowed",
        ),
        pytest.param(
            "git rebase main-feature",
            None,
            id="rebase-main-feature-no-fp",
        ),
        pytest.param(
            "git rebase development",
            None,
            id="rebase-development-no-fp",
        ),
        pytest.param(
            "git rebase feature-main",
            None,
            id="rebase-feature-main-no-fp",
        ),
        pytest.param(
            "git rebase prod-dev",
            None,
            id="rebase-prod-dev-no-fp",
        ),
        # ---------- git-no-gpg-sign ----------
        pytest.param(
            "git commit --no-gpg-sign",
            "git-no-gpg-sign",
            id="commit-no-gpg-sign",
        ),
        pytest.param(
            "git -c commit.gpgsign=false commit -m x",
            "git-no-gpg-sign",
            id="commit-gpgsign-false",
        ),
        pytest.param("git commit -m x", None, id="plain-commit-gpg-allowed"),
        # Regression: `-config` embedded in a commit message/arg after a
        # numeric prefix must NOT trigger the `-c commit.gpgsign=false` rule.
        pytest.param(
            "git commit -m 'USKO-01-config work'",
            None,
            id="commit-numeric-dash-config-message-allowed",
        ),
        pytest.param(
            "git log task/USKO-01-config",
            None,
            id="log-numeric-dash-config-branch-allowed",
        ),
        # ---------- git-config-global ----------
        pytest.param(
            "git config --global user.email foo",
            "git-config-global",
            id="config-global",
        ),
        pytest.param(
            "git config --system core.editor vim",
            "git-config-global",
            id="config-system",
        ),
        pytest.param(
            "git config user.email foo",
            None,
            id="config-local-allowed",
        ),
        # ---------- rm-rf-home-or-root ----------
        pytest.param("rm -rf ~", "rm-rf-home-or-root", id="rm-rf-tilde"),
        pytest.param("rm -rf $HOME", "rm-rf-home-or-root", id="rm-rf-home"),
        pytest.param("rm -rf /", "rm-rf-home-or-root", id="rm-rf-root"),
        pytest.param("rm -r ~", "rm-rf-home-or-root", id="rm-r-tilde"),
        pytest.param(
            "rm -rf ~/foo",
            None,
            id="rm-rf-tilde-subpath-allowed",
        ),
        pytest.param(
            "rm -rf node_modules",
            None,
            id="rm-rf-relative-allowed",
        ),
        pytest.param("rm -rf /tmp/x", None, id="rm-rf-tmp-allowed"),
        pytest.param("rm file.txt", None, id="rm-no-flag-allowed"),
        pytest.param(
            "cd / && rm -rf foo",
            None,
            id="rm-rf-relative-after-cd-allowed",
        ),
        # ---------- git-reset-hard ----------
        pytest.param("git reset --hard", "git-reset-hard", id="reset-hard"),
        pytest.param(
            "git reset --hard HEAD~3",
            "git-reset-hard",
            id="reset-hard-target",
        ),
        pytest.param("git reset HEAD~1", None, id="reset-mixed-allowed"),
        pytest.param(
            "git reset --soft HEAD~1",
            None,
            id="reset-soft-allowed",
        ),
        # ---------- git-clean-force ----------
        pytest.param("git clean -f", "git-clean-force", id="clean-f"),
        pytest.param("git clean -fdx", "git-clean-force", id="clean-fdx"),
        pytest.param(
            "git clean --force",
            "git-clean-force",
            id="clean-force-long",
        ),
        pytest.param("git clean -n", None, id="clean-dry-run-allowed"),
        pytest.param(
            "git clean -n some-file",
            None,
            id="clean-pathspec-with-dash-allowed",
        ),
        pytest.param(
            "git clean -n config-files",
            None,
            id="clean-pathspec-config-allowed",
        ),
        # Regression: pathspec starting with a numeric-prefixed token that
        # contains `-fix` must NOT trigger force-clean.
        pytest.param(
            "git clean -n task/USKO-032-fix-remaining-test-failures",
            None,
            id="clean-pathspec-numeric-dash-fix-allowed",
        ),
        pytest.param(
            "git clean -n release/2025-fast-hotfix",
            None,
            id="clean-pathspec-numeric-dash-fast-allowed",
        ),
        # ---------- git-checkout-discard ----------
        pytest.param(
            "git checkout -- file.py",
            "git-checkout-discard",
            id="checkout-dashes",
        ),
        pytest.param(
            "git checkout .",
            "git-checkout-discard",
            id="checkout-dot",
        ),
        pytest.param("git checkout main", None, id="checkout-branch-allowed"),
        pytest.param(
            "git checkout -b new-branch",
            None,
            id="checkout-create-allowed",
        ),
        # ---------- git-restore-discard ----------
        pytest.param(
            "git restore file.py",
            "git-restore-discard",
            id="restore-file",
        ),
        pytest.param("git restore .", "git-restore-discard", id="restore-dot"),
        pytest.param(
            "git restore --staged file.py",
            None,
            id="restore-staged-allowed",
        ),
        # ---------- git-commit-amend ----------
        pytest.param("git commit --amend", "git-commit-amend", id="commit-amend"),
        pytest.param(
            "git commit --amend --no-edit",
            "git-commit-amend",
            id="commit-amend-no-edit",
        ),
        # ---------- git-push-denied ----------
        pytest.param(
            'git -C "$W" push origin main',
            "git-push-denied",
            id="push-with-C-flag",
        ),
        pytest.param("cd /r && git push", "git-push-denied", id="push-chained"),
        pytest.param(
            "git send-pack origin main",
            "git-push-denied",
            id="push-send-pack",
        ),
        pytest.param(
            "git subtree push --prefix=d origin main",
            "git-push-denied",
            id="push-subtree",
        ),
        pytest.param(
            'setsid env -i sh -c "git push origin main"',
            "git-push-denied",
            id="push-detached-scrubbed",
        ),
        pytest.param(
            'git commit -m "add push support"',
            None,
            id="commit-message-mentioning-push-allowed",
        ),
        pytest.param("git fetch origin", None, id="fetch-allowed"),
        pytest.param("git pull --rebase", None, id="pull-allowed"),
        # ---------- protected-config-write ----------
        pytest.param(
            "rm ~/.claude/git-hooks/pre-push",
            "protected-config-write",
            id="protected-rm-hook",
        ),
        pytest.param(
            "cat > /home/kern/.claude/hooks/lint.py <<EOF\nx\nEOF",
            "protected-config-write",
            id="protected-heredoc-over-lint-hook",
        ),
        pytest.param(
            "mv /home/kern/.claude/hooks/lint.py /tmp/stash",
            "protected-config-write",
            id="protected-rename-lint-hook",
        ),
        pytest.param(
            "sudo chattr -i /home/kern/.claude/hooks/lint.py",
            "protected-config-write",
            id="protected-chattr-lint-hook",
        ),
        pytest.param(
            "echo {} | tee $HOME/.claude/settings.json",
            "protected-config-write",
            id="protected-tee-settings",
        ),
        pytest.param(
            "rm -rf /etc/claude-code",
            "protected-config-write",
            id="protected-rm-managed-policy",
        ),
        pytest.param(
            "cat /home/kern/.claude/hooks/lint.py",
            None,
            id="protected-read-allowed",
        ),
        pytest.param(
            "ls -la ~/.claude/git-hooks",
            None,
            id="protected-ls-allowed",
        ),
        pytest.param(
            "rm -rf /home/kern/.claude/commands",
            None,
            id="unprotected-claude-subdir-allowed",
        ),
    ],
)
def test_check_command(command: str, expected_rule: str | None) -> None:
    rule = check_command(command)
    if expected_rule is None:
        assert rule is None, f"expected allowed, got blocked by {rule.name if rule else None}"
    else:
        assert rule is not None, f"expected blocked by {expected_rule}, got allowed"
        assert rule.name == expected_rule, f"expected {expected_rule}, got {rule.name}"


# ---------- main() integration tests ----------


def test_main_blocks_forbidden_command(
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload(
        {"tool_name": "Bash", "tool_input": {"command": "git commit --no-verify"}},
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "git-no-verify" in capsys.readouterr().err


def test_main_allows_safe_command(stdin_payload: StdinSetter) -> None:
    stdin_payload({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    main()  # must not raise


def test_main_ignores_non_bash_tool(stdin_payload: StdinSetter) -> None:
    stdin_payload({"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x"}})
    main()


def test_main_handles_malformed_json(stdin_payload: StdinSetter) -> None:
    stdin_payload("not json")
    main()


def test_main_handles_missing_tool_input(stdin_payload: StdinSetter) -> None:
    stdin_payload({"tool_name": "Bash"})
    main()


def test_main_handles_missing_command_key(stdin_payload: StdinSetter) -> None:
    stdin_payload({"tool_name": "Bash", "tool_input": {}})
    main()


def test_main_handles_null_command(stdin_payload: StdinSetter) -> None:
    stdin_payload({"tool_name": "Bash", "tool_input": {"command": None}})
    main()


# ---------- check_path() ----------


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        pytest.param("/home/kern/.claude/hooks/lint.py", True, id="lint-hook-itself"),
        # The directory is not protected, and neither is the guard's own source:
        # for that one file the kernel flag the user applies is the layer that
        # holds, and a text rule guarding it cost a hand-off every review round
        # while adding nothing an accidental write would have hit.
        pytest.param("/home/kern/.claude/hooks", False, id="hooks-dir-writable"),
        pytest.param(
            "/home/kern/.claude/hooks/guard.py",
            False,
            id="guard-source-not-text-protected",
        ),
        pytest.param(
            "/home/kern/.claude/hooks/test_guard.py",
            False,
            id="hook-sibling-writable",
        ),
        # Executed by the harness on its own, so still protected.
        pytest.param(
            "/home/kern/.claude/hooks/lint.py",
            True,
            id="lint-hook-protected",
        ),
        pytest.param(
            "/home/kern/.claude/hooks/notify.sh",
            True,
            id="notify-hook-protected",
        ),
        pytest.param(
            "/home/kern/.claude/statusline-command.sh",
            True,
            id="statusline-protected",
        ),
        # Run only by pytest, so writable: the tests must stay maintainable.
        pytest.param(
            "/home/kern/.claude/hooks/conftest.py",
            False,
            id="conftest-writable",
        ),
        pytest.param("/home/kern/.claude/git-hooks/pre-push", True, id="pre-push-hook"),
        pytest.param("/home/kern/.claude/settings.json", True, id="settings-json"),
        pytest.param("/etc/claude-code/managed-settings.json", True, id="managed-policy"),
        pytest.param(
            "/home/kern/.claude/../.claude/git-hooks/pre-push",
            True,
            id="dot-dot-traversal-normalized",
        ),
        pytest.param("/home/kern/.claude/commands/foo.md", False, id="commands-allowed"),
        pytest.param("/home/kern/.claude/CLAUDE.md", False, id="claude-md-allowed"),
        pytest.param("/home/kern/proj/app.py", False, id="project-file-allowed"),
        pytest.param(
            "/home/kern/.claude-other/hooks/x",
            False,
            id="prefix-lookalike-not-protected",
        ),
    ],
)
def test_check_path(path: str, protected: bool) -> None:
    reason = check_path(path)
    if protected:
        assert reason is not None, f"expected {path} to be protected"
        assert path in reason
    else:
        assert reason is None, f"expected {path} to be writable, got: {reason}"


# ---------- main() dispatch for non-Bash tools ----------


@pytest.mark.parametrize(
    ("tool", "tool_input", "target", "location"),
    [
        pytest.param(
            "Write",
            {"file_path": "/home/kern/.claude/hooks/lint.py", "content": "x"},
            "/home/kern/.claude/hooks/lint.py",
            "/home/kern/.claude/hooks/lint.py",
            id="write-lint-hook",
        ),
        pytest.param(
            "Edit",
            {"file_path": "/home/kern/.claude/settings.json"},
            "/home/kern/.claude/settings.json",
            "/home/kern/.claude/settings.json",
            id="edit-settings",
        ),
        pytest.param(
            "MultiEdit",
            {"file_path": "/etc/claude-code/managed-settings.json"},
            "/etc/claude-code/managed-settings.json",
            "/etc/claude-code",
            id="multiedit-managed-policy",
        ),
        pytest.param(
            "NotebookEdit",
            {"notebook_path": "/home/kern/.claude/git-hooks/x.ipynb"},
            "/home/kern/.claude/git-hooks/x.ipynb",
            "/home/kern/.claude/git-hooks",
            id="notebook-git-hooks",
        ),
    ],
)
def test_main_blocks_write_to_protected_path(
    tool: str,
    tool_input: dict[str, object],
    target: str,
    location: str,
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The refusal has to name which path was refused and which location it hit.

    Asserting the tag alone let a change drop the target or the reason and
    still pass, leaving the user-visible explanation unpinned.
    """
    stdin_payload({"tool_name": tool, "tool_input": tool_input})
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("Blocked by guard rule 'protected-path-write': ")
    assert f"'{target}' names the protected location {location}." in err
    assert "Only the user may change it." in err


def test_protected_path_refusal_is_verbatim(
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One full-text anchor, so a reworded refusal cannot slip through the
    fragment assertions above.
    """
    stdin_payload(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/home/kern/.claude/settings.json"},
        },
    )
    with pytest.raises(SystemExit):
        main()
    assert capsys.readouterr().err == (
        "Blocked by guard rule 'protected-path-write': "
        "'/home/kern/.claude/settings.json' names the protected location "
        "/home/kern/.claude/settings.json. It is executed by the harness or by "
        "git without a tool call, or it decides whether the guard runs at all. "
        "Only the user may change it.\n"
    )


def test_main_allows_write_outside_protected_paths(
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload(
        {"tool_name": "Write", "tool_input": {"file_path": "/home/kern/proj/a.py"}},
    )
    main()  # must not raise
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("code", "expected_fragment"),
    [
        pytest.param(
            'import subprocess\nsubprocess.run(["git", "push"])',
            "git-push-denied",
            id="argv-split-push",
        ),
        pytest.param(
            'subprocess.run(["git", "-C", d, "push"])',
            "git-push-denied",
            id="argv-split-push-with-flag",
        ),
        pytest.param("repo.remote().push()", "git-push-denied", id="gitpython-push"),
        pytest.param(
            "open('/home/kern/.claude/hooks/lint.py', 'w').write('')",
            "protected-path-write",
            id="notebook-writes-lint-hook",
        ),
    ],
)
def test_main_blocks_code_tool(
    code: str,
    expected_fragment: str,
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload({"tool_name": "mcp__ide__executeCode", "tool_input": {"code": code}})
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("Blocked by guard rule ")
    assert expected_fragment in err
    # The tag alone is not the contract: the reason has to survive too.
    if expected_fragment == "protected-path-write":
        assert "executed code references the protected location" in err
    else:
        assert "Pushing is reserved for the user" in err


def test_code_tool_refusal_names_the_location(
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload(
        {
            "tool_name": "mcp__ide__executeCode",
            "tool_input": {"code": 'open("/home/kern/.claude/settings.json", "w")'},
        },
    )
    with pytest.raises(SystemExit):
        main()
    assert capsys.readouterr().err == (
        "Blocked by guard rule 'protected-path-write': executed code references "
        "the protected location /home/kern/.claude/settings.json.\n"
    )


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("print(1 + 1)", id="arithmetic"),
        pytest.param("df = df.append(row)", id="pandas-append"),
    ],
)
def test_main_allows_harmless_code_tool(
    code: str,
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload({"tool_name": "mcp__ide__executeCode", "tool_input": {"code": code}})
    main()  # must not raise
    assert capsys.readouterr().err == ""


# The hook is registered for matcher "*", so a raise on an odd payload would
# break every tool call in the session, not only the guarded ones.
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="top-level-list"),
        pytest.param("null", id="top-level-null"),
        pytest.param({"tool_name": 123, "tool_input": {}}, id="non-string-tool-name"),
        pytest.param({"tool_name": "Bash", "tool_input": [1, 2]}, id="list-tool-input"),
        pytest.param(
            {"tool_name": "Write", "tool_input": {"file_path": 123}},
            id="non-string-path",
        ),
        pytest.param(
            {"tool_name": "mcp__ide__executeCode", "tool_input": {"code": None}},
            id="null-code",
        ),
        pytest.param(
            {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}},
            id="read-tool",
        ),
    ],
)
def test_main_survives_unexpected_payload(
    payload: object,
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload(payload)
    main()  # must not raise
    assert capsys.readouterr().err == ""


# ---------- block() ----------


def test_block_exits_2_and_prints_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """Exit code 2 is the contract: it is what makes Claude Code show stderr
    to the model. Any other code turns a refusal into a silent no-op.
    """
    with pytest.raises(SystemExit) as exc:
        block("Blocked by guard rule 'demo': reason text")
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "Blocked by guard rule 'demo': reason text"
    assert captured.out == ""


# ---------- C6: a relative file_path lands in the payload's cwd ----------

PROTECTED_DIR = "/home/kern/.claude"


@pytest.mark.parametrize(
    ("path", "cwd", "protected"),
    [
        pytest.param("hooks/lint.py", PROTECTED_DIR, True, id="relative-into-hooks"),
        pytest.param(
            "git-hooks/pre-push",
            PROTECTED_DIR,
            True,
            id="relative-into-git-hooks",
        ),
        pytest.param("settings.json", PROTECTED_DIR, True, id="relative-settings"),
        pytest.param(
            "../.claude/hooks/lint.py",
            "/home/kern/proj",
            True,
            id="relative-dot-dot-into-hooks",
        ),
        pytest.param("app.py", "/home/kern/proj", False, id="relative-elsewhere-ok"),
        pytest.param(
            "hooks/lint.py",
            "/home/kern/other-project",
            False,
            id="same-name-different-project-ok",
        ),
    ],
)
def test_check_path_resolves_against_payload_cwd(
    path: str,
    cwd: str,
    protected: bool,
) -> None:
    """A relative path lands in the payload's cwd, not the hook process's.

    Resolving against the hook's own directory judged the wrong file: the check
    came back clean while the write went into the guard's own directory.
    """
    reason = check_path(path, cwd)
    if protected:
        assert reason is not None, f"expected {path!r} under {cwd} to be protected"
        assert path in reason
    else:
        assert reason is None, f"expected {path!r} under {cwd} writable, got: {reason}"


def test_main_blocks_relative_path_into_protected_cwd(
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload(
        {
            "tool_name": "Write",
            "cwd": PROTECTED_DIR,
            "tool_input": {"file_path": "hooks/lint.py", "content": "x"},
        },
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "protected-path-write" in capsys.readouterr().err


def test_main_allows_relative_path_outside_protected_cwd(
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload(
        {
            "tool_name": "Write",
            "cwd": "/home/kern/proj",
            "tool_input": {"file_path": "src/app.py", "content": "x"},
        },
    )
    main()  # must not raise
    assert capsys.readouterr().err == ""


# ---------- C4: argv-list spelling of the non-push rules ----------


@pytest.mark.parametrize(
    ("code", "expected_rule"),
    [
        pytest.param(
            'subprocess.run(["git", "reset", "--hard"])',
            "git-reset-hard",
            id="argv-reset-hard",
        ),
        pytest.param(
            'subprocess.run(["git", "commit", "--amend", "--no-edit"])',
            "git-commit-amend",
            id="argv-commit-amend",
        ),
        pytest.param(
            'subprocess.run(["git", "clean", "-fd"])',
            "git-clean-force",
            id="argv-clean-force",
        ),
        pytest.param(
            'subprocess.run(["git", "commit", "--no-verify", "-m", "x"])',
            "git-no-verify",
            id="argv-no-verify",
        ),
        pytest.param(
            'subprocess.run(["git", "branch", "-D", "feature"])',
            "git-branch-force-delete",
            id="argv-branch-force-delete",
        ),
    ],
)
def test_main_blocks_argv_spelling_of_command_rules(
    code: str,
    expected_rule: str,
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """List syntax splits a command across quotes, so the shell rules -- which
    match contiguous text -- looked right past it and let it through.
    """
    stdin_payload({"tool_name": "mcp__ide__executeCode", "tool_input": {"code": code}})
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert expected_rule in capsys.readouterr().err


# ---------- C5: a protected path assembled from pieces ----------


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(
            'p = Path.home() / ".claude" / "hooks" / "lint.py"',
            id="pathlib-joined",
        ),
        pytest.param(
            'os.path.join(home, ".claude", "git-hooks", "pre-push")',
            id="os-path-join",
        ),
        pytest.param(
            'open(home + "/.claude/settings.json", "w")',
            id="concatenated-settings",
        ),
        pytest.param(
            'shutil.copy(src, "/etc/claude-code/managed-settings.json")',
            id="managed-policy-literal",
        ),
    ],
)
def test_main_blocks_constructed_protected_path(
    code: str,
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload({"tool_name": "mcp__ide__executeCode", "tool_input": {"code": code}})
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "protected-path-write" in capsys.readouterr().err


# ---------- normalize_code() ----------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param(
            '["git", "reset", "--hard"]',
            '["git reset --hard"]',
            id="argv-join-becomes-space",
        ),
        pytest.param(
            '"a" / "b" / "c"',
            '"a/b/c"',
            id="path-join-collapsed",
        ),
        pytest.param(
            "df = df.append(row)",
            "df = df.append(row)",
            id="ordinary-code-untouched",
        ),
    ],
)
def test_normalize_code(code: str, expected: str) -> None:
    assert normalize_code(code) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param(
            'os.path.join(home, ".claude", "hooks")',
            'os.path.join(home, ".claude/hooks")',
            id="argv-join-becomes-separator",
        ),
        pytest.param(
            'Path.home() / ".claude" / "hooks"',
            'Path.home() / ".claude/hooks"',
            id="slash-join-collapsed",
        ),
    ],
)
def test_normalize_paths(code: str, expected: str) -> None:
    """The same quote-comma join reads as a separator here and as a space in
    normalize_code; one normalization cannot serve both, so both run.
    """
    assert normalize_paths(code) == expected


def test_the_two_normalizations_disagree_on_the_comma_join() -> None:
    """Pins why both exist: drop either one and its spelling stops being seen."""
    argv = '["git", "reset"]'
    assert normalize_code(argv) == '["git reset"]'
    assert normalize_paths(argv) == '["git/reset"]'


@pytest.mark.parametrize("fn", [normalize_code, normalize_paths])
def test_normalization_leaves_division_between_names_alone(
    fn: Callable[[str], str],
) -> None:
    """Only quote-delimited joins collapse; arithmetic must survive intact."""
    assert fn("total / count") == "total / count"


# ---------- C3: symlink resolution ----------


def test_check_path_follows_a_symlink_into_the_protected_tree(
    tmp_path: Path,
) -> None:
    """check_path promises realpath(); nothing pinned it.

    A link planted anywhere resolves onto the guard, so dropping realpath()
    would have kept every other test green while opening a protected write.
    """
    link = tmp_path / "innocent.py"
    link.symlink_to("/home/kern/.claude/hooks/lint.py")

    reason = check_path(str(link))
    assert reason is not None, "a symlink onto the guard must be refused"
    assert "/home/kern/.claude/hooks/lint.py" in reason


def test_check_path_allows_a_symlink_that_leaves_the_protected_tree(
    tmp_path: Path,
) -> None:
    """The mirror case, so the test above cannot pass by blocking every link."""
    target = tmp_path / "real.py"
    target.write_text("x")
    link = tmp_path / "link.py"
    link.symlink_to(target)

    assert check_path(str(link)) is None


def test_check_path_resolves_dot_dot_out_of_the_protected_tree(
    tmp_path: Path,
) -> None:
    """`..` must be able to leave, not only to enter."""
    assert check_path("/home/kern/.claude/hooks/../../proj/app.py") is None


# ---------- C4: the missing-cwd fallback ----------


def test_check_path_without_cwd_falls_back_to_the_process_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed payload carries no cwd. The fallback is best-effort, and
    best-effort still has to catch the case that matters.
    """
    monkeypatch.chdir("/home/kern/.claude")
    reason = check_path("hooks/lint.py", None)
    assert reason is not None, "relative path onto the guard must be refused"
    assert "hooks/lint.py" in reason


def test_check_path_without_cwd_allows_an_unrelated_relative_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(str(tmp_path))
    assert check_path("src/app.py", None) is None


def test_main_without_cwd_still_refuses_a_relative_path_onto_the_guard(
    monkeypatch: pytest.MonkeyPatch,
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: no cwd key in the payload at all."""
    monkeypatch.chdir("/home/kern/.claude")
    stdin_payload(
        {"tool_name": "Write", "tool_input": {"file_path": "hooks/lint.py"}},
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "protected-path-write" in capsys.readouterr().err


def test_main_with_non_string_cwd_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cwd of the wrong type must degrade to the fallback, not to allowed."""
    monkeypatch.chdir("/home/kern/.claude")
    stdin_payload(
        {
            "tool_name": "Write",
            "cwd": 123,
            "tool_input": {"file_path": "hooks/lint.py"},
        },
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "protected-path-write" in capsys.readouterr().err


# ---------- C5: every option spelling between `git` and the verb ----------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "git -C/tmp -c core.hooksPath=/tmp/empty-hooks push",
            id="attached-short-option-plus-hooks-override",
        ),
        pytest.param("git -C/tmp push", id="attached-short-option"),
        pytest.param("git -C/tmp -q push origin main", id="attached-then-bare-flag"),
        pytest.param(
            "git -c core.hooksPath=/tmp/empty push",
            id="detached-config-override",
        ),
        pytest.param("git -C /tmp push", id="spaced-short-option"),
        pytest.param("git --git-dir=/tmp/x/.git push", id="attached-long-option"),
        pytest.param("git -q -v push", id="bare-flags-only"),
    ],
)
def test_every_option_spelling_still_reaches_the_verb(command: str) -> None:
    """The attached form `-C/tmp` carries its value with no separator.

    The old pattern required a space or `=`, so the whole rule failed to match
    and the command was allowed -- while the `-c core.hooksPath=` half of that
    same line also suppressed the pre-push hook. Both layers fell at once.
    """
    rule = check_command(command)
    assert rule is not None, f"{command!r} must not be able to reach a remote"
    assert rule.name in {"git-push-denied", "git-force-push"}


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('git commit -m "add push support"', id="verb-only-in-message"),
        pytest.param("git -C /tmp status", id="option-then-harmless-verb"),
        pytest.param("git -c user.name=x log --oneline", id="config-then-log"),
    ],
)
def test_broadened_option_pattern_keeps_ordinary_git_allowed(command: str) -> None:
    """The looser option token must not swallow a non-publishing subcommand."""
    assert check_command(command) is None


# ---------- C6: a bare filename in the directory that gives it meaning ----------


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            "cd ~/.claude && printf '{}' > settings.json",
            "/home/kern/proj",
            id="cd-tilde-then-write",
        ),
        pytest.param(
            "cd /home/kern/.claude && printf '{}' > settings.json",
            "/home/kern/proj",
            id="cd-absolute-then-write",
        ),
        pytest.param(
            "printf '{}' > settings.json",
            "/home/kern/.claude",
            id="already-standing-there",
        ),
        pytest.param("rm settings.json", "/home/kern/.claude", id="bare-remove"),
        pytest.param(
            "cd hooks && python3 -c \"open('lint.py','w')\"",
            "/home/kern/.claude",
            id="cd-into-hooks-then-python-write",
        ),
        pytest.param(
            "rm pre-push",
            "/home/kern/.claude/git-hooks",
            id="inside-a-protected-directory",
        ),
        pytest.param(
            "cd ../.claude && rm settings.json",
            "/home/kern/proj",
            id="relative-cd-back-into-claude",
        ),
    ],
)
def test_bare_name_write_is_refused_in_the_directory_that_defines_it(
    command: str,
    cwd: str,
) -> None:
    """Rules match one shell segment at a time, so `cd <dir> && <write>` hid
    the directory from the write and neither half looked protected alone.
    """
    reason = check_relative_target(command, cwd)
    assert reason is not None, f"{command!r} from {cwd} must be refused"


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            # A real directory: a `cd` that fails leaves the shell where it
            # was, so the tracker only follows one that could succeed.
            "cd /tmp && echo x > settings.json",
            "/home/kern/.claude",
            id="cd-away-releases-the-old-directory",
        ),
        pytest.param(
            "echo x > settings.json",
            "/home/kern/proj",
            id="another-project-may-have-that-name",
        ),
        pytest.param(
            "python3 -c \"import json; print(json.load(open('settings.json')))\"",
            "/home/kern/.claude",
            id="python-reading-is-not-writing",
        ),
        pytest.param(
            "cat settings.json 2>&1 | head -5",
            "/home/kern/.claude",
            id="stderr-redirect-is-not-a-write",
        ),
        pytest.param("git status --short", "/home/kern/.claude", id="no-write-verb"),
        pytest.param(
            "printf '{}' > commands/foo.json",
            "/home/kern/.claude",
            id="unprotected-sibling-directory",
        ),
    ],
)
def test_bare_name_check_leaves_ordinary_work_alone(command: str, cwd: str) -> None:
    """Scoped to the directory on purpose: plenty of projects have a
    settings.json, and a `cd` elsewhere has to genuinely move away.
    """
    assert check_relative_target(command, cwd) is None


def test_main_refuses_a_bare_name_write_end_to_end(
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload(
        {
            "tool_name": "Bash",
            "cwd": "/home/kern/proj",
            "tool_input": {"command": "cd ~/.claude && printf '{}' > settings.json"},
        },
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "protected-config-write" in err
    assert "/home/kern/.claude/settings.json" in err


# ---------- precision of the library publish spelling ----------


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("repo.remote().push()", id="gitpython-remote"),
        pytest.param("origin.push(refspec)", id="named-remote"),
        pytest.param("repository.push()", id="repository-object"),
    ],
)
def test_library_publish_spellings_are_refused(
    code: str,
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload({"tool_name": "mcp__ide__executeCode", "tool_input": {"code": code}})
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "git-push-denied" in capsys.readouterr().err


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("stack.push(item)", id="a-stack-is-not-a-remote"),
        pytest.param("queue.push(job)", id="a-queue-is-not-a-remote"),
        pytest.param('print("nothing to see here")', id="plain-print"),
    ],
)
def test_ordinary_push_methods_are_left_alone(
    code: str,
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refusing every method named push made the rule untrustworthy in
    ordinary notebook code, which is how a guard stops being read at all.
    """
    stdin_payload({"tool_name": "mcp__ide__executeCode", "tool_input": {"code": code}})
    main()  # must not raise
    assert capsys.readouterr().err == ""


# ---------- an operand is judged by where it lands, not by its name ----------


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            "printf x > .claude/hooks/lint.py",
            "/home/kern",
            id="parent-relative-onto-a-hook",
        ),
        pytest.param(
            "rm .claude/git-hooks/pre-push",
            "/home/kern",
            id="parent-relative-remove-a-git-hook",
        ),
        pytest.param(
            "cp /tmp/evil .claude/settings.json",
            "/home/kern",
            id="parent-relative-overwrite-registration",
        ),
        pytest.param(
            "rm -rf .claude/git-hooks",
            "/home/kern",
            id="parent-relative-remove-the-directory",
        ),
        pytest.param(
            "printf x > ../.claude/hooks/lint.py",
            "/home/kern/proj",
            id="sibling-relative-onto-a-hook",
        ),
        pytest.param(
            "python3 -c \"open('.claude/hooks/lint.py','w')\"",
            "/home/kern",
            id="parent-relative-inside-a-python-one-liner",
        ),
        pytest.param(
            'cp /tmp/evil "/home/kern/.claude/settings.json"',
            "/home/kern",
            id="quoted-absolute-operand",
        ),
    ],
)
def test_operand_is_judged_by_where_it_resolves(command: str, cwd: str) -> None:
    """Matching bare names only fired once the shell already stood in the
    file's own directory, so a parent-relative operand walked straight past.

    Resolution answers the question the name only gestured at.
    """
    reason = check_relative_target(command, cwd)
    assert reason is not None, f"{command!r} from {cwd} must be refused"


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            "rm -rf .claude/commands",
            "/home/kern",
            id="unprotected-subdirectory",
        ),
        pytest.param(
            "printf x > .claude/CLAUDE.md",
            "/home/kern",
            id="unprotected-file-in-claude",
        ),
        pytest.param(
            "printf x > .claude-other/hooks/lint.py",
            "/home/kern",
            id="lookalike-directory",
        ),
        pytest.param("rm -rf node_modules", "/home/kern/proj", id="ordinary-cleanup"),
        pytest.param("mv old.py new.py", "/home/kern/proj", id="ordinary-rename"),
    ],
)
def test_resolution_does_not_over_reach(command: str, cwd: str) -> None:
    assert check_relative_target(command, cwd) is None


# ---------- a mention is not an operand ----------


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            'echo "regenerating settings.json" > /tmp/log',
            "/home/kern/.claude",
            id="protected-name-inside-prose",
        ),
        pytest.param(
            "echo 'about to rewrite guard.py' > /tmp/log",
            "/home/kern/.claude/hooks",
            id="protected-name-inside-single-quoted-prose",
        ),
    ],
)
def test_a_name_inside_a_prose_string_is_not_a_target(command: str, cwd: str) -> None:
    """Refusing a command that merely names the file teaches the reader to
    route around the guard, which costs more than the case is worth.
    """
    assert check_relative_target(command, cwd) is None


def test_a_quoted_operand_without_spaces_is_still_a_target() -> None:
    """The prose rule keys on whitespace, so a quoted path must survive it."""
    reason = check_relative_target('printf x > ".claude/settings.json"', "/home/kern")
    assert reason is not None


def test_prose_masking_still_recovers_a_nested_quote() -> None:
    """The outer span is prose, the inner one is the real target."""
    reason = check_relative_target(
        "python3 -c \"import io; open('settings.json', 'w')\"",
        "/home/kern/.claude",
    )
    assert reason is not None


def test_main_refuses_a_parent_relative_operand_end_to_end(
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload(
        {
            "tool_name": "Bash",
            "cwd": "/home/kern",
            "tool_input": {"command": "printf x > .claude/hooks/lint.py"},
        },
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "protected-config-write" in err
    assert "/home/kern/.claude/hooks/lint.py" in err


# ---------- separators inside quotes are not separators ----------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("a && b", ["a", "b"], id="and"),
        pytest.param("a || b", ["a", "b"], id="or"),
        pytest.param("a ; b", ["a", "b"], id="semicolon"),
        pytest.param("a | b", ["a", "b"], id="pipe"),
        pytest.param("a && b ; c | d", ["a", "b", "c", "d"], id="mixed"),
        pytest.param(
            'python3 -c "import io; print(1)"',
            ['python3 -c "import io; print(1)"'],
            id="semicolon-inside-double-quotes-is-text",
        ),
        pytest.param(
            "echo 'a && b'",
            ["echo 'a && b'"],
            id="and-inside-single-quotes-is-text",
        ),
        pytest.param(
            "echo 'x | y' | wc -l",
            ["echo 'x | y'", "wc -l"],
            id="quoted-pipe-kept-real-pipe-split",
        ),
        pytest.param("   ", [], id="whitespace-only"),
        pytest.param("a ;; b", ["a", "b"], id="empty-segment-dropped"),
    ],
)
def test_split_segments_respects_quotes(command: str, expected: list[str]) -> None:
    """A regex split cut inside quoted text, so a semicolon in a python
    one-liner separated the interpreter from its target and each half looked
    harmless. Every rule matches per segment, so every rule missed it.
    """
    assert _split_segments(command) == expected


def test_a_quoted_semicolon_no_longer_hides_a_protected_write() -> None:
    """The regression the splitter exists for, end to end through the check."""
    command = "python3 -c \"import os; os.rename('settings.json', '/tmp/x')\""
    assert check_relative_target(command, "/home/kern/.claude") is not None


def test_a_quoted_semicolon_does_not_invent_a_target() -> None:
    """Keeping the command whole must not make an innocent one look guilty."""
    command = 'python3 -c "import os; print(os.getcwd())"'
    assert check_relative_target(command, "/home/kern/.claude") is None


# ---------- a `cd` is a directory change only when the segment is one ----------


HOME = "/home/kern"
PREVIOUS = "/home/kern/previous"


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        pytest.param("cd hooks", "hooks", id="plain"),
        pytest.param("  cd   hooks  ", "hooks", id="surrounding-whitespace"),
        pytest.param("cd", HOME, id="bare-cd-goes-home"),
        pytest.param("cd -", PREVIOUS, id="dash-returns"),
        pytest.param("cd -- /home/kern/.claude", "/home/kern/.claude", id="double-dash-skipped"),
        pytest.param("cd -P /tmp", "/tmp", id="option-skipped"),
        pytest.param("cd -L -- /tmp", "/tmp", id="option-then-double-dash"),
        pytest.param('cd "/tmp/with space"', "/tmp/with space", id="quoted-destination"),
        pytest.param("cd -P", HOME, id="only-options-goes-home"),
        # Not directory changes at all.
        pytest.param("printf 'cd hooks' > settings.json", None, id="cd-inside-quoted-data"),
        pytest.param("echo cd hooks", None, id="cd-as-an-argument"),
        pytest.param("rm settings.json", None, id="no-cd"),
        pytest.param("cdx /tmp", None, id="different-command"),
    ],
)
def test_cd_destination(segment: str, expected: str | None) -> None:
    """Searching for `cd` anywhere in the segment let quoted data pose as a
    directory change, and the tracker then skipped the write on that same line.
    """
    assert _cd_destination(segment, HOME, PREVIOUS) == expected


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            "printf 'cd hooks' > settings.json",
            "/home/kern/.claude",
            id="quoted-cd-does-not-excuse-the-write",
        ),
        pytest.param(
            "cd -- /home/kern/.claude && printf x > settings.json",
            "/home/kern",
            id="double-dash-destination",
        ),
        pytest.param(
            "cd /tmp && cd - && printf x > settings.json",
            "/home/kern/.claude",
            id="dash-returns-to-a-protected-directory",
        ),
        pytest.param(
            "cd && printf x > .claude/settings.json",
            "/tmp",
            id="bare-cd-lands-home",
        ),
        pytest.param(
            "cd -P /home/kern/.claude && rm hooks/lint.py",
            "/tmp",
            id="option-before-the-destination",
        ),
    ],
)
def test_cd_tracking_cannot_be_talked_past(command: str, cwd: str) -> None:
    assert check_relative_target(command, cwd) is not None


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            "printf 'cd hooks' > /tmp/note.txt",
            "/home/kern/.claude",
            id="same-quoted-data-safe-target",
        ),
        pytest.param(
            "cd /tmp && cd - && ls",
            "/home/kern/.claude",
            id="dash-return-without-a-write",
        ),
        pytest.param(
            "cd review && python3 -m pytest -q",
            "/home/kern/.claude",
            id="cd-then-ordinary-command",
        ),
        pytest.param(
            "cd /tmp && echo x > settings.json",
            "/home/kern/.claude",
            id="cd-away-then-write",
        ),
    ],
)
def test_cd_tracking_leaves_ordinary_work_alone(command: str, cwd: str) -> None:
    assert check_relative_target(command, cwd) is None


# ---------- switching the enforcement off is itself refused ----------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "git -c core.hooksPath=/tmp/empty status",
            id="disabling-the-hook-on-a-harmless-verb",
        ),
        pytest.param(
            'g=x; git -c core.hooksPath=/tmp/empty "$g" origin main',
            id="verb-hidden-behind-a-variable",
        ),
        pytest.param(
            "git -c core.hooksPath=/dev/null commit -m x",
            id="disabling-it-for-a-commit",
        ),
        pytest.param(
            "git --git-dir=/tmp/x -c core.hooksPath=/tmp/e fetch",
            id="after-another-option",
        ),
    ],
)
def test_relocating_the_hook_path_is_refused(command: str) -> None:
    """A verb can hide behind a variable, so no text rule can be sure of it.
    The option that makes hiding it worthwhile cannot hide, so that is what
    gets refused -- there is no legitimate reason to move the hook path here.
    """
    rule = check_command(command)
    assert rule is not None, f"{command!r} switches off the pre-push hook"
    assert rule.name == "git-hooks-path-override"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git -c user.name=x log --oneline", id="unrelated-config"),
        pytest.param("git -c commit.gpgsign=true commit -m x", id="another-unrelated"),
        pytest.param("git status --short", id="no-config-at-all"),
        pytest.param(
            'grep -rn "core.hooksPath" review/',
            id="searching-for-the-name-is-not-setting-it",
        ),
    ],
)
def test_other_git_config_is_left_alone(command: str) -> None:
    rule = check_command(command)
    assert rule is None or rule.name != "git-hooks-path-override"


# ---------- a redirect writes its target, not the whole line ----------


CLAUDE_DIR = "/home/kern/.claude"


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            "diff -q hooks/lint.py /tmp/x >/dev/null",
            CLAUDE_DIR,
            id="diff-a-protected-file-writing-dev-null",
        ),
        pytest.param(
            "cat hooks/lint.py > /tmp/copy.txt",
            CLAUDE_DIR,
            id="read-protected-write-elsewhere",
        ),
        pytest.param(
            "grep -n push hooks/lint.py >> /tmp/hits",
            CLAUDE_DIR,
            id="append-findings-elsewhere",
        ),
        pytest.param(
            "wc -l hooks/lint.py git-hooks/pre-push > /tmp/counts",
            CLAUDE_DIR,
            id="two-protected-reads-one-safe-target",
        ),
        pytest.param(
            "python3 -m pytest hooks/ -q > /tmp/out",
            CLAUDE_DIR,
            id="test-run-with-output-redirected",
        ),
    ],
)
def test_a_redirect_does_not_make_every_operand_a_target(
    command: str,
    cwd: str,
) -> None:
    """`>` marked the whole segment as a write and then every token on it was
    resolved as a possible destination, so reading a protected file while
    redirecting output elsewhere was refused. It refused a real command of the
    session that introduced it, within seconds of being installed.
    """
    assert check_relative_target(command, cwd) is None


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param("printf x > hooks/lint.py", CLAUDE_DIR, id="redirect-onto-a-hook"),
        pytest.param("printf x >> settings.json", CLAUDE_DIR, id="append-onto-registration"),
        pytest.param("echo x >hooks/lint.py", CLAUDE_DIR, id="no-space-after-redirect"),
        pytest.param(
            "cat /tmp/evil > .claude/hooks/lint.py",
            "/home/kern",
            id="parent-relative-redirect",
        ),
    ],
)
def test_a_protected_redirect_target_is_still_refused(command: str, cwd: str) -> None:
    assert check_relative_target(command, cwd) is not None


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param("cp /tmp/evil hooks/lint.py", CLAUDE_DIR, id="cp-onto-a-hook"),
        pytest.param("mv hooks/lint.py /tmp/stash", CLAUDE_DIR, id="moving-a-hook-away"),
        pytest.param("rm settings.json", CLAUDE_DIR, id="removing-the-registration"),
        pytest.param(
            "cd hooks && python3 -c \"open('lint.py','w')\"",
            CLAUDE_DIR,
            id="python-write",
        ),
    ],
)
def test_a_write_verb_still_claims_all_of_its_operands(command: str, cwd: str) -> None:
    """Which operand is the destination differs per verb -- `cp a b`, `dd of=`,
    `install -m 644 a b` -- and deciding that is a shell parser's job. So a
    write verb keeps claiming everything it names, unlike a redirect.
    """
    assert check_relative_target(command, cwd) is not None


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        pytest.param("echo x > out.txt", {"out.txt"}, id="single-redirect"),
        pytest.param("echo x >> out.txt", {"out.txt"}, id="append-redirect"),
        pytest.param("echo x >out.txt", {"out.txt"}, id="no-space"),
        pytest.param("cat a.py 2>&1", set(), id="stderr-redirect-claims-nothing"),
        pytest.param("cat a.py >&2", set(), id="fd-redirect-claims-nothing"),
        pytest.param(
            "diff a.py b.py > /dev/null",
            {"/dev/null"},
            id="only-the-target-not-the-inputs",
        ),
    ],
)
def test_write_targets_of_a_redirect_only_segment(
    segment: str,
    expected: set[str],
) -> None:
    assert _write_targets(segment) == expected


def test_write_targets_of_a_verb_segment_include_its_operands() -> None:
    targets = _write_targets("cp /tmp/evil hooks/lint.py")
    assert "hooks/lint.py" in targets
    assert "/tmp/evil" in targets


# ---------- what the chunked review found ----------


HOME_DIR = "/home/kern"
CLAUDE_HOME = "/home/kern/.claude"
PROJECT = "/home/kern/proj"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "git -c core.hookspath=/tmp/empty status",
            id="all-lowercase-key",
        ),
        pytest.param(
            "git -c CORE.HOOKSPATH=/tmp/empty status",
            id="upper-case-key",
        ),
        pytest.param(
            "git -c Core.HooksPath=/tmp/empty status",
            id="mixed-case-key",
        ),
    ],
)
def test_the_hook_path_rule_is_case_insensitive(command: str) -> None:
    """Git config keys are case-insensitive, so an exact-case matcher left
    `core.hookspath` free to switch the pre-push hook off and publish.
    """
    rule = check_command(command)
    assert rule is not None, f"{command!r} switches off the hook"
    assert rule.name == "git-hooks-path-override"


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param("printf x 1>settings.json", CLAUDE_HOME, id="fd-one-redirect"),
        pytest.param("printf x 2>settings.json", CLAUDE_HOME, id="fd-two-redirect"),
        pytest.param("printf x 1>>settings.json", CLAUDE_HOME, id="fd-one-append"),
    ],
)
def test_a_numbered_redirect_still_writes_its_file(command: str, cwd: str) -> None:
    """`>&` duplicates a descriptor and writes nothing, which is the case worth
    excluding. A leading digit does not make a redirect harmless -- `1>file`
    writes the file like any other, and excluding it opened a way in.
    """
    assert check_relative_target(command, cwd) is not None


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param("cat a.py 2>&1 | head", CLAUDE_HOME, id="stderr-to-stdout"),
        pytest.param("cat a.py >&2", CLAUDE_HOME, id="stdout-to-stderr"),
    ],
)
def test_descriptor_duplication_is_still_not_a_write(command: str, cwd: str) -> None:
    assert check_relative_target(command, cwd) is None


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            'cd "$HOME/.claude" && printf x > settings.json',
            PROJECT,
            id="home-variable",
        ),
        pytest.param(
            "cd ${HOME}/.claude && rm settings.json",
            PROJECT,
            id="braced-home-variable",
        ),
    ],
)
def test_the_home_variable_is_expanded_like_the_tilde(command: str, cwd: str) -> None:
    """expanduser knows `~` and nothing else, so `$HOME/.claude` resolved to a
    literal directory of that name under wherever the shell already stood.
    """
    assert check_relative_target(command, cwd) is not None


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            'echo "$(cd ~/.claude && printf x > settings.json)"',
            PROJECT,
            id="dollar-paren",
        ),
        pytest.param(
            "echo `cd ~/.claude && rm settings.json`",
            PROJECT,
            id="backticks",
        ),
        pytest.param(
            'echo "$(echo "$(cd ~/.claude && rm settings.json)")"',
            PROJECT,
            id="nested",
        ),
        pytest.param(
            'result="$(printf x > .claude/settings.json)"',
            HOME_DIR,
            id="assigned-substitution",
        ),
    ],
)
def test_a_command_substitution_is_read_as_a_command(command: str, cwd: str) -> None:
    """The body of a substitution runs, so the write inside it happens while the
    outer line reads as an echo. A flat scan of the outer text finds neither the
    directory change nor the redirect.
    """
    assert check_relative_target(command, cwd) is not None


def test_substitution_recursion_does_not_invent_a_target() -> None:
    assert check_relative_target('echo "$(ls -la)"', CLAUDE_HOME) is None


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        pytest.param('echo "$(a; b)"', ["a; b"], id="one-body"),
        pytest.param("echo `a; b`", ["a; b"], id="backtick-body"),
        pytest.param('x "$(a)" "$(b)"', ["a", "b"], id="two-bodies"),
        pytest.param('"$(echo "$(inner)")"', ['echo "$(inner)"'], id="outer-keeps-inner"),
        pytest.param("echo plain", [], id="none"),
        pytest.param("echo $(unclosed", ["unclosed"], id="unterminated-still-read"),
    ],
)
def test_substitutions(segment: str, expected: list[str]) -> None:
    assert _substitutions(segment) == expected


def test_an_escaped_quote_is_not_a_delimiter() -> None:
    """Flipping the quote state on `\\"` split a command at a semicolon that was
    only ever string content, leaving one half with no verb and the other with
    no target.
    """
    command = "python3 -c \"x=\\\"a;b\\\"; open('settings.json', 'w').write('')\""
    assert check_relative_target(command, CLAUDE_HOME) is not None


def test_a_backslash_inside_single_quotes_is_literal() -> None:
    """The shell gives a backslash no meaning there, so it must not eat the
    closing quote and swallow the rest of the line.
    """
    assert _split_segments("echo 'a\\' ; ls") == ["echo 'a\\'", "ls"]


@pytest.mark.parametrize(
    ("path", "cwd"),
    [
        pytest.param(".claude/settings.json", PROJECT, id="project-own-settings"),
        pytest.param(".claude/hooks/lint.py", PROJECT, id="project-own-hook-name"),
        pytest.param("/home/kern/other/.claude/settings.json", None, id="absolute-elsewhere"),
    ],
)
def test_another_projects_dot_claude_is_not_this_one(path: str, cwd: str | None) -> None:
    """Resolution is the whole answer. A fragment test ran as well, left over
    from when relative paths were judged by name, and it refused a project's own
    `.claude/settings.json` -- a file that resolves nowhere near this one.
    """
    assert check_path(path, cwd) is None


def test_main_allows_a_project_writing_its_own_claude_settings(
    stdin_payload: StdinSetter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_payload(
        {
            "tool_name": "Write",
            "cwd": PROJECT,
            "tool_input": {"file_path": ".claude/settings.json", "content": "{}"},
        },
    )
    main()  # must not raise
    assert capsys.readouterr().err == ""


# ---------- a variable assigned on the same line ----------


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            'target=settings.json; cd ~/.claude && printf x > "$target"',
            PROJECT,
            id="variable-redirect-target",
        ),
        pytest.param(
            'f=settings.json; cd ~/.claude && rm "${f}"',
            PROJECT,
            id="braced-variable",
        ),
        pytest.param(
            "d=.claude/hooks; printf x > $d/lint.py",
            HOME_DIR,
            id="variable-directory-part",
        ),
    ],
)
def test_a_variable_assigned_on_the_line_is_substituted(command: str, cwd: str) -> None:
    """The shell expands it before the write happens, so a literal check of
    `$target` resolves nowhere and the protected file was reached.
    """
    assert check_relative_target(command, cwd) is not None


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            'name=report.txt; echo hi > "$name"',
            PROJECT,
            id="a-variable-target-elsewhere",
        ),
        pytest.param(
            'msg="writing settings.json"; echo "$msg" > /tmp/log',
            CLAUDE_HOME,
            id="the-name-only-inside-a-message",
        ),
        pytest.param(
            "out=/tmp/x; python3 -m pytest review/ -q > $out",
            CLAUDE_HOME,
            id="test-output-to-a-variable",
        ),
    ],
)
def test_substitution_does_not_invent_a_protected_target(command: str, cwd: str) -> None:
    assert check_relative_target(command, cwd) is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("a=1; b=2", {"a": "1", "b": "2"}, id="two-plain"),
        pytest.param('a="x y"', {"a": "x y"}, id="a-quoted-value-keeps-its-space"),
        pytest.param("a='q'", {"a": "q"}, id="quoted-value"),
        pytest.param("echo a=1", {"a": "1"}, id="after-a-word"),
        pytest.param("no assignments here", {}, id="none"),
    ],
)
def test_local_variables(command: str, expected: dict[str, str]) -> None:
    assert _local_variables(command) == expected


def test_expand_locals_does_not_bleed_into_a_longer_name() -> None:
    """`$f` must not eat the `oo` of `$foo`."""
    assert _expand_locals("$f $foo", {"f": "X"}) == "X $foo"


# ---------- the hook path has more than one door ----------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath",
            id="exported-key",
        ),
        pytest.param("GIT_CONFIG_KEY_0=core.hookspath", id="exported-key-lowercase"),
        pytest.param('GIT_CONFIG_KEY_2="core.hooksPath"', id="quoted-key"),
        pytest.param("git config --global core.hooksPath /tmp/e", id="git-config-set"),
    ],
)
def test_every_door_to_the_hook_path_is_refused(command: str) -> None:
    """`-c` is one way in. The environment is another, and an `export` sits in
    its own segment with no `git` beside it, so the assignment is refused
    wherever it appears.
    """
    rule = check_command(command)
    assert rule is not None, f"{command!r} relocates the hook path"
    assert rule.name in {"git-hooks-path-override", "git-config-global"}


def test_naming_the_key_without_setting_it_stays_readable() -> None:
    assert check_command('grep -rn "core.hooksPath" review/') is None


# ---------- process substitution ----------


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        pytest.param(
            "cat <(cd ~/.claude && printf x > settings.json)",
            PROJECT,
            id="input-process-substitution",
        ),
        pytest.param(
            "diff <(cd ~/.claude && rm settings.json) /tmp/x",
            PROJECT,
            id="as-an-argument",
        ),
        pytest.param(
            "tee >(cd ~/.claude && rm settings.json)",
            PROJECT,
            id="output-process-substitution",
        ),
    ],
)
def test_process_substitution_is_read_as_a_command(command: str, cwd: str) -> None:
    """Its body runs like any other command, and the separator inside it tears
    the outer line apart before a per-segment pass could look.
    """
    assert check_relative_target(command, cwd) is not None


def test_ordinary_process_substitution_is_left_alone() -> None:
    assert check_relative_target("diff <(sort a.txt) <(sort b.txt)", CLAUDE_HOME) is None


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        pytest.param("cat <(a; b)", ["a; b"], id="input"),
        pytest.param("tee >(a; b)", ["a; b"], id="output"),
        pytest.param("x $(a) <(b) >(c)", ["a", "b", "c"], id="all-three"),
        pytest.param("cat <(echo <(inner))", ["echo <(inner)"], id="nested-keeps-inner"),
    ],
)
def test_substitutions_covers_process_substitution(
    segment: str,
    expected: list[str],
) -> None:
    assert _substitutions(segment) == expected
# ---------- a newline separates commands like a semicolon ----------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("a\nb", ["a", "b"], id="two-lines"),
        pytest.param("a\n\nb", ["a", "b"], id="blank-line-between"),
        pytest.param("cd /tmp\nls", ["cd /tmp", "ls"], id="cd-then-command"),
        pytest.param(
            'echo "one\ntwo"',
            ['echo "one\ntwo"'],
            id="a-newline-inside-quotes-is-text",
        ),
    ],
)
def test_a_newline_is_a_separator(command: str, expected: list[str]) -> None:
    assert _split_segments(command) == expected


def test_a_two_line_command_tracks_the_directory_between_lines() -> None:
    """Bash runs the second line in the directory the first one moved to.
    Reading both as one segment measured the write against the original cwd.
    """
    assert check_relative_target("cd ~/.claude\nrm settings.json", PROJECT) is not None


def test_a_two_line_command_that_moves_elsewhere_stays_allowed() -> None:
    assert check_relative_target("cd /tmp\nrm settings.json", CLAUDE_HOME) is None


# ---------- a `cd` that fails does not move the shell ----------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cd /definitely-missing; rm settings.json", id="missing-directory"),
        pytest.param("cd /etc/hostname && rm settings.json", id="a-file-not-a-directory"),
    ],
)
def test_a_failed_cd_leaves_the_write_where_it_was(command: str) -> None:
    """Bash reports the error and stays put, so the rest of the line runs in the
    old directory. Following the `cd` anyway measured the write somewhere the
    shell never went.
    """
    assert check_relative_target(command, CLAUDE_HOME) is not None


def test_a_successful_cd_is_still_followed() -> None:
    assert check_relative_target("cd /tmp && rm settings.json", CLAUDE_HOME) is None


# ---------- the file that registers the hook path ----------


@pytest.mark.parametrize(
    ("path", "cwd"),
    [
        pytest.param("/home/kern/.gitconfig", None, id="absolute"),
        pytest.param(".gitconfig", HOME_DIR, id="bare-name-at-home"),
        pytest.param("../.gitconfig", CLAUDE_HOME, id="relative-from-claude"),
    ],
)
def test_the_global_git_config_is_protected(path: str, cwd: str | None) -> None:
    """It is where core.hooksPath is registered, so rewriting it stops the
    pre-push hook running at all -- the same effect as the option this guard
    already refuses, reached by editing a file instead of passing a flag.
    """
    assert check_path(path, cwd) is not None


def test_a_project_gitconfig_is_not_the_global_one() -> None:
    assert check_path(".gitconfig", "/tmp") is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("printf x > ~/.gitconfig", id="tilde-redirect"),
        pytest.param("rm /home/kern/.gitconfig", id="absolute-remove"),
        pytest.param('cp /tmp/evil "$HOME/.gitconfig"', id="home-variable"),
    ],
)
def test_writing_the_global_git_config_is_refused(command: str) -> None:
    assert check_command(command) is not None or check_relative_target(command, HOME_DIR) is not None


# ---------- a variable can hold the config key as well as the verb ----------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'key=core.hooksPath; git -c "$key=/tmp/empty" status',
            id="key-in-a-variable",
        ),
        pytest.param(
            'k=core.hooksPath; v=/tmp/e; git -c "$k=$v" log',
            id="key-and-value-in-variables",
        ),
    ],
)
def test_a_variable_does_not_hide_the_hook_path_key(command: str) -> None:
    """The rules match text, so a key assembled from a variable carried neither
    the key nor the verb literally -- while the shell put both back before git
    saw the command. Assignments on the line are substituted before matching.
    """
    rule = check_command(command)
    assert rule is not None, f"{command!r} relocates the hook path"
    assert rule.name == "git-hooks-path-override"


def test_an_unrelated_variable_config_is_left_alone() -> None:
    assert check_command('k=user.name; git -c "$k=x" log --oneline') is None
# ---------- reading is not setting; single quotes do not expand ----------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git config --get core.hooksPath", id="get"),
        pytest.param("git config --get-all core.hooksPath", id="get-all"),
        pytest.param("git config --list", id="list"),
    ],
)
def test_reading_the_hook_path_is_allowed(command: str) -> None:
    """Matching the key anywhere near `git` refused a read that changes
    nothing -- and reading it is how you check the hook is still in place.
    """
    rule = check_command(command)
    assert rule is None or rule.name != "git-hooks-path-override"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git -c core.hooksPath=/tmp/e status", id="set-with-c"),
        pytest.param("git config core.hooksPath /tmp/e", id="set-with-config"),
        pytest.param("export GIT_CONFIG_KEY_0=core.hooksPath", id="set-via-env"),
    ],
)
def test_setting_the_hook_path_is_still_refused(command: str) -> None:
    rule = check_command(command)
    assert rule is not None
    assert rule.name in {"git-hooks-path-override", "git-config-global"}


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "echo '$(cd ~/.claude && rm settings.json)'",
            id="single-quoted-example",
        ),
        pytest.param("echo 'run $(rm -rf x) to break it'", id="prose-about-a-command"),
        pytest.param("echo '`cd ~/.claude && rm settings.json`'", id="quoted-backticks"),
    ],
)
def test_single_quotes_suppress_substitution(command: str) -> None:
    """Bash prints the text and runs nothing, so recursing into it refused a
    command that only shows an example.
    """
    assert check_relative_target(command, PROJECT) is None
    assert _substitutions(command) == []


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'echo "$(cd ~/.claude && rm settings.json)"',
            id="double-quotes-expand",
        ),
        pytest.param(
            "echo `cd ~/.claude && rm settings.json`",
            id="bare-backticks-expand",
        ),
    ],
)
def test_double_quotes_and_backticks_still_expand(command: str) -> None:
    """Only single quotes suppress it; the others run the body."""
    assert check_relative_target(command, PROJECT) is not None

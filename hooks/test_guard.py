from __future__ import annotations

import io
import json
from collections.abc import Callable

import pytest

from hooks.guard import check_command, main


StdinSetter = Callable[[object], None]


@pytest.fixture
def stdin_payload(monkeypatch: pytest.MonkeyPatch) -> StdinSetter:
    def _set(payload: object) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr("sys.stdin", io.StringIO(text))

    return _set


@pytest.mark.parametrize(
    ("command", "expected_rule"),
    [
        # ---------- git-no-verify ----------
        pytest.param(
            "git commit --no-verify -m 'x'", "git-no-verify", id="no-verify-long",
        ),
        pytest.param(
            "foo && git commit --no-verify -m 'x'",
            "git-no-verify",
            id="no-verify-chained",
        ),
        pytest.param("git commit -m 'x'", None, id="plain-commit-allowed"),
        pytest.param("git push -n origin main", None, id="push-dry-run-allowed"),
        pytest.param("ls -la", None, id="unrelated-allowed"),
        pytest.param("echo --no-verify", None, id="no-verify-without-git-allowed"),
        # ---------- git-force-push ----------
        pytest.param("git push --force", "git-force-push", id="push-force-long"),
        pytest.param(
            "git push -f origin main", "git-force-push", id="push-f-short",
        ),
        pytest.param(
            "git push origin main -f", "git-force-push", id="push-f-trailing",
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
        pytest.param("git push origin main", None, id="push-plain-allowed"),
        pytest.param("git push origin feature", None, id="push-feature-allowed"),
        pytest.param(
            "git push origin feature-fix", None, id="push-branch-with-dash-f-allowed",
        ),
        pytest.param(
            "git push origin some-foo", None, id="push-branch-some-foo-allowed",
        ),
        # Regression: numeric prefix before `-fix` must NOT trigger force-push.
        pytest.param(
            "git push origin task/USKO-032-fix-remaining-test-failures",
            None,
            id="push-branch-numeric-dash-fix-allowed",
        ),
        pytest.param(
            "git push origin 123-fu-branch",
            None,
            id="push-branch-numeric-dash-fu-allowed",
        ),
        pytest.param(
            "git push origin task/USKO-01-feature",
            None,
            id="push-branch-numeric-dash-feature-allowed",
        ),
        pytest.param(
            "git push origin release/2025-fast-hotfix",
            None,
            id="push-branch-numeric-dash-fast-allowed",
        ),
        pytest.param(
            "git push origin v1.2-fun",
            None,
            id="push-branch-dotted-dash-fun-allowed",
        ),
        # ---------- git-branch-force-delete ----------
        pytest.param(
            "git branch -D feature", "git-branch-force-delete", id="branch-D",
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
            "git branch -dv feature", None, id="branch-dv-non-force-allowed",
        ),
        pytest.param(
            "git branch -d fix-draft", None, id="branch-d-fix-draft-allowed",
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
            "git branch --list 'feature/*'", None, id="branch-list-glob-allowed",
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
            "git rebase -i main", "git-rebase-protected", id="rebase-interactive-main",
        ),
        pytest.param(
            "git rebase --onto main feature~5 feature",
            "git-rebase-protected",
            id="rebase-onto-main",
        ),
        pytest.param(
            "git rebase feature-branch", None, id="rebase-feature-allowed",
        ),
        pytest.param(
            "git rebase main-feature", None, id="rebase-main-feature-no-fp",
        ),
        pytest.param(
            "git rebase development", None, id="rebase-development-no-fp",
        ),
        pytest.param(
            "git rebase feature-main", None, id="rebase-feature-main-no-fp",
        ),
        pytest.param(
            "git rebase prod-dev", None, id="rebase-prod-dev-no-fp",
        ),
        # ---------- git-no-gpg-sign ----------
        pytest.param(
            "git commit --no-gpg-sign", "git-no-gpg-sign", id="commit-no-gpg-sign",
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
            "git config user.email foo", None, id="config-local-allowed",
        ),
        # ---------- rm-rf-home-or-root ----------
        pytest.param("rm -rf ~", "rm-rf-home-or-root", id="rm-rf-tilde"),
        pytest.param("rm -rf $HOME", "rm-rf-home-or-root", id="rm-rf-home"),
        pytest.param("rm -rf /", "rm-rf-home-or-root", id="rm-rf-root"),
        pytest.param("rm -r ~", "rm-rf-home-or-root", id="rm-r-tilde"),
        pytest.param(
            "rm -rf ~/foo", None, id="rm-rf-tilde-subpath-allowed",
        ),
        pytest.param(
            "rm -rf node_modules", None, id="rm-rf-relative-allowed",
        ),
        pytest.param("rm -rf /tmp/x", None, id="rm-rf-tmp-allowed"),
        pytest.param("rm file.txt", None, id="rm-no-flag-allowed"),
        pytest.param(
            "cd / && rm -rf foo", None, id="rm-rf-relative-after-cd-allowed",
        ),
        # ---------- git-reset-hard ----------
        pytest.param("git reset --hard", "git-reset-hard", id="reset-hard"),
        pytest.param(
            "git reset --hard HEAD~3", "git-reset-hard", id="reset-hard-target",
        ),
        pytest.param("git reset HEAD~1", None, id="reset-mixed-allowed"),
        pytest.param(
            "git reset --soft HEAD~1", None, id="reset-soft-allowed",
        ),
        # ---------- git-clean-force ----------
        pytest.param("git clean -f", "git-clean-force", id="clean-f"),
        pytest.param("git clean -fdx", "git-clean-force", id="clean-fdx"),
        pytest.param(
            "git clean --force", "git-clean-force", id="clean-force-long",
        ),
        pytest.param("git clean -n", None, id="clean-dry-run-allowed"),
        pytest.param(
            "git clean -n some-file", None, id="clean-pathspec-with-dash-allowed",
        ),
        pytest.param(
            "git clean -n config-files", None, id="clean-pathspec-config-allowed",
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
            "git checkout .", "git-checkout-discard", id="checkout-dot",
        ),
        pytest.param("git checkout main", None, id="checkout-branch-allowed"),
        pytest.param(
            "git checkout -b new-branch", None, id="checkout-create-allowed",
        ),
        # ---------- git-restore-discard ----------
        pytest.param(
            "git restore file.py", "git-restore-discard", id="restore-file",
        ),
        pytest.param("git restore .", "git-restore-discard", id="restore-dot"),
        pytest.param(
            "git restore --staged file.py", None, id="restore-staged-allowed",
        ),
        # ---------- git-commit-amend ----------
        pytest.param("git commit --amend", "git-commit-amend", id="commit-amend"),
        pytest.param(
            "git commit --amend --no-edit",
            "git-commit-amend",
            id="commit-amend-no-edit",
        ),
    ],
)
def test_check_command(command: str, expected_rule: str | None) -> None:
    rule = check_command(command)
    if expected_rule is None:
        assert rule is None, (
            f"expected allowed, got blocked by {rule.name if rule else None}"
        )
    else:
        assert rule is not None, f"expected blocked by {expected_rule}, got allowed"
        assert rule.name == expected_rule, (
            f"expected {expected_rule}, got {rule.name}"
        )


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

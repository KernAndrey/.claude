#!/usr/bin/env python3
"""PreToolUse hook: blocks forbidden commands/flags in Bash tool calls.

Architecture: each forbidden pattern is a `Rule` in the `RULES` list below.
To forbid a new command/flag, append one `Rule(...)` entry — no other code
changes required. The hook reads the PreToolUse JSON payload from stdin,
splits the Bash command into shell segments (by `;`, `&&`, `||`, `|`), and
matches each segment against every rule. On the first hit it exits with
code 2 and prints the reason on stderr — Claude Code surfaces stderr from
a PreToolUse hook back to the model when the exit code is 2.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    reason: str


# Splits a shell command into segments at `;`, `&&`, `||`, `|`.
# Newlines inside a segment (heredocs, multi-line strings) are preserved
# so rules can still match across them via `[\s\S]*`.
SEGMENT_SPLIT = re.compile(r"\s*(?:;|&&|\|\||\|)\s*")


RULES: list[Rule] = [
    Rule(
        name="git-no-verify",
        pattern=re.compile(r"\bgit\b[\s\S]*--no-verify\b"),
        reason=(
            "`git --no-verify` bypasses the pre-commit review hook. "
            "Only the user may use this flag. If the hook is failing, "
            "fix the underlying issue instead of skipping it."
        ),
    ),
    # ---------- Tier 1: enforced from CLAUDE.md "Git Safety" ----------
    Rule(
        name="git-force-push",
        # Catches: --force, --force-with-lease, and any short-flag cluster
        # containing `f` (-f, -fv, -vf, -fu, -fdv, ...). The negative
        # lookbehind on `(?<![a-zA-Z])` requires the leading `-` to start a
        # flag (not be embedded in a branch name like `feature-fix`).
        pattern=re.compile(
            r"\bgit\s+push\b[\s\S]*"
            r"(?:--force(?:-with-lease)?\b|(?<![a-zA-Z])-[a-zA-Z]*f[a-zA-Z]*\b)"
        ),
        reason=(
            "Force push (including --force-with-lease) rewrites shared history "
            "and is forbidden by CLAUDE.md ('Use standard push'). Only the user "
            "may force-push."
        ),
    ),
    Rule(
        name="git-branch-force-delete",
        # Catches:
        #   -D                                       (canonical force-delete)
        #   --delete --force / --force -d            (long-form combos)
        #   -df / -fd / -dfv / -vfd / -Df / ...      (any short-flag cluster
        #                                             containing both d|D and f)
        # The (?<![a-zA-Z]) lookbehind on the short-flag alternative ensures
        # the leading `-` starts a flag (not embedded in a branch name like
        # `fix-draft` or `add-pdf-export`).
        pattern=re.compile(
            r"\bgit\s+branch\b[\s\S]*"
            r"(?:"
            r"-D\b"
            r"|--delete\b[\s\S]*--force\b"
            r"|--force\b[\s\S]*-d\b"
            r"|(?<![a-zA-Z])-(?=[a-zA-Z]*[dD])(?=[a-zA-Z]*f)[a-zA-Z]+\b"
            r")"
        ),
        reason=(
            "Force-deleting a branch is irreversible and forbidden by CLAUDE.md "
            "('Preserve all branches — deleted branches are unrecoverable'). "
            "Only the user may delete branches."
        ),
    ),
    Rule(
        name="git-rebase-protected",
        # Lookbehind (?<![\w-]) prevents matching `feature-main` or `prod-dev`
        # while still allowing `origin/main` (preceded by `/`, not `-` or word).
        pattern=re.compile(
            r"\bgit\s+rebase\b[\s\S]*(?<![\w-])(?:main|master|dev)(?![-\w/])"
        ),
        reason=(
            "Rebasing main/master/dev rewrites shared history and is forbidden "
            "by CLAUDE.md. Rebase only personal feature branches."
        ),
    ),
    Rule(
        name="git-no-gpg-sign",
        # Lookbehind on the `-c` alternative prevents matching dashes
        # embedded in branch/file names like `feature-c` or `auto-config`.
        pattern=re.compile(
            r"\bgit\b[\s\S]*"
            r"(?:--no-gpg-sign\b|(?<![a-zA-Z])-c\s+commit\.gpgsign\s*=\s*false\b)"
        ),
        reason=(
            "Skipping GPG signing is forbidden by the Claude Code system prompt. "
            "Only the user may bypass commit signing."
        ),
    ),
    Rule(
        name="git-config-global",
        pattern=re.compile(r"\bgit\s+config\s+(?:--global|--system)\b"),
        reason=(
            "Modifying global/system git config is forbidden by the Claude Code "
            "system prompt ('NEVER update the git config'). Only the user may "
            "change git config."
        ),
    ),
    # ---------- Tier 2: irreversible destructive operations ----------
    Rule(
        name="rm-rf-home-or-root",
        pattern=re.compile(
            r"\brm\b(?=[\s\S]*-[a-zA-Z]*[rR])"
            r"[\s\S]*\s(?:~|\$HOME|\$\{HOME\}|/)\s*$"
        ),
        reason=(
            "Recursive removal of $HOME or `/` is catastrophic and unrecoverable. "
            "Only the user may run this. Subpaths like ~/foo or /tmp/x are still allowed."
        ),
    ),
    Rule(
        name="git-reset-hard",
        pattern=re.compile(r"\bgit\s+reset\b[\s\S]*--hard\b"),
        reason=(
            "`git reset --hard` discards uncommitted work irreversibly. "
            "Only the user may run this. Use `git stash` to set things aside instead."
        ),
    ),
    Rule(
        name="git-clean-force",
        # Lookbehind prevents matching dashes embedded in pathspec arguments
        # like `git clean -n some-file` (the `-file` would otherwise match).
        pattern=re.compile(
            r"\bgit\s+clean\b[\s\S]*"
            r"(?:(?<![a-zA-Z])-[a-zA-Z]*f[a-zA-Z]*\b|--force\b)"
        ),
        reason=(
            "`git clean -f` deletes untracked files irreversibly. "
            "Only the user may run this. Use `git clean -n` to preview first."
        ),
    ),
    Rule(
        name="git-checkout-discard",
        pattern=re.compile(r"\bgit\s+checkout\s+(?:--(?:\s|$)|\.(?:\s|$))"),
        reason=(
            "`git checkout --` and `git checkout .` discard uncommitted edits "
            "irreversibly. Only the user may run this."
        ),
    ),
    Rule(
        name="git-restore-discard",
        pattern=re.compile(r"\bgit\s+restore\b(?![\s\S]*--staged\b)"),
        reason=(
            "`git restore` (without --staged) discards uncommitted edits "
            "irreversibly. Only the user may run this."
        ),
    ),
    Rule(
        name="git-commit-amend",
        pattern=re.compile(r"\bgit\s+commit\b[\s\S]*--amend\b"),
        reason=(
            "Amending modifies an existing commit. CLAUDE.md and the Claude Code "
            "system prompt require creating NEW commits instead. Only the user "
            "may explicitly request --amend."
        ),
    ),
]


def check_command(command: str) -> Rule | None:
    """Return the first matching forbidden rule, or None if the command is allowed.

    Splits the command into shell segments first so each segment is matched
    independently — that way `git status && git push --force` triggers
    git-force-push on the second segment without polluting the first.
    """
    for segment in SEGMENT_SPLIT.split(command):
        for rule in RULES:
            if rule.pattern.search(segment):
                return rule
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    if data.get("tool_name") != "Bash":
        return

    command: str = data.get("tool_input", {}).get("command") or ""
    if not command:
        return

    rule = check_command(command)
    if rule is None:
        return

    print(
        f"Blocked by guard rule '{rule.name}': {rule.reason}",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()

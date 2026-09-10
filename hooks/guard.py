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
import os
import re
import shlex
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
    Rule(
        name="review-approvals-write",
        # Pre-review approval markers (.review/approvals/<hash>) are the
        # fast-path's "this diff was reviewed CLEAN" signal. They are written
        # ONLY by review/pre_review.py (Python). A shell write here would forge
        # an approval and skip the LLM review for unreviewed code. Reads
        # (cat/ls) are allowed — only write verbs/redirects to that path match.
        pattern=re.compile(
            r"(?:>>?|\btee\b|\bcp\b|\bmv\b|\binstall\b|\btouch\b|\bln\b|\bdd\b)[\s\S]*\.review/approvals"
        ),
        reason=(
            "Writing to .review/approvals/ forges a pre-review approval and "
            "would skip the AI review for unreviewed code. Only review/pre_review.py "
            "may create these markers. Let the pre-review run produce them."
        ),
    ),
    # ---------- Tier 1: enforced from CLAUDE.md "Git Safety" ----------
    Rule(
        name="git-force-push",
        # Catches: --force, --force-with-lease, and any short-flag cluster
        # containing `f` (-f, -fv, -vf, -fu, -fdv, ...). The `(?<![^\s])`
        # lookbehind requires the leading `-` to be at the start of a token
        # (preceded by whitespace or start-of-string) — not embedded in a
        # branch name like `task/USKO-032-fix-...` or `feature-fix`.
        pattern=re.compile(
            r"\bgit\s+push\b[\s\S]*"
            r"(?:--force(?:-with-lease)?\b|(?<![^\s])-[a-zA-Z]*f[a-zA-Z]*\b)"
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
        # The (?<![^\s]) lookbehind on the short-flag alternative ensures
        # the leading `-` is at token start (preceded by whitespace or SOL),
        # not embedded in a branch name like `USKO-032-dfx-feature` or
        # `fix-draft` or `add-pdf-export`.
        pattern=re.compile(
            r"\bgit\s+branch\b[\s\S]*"
            r"(?:"
            r"-D\b"
            r"|--delete\b[\s\S]*--force\b"
            r"|--force\b[\s\S]*-d\b"
            r"|(?<![^\s])-(?=[a-zA-Z]*[dD])(?=[a-zA-Z]*f)[a-zA-Z]+\b"
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
        pattern=re.compile(r"\bgit\s+rebase\b[\s\S]*(?<![\w-])(?:main|master|dev)(?![-\w/])"),
        reason=(
            "Rebasing main/master/dev rewrites shared history and is forbidden "
            "by CLAUDE.md. Rebase only personal feature branches."
        ),
    ),
    Rule(
        name="git-no-gpg-sign",
        # Lookbehind on the `-c` alternative prevents matching dashes
        # embedded in branch/file names like `feature-c`, `USKO-01-config`,
        # or `auto-config`.
        pattern=re.compile(
            r"\bgit\b[\s\S]*"
            r"(?:--no-gpg-sign\b|(?<![^\s])-c\s+commit\.gpgsign\s*=\s*false\b)"
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
        # like `git clean -n some-file` or `USKO-032-fix` (`-file`/`-fix`
        # would otherwise match).
        pattern=re.compile(
            r"\bgit\s+clean\b[\s\S]*"
            r"(?:(?<![^\s])-[a-zA-Z]*f[a-zA-Z]*\b|--force\b)"
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
            "`git checkout --` and `git checkout .` discard uncommitted edits irreversibly. Only the user may run this."
        ),
    ),
    Rule(
        name="git-restore-discard",
        pattern=re.compile(r"\bgit\s+restore\b(?![\s\S]*--staged\b)"),
        reason=(
            "`git restore` (without --staged) discards uncommitted edits irreversibly. Only the user may run this."
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
    # ---------- Tier 3: Claude may not publish, and may not disarm ----------
    Rule(
        name="git-push-denied",
        # Publishing is the user's alone. This rule is the legible half of the
        # block; the load-bearing half is ~/.claude/git-hooks/pre-push, which
        # matches no text at all and so survives `g=push; git $g`, eval, a
        # script file, a codex subprocess, or subprocess.run() in a notebook.
        # The prefix group admits only flag-shaped tokens between `git` and the
        # verb, so `git -C /path push` matches while `git commit -m "add push
        # support"` does not.
        pattern=re.compile(
            # An option is any dash-led token -- `-c`, `--force`, and the
            # attached form `-C/tmp`, which carries its value with no separator
            # and was the spelling that slipped through. A detached value may
            # follow, and must not itself start with a dash, so a bare flag
            # before the verb still leaves the verb matchable.
            r"\bgit\s+(?:-\S+(?:\s+[^-\s]\S*)?\s+)*"
            r"(?:push|send-pack|subtree\s+push)\b"
        ),
        reason=(
            "Pushing is reserved for the user — Claude Code may commit locally "
            "but never publish. The same refusal is enforced at the git level by "
            "~/.claude/git-hooks/pre-push, which the user has made immutable. "
            "Ask the user to push."
        ),
    ),
    Rule(
        name="git-hooks-path-override",
        # core.hooksPath is ordinary configuration and command-line scope beats
        # every file, so this one option switches the pre-push hook off for the
        # invocation. A verb can hide behind a variable -- `g=push; git ... "$g"`
        # carries no literal verb for any text rule -- but the option that makes
        # hiding it worthwhile cannot hide, so refuse the option instead. There
        # is no legitimate reason for this process to relocate the hook path.
        # Git config keys are case-insensitive, so an exact-case match left
        # `-c core.hookspath=` reaching the remote with both layers silent.
        #
        # `-c` is not the only way in. GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n set
        # the same key from the environment, and an `export` sits in its own
        # shell segment with no `git` beside it -- so the assignment is refused
        # wherever it appears, not only next to an invocation. Naming the key
        # without assigning it (grep, a comment) stays readable.
        # Only a *set* is refused. Matching the key anywhere near `git` also
        # refused `git config --get core.hooksPath`, which reads and changes
        # nothing -- and reading it is how you check the hook is still in place.
        pattern=re.compile(
            r"(?i:"
            r"core\.hooksPath\s*="
            r"|GIT_CONFIG_KEY_\d+\s*=\s*[\x22\x27]?core\.hooksPath"
            r"|\bgit\s+config\b(?![^\n]*--(?:get|list|get-all|get-regexp))"
            r"[^\n]{0,80}?core\.hooksPath"
            r")"
        ),
        reason=(
            "Overriding core.hooksPath disables the pre-push hook for that "
            "invocation, which is the enforcement this process is not allowed "
            "to switch off. Only the user may do this."
        ),
    ),
    Rule(
        name="protected-config-write",
        # The guard, the global git hooks and the hook registration are the
        # machinery enforcing every other rule here — one write to any of them
        # disarms the lot. Write verbs aimed at those paths are refused; reads
        # (cat/sed/head/grep/ls) are deliberately left alone.
        pattern=re.compile(
            # `(?<![0-9&])` and `(?!&)` keep `2>&1` and `>&2` out: those move
            # a stream, they do not write the named file, and reading a
            # protected file with stderr attached was being refused.
            r"(?:(?<![0-9&])>{1,2}(?!&)|\btee\b|\bcp\b|\bmv\b|\brm\b|\binstall\b|\btouch\b"
            r"|\bln\b|\bdd\b|\btruncate\b|\bchattr\b|\bchmod\b|\bchown\b"
            r"|\bpython3?\b|\bsed\b[^\n]*?-i)"
            r"[\s\S]*"
            r"(?:(?:/home/kern|~|\$HOME|\$\{HOME\})/\.claude/"
            r"(?:hooks/lint\.py|hooks/notify\.sh"
            r"|statusline-command\.sh|git-hooks|settings\.json)\b"
            r"|(?:/home/kern|~|\$HOME|\$\{HOME\})/\.gitconfig\b"
            r"|/etc/claude-code\b)"
        ),
        reason=(
            "That path holds the guard hook, the global git hooks or the hook "
            "registration — the machinery that enforces every other rule here. "
            "Only the user may change it; the files are root-immutable (chattr +i)."
        ),
    ),
]


# A newline separates two commands exactly as `;` does. Leaving it out meant a
# two-line command was read as one segment, so a `cd` on the first line never
# registered and the write on the second was judged against the wrong place.
_SEPARATORS: tuple[str, ...] = ("&&", "||", ";", "|", "\n")
_QUOTE_CHARS = "\x22\x27"  # " and ' -- spelled in hex so either may appear here


# A substitution body is always strictly shorter than the text it came from, so
# recursion terminates on its own; the cap is a guard against a pathological
# command, not part of the logic.
_MAX_SUBSTITUTION_DEPTH = 8


# Every construct whose parenthesised body is a command in its own right:
# command substitution and both directions of process substitution.
_SUBSTITUTION_OPENERS: tuple[str, ...] = ("$(", "<(", ">(")


def _balanced_body(command: str, start: int) -> tuple[str, int]:
    """The body of the substitution opening at `start`, and where it ends.

    Nesting is counted so an inner opener keeps its body attached to the outer
    one. An unterminated body is returned as far as it goes, since half a
    command is still worth reading.
    """
    depth = 1
    first = start + 2
    scan = first
    while scan < len(command) and depth:
        if any(command.startswith(o, scan) for o in _SUBSTITUTION_OPENERS):
            depth += 1
            scan += 2
            continue
        if command[scan] == ")":
            depth -= 1
        scan += 1
    return (command[first : scan - 1] if depth == 0 else command[first:]), scan


def _substitutions(command: str) -> list[str]:
    """The bodies of `$(...)`, `<(...)`, `>(...)` and backtick substitutions.

    Each body is a command in its own right, so it has to be read as one rather
    than left as text on the outer line. Nesting is tracked so an inner opener
    keeps its body attached to the outer one.
    """
    bodies: list[str] = []
    index = 0
    quoted = False
    while index < len(command):
        # Single quotes suppress substitution entirely: `echo '$(rm x)'` prints
        # the text and runs nothing, and recursing into it refused a command
        # that only shows an example. Double quotes do expand, so they are
        # deliberately not treated the same way.
        if command[index] == "\x27":
            quoted = not quoted
            index += 1
            continue
        opener = None if quoted else next((o for o in _SUBSTITUTION_OPENERS if command.startswith(o, index)), None)
        if opener is not None:
            body, index = _balanced_body(command, index)
            bodies.append(body)
            continue
        if command[index] == "`" and not quoted:
            end = command.find("`", index + 1)
            if end == -1:
                break
            bodies.append(command[index + 1 : end])
            index = end + 1
            continue
        index += 1
    return bodies


# A `name=value` assignment, at the start of the line or after a separator.
# A quoted value is taken whole, spaces included, because that is what the
# shell assigns; reading it as far as the first space produced a fragment that
# was neither the value nor nothing.
_ASSIGNMENT = re.compile(
    r"(?:^|[\s;&|(])([A-Za-z_]\w*)="
    r"(?:\x22([^\x22]*)\x22|\x27([^\x27]*)\x27|([^\s;&|)]*))"
)


def _local_variables(command: str) -> dict[str, str]:
    """Assignments made earlier on the same command line.

    `target=settings.json; cd ~/.claude && printf x > "$target"` writes the
    protected file, while a literal check of `$target` resolves nowhere. This
    reads the simple form and nothing cleverer -- an expansion whose value is
    computed is past what any text pass can follow, which is why the layers
    that do not read text exist.
    """
    return {
        match.group(1): next(g for g in match.groups()[1:] if g is not None) for match in _ASSIGNMENT.finditer(command)
    }


def _expand_locals(text: str, variables: dict[str, str]) -> str:
    """Substitute `$name` and `${name}` for the values assigned on this line."""
    for name, value in variables.items():
        text = re.sub(rf"\$\{{{re.escape(name)}\}}|\${re.escape(name)}\b", value, text)
    return text


def _expand(path: str) -> str:
    """Expand both spellings of the home directory.

    `expanduser` handles `~` and nothing else, so `cd "$HOME/.claude"` resolved
    to a literal `$HOME` directory underneath wherever the shell already stood,
    and the protected write that followed was judged against the wrong place.
    """
    return os.path.expanduser(os.path.expandvars(path))


def _split_segments(command: str) -> list[str]:
    """Split `command` at shell separators that sit outside quotes.

    SEGMENT_SPLIT is a plain regex and cuts inside quoted text too, so a
    semicolon in a python one-liner separated the interpreter from its target
    and left two harmless-looking halves. Every rule matches per segment, so
    every rule missed it -- this was never specific to one check.
    """
    segments: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        # A backslash escapes the next character everywhere except inside single
        # quotes, where the shell gives it no special meaning. Treating `\"` as a
        # real delimiter flipped the quote state and split a command in half at a
        # semicolon that was only ever string content.
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote is not None:
            quote = None if char == quote else quote
        elif char in _QUOTE_CHARS:
            quote = char
        else:
            hit = next((s for s in _SEPARATORS if command.startswith(s, index)), None)
            if hit is not None:
                segments.append(command[start:index])
                index += len(hit)
                start = index
                continue
        index += 1
    segments.append(command[start:])
    return [segment.strip() for segment in segments if segment.strip()]


def check_command(command: str) -> Rule | None:
    """Return the first matching forbidden rule, or None if the command is allowed.

    Splits the command into shell segments first so each segment is matched
    independently — that way `git status && git push --force` triggers
    git-force-push on the second segment without polluting the first.

    Variables assigned on the line are substituted before matching, for the
    same reason the path checks do it: `key=core.hooksPath; git -c "$key=/tmp"`
    carries neither the key nor the verb literally, and the shell puts both
    back before git ever sees the command.
    """
    variables = _local_variables(command)
    for raw_segment in _split_segments(command):
        segment = _expand_locals(raw_segment, variables)
        for rule in RULES:
            if rule.pattern.search(segment):
                return rule
    return None


# Absolute locations no tool may write. They hold this guard, the global git
# hooks, and the registration that makes the guard run at all — a single Write
# to any of them disarms every rule above. Claude Code's Write/Edit tools never
# reach check_command(), which is exactly why they need their own check.
PROTECTED_PATHS: tuple[str, ...] = (
    # Every file the harness or git runs by itself, minus this one. A file that
    # executes without a tool call is somewhere to leave code that runs later,
    # which is why the rest are here; test scaffolding beside them is only ever
    # run by pytest and is not.
    #
    # guard.py is deliberately absent. What this machinery is for is that a push
    # to a real repository cannot happen by accident -- the git hooks and the
    # registration below carry that, and they are edited rarely. Guarding the
    # guard's own source additionally assumed a session that would set out to
    # disarm itself, which is not the threat here, and it cost a hand-off on
    # every review round. An accidental write does not aim at this file.
    "/home/kern/.claude/hooks/lint.py",
    "/home/kern/.claude/hooks/notify.sh",
    "/home/kern/.claude/statusline-command.sh",
    "/home/kern/.claude/git-hooks",
    "/home/kern/.claude/settings.json",
    "/etc/claude-code",
    # ~/.gitconfig is where core.hooksPath is registered, so rewriting it stops
    # the pre-push hook from running at all -- the same effect as the option
    # this file already refuses, reached by editing a file instead of passing a
    # flag. `git config --global` was refused and the file itself was not.
    "/home/kern/.gitconfig",
)


# Path tails that identify a protected location no matter what they are joined
# to. A relative path, or one assembled at runtime, never matches PROTECTED_PATHS
# by prefix, but it still names the same files.
PROTECTED_FRAGMENTS: tuple[str, ...] = (
    ".claude/hooks/lint.py",
    ".claude/hooks/notify.sh",
    ".claude/statusline-command.sh",
    ".claude/git-hooks",
    ".claude/settings.json",
    "/etc/claude-code",
    "/.gitconfig",
)


def _protection_reason(where: str, subject: str) -> str:
    return (
        f"{subject} names the protected location {where}. It is executed by "
        "the harness or by git without a tool call, or it decides whether the "
        "guard runs at all. Only the user may change it."
    )


# Shell write verbs, kept beside the rule above so the two cannot drift.
# `python3` is deliberately absent: it is a write only when the source says so,
# and treating every invocation as one refused ordinary reads.
# `(?<![0-9&])` and `(?!&)` keep `2>&1` and `>&2` out: those move a stream,
# they do not write the named file.
# `(?!&)` is the whole exclusion: `>&` duplicates a file descriptor and writes
# no file, which covers `2>&1` and `>&2`. A leading digit does NOT make it
# harmless -- `1>file` and `2>errors.log` write their file like any other
# redirect, and excluding them let `printf x 1>settings.json` through.
_REDIRECT = re.compile(r">{1,2}(?!&)")

# What a redirect claims: the one token after it, and nothing else on the line.
_REDIRECT_TARGET = re.compile(r">{1,2}(?!&)\s*([^\s;&|<>]+)")

# Verbs that act on their operands. Which operand is the destination differs
# per verb -- `cp a b`, `dd of=x`, `install -m 644 a b` -- and deciding that
# is a shell parser's job, so every operand of such a segment counts.
_VERB_WRITE = re.compile(
    r"\btee\b|\bcp\b|\bmv\b|\brm\b|\binstall\b"
    r"|\btouch\b|\bln\b|\bdd\b|\btruncate\b|\bchattr\b|\bchmod\b"
    r"|\bchown\b|\bsed\b[^\n]*?-i"
)

# A python one-liner counts as a write only when it says so in the source.
_PY_WRITE = re.compile(
    r"\bpython3?\b[\s\S]*?(?:['\"][wax]b?\+?['\"]|\.write\(|\.writelines\(|\.unlink\("
    r"|\.rename\(|\.replace\(|\.truncate\(|\.mkdir\("
    r"|shutil\.(?:copy\w*|move|rmtree)|os\.(?:remove|unlink|rename|replace))"
)

# A `cd` is a directory change only when the segment IS one. Searching for it
# anywhere let quoted data pose as a directory change -- and, worse, made the
# tracker skip the write that followed on the same line.
_CD_SEGMENT = re.compile(r"^cd(?:\s+(?P<args>.*))?$")


def _is_write(segment: str) -> bool:
    return bool(_REDIRECT.search(segment) or _VERB_WRITE.search(segment) or _PY_WRITE.search(segment))


def _write_targets(segment: str) -> set[str]:
    """The operands `segment` could actually write.

    A redirect names its target, so it gets that token and no other. Treating
    every operand as a target instead refused `diff -q <protected> x >
    /dev/null`, which reads the protected file and writes /dev/null -- a real
    command of this session, refused seconds after the check went in.

    A write verb still claims all of its operands, because separating source
    from destination per verb is a shell parser's job.
    """
    # The captured token keeps whatever quoting the shell would strip, and a
    # path judged with its quotes attached resolves nowhere.
    targets = {match.group(1).strip("\x22\x27") for match in _REDIRECT_TARGET.finditer(segment)}
    if _VERB_WRITE.search(segment) or _PY_WRITE.search(segment):
        targets |= _operands(segment)
    return targets


def _cd_destination(segment: str, home: str, previous: str) -> str | None:
    """The directory `segment` changes to, or None if it is not a `cd`.

    The anchor at the start of the segment is what settles it: `printf 'cd
    hooks' > settings.json` is a write, not a directory change. Masking the
    quotes as well added nothing and blanked a destination containing a space.

    Arguments go through shlex, so a quoted path survives. `--` and option
    tokens are skipped rather than mistaken for the destination, a bare `cd`
    goes home, and `cd -` returns to wherever the last change came from.
    """
    match = _CD_SEGMENT.match(segment.strip())
    if match is None:
        return None
    args = (match.group("args") or "").strip()
    if not args:
        return home
    try:
        tokens = shlex.split(args)
    except ValueError:  # unbalanced quote: fall back to whitespace
        tokens = args.split()
    for token in tokens:
        if token == "-":
            return previous
        if token == "--" or token.startswith("-"):
            continue
        return token
    return home


# Operands a shell segment might be writing: quoted strings keep their spaces,
# bare words stop at whitespace and shell metacharacters. Both spellings reach
# the same file, and a python one-liner spells its target as a quoted string.
_QUOTED_SINGLE = re.compile(r"'([^']+)'")
_QUOTED_DOUBLE = re.compile(r'"([^"]+)"')
_BARE_WORD = re.compile(r"[^\s;&|<>()'\x22]+")


def _mask_prose(segment: str) -> str:
    """Blank quoted spans that read as prose rather than as a filename.

    `echo "regenerating settings.json" > /tmp/log` mentions a protected name
    without touching it. Refusing that teaches the reader to route around the
    guard, which costs more than the case is worth. A quoted span containing
    whitespace is prose; one without is a plausible operand and stays. Either
    way a nested quote is still recovered by its own pass.
    """

    def blank(match: re.Match[str]) -> str:
        if " " in match.group(1) or "	" in match.group(1):
            return " " * len(match.group(0))
        return match.group(0)

    return _QUOTED_DOUBLE.sub(blank, _QUOTED_SINGLE.sub(blank, segment))


def _operands(segment: str) -> set[str]:
    """Every token in `segment` that could name a file.

    All passes run over the whole segment rather than tokenising it once: a
    python one-liner nests its real target inside an outer quote --
    `python3 -c "open('guard.py', 'w')"` -- and a single pass hands back
    only the outer string, whose resolution lands nowhere.
    """
    found = {m.group(1) for m in _QUOTED_SINGLE.finditer(segment)}
    found |= {m.group(1) for m in _QUOTED_DOUBLE.finditer(segment)}
    found |= set(_BARE_WORD.findall(_mask_prose(segment)))
    return found


def _protected_in(directory: str, segment: str) -> str | None:
    """Refusal reason if anything `segment` writes lands on a protected file.

    Every operand is resolved against `directory` and handed to check_path.
    Matching bare names instead only fired when the shell already stood in the
    file's own directory, so a parent-relative operand -- `.claude/hooks/guard.py`
    from the home directory -- walked straight past it.
    """
    for operand in _write_targets(segment):
        if not operand or operand.startswith("-"):
            continue
        reason = check_path(operand, directory)
        if reason is not None:
            return reason
    return None


def check_relative_target(command: str, cwd: str | None, depth: int = 0) -> str | None:
    """Refuse a write whose operand lands on a protected file.

    Rules match one shell segment at a time, so `cd <dir> && <write>` split the
    directory away from the write and neither half looked protected. Walking
    the segments in order, carrying the directory `cd` leaves behind, puts them
    back together -- and carrying it forward rather than accumulating means a
    `cd` elsewhere genuinely moves away, instead of leaving the old directory
    armed for the rest of the line.

    Scoped by resolution, not by name: `settings.json` is only the harness's
    registration once it resolves inside ~/.claude. Plenty of projects have a
    file by that name, and they resolve elsewhere.
    """
    if depth > _MAX_SUBSTITUTION_DEPTH:
        return None

    home = os.path.realpath(os.path.expanduser("~"))
    current = os.path.realpath(cwd or os.getcwd())
    previous = current

    # Substitutions come out of the whole command first, because the splitter
    # is a flat scanner: a separator inside a substitution, or a quote that
    # restarts inside one, tears it in half before any per-segment pass could
    # look. `echo `cd ~/.claude && rm settings.json`` split at the `&&` and
    # left two fragments each holding one backtick.
    for body in _substitutions(command):
        reason = check_relative_target(body, current, depth + 1)
        if reason is not None:
            return reason

    variables = _local_variables(command)
    for raw_segment in _split_segments(command):
        # review-note: a shell command cannot be judged completely by reading
        # it -- expansion, substitution and quoting compose without limit, and
        # each round of review has found another spelling. This layer exists to
        # refuse the legible cases early and explain why; the invariant it
        # serves is carried by the two layers that read no text at all: the
        # pre-push hook, which sees the act rather than its spelling, and the
        # kernel flag the user sets on the files below. Treat a gap here as a
        # missing refusal, not as a hole in the protection.
        segment = _expand_locals(raw_segment, variables)

        # The write is checked first and unconditionally. Treating a segment as
        # a directory change used to skip it, which is how a line that both
        # mentioned `cd` and performed a write went unexamined.
        if _is_write(segment):
            reason = _protected_in(current, segment)
            if reason is not None:
                return reason
        # And again per segment, now that a `cd` earlier on the line has moved
        # `current`: a substitution that survived the split is judged from where
        # it would actually run.
        for body in _substitutions(segment):
            reason = check_relative_target(body, current, depth + 1)
            if reason is not None:
                return reason

        destination = _cd_destination(segment, home, previous)
        if destination is not None:
            moved = os.path.realpath(os.path.join(current, _expand(destination)))
            # A `cd` to somewhere that is not a directory fails and leaves the
            # shell where it was. Following it anyway sent the rest of the line
            # to be judged against a directory the shell never entered, so
            # `cd /nowhere; rm settings.json` was measured in /nowhere.
            if os.path.isdir(moved):
                previous = current
                current = moved
    return None


def check_path(path: str, cwd: str | None = None) -> str | None:
    """Return a refusal reason if `path` names a protected location.

    `cwd` is the payload's working directory, which is where a relative
    file_path actually lands. Resolving against the hook process's own cwd
    instead let `file_path="hooks/guard.py"` be judged against the wrong
    directory and come back clean while rewriting the guard.

    Symlinks and `..` are resolved too, so `~/.claude/../.claude/hooks/guard.py`
    and a symlink planted elsewhere both normalize onto the protected prefix.
    """
    expanded = _expand(path)
    if not os.path.isabs(expanded):
        # A malformed payload can omit cwd; os.getcwd() is then the best
        # base available.
        expanded = os.path.join(cwd or os.getcwd(), expanded)
    resolved = os.path.realpath(expanded)

    # Resolution is the whole answer here. A fragment test used to run as well,
    # from when relative paths were judged by name; now that they resolve
    # against cwd it decided nothing extra and refused a project's own
    # `.claude/settings.json`, which resolves nowhere near this one. Fragments
    # still serve the code tool, where there is no path to resolve.
    for protected in PROTECTED_PATHS:
        if resolved == protected or resolved.startswith(protected + os.sep):
            return _protection_reason(protected, f"'{path}'")
    return None


# Tools that name a file to write; the path is what has to be checked. These
# never carry a shell command, so the command rules above cannot see them --
# before this dispatch existed, a Write to guard.py silently disarmed the guard.
PATH_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
PATH_KEYS: tuple[str, ...] = ("file_path", "notebook_path")

# Tools that run code in a runtime of their own. mcp__ide__executeCode is a
# Jupyter kernel: subprocess.run(["git", "push"]) there answers to none of the
# shell rules, so its source is matched as though it were a shell command.
CODE_TOOLS: frozenset[str] = frozenset({"mcp__ide__executeCode"})

# Matched only against source handed to CODE_TOOLS. subprocess.run(["git",
# "push"]) splits the two words across list syntax, so the shell rules above
# look right past it; bounded proximity catches every argv shape. The `.push(`
# alternative catches the GitPython/pygit2 spelling -- safe in a Python kernel,
# where lists append rather than push.
CODE_PUSH_RULE: Rule = Rule(
    name="git-push-denied",
    pattern=re.compile(
        # A trailing hyphen after `git`, or a leading one before `push`, means a
        # compound word: `git-hooks`, `pre-push`. Excluding both keeps a path
        # like git-hooks/pre-push from reading as a publish attempt, while
        # ["git", "push"] -- a space after normalization -- still matches.
        r"\bgit\b(?!-)[\s\S]{0,160}?(?<![-\w])(?:push|send[-_]pack)\b"
        # The library spelling needs a git-shaped receiver: `repo.remote().push()`
        # publishes, `stack.push(item)` does not, and refusing both made the
        # rule untrustworthy in ordinary notebook code.
        r"|\b(?:repo|repository|remote|origin)\w*(?:\.\w+\([^)]*\))*\.push\s*\("
    ),
    reason=(
        "Pushing is reserved for the user -- Claude Code may commit locally but "
        "never publish. The same refusal is enforced at the git level by "
        "~/.claude/git-hooks/pre-push. Ask the user to push."
    ),
)


# Source handed to a code tool is not a shell command: subprocess.run(["git",
# "reset", "--hard"]) spreads one command across list syntax, and
# Path.home() / ".claude" / "hooks" spreads one path the same way. Collapsing
# the quote-comma and quote-slash joins turns both back into the contiguous
# text the rules and fragments are written against.
_ARGV_JOIN = re.compile(r"""['"]\s*,\s*['"]""")
_PATH_JOIN = re.compile(r"""['"]\s*/\s*['"]""")


def normalize_code(code: str) -> str:
    """Rewrite argv-list syntax into a contiguous command: a quote-comma join
    becomes a space, because that is the separator argv stands in for.
    """
    return _PATH_JOIN.sub("/", _ARGV_JOIN.sub(" ", code))


def normalize_paths(code: str) -> str:
    """Rewrite joined path pieces into a contiguous path.

    The same quote-comma join means a separator here, not a space:
    os.path.join(home, ".claude", "hooks") builds one path, so it has to
    collapse to `.claude/hooks` for the fragment test to see it. One
    normalization cannot serve both readings, hence two.
    """
    return _ARGV_JOIN.sub("/", _PATH_JOIN.sub("/", code))


def block(message: str) -> None:
    """Refuse the call: exit 2, which makes Claude Code surface stderr to the model."""
    print(message, file=sys.stderr)
    sys.exit(2)


def _check_bash(tool_input: dict[str, object], cwd: str | None) -> None:
    """Refuse a shell command that matches a rule, or that writes a protected
    file under a bare name from the directory where that name resolves to it.
    """
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return
    rule = check_command(command)
    if rule is not None:
        block(f"Blocked by guard rule {rule.name!r}: {rule.reason}")
    reason = check_relative_target(command, cwd)
    if reason is not None:
        block(f"Blocked by guard rule 'protected-config-write': {reason}")


def _check_path_tool(tool_input: dict[str, object], cwd: str | None) -> None:
    """Refuse a file-writing tool aimed at a protected location."""
    for key in PATH_KEYS:
        path = tool_input.get(key)
        if not isinstance(path, str) or not path:
            continue
        reason = check_path(path, cwd)
        if reason is not None:
            block(f"Blocked by guard rule 'protected-path-write': {reason}")


def _check_code_tool(tool_input: dict[str, object]) -> None:
    """Refuse executed source that publishes, or that touches a protected path."""
    code = tool_input.get("code")
    if not isinstance(code, str) or not code:
        return
    normalized = normalize_code(code)

    if CODE_PUSH_RULE.pattern.search(normalized):
        block(f"Blocked by guard rule {CODE_PUSH_RULE.name!r}: {CODE_PUSH_RULE.reason}")

    # Both spellings: the raw source for anything already contiguous, the
    # normalized form for the argv-list spelling of the very same command.
    for candidate in (code, normalized):
        rule = check_command(candidate)
        if rule is not None:
            block(f"Blocked by guard rule {rule.name!r}: {rule.reason}")

    spellings = (code, normalized, normalize_paths(code))
    # review-note: this branch refuses a protected path in executed source
    # whether the source reads or writes it, and that asymmetry with the Bash
    # rule is deliberate. Telling a read from a write here needs the value of
    # an expression -- `open(p, mode)`, a variable, a chr() splice -- which is
    # decidable only by running the code, and a wrong guess writes the guard.
    # Reading a protected file has a first-class path that is unaffected: the
    # Read tool. So the invariant is "executed source does not name the
    # enforcement files", which costs an inspection route that already exists
    # and buys a check that cannot be talked past.
    for where in (*PROTECTED_PATHS, *PROTECTED_FRAGMENTS):
        if any(where in spelling for spelling in spellings):
            block(
                "Blocked by guard rule 'protected-path-write': executed code "
                f"references the protected location {where}."
            )


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    if not isinstance(data, dict):
        return

    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    # A hook that raises on an unexpected payload would break every tool call,
    # not just the guarded ones -- this hook is registered for matcher "*".
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return

    # Where a relative path actually lands, for the file tools and for a
    # shell command alike. Documented as always present
    # for PreToolUse; treated as optional so a malformed payload still returns
    # quietly rather than breaking every tool call under matcher "*".
    cwd = data.get("cwd")
    if not isinstance(cwd, str):
        cwd = None

    if tool == "Bash":
        _check_bash(tool_input, cwd)
    elif tool in PATH_TOOLS:
        _check_path_tool(tool_input, cwd)
    elif tool in CODE_TOOLS:
        _check_code_tool(tool_input)


if __name__ == "__main__":
    main()

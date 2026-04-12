#!/usr/bin/env python3
"""
Git pre-commit code review gate.

Called by ~/.claude/git-hooks/pre-commit. Sends staged diff to a Claude Code
session for review.

Exit codes:
    0 — commit allowed (review passed or skipped)
    1 — commit BLOCKED (critical issues found)

Skip with: git commit --no-verify
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

# --- Configuration ---
GLOBAL_PROMPT = Path.home() / ".claude" / "review_prompt.md"
PROJECT_PROMPT = Path(".claude") / "review_prompt.md"
LOG_DIR = Path.home() / ".claude" / "review-logs"
MAX_DIFF_LINES = 2000
MIN_LINES_TO_REVIEW = 1
TIMEOUT_SECONDS = 1200


def warn(msg: str) -> None:
    """Print warning to stderr."""
    print(f"\033[33m⚠️  [code-review] {msg}\033[0m", file=sys.stderr)


def error(msg: str) -> None:
    """Print error to stderr."""
    print(f"\033[31m❌ [code-review] {msg}\033[0m", file=sys.stderr)


def info(msg: str) -> None:
    """Print info to stderr."""
    print(f"\033[36mℹ️  [code-review] {msg}\033[0m", file=sys.stderr)


def read_file(path: Path | str) -> str:
    """Read file contents, return empty string if not found."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError):
        return ""


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def get_staged_diff() -> tuple[str, str]:
    """Get the staged diff. Returns (diff_text, error_msg)."""
    result = subprocess.run(
        ["git", "diff", "--cached"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "", f"git diff --cached failed (rc={result.returncode}): {result.stderr.strip()}"
    return result.stdout.strip(), ""


def get_staged_files() -> str:
    """Get list of staged file names."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def get_git_status() -> str:
    """Get git status for diagnostics."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "(git status failed)"


def count_changed_lines(diff: str) -> int:
    """Count lines added or removed in the diff (excluding file headers)."""
    return sum(
        1
        for line in diff.split("\n")
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


# ---------------------------------------------------------------------------
# Diff processing
# ---------------------------------------------------------------------------

def check_diff_size(diff: str) -> str | None:
    """Return an error message if the diff exceeds MAX_DIFF_LINES, else None."""
    line_count = len(diff.split("\n"))
    if line_count <= MAX_DIFF_LINES:
        return None
    return (
        f"Diff is {line_count} lines (limit {MAX_DIFF_LINES}). "
        "Split this into smaller commits so each one can be fully reviewed."
    )


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    """Assemble system prompt from review_prompt.md files."""
    parts = []

    global_prompt = read_file(GLOBAL_PROMPT)
    if not global_prompt:
        return ""
    parts.append(global_prompt)

    project_rules = read_file(PROJECT_PROMPT)
    if project_rules:
        parts.append(f"## Project-specific review rules:\n{project_rules}")

    return "\n\n---\n\n".join(parts)


def build_user_prompt(diff: str, files: str, is_merge: bool) -> str:
    """Build the user prompt with diff and file list."""
    parts = []

    if is_merge:
        parts.append(
            "**MERGE CONFLICT RESOLUTION**: This is a merge commit. "
            "The individual commits were already reviewed in the feature branch. "
            "Focus ONLY on how conflicts were resolved — look for incorrect "
            "resolution, lost changes, or logic errors introduced during merge."
        )

    parts.append(f"## Changed files:\n{files}")
    parts.append(f"## Diff to review:\n```diff\n{diff}\n```")
    parts.append(
        "## Your verdict:\n"
        "Analyze the diff using your tools (Read changed files for full context, "
        "Grep for duplicates with synonym strategy, Glob for test files).\n\n"
        "Then output your findings followed by your verdict as the LAST line:\n"
        "- If any CRITICAL issue: last line must be `BLOCK`\n"
        "- If only WARNINGs or no issues: last line must be `OK`"
    )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Review runners
# ---------------------------------------------------------------------------

def _parse_opencode_json(raw: str) -> str:
    """Extract text content from opencode --format json output."""
    import json

    parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "text":
            text = event.get("part", {}).get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts) if parts else ""


def run_opencode(system_prompt: str, user_prompt: str) -> tuple[str, str, int]:
    """Run OpenCode CLI (--pure, no plugins) for review. Returns (stdout, stderr, returncode)."""
    full_prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt

    cmd = [
        "opencode",
        "run",
        "--pure",
        "--model", "github-copilot/gpt-5.4",
        "--format", "json",
        full_prompt,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )

    review = _parse_opencode_json(result.stdout) if result.stdout else ""
    return review, result.stderr.strip(), result.returncode


def run_claude(system_prompt: str, user_prompt: str) -> tuple[str, str, int]:
    """Run Claude Code for review. Returns (stdout, stderr, returncode)."""
    cmd = [
        "claude",
        "-p",
        "--model", "sonnet",
        "--no-session-persistence",
        "--tools", "Read,Grep,Glob",
        "--output-format", "text",
    ]

    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    result = subprocess.run(
        cmd,
        input=user_prompt,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )

    return result.stdout.strip(), result.stderr.strip(), result.returncode


def parse_verdict(review: str) -> str:
    """Extract verdict from the last non-empty line of review."""
    if not review:
        return "OK"
    lines = [line.strip() for line in review.split("\n") if line.strip()]
    if not lines:
        return "OK"
    last = lines[-1].upper()
    if last == "BLOCK":
        return "BLOCK"
    if last == "OK":
        return "OK"
    # If verdict is unclear, don't block
    return "OK"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def save_log(
    verdict: str,
    files: str = "",
    diff: str = "",
    review: str = "",
    error_msg: str | None = None,
    diag: str | None = None,
    reviewer: str | None = None,
) -> None:
    """Save review to a log file for debugging."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        project = Path.cwd().name
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_path = LOG_DIR / f"{timestamp}_{project}_{verdict}.md"

        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# Review: {project} @ {timestamp}\n\n")
            f.write(f"**Verdict:** {verdict}\n")
            if reviewer:
                f.write(f"**Reviewer:** {reviewer}\n")
            if files:
                f.write(f"**Files:**\n{files}\n\n")
            if diff:
                f.write(f"## Diff stats\n{len(diff.splitlines())} lines in diff\n\n")
            if error_msg:
                f.write(f"## Error\n```\n{error_msg}\n```\n\n")
            if diag:
                f.write(f"## Diagnostics\n```\n{diag}\n```\n\n")
            if review:
                f.write(f"## Review output\n```\n{review}\n```\n\n")
            if diff:
                f.write(f"## Full diff\n```diff\n{diff}\n```\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def collect_diff() -> tuple[str, str, bool] | None:
    """Collect staged diff and metadata. Returns (diff, files, is_merge) or None."""
    diff, git_error = get_staged_diff()

    if git_error:
        warn(f"git diff failed: {git_error}")
        save_log("SKIP", error_msg=git_error)
        return None

    if not diff:
        status = get_git_status()
        diag = f"git diff --cached returned empty.\ngit status:\n{status}"
        warn("No staged changes to review.")
        save_log("SKIP", diag=diag)
        return None

    changed = count_changed_lines(diff)
    if changed < MIN_LINES_TO_REVIEW:
        save_log("SKIP", diag=f"only {changed} changed lines (min {MIN_LINES_TO_REVIEW})")
        return None

    too_big = check_diff_size(diff)
    if too_big:
        error(too_big)
        save_log("TOO_BIG", diff=diff, error_msg=too_big)
        sys.exit(1)

    files = get_staged_files()
    is_merge = Path(".git/MERGE_HEAD").is_file()
    return diff, files, is_merge


def _call_reviewer(
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, str, str, int]:
    """Try OpenCode first, fall back to Claude Code.

    Returns (review, stderr, reviewer_name, returncode).
    Raises subprocess.TimeoutExpired or FileNotFoundError when both fail.
    """
    reviewer: str | None = None
    review: str = ""
    reviewer_stderr: str = ""
    returncode: int = -1

    # Primary: OpenCode
    try:
        review, reviewer_stderr, returncode = run_opencode(system_prompt, user_prompt)
        reviewer = "opencode"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        warn(f"OpenCode unavailable ({exc}), falling back to Claude Code")

    # Fallback: Claude Code
    if reviewer is None or returncode != 0 or not review:
        if reviewer == "opencode":
            warn(f"OpenCode failed (rc={returncode}), falling back to Claude Code")
        review, reviewer_stderr, returncode = run_claude(system_prompt, user_prompt)
        reviewer = "claude"

    return review, reviewer_stderr, reviewer, returncode


def run_review(diff: str, files: str, is_merge: bool) -> tuple[str | None, str]:
    """Execute the review. Returns (review_text, verdict)."""
    system_prompt = build_system_prompt()
    if not system_prompt:
        warn("No review_prompt.md found, skipping review")
        save_log("SKIP", files=files, diff=diff, error_msg="no review_prompt.md")
        return None, "SKIP"

    user_prompt = build_user_prompt(diff, files, is_merge)
    info(f"Reviewing {len(files.splitlines())} file(s)...")

    try:
        review, reviewer_stderr, reviewer, returncode = _call_reviewer(system_prompt, user_prompt)
    except subprocess.TimeoutExpired:
        warn(f"Review timed out after {TIMEOUT_SECONDS}s — allowing commit")
        save_log("TIMEOUT", files=files, diff=diff, error_msg="timed out")
        return None, "TIMEOUT"
    except FileNotFoundError:
        warn("Both reviewers unavailable — allowing commit")
        save_log("SKIP", files=files, diff=diff, error_msg="no reviewer available")
        return None, "SKIP"

    if returncode != 0:
        detail = f"{reviewer} exited with code {returncode}\nstderr: {reviewer_stderr}\nstdout: {review}"
        warn(f"{reviewer} failed (rc={returncode}) — allowing commit")
        save_log("ERROR", files=files, diff=diff, error_msg=detail)
        return None, "ERROR"

    if not review:
        warn(f"{reviewer} returned empty output — allowing commit")
        save_log("EMPTY", files=files, diff=diff, reviewer=reviewer,
                 error_msg=f"empty output. stderr: {reviewer_stderr}")
        return None, "EMPTY"

    verdict = parse_verdict(review)
    info(f"Reviewer: {reviewer}")
    save_log(verdict, files=files, diff=diff, review=review, reviewer=reviewer)
    return review, verdict


def main() -> None:
    try:
        context = collect_diff()
        if not context:
            sys.exit(0)

        diff, files, is_merge = context
        review, verdict = run_review(diff, files, is_merge)

        if review is None:
            sys.exit(0)

        if verdict == "BLOCK":
            error(f"Review BLOCKED this commit:\n\n{review}")
            sys.exit(1)

        if review.strip() != "OK":
            warn(f"Review notes:\n{review}")
        sys.exit(0)

    except Exception as exc:
        # Never let a bug in this script block a commit
        warn(f"Review script crashed: {exc} — allowing commit")
        save_log("CRASH", error_msg=f"{type(exc).__name__}: {exc}")
        sys.exit(0)


if __name__ == "__main__":
    main()

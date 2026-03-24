#!/usr/bin/env python3
"""
Claude Code pre-commit quality gate.

Intercepts `git commit` via PreToolUse hook, sends staged diff to a separate
Claude Code session for review.

Exit codes:
    0 — commit allowed (review passed or skipped)
    2 — commit BLOCKED (critical issues found, stderr has details)
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# --- Configuration ---
GLOBAL_PROMPT = Path.home() / ".claude" / "review_prompt.md"
PROJECT_PROMPT = Path(".claude") / "review_prompt.md"
LOG_DIR = Path.home() / ".claude" / "review-logs"
MAX_DIFF_LINES = 2000
MIN_LINES_TO_REVIEW = 3
TIMEOUT_SECONDS = 300

def read_file(path):
    """Read file contents, return empty string if not found."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError):
        return ""


def is_git_commit(cmd):
    """Check if command is a git commit (not merge, rebase, etc.)."""
    # Handle optional env var prefixes: VAR=val VAR2=val2 git commit ...
    return bool(re.match(r"^(\s*\w+=\S+\s+)*git\s+commit\b", cmd))


def get_staged_diff():
    """Get the staged diff."""
    result = subprocess.run(
        ["git", "diff", "--cached"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def get_staged_files():
    """Get list of staged file names."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def count_added_lines(diff):
    """Count lines added in the diff (excluding file headers)."""
    return sum(
        1
        for line in diff.split("\n")
        if line.startswith("+") and not line.startswith("+++")
    )


def truncate_diff(diff):
    """Truncate diff to MAX_DIFF_LINES, keeping complete file sections."""
    lines = diff.split("\n")
    if len(lines) <= MAX_DIFF_LINES:
        return diff

    # Split into per-file chunks
    chunks = []
    current_chunk = []
    for line in lines:
        if line.startswith("diff --git") and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
        current_chunk.append(line)
    if current_chunk:
        chunks.append(current_chunk)

    # Keep chunks that fit, smallest first (review more files)
    chunks.sort(key=len)
    kept = []
    total = 0
    omitted_files = 0
    omitted_lines = 0
    for chunk in chunks:
        if total + len(chunk) <= MAX_DIFF_LINES:
            kept.append(chunk)
            total += len(chunk)
        else:
            omitted_files += 1
            omitted_lines += len(chunk)

    result_lines = []
    for chunk in kept:
        result_lines.extend(chunk)

    if omitted_files:
        result_lines.append(
            f"\n[TRUNCATED: {omitted_files} file(s), {omitted_lines} lines omitted]"
        )

    return "\n".join(result_lines)


def build_system_prompt():
    """Assemble system prompt: review rules + project-specific review rules.

    CLAUDE.md is auto-discovered by claude (no --bare), so we don't include it here.
    """
    parts = []

    # 1. Global review instructions (required)
    global_prompt = read_file(GLOBAL_PROMPT)
    if not global_prompt:
        return ""
    parts.append(global_prompt)

    # 2. Project-specific review rules (optional)
    project_rules = read_file(PROJECT_PROMPT)
    if project_rules:
        parts.append(f"## Project-specific review rules:\n{project_rules}")

    return "\n\n---\n\n".join(parts)


def build_user_prompt(diff, files, is_merge):
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


def run_claude(system_prompt, user_prompt):
    """Run a separate Claude Code session for review."""
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

    return result.stdout.strip()


def parse_verdict(review):
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


def save_log(files, diff, review, verdict, error=None):
    """Save every review to a log file for debugging and analysis."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        project = Path.cwd().name
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_path = LOG_DIR / f"{timestamp}_{project}_{verdict}.md"

        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# Review: {project} @ {timestamp}\n\n")
            f.write(f"**Verdict:** {verdict}\n")
            f.write(f"**Files:**\n{files}\n\n")
            f.write(f"## Diff stats\n{len(diff.splitlines())} lines in diff\n\n")
            if error:
                f.write(f"## Error\n```\n{error}\n```\n\n")
            if review:
                f.write(f"## Review output\n```\n{review}\n```\n\n")
            f.write(f"## Full diff\n```diff\n{diff}\n```\n")
    except OSError:
        pass  # Don't fail the hook if logging fails


def log_skip(reason):
    """Log early exits for debugging."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        project = Path.cwd().name
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_path = LOG_DIR / f"{timestamp}_{project}_SKIP.md"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# Skip: {project} @ {timestamp}\n\n")
            f.write(f"**Reason:** {reason}\n")
    except OSError:
        pass


def parse_hook_input():
    """Parse stdin and return the command string, or None to skip."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return None
    cmd = data.get("tool_input", {}).get("command", "")
    if not is_git_commit(cmd):
        return None
    return cmd


def collect_diff_context():
    """Collect staged diff and file list. Returns (diff, files, is_merge) or None to skip."""
    diff = get_staged_diff()
    if not diff:
        log_skip("empty diff")
        return None
    added = count_added_lines(diff)
    if added < MIN_LINES_TO_REVIEW:
        log_skip(f"only {added} added lines (min {MIN_LINES_TO_REVIEW})")
        return None
    files = get_staged_files()
    is_merge = Path(".git/MERGE_HEAD").is_file()
    return truncate_diff(diff), files, is_merge


def execute_review(diff, files, is_merge):
    """Build prompts and run review. Returns (review_text, verdict)."""
    system_prompt = build_system_prompt()
    if not system_prompt:
        print("⚠️ No review_prompt.md found, skipping review", file=sys.stderr)
        log_skip("no review_prompt.md found")
        return None, "SKIP"

    user_prompt = build_user_prompt(diff, files, is_merge)

    try:
        review = run_claude(system_prompt, user_prompt)
    except subprocess.TimeoutExpired:
        print("⚠️ Code review timed out, allowing commit", file=sys.stderr)
        save_log(files, diff, "", "TIMEOUT", error="Review timed out")
        return None, "TIMEOUT"
    except FileNotFoundError:
        print("⚠️ claude CLI not found, skipping review", file=sys.stderr)
        save_log(files, diff, "", "SKIP", error="claude CLI not found")
        return None, "SKIP"

    verdict = parse_verdict(review)
    save_log(files, diff, review, verdict)
    return review, verdict


def report_and_exit(review, verdict):
    """Print review results to stderr and exit with appropriate code."""
    if verdict == "OK":
        if review and review.strip() != "OK":
            print(f"⚠️ Code review notes:\n{review}", file=sys.stderr)
        sys.exit(0)
    else:
        print(f"❌ Code review BLOCKED commit:\n\n{review}", file=sys.stderr)
        sys.exit(2)


def main():
    if not parse_hook_input():
        sys.exit(0)

    context = collect_diff_context()
    if not context:
        sys.exit(0)

    diff, files, is_merge = context
    review, verdict = execute_review(diff, files, is_merge)

    if review is None:
        sys.exit(0)

    report_and_exit(review, verdict)


if __name__ == "__main__":
    main()

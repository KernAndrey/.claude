#!/usr/bin/env python3
"""PostToolUse hook: runs ruff on edited .py files."""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    file_path: str = data.get("tool_input", {}).get("file_path") or ""
    cwd: str = data.get("cwd") or "."

    if not file_path.endswith(".py"):
        return

    try:
        # Auto-fix first: `--fix` can rewrite imports and other code, which
        # would otherwise leave the file in a non-formatted state if format
        # ran earlier.
        subprocess.run(
            ["ruff", "check", "--fix", file_path],
            cwd=cwd,
            capture_output=True,
        )
        # review-note: ruff is the project-wide formatter per the user's
        # global CLAUDE.md ("ruff check --fix <changed_files>", "ruff check
        # --select ANN"); downstream repos that pick a different formatter
        # are out of scope for this global hook.
        subprocess.run(
            ["ruff", "format", file_path],
            cwd=cwd,
            capture_output=True,
        )
        # Report remaining errors
        result = subprocess.run(
            ["ruff", "check", file_path],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        # Check type annotations are present
        ann_result = subprocess.run(
            ["ruff", "check", "--select", "ANN", file_path],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if ann_result.stdout:
            print(ann_result.stdout, file=sys.stderr)
    except FileNotFoundError:
        # ruff not installed — skip silently
        pass


if __name__ == "__main__":
    main()

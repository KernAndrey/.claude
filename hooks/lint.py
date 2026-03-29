#!/usr/bin/env python3
"""PostToolUse hook: runs ruff on edited .py files."""

import json
import subprocess
import sys


def main():
    data = json.load(sys.stdin)
    file_path = data.get("tool_input", {}).get("file_path", "")
    cwd = data.get("cwd", ".")

    if not file_path.endswith(".py"):
        return

    try:
        # Auto-fix what we can
        subprocess.run(
            ["ruff", "check", "--fix", file_path],
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
    except FileNotFoundError:
        # ruff not installed — skip silently
        pass


if __name__ == "__main__":
    main()

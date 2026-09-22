#!/usr/bin/env bash
# Idempotent setup for the chunked-review pipeline.
#
# What it ensures:
#   1. ~/.config/git/ignore exists and contains `.review/`
#   2. git config --global core.excludesFile ~/.config/git/ignore
#   3. ~/.claude/git-hooks/post-commit is executable (archive .review/)
#
# Re-running is safe: every step checks before writing.

set -euo pipefail

# ----------------------------------------------------------------------------
# 1. Global gitignore
# ----------------------------------------------------------------------------
GLOBAL_IGNORE="${XDG_CONFIG_HOME:-$HOME/.config}/git/ignore"

mkdir -p "$(dirname "$GLOBAL_IGNORE")"

if [ ! -f "$GLOBAL_IGNORE" ]; then
    : > "$GLOBAL_IGNORE"
    echo "created $GLOBAL_IGNORE"
fi

if grep -qxF '.review/' "$GLOBAL_IGNORE"; then
    echo "ok: .review/ already in $GLOBAL_IGNORE"
else
    printf '\n# Chunked-review working directory (artifacts only, never committed)\n.review/\n' \
        >> "$GLOBAL_IGNORE"
    echo "added: .review/ → $GLOBAL_IGNORE"
fi

# ----------------------------------------------------------------------------
# 2. Wire global core.excludesFile
# ----------------------------------------------------------------------------
CURRENT_EXCLUDES="$(git config --global --get core.excludesFile 2>/dev/null || true)"
if [ "$CURRENT_EXCLUDES" = "$GLOBAL_IGNORE" ]; then
    echo "ok: git core.excludesFile already → $GLOBAL_IGNORE"
elif [ -n "$CURRENT_EXCLUDES" ]; then
    echo "warn: git core.excludesFile already set to '$CURRENT_EXCLUDES' (not $GLOBAL_IGNORE)"
    echo "      leaving it alone. If you want chunked-review's .review/ to be ignored,"
    echo "      add a '.review/' line to '$CURRENT_EXCLUDES' yourself."
else
    git config --global core.excludesFile "$GLOBAL_IGNORE"
    echo "set: git core.excludesFile → $GLOBAL_IGNORE"
fi

# ----------------------------------------------------------------------------
# 3. Post-commit hook executable
# ----------------------------------------------------------------------------
POST_COMMIT="$HOME/.claude/git-hooks/post-commit"
if [ ! -f "$POST_COMMIT" ]; then
    echo "warn: $POST_COMMIT not found — chunked archive will not run after commits"
    echo "      (re-deploy ~/.claude/git-hooks/post-commit from the repo)"
else
    if [ ! -x "$POST_COMMIT" ]; then
        chmod +x "$POST_COMMIT"
        echo "set: chmod +x $POST_COMMIT"
    else
        echo "ok: $POST_COMMIT already executable"
    fi
fi

# ----------------------------------------------------------------------------
# 4. Verify the global hooks path is wired up (informational only)
# ----------------------------------------------------------------------------
HOOKS_PATH="$(git config --global --get core.hooksPath 2>/dev/null || true)"
if [ -z "$HOOKS_PATH" ]; then
    echo "warn: git core.hooksPath is not set globally"
    echo "      run: git config --global core.hooksPath ~/.claude/git-hooks"
elif [ "$HOOKS_PATH" != "$HOME/.claude/git-hooks" ]; then
    echo "warn: git core.hooksPath = $HOOKS_PATH (expected $HOME/.claude/git-hooks)"
else
    echo "ok: git core.hooksPath → $HOOKS_PATH"
fi

echo
echo "chunked-review setup complete."

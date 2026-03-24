#!/usr/bin/env bash
set -euo pipefail

command -v notify-send >/dev/null 2>&1 || exit 0
command -v jq >/dev/null 2>&1 || exit 0

INPUT=$(cat)
EVENT=$(echo "$INPUT" | jq -r '.hook_event_name // empty')

case "$EVENT" in
  Notification)
    notify-send --urgency=critical --app-name="Claude Code" \
      "Claude Code" "Waiting for your input"
    ;;
  Stop)
    notify-send --urgency=normal --app-name="Claude Code" \
      "Claude finished" "Task complete"
    ;;
esac

exit 0

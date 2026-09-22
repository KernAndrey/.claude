#!/usr/bin/env bash
# Run the global pre-commit gate against the current index without committing.
#
# Usage: run_gate.sh <out_dir> [msg_file]
#
# Run it from inside the repository. Writes into <out_dir>:
#   pid            this script's pid (written first, for wait_gate.sh)
#   procid         "<pid> <starttime>" — identity wait_gate.sh checks against
#   env.txt        gate env switches inherited from the session
#   notes.txt      only when something about the run needs saying in the report
#   review-backup/ copy of a .review/ that existed before the run — kept for
#                  forensics, never restored: the run legitimately rewrites
#                  .review/ (chunked state) and may scaffold a manifest there,
#                  and a rerun needs that manifest in place
#   gate.log       raw wrapper output; gate.txt is the same without ANSI colours
#   marker         GATE_RC / VERDICT / LOG / LOG_COUNT, written last and atomically
set -uo pipefail

out="$(realpath -m "${1:?usage: run_gate.sh <out_dir> [msg_file]}")"
msg_file="${2:-}"
mkdir -p "$out"

write_marker() {
    printf 'GATE_RC=%s\nVERDICT=%s\nLOG=%s\nLOG_COUNT=%s\n' "$1" "$2" "$3" "$4" > "$out/marker.tmp"
    mv "$out/marker.tmp" "$out/marker"
}

# One run per out_dir. `mkdir` either creates the directory or fails, with no
# window in between, so of two concurrent launches exactly one proceeds and
# pid/gate.log/marker keep a single writer. A loser touches none of them —
# overwriting them is what would make wait_gate.sh report the wrong run.
if ! mkdir "$out/run.lock" 2> /dev/null; then
    printf 'out-dir-reused: %s is already claimed by another run (this pid %s)\n' "$out" "$$" \
        >> "$out/launch-error.txt"
    echo "run_gate.sh: $out already holds a run — launch again with a fresh output directory" >&2
    exit 1
fi
echo "$$" > "$out/pid"

# A pid alone is ambiguous once the process dies and the number is handed out
# again. Pair it with this process's start time — the kernel's own tiebreaker,
# and unlike the command line it does not depend on how the caller spelled the
# arguments. Start time is field 22 of /proc/self/stat, counted past the
# "(comm)" field, which may itself hold spaces or parentheses.
record_procid() {
    local stat rest
    # /proc/$$, not /proc/self: inside a command substitution "self" is the
    # `cat` subprocess, so "self" would record that process's start time and
    # every liveness check against it would fail.
    [ -r "/proc/$$/stat" ] || return 0
    stat="$(cat "/proc/$$/stat")" || return 0
    rest="${stat#*') '}"
    # shellcheck disable=SC2086 # deliberate split: /proc stat fields are space-delimited
    set -- $rest
    # Rename, so the waiter never sees a half-written file: an empty read there
    # is permanent, silently downgrading its check to `kill -0` for the whole
    # run — the very ambiguity this file exists to remove.
    printf '%s %s\n' "$$" "${20:-}" > "$out/procid.tmp"
    mv "$out/procid.tmp" "$out/procid"
}
record_procid

if ! repo_root="$(git rev-parse --show-toplevel 2> "$out/launch-error.txt")"; then
    write_marker launch-error not-a-git-repo none 0
    exit 1
fi
cd "$repo_root" || { write_marker launch-error cd-failed none 0; exit 1; }

# An inherited _CLAUDE_HOOK_RUNNING makes the wrapper exit 0 before doing
# anything, and SDD_REVIEW_FASTPATH lets hook.py skip the LLM review — a manual
# `git commit` has neither.
unset _CLAUDE_HOOK_RUNNING SDD_REVIEW_FASTPATH

# Recorded, not unset: a real commit from the same session inherits them too.
for var in CODE_REVIEW_SKIP_GATE CODE_REVIEW_SKIP_COVERAGE CODE_REVIEW_SKIP_ASSERT \
    CODE_REVIEW_GATE_FAIL_OPEN CODE_REVIEW_COMPARE_BRANCH; do
    printf '%s=%s\n' "$var" "${!var-<unset>}"
done > "$out/env.txt"

# The wrapper re-exports CLAUDE_COMMIT_MSG from COMMIT_EDITMSG whenever that
# file is younger than ~10s, overwriting ours — so a dry run started right
# after a commit would be reviewed against that commit's message. Outwait the
# freshness window instead of touching git's own file.
wait_out_fresh_editmsg() {
    local editmsg tries=0
    editmsg="$(git rev-parse --git-dir)/COMMIT_EDITMSG"
    [ -f "$editmsg" ] || return 0
    while [ -n "$(find "$editmsg" -mmin -0.17 2> /dev/null)" ]; do
        if [ "$tries" -ge 8 ]; then
            echo "COMMIT_EDITMSG kept being rewritten — the review may have used its text, not msg.txt" \
                >> "$out/notes.txt"
            return 0
        fi
        tries=$((tries + 1))
        sleep 2
    done
}

if [ -n "$msg_file" ]; then
    if [ ! -f "$msg_file" ]; then
        echo "commit message file not found: $msg_file" > "$out/launch-error.txt"
        write_marker launch-error msg-file-missing none 0
        exit 1
    fi
    CLAUDE_COMMIT_MSG="$(cat "$msg_file")"
    export CLAUDE_COMMIT_MSG
    wait_out_fresh_editmsg
fi

# A chunked run wipes .review/state and .review/raw, and with no commit the
# post-commit hook never archives the previous run's artifacts.
if [ -d .review ]; then
    cp -r .review "$out/review-backup"
fi

log_dir="$HOME/.claude/review/logs"
list_logs() {
    find "$log_dir" -maxdepth 1 -type f -name '*.md' -printf '%f\n' 2> /dev/null | LC_ALL=C sort
}
list_logs > "$out/logs.before"

bash "$HOME/.claude/git-hooks/pre-commit" > "$out/gate.log" 2>&1
rc=$?

sed 's/\x1b\[[0-9;]*m//g' "$out/gate.log" > "$out/gate.txt"
list_logs > "$out/logs.after"

# hook.py names logs <YYYY-mm-dd_HH-MM-SS>_<cwd basename>_<VERDICT>.md
project="$(basename "$PWD")"
verdict=none
log=none
count=0
while IFS= read -r name; do
    rest="${name#????-??-??_??-??-??_"$project"_}"
    [ "$rest" = "$name" ] && continue
    rest="${rest%.md}"
    if [[ "$rest" =~ ^[A-Z_]+$ ]]; then
        verdict="$rest"
        log="$log_dir/$name"
        count=$((count + 1))
    fi
done < <(LC_ALL=C comm -13 "$out/logs.before" "$out/logs.after")

write_marker "$rc" "$verdict" "$log" "$count"

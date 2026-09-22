#!/usr/bin/env bash
# Wait for run_gate.sh to finish; print its marker, or report that it died.
#
# Usage: wait_gate.sh <out_dir>
# Exit 0: marker printed. Exit 1: the run never started or was killed.
#
# Liveness comes from the identity run_gate.sh recorded, not `pgrep -f
# <out_dir>`: this waiter's own command line contains <out_dir> as well, so a
# pattern match finds itself and a killed run would look like a slow one
# forever.
set -uo pipefail

out="$(realpath -m "${1:?usage: wait_gate.sh <out_dir>}")"
poll="${GATE_POLL_SECONDS:-20}"

# A refused launch leaves the previous run's pid and marker untouched, so
# without this check the waiter would report that older run as this one's.
if [ -f "$out/launch-error.txt" ] && grep -q '^out-dir-reused:' "$out/launch-error.txt"; then
    echo "OUT_DIR_REUSED: $out already held a run — relaunch with a fresh output directory"
    exit 1
fi

for _ in $(seq 60); do
    [ -f "$out/pid" ] && break
    sleep 1
done
if [ ! -f "$out/pid" ]; then
    echo "LAUNCH_FAILED: no pid file in $out after 60s"
    cat "$out/launcher.log" 2> /dev/null
    exit 1
fi

pid="$(cat "$out/pid")"

# run_gate.sh writes procid just after pid; a moment's wait avoids reading the
# gap and falling back to the weaker check for the whole run.
for _ in $(seq 20); do
    [ -f "$out/procid" ] && break
    sleep 0.5
done
want_starttime="$(cut -d' ' -f2 "$out/procid" 2> /dev/null || true)"

# Start time as the kernel reports it now, or empty when procfs is unavailable.
current_starttime() {
    local stat rest
    [ -r "/proc/$1/stat" ] || return 0
    stat="$(cat "/proc/$1/stat" 2> /dev/null)" || return 0
    rest="${stat#*') '}"
    # shellcheck disable=SC2086 # deliberate split: /proc stat fields are space-delimited
    set -- $rest
    printf '%s' "${20:-}"
}

run_alive() {
    kill -0 "$pid" 2> /dev/null || return 1
    # pid + start time is unique: a recycled pid necessarily started later, so
    # this catches an unrelated process wearing the number — including another
    # gate run, which a command-line match would have accepted. Comparing
    # identity rather than argument text also means the caller may spell the
    # output directory however they like.
    [ -n "$want_starttime" ] || return 0 # no procfs: kill -0 is all there is
    [ "$(current_starttime "$pid")" = "$want_starttime" ]
}

while [ ! -f "$out/marker" ]; do
    if ! run_alive; then
        # The run may have written its marker and exited between the two checks.
        [ -f "$out/marker" ] && break
        echo "PROCESS_GONE_WITHOUT_MARKER (pid $pid)"
        tail -n 20 "$out/gate.log" 2> /dev/null
        exit 1
    fi
    sleep "$poll"
done
cat "$out/marker"

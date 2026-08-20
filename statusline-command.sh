#!/usr/bin/env bash
# Status line: user@host:/path | Model | account | 5h: X% | 7d: Y%
# No jq dependency — uses grep/sed to parse JSON
input=$(cat)

PROFILES_DIR="$HOME/.claude-profiles"
CC_SWITCH="$HOME/.claude/cc_switch.py"

# Helper: extract value by key from flat JSON
json_val() {
    echo "$input" | grep -o "\"$1\":\"[^\"]*\"" | head -1 | sed 's/^[^:]*:"//' | sed 's/"$//'
}
json_num() {
    echo "$input" | grep -o "\"$1\":[^,}]*" | head -1 | sed 's/.*://' | tr -d ' '
}

# Helper: extract nested value like rate_limits.five_hour.used_percentage
json_nested() {
    echo "$input" | grep -o "\"$1\":{[^}]*}" | head -1 | grep -o "\"$2\":[^,}]*" | head -1 | sed 's/.*://' | tr -d ' '
}

# Same, for a nested string value (quotes stripped; empty when absent)
json_nested_str() {
    echo "$input" | grep -o "\"$1\":{[^}]*}" | head -1 | grep -o "\"$2\":\"[^\"]*\"" | head -1 |
        sed 's/^[^:]*:"//' | sed 's/"$//'
}

MODEL=$(json_val "display_name" | sed 's/ ([^)]*context)//') # strip "(1M context)"
CTX=$(json_num "used_percentage")  # context window used %
FIVE_H=$(json_nested "five_hour" "used_percentage")
WEEK=$(json_nested "seven_day" "used_percentage")
RESETS_5H=$(json_nested_str "five_hour" "resets_at")
RESETS_7D=$(json_nested_str "seven_day" "resets_at")

ACCOUNT=""
[ -r "$PROFILES_DIR/.active" ] && read -r ACCOUNT < "$PROFILES_DIR/.active"

printf -v NOW '%(%s)T' -1

EXHAUSTED=0
[ -r "$PROFILES_DIR/.exhausted" ] && read -r EXHAUSTED < "$PROFILES_DIR/.exhausted"

# Auto-switch gate. cc-switch writes the trigger point it wants to be woken
# at, so a normal render costs four integer comparisons and spawns nothing.
# Thresholds live in cc_switch.py only — this file never hardcodes them.
if [ -r "$PROFILES_DIR/.auto" ] && [ -n "$FIVE_H" ] && [ -n "$WEEK" ]; then
    # Rounded DOWN only to compare cheaply in bash, which has no floats. The
    # gate is a sieve, never the decision: flooring can only wake a tick
    # early, and cc-switch re-decides on the exact values passed below.
    FIVE_I=${FIVE_H%%.*}
    WEEK_I=${WEEK%%.*}
    SPAWN=0
    if [ -r "$PROFILES_DIR/.gate" ]; then
        read -r NOT_BEFORE RECHECK T5H T7D < "$PROFILES_DIR/.gate"
        if [ "$NOW" -ge "${NOT_BEFORE:-0}" ]; then
            { [ "${RECHECK:-0}" -gt 0 ] && [ "$NOW" -ge "${RECHECK:-0}" ]; } && SPAWN=1
            [ "${FIVE_I:-0}" -ge "${T5H:-95}" ] && SPAWN=1
            [ "${WEEK_I:-0}" -ge "${T7D:-99}" ] && SPAWN=1
        fi
    else
        SPAWN=1 # no gate yet — one tick creates it
    fi
    if [ "$SPAWN" = "1" ]; then
        # Exact percentages, not the floored ones: a fractional threshold
        # must not fire early because bash could not compare it.
        ( "$CC_SWITCH" tick --5h "$FIVE_H" --7d "$WEEK" \
            --resets-5h "$RESETS_5H" --resets-7d "$RESETS_7D" >/dev/null 2>&1 & )
    fi
fi

# PS1-style prefix
PREFIX=$(printf "\033[01;32m%s@%s\033[00m:\033[01;34m%s\033[00m" "$(whoami)" "$(hostname -s)" "$(pwd)")

# Build right part
RIGHT=""
if [ -n "$MODEL" ] && [ -n "$CTX" ]; then
    RIGHT="$MODEL (ctx: $(printf '%.0f' "$CTX")%)"
elif [ -n "$MODEL" ]; then
    RIGHT="$MODEL"
fi
[ -n "$ACCOUNT" ] && RIGHT="${RIGHT:+$RIGHT | }$ACCOUNT"
[ -n "$FIVE_H" ] && RIGHT="${RIGHT:+$RIGHT | }5h: $(printf '%.0f' "$FIVE_H")%"
[ -n "$WEEK" ] && RIGHT="${RIGHT:+$RIGHT | }7d: $(printf '%.0f' "$WEEK")%"
if [ "${EXHAUSTED:-0}" -gt "$NOW" ]; then
    RIGHT="${RIGHT:+$RIGHT | }⛔ all spent until $(date -d "@$EXHAUSTED" '+%H:%M')"
fi

if [ -n "$RIGHT" ]; then
    echo "$PREFIX | $RIGHT"
else
    echo "$PREFIX"
fi

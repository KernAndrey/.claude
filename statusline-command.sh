#!/usr/bin/env bash
# Status line: user@host:/path | Model | account | 5h: X% | 7d: Y%
# No jq dependency — uses grep/sed to parse JSON
input=$(cat)

PROFILES_DIR="$HOME/.claude-profiles"
CC_SWITCH="$HOME/.claude/cc-switch/cc_switch.py"
CREDS_FILE="$HOME/.claude/.credentials.json"
SPAWNED_DEAD=""

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
# `resets_at` is a *number* — Unix epoch seconds — in the statusline payload,
# and an ISO string everywhere the OAuth usage API is the source. The string
# extractor alone matched nothing on every single render, so the active
# account's snapshot recorded a null weekly deadline for months while every
# other account got a real one; nothing could tell a window closing in twelve
# hours from one closing in six days. The numeric extractor alone is no good
# either: its `sed 's/.*://'` is greedy and an ISO timestamp is full of
# colons, so `"resets_at":"2026-08-27T13:59:59Z"` comes out as `59Z`. String
# first, number second, and cc-switch parses whichever arrived.
RESETS_5H=$(json_nested_str "five_hour" "resets_at")
[ -z "$RESETS_5H" ] && RESETS_5H=$(json_nested "five_hour" "resets_at")
RESETS_7D=$(json_nested_str "seven_day" "resets_at")
[ -z "$RESETS_7D" ] && RESETS_7D=$(json_nested "seven_day" "resets_at")

# `.live` holds the account cc-switch last resolved from the real credentials.
# `.active` is only a fallback: it goes stale the moment Claude Code logs into
# a different saved account on its own, and showing that would mislead.
ACCOUNT=""
if [ -r "$PROFILES_DIR/.live" ]; then
    read -r ACCOUNT < "$PROFILES_DIR/.live"
fi
if [ -z "$ACCOUNT" ] && [ -r "$PROFILES_DIR/.active" ]; then
    read -r ACCOUNT < "$PROFILES_DIR/.active"
fi

printf -v NOW '%(%s)T' -1

EXHAUSTED=0
[ -r "$PROFILES_DIR/.exhausted" ] && read -r EXHAUSTED < "$PROFILES_DIR/.exhausted"

# Auto-switch gate. cc-switch writes the trigger point it wants to be woken
# at, so a normal render costs four integer comparisons and spawns nothing.
# Thresholds live in cc_switch.py only — this file never hardcodes them.
# A logged-out session reports no usage at all, so the percentage triggers
# below can never fire. Two things can put us there: the recorded login
# deadline passing, or the credentials file simply going away — the latter
# has no deadline to wait for, so it is checked directly. The settle window
# still applies: without it a dead login with nowhere to go would spawn a
# tick on every single render.
if [ -r "$PROFILES_DIR/.auto" ] && [ -r "$PROFILES_DIR/.gate" ]; then
    read -r _ _ _ _ LOGIN_DEADLINE GATE_SETTLE _REST < "$PROFILES_DIR/.gate"
    # The settle window, not `not_before`: that one also carries the
    # exhaustion deadline, which can be days out, and being at a limit is no
    # reason to stay logged out. An older gate file has no sixth field, which
    # reads as 0 and simply lets the check run.
    if [ "$NOW" -ge "${GATE_SETTLE:-0}" ]; then
        DEAD=0
        # The recorded deadline was written while the credentials were still
        # healthy, so a file corrupted afterwards matches neither "missing"
        # nor "past deadline" — and once Claude stops reporting usage no tick
        # would ever run to correct it. The shell cannot parse JSON, but the
        # one field that decides everything is a plain substring. Read and
        # match are both builtins: no subprocess on an ordinary render.
        if [ ! -r "$CREDS_FILE" ]; then
            DEAD=1
        else
            IFS= read -r -d "" CREDS_BLOB < "$CREDS_FILE" || true
            # Every space, tab and newline goes first. JSON allows any amount
            # of either around a colon, and enumerating the spellings meant
            # `"refreshToken":\n "r"` — a perfectly healthy file — read as
            # dead on every render, spawning a tick each time while Python
            # kept finding it alive and so never armed a back-off. One
            # substitution, no subprocess, and one spelling to match.
            COMPACT=${CREDS_BLOB//[[:space:]]/}
            # A file that never closes its object was truncated or caught
            # mid-write. The substring test below would still find a
            # plausible refreshToken inside it and call the login healthy,
            # and a logged-out session reports no usage, so nothing else
            # would ever spawn the tick that notices. This shell is a
            # trigger, not a parser: where it cannot be sure it wakes
            # cc-switch, which parses properly and costs one process.
            case $COMPACT in
                *'}') ;;
                *) DEAD=1 ;;
            esac
            # The quote and colon matter: `refreshTokenExpiresAt` contains
            # `refreshToken` as a prefix, so a logged-out file holding only
            # the expiry would have looked perfectly alive.
            # Presence is not usability: `null`, `""` and a bare number are
            # all logged-out shapes that a presence check calls alive. The
            # value must open with a quote and hold something.
            case $COMPACT in
                *'"refreshToken":""'*) DEAD=1 ;;
                *'"refreshToken":"'*) ;;
                *) DEAD=1 ;;
            esac
        fi
        { [ "${LOGIN_DEADLINE:-0}" -gt 0 ] && [ "$NOW" -ge "${LOGIN_DEADLINE:-0}" ]; } && DEAD=1
        if [ "$DEAD" = "1" ]; then
            ( "$CC_SWITCH" tick --5h 0 --7d 0 >/dev/null 2>&1 & )
            SPAWNED_DEAD=1
        fi
    fi
fi

# The dead-login branch above may already have started one. A second tick
# would be harmless — the lock serialises them — but it is still a process
# spawned for an answer we are already getting.
if [ -r "$PROFILES_DIR/.auto" ] && [ -z "$SPAWNED_DEAD" ] && [ -n "$FIVE_H" ] && [ -n "$WEEK" ]; then
    # Bash has no floats, so both sides work in tenths of a percent: the gate
    # writes its triggers scaled, and these scale the reported usage the same
    # way. Whole numbers could not express a fractional threshold without
    # either firing constantly or never firing at all. All parameter
    # expansion — no subprocess on an ordinary render.
    FIVE_INT=${FIVE_H%%.*}
    case $FIVE_H in
        *.*) FIVE_FRAC=${FIVE_H#*.}0 ;;
        *) FIVE_FRAC=00 ;;
    esac
    WEEK_INT=${WEEK%%.*}
    case $WEEK in
        *.*) WEEK_FRAC=${WEEK#*.}0 ;;
        *) WEEK_FRAC=00 ;;
    esac
    FIVE_I=$(( ${FIVE_INT:-0} * 10 + ${FIVE_FRAC:0:1} ))
    WEEK_I=$(( ${WEEK_INT:-0} * 10 + ${WEEK_FRAC:0:1} ))
    SPAWN=0
    if [ -r "$PROFILES_DIR/.gate" ]; then
        # Every field is named. `read` puts the unconsumed remainder into the
        # last variable, so a short list silently turned T7D into
        # "47 1789039034" and every comparison against it errored out.
        read -r NOT_BEFORE RECHECK T5H T7D _REST < "$PROFILES_DIR/.gate"
        if [ "$NOW" -ge "${NOT_BEFORE:-0}" ]; then
            { [ "${RECHECK:-0}" -gt 0 ] && [ "$NOW" -ge "${RECHECK:-0}" ]; } && SPAWN=1
            [ "${FIVE_I:-0}" -ge "${T5H:-950}" ] && SPAWN=1
            [ "${WEEK_I:-0}" -ge "${T7D:-990}" ] && SPAWN=1
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

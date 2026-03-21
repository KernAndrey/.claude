#!/usr/bin/env bash
# Status line: user@host:/path | Model | 5h: X% | 7d: Y%
# No jq dependency — uses grep/sed to parse JSON
input=$(cat)

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

MODEL=$(json_val "display_name" | sed 's/ ([^)]*context)//') # strip "(1M context)"
CTX=$(json_num "used_percentage")  # context window used %
FIVE_H=$(json_nested "five_hour" "used_percentage")
WEEK=$(json_nested "seven_day" "used_percentage")

# PS1-style prefix
PREFIX=$(printf "\033[01;32m%s@%s\033[00m:\033[01;34m%s\033[00m" "$(whoami)" "$(hostname -s)" "$(pwd)")

# Build right part
RIGHT=""
if [ -n "$MODEL" ] && [ -n "$CTX" ]; then
    RIGHT="$MODEL (ctx: $(printf '%.0f' "$CTX")%)"
elif [ -n "$MODEL" ]; then
    RIGHT="$MODEL"
fi
[ -n "$FIVE_H" ] && RIGHT="${RIGHT:+$RIGHT | }5h: $(printf '%.0f' "$FIVE_H")%"
[ -n "$WEEK" ] && RIGHT="${RIGHT:+$RIGHT | }7d: $(printf '%.0f' "$WEEK")%"

if [ -n "$RIGHT" ]; then
    echo "$PREFIX | $RIGHT"
else
    echo "$PREFIX"
fi

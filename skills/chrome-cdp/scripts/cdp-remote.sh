#!/usr/bin/env bash
# Wrapper for cdp.mjs to connect to remote Chrome via Tailscale
# Usage: cdp-remote.sh <command> [args...]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDP_REMOTE_CONF="${HOME}/.config/cdp-remote/config"

if [ ! -f "$CDP_REMOTE_CONF" ]; then
    echo "ERROR: Remote CDP not configured. Run cdp-remote-setup.sh first." >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$CDP_REMOTE_CONF"

if [ -z "$CDP_HOST" ] || [ -z "$CDP_PORT" ]; then
    echo "ERROR: CDP_HOST and CDP_PORT must be set in $CDP_REMOTE_CONF" >&2
    exit 1
fi

# Fetch browser websocket URL and create DevToolsActivePort
PORT_FILE="${HOME}/.config/cdp-remote/DevToolsActivePort"

WS_URL=$(curl -s --connect-timeout 5 --max-time 10 "http://${CDP_HOST}:${CDP_PORT}/json/version" 2>/dev/null)
if [ -z "$WS_URL" ]; then
    echo "ERROR: Cannot connect to Chrome at ${CDP_HOST}:${CDP_PORT}" >&2
    echo "" >&2
    echo "Make sure on Windows:" >&2
    echo "  1. Chrome is running with: --remote-debugging-port=${CDP_PORT} --remote-allow-origins=*" >&2
    echo "  2. Port proxy is active: netsh interface portproxy show all" >&2
    exit 1
fi

# Extract websocket path from version response
WS_PATH=$(echo "$WS_URL" | node -e "
    let d=''; process.stdin.on('data',c=>d+=c);
    process.stdin.on('end',()=>{
        try {
            const j=JSON.parse(d);
            const url=new URL(j.webSocketDebuggerUrl.replace('ws://','http://'));
            process.stdout.write(url.pathname);
        } catch(e) { process.exit(1); }
    });
")

if [ -z "$WS_PATH" ]; then
    echo "ERROR: Failed to parse WebSocket URL from Chrome" >&2
    exit 1
fi

printf '%s\n%s\n' "$CDP_PORT" "$WS_PATH" > "$PORT_FILE"

export CDP_PORT_FILE="$PORT_FILE"
export CDP_HOST="$CDP_HOST"

exec node "${SCRIPT_DIR}/cdp.mjs" "$@"

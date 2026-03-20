#!/usr/bin/env bash
# One-time setup for remote Chrome CDP via Tailscale
# Usage: cdp-remote-setup.sh [host] [port]

CDP_HOST="${1:-100.75.193.93}"
CDP_PORT="${2:-9333}"

CONF_DIR="${HOME}/.config/cdp-remote"
mkdir -p "$CONF_DIR"

cat > "${CONF_DIR}/config" <<EOF
CDP_HOST=${CDP_HOST}
CDP_PORT=${CDP_PORT}
EOF

echo "Remote CDP configured:"
echo "  Host: ${CDP_HOST}"
echo "  Port: ${CDP_PORT}"
echo "  Config: ${CONF_DIR}/config"
echo ""
echo "Testing connection..."

RESULT=$(curl -s --connect-timeout 5 --max-time 10 "http://${CDP_HOST}:${CDP_PORT}/json/version" 2>/dev/null)
if [ -n "$RESULT" ]; then
    echo "OK - Connected to Chrome:"
    echo "$RESULT" | head -3
else
    echo "FAILED - Chrome not reachable at ${CDP_HOST}:${CDP_PORT}"
    echo ""
    echo "On Windows (PowerShell as Admin):"
    echo "  1. taskkill /F /IM chrome.exe"
    echo '  2. Start-Process "C:\Program Files\Google\Chrome\Application\chrome.exe" -ArgumentList "--remote-debugging-port='"${CDP_PORT}"'","--remote-allow-origins=*","--user-data-dir=C:\tmp\chrome-debug"'
    echo "  3. netsh interface portproxy add v4tov6 listenaddress=${CDP_HOST} listenport=${CDP_PORT} connectaddress=::1 connectport=${CDP_PORT}"
fi

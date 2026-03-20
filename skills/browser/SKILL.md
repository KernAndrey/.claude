---
name: browser
description: Connect to user's remote Chrome browser via Tailscale and confirm readiness
user_invocable: true
---

# Browser Connect

Connects to user's Chrome on Windows (office-pc) via Tailscale CDP.

## Steps

1. Test connection to remote Chrome:

```bash
RESULT=$(curl -s --connect-timeout 5 --max-time 10 http://100.75.193.93:9333/json/version 2>/dev/null)
if [ -n "$RESULT" ]; then
    echo "CONNECTED"
    echo "$RESULT"
else
    echo "NOT_CONNECTED"
fi
```

2. If CONNECTED — list open tabs:

```bash
~/.claude/skills/chrome-cdp/scripts/cdp-remote.sh list
```

Then report: "Connected to Chrome. N tabs open." and list them.

3. If NOT_CONNECTED — tell the user to run on Windows:

**Start debug Chrome (regular PowerShell):**
```
Start-Process "C:\Program Files\Google\Chrome\Application\chrome.exe" -ArgumentList "--remote-debugging-port=9333","--remote-allow-origins=*","--user-data-dir=C:\tmp\chrome-debug"
```

**Port proxy if needed (Admin PowerShell, one-time until reboot):**
```
netsh interface portproxy add v4tov4 listenaddress=100.75.193.93 listenport=9333 connectaddress=127.0.0.1 connectport=9333
```

Then wait for user to say "ready" and retry step 1.

## Available commands after connection

All via `~/.claude/skills/chrome-cdp/scripts/cdp-remote.sh`:

- `list` — open tabs
- `shot <target> [file]` — screenshot
- `snap <target>` — accessibility tree
- `eval <target> <expr>` — run JS
- `click <target> <selector>` — click element
- `clickxy <target> <x> <y>` — click coordinates
- `type <target> <text>` — type text
- `nav <target> <url>` — navigate
- `html <target> [selector]` — get HTML
- `open [url]` — new tab

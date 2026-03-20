---
name: chrome-cdp
description: Interact with Chrome browser session — local or remote via Tailscale (only on explicit user approval after being asked to inspect, debug, or interact with a page open in Chrome)
---

# Chrome CDP

Lightweight Chrome DevTools Protocol CLI. Connects directly via WebSocket — no Puppeteer, works with 100+ tabs, instant connection.

## Remote Setup (Chrome on Windows via Tailscale)

This environment uses remote Chrome: browser runs on user's Windows machine (office-pc, Tailscale IP 100.75.193.93), Claude runs on Ubuntu.

### First-time setup (already done)

On Ubuntu:
```bash
scripts/cdp-remote-setup.sh 100.75.193.93 9333
```

### Before each session — user must do on Windows (PowerShell)

Tell the user to run these commands:

**Step 1 — Start debug Chrome (regular PowerShell, alongside normal Chrome):**
```powershell
Start-Process "C:\Program Files\Google\Chrome\Application\chrome.exe" -ArgumentList "--remote-debugging-port=9333","--remote-allow-origins=*","--user-data-dir=C:\tmp\chrome-debug"
```
This opens a separate Chrome window with a debug profile. User's main Chrome keeps running normally.

**Step 2 — Port proxy (Admin PowerShell, one-time until reboot):**
```powershell
netsh interface portproxy add v4tov4 listenaddress=100.75.193.93 listenport=9333 connectaddress=127.0.0.1 connectport=9333
```

To check if proxy is already active: `netsh interface portproxy show all`
To remove: `netsh interface portproxy delete v4tov4 listenaddress=100.75.193.93 listenport=9333`

### Verify connection from Ubuntu

```bash
curl -s http://100.75.193.93:9333/json/version
```

### Known issues

- **VS Code occupies ports 9222 and nearby** — always use port 9333
- **Default Chrome profile ignores --remote-debugging-port** — must use `--user-data-dir=C:\tmp\chrome-debug`
- **Chrome in non-headless mode binds to IPv4 (127.0.0.1)** — portproxy uses `v4tov4`. Headless may bind to IPv6 `[::1]` — use `v4tov6` in that case.

## Commands

All commands use `scripts/cdp-remote.sh` (wrapper that auto-configures remote connection).

The `<target>` is a **unique** targetId prefix from `list`; copy the full prefix shown in the `list` output (for example `6BE827FA`). The CLI rejects ambiguous prefixes.

### List open pages

```bash
scripts/cdp-remote.sh list
```

### Take a screenshot

```bash
scripts/cdp-remote.sh shot <target> [file]    # default: screenshot-<target>.png in runtime dir
```

Captures the **viewport only**. Scroll first with `eval` if you need content below the fold. Output includes the page's DPR and coordinate conversion hint (see **Coordinates** below).

### Accessibility tree snapshot

```bash
scripts/cdp-remote.sh snap <target>
```

### Evaluate JavaScript

```bash
scripts/cdp-remote.sh eval <target> <expr>
```

> **Watch out:** avoid index-based selection (`querySelectorAll(...)[i]`) across multiple `eval` calls when the DOM can change between them (e.g. after clicking Ignore, card indices shift). Collect all data in one `eval` or use stable selectors.

### Other commands

```bash
scripts/cdp-remote.sh html    <target> [selector]   # full page or element HTML
scripts/cdp-remote.sh nav     <target> <url>         # navigate and wait for load
scripts/cdp-remote.sh net     <target>               # resource timing entries
scripts/cdp-remote.sh click   <target> <selector>    # click element by CSS selector
scripts/cdp-remote.sh clickxy <target> <x> <y>       # click at CSS pixel coords
scripts/cdp-remote.sh type    <target> <text>         # Input.insertText at current focus; works in cross-origin iframes unlike eval
scripts/cdp-remote.sh loadall <target> <selector> [ms]  # click "load more" until gone (default 1500ms between clicks)
scripts/cdp-remote.sh evalraw <target> <method> [json]  # raw CDP command passthrough
scripts/cdp-remote.sh open    [url]                  # open new tab (each triggers Allow prompt)
scripts/cdp-remote.sh stop    [target]               # stop daemon(s)
```

## Coordinates

`shot` saves an image at native resolution: image pixels = CSS pixels × DPR. CDP Input events (`clickxy` etc.) take **CSS pixels**.

```
CSS px = screenshot image px / DPR
```

`shot` prints the DPR for the current page. Typical Retina (DPR=2): divide screenshot coords by 2.

## Tips

- Prefer `snap --compact` over `html` for page structure.
- Use `type` (not eval) to enter text in cross-origin iframes — `click`/`clickxy` to focus first, then `type`.
- Chrome shows an "Allow debugging" modal once per tab on first access. A background daemon keeps the session alive so subsequent commands need no further approval. Daemons auto-exit after 20 minutes of inactivity.

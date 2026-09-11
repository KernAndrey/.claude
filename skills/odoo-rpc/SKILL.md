---
name: odoo-rpc
description: >
  Query or modify an Odoo database over RPC from the shell. Use when the user
  wants to read Odoo data outside the USKO MCP connector, inspect a model's
  schema or fields before writing a domain, create/update/delete records in a
  dev database, check what an Odoo instance is before touching it, or verify a
  restored dump is neutralized. Triggers: "odoo rpc", "json-rpc", "execute_kw",
  "search_read on <model>", "what fields does <model> have", "create a partner
  in the test db", "is this database neutralized", "connect to the odoo on port
  8569", "which odoo version is running".
---

# Odoo RPC

`odoo-rpc` talks to any Odoo 17/18/19 instance over RPC. Reads run freely.
Writes need two independent things to be true, and the tool checks both.

## Before the first call

Run `odoo-rpc check`. It reports the server version, which transport was chosen
and why, the authenticated user, and the gate verdict:

| Verdict | Meaning | Writes |
|---|---|---|
| `NEUTRALIZED` | `database.is_neutralized` is set and no live surface remains | allowed with `--write` |
| `UNVERIFIED` | nothing live found, but the flag is absent — cannot be proven a safe copy | refused |
| `LIVE` | a live mail server, cron, webhook or payment provider is present | refused |

`check` needs Settings access (`base.group_system`) to read
`ir.config_parameter`. Without it every check reports `SKIPPED` and the verdict
is `UNVERIFIED` — a filtered read must never look like a clean one.

## Commands

Model and method names are passed as they exist in Odoo. Domains and values are
JSON.

| Command | Example |
|---|---|
| `check` | `odoo-rpc check --profile pri` |
| `profiles` | `odoo-rpc profiles` |
| `models` | `odoo-rpc models --like partner` |
| `fields` | `odoo-rpc fields res.partner --attrs string,type,relation,required` |
| `search` | `odoo-rpc search res.partner --domain '[["is_company","=",true]]' --fields name,email` |
| `count` | `odoo-rpc count sale.order --domain '[["state","=","sale"]]'` |
| `read` | `odoo-rpc read res.partner --ids 1,2 --fields name,email` |
| `read-group` | `odoo-rpc read-group sale.order --domain '[]' --fields amount_total:sum --groupby partner_id` |
| `create` | `odoo-rpc create res.partner --values '{"name":"Acme"}' --write` |
| `write` | `odoo-rpc write res.partner --ids 5 --values '{"name":"Acme II"}' --write` |
| `unlink` | `odoo-rpc unlink res.partner --ids 5 --write` |
| `call` | `odoo-rpc call res.partner action_archive --ids 5 --write` |

Shared flags: `--profile`, `--url`, `--db`, `--login`, `--api-key`, `--timeout`
(default 30s), `--compact`, `--traceback`, `--max-bytes` (default 40000).

Start from `models` and `fields` when the schema is unfamiliar — a domain
written against guessed field names fails at the server and costs a round trip.

## The write gate

Two separate questions, answered separately:

1. **Did you mean to write?** `create`, `write` and `unlink` refuse without
   `--write`. So does `call` with any method outside the read allowlist
   (`search`, `search_read`, `search_count`, `read`, `read_group`, `fields_get`,
   `name_search`, `default_get`, `context_get`, `get_views`, …). An unrecognised
   method counts as mutating, so a custom addon method cannot slip through.
2. **Is this database safe to write to?** The gate runs the neutralization
   checks and refuses anything but `NEUTRALIZED`.

Reads are never gated — reading a live database breaks nothing, and gating reads
would train the override into a reflex.

To fix an `UNVERIFIED` database, neutralize it: `odoo-bin neutralize -d <db>`.

`--i-know-this-is-live` lifts the gate for one invocation and only alongside
`--write`. Together they do write to a live database, deliberately.

## Configuration

`.odoo-rpc.toml` in the project root, falling back to
`~/.config/odoo-rpc/config.toml` for instances shared across projects. Both hold
credentials, so a file readable by group or other is refused; keep them `0600`.

```toml
default_profile = "pri"

[profiles.pri]
url   = "http://localhost:8169"
db    = "pri_seed"
login = "admin"
api_key = "..."          # or password = "...", or api_key_env = "PRI_ODOO_KEY"
transport = "auto"       # auto | jsonrpc | json2
```

`--url` and `--db` work with no config file at all, which is the quickest path
to a one-off worktree instance.

## Adding a new project

`odoo-rpc init` writes the file for you. It reads `.wt.toml` → `[odoo].conf` →
that `odoo.conf` for the port and database, creates `.odoo-rpc.toml` at `0600`,
and adds it to `.git/info/exclude`. Fill in `login` and the key, then run
`odoo-rpc check`.

Inside a worktree it computes the port the way `wt` does (`base_port` plus the
position in `git worktree list`). If `.wt.toml` or the `odoo.conf` it names is
missing, `init` stops and says so rather than guessing a port — `wt` itself
silently falls back to port 50005 and `odoo-postgres-1`, and since both exist on
this machine the result is not an error but a different project's database.

## API keys must be global and unexpired

An RPC key has to be created with **no scope**. Odoo checks RPC keys with
`scope='rpc'`, which no key is ever issued under (`res_users.py`: *"'rpc' scope
does not really exist, we basically require a global key (scope NULL)"*), so a
scoped key can never authenticate. Expired keys fail the same way. Both surface
as a bare 403, so if authentication fails with a key that "looks right", check
its scope and expiry first. Generate one at **Settings → My Profile → Account
Security → New API Key**.

## Version notes

The transport is chosen by probing the running server, never by the project
directory — Odoo sources are shared and the version comes from `addons_path`.

- **17, 18** — classic `POST /jsonrpc` with `execute_kw`. Errors arrive inside
  an HTTP 200, so the body decides, not the status.
- **19+** — `POST /json/2/<model>/<method>`, named arguments and real HTTP
  status codes, used when the profile has an API key. Classic RPC still works on
  19 but is scheduled for removal in Odoo 22.
- `read_group` carries `@api.deprecated` on 19, which is `warnings.deprecated`:
  it logs a DeprecationWarning server-side and then runs the method, so
  `read-group` works there today. If a later release removes it, reach
  `web_read_group` through `call --kwargs`.

## Reading USKO data without RPC

For the USKO database specifically, the read-only `Odoo USKO` MCP connector is
already available (`list_models`, `get_schema`, `search_read`, `read`,
`read_group`, `search_count`) and needs no credentials or tunnel URL. Prefer it
for reads there, and use this skill when you need another instance or a write.

Running Odoo, managing its Postgres databases and creating worktrees belong to
the `wt` skill, not here.

## Files in this skill

- `scripts/odoo_rpc.py` — the CLI; also installed as `~/bin/odoo-rpc`
- `scripts/config.py` — profile discovery, permission checks, `init` scaffolding
- `scripts/transport.py` — version probe, both transports, error unwrapping
- `scripts/client.py` — the write guard and the read allowlist
- `scripts/neutralize.py` — the neutralization checks and verdict
- `scripts/test_*.py` — run with `python3 -m pytest scripts/ -q`

---
name: wt
description: Manage git worktrees with client profiles (create, run, list, remove, template)
user_invocable: true
argument_hint: "<command> [--client <name>] [args]"
---

# Worktree Manager

Manages git worktrees with per-client addon paths, template DBs, and port assignment via `~/bin/wt`.

## Context

Gather before acting:

```bash
pwd
git branch --show-current
wt list
```

## Commands

| Command | Description |
|---------|-------------|
| `wt create [--client <name>] <branch>` | Create worktree + DB from template |
| `wt run [--client <name>] <branch>` | Start Odoo (blocking; auto-detects client) |
| `wt remove <branch>` | Delete worktree + DB + branch |
| `wt list` | Show worktrees, DBs, client annotations |
| `wt clients` | List client profiles from `.wt.toml` |
| `wt template [--client <name>] [source_db]` | Create/refresh template DB |

## Execution

1. If `$ARGUMENTS` is empty — run `wt list`.
2. Otherwise run `wt $ARGUMENTS`.
3. After `create` — show path and next steps.
4. After `run` — warn it's blocking. Suggest `nohup wt run <branch> > /tmp/odoo-<branch>.log 2>&1 &`.
5. If `create --client` fails on missing template — guide first-time setup below.

## First-Time Client Template

```bash
# 1. Seed DB with client modules
python3 /home/kern/projects/freight-erp/vendor/odoo/odoo-bin \
  -c docker/odoo-local.conf \
  -d <seed_db> --without-demo=False \
  --addons-path=vendor/odoo/addons,custom-addons,third-party-addons,clients/<client>/addons \
  -i <modules> \
  --db_host localhost --db_port 50010 --db_user odoo --db_password odoo \
  --stop-after-init

# 2. Save as template
wt template --client <client> <seed_db>
```

## Config (`.wt.toml`)

```toml
[clients.<name>]
addons = ["clients/<name>/addons"]
template = "template_<project>_<name>"
base_port = <port>          # required; fallback 10000 if omitted
```

#!/usr/bin/env python3
"""
wt — Worktree + DB manager for Odoo development.

Run from inside any git repository.

Usage:
    wt create [--client <name>] <branch>
    wt run [--client <name>] <branch>
    wt remove <branch>
    wt list
    wt clients
    wt template [--client <name>] [source_db]

Client profiles:
    Define [clients.<name>] sections in .wt.toml to add client-specific
    addon paths and template databases. On create, the client name is saved
    to .wt-client in the worktree and auto-detected on subsequent commands.

Config:
    Place .wt.toml in repo root to override defaults.
    Environment variables (WT_PG_CONTAINER, WT_PG_USER, WT_PG_PASSWORD,
    WT_PG_PORT, WT_BASE_PORT) override both defaults and .wt.toml.

    Example .wt.toml:
        [postgres]
        container = "freight-erp-db"
        user = "odoo"
        password = "odoo"
        port = 50010

        [odoo]
        base_port = 9069
        bin = "vendor/odoo/odoo-bin"       # relative to repo root
        conf = "docker/odoo-local.conf"    # relative to repo root
        python = ".venv/bin/python3"       # relative to repo root
        addons = [                         # resolved: worktree first, then repo
            "vendor/odoo/addons",
            "custom-addons",
            "third-party-addons",
        ]

        [clients.internal-crm]
        addons = ["clients/internal-crm/addons"]
        template = "template_freight-erp-crm"
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # fallback
    except ModuleNotFoundError:
        tomllib = None

# ── Defaults ──────────────────────────────────────────────────
DEFAULTS = {
    "pg_container": "odoo-postgres-1",
    "pg_user": "odoo",
    "pg_password": "password",
    "pg_port": 50005,
    "base_port": 8069,
    "odoo_bin": "odoo-bin",
    "odoo_conf": "",
    "odoo_python": ".venv/bin/python3",
    "odoo_addons": [
        "addons", "odoo/addons", "enterprise",
        "custom-addons", "third-party-addons",
    ],
    "clients": {},
}
# ──────────────────────────────────────────────────────────────

CLIENT_FILE = ".wt-client"


def find_repo():
    """Find git repo root from CWD."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print("❌ Not inside a git repository")
        sys.exit(1)
    return Path(result.stdout.strip())


def load_config(repo: Path) -> dict:
    """Load config: defaults ← .wt.toml ← env vars."""
    cfg = dict(DEFAULTS)
    project = repo.name

    # Layer 2: .wt.toml
    toml_path = repo / ".wt.toml"
    if toml_path.exists():
        if tomllib is None:
            print("⚠️  .wt.toml found but no TOML parser available.")
            print("   Python 3.11+ has built-in tomllib, or: pip install tomli")
        else:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
            pg = data.get("postgres", {})
            odoo = data.get("odoo", {})
            if "container" in pg:
                cfg["pg_container"] = pg["container"]
            if "user" in pg:
                cfg["pg_user"] = pg["user"]
            if "password" in pg:
                cfg["pg_password"] = str(pg["password"])
            if "port" in pg:
                cfg["pg_port"] = int(pg["port"])
            if "base_port" in odoo:
                cfg["base_port"] = int(odoo["base_port"])
            if "bin" in odoo:
                cfg["odoo_bin"] = odoo["bin"]
            if "conf" in odoo:
                cfg["odoo_conf"] = odoo["conf"]
            if "python" in odoo:
                cfg["odoo_python"] = odoo["python"]
            if "addons" in odoo:
                cfg["odoo_addons"] = list(odoo["addons"])

            # Parse client profiles
            clients = {}
            for name, client_data in data.get("clients", {}).items():
                clients[name] = {
                    "addons": client_data.get("addons", []),
                    "template": client_data.get(
                        "template",
                        f"template_{project}_{name.replace('-', '_')}",
                    ),
                    "base_port": client_data.get("base_port"),
                }
            cfg["clients"] = clients

    # Layer 3: environment variables (highest priority)
    env_map = {
        "WT_PG_CONTAINER": "pg_container",
        "WT_PG_USER": "pg_user",
        "WT_PG_PASSWORD": "pg_password",
        "WT_PG_PORT": ("pg_port", int),
        "WT_BASE_PORT": ("base_port", int),
    }
    for env_key, target in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            if isinstance(target, tuple):
                cfg[target[0]] = target[1](val)
            else:
                cfg[target] = val

    return cfg


REPO = find_repo()
PROJECT = REPO.name
WT_BASE = REPO.parent / "worktrees" / PROJECT
TEMPLATE_DB = f"template_{PROJECT}"
CFG = load_config(REPO)


def run(cmd, cwd=None, check=True, capture=False):
    """Run a shell command."""
    return subprocess.run(
        cmd, cwd=cwd, capture_output=capture, text=True, check=check,
    )


def dexec(*args):
    """Execute command inside PostgreSQL Docker container."""
    return run(["docker", "exec", CFG["pg_container"], *args], capture=True, check=False)


def _validate_db_name(name):
    """Validate database name to prevent SQL injection."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        print(f"❌ Invalid database name: {name}")
        sys.exit(1)
    return name


def pg_terminate(db_name_val):
    """Kill active connections to a database."""
    _validate_db_name(db_name_val)
    dexec(
        "psql", "-U", CFG["pg_user"], "-d", "postgres", "-c",
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{db_name_val}';",
    )


def safe_name(branch):
    return branch.replace("/", "_")


def db_name(branch, client=None):
    """Build database name, optionally scoped to a client."""
    safe = safe_name(branch)
    if client:
        return f"dev_{PROJECT}_{client.replace('-', '_')}_{safe}"
    return f"dev_{PROJECT}_{safe}"


def get_template_db(client=None):
    """Get the template DB name for a client (or global default)."""
    if client and client in CFG["clients"]:
        return CFG["clients"][client]["template"]
    return TEMPLATE_DB


DEFAULT_CLIENT_PORT = 10000


def get_base_port(client=None):
    """Get base port, with optional client override."""
    if client and client in CFG["clients"]:
        port = CFG["clients"][client].get("base_port")
        if port is not None:
            return port
        return DEFAULT_CLIENT_PORT
    return CFG["base_port"]


def get_worktree_index(name):
    """Get worktree position in list for port assignment."""
    result = run(["git", "worktree", "list"], cwd=REPO, capture=True)
    for i, line in enumerate(result.stdout.strip().split("\n"), 1):
        parts = line.split()
        if parts and Path(parts[0]).name == name:
            return i
    return 1


def read_wt_client(wt_path):
    """Read client name from .wt-client file in worktree."""
    client_file = wt_path / CLIENT_FILE
    if client_file.exists():
        name = client_file.read_text().strip()
        if name:
            return name
    return None


def write_wt_client(wt_path, client):
    """Write client name to .wt-client file in worktree."""
    client_file = wt_path / CLIENT_FILE
    client_file.write_text(client + "\n")


def validate_client(client):
    """Validate that a client profile exists in config."""
    if client not in CFG["clients"]:
        available = ", ".join(CFG["clients"]) or "(none)"
        print(f"❌ Unknown client profile: {client}")
        print(f"   Available: {available}")
        print(f"   Define [clients.{client}] in .wt.toml")
        sys.exit(1)


def resolve_addon_paths(wt_path, client=None):
    """Resolve addon paths: prefer worktree, fall back to main repo."""
    def _resolve(p):
        wt_resolved = wt_path / p
        if wt_resolved.exists():
            return str(wt_resolved)
        if (REPO / p).exists():
            return str(REPO / p)
        return None

    resolved = [r for p in CFG["odoo_addons"] if (r := _resolve(p))]
    if client and client in CFG["clients"]:
        resolved += [r for p in CFG["clients"][client]["addons"] if (r := _resolve(p))]
    return resolved


def generate_launch_json(wt_path, db, port, client=None):
    """Generate .vscode/launch.json for a worktree."""
    odoo_bin = str(REPO / CFG["odoo_bin"])
    python = str(REPO / CFG["odoo_python"])
    addons = ",".join(resolve_addon_paths(wt_path, client))

    base_args = []
    if CFG["odoo_conf"]:
        base_args += ["-c", str(REPO / CFG["odoo_conf"])]
    base_args += ["-d", db, f"--addons-path={addons}"]

    launch = {
        "version": "0.2.0",
        "configurations": [
            {
                "name": "Odoo: Run odoo-bin",
                "type": "debugpy",
                "request": "launch",
                "program": odoo_bin,
                "python": python,
                "args": [*base_args, "-p", str(port)],
                "console": "integratedTerminal",
            },
            {
                "name": "Odoo: Run odoo-bin with addon update",
                "type": "debugpy",
                "request": "launch",
                "program": odoo_bin,
                "python": python,
                "args": [*base_args, "-u", "MODULE_NAME", "-p", str(port)],
                "console": "integratedTerminal",
            },
            {
                "name": "Odoo Tests",
                "type": "debugpy",
                "request": "launch",
                "program": odoo_bin,
                "python": python,
                "args": [
                    *base_args,
                    "--test-enable", "--stop-after-init",
                    "--http-port", "9099",
                    "-i", "MODULE_NAME",
                ],
                "console": "integratedTerminal",
            },
        ],
    }

    vscode_dir = wt_path / ".vscode"
    vscode_dir.mkdir(exist_ok=True)
    launch_path = vscode_dir / "launch.json"
    with open(launch_path, "w") as f:
        json.dump(launch, f, indent=4)
        f.write("\n")

    print(f"✅ VS Code: {launch_path}")


def extract_flag(args, flag):
    """Extract --flag value from args list. Returns (value, remaining_args)."""
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args):
            value = args[idx + 1]
            remaining = args[:idx] + args[idx + 2:]
            return value, remaining
        print(f"❌ {flag} requires a value")
        sys.exit(1)
    return None, args


# ── Commands ──────────────────────────────────────────────────


def cmd_create(branch, client=None):
    if client:
        validate_client(client)

    safe = safe_name(branch)
    db = db_name(branch, client)
    template = get_template_db(client)
    wt_path = WT_BASE / safe

    WT_BASE.mkdir(parents=True, exist_ok=True)

    # Create worktree
    result = run(
        ["git", "worktree", "add", str(wt_path), "-b", branch],
        cwd=REPO, check=False,
    )
    if result.returncode != 0:
        result = run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=REPO, check=False,
        )
        if result.returncode != 0:
            print("❌ Failed to create worktree")
            sys.exit(1)

    # Save client profile
    if client:
        write_wt_client(wt_path, client)

    # Clone database from template
    pg_terminate(template)
    result = dexec("createdb", "-U", CFG["pg_user"], "-T", template, db)
    if result.returncode != 0:
        print(f"❌ Failed to create database: {result.stderr}")
        hint = f"wt template --client {client}" if client else "wt template"
        print(f"   Did you run '{hint}' first?")
        sys.exit(1)

    # Generate VS Code launch config
    port = get_base_port(client) + get_worktree_index(safe)
    generate_launch_json(wt_path, db, port, client)

    print(f"✅ Worktree: {wt_path}")
    print(f"✅ Database: {db}")
    if client:
        print(f"✅ Client:   {client}")
    print(f"   Port: {port}")
    print()
    print("Next steps:")
    print(f"  cd {wt_path} && claude")
    print(f"  wt run {branch}")


def cmd_run(branch, client=None):
    safe = safe_name(branch)
    wt_path = WT_BASE / safe

    if not wt_path.exists():
        print(f"❌ Worktree not found: {wt_path}")
        sys.exit(1)

    # Auto-detect client from .wt-client (--client flag overrides)
    if client is None:
        client = read_wt_client(wt_path)
    if client:
        validate_client(client)

    db = db_name(branch, client)
    port = get_base_port(client) + get_worktree_index(safe)
    addons = ",".join(resolve_addon_paths(wt_path, client))

    venv_python = REPO / CFG["odoo_python"]
    odoo_bin = REPO / CFG["odoo_bin"]

    client_label = f" ({client})" if client else ""
    print(f"🚀 [{PROJECT}{client_label}] Odoo on http://localhost:{port}  DB: {db}")
    print(f"   Ctrl+C to stop")
    print()

    odoo_args = [
        str(venv_python), str(odoo_bin),
    ]
    if CFG["odoo_conf"]:
        odoo_args += ["-c", str(REPO / CFG["odoo_conf"])]
    odoo_args += [
        "-d", db,
        "--http-port", str(port),
        "--db_host", "localhost",
        "--db_port", str(CFG["pg_port"]),
        "--db_user", CFG["pg_user"],
        "--db_password", CFG["pg_password"],
        f"--addons-path={addons}",
    ]

    # Update modules first
    run([*odoo_args, "-u", "all", "--stop-after-init"], check=False)

    # Run Odoo
    try:
        run(odoo_args, check=False)
    except KeyboardInterrupt:
        print("\n⏹ Stopped")


def cmd_remove(branch):
    safe = safe_name(branch)
    wt_path = WT_BASE / safe

    # Auto-detect client for correct DB name
    client = read_wt_client(wt_path) if wt_path.exists() else None
    db = db_name(branch, client)

    # Drop database
    pg_terminate(db)
    dexec("dropdb", "-U", CFG["pg_user"], "--if-exists", db)

    # Remove worktree
    run(
        ["git", "worktree", "remove", str(wt_path), "--force"],
        cwd=REPO, check=False,
    )

    client_label = f" ({client})" if client else ""
    print(f"🗑️  Removed: {safe}{client_label} + {db}")


def cmd_list():
    # Parse worktree list for client annotations
    result = run(["git", "worktree", "list"], cwd=REPO, capture=True)
    lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

    print(f"=== [{PROJECT}] Worktrees ===")
    for line in lines:
        parts = line.split()
        if parts:
            wt_path = Path(parts[0])
            client = read_wt_client(wt_path)
            suffix = f"  [{client}]" if client else ""
            print(f"  {line}{suffix}")

    # Show databases — include client template DBs
    print(f"\n=== [{PROJECT}] Databases ===")
    _validate_db_name(TEMPLATE_DB)
    like_clauses = [f"datname LIKE 'dev_{PROJECT}_%'", f"datname = '{TEMPLATE_DB}'"]
    for client_cfg in CFG["clients"].values():
        tpl = client_cfg['template']
        _validate_db_name(tpl)
        like_clauses.append(f"datname = '{tpl}'")
    where = " OR ".join(like_clauses)

    result = dexec(
        "psql", "-U", CFG["pg_user"], "-d", "postgres", "-c",
        f"SELECT datname, pg_database_size(datname)/1024/1024 as mb "
        f"FROM pg_database "
        f"WHERE {where} "
        f"ORDER BY datname;",
    )
    if result.returncode == 0:
        print(result.stdout)
    else:
        print("  (container not running?)")

    print(f"\n=== Config ===")
    print(f"  Container: {CFG['pg_container']}")
    print(f"  PG port:   {CFG['pg_port']}")
    print(f"  PG user:   {CFG['pg_user']}")
    print(f"  Base port: {CFG['base_port']}")
    toml_path = REPO / ".wt.toml"
    print(f"  Config:    {toml_path}" if toml_path.exists() else "  Config:    (defaults)")

    if CFG["clients"]:
        print(f"\n=== Client Profiles ===")
        for name, ccfg in CFG["clients"].items():
            port_info = ccfg.get("base_port") or "(global)"
            print(f"  {name}: template={ccfg['template']}  port={port_info}")


def cmd_clients():
    """List available client profiles from .wt.toml."""
    clients = CFG.get("clients", {})
    if not clients:
        print(f"No client profiles defined in .wt.toml")
        print(f"Add [clients.<name>] sections to configure.")
        return

    print(f"=== [{PROJECT}] Client Profiles ===")
    for name, ccfg in clients.items():
        port = ccfg.get("base_port")
        print(f"\n  {name}")
        print(f"    Addons:   {', '.join(ccfg['addons'])}")
        print(f"    Template: {ccfg['template']}")
        print(f"    Port:     {port or '(global default)'}")


def cmd_template(source_db="postgres", client=None):
    if client:
        validate_client(client)

    template = get_template_db(client)
    client_label = f" ({client})" if client else ""

    print(f"[{PROJECT}{client_label}] Creating template from {source_db}...")

    pg_terminate(source_db)
    dexec("dropdb", "-U", CFG["pg_user"], "--if-exists", template)
    result = dexec("createdb", "-U", CFG["pg_user"], "-T", source_db, template)

    if result.returncode != 0:
        print(f"❌ Failed: {result.stderr}")
        sys.exit(1)

    print(f"✅ Template '{template}' refreshed from {source_db}")


# ── CLI ───────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print(f"  Detected repo: {REPO}")
        print(f"  Project:       {PROJECT}")
        print(f"  Worktrees:     {WT_BASE}")
        print(f"  Template DB:   {TEMPLATE_DB}")
        print(f"  Container:     {CFG['pg_container']}")
        print(f"  PG port:       {CFG['pg_port']}")
        if CFG["clients"]:
            print(f"  Clients:       {', '.join(CFG['clients'])}")
        sys.exit(0)

    args = sys.argv[1:]
    client, args = extract_flag(args, "--client")

    if not args:
        print("❌ No command specified")
        sys.exit(1)

    cmd = args[0]
    rest = args[1:]

    if cmd == "create":
        if not rest:
            print("Usage: wt create [--client <name>] <branch>")
            sys.exit(1)
        cmd_create(rest[0], client)

    elif cmd == "run":
        if not rest:
            print("Usage: wt run [--client <name>] <branch>")
            sys.exit(1)
        cmd_run(rest[0], client)

    elif cmd == "remove":
        if not rest:
            print("Usage: wt remove <branch>")
            sys.exit(1)
        cmd_remove(rest[0])

    elif cmd == "list":
        cmd_list()

    elif cmd == "clients":
        cmd_clients()

    elif cmd == "template":
        source = rest[0] if rest else "postgres"
        cmd_template(source, client)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

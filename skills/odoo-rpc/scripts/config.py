#!/usr/bin/env python3
"""Profile discovery, validation and scaffolding for the odoo-rpc skill.

A profile names one Odoo instance: where it lives, which database to talk to,
and how to authenticate. Profiles are resolved in this order:

    1. .odoo-rpc.toml, searched from the start directory upward to the git root
    2. ~/.config/odoo-rpc/config.toml

Config files hold credentials, so one readable by group or other is refused.

Usage:
    from config import load_profile, list_profiles, init_project_config
"""

from __future__ import annotations

import configparser
import os
import re
import stat
import subprocess
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

PROJECT_CONFIG_NAME = ".odoo-rpc.toml"
GLOBAL_CONFIG_PATH = Path.home() / ".config" / "odoo-rpc" / "config.toml"
WT_CONFIG_NAME = ".wt.toml"
WT_CLIENT_FILE = ".wt-client"
VALID_TRANSPORTS = ("auto", "jsonrpc", "json2")
# wt's own default when a worktree names a client that declares no base_port.
DEFAULT_CLIENT_PORT = 10000


class ConfigError(Exception):
    """Raised when configuration is missing, malformed or unsafe to read."""


@dataclass(frozen=True)
class Profile:
    """One resolved Odoo connection."""

    name: str
    url: str
    db: str | None = None
    login: str | None = None
    secret: str | None = None
    secret_kind: str = "none"
    transport: str = "auto"
    source: Path | None = None

    def redacted(self) -> dict[str, object]:
        """Describe the profile without exposing the credential."""
        return {
            "name": self.name,
            "url": self.url,
            "db": self.db,
            "login": self.login,
            "secret_kind": self.secret_kind,
            "transport": self.transport,
            "source": str(self.source) if self.source else None,
        }


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Run a git command, returning stripped stdout or None when it fails."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def find_git_root(start: Path) -> Path | None:
    """Return the git root containing start, or None outside a repository."""
    output = _run_git(["rev-parse", "--show-toplevel"], start)
    return Path(output) if output else None


def find_main_checkout(start: Path) -> Path | None:
    """Return the main checkout of the repository, which owns .wt.toml.

    `git worktree list` prints the main checkout first; linked worktrees follow.
    """
    output = _run_git(["worktree", "list"], start)
    if not output:
        return None
    first = output.splitlines()[0].split()
    return Path(first[0]) if first else None


def check_permissions(path: Path) -> None:
    """Refuse a credential file that group or other can read."""
    mode = path.stat().st_mode
    if mode & 0o077:
        raise ConfigError(
            f"{path} is readable by group or other (mode {stat.S_IMODE(mode):04o}) "
            f"and it holds credentials.\nFix it with: chmod 600 {path}"
        )


def load_toml(path: Path, holds_credentials: bool = True) -> dict[str, object]:
    """Read a TOML config file.

    The permission check applies only to files this tool owns, which carry API
    keys. Borrowed files such as wt's .wt.toml hold ports and paths, ship at 0664
    in every project, and are none of our business to police.
    """
    if holds_credentials:
        check_permissions(path)
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def find_project_config(start: Path) -> Path | None:
    """Search for .odoo-rpc.toml from start upward, stopping at the git root."""
    start = start.resolve()
    root = find_git_root(start)
    current = start
    while True:
        candidate = current / PROJECT_CONFIG_NAME
        if candidate.is_file():
            return candidate
        if root is not None and current == root:
            return None
        if current.parent == current:
            return None
        current = current.parent


def _config_sources(start: Path) -> list[Path]:
    """Return existing config files in precedence order: project, then global."""
    sources: list[Path] = []
    project = find_project_config(start)
    if project is not None:
        sources.append(project)
    if GLOBAL_CONFIG_PATH.is_file():
        sources.append(GLOBAL_CONFIG_PATH)
    return sources


def _profiles_of(data: dict[str, object]) -> dict[str, dict[str, object]]:
    """Extract the [profiles.*] table from parsed TOML."""
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("'profiles' must be a table of named profiles")
    return {k: v for k, v in profiles.items() if isinstance(v, dict)}


def list_profiles(start: Path) -> list[tuple[str, Path]]:
    """List every resolvable profile name with the file that defines it."""
    seen: dict[str, Path] = {}
    for source in _config_sources(start):
        for name in _profiles_of(load_toml(source)):
            seen.setdefault(name, source)
    return sorted(seen.items())


def _resolve_secret(raw: dict[str, object], source: Path,
                    override: bool = False) -> tuple[str | None, str]:
    """Pick the credential from a profile table, preferring an API key.

    An api_key_env indirection keeps the secret out of the file entirely.

    `override` says the caller passed --api-key, which replaces whatever this
    resolves. An unset environment variable is then not an error to report but a
    value about to be discarded, so raising here would defeat the one flag that
    could rescue the run.
    """
    env_name = raw.get("api_key_env")
    if isinstance(env_name, str):
        value = os.environ.get(env_name)
        if not value and override:
            return None, "none"
        if not value:
            raise ConfigError(
                f"{source} sets api_key_env = \"{env_name}\" but that variable is "
                f"empty or unset."
            )
        return value, "api_key"
    api_key = raw.get("api_key")
    if isinstance(api_key, str) and api_key:
        return api_key, "api_key"
    password = raw.get("password")
    if isinstance(password, str) and password:
        return password, "password"
    return None, "none"


def _build_profile(name: str, raw: dict[str, object], source: Path,
                   override: bool = False) -> Profile:
    """Turn a parsed [profiles.<name>] table into a Profile."""
    url = raw.get("url")
    if not isinstance(url, str) or not url:
        raise ConfigError(f"profile '{name}' in {source} is missing a 'url'")
    transport = raw.get("transport", "auto")
    if transport not in VALID_TRANSPORTS:
        raise ConfigError(
            f"profile '{name}' in {source} has transport = {transport!r}; "
            f"expected one of {', '.join(VALID_TRANSPORTS)}"
        )
    secret, secret_kind = _resolve_secret(raw, source, override)
    db = raw.get("db")
    login = raw.get("login")
    return Profile(
        name=name,
        url=url.rstrip("/"),
        db=db if isinstance(db, str) else None,
        login=login if isinstance(login, str) else None,
        secret=secret,
        secret_kind=secret_kind,
        transport=str(transport),
        source=source,
    )


def _default_profile_name(sources: list[Path]) -> str | None:
    """Find the default profile: an explicit key, or a lone profile."""
    for source in sources:
        data = load_toml(source)
        default = data.get("default_profile")
        if isinstance(default, str):
            return default
        profiles = _profiles_of(data)
        if len(profiles) == 1:
            return next(iter(profiles))
    return None


def load_profile(
    name: str | None = None,
    start: Path | None = None,
    url: str | None = None,
    db: str | None = None,
    login: str | None = None,
    api_key: str | None = None,
) -> Profile:
    """Resolve a profile, applying any command-line overrides on top.

    A --url with no config file at all yields an ad-hoc profile, which is the
    practical path for a one-off worktree instance.
    """
    start = (start or Path.cwd()).resolve()
    sources = _config_sources(start)
    target = name or _default_profile_name(sources)

    profile: Profile | None = None
    if target is not None:
        for source in sources:
            raw = _profiles_of(load_toml(source)).get(target)
            if raw is not None:
                profile = _build_profile(target, raw, source,
                                         override=api_key is not None)
                break
        if profile is None and name is not None:
            known = ", ".join(n for n, _ in list_profiles(start)) or "none"
            raise ConfigError(
                f"profile '{name}' is not defined in any config file.\n"
                f"Known profiles: {known}\n"
                f"Searched: {', '.join(str(s) for s in sources) or 'no config files found'}"
            )

    if profile is None:
        if url is None:
            raise ConfigError(
                "no profile found and no --url given.\n"
                f"Run 'odoo-rpc init' in the project, write {PROJECT_CONFIG_NAME}, "
                f"or pass --url explicitly."
            )
        profile = Profile(name="<ad-hoc>", url=url.rstrip("/"))

    if url is not None:
        profile = replace(profile, url=url.rstrip("/"))
    if not profile.url:
        raise ConfigError(
            "the resolved URL is empty; pass --url http://host:port or set 'url' "
            "in the profile"
        )
    if db is not None:
        profile = replace(profile, db=db)
    if login is not None:
        profile = replace(profile, login=login)
    if api_key is not None:
        profile = replace(profile, secret=api_key, secret_kind="api_key")
    return profile


def _read_odoo_conf(path: Path) -> tuple[int | None, str | None]:
    """Pull http_port and db_name out of an odoo.conf."""
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error as exc:
        raise ConfigError(f"{path} is not a readable odoo.conf: {exc}") from exc
    if not parser.has_section("options"):
        return None, None
    port_raw = parser.get("options", "http_port", fallback=None)
    db = parser.get("options", "db_name", fallback=None)
    port = int(port_raw) if port_raw and port_raw.isdigit() else None
    return port, (db or None)


def _worktree_index(repo: Path, main: Path) -> int:
    """Reproduce wt.py's port offset: 1-based position in `git worktree list`.

    Matches resolved absolute paths, not directory names. Two worktrees of
    different repositories — or two of the same one under different parents —
    can share a final path segment, and picking the first namesake derives a port
    that belongs to whichever instance happened to be listed first.

    Refuses rather than guessing. Position 1 is the main checkout, so falling
    back to it would hand out `base_port + 1` — a port that belongs to a real
    other worktree, which is the cross-instance write this module exists to
    prevent.
    """
    output = _run_git(["worktree", "list", "--porcelain"], main)
    if not output:
        raise ConfigError(
            f"`git worktree list` produced nothing in {main}, so this worktree's "
            f"port cannot be determined.\nPass --url explicitly."
        )
    target = repo.resolve()
    index = 0
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        index += 1
        if Path(line[len("worktree ") :].strip()).resolve() == target:
            return index
    raise ConfigError(
        f"{repo} does not appear in `git worktree list`, so its port cannot be "
        f"determined.\nPass --url explicitly."
    )


def _wt_settings(main: Path) -> dict[str, object]:
    """Read the [odoo] table from the main checkout's .wt.toml."""
    return _wt_tables(main)[0]


def _wt_tables(main: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Read the [odoo] and [clients.*] tables from the main checkout's .wt.toml."""
    wt_path = main / WT_CONFIG_NAME
    if not wt_path.is_file():
        raise ConfigError(
            f"{wt_path} not found, so the port and database cannot be derived.\n"
            f"Write {PROJECT_CONFIG_NAME} by hand, or pass --url and --db."
        )
    data = load_toml(wt_path, holds_credentials=False)
    odoo = data.get("odoo")
    if not isinstance(odoo, dict):
        raise ConfigError(f"{wt_path} has no [odoo] section")
    clients = data.get("clients")
    return odoo, clients if isinstance(clients, dict) else {}


def _resolve_base_port(odoo: dict[str, object], clients: dict[str, object],
                       client: str) -> int:
    """Reproduce wt's base-port resolution, including both overrides.

    wt reads a client's own `base_port` when the worktree names one, falls back
    to its client default when that client declares none, and lets WT_BASE_PORT
    replace the global port. Following only [odoo].base_port would compute a port
    that belongs to a different instance — and the whole point of deriving rather
    than defaulting is that a wrong port here is not an error but someone else's
    database.
    """
    if client and client not in clients:
        known = ", ".join(sorted(clients)) or "(none)"
        raise ConfigError(
            f"this worktree's {WT_CLIENT_FILE} names client '{client}', which "
            f"{WT_CONFIG_NAME} does not define.\nKnown clients: {known}\n"
            f"`wt` refuses an unknown client too; fix the file or pass --url."
        )
    if client:
        entry = clients[client]
        port = entry.get("base_port") if isinstance(entry, dict) else None
        if isinstance(port, int):
            return port
        return DEFAULT_CLIENT_PORT
    env_port = os.environ.get("WT_BASE_PORT")
    if env_port and env_port.lstrip("-").isdigit():
        return int(env_port)
    base_port = odoo.get("base_port")
    if not isinstance(base_port, int):
        raise ConfigError(
            "[odoo].base_port is missing, which is required to compute this "
            "worktree's port."
        )
    return base_port


def _derive_worktree(repo: Path, main: Path, odoo: dict[str, object],
                     clients: dict[str, object] | None = None) -> tuple[str, str | None, str]:
    """Port and database for a linked worktree, computed the way wt computes them."""
    client_file = repo / WT_CLIENT_FILE
    client = client_file.read_text().strip() if client_file.is_file() else ""
    base_port = _resolve_base_port(odoo, clients or {}, client)
    index = _worktree_index(repo, main)
    # A detached checkout answers "HEAD", which every detached worktree shares —
    # they would all derive one database name and write over each other. wt names
    # the worktree directory after the branch it created, so the directory is the
    # faithful fallback.
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    if not branch or branch == "HEAD":
        branch = repo.name
    safe = branch.replace("/", "_")
    db = (
        f"dev_{main.name}_{client.replace('-', '_')}_{safe}"
        if client
        else f"dev_{main.name}_{safe}"
    )
    source = f"client '{client}' base_port" if client and client in (clients or {}) \
        else "[odoo].base_port"
    why = f"worktree: {source} {base_port} + position {index}"
    return f"http://localhost:{base_port + index}", db, why


def _derive_main(main: Path, odoo: dict[str, object]) -> tuple[str, str | None, str]:
    """Port and database for the main checkout, read from its odoo.conf."""
    conf_value = odoo.get("conf")
    if not isinstance(conf_value, str):
        raise ConfigError(f"{main / WT_CONFIG_NAME} has no [odoo].conf entry")
    conf_path = Path(conf_value)
    if not conf_path.is_absolute():
        conf_path = main / conf_path
    if not conf_path.is_file():
        raise ConfigError(
            f"{main / WT_CONFIG_NAME} points [odoo].conf at {conf_path}, which does "
            f"not exist."
        )
    port, db = _read_odoo_conf(conf_path)
    if port is not None:
        return f"http://localhost:{port}", db, f"main checkout: http_port from {conf_path}"

    # .wt.toml often names the worktree config, which carries no http_port because
    # wt passes the port on the command line. The main instance's port lives in a
    # sibling odoo.conf. Read it rather than inventing a number.
    sibling = conf_path.parent / "odoo.conf"
    if sibling != conf_path and sibling.is_file():
        port, db = _read_odoo_conf(sibling)
        if port is not None:
            return f"http://localhost:{port}", db, f"main checkout: http_port from {sibling}"
    raise ConfigError(
        f"no http_port found under [options] in {conf_path}"
        + (f" or {sibling}" if sibling != conf_path else "")
        + ".\nSet url by hand in .odoo-rpc.toml, or pass --url."
    )


def derive_connection(start: Path) -> tuple[str, str | None, str]:
    """Derive (url, db, explanation) for the project containing start.

    Fails loudly rather than defaulting: wt silently falls back to port 50005 and
    container odoo-postgres-1 when called from a worktree, and because both exist
    on this machine the result is not an error but someone else's database. A tool
    that can write must not inherit that.
    """
    repo = find_git_root(start)
    if repo is None:
        raise ConfigError(f"{start} is not inside a git repository")
    main = find_main_checkout(start) or repo
    odoo, clients = _wt_tables(main)
    if repo.resolve() != main.resolve():
        return _derive_worktree(repo, main, odoo, clients)
    return _derive_main(main, odoo)


def _git_exclude(repo: Path, entry: str) -> bool:
    """Add entry to .git/info/exclude, matching how .wt.toml is kept out of git."""
    git_dir = _run_git(["rev-parse", "--git-common-dir"], repo)
    if not git_dir:
        return False
    exclude = Path(git_dir)
    if not exclude.is_absolute():
        exclude = repo / exclude
    exclude = exclude / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.is_file() else ""
    if entry in existing.split():
        return False
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    with exclude.open("a") as handle:
        handle.write(f"{prefix}{entry}\n")
    return True


def _profile_name_for(repo_name: str) -> str:
    """Turn a directory name into a TOML bare key that is also a usable profile.

    A dot would open a nested table — `[profiles..claude]` is a parse error and
    `[profiles.a.b]` resolves to something `--profile a.b` never finds — so every
    character outside the bare-key set becomes a hyphen. This repository is
    itself named `.claude`, which is exactly the case that breaks.
    """
    cleaned = re.sub(r"[^a-z0-9-]+", "-", repo_name.lower().replace("_", "-"))
    cleaned = cleaned.strip("-")
    return cleaned or "default"


def init_project_config(start: Path, force: bool = False) -> tuple[Path, str]:
    """Scaffold .odoo-rpc.toml for the project containing start."""
    repo = find_git_root(start)
    if repo is None:
        raise ConfigError(f"{start} is not inside a git repository")
    target = repo / PROJECT_CONFIG_NAME
    if target.exists() and not force:
        raise ConfigError(f"{target} already exists; pass --force to overwrite it")

    url, db, why = derive_connection(start)
    name = _profile_name_for(repo.name)
    db_line = f'db    = "{db}"' if db else '# db  = "<database>"   # not found in odoo.conf'
    content = f"""# odoo-rpc profile for {repo.name}
# Derived from .wt.toml ({why}).
# Kept out of git via .git/info/exclude, like .wt.toml itself.

default_profile = "{name}"

[profiles.{name}]
url   = "{url}"
{db_line}
login = "admin"

# An RPC API key must be GLOBAL (created with no scope) and unexpired, otherwise
# authentication fails with a bare 403. Settings -> My Profile -> Account
# Security -> New API Key.
# api_key = "..."
# Or keep the secret out of this file entirely:
# api_key_env = "{name.upper().replace('-', '_')}_ODOO_KEY"
# Or fall back to a password (classic /jsonrpc only):
# password = "..."

transport = "auto"   # auto | jsonrpc | json2
"""
    target.write_text(content)
    target.chmod(0o600)
    _git_exclude(repo, f"/{PROJECT_CONFIG_NAME}")
    return target, why

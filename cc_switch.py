#!/usr/bin/env python3
"""cc-switch — switch Claude Code OAuth accounts.

Swaps only the credentials (`~/.claude/.credentials.json`) and the
`oauthAccount` field inside `~/.claude.json`. Nothing else under `~/.claude/`
is touched.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CREDS_FILE = CLAUDE_DIR / ".credentials.json"
MAIN_FILE = HOME / ".claude.json"
PROFILES_DIR = HOME / ".claude-profiles"
ACTIVE_FILE = PROFILES_DIR / ".active"
BACKUPS_DIR = PROFILES_DIR / ".backups"
BACKUP_KEEP = 5

EXIT_OK = 0
EXIT_USER = 1
EXIT_SYS = 2


def die(msg: str, code: int = EXIT_SYS) -> NoReturn:
    print(f"cc-switch: {msg}", file=sys.stderr)
    sys.exit(code)


def ensure_dirs() -> None:
    try:
        PROFILES_DIR.mkdir(mode=0o700, exist_ok=True)
        BACKUPS_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError as e:
        die(f"failed to create {PROFILES_DIR}: {e}")


def detect_indent(raw: str) -> int:
    for line in raw.split("\n")[1:]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return 2


def _parse_json_object(path: Path) -> tuple[dict[str, Any], str, bool]:
    """Read and parse a JSON-object file.

    Raises FileNotFoundError, OSError, json.JSONDecodeError, or TypeError
    (on non-object JSON) — callers wrap these with command-specific messages.
    """
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("expected a JSON object")
    return data, raw, raw.endswith("\n")


def read_json_file(path: Path) -> tuple[dict[str, Any], str, bool]:
    try:
        return _parse_json_object(path)
    except FileNotFoundError:
        die(f"file not found: {path}")
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {path}: {e}")
    except TypeError:
        die(f"expected a JSON object in {path}")
    except OSError as e:
        die(f"failed to read {path}: {e}")


def _open_write_0600(path: Path) -> io.TextIOWrapper:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def atomic_write_json(
    path: Path,
    data: dict[str, Any],
    indent: int,
    trailing_nl: bool,
) -> None:
    tmp = path.parent / (path.name + ".tmp")
    try:
        with _open_write_0600(tmp) as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            if trailing_nl:
                f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        die(f"failed to write {path}: {e}")


def profile_path(name: str) -> Path:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        die(f"invalid profile name: {name!r}", EXIT_USER)
    return PROFILES_DIR / f"{name}.json"


def confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def read_profile(name: str) -> dict[str, Any]:
    path = profile_path(name)
    if not path.exists():
        die(f"profile not found: {name}", EXIT_USER)
    try:
        data, _, _ = _parse_json_object(path)
    except json.JSONDecodeError as e:
        die(f"corrupted profile {name}: {e}")
    except TypeError:
        die(f"corrupted profile {name}: expected a JSON object")
    except OSError as e:
        die(f"failed to read {path}: {e}")
    return data


def save_profile(name: str, creds: dict[str, Any], oauth_account: dict[str, Any]) -> None:
    path = profile_path(name)
    now = _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload: dict[str, Any] = {"credentials": creds, "oauthAccount": oauth_account, "savedAt": now}
    atomic_write_json(path, payload, indent=2, trailing_nl=True)


def read_active() -> str | None:
    if not ACTIVE_FILE.exists():
        return None
    try:
        value = ACTIVE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def write_active(name: str) -> None:
    tmp = ACTIVE_FILE.parent / (ACTIVE_FILE.name + ".tmp")
    try:
        with _open_write_0600(tmp) as f:
            f.write(name + "\n")
        os.replace(tmp, ACTIVE_FILE)
    except OSError as e:
        die(f"failed to write {ACTIVE_FILE}: {e}")


def rotate_backups() -> None:
    try:
        backups = sorted(p for p in BACKUPS_DIR.iterdir() if p.is_file() and p.name.startswith("claude.json."))
    except OSError:
        return
    while len(backups) > BACKUP_KEEP:
        victim = backups.pop(0)
        try:
            victim.unlink()
        except OSError:
            pass


def backup_main() -> None:
    if not MAIN_FILE.exists():
        return
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = BACKUPS_DIR / f"claude.json.{ts}"
    try:
        shutil.copy2(MAIN_FILE, dest)
        os.chmod(dest, 0o600)
    except OSError as e:
        die(f"failed to create backup {dest}: {e}")
    rotate_backups()


def read_current_state(strict: bool = True) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Read the currently active credentials and oauthAccount.

    When strict=False, returns None instead of exiting when data is missing.
    """
    if not CREDS_FILE.exists():
        if strict:
            die(f"missing {CREDS_FILE} — log in to Claude Code first", EXIT_USER)
        return None
    creds, _, _ = read_json_file(CREDS_FILE)
    if "claudeAiOauth" not in creds:
        if strict:
            die(f"{CREDS_FILE} has no 'claudeAiOauth' key")
        return None
    if not MAIN_FILE.exists():
        if strict:
            die(f"missing {MAIN_FILE} — log in to Claude Code first", EXIT_USER)
        return None
    main_data, _, _ = read_json_file(MAIN_FILE)
    oauth = main_data.get("oauthAccount")
    if not isinstance(oauth, dict):
        if strict:
            die(f"{MAIN_FILE} has no 'oauthAccount' field — log in to Claude Code first")
        return None
    return creds, oauth


def list_profiles() -> list[Path]:
    try:
        items = [
            p for p in PROFILES_DIR.iterdir() if p.is_file() and p.suffix == ".json" and not p.name.startswith(".")
        ]
    except OSError:
        return []
    return sorted(items)


# ---- Commands ---------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    ensure_dirs()
    name: str = args.name
    path = profile_path(name)
    if path.exists():
        if not confirm(f"Profile '{name}' already exists. Overwrite?"):
            print("Cancelled.")
            return EXIT_OK
    state = read_current_state(strict=True)
    assert state is not None  # strict=True would have exited on failure
    creds, oauth = state
    save_profile(name, creds, oauth)
    email = oauth.get("emailAddress", "?")
    sub = creds.get("claudeAiOauth", {}).get("subscriptionType", "?")
    print(f"Saved profile '{name}' ({email}, {sub}) -> {path}")
    return EXIT_OK


def _auto_backup_active(name: str) -> None:
    active = read_active()
    if not active or active == name or not profile_path(active).exists():
        return
    state = read_current_state(strict=False)
    if state is None:
        return
    cur_creds, cur_oauth = state
    save_profile(active, cur_creds, cur_oauth)
    print(f"Auto-saved active profile '{active}'")


def _load_profile_for_use(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = read_profile(name)
    new_creds = profile.get("credentials")
    new_oauth = profile.get("oauthAccount")
    if not isinstance(new_creds, dict) or not isinstance(new_oauth, dict):
        die(f"corrupted profile: {name} (missing credentials/oauthAccount)")
    return new_creds, new_oauth


def _preflight_main() -> tuple[dict[str, Any], int, bool]:
    if not MAIN_FILE.exists():
        die(f"missing {MAIN_FILE} — log in to Claude Code first", EXIT_USER)
    main_data, raw_main, tnl_main = read_json_file(MAIN_FILE)
    return main_data, detect_indent(raw_main), tnl_main


def _preflight_creds() -> tuple[dict[str, Any] | None, int, bool]:
    if CREDS_FILE.exists():
        old_creds, raw_creds, creds_tnl = read_json_file(CREDS_FILE)
        return old_creds, detect_indent(raw_creds), creds_tnl
    if not CLAUDE_DIR.exists():
        die(f"missing {CLAUDE_DIR} — Claude Code has never been run", EXIT_USER)
    return None, 2, False


def _apply_switch_atomic(
    new_creds: dict[str, Any],
    new_oauth: dict[str, Any],
    main_data: dict[str, Any],
    indent_main: int,
    tnl_main: bool,
    old_creds: dict[str, Any] | None,
    creds_indent: int,
    creds_tnl: bool,
) -> None:
    """Apply creds then oauth; roll creds back on oauth failure."""
    atomic_write_json(CREDS_FILE, new_creds, creds_indent, creds_tnl)
    main_data["oauthAccount"] = new_oauth
    try:
        atomic_write_json(MAIN_FILE, main_data, indent_main, tnl_main)
    except SystemExit:
        if old_creds is not None:
            try:
                atomic_write_json(CREDS_FILE, old_creds, creds_indent, creds_tnl)
            except SystemExit:
                pass
        raise


def cmd_use(args: argparse.Namespace) -> int:
    ensure_dirs()
    name: str = args.name
    if not profile_path(name).exists():
        die(f"profile not found: {name}", EXIT_USER)

    _auto_backup_active(name)

    new_creds, new_oauth = _load_profile_for_use(name)
    main_data, indent_main, tnl_main = _preflight_main()
    old_creds, creds_indent, creds_tnl = _preflight_creds()

    backup_main()
    _apply_switch_atomic(
        new_creds,
        new_oauth,
        main_data,
        indent_main,
        tnl_main,
        old_creds,
        creds_indent,
        creds_tnl,
    )
    write_active(name)

    email = new_oauth.get("emailAddress", "?")
    sub = new_creds.get("claudeAiOauth", {}).get("subscriptionType", "?")
    print(f"Switched to '{name}' ({email}, {sub})")
    return EXIT_OK


def _profile_summary(p: Path) -> tuple[str, str]:
    """Return (email, subscriptionType) for a saved profile file.

    Returns sentinel values on any read/parse error so list/current never
    crash on a single malformed profile.
    """
    try:
        data, _, _ = _parse_json_object(p)
    except (OSError, json.JSONDecodeError, TypeError):
        return "<corrupted>", "?"
    oauth = data.get("oauthAccount")
    creds = data.get("credentials")
    email = oauth.get("emailAddress", "?") if isinstance(oauth, dict) else "?"
    if isinstance(creds, dict):
        claude_oauth = creds.get("claudeAiOauth")
        sub = claude_oauth.get("subscriptionType", "?") if isinstance(claude_oauth, dict) else "?"
    else:
        sub = "?"
    return email, sub


def cmd_list(_args: argparse.Namespace) -> int:
    ensure_dirs()
    active = read_active()
    profiles = list_profiles()
    if not profiles:
        print("No profiles yet. Save the current one: cc-switch add <name>")
        return EXIT_OK

    rows: list[tuple[bool, str, str, str]] = []
    for p in profiles:
        name = p.stem
        email, sub = _profile_summary(p)
        rows.append((name == active, name, email, sub))

    name_w = max(len(r[1]) for r in rows)
    email_w = max(len(r[2]) for r in rows)
    for is_active, name, email, sub in rows:
        marker = "*" if is_active else " "
        print(f"{marker} {name:<{name_w}}  {email:<{email_w}}  ({sub})")
    return EXIT_OK


def _profile_email(p: Path) -> str | None:
    try:
        data, _, _ = _parse_json_object(p)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    oauth = data.get("oauthAccount")
    if not isinstance(oauth, dict):
        return None
    email = oauth.get("emailAddress")
    return email if isinstance(email, str) else None


def cmd_current(_args: argparse.Namespace) -> int:
    ensure_dirs()
    active = read_active()
    if active and profile_path(active).exists():
        print(active)
        return EXIT_OK

    state = read_current_state(strict=False)
    if state is None:
        print("No active profile (no current credentials)")
        return EXIT_OK
    _, oauth = state
    cur_email = oauth.get("emailAddress")
    if cur_email:
        for p in list_profiles():
            if _profile_email(p) == cur_email:
                print(p.stem)
                return EXIT_OK
    print(f"No active profile (current email: {cur_email or '?'})")
    return EXIT_OK


def cmd_remove(args: argparse.Namespace) -> int:
    ensure_dirs()
    name: str = args.name
    path = profile_path(name)
    if not path.exists():
        die(f"profile not found: {name}", EXIT_USER)
    if not confirm(f"Remove profile '{name}'?"):
        print("Cancelled.")
        return EXIT_OK
    try:
        path.unlink()
    except OSError as e:
        die(f"failed to remove {path}: {e}")
    if read_active() == name:
        try:
            ACTIVE_FILE.unlink()
        except OSError:
            pass
    print(f"Removed profile '{name}'")
    return EXIT_OK


# ---- Entry point ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cc-switch",
        description="Switch Claude Code OAuth accounts (swaps only credentials; leaves hooks/skills/projects alone).",
    )
    sub = p.add_subparsers(dest="cmd", metavar="command")

    a = sub.add_parser("add", help="save the current active account as a profile")
    a.add_argument("name", help="profile name")
    a.set_defaults(func=cmd_add)

    u = sub.add_parser("use", help="switch to a saved profile")
    u.add_argument("name", help="profile name")
    u.set_defaults(func=cmd_use)

    lst = sub.add_parser("list", help="list all profiles")
    lst.set_defaults(func=cmd_list)

    c = sub.add_parser("current", help="print the active profile name")
    c.set_defaults(func=cmd_current)

    r = sub.add_parser("remove", help="delete a saved profile")
    r.add_argument("name", help="profile name")
    r.set_defaults(func=cmd_remove)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        cmd_list(args)
        print()
        parser.print_help()
        return EXIT_OK
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(EXIT_USER)

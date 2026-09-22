#!/usr/bin/env python3
"""Talk to an Odoo database over RPC, with a guard in front of every write.

Reads run freely. Writes need two independent things to be true: you asked for
one (--write), and the target database is verifiably neutralized (the gate).

Usage:
    odoo-rpc init                       scaffold .odoo-rpc.toml for this project
    odoo-rpc profiles                   list configured profiles
    odoo-rpc check                      version, transport, user, gate verdict

    odoo-rpc models --like partner
    odoo-rpc fields res.partner --attrs string,type,relation
    odoo-rpc search res.partner --domain '[["is_company","=",true]]' --fields name
    odoo-rpc count res.partner --domain '[]'
    odoo-rpc read res.partner --ids 1,2 --fields name,email
    odoo-rpc read-group sale.order --domain '[]' --fields amount_total:sum \\
             --groupby partner_id

    odoo-rpc create res.partner --values '{"name":"Acme"}' --write
    odoo-rpc write res.partner --ids 5 --values '{"name":"Acme II"}' --write
    odoo-rpc unlink res.partner --ids 5 --write
    odoo-rpc call res.partner action_archive --ids 5 --write

Every command accepts --profile, --url, --db, --login, --api-key, --timeout,
--compact, --traceback and --max-bytes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import neutralize
from client import GuardError, OdooClient
from config import (
    ConfigError,
    Profile,
    init_project_config,
    list_profiles,
    load_profile,
)
from transport import (
    DEFAULT_TIMEOUT,
    JsonValue,
    OdooError,
    ServerInfo,
    TransportError,
    build_transport,
    probe,
)

DEFAULT_SEARCH_LIMIT = 80
DEFAULT_MAX_BYTES = 40000


def _parse_json(raw: str | None, what: str) -> JsonValue:
    """Decode a JSON command-line argument with a message naming the flag."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"--{what} is not valid JSON: {exc}") from exc


def _parse_typed_json(raw: str | None, what: str,
                      expected: type | tuple[type, ...], label: str) -> JsonValue:
    """Decode a JSON argument and hold it to the shape the flag documents.

    Without this the wrong shape travels all the way to the transport, where
    `positional.extend(1)` or `dict(["x"])` raises a TypeError or ValueError that
    `main` does not catch — the user gets a traceback instead of a message.
    """
    value = _parse_json(raw, what)
    if value is not None and not isinstance(value, expected):
        raise ConfigError(
            f"--{what} must be a JSON {label}, got {type(value).__name__}"
        )
    return value


def _parse_domain(raw: str | None) -> list[Any]:
    """Decode --domain, refusing anything that is not a list.

    `or []` on a falsy value would turn `--domain false`, `--domain 0` or
    `--domain {}` into the empty domain, so a typo silently becomes an
    unfiltered search or a count of the whole model.
    """
    value = _parse_typed_json(raw, "domain", list, "list")
    return value if isinstance(value, list) else []


def _parse_ids(raw: list[str] | None) -> list[int] | None:
    """Accept --ids 1,2 and --ids 1 --ids 2 alike."""
    if not raw:
        return None
    ids: list[int] = []
    for chunk in raw:
        for piece in chunk.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if not piece.lstrip("-").isdigit():
                raise ConfigError(f"--ids expects integers, got {piece!r}")
            ids.append(int(piece))
    return ids


def _parse_csv(raw: str | None) -> list[str] | None:
    """Split a comma-separated flag value."""
    if not raw:
        return None
    return [piece.strip() for piece in raw.split(",") if piece.strip()]


def _emit(result: JsonValue, opts: argparse.Namespace) -> None:
    """Print a result as JSON, keeping stdout parseable when it is too large.

    Oversized lists lose whole records, never bytes: a byte-level cut would emit
    JSON that nothing downstream can read. Anything else is printed in full with
    a warning, because dropping keys from a dict silently changes its meaning.
    """
    indent = None if opts.compact else 2
    payload = json.dumps(result, indent=indent, default=str)
    if len(payload) <= opts.max_bytes:
        print(payload)
        return

    if isinstance(result, list):
        kept = list(result)
        while kept and len(json.dumps(kept, indent=indent, default=str)) > opts.max_bytes:
            kept.pop()
        dropped = len(result) - len(kept)
        print(json.dumps(kept, indent=indent, default=str))
        print(
            f"warning: dropped {dropped} of {len(result)} records to stay under "
            f"--max-bytes {opts.max_bytes}; narrow --fields or lower --limit",
            file=sys.stderr,
        )
        return

    print(payload)
    print(
        f"warning: result is {len(payload)} bytes, over --max-bytes "
        f"{opts.max_bytes}; narrow it with --attrs or --fields",
        file=sys.stderr,
    )


def _connect(opts: argparse.Namespace) -> tuple[OdooClient, Profile, ServerInfo, str]:
    """Resolve a profile, probe the server and build a guarded client."""
    profile = load_profile(
        name=opts.profile,
        start=Path.cwd(),
        url=opts.url,
        db=opts.db,
        login=opts.login,
        api_key=opts.api_key,
    )
    server = probe(profile.url, opts.timeout)
    transport, why = build_transport(
        profile.url,
        profile.db,
        profile.login,
        profile.secret,
        profile.secret_kind,
        preference=profile.transport,
        timeout=opts.timeout,
        server=server,
    )
    client = OdooClient(
        transport,
        allow_write=opts.write,
        override=opts.i_know_this_is_live,
        gate=neutralize.evaluate,
    )
    return client, profile, server, why


# --- commands that need no server ---------------------------------------------

def cmd_init(opts: argparse.Namespace) -> int:
    target, why = init_project_config(Path.cwd(), force=opts.force)
    print(f"wrote {target}\n  derived from {why}")
    print("Fill in login and api_key, then run: odoo-rpc check")
    return 0


def cmd_profiles(opts: argparse.Namespace) -> int:
    found = list_profiles(Path.cwd())
    if not found:
        print("no profiles found; run 'odoo-rpc init' in a project")
        return 0
    for name, source in found:
        print(f"{name}\n  from {source}")
    return 0


# --- check --------------------------------------------------------------------

def cmd_check(opts: argparse.Namespace) -> int:
    client, profile, server, why = _connect(opts)
    print(f"profile      {profile.name}  ({profile.source or 'command line'})")
    print(f"url          {profile.url}")
    print(f"database     {profile.db or '(not set)'}")
    print(f"version      {server.version or '?'}  (via {server.source})")
    print(f"transport    {client.transport.name}  — {why}")
    print(f"credential   {profile.secret_kind}")
    try:
        uid = client.transport.authenticate()
        print(f"user         {profile.login or '?'} (uid {uid})")
    except (TransportError, OdooError) as exc:
        print(f"user         FAILED — {exc}")
        return 1

    verdict = neutralize.evaluate(client)
    print(f"\nGate         {verdict.status}")
    for result in verdict.results:
        print(result.line())
    if verdict.base_url:
        print(f"\nweb.base.url {verdict.base_url}")
    print(
        "\nWrites are "
        + ("allowed with --write." if verdict.allows_write
           else "refused; see the reasons above.")
    )
    return 0


# --- reads --------------------------------------------------------------------

def cmd_models(opts: argparse.Namespace) -> int:
    client, _profile, _server, _why = _connect(opts)
    domain: list[Any] = []
    if opts.like:
        domain = ["|", ("model", "ilike", opts.like), ("name", "ilike", opts.like)]
    _emit(client.search_read("ir.model", domain, fields=["model", "name"],
                             limit=opts.limit, order="model"), opts)
    return 0


def cmd_fields(opts: argparse.Namespace) -> int:
    client, _profile, _server, _why = _connect(opts)
    kwargs: dict[str, Any] = {}
    attributes = _parse_csv(opts.attrs)
    if attributes:
        kwargs["attributes"] = attributes
    _emit(client.call(opts.model, "fields_get", kwargs=kwargs), opts)
    return 0


def cmd_search(opts: argparse.Namespace) -> int:
    client, _profile, _server, _why = _connect(opts)
    _emit(
        client.search_read(
            opts.model,
            _parse_domain(opts.domain),
            fields=_parse_csv(opts.fields),
            limit=opts.limit,
            offset=opts.offset,
            order=opts.order,
        ),
        opts,
    )
    return 0


def cmd_count(opts: argparse.Namespace) -> int:
    client, _profile, _server, _why = _connect(opts)
    _emit(client.search_count(opts.model, _parse_domain(opts.domain)), opts)
    return 0


def cmd_read(opts: argparse.Namespace) -> int:
    client, _profile, _server, _why = _connect(opts)
    ids = _parse_ids(opts.ids)
    if not ids:
        raise ConfigError("read needs --ids")
    kwargs: dict[str, Any] = {}
    fields = _parse_csv(opts.fields)
    if fields:
        kwargs["fields"] = fields
    _emit(client.call(opts.model, "read", ids=ids, kwargs=kwargs), opts)
    return 0


def cmd_read_group(opts: argparse.Namespace) -> int:
    client, _profile, _server, _why = _connect(opts)
    kwargs: dict[str, Any] = {
        "domain": _parse_domain(opts.domain),
        "fields": _parse_csv(opts.fields) or [],
        "groupby": _parse_csv(opts.groupby) or [],
    }
    if opts.limit is not None:
        kwargs["limit"] = opts.limit
    _emit(client.call(opts.model, "read_group", kwargs=kwargs), opts)
    return 0


# --- writes -------------------------------------------------------------------

def cmd_create(opts: argparse.Namespace) -> int:
    client, _profile, _server, _why = _connect(opts)
    values = _parse_typed_json(opts.values, "values", (dict, list), "object or list")
    if values is None:
        raise ConfigError("create needs --values")
    _emit(client.create(opts.model, values), opts)
    return 0


def cmd_write(opts: argparse.Namespace) -> int:
    client, _profile, _server, _why = _connect(opts)
    ids = _parse_ids(opts.ids)
    values = _parse_typed_json(opts.values, "values", dict, "object")
    if not ids or values is None:
        raise ConfigError("write needs --ids and --values")
    _emit(client.call(opts.model, "write", ids=ids, kwargs={"vals": values}), opts)
    return 0


def cmd_unlink(opts: argparse.Namespace) -> int:
    client, _profile, _server, _why = _connect(opts)
    ids = _parse_ids(opts.ids)
    if not ids:
        raise ConfigError("unlink needs --ids")
    _emit(client.call(opts.model, "unlink", ids=ids), opts)
    return 0


def cmd_call(opts: argparse.Namespace) -> int:
    client, _profile, _server, _why = _connect(opts)
    _emit(
        client.call(
            opts.model,
            opts.method,
            ids=_parse_ids(opts.ids),
            args=_parse_typed_json(opts.args, "args", list, "list"),
            kwargs=_parse_typed_json(opts.kwargs, "kwargs", dict, "object"),
        ),
        opts,
    )
    return 0


# --- wiring -------------------------------------------------------------------

def _add_common(parser: argparse.ArgumentParser) -> None:
    """Flags every subcommand shares."""
    parser.add_argument("--profile", help="named profile to use")
    parser.add_argument("--url", help="override the profile URL")
    parser.add_argument("--db", help="override the database name")
    parser.add_argument("--login", help="override the login")
    parser.add_argument("--api-key", dest="api_key", help="override the API key")
    parser.add_argument("--write", action="store_true",
                        help="permit this call to modify data")
    parser.add_argument("--i-know-this-is-live", dest="i_know_this_is_live",
                        action="store_true",
                        help="write even though the gate refused (needs --write)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--compact", action="store_true",
                        help="emit compact JSON instead of indented")
    parser.add_argument("--traceback", action="store_true",
                        help="show the server-side traceback on error")
    parser.add_argument("--max-bytes", dest="max_bytes", type=int,
                        default=DEFAULT_MAX_BYTES,
                        help=f"output budget (default {DEFAULT_MAX_BYTES})")


# A subcommand factory: every subcommand carries the shared flags.
SubParsers = "argparse._SubParsersAction[argparse.ArgumentParser]"


def _make_adder(subparsers: argparse._SubParsersAction) -> Callable[[str, str], argparse.ArgumentParser]:
    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        _add_common(sub)
        return sub

    return add


def _add_meta_commands(add: Callable[[str, str], argparse.ArgumentParser]) -> None:
    """Commands that describe the setup rather than query data."""
    init = add("init", "scaffold .odoo-rpc.toml for this project")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.set_defaults(func=cmd_init)

    add("profiles", "list configured profiles").set_defaults(func=cmd_profiles)
    add("check", "report version, transport, user and gate verdict").set_defaults(
        func=cmd_check)


def _add_schema_commands(add: Callable[[str, str], argparse.ArgumentParser]) -> None:
    """Schema discovery: what exists before you write a domain."""
    models = add("models", "list models")
    models.add_argument("--like", help="filter by model name or label")
    models.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    models.set_defaults(func=cmd_models)

    fields = add("fields", "describe a model's fields")
    fields.add_argument("model")
    fields.add_argument("--attrs", help="comma-separated attributes to return")
    fields.set_defaults(func=cmd_fields)


def _add_query_commands(add: Callable[[str, str], argparse.ArgumentParser]) -> None:
    """Ungated record queries: none of these can change data."""
    search = add("search", "search_read a model")
    search.add_argument("model")
    search.add_argument("--domain", help="JSON search domain (default [])")
    search.add_argument("--fields", help="comma-separated field names")
    search.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--order")
    search.set_defaults(func=cmd_search)

    count = add("count", "count matching records")
    count.add_argument("model")
    count.add_argument("--domain", help="JSON search domain (default [])")
    count.set_defaults(func=cmd_count)

    read = add("read", "read records by id")
    read.add_argument("model")
    read.add_argument("--ids", action="append")
    read.add_argument("--fields", help="comma-separated field names")
    read.set_defaults(func=cmd_read)

    group = add("read-group", "aggregate records")
    group.add_argument("model")
    group.add_argument("--domain")
    group.add_argument("--fields", help="comma-separated aggregates")
    group.add_argument("--groupby", help="comma-separated group keys")
    group.add_argument("--limit", type=int)
    group.set_defaults(func=cmd_read_group)


def _add_write_commands(add: Callable[[str, str], argparse.ArgumentParser]) -> None:
    """Gated commands, plus the escape hatch the guard also classifies."""
    create = add("create", "create records (needs --write)")
    create.add_argument("model")
    create.add_argument("--values", help="JSON dict, or list of dicts")
    create.set_defaults(func=cmd_create)

    write = add("write", "update records (needs --write)")
    write.add_argument("model")
    write.add_argument("--ids", action="append")
    write.add_argument("--values", help="JSON dict of values")
    write.set_defaults(func=cmd_write)

    unlink = add("unlink", "delete records (needs --write)")
    unlink.add_argument("model")
    unlink.add_argument("--ids", action="append")
    unlink.set_defaults(func=cmd_unlink)

    call = add("call", "call any model method")
    call.add_argument("model")
    call.add_argument("method")
    call.add_argument("--ids", action="append")
    call.add_argument("--args", help="JSON list of positional arguments")
    call.add_argument("--kwargs", help="JSON dict of keyword arguments")
    call.set_defaults(func=cmd_call)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odoo-rpc",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add = _make_adder(subparsers)
    _add_meta_commands(add)
    _add_schema_commands(add)
    _add_query_commands(add)
    _add_write_commands(add)
    return parser


def _report(exc: Exception, show_traceback: bool) -> int:
    """Print one readable line instead of a wall of traceback."""
    print(f"error: {exc}", file=sys.stderr)
    if show_traceback and isinstance(exc, OdooError) and exc.debug:
        print(exc.debug, file=sys.stderr)
    return 1


def main() -> int:
    opts = _build_arg_parser().parse_args()
    if opts.i_know_this_is_live and not opts.write:
        print(
            "error: --i-know-this-is-live does nothing on its own; it only lifts "
            "the gate for a write, so pass --write too.",
            file=sys.stderr,
        )
        return 1
    if opts.timeout <= 0:
        print(
            f"error: --timeout must be greater than 0, got {opts.timeout:g}",
            file=sys.stderr,
        )
        return 1
    try:
        return int(opts.func(opts))
    except (ConfigError, GuardError, TransportError, OdooError) as exc:
        return _report(exc, opts.traceback)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Tests for the command bodies: what each subcommand actually asks the server.

These drive the `cmd_*` functions directly with a fake client, so they pin the
call each command builds — the model, the method, and the keyword arguments —
without a server. A command that quietly sent `read` where it promised
`search_read`, or dropped `--fields`, would still print plausible JSON; only an
assertion on the recorded call catches that.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

import neutralize
import odoo_rpc
from client import GuardError
from config import ConfigError, Profile
from transport import DEFAULT_TIMEOUT, JsonValue, OdooError, ServerInfo, TransportError


class FakeTransport:
    """Stands in for a real transport, recording nothing but its own name."""

    name = "fake"

    def __init__(self, uid: int = 7) -> None:
        self.uid = uid
        self.authenticated = 0

    def authenticate(self) -> int:
        self.authenticated += 1
        return self.uid


class FakeClient:
    """Records every call so a test can assert what the command asked for."""

    def __init__(self, result: JsonValue = None, count: int = 3) -> None:
        self.transport = FakeTransport()
        self.calls: list[tuple[Any, ...]] = []
        self.result = [] if result is None else result
        self.count = count

    def call(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> JsonValue:
        self.calls.append(("call", model, method, ids, args, kwargs))
        return self.result

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str | None = None,
    ) -> JsonValue:
        self.calls.append(("search_read", model, domain, fields, limit, offset, order))
        return self.result

    def search_count(self, model: str, domain: list[Any]) -> int:
        self.calls.append(("search_count", model, domain))
        return self.count

    def create(self, model: str, values: JsonValue) -> JsonValue:
        self.calls.append(("create", model, values))
        return 42


def make_opts(**overrides: object) -> argparse.Namespace:
    """A Namespace carrying every flag `_add_common` defines, plus overrides."""
    base: dict[str, Any] = {
        "profile": None,
        "url": None,
        "db": None,
        "login": None,
        "api_key": None,
        "write": False,
        "i_know_this_is_live": False,
        "timeout": DEFAULT_TIMEOUT,
        "compact": True,
        "traceback": False,
        "max_bytes": 40000,
        "model": "res.partner",
        "ids": None,
        "fields": None,
        "domain": None,
        "limit": None,
        "offset": 0,
        "order": None,
        "attrs": None,
        "groupby": None,
        "values": None,
        "args": None,
        "kwargs": None,
        "like": None,
        "force": False,
        "method": "action_archive",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Patch `_connect` so every command runs against a recording fake."""
    fake = FakeClient()
    profile = Profile(
        name="test", url="http://localhost:8069", db="testdb", login="admin", secret="k", secret_kind="api_key"
    )
    server = ServerInfo(version="19.0", version_info=[19, 0], source="/web/version")
    monkeypatch.setattr(odoo_rpc, "_connect", lambda _opts: (fake, profile, server, "because"))
    return fake


# --- reads --------------------------------------------------------------------


def test_models_without_filter_searches_every_model(client: FakeClient, capsys: pytest.CaptureFixture[str]) -> None:
    assert odoo_rpc.cmd_models(make_opts(limit=80)) == 0
    kind, model, domain, fields, limit, _offset, order = client.calls[0]
    assert (kind, model, domain) == ("search_read", "ir.model", [])
    assert fields == ["model", "name"]
    assert (limit, order) == (80, "model")
    assert json.loads(capsys.readouterr().out) == []


def test_models_like_matches_technical_name_or_label(client: FakeClient) -> None:
    """--like must reach both columns; an ilike on one alone misses half."""
    odoo_rpc.cmd_models(make_opts(like="partner", limit=80))
    domain = client.calls[0][2]
    assert domain == ["|", ("model", "ilike", "partner"), ("name", "ilike", "partner")]


def test_fields_passes_requested_attributes(client: FakeClient) -> None:
    assert odoo_rpc.cmd_fields(make_opts(attrs="string,type")) == 0
    assert client.calls[0] == ("call", "res.partner", "fields_get", None, None, {"attributes": ["string", "type"]})


def test_fields_without_attrs_sends_no_attribute_filter(client: FakeClient) -> None:
    odoo_rpc.cmd_fields(make_opts(attrs=None))
    assert client.calls[0][5] == {}


def test_search_forwards_domain_fields_and_paging(client: FakeClient) -> None:
    opts = make_opts(domain='[["is_company","=",true]]', fields="name,email", limit=5, offset=10, order="name desc")
    assert odoo_rpc.cmd_search(opts) == 0
    kind, model, domain, fields, limit, offset, order = client.calls[0]
    assert kind == "search_read"
    assert domain == [["is_company", "=", True]]
    assert fields == ["name", "email"]
    assert (limit, offset, order) == (5, 10, "name desc")


def test_search_defaults_to_the_empty_domain(client: FakeClient) -> None:
    odoo_rpc.cmd_search(make_opts(domain=None, limit=80))
    assert client.calls[0][2] == []


def test_count_prints_the_number(client: FakeClient, capsys: pytest.CaptureFixture[str]) -> None:
    assert odoo_rpc.cmd_count(make_opts(domain="[]")) == 0
    assert client.calls[0] == ("search_count", "res.partner", [])
    assert json.loads(capsys.readouterr().out) == 3


def test_read_sends_ids_and_fields(client: FakeClient) -> None:
    assert odoo_rpc.cmd_read(make_opts(ids=["1,2"], fields="name")) == 0
    assert client.calls[0] == ("call", "res.partner", "read", [1, 2], None, {"fields": ["name"]})


def test_read_without_fields_asks_for_all_of_them(client: FakeClient) -> None:
    odoo_rpc.cmd_read(make_opts(ids=["1"], fields=None))
    assert client.calls[0][5] == {}


def test_read_without_ids_is_refused(client: FakeClient) -> None:
    with pytest.raises(ConfigError, match="read needs --ids"):
        odoo_rpc.cmd_read(make_opts(ids=None))


def test_read_group_forwards_aggregates_and_keys(client: FakeClient) -> None:
    opts = make_opts(model="sale.order", domain="[]", fields="amount_total:sum", groupby="partner_id", limit=5)
    assert odoo_rpc.cmd_read_group(opts) == 0
    assert client.calls[0] == (
        "call",
        "sale.order",
        "read_group",
        None,
        None,
        {"domain": [], "fields": ["amount_total:sum"], "groupby": ["partner_id"], "limit": 5},
    )


def test_read_group_omits_limit_when_unset(client: FakeClient) -> None:
    odoo_rpc.cmd_read_group(make_opts(model="sale.order", limit=None))
    assert "limit" not in client.calls[0][5]


# --- writes -------------------------------------------------------------------


def test_create_passes_values_through(client: FakeClient, capsys: pytest.CaptureFixture[str]) -> None:
    assert odoo_rpc.cmd_create(make_opts(values='{"name":"Acme"}')) == 0
    assert client.calls[0] == ("create", "res.partner", {"name": "Acme"})
    assert json.loads(capsys.readouterr().out) == 42


def test_create_without_values_is_refused(client: FakeClient) -> None:
    with pytest.raises(ConfigError, match="create needs --values"):
        odoo_rpc.cmd_create(make_opts(values=None))


def test_write_sends_values_under_the_vals_key(client: FakeClient) -> None:
    """Odoo's write takes `vals`; any other key silently updates nothing."""
    opts = make_opts(ids=["5"], values='{"name":"Acme II"}')
    assert odoo_rpc.cmd_write(opts) == 0
    assert client.calls[0] == ("call", "res.partner", "write", [5], None, {"vals": {"name": "Acme II"}})


@pytest.mark.parametrize(
    ("ids", "values"),
    [(None, '{"name":"x"}'), (["5"], None)],
    ids=["no-ids", "no-values"],
)
def test_write_needs_both_ids_and_values(client: FakeClient, ids: list[str] | None, values: str | None) -> None:
    with pytest.raises(ConfigError, match="write needs --ids and --values"):
        odoo_rpc.cmd_write(make_opts(ids=ids, values=values))


def test_unlink_sends_the_ids(client: FakeClient) -> None:
    assert odoo_rpc.cmd_unlink(make_opts(ids=["5,6"])) == 0
    assert client.calls[0] == ("call", "res.partner", "unlink", [5, 6], None, None)


def test_unlink_without_ids_is_refused(client: FakeClient) -> None:
    with pytest.raises(ConfigError, match="unlink needs --ids"):
        odoo_rpc.cmd_unlink(make_opts(ids=None))


def test_call_forwards_method_args_and_kwargs(client: FakeClient) -> None:
    opts = make_opts(method="action_confirm", ids=["5"], args="[1]", kwargs='{"force":true}')
    assert odoo_rpc.cmd_call(opts) == 0
    assert client.calls[0] == ("call", "res.partner", "action_confirm", [5], [1], {"force": True})


# --- meta commands ------------------------------------------------------------


def test_init_reports_where_it_wrote(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        odoo_rpc,
        "init_project_config",
        lambda _start, force=False: (Path("/repo/.odoo-rpc.toml"), "worktree: port 50010"),
    )
    assert odoo_rpc.cmd_init(make_opts()) == 0
    out = capsys.readouterr().out
    assert "/repo/.odoo-rpc.toml" in out
    assert "worktree: port 50010" in out


def test_init_passes_force_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, bool] = {}

    def fake_init(_start: Path, force: bool = False) -> tuple[Path, str]:
        seen["force"] = force
        return Path("/repo/.odoo-rpc.toml"), "why"

    monkeypatch.setattr(odoo_rpc, "init_project_config", fake_init)
    odoo_rpc.cmd_init(make_opts(force=True))
    assert seen["force"] is True


def test_profiles_lists_each_name_with_its_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(odoo_rpc, "list_profiles", lambda _start: [("pri", Path("/repo/.odoo-rpc.toml"))])
    assert odoo_rpc.cmd_profiles(make_opts()) == 0
    out = capsys.readouterr().out
    assert "pri" in out
    assert "/repo/.odoo-rpc.toml" in out


def test_profiles_says_so_when_there_are_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(odoo_rpc, "list_profiles", lambda _start: [])
    assert odoo_rpc.cmd_profiles(make_opts()) == 0
    assert "odoo-rpc init" in capsys.readouterr().out


# --- check --------------------------------------------------------------------


def _verdict(status: str) -> neutralize.Verdict:
    return neutralize.Verdict(
        status,
        [neutralize.CheckResult("flag", "Neutralization flag", neutralize.PASS, "set")],
        "http://localhost:8069",
    )


def test_check_reports_the_gate_verdict(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(neutralize, "evaluate", lambda _c: _verdict(neutralize.NEUTRALIZED))
    assert odoo_rpc.cmd_check(make_opts()) == 0
    out = capsys.readouterr().out
    assert "NEUTRALIZED" in out
    assert "uid 7" in out
    assert "allowed with --write" in out


def test_check_says_writes_are_refused_when_not_neutralized(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(neutralize, "evaluate", lambda _c: _verdict(neutralize.LIVE))
    assert odoo_rpc.cmd_check(make_opts()) == 0
    assert "refused" in capsys.readouterr().out


def test_check_fails_when_authentication_is_refused(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed login must not be followed by a gate verdict read as trustworthy."""

    def boom() -> int:
        raise TransportError("cannot reach the server")

    monkeypatch.setattr(client.transport, "authenticate", boom)
    assert odoo_rpc.cmd_check(make_opts()) == 1
    assert "FAILED" in capsys.readouterr().out


# --- _connect -----------------------------------------------------------------


def test_connect_builds_a_guarded_client(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = Profile(name="p", url="http://localhost:8069", db="db", login="admin", secret="k", secret_kind="api_key")
    transport = FakeTransport()
    monkeypatch.setattr(odoo_rpc, "load_profile", lambda **_kw: profile)
    monkeypatch.setattr(odoo_rpc, "probe", lambda _url, _timeout: ServerInfo("19.0", [19, 0], "/web/version"))
    monkeypatch.setattr(odoo_rpc, "build_transport", lambda *_a, **_kw: (transport, "chosen"))

    client_obj, resolved, server, why = odoo_rpc._connect(make_opts(write=True))

    assert resolved is profile
    assert server.major == 19
    assert why == "chosen"
    assert client_obj.allow_write is True
    assert client_obj.override is False
    assert client_obj._gate is neutralize.evaluate


def test_connect_carries_the_override_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = Profile(name="p", url="http://localhost:8069")
    monkeypatch.setattr(odoo_rpc, "load_profile", lambda **_kw: profile)
    monkeypatch.setattr(odoo_rpc, "probe", lambda _url, _timeout: ServerInfo("18.0", [18, 0], "x"))
    monkeypatch.setattr(odoo_rpc, "build_transport", lambda *_a, **_kw: (FakeTransport(), "why"))

    client_obj, _p, _s, _w = odoo_rpc._connect(make_opts(write=True, i_know_this_is_live=True))
    assert client_obj.override is True


# --- error reporting ----------------------------------------------------------


def test_report_prints_one_line_without_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    error = OdooError("odoo.exceptions.AccessError", "not allowed", debug="TRACE")
    assert odoo_rpc._report(error, show_traceback=False) == 1
    captured = capsys.readouterr().err
    assert "AccessError: not allowed" in captured
    assert "TRACE" not in captured


def test_report_adds_the_server_traceback_on_request(capsys: pytest.CaptureFixture[str]) -> None:
    error = OdooError("odoo.exceptions.UserError", "boom", debug="SERVER TRACE")
    odoo_rpc._report(error, show_traceback=True)
    assert "SERVER TRACE" in capsys.readouterr().err


def test_main_turns_a_guard_refusal_into_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused write exits non-zero instead of raising at the user."""

    def boom(_opts: argparse.Namespace) -> int:
        raise GuardError("would modify data; re-run with --write")

    monkeypatch.setattr(
        odoo_rpc.sys, "argv", ["odoo-rpc", "unlink", "res.partner", "--ids", "5"]
    )
    monkeypatch.setattr(odoo_rpc, "cmd_unlink", boom)
    assert odoo_rpc.main() == 1
    assert "re-run with --write" in capsys.readouterr().err


def test_main_reports_a_config_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(odoo_rpc.sys, "argv", ["odoo-rpc", "profiles"])
    monkeypatch.setattr(odoo_rpc, "cmd_profiles", lambda _o: (_ for _ in ()).throw(ConfigError("no profile found")))
    assert odoo_rpc.main() == 1
    assert "no profile found" in capsys.readouterr().err


def test_main_treats_interruption_as_130(monkeypatch: pytest.MonkeyPatch) -> None:
    """130 is the shell's convention for SIGINT; 1 would read as a real failure."""
    monkeypatch.setattr(odoo_rpc.sys, "argv", ["odoo-rpc", "profiles"])
    monkeypatch.setattr(odoo_rpc, "cmd_profiles", lambda _o: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert odoo_rpc.main() == 130


def test_main_dispatches_to_the_parsed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(odoo_rpc.sys, "argv", ["odoo-rpc", "count", "res.partner", "--domain", "[]"])
    monkeypatch.setattr(odoo_rpc, "cmd_count", lambda o: seen.append(o.model) or 0)
    assert odoo_rpc.main() == 0
    assert seen == ["res.partner"]


# --- review findings: every arm of the error handlers --------------------------


def test_check_reports_an_odoo_side_authentication_failure(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An Odoo-side refusal arrives as OdooError, not TransportError."""

    def boom() -> int:
        raise OdooError("odoo.exceptions.AccessDenied", "Access Denied")

    monkeypatch.setattr(client.transport, "authenticate", boom)
    assert odoo_rpc.cmd_check(make_opts()) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "Access Denied" in out


def test_main_reports_a_transport_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreachable server must print one line, not a traceback."""
    monkeypatch.setattr(odoo_rpc.sys, "argv", ["odoo-rpc", "profiles"])
    monkeypatch.setattr(
        odoo_rpc,
        "cmd_profiles",
        lambda _o: (_ for _ in ()).throw(TransportError("cannot reach http://x")),
    )
    assert odoo_rpc.main() == 1
    assert "cannot reach" in capsys.readouterr().err


def test_main_reports_an_odoo_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        odoo_rpc.sys, "argv", ["odoo-rpc", "count", "res.partner", "--domain", "[]"]
    )
    monkeypatch.setattr(
        odoo_rpc,
        "cmd_count",
        lambda _o: (_ for _ in ()).throw(
            OdooError("odoo.exceptions.AccessError", "not allowed")
        ),
    )
    assert odoo_rpc.main() == 1
    assert "AccessError: not allowed" in capsys.readouterr().err

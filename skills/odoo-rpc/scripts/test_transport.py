#!/usr/bin/env python3
"""Tests for version probing, both transports, and error unwrapping."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

import transport
from transport import (
    AuthError,
    Json2Transport,
    JsonRpcTransport,
    OdooError,
    TransportError,
    build_transport,
    probe,
)

URL = "https://odoo.test"

# routes -> installed fake; the fixture returns this installer
Routes = dict[str, tuple[int, object]]


class FakeHttp:
    """Stands in for _urlopen, matching on URL substring."""

    def __init__(self, routes: Routes) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        url = request.full_url
        body = json.loads(request.data) if request.data else {}
        self.calls.append((url, body, dict(request.headers)))
        for fragment, (status, payload) in self.routes.items():
            if fragment in url:
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return status, raw
        raise AssertionError(f"unexpected request to {url}")


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Callable[[Routes], FakeHttp]:
    def install(routes: Routes) -> FakeHttp:
        fake = FakeHttp(routes)
        monkeypatch.setattr(transport, "_urlopen", fake)
        return fake
    return install


# --- probe --------------------------------------------------------------------

def test_probe_uses_web_version_on_19(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/web/version": (200, {"version_info": [19, 0, 0, "final", 0, ""],
                                 "version": "19.0"})})
    info = probe(URL)
    assert info.major == 19
    assert info.source == "/web/version"


def test_probe_ignores_html_answered_with_200(http: Callable[[Routes], FakeHttp]) -> None:
    """A website module can answer 200 with HTML; only JSON proves the route."""
    fake = http({
        "/web/version": (200, b"<!DOCTYPE html><html>not found</html>"),
        "/jsonrpc": (200, {"result": {"server_version": "17.0",
                                      "server_version_info": [17, 0, 0, "final", 0, ""]}}),
    })
    info = probe(URL)
    assert info.major == 17
    assert info.source == "/jsonrpc common.version"
    assert len(fake.calls) == 2


def test_probe_falls_back_to_jsonrpc_on_404(http: Callable[[Routes], FakeHttp]) -> None:
    http({
        "/web/version": (404, {"error": "not found"}),
        "/jsonrpc": (200, {"result": {"server_version": "18.0",
                                      "server_version_info": [18, 0, 0, "final", 0, ""]}}),
    })
    assert probe(URL).major == 18


def test_probe_rejects_a_non_odoo_server(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/web/version": (404, {}), "/jsonrpc": (200, {"jsonrpc": "2.0"})})
    with pytest.raises(TransportError, match="not an Odoo server"):
        probe(URL)


# --- classic JSON-RPC ---------------------------------------------------------

def test_jsonrpc_authenticates_and_calls(http: Callable[[Routes], FakeHttp]) -> None:
    fake = http({"/jsonrpc": (200, {"result": 7})})
    rpc = JsonRpcTransport(URL, "db", "admin", "key")
    assert rpc.call("res.partner", "search_read", kwargs={"fields": ["name"]}) == 7
    auth_args = fake.calls[0][1]["params"]["args"]
    assert auth_args == ["db", "admin", "key", {}]
    exec_args = fake.calls[1][1]["params"]["args"]
    assert exec_args[:5] == ["db", 7, "key", "res.partner", "search_read"]
    assert exec_args[5:] == [[], {"fields": ["name"]}]


def test_jsonrpc_puts_ids_first_in_positional_args(http: Callable[[Routes], FakeHttp]) -> None:
    """execute_kw expects the recordset ids as the first positional argument."""
    fake = http({"/jsonrpc": (200, {"result": 3})})
    rpc = JsonRpcTransport(URL, "db", "admin", "key")
    rpc.uid = 3
    rpc.call("res.partner", "write", ids=[5], args=[{"name": "x"}])
    assert fake.calls[0][1]["params"]["args"][5] == [[5], {"name": "x"}]


def test_jsonrpc_error_inside_a_200_is_raised(http: Callable[[Routes], FakeHttp]) -> None:
    """Classic RPC always answers 200; the failure hides in the body."""
    http({"/jsonrpc": (200, {"error": {"code": 200, "message": "Odoo Server Error",
                                       "data": {"name": "odoo.exceptions.AccessError",
                                                "message": "Not allowed",
                                                "arguments": ["Not allowed"],
                                                "debug": "Traceback..."}}})})
    rpc = JsonRpcTransport(URL, "db", "admin", "key")
    rpc.uid = 1
    with pytest.raises(OdooError) as excinfo:
        rpc.call("res.partner", "read")
    assert str(excinfo.value) == "AccessError: Not allowed"
    assert excinfo.value.debug == "Traceback..."


def test_jsonrpc_refused_credentials_mention_key_scope(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/jsonrpc": (200, {"result": False})})
    with pytest.raises(AuthError, match="GLOBAL"):
        JsonRpcTransport(URL, "db", "admin", "bad").authenticate()


def test_jsonrpc_requires_db_and_login(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/jsonrpc": (200, {"result": 1})})
    with pytest.raises(AuthError, match="needs both a database and a login"):
        JsonRpcTransport(URL, None, None, "key").authenticate()


# --- JSON-2 -------------------------------------------------------------------

def test_json2_sends_bearer_and_database_headers(http: Callable[[Routes], FakeHttp]) -> None:
    fake = http({"/json/2/res.partner/search_read": (200, [{"id": 1}])})
    api = Json2Transport(URL, "db", "key")
    assert api.call("res.partner", "search_read", kwargs={"limit": 1}) == [{"id": 1}]
    _url, body, headers = fake.calls[0]
    assert headers["Authorization"] == "bearer key"
    assert headers["X-odoo-database"] == "db"
    assert body == {"limit": 1}


def test_json2_puts_ids_in_the_body(http: Callable[[Routes], FakeHttp]) -> None:
    fake = http({"/json/2/res.partner/write": (200, True)})
    Json2Transport(URL, "db", "key").call("res.partner", "write", ids=[5],
                                          kwargs={"vals": {"name": "x"}})
    assert fake.calls[0][1] == {"vals": {"name": "x"}, "ids": [5]}


def test_json2_rejects_positional_args_with_a_usable_message(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/json/2": (200, True)})
    with pytest.raises(TransportError, match="named arguments only"):
        Json2Transport(URL, "db", "key").call("res.partner", "write", args=[{"a": 1}])


def test_json2_error_comes_from_the_http_status(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/json/2/res.partner/read": (400, {"name": "odoo.exceptions.UserError",
                                             "message": "Bad request",
                                             "arguments": ["Bad request"]})})
    with pytest.raises(OdooError) as excinfo:
        Json2Transport(URL, "db", "key").call("res.partner", "read", ids=[1])
    assert str(excinfo.value) == "UserError: Bad request"


def test_json2_403_explains_the_key_must_be_global(http: Callable[[Routes], FakeHttp]) -> None:
    """A scoped or expired key fails with a bare 403 and no explanation."""
    http({"/json/2/res.partner/read": (403, {"message": "forbidden"})})
    with pytest.raises(AuthError, match="GLOBAL"):
        Json2Transport(URL, "db", "key").call("res.partner", "read", ids=[1])


def test_json2_uid_comes_from_context_get(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/json/2/res.users/context_get": (200, {"lang": "en_US", "uid": 42})})
    assert Json2Transport(URL, "db", "key").authenticate() == 42


def test_json2_without_a_key_is_refused(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/json/2": (200, {})})
    with pytest.raises(AuthError, match="API key only"):
        Json2Transport(URL, "db", None).authenticate()


# --- selection ----------------------------------------------------------------

def test_selects_json2_on_19_with_an_api_key(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/web/version": (200, {"version_info": [19, 0], "version": "19.0"})})
    chosen, why = build_transport(URL, "db", "admin", "key", "api_key")
    assert chosen.name == "json2"
    assert "19" in why


def test_falls_back_to_classic_on_19_without_an_api_key(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/web/version": (200, {"version_info": [19, 0], "version": "19.0"})})
    chosen, why = build_transport(URL, "db", "admin", "pw", "password")
    assert chosen.name == "jsonrpc"
    assert "needs an API key" in why


def test_selects_classic_below_19(http: Callable[[Routes], FakeHttp]) -> None:
    http({"/web/version": (404, {}),
          "/jsonrpc": (200, {"result": {"server_version": "17.0",
                                        "server_version_info": [17, 0]}})})
    chosen, _ = build_transport(URL, "db", "admin", "key", "api_key")
    assert chosen.name == "jsonrpc"


def test_config_can_force_a_transport(http: Callable[[Routes], FakeHttp]) -> None:
    """Forcing must not probe at all."""
    fake = http({})
    chosen, why = build_transport(URL, "db", "admin", "key", "api_key",
                                  preference="jsonrpc")
    assert chosen.name == "jsonrpc"
    assert why == "forced by config"
    assert fake.calls == []


def test_timeout_is_reported_as_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        raise TransportError("https://odoo.test/jsonrpc timed out after 30s; "
                             "narrow the query or raise --timeout")
    monkeypatch.setattr(transport, "_urlopen", boom)
    rpc = JsonRpcTransport(URL, "db", "admin", "key")
    rpc.uid = 1
    with pytest.raises(TransportError, match="--timeout"):
        rpc.call("res.partner", "search_read")


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (ConnectionResetError(104, "Connection reset by peer"), "cannot reach"),
        (urllib.error.URLError("refused"), "cannot reach"),
        (TimeoutError(), "--timeout"),
    ],
)
def test_socket_level_failures_become_readable_errors(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, expected: str
) -> None:
    """A dropped connection is an OSError but not a URLError; it must not escape."""
    def boom(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        raise raised

    monkeypatch.setattr(transport.urllib.request, "urlopen", boom)
    with pytest.raises(TransportError, match=expected):
        transport._urlopen(urllib.request.Request(f"{URL}/web/version"), 5.0)


def test_jsonrpc_create_sends_values_positionally(http: Callable[[Routes], FakeHttp]) -> None:
    """Odoo 17/18 read args[0] in _call_kw_model_create; a keyword leaves it empty."""
    fake = http({"/jsonrpc": (200, {"result": 11})})
    rpc = JsonRpcTransport(URL, "db", "admin", "key")
    rpc.uid = 1
    assert rpc.create("res.partner", {"name": "Acme"}) == 11
    positional = fake.calls[0][1]["params"]["args"][5]
    assert positional == [{"name": "Acme"}]
    assert fake.calls[0][1]["params"]["args"][6] == {}


def test_json2_create_uses_the_named_parameter(http: Callable[[Routes], FakeHttp]) -> None:
    """JSON-2 has no positional arguments, so the real parameter name is required."""
    fake = http({"/json/2/res.partner/create": (200, [11])})
    Json2Transport(URL, "db", "key").create("res.partner", [{"name": "Acme"}])
    assert fake.calls[0][1] == {"vals_list": [{"name": "Acme"}]}

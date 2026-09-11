#!/usr/bin/env python3
"""Tests for the failure branches: what happens when the environment misbehaves.

Every path here is one the happy-path tests never reach — a malformed config, a
server that answers with HTML, a git command that fails. They are the branches
that decide whether a wrong answer surfaces as a readable error or as a
traceback, and a few where a wrong answer would be silently accepted.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

import config
import neutralize
import odoo_rpc
import transport
from client import READ_METHODS, GuardError, OdooClient
from config import ConfigError
from neutralize import FAIL, PASS, SKIPPED, CheckResult, Verdict
from transport import (
    JsonValue,
    AuthError,
    Json2Transport,
    JsonRpcTransport,
    OdooError,
    ServerInfo,
    Transport,
    TransportError,
    build_transport,
    probe,
)

URL = "https://odoo.test"


@pytest.fixture(autouse=True)
def _no_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real ~/.config/odoo-rpc out of every test in this file.

    Without it a profile the developer happens to have configured joins the
    results and the assertion fails for a reason that has nothing to do with the
    code under test.
    """
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")


class RecordingTransport:
    """Captures the kwargs a client builds, without a server."""

    name = "recording"

    def __init__(self, result: JsonValue = None) -> None:
        self.result = [] if result is None else result
        self.calls: list[tuple[Any, ...]] = []

    def call(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> JsonValue:
        self.calls.append((model, method, ids, args, kwargs))
        return self.result


# --- client -------------------------------------------------------------------


def test_verdict_is_none_when_no_gate_is_installed() -> None:
    """Without a gate there is nothing to consult, and writes are unguarded."""
    client = OdooClient(RecordingTransport(), allow_write=True)
    assert client.verdict() is None
    client.call("res.partner", "write", ids=[1], kwargs={"vals": {"name": "x"}})
    assert client.transport.calls[0][1] == "write"


def test_search_read_forwards_limit_and_order() -> None:
    recording = RecordingTransport()
    client = OdooClient(recording)
    client.search_read("res.partner", [], fields=["name"], limit=5, order="name desc")
    kwargs = recording.calls[0][4]
    assert kwargs["limit"] == 5
    assert kwargs["order"] == "name desc"


def test_search_read_omits_paging_keys_when_unset() -> None:
    """An explicit limit=None must not become limit: null in the payload."""
    recording = RecordingTransport()
    client = OdooClient(recording)
    client.search_read("res.partner", [])
    kwargs = recording.calls[0][4]
    assert "limit" not in kwargs
    assert "order" not in kwargs


def test_search_count_returns_zero_for_a_non_integer_answer() -> None:
    client = OdooClient(RecordingTransport(result="not a number"))
    assert client.search_count("res.partner", []) == 0


# --- neutralize ---------------------------------------------------------------


def test_failures_lists_only_the_failed_checks() -> None:
    verdict = Verdict(
        neutralize.LIVE,
        [
            CheckResult("flag", "Flag", PASS),
            CheckResult("crons", "Crons", FAIL, "2 active"),
            CheckResult("mail", "Mail", SKIPPED, "unavailable"),
        ],
    )
    assert [r.key for r in verdict.failures()] == ["crons"]


def test_cron_check_skips_when_search_returns_something_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-list answer must not be counted as zero live crons."""
    client = OdooClient(RecordingTransport())
    monkeypatch.setattr(neutralize, "_autovacuum_id", lambda _c: 1)
    monkeypatch.setattr(client, "raw_call", lambda *_a, **_kw: "unexpected")
    result = neutralize._check_crons(client)
    assert result.outcome == SKIPPED
    assert "returned" in result.detail


def test_evaluate_survives_an_unreadable_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """web.base.url is advisory; losing it must not abort the whole verdict."""
    client = OdooClient(RecordingTransport())

    def fake_get_param(_client: OdooClient, key: str) -> str | None:
        if key == neutralize.NEUTRALIZE_FLAG:
            return "True"
        raise OdooError("odoo.exceptions.AccessError", "denied")

    monkeypatch.setattr(neutralize, "_get_param", fake_get_param)
    monkeypatch.setattr(neutralize, "_run_count_check", lambda _c, check: CheckResult(check.key, check.label, PASS))
    monkeypatch.setattr(neutralize, "_check_crons", lambda _c: CheckResult("crons", "Crons", PASS))

    verdict = neutralize.evaluate(client)
    assert verdict.status == neutralize.NEUTRALIZED
    assert verdict.base_url is None


# --- transport: version parsing -----------------------------------------------


def test_major_falls_back_to_the_version_string() -> None:
    """Some servers report version without a structured version_info."""
    assert ServerInfo("18.0-20240101", [], "x").major == 18


def test_major_is_zero_when_nothing_parses() -> None:
    assert ServerInfo("saas~unknown", [], "x").major == 0


# --- transport: HTTP plumbing -------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_urlopen_returns_status_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transport.urllib.request, "urlopen", lambda _req, timeout=None: FakeResponse(200, b'{"ok":true}')
    )
    request = urllib.request.Request(URL)
    assert transport._urlopen(request, 5.0) == (200, b'{"ok":true}')


def test_urlopen_treats_an_http_error_as_a_normal_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON-2 carries structured errors inside a 4xx, so the body must survive."""

    def raise_http_error(_req: object, timeout: float | None = None) -> None:
        raise urllib.error.HTTPError(URL, 403, "Forbidden", {}, io.BytesIO(b'{"name":"x"}'))

    monkeypatch.setattr(transport.urllib.request, "urlopen", raise_http_error)
    status, body = transport._urlopen(urllib.request.Request(URL), 5.0)
    assert status == 403
    assert body == b'{"name":"x"}'


def test_urlopen_reports_a_dropped_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reset is an OSError but not a URLError, and would escape unhandled."""

    def reset(_req: object, timeout: float | None = None) -> None:
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr(transport.urllib.request, "urlopen", reset)
    with pytest.raises(TransportError, match="cannot reach"):
        transport._urlopen(urllib.request.Request(URL), 5.0)


def test_non_json_body_names_the_status_and_shows_a_snippet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTML error page is the usual cause; the snippet is what identifies it."""
    monkeypatch.setattr(transport, "_urlopen", lambda _req, _timeout: (502, b"<html>Bad Gateway</html>"))
    with pytest.raises(TransportError, match="non-JSON body") as excinfo:
        transport._post_json(URL, {}, 5.0)
    assert "Bad Gateway" in str(excinfo.value)


def test_probe_falls_back_to_jsonrpc_when_web_version_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-19 servers have no /web/version route at all."""

    def fake_urlopen(request: urllib.request.Request, _timeout: float) -> tuple[int, bytes]:
        if "/web/version" in request.full_url:
            raise TransportError("cannot reach")
        payload = {"result": {"server_version": "17.0", "server_version_info": [17, 0, 0, "final", 0, ""]}}
        return 200, json.dumps(payload).encode()

    monkeypatch.setattr(transport, "_urlopen", fake_urlopen)
    info = probe(URL, 5.0)
    assert info.major == 17
    assert info.source == "/jsonrpc common.version"


def test_jsonrpc_rejects_a_non_dict_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "_urlopen", lambda _req, _timeout: (200, b'"surprise"'))
    client = JsonRpcTransport(URL, "db", "admin", "secret")
    with pytest.raises(TransportError, match="returned"):
        client._rpc("object", "execute_kw", [])


def test_json2_authenticate_is_cached() -> None:
    """context_get costs a round trip; the uid does not change mid-run."""
    client = Json2Transport(URL, "db", "key")
    client.uid = 9
    assert client.authenticate() == 9


def test_json2_rejects_a_context_without_a_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Json2Transport(URL, "db", "key")
    monkeypatch.setattr(client, "call", lambda *_a, **_kw: {"lang": "en_US"})
    with pytest.raises(AuthError, match="context_get returned"):
        client.authenticate()


def test_abstract_transport_has_no_implementation() -> None:
    """The base class defines the interface; every method must be overridden."""
    base = Transport()
    with pytest.raises(NotImplementedError):
        base.authenticate()
    with pytest.raises(NotImplementedError):
        base.call("res.partner", "read")
    with pytest.raises(NotImplementedError):
        base.create("res.partner", {})


def test_transport_can_be_forced_to_json2() -> None:
    chosen, why = build_transport(URL, "db", "admin", "key", "api_key", preference="json2")
    assert isinstance(chosen, Json2Transport)
    assert why == "forced by config"


# --- config: git and TOML failures --------------------------------------------


def test_run_git_returns_none_when_git_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def no_git(*_a: object, **_kw: object) -> None:
        raise OSError("git not found")

    monkeypatch.setattr(config.subprocess, "run", no_git)
    assert config._run_git(["status"], tmp_path) is None


def test_main_checkout_is_none_without_worktree_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(config, "_run_git", lambda *_a, **_kw: None)
    assert config.find_main_checkout(tmp_path) is None


def test_malformed_toml_names_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "broken.toml"
    bad.write_text("this is [not toml")
    bad.chmod(0o600)
    with pytest.raises(ConfigError, match="is not valid TOML"):
        config.load_toml(bad)


def test_profiles_table_must_be_a_table() -> None:
    with pytest.raises(ConfigError, match="must be a table"):
        config._profiles_of({"profiles": "a string"})


def test_password_is_used_when_no_api_key_is_present(tmp_path: Path) -> None:
    """Classic RPC authenticates with a password, so it stays a valid credential."""
    secret, kind = config._resolve_secret({"password": "hunter2"}, tmp_path / "c.toml")
    assert (secret, kind) == ("hunter2", "password")


def test_profile_without_a_url_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing a 'url'"):
        config._build_profile("p", {"db": "x"}, tmp_path / "c.toml")


def test_a_lone_profile_becomes_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "one.toml"
    source.write_text('[profiles.only]\nurl = "http://localhost:8069"\n')
    source.chmod(0o600)
    assert config._default_profile_name([source]) == "only"


def test_no_default_when_several_profiles_and_no_key(tmp_path: Path) -> None:
    source = tmp_path / "two.toml"
    source.write_text('[profiles.a]\nurl = "http://a"\n\n[profiles.b]\nurl = "http://b"\n')
    source.chmod(0o600)
    assert config._default_profile_name([source]) is None


# --- config: odoo.conf and worktree derivation --------------------------------


def test_unreadable_odoo_conf_is_reported(tmp_path: Path) -> None:
    conf = tmp_path / "odoo.conf"
    conf.write_text("[options\nhttp_port = 8069\n")
    with pytest.raises(ConfigError, match="not a readable odoo.conf"):
        config._read_odoo_conf(conf)


def test_odoo_conf_without_an_options_section_yields_nothing(tmp_path: Path) -> None:
    conf = tmp_path / "odoo.conf"
    conf.write_text("[other]\nkey = value\n")
    assert config._read_odoo_conf(conf) == (None, None)


def _porcelain(*paths: Path) -> str:
    return "".join(f"worktree {p}\nHEAD abc123\nbranch refs/heads/x\n\n" for p in paths)


def test_worktree_index_is_the_position_in_the_listing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The port offset is this position; getting it wrong picks another database."""
    main = tmp_path / "repo"
    repo = tmp_path / "repo-feature"
    main.mkdir()
    repo.mkdir()
    monkeypatch.setattr(config, "_run_git", lambda *_a, **_kw: _porcelain(main, repo))
    assert config._worktree_index(repo, main) == 2


def test_worktree_index_matches_the_full_path_not_the_directory_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two worktrees can share a final segment; the first namesake is a different
    instance, and its port belongs to whatever is running there."""
    main = tmp_path / "main"
    other = tmp_path / "elsewhere" / "feature-x"
    repo = tmp_path / "ours" / "feature-x"
    for path in (main, other, repo):
        path.mkdir(parents=True)
    monkeypatch.setattr(config, "_run_git",
                        lambda *_a, **_kw: _porcelain(main, other, repo))
    assert config._worktree_index(repo, main) == 3


def test_worktree_index_refuses_without_a_listing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Position 1 is the main checkout, so guessing it targets a real instance."""
    monkeypatch.setattr(config, "_run_git", lambda *_a, **_kw: None)
    with pytest.raises(ConfigError, match="port cannot be determined"):
        config._worktree_index(tmp_path, tmp_path)


def test_worktree_index_refuses_when_absent_from_the_listing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(config, "_run_git",
                        lambda *_a, **_kw: "worktree /elsewhere\nHEAD abc\n\n")
    with pytest.raises(ConfigError, match="does not appear in"):
        config._worktree_index(tmp_path / "repo", tmp_path)


def test_wt_settings_needs_an_odoo_section(tmp_path: Path) -> None:
    (tmp_path / config.WT_CONFIG_NAME).write_text('[other]\nkey = "value"\n')
    with pytest.raises(ConfigError, match="no \\[odoo\\] section"):
        config._wt_settings(tmp_path)


def test_derive_main_needs_a_conf_entry(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no \\[odoo\\].conf entry"):
        config._derive_main(tmp_path, {"base_port": 50000})


def test_derive_connection_outside_a_repository_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(config, "find_git_root", lambda _s: None)
    with pytest.raises(ConfigError, match="not inside a git repository"):
        config.derive_connection(tmp_path)


def test_derive_connection_uses_the_worktree_path_for_a_linked_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A linked worktree gets base_port + position, never the main port."""
    main = tmp_path / "repo"
    repo = tmp_path / "repo-feature"
    main.mkdir()
    repo.mkdir()
    (main / config.WT_CONFIG_NAME).write_text("[odoo]\nbase_port = 50000\n")
    monkeypatch.setattr(config, "find_git_root", lambda _s: repo)
    monkeypatch.setattr(config, "find_main_checkout", lambda _s: main)
    monkeypatch.setattr(config, "_worktree_index", lambda _r, _m: 3)
    monkeypatch.setattr(config, "_run_git", lambda args, _cwd: "feature/x" if "rev-parse" in args else None)

    url, db, why = config.derive_connection(repo)
    assert url == "http://localhost:50003"
    assert db == "dev_repo_feature_x"
    assert "position 3" in why


def test_git_exclude_is_a_no_op_without_a_git_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(config, "_run_git", lambda *_a, **_kw: None)
    assert config._git_exclude(tmp_path, "/.odoo-rpc.toml") is False


def test_project_config_search_stops_at_the_filesystem_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Outside a repository the walk must terminate rather than loop at '/'."""
    monkeypatch.setattr(config, "find_git_root", lambda _s: None)
    assert config.find_project_config(tmp_path) is None


# --- symbols exercised directly ------------------------------------------------


def test_check_permissions_accepts_an_owner_only_file(tmp_path: Path) -> None:
    """0600 is the only mode a credential file may carry."""
    secure = tmp_path / "ok.toml"
    secure.write_text("")
    secure.chmod(0o600)
    assert config.check_permissions(secure) is None


def test_check_permissions_refuses_a_group_readable_file(tmp_path: Path) -> None:
    exposed = tmp_path / "leaky.toml"
    exposed.write_text("")
    exposed.chmod(0o640)
    with pytest.raises(ConfigError, match="readable by group or other"):
        config.check_permissions(exposed)


def test_count_check_carries_its_domain_into_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dummy mail server neutralization inserts must not read as live."""
    check = neutralize.CountCheck(
        key="mail_servers",
        label="Live outgoing mail servers",
        model="ir.mail_server",
        domain=[("active", "=", True), ("smtp_host", "!=", "invalid")],
        remedy="mail would actually be delivered",
    )
    seen: list[tuple[str, list[Any]]] = []

    class Counting(OdooClient):
        def search_count(self, model: str, domain: list[Any]) -> int:
            seen.append((model, domain))
            return 0

    result = neutralize._run_count_check(Counting(RecordingTransport()), check)
    assert result.outcome == PASS
    assert seen[0][0] == "ir.mail_server"
    assert ("smtp_host", "!=", "invalid") in seen[0][1]


def test_count_check_reports_the_remedy_when_something_is_live() -> None:
    check = neutralize.CountCheck("crons", "Crons", "ir.cron", [],
                                  "scheduled jobs would fire")

    class Counting(OdooClient):
        def search_count(self, model: str, domain: list[Any]) -> int:
            return 2

    result = neutralize._run_count_check(Counting(RecordingTransport()), check)
    assert result.outcome == FAIL
    assert "scheduled jobs would fire" in result.detail


def test_check_result_line_shows_outcome_label_and_detail() -> None:
    rendered = CheckResult("crons", "Live scheduled jobs", FAIL, "2 active").line()
    assert "FAIL" in rendered
    assert "Live scheduled jobs" in rendered
    assert "2 active" in rendered


def test_check_result_line_omits_an_empty_detail() -> None:
    assert CheckResult("flag", "Flag", PASS).line().rstrip().endswith("Flag")


def test_short_name_drops_the_module_path() -> None:
    """Reports name the exception, not odoo.exceptions.AccessError in full."""
    assert OdooError("odoo.exceptions.AccessError", "no").short_name() == "AccessError"


def test_short_name_falls_back_when_the_name_is_empty() -> None:
    assert OdooError("", "no").short_name() == "OdooError"


def test_a_file_url_is_refused_before_it_is_opened() -> None:
    """urllib would happily read a local file and return its bytes as the answer."""
    request = urllib.request.Request("file:///etc/passwd")
    with pytest.raises(TransportError, match="speaks only http, https"):
        transport._urlopen(request, 5.0)


def test_https_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transport.urllib.request,
        "urlopen",
        lambda _req, timeout=None: FakeResponse(200, b"{}"),
    )
    request = urllib.request.Request("https://odoo.example.com/jsonrpc")
    assert transport._urlopen(request, 5.0) == (200, b"{}")


# --- review findings: the guard allowlist -------------------------------------


@pytest.mark.parametrize("method", sorted(READ_METHODS))
def test_every_allowlisted_method_reaches_the_transport_unguarded(method: str) -> None:
    """Each entry is a promise that the method needs no --write; pin all of them."""
    recording = RecordingTransport()
    client = OdooClient(recording, allow_write=False)
    client.call("res.partner", method)
    assert recording.calls[0][1] == method


def test_a_method_outside_the_allowlist_is_refused_without_write() -> None:
    client = OdooClient(RecordingTransport(), allow_write=False)
    with pytest.raises(GuardError, match="re-run with --write"):
        client.call("res.partner", "action_custom_thing")


def test_search_read_omits_fields_when_none_are_requested() -> None:
    """fields=None is not the same request as omitting the key."""
    recording = RecordingTransport()
    OdooClient(recording).search_read("res.partner", [])
    assert "fields" not in recording.calls[0][4]


# --- review findings: JSON-2 status handling ----------------------------------


def _json2_answering(monkeypatch: pytest.MonkeyPatch, status: int,
                     payload: object) -> Json2Transport:
    monkeypatch.setattr(
        transport, "_post_json", lambda *_a, **_kw: (status, payload))
    return Json2Transport(URL, "db", "key")


def test_401_is_always_a_credential_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _json2_answering(monkeypatch, 401, {"name": "odoo.exceptions.AccessDenied",
                                                 "message": "no"})
    with pytest.raises(AuthError, match="rejected the credential"):
        client.call("res.partner", "read", ids=[1])


def test_403_from_a_bad_key_is_a_credential_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _json2_answering(monkeypatch, 403, {"name": "odoo.exceptions.AccessDenied",
                                                 "message": "Access Denied"})
    with pytest.raises(AuthError, match="rejected the credential"):
        client.call("res.partner", "read", ids=[1])


def test_403_from_a_permission_refusal_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Odoo maps AccessError to 403 too; calling that a bad key misdirects."""
    client = _json2_answering(
        monkeypatch, 403,
        {"name": "odoo.exceptions.AccessError",
         "message": "You are not allowed to access 'Partner' records."})
    with pytest.raises(OdooError) as excinfo:
        client.call("res.partner", "read", ids=[1])
    assert excinfo.value.short_name() == "AccessError"
    assert "not allowed" in excinfo.value.message


def test_403_without_a_usable_body_stays_a_credential_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _json2_answering(monkeypatch, 403, "forbidden")
    with pytest.raises(AuthError):
        client.call("res.partner", "read", ids=[1])


def test_a_non_dict_error_body_becomes_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 carrying a bare JSON string must not be fed to the error unwrapper."""
    client = _json2_answering(monkeypatch, 500, ["unexpected"])
    with pytest.raises(TransportError, match="HTTP 500"):
        client.call("res.partner", "read", ids=[1])


def test_a_dict_error_body_is_unwrapped_into_an_odoo_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _json2_answering(monkeypatch, 500, {"name": "odoo.exceptions.UserError",
                                                 "message": "boom"})
    with pytest.raises(OdooError, match="boom"):
        client.call("res.partner", "read", ids=[1])


# --- review findings: probe robustness ----------------------------------------


def test_a_jsonrpc_envelope_without_a_mapping_is_not_an_odoo_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`{"result": [...]}` passes the envelope check but is not version info."""

    def fake_urlopen(request: urllib.request.Request, _timeout: float) -> tuple[int, bytes]:
        if "/web/version" in request.full_url:
            raise TransportError("cannot reach")
        return 200, json.dumps({"result": [1, 2]}).encode()

    monkeypatch.setattr(transport, "_urlopen", fake_urlopen)
    with pytest.raises(TransportError, match="does not look like an Odoo server"):
        probe(URL, 5.0)


# --- review findings: the neutralization verdict ------------------------------


def test_an_inconclusive_audit_withholds_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled payment providers stay live and invisible; that is not proof."""
    client = OdooClient(RecordingTransport())
    monkeypatch.setattr(neutralize, "_get_param",
                        lambda _c, key: "True" if key == neutralize.NEUTRALIZE_FLAG
                        else "http://localhost:8069")
    monkeypatch.setattr(
        neutralize, "_run_count_check",
        lambda _c, check: CheckResult(check.key, check.label, SKIPPED,
                                      "denied", inconclusive=True)
        if check.key == "payment"
        else CheckResult(check.key, check.label, PASS))
    monkeypatch.setattr(neutralize, "_check_crons",
                        lambda _c: CheckResult("crons", "Crons", PASS))

    verdict = neutralize.evaluate(client)
    assert verdict.status == neutralize.UNVERIFIED
    assert verdict.allows_write is False


def test_an_uninstalled_model_still_allows_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent module has no live surface, so its skip is evidence of absence."""
    client = OdooClient(RecordingTransport())
    monkeypatch.setattr(neutralize, "_get_param",
                        lambda _c, key: "True" if key == neutralize.NEUTRALIZE_FLAG
                        else "http://localhost:8069")
    monkeypatch.setattr(
        neutralize, "_run_count_check",
        lambda _c, check: CheckResult(check.key, check.label, SKIPPED, "not installed")
        if check.key == "payment"
        else CheckResult(check.key, check.label, PASS))
    monkeypatch.setattr(neutralize, "_check_crons",
                        lambda _c: CheckResult("crons", "Crons", PASS))

    assert neutralize.evaluate(client).status == neutralize.NEUTRALIZED


def test_an_access_error_skip_is_inconclusive() -> None:
    check = neutralize.CountCheck("payment", "Providers", "payment.provider", [],
                                  "real payments could be taken")

    class Denying(OdooClient):
        def search_count(self, model: str, domain: list[Any]) -> int:
            raise OdooError("odoo.exceptions.AccessError", "not allowed")

    result = neutralize._run_count_check(Denying(RecordingTransport()), check)
    assert result.outcome == SKIPPED
    assert result.inconclusive is True


def test_a_missing_model_skip_is_conclusive() -> None:
    check = neutralize.CountCheck("payment", "Providers", "payment.provider", [],
                                  "real payments could be taken")

    class Absent(OdooClient):
        def search_count(self, model: str, domain: list[Any]) -> int:
            raise OdooError("odoo.exceptions.MissingError", "doesn't exist")

    result = neutralize._run_count_check(Absent(RecordingTransport()), check)
    assert result.inconclusive is False


# --- review findings: worktree port resolution --------------------------------


def test_a_client_worktree_uses_the_client_base_port() -> None:
    """wt gives a named client its own port; the global one is a different server."""
    odoo = {"base_port": 9069}
    clients = {"internal-crm": {"base_port": 8071}}
    assert config._resolve_base_port(odoo, clients, "internal-crm") == 8071


def test_a_client_without_its_own_port_uses_wt_client_default() -> None:
    assert config._resolve_base_port({"base_port": 9069},
                                     {"internal-crm": {}}, "internal-crm") == 10000


def test_wt_base_port_env_overrides_the_global_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WT_BASE_PORT", "7000")
    assert config._resolve_base_port({"base_port": 9069}, {}, "") == 7000


def test_wt_base_port_env_does_not_override_a_client_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wt applies the env override to the global port only."""
    monkeypatch.setenv("WT_BASE_PORT", "7000")
    assert config._resolve_base_port({"base_port": 9069},
                                     {"crm": {"base_port": 8071}}, "crm") == 8071


def test_a_missing_base_port_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WT_BASE_PORT", raising=False)
    with pytest.raises(ConfigError, match="base_port is missing"):
        config._resolve_base_port({}, {}, "")


# --- review findings: profile names and domain shapes -------------------------


@pytest.mark.parametrize(
    ("repo_name", "expected"),
    [
        (".claude", "claude"),
        ("hubcraft_tms", "hubcraft-tms"),
        ("PRI_Odoo", "pri-odoo"),
        ("my.project.v2", "my-project-v2"),
        ("...", "default"),
    ],
)
def test_profile_name_is_always_a_usable_toml_key(repo_name: str,
                                                  expected: str) -> None:
    """A dot would open a nested table that --profile can never address."""
    assert config._profile_name_for(repo_name) == expected


def test_init_writes_a_loadable_profile_for_a_dotted_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The generated file must parse and resolve — this repo is named '.claude'."""
    repo = tmp_path / ".claude"
    repo.mkdir()
    monkeypatch.setattr(config, "find_git_root", lambda _s: repo)
    monkeypatch.setattr(
        config, "derive_connection",
        lambda _s: ("http://localhost:9070", "dev_db", "worktree: test"))
    monkeypatch.setattr(config, "_git_exclude", lambda *_a: True)

    target, _why = config.init_project_config(repo)
    profiles = config.list_profiles(repo)
    assert [name for name, _src in profiles] == ["claude"]
    assert config.load_profile("claude", start=repo).url == "http://localhost:9070"
    assert target.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("raw", ["false", "0", '""', "{}", "null"])
def test_a_domain_that_is_not_a_list_is_refused(raw: str) -> None:
    """`or []` would turn each of these into an unfiltered whole-model query."""
    if raw == "null":
        assert odoo_rpc._parse_domain(raw) == []
        return
    with pytest.raises(ConfigError, match="--domain must be a JSON list"):
        odoo_rpc._parse_domain(raw)


def test_an_omitted_domain_is_the_empty_domain() -> None:
    assert odoo_rpc._parse_domain(None) == []


def test_a_real_domain_survives() -> None:
    assert odoo_rpc._parse_domain('[["is_company","=",true]]') == [
        ["is_company", "=", True]]


def test_create_values_may_be_an_object_or_a_list() -> None:
    assert odoo_rpc._parse_typed_json('{"name":"a"}', "values", (dict, list),
                                      "object or list") == {"name": "a"}
    assert odoo_rpc._parse_typed_json('[{"name":"a"}]', "values", (dict, list),
                                      "object or list") == [{"name": "a"}]


def test_create_values_reject_a_scalar() -> None:
    with pytest.raises(ConfigError, match="must be a JSON object or list"):
        odoo_rpc._parse_typed_json("5", "values", (dict, list), "object or list")


def test_write_values_must_be_an_object() -> None:
    with pytest.raises(ConfigError, match="--values must be a JSON object"):
        odoo_rpc._parse_typed_json('[{"name":"a"}]', "values", dict, "object")


# --- review findings: credential override and unknown clients -----------------


def test_api_key_flag_rescues_an_unset_env_indirection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--api-key is documented as an override, so it must survive this."""
    source = tmp_path / ".odoo-rpc.toml"
    source.write_text(
        '[profiles.p]\nurl = "http://localhost:8069"\n'
        'db = "d"\nlogin = "admin"\napi_key_env = "NOT_SET_ANYWHERE"\n'
    )
    source.chmod(0o600)
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    monkeypatch.setattr(config, "find_project_config", lambda _s: source)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")

    profile = config.load_profile("p", start=tmp_path, api_key="supplied")
    assert profile.secret == "supplied"
    assert profile.secret_kind == "api_key"


def test_an_unset_env_indirection_without_an_override_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Silence here would authenticate as nobody and fail with a bare 403."""
    source = tmp_path / ".odoo-rpc.toml"
    source.write_text(
        '[profiles.p]\nurl = "http://localhost:8069"\napi_key_env = "NOT_SET_ANYWHERE"\n'
    )
    source.chmod(0o600)
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    monkeypatch.setattr(config, "find_project_config", lambda _s: source)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")

    with pytest.raises(ConfigError, match="empty or unset"):
        config.load_profile("p", start=tmp_path)


def test_an_unknown_client_is_refused(tmp_path: Path) -> None:
    """wt exits on an unknown client; inheriting the global port pairs a
    client-scoped database name with a different instance."""
    with pytest.raises(ConfigError, match="does not define"):
        config._resolve_base_port({"base_port": 9069},
                                  {"internal-crm": {"base_port": 8071}}, "typo-crm")


def test_the_refusal_lists_the_clients_that_do_exist(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="internal-crm"):
        config._resolve_base_port({"base_port": 9069},
                                  {"internal-crm": {}}, "wrong")


def test_no_client_file_still_uses_the_global_port() -> None:
    assert config._resolve_base_port({"base_port": 9069}, {"crm": {}}, "") == 9069


# --- review findings: the JSON-2 database header ------------------------------


def test_json2_omits_the_database_header_when_no_db_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank or defaulted header can route the call to a different database."""
    seen: dict[str, str] = {}

    def capture(_url: str, _body: object, _timeout: float,
                headers: dict[str, str] | None = None) -> tuple[int, object]:
        seen.update(headers or {})
        return 200, {}

    monkeypatch.setattr(transport, "_post_json", capture)
    Json2Transport(URL, None, "key").call("res.partner", "read", ids=[1])
    assert "X-Odoo-Database" not in seen
    assert seen["Authorization"] == "bearer key"


def test_json2_sends_the_database_header_when_one_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def capture(_url: str, _body: object, _timeout: float,
                headers: dict[str, str] | None = None) -> tuple[int, object]:
        seen.update(headers or {})
        return 200, {}

    monkeypatch.setattr(transport, "_post_json", capture)
    Json2Transport(URL, "testdb", "key").call("res.partner", "read", ids=[1])
    assert seen["X-Odoo-Database"] == "testdb"


def test_an_unexpected_error_is_inconclusive_not_absence() -> None:
    """A validation error says nothing about whether the surface is live."""
    check = neutralize.CountCheck("webhooks", "Webhooks", "ir.actions.server", [],
                                  "webhooks would fire at external systems")

    class Odd(OdooClient):
        def search_count(self, model: str, domain: list[Any]) -> int:
            raise OdooError("odoo.exceptions.ValidationError", "unknown field")

    result = neutralize._run_count_check(Odd(RecordingTransport()), check)
    assert result.outcome == SKIPPED
    assert result.inconclusive is True


def test_a_cron_query_error_is_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OdooClient(RecordingTransport())

    def boom(*_a: object, **_kw: object) -> None:
        raise OdooError("odoo.exceptions.ValidationError", "broken")

    monkeypatch.setattr(client, "raw_call", boom)
    result = neutralize._check_crons(client)
    assert result.outcome == SKIPPED
    assert result.inconclusive is True


def test_a_detached_worktree_does_not_share_one_database_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Every detached checkout answers 'HEAD'; they would all collide."""
    main = tmp_path / "proj"
    repo = tmp_path / "feature_x"
    main.mkdir()
    repo.mkdir()
    monkeypatch.setattr(config, "_worktree_index", lambda _r, _m: 2)
    monkeypatch.setattr(config, "_run_git", lambda _args, _cwd: "HEAD")
    _url, db, _why = config._derive_worktree(repo, main, {"base_port": 8400}, {})
    assert db == "dev_proj_feature_x"
    assert "HEAD" not in db


# --- review findings: relative conf paths, dbless init, cached auth, reused probe


def test_a_relative_conf_path_resolves_from_the_main_checkout(
    tmp_path: Path,
) -> None:
    """Resolving from the process directory would read a different project's conf."""
    main = tmp_path / "proj"
    (main / "deploy").mkdir(parents=True)
    (main / "deploy" / "odoo-local.conf").write_text(
        "[options]\nhttp_port = 9100\ndb_name = proj_dev\n")
    url, db, why = config._derive_main(main, {"conf": "deploy/odoo-local.conf"})
    assert url == "http://localhost:9100"
    assert db == "proj_dev"
    assert "http_port from" in why


def test_an_absolute_conf_path_is_used_as_given(tmp_path: Path) -> None:
    conf = tmp_path / "elsewhere.conf"
    conf.write_text("[options]\nhttp_port = 8169\ndb_name = other\n")
    url, db, _why = config._derive_main(tmp_path / "proj", {"conf": str(conf)})
    assert (url, db) == ("http://localhost:8169", "other")


def test_init_writes_a_loadable_profile_when_the_conf_names_no_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A port without a db_name must still produce a file that parses."""
    repo = tmp_path / "proj"
    repo.mkdir()
    monkeypatch.setattr(config, "find_git_root", lambda _s: repo)
    monkeypatch.setattr(
        config, "derive_connection",
        lambda _s: ("http://localhost:9100", None, "main checkout: test"))
    monkeypatch.setattr(config, "_git_exclude", lambda *_a: True)

    target, _why = config.init_project_config(repo)
    assert "not found in odoo.conf" in target.read_text()
    profile = config.load_profile("proj", start=repo)
    assert profile.db is None
    assert profile.url == "http://localhost:9100"


def test_classic_authenticate_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second credential round trip per call would double every request."""
    calls: list[str] = []

    def fake_rpc(_self: object, service: str, _method: str,
                 _args: list[object]) -> object:
        calls.append(service)
        return 7

    monkeypatch.setattr(JsonRpcTransport, "_rpc", fake_rpc)
    client = JsonRpcTransport(URL, "db", "admin", "secret")
    assert client.authenticate() == 7
    assert client.authenticate() == 7
    assert calls == ["common"]


def test_build_transport_reuses_a_probe_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI probes once and passes the result; probing again costs a round trip."""

    def fail(*_a: object, **_kw: object) -> ServerInfo:
        raise AssertionError("probe called despite server= being supplied")

    monkeypatch.setattr(transport, "probe", fail)
    chosen, why = build_transport(
        URL, "db", "admin", "key", "api_key",
        server=ServerInfo("19.0", [19, 0], "/web/version"))
    assert isinstance(chosen, Json2Transport)
    assert "19" in why


def test_build_transport_probes_when_no_server_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        transport, "probe",
        lambda _url, _timeout: ServerInfo("17.0", [17, 0], "/jsonrpc"))
    chosen, why = build_transport(URL, "db", "admin", "pw", "password")
    assert isinstance(chosen, JsonRpcTransport)
    assert "predates JSON-2" in why


# --- review findings: malformed endpoints and empty URLs ----------------------


def test_an_empty_url_is_refused_with_a_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """urllib treats '' as a relative path and raises before any guard runs."""
    monkeypatch.setattr(config, "find_project_config", lambda _s: None)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")
    with pytest.raises(ConfigError, match="resolved URL is empty"):
        config.load_profile(url="", start=tmp_path)


def test_a_profile_url_of_just_a_slash_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = tmp_path / ".odoo-rpc.toml"
    source.write_text('[profiles.p]\nurl = "/"\n')
    source.chmod(0o600)
    monkeypatch.setattr(config, "find_project_config", lambda _s: source)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")
    with pytest.raises(ConfigError, match="resolved URL is empty"):
        config.load_profile("p", start=tmp_path)


def test_a_200_without_version_info_falls_back_to_classic_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON object is not proof: only version_info marks the Odoo 19 route."""

    def fake_urlopen(request: urllib.request.Request, _timeout: float) -> tuple[int, bytes]:
        if "/web/version" in request.full_url:
            return 200, json.dumps({"something": "else"}).encode()
        payload = {"result": {"server_version": "17.0",
                              "server_version_info": [17, 0]}}
        return 200, json.dumps(payload).encode()

    monkeypatch.setattr(transport, "_urlopen", fake_urlopen)
    info = probe(URL, 5.0)
    assert info.source == "/jsonrpc common.version"
    assert info.major == 17


@pytest.mark.parametrize("bad", [19, "19.0", {"major": 19}])
def test_a_malformed_version_info_is_reported_not_crashed(
    monkeypatch: pytest.MonkeyPatch, bad: object,
) -> None:
    """list(19) raises TypeError, which would surface as a traceback."""
    monkeypatch.setattr(
        transport, "_urlopen",
        lambda _req, _timeout: (200, json.dumps(
            {"version": "19.0", "version_info": bad}).encode()))
    with pytest.raises(TransportError, match="not a list"):
        probe(URL, 5.0)


def test_a_jsonrpc_version_info_of_the_wrong_shape_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: urllib.request.Request, _timeout: float) -> tuple[int, bytes]:
        if "/web/version" in request.full_url:
            raise TransportError("cannot reach")
        return 200, json.dumps(
            {"result": {"server_version": "17.0", "server_version_info": 17}}).encode()

    monkeypatch.setattr(transport, "_urlopen", fake_urlopen)
    with pytest.raises(TransportError, match="not a list"):
        probe(URL, 5.0)


def test_an_absent_version_info_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The field is optional; only a wrong type is an error."""
    monkeypatch.setattr(
        transport, "_urlopen",
        lambda _req, _timeout: (200, json.dumps(
            {"version": "19.0", "version_info": []}).encode()))
    assert probe(URL, 5.0).major == 19


def test_profiles_prefers_the_project_file_as_a_duplicate_name_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`odoo-rpc profiles` names the file whose credential actually wins."""
    project = tmp_path / ".odoo-rpc.toml"
    project.write_text('[profiles.shared]\nurl = "http://project"\n')
    project.chmod(0o600)
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text('[profiles.shared]\nurl = "http://global"\n')
    global_cfg.chmod(0o600)
    monkeypatch.setattr(config, "find_project_config", lambda _s: project)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", global_cfg)

    listed = config.list_profiles(tmp_path)
    assert listed == [("shared", project)]
    assert config.load_profile("shared", start=tmp_path).url == "http://project"

#!/usr/bin/env python3
"""Tests for the write guard.

--write is reachable through four distinct paths — create, write, unlink, and a
non-allowlisted method sent through `call` — so each one gets its own test, and
each must fail if the guard is removed.
"""

from __future__ import annotations

from typing import Any

import pytest

from client import GuardError, OdooClient, is_mutating
from transport import JsonValue, Transport


class FakeTransport(Transport):
    """Records what actually reached the wire."""

    name = "fake"

    def __init__(self, result: JsonValue = True) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def authenticate(self) -> int:
        return 2

    def call(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> JsonValue:
        self.calls.append((model, method))
        return self.result


class StubVerdict:
    def __init__(self, allows_write: bool, text: str = "LIVE: active cron") -> None:
        self._allows = allows_write
        self._text = text
        self.evaluations = 0

    @property
    def allows_write(self) -> bool:
        return self._allows

    def explain(self) -> str:
        return self._text


def make_client(allow_write: bool = False, neutralized: bool = True,
                override: bool = False) -> tuple[OdooClient, FakeTransport, StubVerdict]:
    fake = FakeTransport()
    verdict = StubVerdict(neutralized)

    def gate(_client: OdooClient) -> StubVerdict:
        verdict.evaluations += 1
        return verdict

    return OdooClient(fake, allow_write=allow_write, override=override, gate=gate), fake, verdict


# --- classification -----------------------------------------------------------

@pytest.mark.parametrize("method", ["search", "search_read", "read", "fields_get",
                                    "read_group", "search_count", "name_search"])
def test_read_methods_are_not_mutating(method: str) -> None:
    assert is_mutating(method) is False


@pytest.mark.parametrize("method", ["create", "write", "unlink", "action_confirm",
                                    "send_mail", "some_custom_addon_method"])
def test_everything_else_is_mutating(method: str) -> None:
    """The guard fails closed: an unknown method is assumed to write."""
    assert is_mutating(method) is True


# --- the four paths to --write ------------------------------------------------

@pytest.mark.parametrize("method", ["create", "write", "unlink"])
def test_mutating_method_is_refused_without_the_write_flag(method: str) -> None:
    client, fake, _ = make_client(allow_write=False)
    with pytest.raises(GuardError, match="--write"):
        client.call("res.partner", method, ids=[1], args=[{"name": "x"}])
    assert fake.calls == []


def test_escape_hatch_cannot_walk_around_the_guard() -> None:
    """`call` with an arbitrary method is the fourth path and must be gated."""
    client, fake, _ = make_client(allow_write=False)
    with pytest.raises(GuardError, match="--write"):
        client.call("res.partner", "action_archive", ids=[1])
    assert fake.calls == []


def test_allowlisted_method_through_call_needs_no_write_flag() -> None:
    client, fake, _ = make_client(allow_write=False)
    client.call("res.partner", "search_read", kwargs={"domain": []})
    assert fake.calls == [("res.partner", "search_read")]


# --- the gate, on top of the guard --------------------------------------------

def test_reads_run_against_a_live_database_ungated() -> None:
    """Reading a live database breaks nothing, so the gate never runs for it."""
    client, fake, verdict = make_client(allow_write=False, neutralized=False)
    client.search_read("res.partner", [], fields=["name"])
    assert fake.calls == [("res.partner", "search_read")]
    assert verdict.evaluations == 0


def test_write_flag_alone_is_not_enough_on_a_live_database() -> None:
    client, fake, _ = make_client(allow_write=True, neutralized=False)
    with pytest.raises(GuardError, match="not verifiably neutralized"):
        client.call("res.partner", "create", kwargs={"vals": {"name": "x"}})
    assert fake.calls == []


def test_refusal_explains_what_the_gate_found() -> None:
    client, _fake, _ = make_client(allow_write=True, neutralized=False)
    with pytest.raises(GuardError, match="active cron"):
        client.call("res.partner", "create", kwargs={"vals": {}})


def test_write_succeeds_on_a_neutralized_database() -> None:
    client, fake, _ = make_client(allow_write=True, neutralized=True)
    client.call("res.partner", "create", kwargs={"vals": {"name": "x"}})
    assert fake.calls == [("res.partner", "create")]


def test_override_plus_write_executes_against_a_live_database() -> None:
    """Both layers deliberately satisfied is a fully-spelled instruction."""
    client, fake, _ = make_client(allow_write=True, neutralized=False, override=True)
    client.call("res.partner", "create", kwargs={"vals": {"name": "x"}})
    assert fake.calls == [("res.partner", "create")]


def test_override_without_write_still_refuses() -> None:
    """The override answers 'where', never 'whether'."""
    client, fake, _ = make_client(allow_write=False, neutralized=False, override=True)
    with pytest.raises(GuardError, match="--write"):
        client.call("res.partner", "create", kwargs={"vals": {}})
    assert fake.calls == []


def test_gate_is_evaluated_once_per_client() -> None:
    client, _fake, verdict = make_client(allow_write=True, neutralized=True)
    client.call("res.partner", "create", kwargs={"vals": {}})
    client.call("res.partner", "write", ids=[1], kwargs={"vals": {}})
    assert verdict.evaluations == 1


def test_raw_call_bypasses_the_guard_for_the_gate_itself() -> None:
    """The gate must read ir.config_parameter before any verdict exists."""
    client, fake, verdict = make_client(allow_write=False, neutralized=False)
    client.raw_call("ir.config_parameter", "search_read", kwargs={"domain": []})
    assert fake.calls == [("ir.config_parameter", "search_read")]
    assert verdict.evaluations == 0


class RecordingTransport(FakeTransport):
    """Also records create(), which does not go through call()."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[tuple[str, object]] = []

    def create(self, model: str, values: JsonValue) -> JsonValue:
        self.created.append((model, values))
        return 1


def test_create_helper_is_refused_without_the_write_flag() -> None:
    """create bypasses call() to fix argument passing; the guard must still apply."""
    transport = RecordingTransport()
    client = OdooClient(transport, allow_write=False, gate=lambda _c: StubVerdict(True))
    with pytest.raises(GuardError, match="--write"):
        client.create("res.partner", {"name": "x"})
    assert transport.created == []


def test_create_helper_is_refused_on_a_live_database() -> None:
    transport = RecordingTransport()
    client = OdooClient(transport, allow_write=True, gate=lambda _c: StubVerdict(False))
    with pytest.raises(GuardError, match="not verifiably neutralized"):
        client.create("res.partner", {"name": "x"})
    assert transport.created == []


def test_create_helper_runs_when_both_layers_are_satisfied() -> None:
    transport = RecordingTransport()
    client = OdooClient(transport, allow_write=True, gate=lambda _c: StubVerdict(True))
    assert client.create("res.partner", {"name": "x"}) == 1
    assert transport.created == [("res.partner", {"name": "x"})]

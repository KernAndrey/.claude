#!/usr/bin/env python3
"""The guarded Odoo client: classifies calls and gates the mutating ones.

Two independent things have to be true before a write reaches a database:

    1. the caller asked for a write     -> --write        (this module)
    2. the database is safe to write to -> neutralization (neutralize.py)

Reads are never gated. Reading from a live database breaks nothing, and gating
reads would train the override flag to be typed reflexively until it stops
meaning anything.

The gate is injected rather than imported so this module stays independent of
how a verdict is reached.

Usage:
    from client import OdooClient, is_mutating
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from transport import JsonValue, Transport

# Methods that only read. Everything else — including custom model methods
# reached through `call` — counts as mutating, so the guard fails closed.
READ_METHODS = frozenset(
    {
        "search",
        "search_read",
        "search_count",
        "read",
        "read_group",
        "fields_get",
        "name_search",
        "name_get",
        "default_get",
        "context_get",
        "get_views",
        "get_view",
        "web_read_group",
        "web_search_read",
    }
)


class GuardError(Exception):
    """A mutating call was refused before it reached the server."""


class Verdict(Protocol):
    """What the neutralization gate reports back."""

    @property
    def allows_write(self) -> bool: ...

    def explain(self) -> str: ...


def is_mutating(method: str) -> bool:
    """True when a method may change data.

    Unknown methods are treated as mutating: an escape hatch that let any
    unrecognised name through would walk straight around the guard.
    """
    return method not in READ_METHODS


class OdooClient:
    """Wraps a transport with the write guard and the neutralization gate."""

    def __init__(
        self,
        transport: Transport,
        allow_write: bool = False,
        override: bool = False,
        gate: Callable[[OdooClient], Verdict] | None = None,
    ) -> None:
        self.transport = transport
        self.allow_write = allow_write
        self.override = override
        self._gate = gate
        self._verdict: Verdict | None = None

    def raw_call(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> JsonValue:
        """Call a method with no guard at all.

        Used by the gate itself, which must read ir.config_parameter and friends
        before any verdict exists.
        """
        return self.transport.call(model, method, ids=ids, args=args, kwargs=kwargs)

    def verdict(self) -> Verdict | None:
        """Evaluate the neutralization gate once, on first demand.

        Deliberately not cached across processes: `wt` can drop a database and
        restore a fresh production dump under the same name on the same port, so
        a stale verdict would authorise exactly the write this exists to stop.
        """
        if self._gate is None:
            return None
        if self._verdict is None:
            self._verdict = self._gate(self)
        return self._verdict

    def _refuse_without_write_flag(self, model: str, method: str) -> None:
        raise GuardError(
            f"{model}.{method} would modify data; re-run with --write.\n"
            f"(Only these methods run unguarded: {', '.join(sorted(READ_METHODS))})"
        )

    def _authorize(self, model: str, method: str) -> None:
        """Apply both layers, in order, before a mutating call."""
        if not self.allow_write:
            self._refuse_without_write_flag(model, method)
        verdict = self.verdict()
        if verdict is None or verdict.allows_write:
            return
        if self.override:
            return
        raise GuardError(
            f"{model}.{method} refused: this database is not verifiably "
            f"neutralized.\n\n{verdict.explain()}"
        )

    def call(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> JsonValue:
        """Call a method, gating it when it may change data."""
        if is_mutating(method):
            self._authorize(model, method)
        return self.raw_call(model, method, ids=ids, args=args, kwargs=kwargs)

    def create(self, model: str, values: JsonValue) -> JsonValue:
        """Create records, gated like any other mutation."""
        self._authorize(model, "create")
        return self.transport.create(model, values)

    # --- typed conveniences the CLI builds on ---------------------------------

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str | None = None,
    ) -> JsonValue:
        kwargs: dict[str, Any] = {"domain": domain, "offset": offset}
        if fields:
            kwargs["fields"] = fields
        if limit is not None:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.call(model, "search_read", kwargs=kwargs)

    def search_count(self, model: str, domain: list[Any]) -> int:
        result = self.call(model, "search_count", kwargs={"domain": domain})
        return int(result) if isinstance(result, int) else 0

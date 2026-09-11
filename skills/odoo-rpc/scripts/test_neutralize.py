#!/usr/bin/env python3
"""Tests for the neutralization gate.

The central case is a correctly neutralized database: Odoo's own neutralize.sql
leaves an active dummy mail server and an active autovacuum cron behind, and a
gate that called those live would refuse every database it was built to allow.
"""

from __future__ import annotations

from typing import Any

import pytest

from client import OdooClient
from neutralize import (
    FAIL,
    LIVE,
    NEUTRALIZED,
    PASS,
    SKIPPED,
    UNVERIFIED,
    Verdict,
    evaluate,
)
from transport import JsonValue, OdooError, Transport

AUTOVACUUM_ID = 4


class FakeOdoo(Transport):
    """A tiny in-memory Odoo: config parameters, counts, and cron ids."""

    name = "fake"

    def __init__(
        self,
        params: dict[str, str] | None = None,
        counts: dict[str, int] | None = None,
        cron_ids: list[int] | None = None,
        missing: set[str] | None = None,
        no_autovacuum: bool = False,
    ) -> None:
        self.params = params or {}
        self.counts = counts or {}
        self.cron_ids = [AUTOVACUUM_ID] if cron_ids is None else cron_ids
        self.missing = missing or set()
        self.no_autovacuum = no_autovacuum

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
        kwargs = kwargs or {}
        if model in self.missing:
            raise OdooError("odoo.exceptions.MissingError",
                            f"Object {model} doesn't exist")
        if model == "ir.config_parameter":
            key = kwargs["domain"][0][2]
            value = self.params.get(key)
            return [{"value": value}] if value is not None else []
        if model == "ir.model.data":
            return [] if self.no_autovacuum else [{"res_id": AUTOVACUUM_ID}]
        if model == "ir.cron" and method == "search":
            return list(self.cron_ids)
        if method == "search_count":
            return self.counts.get(model, 0)
        raise AssertionError(f"unexpected {model}.{method}")


def build(**kwargs: object) -> OdooClient:
    return OdooClient(FakeOdoo(**kwargs))


def outcome(verdict: Verdict, key: str) -> str:
    return next(r.outcome for r in verdict.results if r.key == key)


NEUTRAL_PARAMS = {"database.is_neutralized": "True",
                  "web.base.url": "http://localhost:8069"}


# --- the case the gate exists to allow ----------------------------------------

def test_correctly_neutralized_database_passes() -> None:
    """neutralize.sql leaves the dummy mail server and autovacuum cron running."""
    verdict = evaluate(build(params=NEUTRAL_PARAMS, cron_ids=[AUTOVACUUM_ID]))
    assert verdict.status == NEUTRALIZED
    assert verdict.allows_write is True
    assert outcome(verdict, "crons") == PASS
    assert outcome(verdict, "mail_servers") == PASS


def test_autovacuum_cron_alone_is_not_a_finding() -> None:
    verdict = evaluate(build(params=NEUTRAL_PARAMS, cron_ids=[AUTOVACUUM_ID]))
    assert outcome(verdict, "crons") == PASS


# --- one test per blocking check ----------------------------------------------

@pytest.mark.parametrize(
    ("model", "key"),
    [
        ("ir.mail_server", "mail_servers"),
        ("fetchmail.server", "fetchmail"),
        ("ir.actions.server", "webhooks"),
        ("payment.provider", "payment"),
    ],
)
def test_each_live_surface_blocks(model: str, key: str) -> None:
    verdict = evaluate(build(params=NEUTRAL_PARAMS, counts={model: 1}))
    assert verdict.status == LIVE
    assert outcome(verdict, key) == FAIL
    assert verdict.allows_write is False


def test_extra_active_cron_blocks() -> None:
    verdict = evaluate(build(params=NEUTRAL_PARAMS,
                             cron_ids=[AUTOVACUUM_ID, 99]))
    assert verdict.status == LIVE
    assert outcome(verdict, "crons") == FAIL


# --- verdict transitions ------------------------------------------------------

def test_no_flag_on_a_clean_private_database_is_unverified() -> None:
    """A hand-built dev database: nothing live, but nothing proven either."""
    verdict = evaluate(build(params={"web.base.url": "http://localhost:8069"}))
    assert verdict.status == UNVERIFIED
    assert verdict.allows_write is False


def test_no_flag_on_a_public_url_is_live() -> None:
    verdict = evaluate(build(params={"web.base.url": "https://erp.example.com"}))
    assert verdict.status == LIVE


def test_flag_set_but_a_surface_live_is_live() -> None:
    """The flag can lie on a database that was modified after neutralization."""
    verdict = evaluate(build(params=NEUTRAL_PARAMS, counts={"ir.cron": 0,
                                                            "payment.provider": 2}))
    assert verdict.status == LIVE


def test_flag_false_is_not_neutralized() -> None:
    verdict = evaluate(build(params={"database.is_neutralized": "False",
                                     "web.base.url": "http://localhost:8069"}))
    assert outcome(verdict, "flag") == FAIL
    assert verdict.status == UNVERIFIED


@pytest.mark.parametrize("url", ["http://localhost:8069", "http://127.0.0.1:8069",
                                 "http://10.1.2.3", "http://192.168.1.9:8069",
                                 "http://odoo.local", None])
def test_private_urls_downgrade_live_to_unverified(url: str | None) -> None:
    params = {"web.base.url": url} if url else {}
    assert evaluate(build(params=params)).status == UNVERIFIED


# --- skips never read as clean ------------------------------------------------

def test_missing_model_is_skipped_not_passed() -> None:
    verdict = evaluate(build(params=NEUTRAL_PARAMS, missing={"payment.provider"}))
    assert outcome(verdict, "payment") == SKIPPED
    assert verdict.status == NEUTRALIZED


def test_unresolvable_autovacuum_skips_instead_of_failing() -> None:
    """A resolution hiccup must not manufacture a false LIVE.

    It does withhold the verdict: an active cron is present and nothing can say
    whether it is the one neutralization keeps or a job that will fire.
    """
    verdict = evaluate(build(params=NEUTRAL_PARAMS, no_autovacuum=True,
                             cron_ids=[AUTOVACUUM_ID]))
    assert outcome(verdict, "crons") == SKIPPED
    assert verdict.status == UNVERIFIED
    assert verdict.status != LIVE


def test_unresolvable_autovacuum_with_no_active_cron_still_passes() -> None:
    """With nothing scheduled, identifying the keeper does not matter."""
    verdict = evaluate(build(params=NEUTRAL_PARAMS, no_autovacuum=True,
                             cron_ids=[]))
    assert outcome(verdict, "crons") == PASS
    assert verdict.status == NEUTRALIZED


def test_no_audit_check_can_run_is_unverified() -> None:
    """'Nothing was checked' must never read as NEUTRALIZED."""
    verdict = evaluate(build(
        params=NEUTRAL_PARAMS,
        missing={"ir.mail_server", "fetchmail.server", "ir.actions.server",
                 "payment.provider", "ir.cron", "ir.model.data"},
    ))
    assert verdict.status == UNVERIFIED


def test_non_admin_user_cannot_verify_anything() -> None:
    """ir.config_parameter is group_system only; a filtered read must not pass."""
    verdict = evaluate(build(missing={"ir.config_parameter"}))
    assert verdict.status == UNVERIFIED
    assert outcome(verdict, "flag") == SKIPPED
    assert all(r.outcome == SKIPPED for r in verdict.results)


def test_non_admin_explanation_names_the_group() -> None:
    verdict = evaluate(build(missing={"ir.config_parameter"}))
    assert "base.group_system" in verdict.explain()


# --- explanations -------------------------------------------------------------

def test_live_explanation_names_the_failing_check_and_the_override() -> None:
    verdict = evaluate(build(params=NEUTRAL_PARAMS, counts={"ir.mail_server": 3}))
    text = verdict.explain()
    assert "Live outgoing mail servers" in text
    assert "--i-know-this-is-live" in text


def test_unverified_explanation_names_the_fix() -> None:
    verdict = evaluate(build(params={"web.base.url": "http://localhost:8069"}))
    assert "odoo-bin neutralize" in verdict.explain()

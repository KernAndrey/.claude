#!/usr/bin/env python3
"""The neutralization gate: is this database safe to write to?

Every check mirrors something Odoo's own `neutralize.sql` does, so a correctly
neutralized database passes. Two details in those scripts would otherwise make
every neutralized database look live, and both are encoded below:

  * neutralization LEAVES one active mail server — a dummy it inserts itself,
    with smtp_host = 'invalid';
  * neutralization LEAVES one active cron — base.autovacuum_job is explicitly
    excluded from the deactivating UPDATE.

Sources: base/data/neutralize.sql, mail/data/neutralize.sql,
payment/data/neutralize.sql in Odoo 17/18/19.

Usage:
    from neutralize import evaluate
    verdict = evaluate(client)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from transport import OdooError

if TYPE_CHECKING:  # pragma: no cover - import exists for type checkers only
    from client import OdooClient

PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

NEUTRALIZED = "NEUTRALIZED"
UNVERIFIED = "UNVERIFIED"
LIVE = "LIVE"

NEUTRALIZE_FLAG = "database.is_neutralized"
DUMMY_SMTP_HOST = "invalid"
DISABLED_WEBHOOK_URL = "neutralization - disable webhook"
AUTOVACUUM = ("base", "autovacuum_job")

# The only errors that prove a surface is absent rather than unchecked. Anything
# else — a permission refusal, a validation error, an unexpected field problem —
# leaves the surface unverified, so the verdict is withheld rather than granted
# on the strength of the checks that happened to succeed.
ABSENCE_ERROR_NAMES = ("MissingError", "KeyError")

PRIVATE_HOST_SUFFIXES = (".local", ".test", ".localhost", ".internal")
PRIVATE_HOST_PREFIXES = ("10.", "192.168.", "127.", "172.16.", "172.17.", "172.18.",
                         "172.19.", "172.20.", "172.21.", "172.22.", "172.23.",
                         "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
                         "172.29.", "172.30.", "172.31.")


@dataclass(frozen=True)
class CountCheck:
    """One 'how many live records of this kind remain' probe."""

    key: str
    label: str
    model: str
    domain: list[object]
    remedy: str


# Each domain describes what neutralization should have left behind: nothing.
COUNT_CHECKS: tuple[CountCheck, ...] = (
    CountCheck(
        key="mail_servers",
        label="Live outgoing mail servers",
        model="ir.mail_server",
        # The dummy that neutralization itself inserts is excluded by smtp_host.
        domain=[("active", "=", True), ("smtp_host", "!=", DUMMY_SMTP_HOST)],
        remedy="mail would actually be delivered from this database",
    ),
    CountCheck(
        key="fetchmail",
        label="Live incoming mail servers",
        model="fetchmail.server",
        domain=[("active", "=", True)],
        remedy="incoming mail would be fetched and processed",
    ),
    CountCheck(
        key="webhooks",
        label="Live outgoing webhooks",
        model="ir.actions.server",
        domain=[("state", "=", "webhook"), ("webhook_url", "!=", DISABLED_WEBHOOK_URL)],
        remedy="webhooks would fire at external systems",
    ),
    CountCheck(
        key="payment",
        label="Enabled payment providers",
        model="payment.provider",
        domain=[("state", "not in", ["test", "disabled"])],
        remedy="real payments could be taken",
    ),
)


@dataclass
class CheckResult:
    """The outcome of one check, including why it was skipped."""

    key: str
    label: str
    outcome: str
    detail: str = ""
    # True when this check could not reach a conclusion — as opposed to proving
    # the surface is absent. An inconclusive audit withholds the verdict.
    inconclusive: bool = False

    def line(self) -> str:
        suffix = f" — {self.detail}" if self.detail else ""
        return f"  [{self.outcome:<7}] {self.label}{suffix}"


@dataclass
class Verdict:
    """What the gate concluded, and the evidence behind it."""

    status: str
    results: list[CheckResult] = field(default_factory=list)
    base_url: str | None = None

    @property
    def allows_write(self) -> bool:
        return self.status == NEUTRALIZED

    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.outcome == FAIL]

    def explain(self) -> str:
        """A refusal message that names the problem and the fix."""
        lines = [f"Verdict: {self.status}"]
        lines.extend(r.line() for r in self.results)
        if self.status == LIVE:
            lines.append(
                "\nThis database still has production surfaces switched on. "
                "Neutralize it (odoo-bin neutralize) or point at a dev database."
            )
        elif self.status == UNVERIFIED:
            lines.append(
                f"\nNothing live was found, but '{NEUTRALIZE_FLAG}' is not set, so "
                f"this cannot be proven to be a neutralized copy.\n"
                f"Run: odoo-bin neutralize -d <db>"
            )
        lines.append(
            "\nTo write anyway, add --i-know-this-is-live alongside --write."
        )
        return "\n".join(lines)


def _is_private(url: str | None) -> bool:
    """Whether a base URL points somewhere that cannot be production."""
    if not url:
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host or host in ("localhost", "::1"):
        return True
    if host.startswith(PRIVATE_HOST_PREFIXES):
        return True
    return host.endswith(PRIVATE_HOST_SUFFIXES)


def _get_param(client: OdooClient, key: str) -> str | None:
    """Read one ir.config_parameter value."""
    rows = client.raw_call(
        "ir.config_parameter",
        "search_read",
        kwargs={"domain": [("key", "=", key)], "fields": ["value"], "limit": 1},
    )
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        value = rows[0].get("value")
        return str(value) if value is not None else None
    return None


def _check_flag(client: OdooClient) -> tuple[CheckResult, bool]:
    """Read the neutralization flag. Also proves we have group_system access."""
    try:
        raw = _get_param(client, NEUTRALIZE_FLAG)
    except OdooError as exc:
        return (
            CheckResult(
                "flag",
                "Neutralization flag",
                SKIPPED,
                f"cannot read ir.config_parameter ({exc.short_name()}); "
                f"the RPC user needs Settings access (base.group_system)",
            ),
            False,
        )
    if raw is not None and raw.strip().lower() in ("true", "1", "t"):
        return CheckResult("flag", "Neutralization flag", PASS, "set"), True
    detail = "not set" if raw is None else f"set to {raw!r}"
    return CheckResult("flag", "Neutralization flag", FAIL, detail), True


def _run_count_check(client: OdooClient, check: CountCheck) -> CheckResult:
    """Count what should be zero, skipping models or fields this version lacks."""
    try:
        count = client.search_count(check.model, list(check.domain))
    except OdooError as exc:
        return CheckResult(
            check.key, check.label, SKIPPED,
            f"{check.model} unavailable ({exc.short_name()})",
            inconclusive=exc.short_name() not in ABSENCE_ERROR_NAMES,
        )
    if count:
        return CheckResult(check.key, check.label, FAIL,
                           f"{count} found — {check.remedy}")
    return CheckResult(check.key, check.label, PASS)


def _autovacuum_id(client: OdooClient) -> int | None:
    """Resolve base.autovacuum_job, the one cron neutralization leaves running."""
    module, name = AUTOVACUUM
    rows = client.raw_call(
        "ir.model.data",
        "search_read",
        kwargs={
            "domain": [("module", "=", module), ("name", "=", name),
                       ("model", "=", "ir.cron")],
            "fields": ["res_id"],
            "limit": 1,
        },
    )
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        res_id = rows[0].get("res_id")
        return int(res_id) if isinstance(res_id, int) else None
    return None


def _check_crons(client: OdooClient) -> CheckResult:
    """Count active crons, excluding the autovacuum job.

    A failed xml-id resolution is SKIPPED rather than 'no exclusion', which would
    turn every neutralized database into a false LIVE.
    """
    label = "Live scheduled jobs"
    try:
        keeper = _autovacuum_id(client)
        ids = client.raw_call("ir.cron", "search",
                              kwargs={"domain": [("active", "=", True)]})
    except OdooError as exc:
        return CheckResult("crons", label, SKIPPED, f"unavailable ({exc.short_name()})",
                           inconclusive=True)
    if not isinstance(ids, list):
        return CheckResult("crons", label, SKIPPED, f"ir.cron.search returned {ids!r}",
                           inconclusive=True)
    if keeper is None:
        # No active cron at all is a clean answer whether or not the xml-id
        # resolves. With active crons present and no way to tell the kept
        # autovacuum job apart from a real one, the audit cannot conclude —
        # and an inconclusive cron audit must not authorise writes.
        if not ids:
            return CheckResult("crons", label, PASS)
        return CheckResult(
            "crons", label, SKIPPED,
            f"{len(ids)} active cron(s), and base.autovacuum_job could not be "
            f"resolved to tell them apart from the one neutralization keeps",
            inconclusive=True,
        )
    live = [i for i in ids if i != keeper]
    if live:
        return CheckResult("crons", label, FAIL,
                           f"{len(live)} active — scheduled jobs would fire")
    return CheckResult("crons", label, PASS)


def _decide(results: list[CheckResult], flag_ok: bool, base_url: str | None) -> str:
    """Turn the check results into one of the three verdicts.

    A check skipped because the model is not installed is evidence of absence:
    an uninstalled payment module has no live providers. Every other skip is
    not — a refused read, a validation error, an unresolvable cron keeper all
    leave a surface that may be live and unseen, so the verdict is withheld
    rather than granted on the strength of whatever happened to succeed.
    """
    if any(r.outcome == FAIL and r.key != "flag" for r in results):
        return LIVE
    audits = [r for r in results if r.key != "flag"]
    audit_ran = any(r.outcome in (PASS, FAIL) for r in audits)
    blinded = any(r.inconclusive for r in audits)
    if not flag_ok or not audit_ran or blinded:
        return LIVE if not _is_private(base_url) else UNVERIFIED
    return NEUTRALIZED


def evaluate(client: OdooClient) -> Verdict:
    """Run every check and return the verdict with its evidence."""
    flag_result, readable = _check_flag(client)
    results = [flag_result]
    base_url: str | None = None

    if not readable:
        results.extend(
            CheckResult(c.key, c.label, SKIPPED, "no Settings access")
            for c in COUNT_CHECKS
        )
        results.append(CheckResult("crons", "Live scheduled jobs", SKIPPED,
                                   "no Settings access"))
        return Verdict(UNVERIFIED, results, None)

    try:
        base_url = _get_param(client, "web.base.url")
    except OdooError:
        base_url = None

    results.extend(_run_count_check(client, check) for check in COUNT_CHECKS)
    results.append(_check_crons(client))
    flag_ok = flag_result.outcome == PASS
    return Verdict(_decide(results, flag_ok, base_url), results, base_url)

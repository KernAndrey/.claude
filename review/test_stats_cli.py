"""Tests for stats_cli — argparse + filters + table renderer.

The CLI reads ``logs/stats.jsonl`` and either prints aggregates or emits
filtered rows as JSON. Tests use a synthetic JSONL so they don't depend
on a populated logs directory.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch


from stats_cli import main as cli_main


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write rows as a JSONL file at ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _row(
    timestamp: str,
    project: str,
    verdict: str,
    backends: list[dict],
    fallback: dict | None = None,
) -> dict:
    """Tiny row factory matching the schema_version=1 shape."""
    return {
        "schema_version": 1,
        "timestamp": timestamp,
        "project": project,
        "verdict": verdict,
        "mode": "fanout",
        "diff": {"total_lines": 100, "added_prod_lines": 50, "files_count": 3},
        "backends": backends,
        "consolidation": {
            "total_clusters": 0,
            "upheld_clusters": 0,
            "overturned_clusters": 0,
            "consensus_rate": 0.0,
            "arbiter": {"status": "skipped_no_findings", "duration_seconds": 0.0, "error": None},
        },
        "fallback": fallback or {"triggered": False, "reason": None},
    }


def _backend(
    name: str,
    model: str,
    status: str = "ok",
    duration: float = 30.0,
    upheld: int = 1,
    overturned: int = 0,
    consensus: int = 0,
    solo: int = 1,
    crit: int = 1,
    fallback_used: bool = False,
) -> dict:
    """Backend block factory."""
    return {
        "config": {"backend": name, "model": model, "timeout": 1200},
        "used_backend": name,
        "status": status,
        "fallback_used": fallback_used,
        "duration_seconds": duration,
        "raw_findings": {"critical": crit, "warning": 0},
        "unique_findings_in_clusters": consensus + solo,
        "consensus_findings": consensus,
        "solo_findings": solo,
        "upheld_findings": upheld,
        "overturned_findings": overturned,
        "error": None,
        "per_lens": None,
    }


def _capture_stdout(argv: list[str]) -> str:
    """Run cli_main with argv and return captured stdout."""
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        cli_main(argv)
    return buf.getvalue()


def test_cli_missing_jsonl_returns_nonzero(tmp_path: Path) -> None:
    """Friendly error + nonzero exit when there's no stats yet."""
    missing = tmp_path / "nope.jsonl"
    rc = cli_main(["--path", str(missing)])
    assert rc == 1


def test_cli_default_summary_prints_per_backend_table(tmp_path: Path) -> None:
    """Default view aggregates per-backend metrics."""
    path = tmp_path / "stats.jsonl"
    _write_jsonl(
        path,
        [
            _row("2026-05-01T10:00:00", "p1", "OK", [_backend("opencode", "m1", duration=10.0)]),
            _row("2026-05-01T11:00:00", "p1", "BLOCK", [_backend("opencode", "m1", duration=30.0, upheld=2, solo=2)]),
        ],
    )
    out = _capture_stdout(["--path", str(path)])
    assert "opencode/m1" in out
    assert "Backend" in out  # table header


def test_cli_compare_renders_pair_block(tmp_path: Path) -> None:
    """--compare prints a head-to-head block for each pair of backends."""
    path = tmp_path / "stats.jsonl"
    _write_jsonl(
        path,
        [
            _row(
                "2026-05-01T10:00:00",
                "p1",
                "BLOCK",
                [
                    _backend("opencode", "m1", crit=2),
                    _backend("claude", "m2", crit=1),
                ],
            ),
        ],
    )
    out = _capture_stdout(["--path", str(path), "--compare"])
    assert "opencode/m1" in out
    assert "claude/m2" in out
    assert "Pair" in out
    assert "Both flagged something" in out


def test_cli_compare_empty_when_no_multi_backend_runs(tmp_path: Path) -> None:
    """--compare on N==1-only data prints a friendly notice, not a crash."""
    path = tmp_path / "stats.jsonl"
    _write_jsonl(
        path,
        [
            _row("2026-05-01T10:00:00", "p1", "OK", [_backend("opencode", "m1")]),
        ],
    )
    out = _capture_stdout(["--path", str(path), "--compare"])
    assert "nothing to compare" in out


def test_cli_filter_by_backend(tmp_path: Path) -> None:
    """--backend filter narrows to runs that included that backend."""
    path = tmp_path / "stats.jsonl"
    _write_jsonl(
        path,
        [
            _row("2026-05-01T10:00:00", "p1", "OK", [_backend("opencode", "m1")]),
            _row("2026-05-01T11:00:00", "p1", "OK", [_backend("claude", "m2")]),
        ],
    )
    out = _capture_stdout(["--path", str(path), "--backend", "opencode", "--json"])
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["backends"][0]["config"]["backend"] == "opencode"


def test_cli_filter_by_project(tmp_path: Path) -> None:
    """--project filter matches the row's project field exactly."""
    path = tmp_path / "stats.jsonl"
    _write_jsonl(
        path,
        [
            _row("2026-05-01T10:00:00", "alpha", "OK", [_backend("opencode", "m1")]),
            _row("2026-05-01T10:00:00", "beta", "OK", [_backend("opencode", "m1")]),
        ],
    )
    out = _capture_stdout(["--path", str(path), "--project", "beta", "--json"])
    parsed = json.loads(out)
    assert [r["project"] for r in parsed] == ["beta"]


def test_cli_filter_by_since(tmp_path: Path) -> None:
    """--since YYYY-MM-DD drops earlier rows."""
    path = tmp_path / "stats.jsonl"
    _write_jsonl(
        path,
        [
            _row("2026-04-30T10:00:00", "p1", "OK", [_backend("opencode", "m1")]),
            _row("2026-05-02T10:00:00", "p1", "OK", [_backend("opencode", "m1")]),
        ],
    )
    out = _capture_stdout(["--path", str(path), "--since", "2026-05-01", "--json"])
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["timestamp"].startswith("2026-05-02")


def test_cli_last_n_renders_recent_run_table(tmp_path: Path) -> None:
    """--last N shows the last N rows in chronological order."""
    path = tmp_path / "stats.jsonl"
    _write_jsonl(
        path,
        [
            _row("2026-05-01T10:00:00", "p1", "OK", [_backend("opencode", "m1")]),
            _row("2026-05-01T11:00:00", "p1", "BLOCK", [_backend("opencode", "m1")]),
            _row("2026-05-01T12:00:00", "p1", "OK", [_backend("claude", "m2")]),
        ],
    )
    out = _capture_stdout(["--path", str(path), "--last", "2"])
    assert "BLOCK" in out
    assert "claude" in out
    # First row (10:00) must NOT appear with --last 2
    lines = [ln for ln in out.splitlines() if "10:00:00" in ln]
    assert lines == []


def test_cli_summary_reports_fallback_share(tmp_path: Path) -> None:
    """The 'Fallback fired on N / M runs' line surfaces in the default view."""
    path = tmp_path / "stats.jsonl"
    _write_jsonl(
        path,
        [
            _row("2026-05-01T10:00:00", "p1", "OK", [_backend("opencode", "m1")]),
            _row(
                "2026-05-01T11:00:00",
                "p1",
                "EMPTY",
                [
                    _backend("opencode", "m1", status="error", upheld=0, solo=0, crit=0),
                    _backend("claude", "fb-m", fallback_used=True),
                ],
                fallback={"triggered": True, "reason": "demo"},
            ),
        ],
    )
    out = _capture_stdout(["--path", str(path)])
    assert "Fallback fired on 1" in out


def test_cli_no_args_invocation_does_not_crash(tmp_path: Path) -> None:
    """Smoke test: minimal valid jsonl + default args returns 0."""
    path = tmp_path / "stats.jsonl"
    _write_jsonl(path, [_row("2026-05-01T10:00:00", "p1", "OK", [_backend("opencode", "m1")])])
    rc = cli_main(["--path", str(path)])
    assert rc == 0

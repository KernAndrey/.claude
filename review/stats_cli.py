#!/usr/bin/env python3
"""CLI for the rolling pre-commit review stats (``logs/stats.jsonl``).

    python ~/.claude/review/stats_cli.py                   # default summary
    python ~/.claude/review/stats_cli.py --last 20         # last N runs (table)
    python ~/.claude/review/stats_cli.py --since 2026-04-01
    python ~/.claude/review/stats_cli.py --backend opencode
    python ~/.claude/review/stats_cli.py --project hubcraft-tms
    python ~/.claude/review/stats_cli.py --compare         # head-to-head pairs
    python ~/.claude/review/stats_cli.py --json            # raw JSON, for jq

The stats schema is produced by ``stats.build_run_stats`` and appended
to ``logs/stats.jsonl`` by ``stats.save``. Filters compose: e.g.
``--last 20 --backend claude`` shows the 20 most recent runs that
included the ``claude`` backend.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from stats import aggregate_per_backend, default_aggregate_path, load_jsonl


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    path = Path(args.path).expanduser() if args.path else default_aggregate_path()
    rows = load_jsonl(path)
    if not rows:
        print(f"No stats yet at {path}. Make a commit through the review hook first.", file=sys.stderr)
        return 1

    rows = _apply_filters(rows, args)
    if not rows:
        print("No stats matched the filters.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if args.compare:
        _render_compare(rows)
        return 0
    if args.last is not None:
        _render_recent_runs(rows[-args.last :])
        return 0
    _render_summary(rows)
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inspect pre-commit review stats (multi-backend comparison).")
    p.add_argument("--last", type=int, metavar="N", help="Show the last N runs as a table.")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="Filter to runs at or after this date.")
    p.add_argument("--backend", metavar="NAME", help="Filter to runs that included this backend.")
    p.add_argument("--project", metavar="NAME", help="Filter to runs from this project (cwd basename).")
    p.add_argument("--compare", action="store_true", help="Print a head-to-head comparison of pairs of backends.")
    p.add_argument("--json", action="store_true", help="Emit filtered rows as raw JSON for jq / piping.")
    p.add_argument(
        "--path", metavar="PATH", help="Override the stats.jsonl path (default: ~/.claude/review/logs/stats.jsonl)."
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _apply_filters(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    out = rows
    if args.since:
        cutoff = _parse_date(args.since)
        out = [r for r in out if _row_date(r) >= cutoff]
    if args.backend:
        out = [r for r in out if any(b.get("config", {}).get("backend") == args.backend for b in r.get("backends", []))]
    if args.project:
        out = [r for r in out if r.get("project") == args.project]
    return out


def _parse_date(s: str) -> datetime:
    """Permissive YYYY-MM-DD parser; CLI-friendly errors via argparse."""
    return datetime.strptime(s, "%Y-%m-%d")


def _row_date(row: dict[str, Any]) -> datetime:
    """Best-effort parse of the row timestamp; falls back to epoch on garbage."""
    ts = row.get("timestamp", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d_%H-%M-%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return datetime.min


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_summary(rows: list[dict[str, Any]]) -> None:
    """Default view: per-backend aggregates over all filtered rows."""
    agg = aggregate_per_backend(rows)
    if not agg:
        print("(no backend entries found)")
        return

    headers = ["Backend", "Runs", "Success", "p50 lat", "p95 lat", "Avg crit", "Upheld%", "Solo%"]
    table = [headers]
    for key in sorted(agg):
        m = agg[key]
        table.append(
            [
                key,
                str(m["runs"]),
                f"{m['success_rate'] * 100:.1f}%",
                f"{m['p50_latency_seconds']:.1f}s",
                f"{m['p95_latency_seconds']:.1f}s",
                f"{m['avg_critical_findings']:.2f}",
                f"{m['upheld_rate'] * 100:.0f}%",
                f"{m['solo_rate'] * 100:.0f}%",
            ]
        )
    _print_table(table)

    fb_runs = sum(1 for r in rows if r.get("fallback", {}).get("triggered"))
    if fb_runs:
        pct = fb_runs / len(rows) * 100
        print(f"\nFallback fired on {fb_runs} / {len(rows)} runs ({pct:.1f}%).")


def _render_recent_runs(rows: list[dict[str, Any]]) -> None:
    """Per-run table — useful with --last N."""
    headers = ["Timestamp", "Project", "Verdict", "Backends (status, dur)"]
    table = [headers]
    for r in rows:
        backends = []
        for b in r.get("backends", []):
            cfg = b.get("config", {})
            label = f"{cfg.get('backend', '?')}={b.get('status', '?')}/{b.get('duration_seconds', 0):.0f}s"
            if b.get("fallback_used"):
                label += "(fb)"
            backends.append(label)
        table.append(
            [
                r.get("timestamp", ""),
                r.get("project", ""),
                r.get("verdict", ""),
                "; ".join(backends),
            ]
        )
    _print_table(table)


def _render_compare(rows: list[dict[str, Any]]) -> None:
    """Head-to-head comparison for every pair of backends seen together."""
    pairs = _collect_pairs(rows)
    if not pairs:
        print("No commit included two or more backends — nothing to compare.")
        return
    for (a, b), pair_rows in pairs.items():
        _print_pair_block(a, b, pair_rows)


def _collect_pairs(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Index rows by the unordered pairs of backend keys present in each row."""
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        keys = sorted(
            {
                f"{b.get('config', {}).get('backend', '?')}/{b.get('config', {}).get('model', '?')}"
                for b in row.get("backends", [])
                if not b.get("fallback_used")
            }
        )
        if len(keys) < 2:
            continue
        # Pair each unique combination in this row
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                pairs.setdefault((keys[i], keys[j]), []).append(row)
    return pairs


def _print_pair_block(a: str, b: str, rows: list[dict[str, Any]]) -> None:
    """Render the comparison stats block for one (a, b) pair."""
    counters = _pair_counters(a, b, rows)
    fa = counters["a_failures"]
    fb = counters["b_failures"]
    n = len(rows)
    print(f"\nPair: {a}  ↔  {b}   (over {n} commit(s))\n")
    print("  Findings overlap:")
    print(f"    Both flagged something: {counters['both_flagged']:>3}")
    print(f"    Only {a:<25}  {counters['only_a']:>3}")
    print(f"    Only {b:<25}  {counters['only_b']:>3}")
    print(f"    Neither flagged:        {counters['neither']:>3}")
    print("\n  Reliability (failures = status != 'ok'):")
    print(f"    {a}: {fa}/{n}  ({fa / n * 100 if n else 0:.1f}%)")
    print(f"    {b}: {fb}/{n}  ({fb / n * 100 if n else 0:.1f}%)")
    if counters["a_durations"]:
        print("\n  Avg latency on success:")
        avg_a = sum(counters["a_durations"]) / len(counters["a_durations"])
        avg_b = sum(counters["b_durations"]) / len(counters["b_durations"]) if counters["b_durations"] else 0
        print(f"    {a}: {avg_a:.1f}s")
        print(f"    {b}: {avg_b:.1f}s")


def _pair_counters(a: str, b: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute overlap + reliability counters for a single pair of backends."""
    out: Counter[str] = Counter()
    a_durations: list[float] = []
    b_durations: list[float] = []
    a_failures = 0
    b_failures = 0
    for row in rows:
        ba = _find_backend(row, a)
        bb = _find_backend(row, b)
        a_has = bool(ba and int(ba.get("raw_findings", {}).get("critical", 0)))
        b_has = bool(bb and int(bb.get("raw_findings", {}).get("critical", 0)))
        if a_has and b_has:
            out["both_flagged"] += 1
        elif a_has:
            out["only_a"] += 1
        elif b_has:
            out["only_b"] += 1
        else:
            out["neither"] += 1
        if ba:
            if ba.get("status") != "ok":
                a_failures += 1
            elif ba.get("duration_seconds") is not None:
                a_durations.append(float(ba["duration_seconds"]))
        if bb:
            if bb.get("status") != "ok":
                b_failures += 1
            elif bb.get("duration_seconds") is not None:
                b_durations.append(float(bb["duration_seconds"]))
    return {
        "both_flagged": out["both_flagged"],
        "only_a": out["only_a"],
        "only_b": out["only_b"],
        "neither": out["neither"],
        "a_failures": a_failures,
        "b_failures": b_failures,
        "a_durations": a_durations,
        "b_durations": b_durations,
    }


def _find_backend(row: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Locate the per-backend entry whose config matches ``backend/model``."""
    for b in row.get("backends", []):
        cfg = b.get("config", {})
        full = f"{cfg.get('backend', '?')}/{cfg.get('model', '?')}"
        if full == key:
            return b
    return None


# ---------------------------------------------------------------------------
# Table printer (no external deps)
# ---------------------------------------------------------------------------


def _print_table(rows: list[list[str]]) -> None:
    """Minimal whitespace-padded table — one stdlib-only renderer is enough."""
    if not rows:
        return
    widths = [max(len(row[col]) for row in rows) for col in range(len(rows[0]))]
    for i, row in enumerate(rows):
        line = "  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row))
        print(line)
        if i == 0:
            print("  ".join("─" * w for w in widths))


if __name__ == "__main__":
    sys.exit(main())

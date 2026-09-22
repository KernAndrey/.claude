#!/usr/bin/env python3
"""
Triage classifier for survived mutmut mutants.

Reads structured JSON from parse_results.py and classifies each survived
mutant into one of three buckets, with confidence:

    real_test_gap       — high confidence this is a missing test
    equivalent_candidate — semantically equivalent or near-equivalent mutation
    known_noise         — pattern that's almost always false positive

Heuristics are conservative — when uncertain, defaults to real_test_gap so
nothing critical gets silently filtered. Manual review is always required for
final whitelist decisions.

Usage:
    python triage_classifier.py results.json
    python triage_classifier.py results.json --output classified.json
    cat results.json | python triage_classifier.py -
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from parse_results import HIGH_SIGNAL_OPERATORS, NOISE_OPERATORS


# --- Heuristic rules -------------------------------------------------------

# Imported, never re-declared: parse_results.infer_operator emits these labels
# and therefore owns the taxonomy. A local copy silently no-ops every rule below.

EQUIVALENT_BOUNDARY_PAIRS = [
    # (operator_label, reason)
    ("ROR_gt_to_ge", "boundary equivalence possible if values are integers and the boundary is unreachable"),
    ("ROR_lt_to_le", "boundary equivalence possible if values are integers and the boundary is unreachable"),
]

NOISE_FILE_PATTERNS = [
    (r"__init__\.py$", "package init files rarely contain testable logic"),
    (r"_version\.py$|version\.py$", "version files don't need behavioural tests"),
    (r"conftest\.py$", "pytest fixtures, not production logic"),
    (r"settings\.py$|config\.py$", "configuration declarations, not behaviour"),
    (r"/migrations/", "auto-generated migration code"),
    (r"/admin\.py$", "Django admin UI, often without behavioural assertions"),
]

NOISE_LINE_PATTERNS = [
    # Logging statements
    (r"^\s*[+-]\s*(log|logger|logging)\.", "logging call mutation, side-effect-only"),
    (r"^\s*[+-]\s*print\(", "print statement, debug output"),
    # Indent / formatting constants
    (r"INDENT_STEP|INDENT_SIZE|TAB_WIDTH", "indentation constants, cosmetic"),
    # Version strings
    (r"^\s*[+-]\s*__version__", "version string, not behaviour"),
    # Docstrings (mutmut 3 usually skips these but pragmas can leak)
    (r'^\s*[+-]\s*"""', "docstring mutation"),
]


def classify(mutant: dict) -> dict:
    """Classify a single mutant. Returns dict with category, confidence, reason."""
    operator = mutant.get("operator") or "unknown"
    file_path = mutant.get("file", "")
    diff_lines = mutant.get("diff_lines", [])

    # Layer 1: noise file patterns (strongest signal)
    for pattern, reason in NOISE_FILE_PATTERNS:
        if re.search(pattern, file_path):
            return {
                "category": "known_noise",
                "confidence": "high",
                "reason": f"file pattern: {reason}",
                "action": "exclude path from paths_to_mutate or add # pragma: no mutate",
            }

    # Layer 2: noise line patterns
    for pattern, reason in NOISE_LINE_PATTERNS:
        for line in diff_lines:
            if re.search(pattern, line):
                return {
                    "category": "known_noise",
                    "confidence": "high",
                    "reason": f"line pattern: {reason}",
                    "action": "add # pragma: no mutate to that line",
                }

    # Layer 3: noise operators
    if operator in NOISE_OPERATORS:
        return {
            "category": "known_noise",
            "confidence": "medium",
            "reason": NOISE_OPERATORS[operator],
            # mutmut 3 has no operator-level exclusion (`exclude_operators` is
            # silently ignored). The only real levers are pragma and do_not_mutate.
            "action": "review; if noise, `# pragma: no mutate` the line or add its path to `do_not_mutate`",
        }

    # Layer 4: candidate equivalent mutants — high signal operator that often has equivalence
    if operator in {"ROR_gt_to_ge", "ROR_lt_to_le"}:
        # Check if neighbour is integer literal — common boundary equivalence
        for line in diff_lines:
            if re.search(r"\b\d+\b", line):
                return {
                    "category": "equivalent_candidate",
                    "confidence": "low",
                    "reason": (
                        "boundary mutation (>/>= or </<=) on integer comparison; "
                        "may be equivalent if boundary value is unreachable, "
                        "but usually indicates missing boundary test"
                    ),
                    "action": (
                        "REVIEW: write boundary test first. "
                        "Only whitelist if test cannot be written because value is provably unreachable."
                    ),
                }

    # Layer 5: high signal operators — almost always real gaps
    if operator in HIGH_SIGNAL_OPERATORS:
        return {
            "category": "real_test_gap",
            "confidence": "high",
            "reason": f"high-signal operator {operator}; mutation almost certainly changes behaviour",
            "action": f"write targeted test — see fix_recipes.md for {operator}",
        }

    # Default: assume real gap, low confidence (manual review needed)
    return {
        "category": "real_test_gap",
        "confidence": "low",
        "reason": "operator not classified — manual review needed",
        "action": "review diff manually, apply mutant locally to understand impact",
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        type=str,
        help='Path to parse_results.py output JSON, or "-" for stdin',
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Write classified JSON to this path. Default: stdout.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only the summary breakdown, not per-mutant classification",
    )
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()

    if args.input == "-":
        data = json.load(sys.stdin)
    else:
        data = json.loads(Path(args.input).read_text())

    survived = data.get("survived_mutants", [])
    if not survived:
        print("No survived mutants in input.", file=sys.stderr)
        return 0

    classified = []
    counts = {"real_test_gap": 0, "equivalent_candidate": 0, "known_noise": 0}
    by_action = {}

    for mutant in survived:
        triage = classify(mutant)
        classified.append({**mutant, "triage": triage})
        counts[triage["category"]] += 1
        action_key = triage["action"][:60]
        by_action[action_key] = by_action.get(action_key, 0) + 1

    output = {
        "summary": {
            "total_survived": len(survived),
            "real_test_gap": counts["real_test_gap"],
            "equivalent_candidate": counts["equivalent_candidate"],
            "known_noise": counts["known_noise"],
        },
        "actions_by_frequency": dict(sorted(by_action.items(), key=lambda kv: kv[1], reverse=True)),
        "classified_mutants": [] if args.summary_only else classified,
    }

    out_json = json.dumps(output, indent=2, default=str)
    if args.output:
        args.output.write_text(out_json)
        print(f"Wrote classification to {args.output}", file=sys.stderr)
    else:
        print(out_json)

    # Stderr summary always
    total = output["summary"]["total_survived"]
    print(
        f"\nClassification of {total} survived mutants:",
        file=sys.stderr,
    )
    print(
        f"  real_test_gap:        {counts['real_test_gap']:4d} ({counts['real_test_gap'] * 100 // total}%)",
        file=sys.stderr,
    )
    print(
        f"  equivalent_candidate: {counts['equivalent_candidate']:4d} "
        f"({counts['equivalent_candidate'] * 100 // total}%)",
        file=sys.stderr,
    )
    print(
        f"  known_noise:          {counts['known_noise']:4d} ({counts['known_noise'] * 100 // total}%)",
        file=sys.stderr,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())

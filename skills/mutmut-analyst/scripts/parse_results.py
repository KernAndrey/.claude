#!/usr/bin/env python3
"""
Parse mutmut output into structured JSON for analysis.

mutmut 3.5+ stores everything we need on disk in `mutants/`:
  - `mutants/<source>.py.meta`      JSON: {exit_code_by_key, durations_by_key, ...}
  - `mutants/mutmut-stats.json`     JSON: {tests_by_mangled_function_name, ...}
  - `mutants/<source>.py`           Python source: each function appears as
                                    `x_<name>__mutmut_orig` and `x_<name>__mutmut_<N>`
                                    variants — diff is just AST-unparse(orig) vs
                                    AST-unparse(variant).

This file-based approach replaces the subprocess-per-mutant model used by
earlier versions. On a 2500-mutant project, the old approach took ~5GB peak
RAM and ~10 minutes; the file-based approach is one AST parse + a JSON load
and finishes in seconds.

Exit codes in `.meta`:
  0       killed (test failed under mutation — good)
  1       survived (test passed under mutation — bad)
  33      mutmut programmatic fail (treated as killed, see mutmut source)
  negative pytest crashed / timed out (treated as timeout / suspicious)

Usage:
    python parse_results.py --mutants-dir <path> [--out FILE]
    python parse_results.py                     # auto-detect ./mutants

Output JSON structure:
    {
      "summary": {"total", "killed", "survived", "timeout", "suspicious", "skipped", "mutation_score"},
      "by_operator": {"<op>": {"killed": int, "survived": int, ...}},
      "by_file": {"<filepath>": {"killed": int, "survived": int, "mutants": [...]}},
      "by_function": {"<filepath>::<function>": {...}},
      "survived_mutants": [
        {"id", "file", "line", "function", "operator", "diff_lines", "raw_diff", "covering_tests"},
        ...
      ]
    }
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Mutant:
    id: str
    file: str
    line: int | None
    function: str | None
    status: str
    operator: str | None
    diff_lines: list[str] = field(default_factory=list)
    raw_diff: str = ""
    covering_tests: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Status mapping (mutmut 3.5+ exit code semantics)
# ---------------------------------------------------------------------------

_TIMEOUT_EXITS = {-9, -15, -24, 124}  # SIGKILL, SIGTERM, SIGXFSZ, GNU timeout


def status_from_exit_code(code: int) -> str:
    """Map pytest exit code stored in mutmut's .meta to mutmut status.

    pytest exit code semantics under mutation:
      0  = all tests passed     -> mutant survived (tests didn't catch the change)
      1  = some test failed     -> mutant killed (tests caught the change)
      33 = MutmutProgrammaticFailException raised before tests ran -> killed
      other / negative = pytest crashed / timed out / signalled -> timeout/suspicious
    """
    if code == 0:
        return "survived"
    if code == 1:
        return "killed"
    if code == 33:
        return "killed"
    if code in _TIMEOUT_EXITS:
        return "timeout"
    return "suspicious"


# ---------------------------------------------------------------------------
# Operator inference from diff lines
# ---------------------------------------------------------------------------


def infer_operator(before: str, after: str) -> str | None:
    """Best-effort operator classification.

    Order matters — check most specific patterns first.
    """
    if not before.strip() and not after.strip():
        return None
    if not after.strip() or after.strip() in {"pass", "return"}:
        return "SBR"
    # mutmut sentinel for string mutations: wraps the string in `XX...XX`
    if "XX" in after and "XX" not in before:
        return "string_literal"

    pairs: list[tuple[str, str, str]] = [
        (r"\bTrue\b", r"\bFalse\b", "CRC_True_to_False"),
        (r"\bFalse\b", r"\bTrue\b", "CRC_False_to_True"),
        (r"\band\b", r"\bor\b", "LCR_and_to_or"),
        (r"\bor\b", r"\band\b", "LCR_or_to_and"),
        (r"\bnot\b", "", "LCR_not_removal"),
        (r">=", r"\bin\b", "ROR"),
        (r">=", r">", "ROR_ge_to_gt"),
        (r"<=", r"<", "ROR_le_to_lt"),
        (r">(?!=)", r">=", "ROR_gt_to_ge"),
        (r"<(?!=)", r"<=", "ROR_lt_to_le"),
        (r"==", r"!=", "ROR_eq_to_ne"),
        (r"!=", r"==", "ROR_ne_to_eq"),
        (r"\+", r"-", "AOR_add_to_sub"),
        (r"-", r"\+", "AOR_sub_to_add"),
        (r"\*(?!\*)", r"/", "AOR_mul_to_div"),
        (r"/", r"\*", "AOR_div_to_mul"),
        (r"\bNone\b", "", "CRC_None_replaced"),
    ]

    for pat_before, pat_after, label in pairs:
        if pat_before and not re.search(pat_before, before):
            continue
        if pat_after and not re.search(pat_after, after):
            continue
        if pat_before and re.search(pat_before, before) and (not pat_after or re.search(pat_after, after)):
            return label

    # Catch-all: numeric constant change (e.g. `30` -> `31`)
    if re.search(r"\b\d+\b", before) and re.search(r"\b\d+\b", after):
        before_nums = re.findall(r"\b\d+\b", before)
        after_nums = re.findall(r"\b\d+\b", after)
        if before_nums != after_nums:
            return "CRC_numeric"

    # Catch-all: dict.get default value swap (e.g. `.get(k, {})` -> `.get(k, None)`)
    if "{}" in before and "None" in after:
        return "CRC_dict_default"

    return "unknown"


# ---------------------------------------------------------------------------
# AST extraction from mutated source file
# ---------------------------------------------------------------------------


@dataclass
class FunctionVariants:
    """Holds the original function and all mutated variants for one source func."""

    name: str
    orig: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    variants: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)


_VARIANT_NAME_RE = re.compile(r"^x_(.+?)__mutmut_(orig|(\d+))$")


def parse_mutated_source(mutated_path: Path) -> dict[str, FunctionVariants]:
    """Parse mutmut's mutated .py file and group functions by base name.

    The mutated file contains, for each original function `foo`:
        x_foo__mutmut_orig (the original implementation, renamed)
        x_foo__mutmut_1, x_foo__mutmut_2, ... (mutated variants)

    Plus the trampoline `foo()` that dispatches to the right variant based on
    `os.environ['MUTANT_UNDER_TEST']`. We ignore trampolines — only `x_*` matter.
    """
    source = mutated_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    variants: dict[str, FunctionVariants] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        m = _VARIANT_NAME_RE.match(node.name)
        if not m:
            continue
        base_name = m.group(1)
        kind = m.group(2)
        fv = variants.setdefault(base_name, FunctionVariants(name=base_name))
        if kind == "orig":
            fv.orig = node
        else:
            fv.variants[int(kind)] = node

    return variants


def diff_for_variant(fv: FunctionVariants, mutant_n: int) -> tuple[list[str], str, int | None]:
    """Compute unified diff between a mutated variant and the original.

    Returns (diff_lines [-line, +line], raw_unified_diff, first_changed_lineno).
    """
    if fv.orig is None or mutant_n not in fv.variants:
        return [], "", None

    orig_src = ast.unparse(fv.orig).splitlines()
    mutant_src = ast.unparse(fv.variants[mutant_n]).splitlines()

    diff = list(
        difflib.unified_diff(
            orig_src,
            mutant_src,
            fromfile=f"x_{fv.name}__mutmut_orig",
            tofile=f"x_{fv.name}__mutmut_{mutant_n}",
            lineterm="",
            n=2,
        )
    )

    # Pull first - / + line for operator classification
    diff_pair: list[str] = []
    for line in diff:
        if line.startswith("-") and not line.startswith("---"):
            diff_pair.append(line)
        elif line.startswith("+") and not line.startswith("+++"):
            diff_pair.append(line)
        if len(diff_pair) >= 2:
            break

    # First @@ hunk line gives the changed line offset within the function.
    first_line: int | None = None
    for line in diff:
        m = re.match(r"^@@\s+-(\d+)", line)
        if m:
            first_line = int(m.group(1))
            break

    return diff_pair, "\n".join(diff), first_line


# ---------------------------------------------------------------------------
# Build mutant records from .meta + mutated source
# ---------------------------------------------------------------------------


_KEY_RE = re.compile(r"^([\w.]+)\.x_([\w]+)__mutmut_(\d+)$")


def build_mutants(meta: dict, variants: dict[str, FunctionVariants], source_rel_path: str) -> list[Mutant]:
    """Assemble Mutant records from `.meta` exit codes + AST diffs."""
    mutants: list[Mutant] = []
    exit_codes = meta.get("exit_code_by_key", {})

    for key, code in exit_codes.items():
        m = _KEY_RE.match(key)
        if not m:
            continue
        _module, func_name, n_str = m.groups()
        n = int(n_str)
        fv = variants.get(func_name)
        if fv is None:
            continue

        diff_pair, raw_diff, line_no = diff_for_variant(fv, n)
        before = diff_pair[0][1:] if diff_pair else ""
        after = diff_pair[1][1:] if len(diff_pair) > 1 else ""
        operator = infer_operator(before, after) if (before or after) else None

        mutants.append(
            Mutant(
                id=key,
                file=source_rel_path,
                line=line_no,
                function=func_name,
                status=status_from_exit_code(code),
                operator=operator,
                diff_lines=diff_pair,
                raw_diff=raw_diff,
                covering_tests=[],  # filled in below
            )
        )

    return mutants


def attach_covering_tests(mutants: list[Mutant], stats_path: Path) -> None:
    """Populate `covering_tests` field from mutmut-stats.json.

    Stats key format: `<module>.x_<funcname>` — same as mutant.function (after
    stripping module). We match by function name.
    """
    if not stats_path.exists():
        return
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    tests_by_func = stats.get("tests_by_mangled_function_name", {})

    # Build a function -> tests index (drop module prefix, drop x_ prefix)
    fn_index: dict[str, list[str]] = {}
    for key, tests in tests_by_func.items():
        m = re.match(r".*\.x_(.+)$", key)
        if m:
            fn_index[m.group(1)] = list(tests)

    for mut in mutants:
        if mut.function:
            mut.covering_tests = fn_index.get(mut.function, [])


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate(mutants: list[Mutant]) -> dict:
    summary = {
        "total": len(mutants),
        "killed": 0,
        "survived": 0,
        "timeout": 0,
        "suspicious": 0,
        "skipped": 0,
        "mutation_score": 0.0,
    }
    by_operator: dict[str, dict[str, int]] = defaultdict(
        lambda: {"killed": 0, "survived": 0, "timeout": 0, "suspicious": 0, "total": 0}
    )
    by_file: dict[str, dict] = defaultdict(
        lambda: {
            "killed": 0,
            "survived": 0,
            "timeout": 0,
            "suspicious": 0,
            "total": 0,
            "mutants": [],
        }
    )
    by_function: dict[str, dict[str, int]] = defaultdict(
        lambda: {"killed": 0, "survived": 0, "timeout": 0, "suspicious": 0, "total": 0}
    )

    for mut in mutants:
        status = mut.status if mut.status in summary else "suspicious"
        summary[status] = summary.get(status, 0) + 1

        op = mut.operator or "unknown"
        by_operator[op][status] = by_operator[op].get(status, 0) + 1
        by_operator[op]["total"] += 1

        by_file[mut.file][status] = by_file[mut.file].get(status, 0) + 1
        by_file[mut.file]["total"] += 1
        by_file[mut.file]["mutants"].append(asdict(mut))

        if mut.function:
            fkey = f"{mut.file}::{mut.function}"
            by_function[fkey][status] = by_function[fkey].get(status, 0) + 1
            by_function[fkey]["total"] += 1

    denom = summary["killed"] + summary["survived"]
    summary["mutation_score"] = round(summary["killed"] * 100 / denom, 2) if denom else 0.0

    return {
        "summary": summary,
        "by_operator": dict(by_operator),
        "by_file": dict(by_file),
        "by_function": dict(by_function),
        "survived_mutants": [asdict(m) for m in mutants if m.status == "survived"],
    }


# ---------------------------------------------------------------------------
# Discovery & main
# ---------------------------------------------------------------------------


def discover_meta_files(mutants_dir: Path) -> list[tuple[Path, Path, str]]:
    """Find all `<source>.py.meta` + matching mutated `.py` pairs under mutants_dir.

    Returns list of (mutated_py_path, meta_path, rel_source_path).
    `rel_source_path` is the path of the original source (e.g. "scripts/foo.py").
    """
    pairs: list[tuple[Path, Path, str]] = []
    for meta in mutants_dir.rglob("*.py.meta"):
        py = meta.with_suffix("")  # drop ".meta", leaves "...py"
        if not py.exists():
            continue
        rel = py.relative_to(mutants_dir).as_posix()
        pairs.append((py, meta, rel))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mutants-dir",
        type=Path,
        default=Path.cwd() / "mutants",
        help="Path to mutmut's mutants/ directory (default: ./mutants).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write JSON to this path. Default: stdout.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only the human summary to stderr, skip JSON output.",
    )
    args = parser.parse_args()

    if not args.mutants_dir.exists():
        print(
            f"error: mutants directory not found: {args.mutants_dir}\nRun `mutmut run` first, or pass --mutants-dir.",
            file=sys.stderr,
        )
        return 2

    pairs = discover_meta_files(args.mutants_dir)
    if not pairs:
        print(
            f"error: no `*.py.meta` files found under {args.mutants_dir}",
            file=sys.stderr,
        )
        return 2

    stats_path = args.mutants_dir / "mutmut-stats.json"
    all_mutants: list[Mutant] = []

    for mutated_py, meta_path, rel in pairs:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"warning: cannot parse {meta_path}: {exc}", file=sys.stderr)
            continue
        try:
            variants = parse_mutated_source(mutated_py)
        except SyntaxError as exc:
            print(
                f"warning: cannot parse mutated source {mutated_py}: {exc}",
                file=sys.stderr,
            )
            continue
        mutants = build_mutants(meta, variants, rel)
        all_mutants.extend(mutants)

    attach_covering_tests(all_mutants, stats_path)

    output = aggregate(all_mutants)

    # Human summary to stderr
    s = output["summary"]
    print(
        f"\nSummary: {s['killed']} killed, {s['survived']} survived, "
        f"{s['timeout']} timeout, {s['suspicious']} suspicious — "
        f"score: {s['mutation_score']}%",
        file=sys.stderr,
    )
    print("\nDistribution by operator (survived first):", file=sys.stderr)
    sorted_ops = sorted(
        output["by_operator"].items(),
        key=lambda kv: kv[1].get("survived", 0),
        reverse=True,
    )
    for op, counts in sorted_ops:
        if counts.get("survived", 0) or counts.get("killed", 0):
            print(
                f"  {op:30s} survived={counts.get('survived', 0):4d}  killed={counts.get('killed', 0):4d}",
                file=sys.stderr,
            )

    print("\nTop 10 functions by survivor count:", file=sys.stderr)
    sorted_fns = sorted(
        output["by_function"].items(),
        key=lambda kv: kv[1].get("survived", 0),
        reverse=True,
    )[:10]
    for fkey, counts in sorted_fns:
        if counts.get("survived", 0):
            print(
                f"  {fkey:60s} survived={counts['survived']:4d}  killed={counts.get('killed', 0):4d}",
                file=sys.stderr,
            )

    if args.summary_only:
        return 0

    out_json = json.dumps(output, indent=2, default=str)
    if args.out:
        args.out.write_text(out_json)
        print(f"\nWrote {len(all_mutants)} mutants to {args.out}", file=sys.stderr)
    else:
        print(out_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())

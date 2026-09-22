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

Exit codes in `.meta` — mirrors `status_by_exit_code` in mutmut/__main__.py (3.5.0).
Verified against the source; do not "simplify" this table:
  0            survived (tests passed under mutation — the gap you are hunting)
  1, 3         killed (a test failed — mutation caught)
  5, 33        no tests (no test covers this function — NOT a kill, NOT a gap)
  34           skipped
  36, 24, -24, 152, 255   timeout
  37           caught by type check
  2            check was interrupted by user
  -9, -11      segfault
  None         not checked
  anything else            suspicious

Usage:
    python parse_results.py --mutants-dir <path> [--out FILE]
    python parse_results.py                     # auto-detect ./mutants

Output JSON structure:
    {
      # Seeded from ALL_STATUSES, so every status appears — including
      # "no tests", which is deliberately excluded from mutation_score.
      "summary": {<every status in ALL_STATUSES>, "total", "mutation_score"},
      "by_operator": {"<op>": {"killed": int, "survived": int, ...}},
      "by_file": {"<filepath>": {"killed": int, "survived": int, "mutants": [...]}},
      "by_function": {"<filepath>::<function>": {...}},
      "survived_mutants": [
        {"id", "file", "line", "function", "operator", "diff_lines", "raw_diff",
         "covering_tests", "line_offset_in_function"},
        ...
      ]
    }

`line` is the enclosing function's `def` line, NOT the mutated statement's:
diffs come from `ast.unparse`, which renumbers from 1 and strips comments, so
the statement's true line is unrecoverable and `line + offset` is not it either.
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
    # The enclosing function's `def` line in the original file — NOT the
    # mutated statement's line, which `ast.unparse` makes unrecoverable.
    line: int | None
    function: str | None
    status: str
    operator: str | None
    diff_lines: list[str] = field(default_factory=list)
    raw_diff: str = ""
    covering_tests: list[str] = field(default_factory=list)
    # Offset within the unparsed function body. Not a file line; adding it to
    # `line` does not yield one either (unparse drops comments and blanks).
    line_offset_in_function: int | None = None


# ---------------------------------------------------------------------------
# Status mapping (mutmut 3.5+ exit code semantics)
# ---------------------------------------------------------------------------

# Transcribed verbatim from `status_by_exit_code` in mutmut/__main__.py (3.5.0).
# Note: mutmut's own dict lists -24 twice (killed, then timeout); the later
# literal wins, so -24 is a timeout. Keep this in sync with the installed
# mutmut version rather than trusting any blog post.
_STATUS_BY_EXIT_CODE: dict[int | None, str] = {
    0: "survived",
    1: "killed",
    3: "killed",  # internal pytest error still means the mutant was caught
    5: "no tests",
    33: "no tests",  # mutmut skipped it: no test covers this function
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    37: "caught by type check",
    2: "check was interrupted by user",
    24: "timeout",
    -24: "timeout",  # SIGXCPU
    152: "timeout",
    255: "timeout",
    -9: "segfault",
    -11: "segfault",
    None: "not checked",
}

# Statuses that represent a real verdict about test strength. Everything else
# ("no tests", "skipped", "not checked", ...) must be reported separately and
# kept out of the mutation score, or the score silently lies.
SCORED_STATUSES = ("killed", "survived")


def status_from_exit_code(code: int | None) -> str:
    """Map the exit code stored in mutmut's .meta to a mutmut 3.5 status.

    Counter-intuitive but correct: exit 0 means the test suite PASSED while the
    mutation was active, i.e. the mutant SURVIVED. Do not invert this.

    Equally important: 33 is "no tests", not "killed". Treating it as a kill
    inflates the mutation score by counting untested code as tested.
    """
    return _STATUS_BY_EXIT_CODE.get(code, "suspicious")


# ---------------------------------------------------------------------------
# Operator inference from diff lines
# ---------------------------------------------------------------------------


def _infer_keyword_operator(before: str, after: str) -> str | None:
    """mutmut 3 `operator_keywords`: is/is not, in/not in, break->return, continue->break.

    Note there is NO statement-removal operator in mutmut 3, so an emptied or
    `return`ed line is the break/continue mapping, never "SBR".
    """
    b, a = before.strip(), after.strip()
    if b == "break" and a == "return":
        return "break_to_return"
    if b == "continue" and a == "break":
        return "continue_to_break"
    for pattern, added_label, removed_label in (
        (r"\bis\s+not\b", "keyword_is_to_is_not", "keyword_is_not_to_is"),
        (r"\bnot\s+in\b", "keyword_in_to_not_in", "keyword_not_in_to_in"),
    ):
        in_before = re.search(pattern, before) is not None
        in_after = re.search(pattern, after) is not None
        if in_after and not in_before:
            return added_label
        if in_before and not in_after:
            return removed_label
    if not a or a in {"pass", "return"}:
        return "statement_neutralized"
    return None


def _infer_string_operator(before: str, after: str) -> str | None:
    """mutmut 3 `operator_string`: XX-wrap, and lower()/upper() of the literal."""
    if "XX" in after and "XX" not in before:
        return "string_literal"
    # Case-only edit: same characters, different case. Nearly always noise
    # (messages, case-insensitively compared keys), so give it its own bucket
    # instead of burying it in "unknown".
    if before != after and before.lower() == after.lower():
        return "string_case"
    return None


def _infer_arg_operator(before: str, after: str) -> str | None:
    """mutmut 3 `operator_arg_removal`: each arg -> None, or an arg dropped.

    Checked before the relational/arithmetic pairs: a nulled argument often
    still contains an operator from the original expression and would otherwise
    be mislabelled as ROR/AOR.
    """
    if re.search(r"\bNone\b", after) and not re.search(r"\bNone\b", before):
        return "arg_to_None"
    if before.count(",") > after.count(",") and before.count("(") == after.count("("):
        return "arg_dropped"
    return None


def infer_operator(before: str, after: str) -> str | None:
    """Best-effort operator classification.

    These labels are an analytical taxonomy inferred from the diff — mutmut does
    not report operators. Order matters: most specific first.
    """
    if not before.strip() and not after.strip():
        return None

    # String detection runs FIRST. `operator_string` upper/lower-cases whole
    # literals, so `msg = 'User is not active'` -> `'USER IS NOT ACTIVE'` looks
    # like an `is not` removal to the keyword detector (which is case-sensitive
    # and cannot see quotes) and lands in real_test_gap instead of noise.
    for detector in (_infer_string_operator, _infer_keyword_operator, _infer_arg_operator):
        label = detector(before, after)
        if label is not None:
            return label

    pairs: list[tuple[str, str, str]] = [
        (r"\bTrue\b", r"\bFalse\b", "CRC_True_to_False"),
        (r"\bFalse\b", r"\bTrue\b", "CRC_False_to_True"),
        (r"\band\b", r"\bor\b", "LCR_and_to_or"),
        (r"\bor\b", r"\band\b", "LCR_or_to_and"),
        (r"\bnot\b", "", "LCR_not_removal"),
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
# Operator taxonomy — signal ranking for triage
# ---------------------------------------------------------------------------
# `infer_operator` is the only producer of these labels, so the ranking lives
# beside it and consumers import it. A second hand-written copy drifts silently:
# every label here must be one `infer_operator` can actually return.

# Almost always a genuine test gap in business logic.
HIGH_SIGNAL_OPERATORS: frozenset[str] = frozenset(
    {
        "ROR_gt_to_ge",
        "ROR_ge_to_gt",
        "ROR_lt_to_le",
        "ROR_le_to_lt",
        "ROR_eq_to_ne",
        "ROR_ne_to_eq",
        "AOR_add_to_sub",
        "AOR_sub_to_add",
        "AOR_mul_to_div",
        "AOR_div_to_mul",
        "LCR_and_to_or",
        "LCR_or_to_and",
        "LCR_not_removal",
        # Control-flow keywords: NOT equivalent, despite the old break<->continue
        # folklore — they change which iterations run.
        "break_to_return",
        "continue_to_break",
    }
)

# Usually noise: display text and nulled message/log arguments.
NOISE_OPERATORS: dict[str, str] = {
    "string_literal": "string mutation — rarely a gap unless the string drives business logic",
    "string_case": "case-only string edit — messages and case-insensitive keys",
}


# ---------------------------------------------------------------------------
# AST extraction from mutated source file
# ---------------------------------------------------------------------------


@dataclass
class FunctionVariants:
    """Holds the original function and all mutated variants for one source func."""

    name: str
    orig: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    variants: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    # Cache: the original is re-unparsed once per mutant otherwise, and a
    # function with many survivors is exactly the case this tool is built for.
    _orig_lines: list[str] | None = field(default=None, repr=False, compare=False)

    def orig_lines(self) -> list[str]:
        """Unparsed source of the original, computed once per function."""
        if self._orig_lines is None:
            if self.orig is None:
                return []
            self._orig_lines = _unparse_under_name(self.orig, self.name).splitlines()
        return self._orig_lines


# mutmut's `mangle_function_name` (see mutmut/trampoline_templates.py) produces
#   module-level function `foo`      -> "x_foo"
#   method `Bar.foo`                 -> "xǁBarǁfoo"   (U+01C1 separator)
# Matching only "x_" silently drops every method — i.e. nearly all of an OOP
# codebase — and the report then claims zero mutants. Handle both forms.
CLASS_NAME_SEPARATOR = "ǁ"
_MANGLED_PREFIXES = ("x_", "x" + CLASS_NAME_SEPARATOR)


def split_variant_name(name: str) -> tuple[str, str] | None:
    """`xǁBarǁfoo__mutmut_3` -> ("xǁBarǁfoo", "3"); non-variants -> None."""
    if "__mutmut_" not in name:
        return None
    mangled, _, kind = name.rpartition("__mutmut_")
    if not mangled.startswith(_MANGLED_PREFIXES):
        return None
    if kind != "orig" and not kind.isdigit():
        return None
    return mangled, kind


def human_function_name(mangled: str) -> str:
    """`xǁBarǁfoo` -> "Bar.foo"; `x_foo` -> "foo"."""
    if mangled.startswith("x" + CLASS_NAME_SEPARATOR):
        return ".".join(mangled.split(CLASS_NAME_SEPARATOR)[1:])
    return mangled.removeprefix("x_")


def parse_mutated_source(mutated_path: Path) -> dict[str, FunctionVariants]:
    """Parse mutmut's mutated .py file, keyed by mutmut's mangled function name.

    The mutated file contains, for each original function `foo`:
        x_foo__mutmut_orig (the original implementation, renamed)
        x_foo__mutmut_1, x_foo__mutmut_2, ... (mutated variants)
    and for a method `Bar.foo`, the same with `xǁBarǁfoo`.

    Plus the trampoline `foo()` that dispatches to the right variant based on
    `os.environ['MUTANT_UNDER_TEST']`. We ignore trampolines.

    Keys are the mangled name so they join directly against `.meta` keys and
    `mutmut-stats.json` keys, which use the same mangling.
    """
    source = mutated_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    variants: dict[str, FunctionVariants] = {}
    # ast.walk descends into ClassDef bodies, so methods are covered too.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        split = split_variant_name(node.name)
        if split is None:
            continue
        mangled, kind = split
        fv = variants.setdefault(mangled, FunctionVariants(name=mangled))
        if kind == "orig":
            fv.orig = node
        else:
            fv.variants[int(kind)] = node

    return variants


def _collect_def_lines(node: ast.AST, prefix: tuple[str, ...], out: dict[str, int]) -> None:
    """Walk ClassDefs to record `def` lines under dotted names (`Bar.foo`)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            _collect_def_lines(child, (*prefix, child.name), out)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # `lineno` is the `def` line, not the first decorator — which is
            # what we want. Nested defs are not mangled separately by mutmut,
            # so we do not descend into function bodies.
            out[".".join((*prefix, child.name))] = child.lineno


def def_lines_from_source(source_path: Path) -> dict[str, int]:
    """Map `foo` / `Bar.foo` -> the `def` line in the ORIGINAL source file.

    The mutated copy renames and reorders functions and `ast.unparse` renumbers
    from 1, so only the untouched original knows real line numbers.

    Returns {} when unreadable or unparseable: a missing line degrades the
    report, it must never abort the run.
    """
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return {}
    out: dict[str, int] = {}
    _collect_def_lines(tree, (), out)
    return out


def _unparse_under_name(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> str:
    """Unparse a function node as if it were named `name`.

    Swaps the name in place and restores it rather than deep-copying the node:
    this runs once per mutant and deep-copying every AST would dominate runtime.
    """
    original = node.name
    node.name = name
    try:
        return ast.unparse(node)
    finally:
        node.name = original


def diff_for_variant(fv: FunctionVariants, mutant_n: int) -> tuple[list[str], str, int | None]:
    """Compute unified diff between a mutated variant and the original.

    Returns (diff_lines [-line, +line], raw_unified_diff, line_offset_in_function).

    The two variants are unparsed under a common function name. Without that, the
    `def` line always differs (`..__mutmut_orig` vs `..__mutmut_13`) and becomes
    the first hunk, so every mutant is reported as a rename of itself and the
    real mutation never shows up — which also leaves the operator as "unknown".
    """
    if fv.orig is None or mutant_n not in fv.variants:
        return [], "", None

    orig_src = fv.orig_lines()
    mutant_src = _unparse_under_name(fv.variants[mutant_n], fv.name).splitlines()

    diff = list(
        difflib.unified_diff(
            orig_src,
            mutant_src,
            fromfile=f"{fv.name}__mutmut_orig",
            tofile=f"{fv.name}__mutmut_{mutant_n}",
            lineterm="",
            n=2,
        )
    )

    # Pull first - / + line for operator classification
    diff_pair: list[str] = []
    for line in diff:
        if (line.startswith("-") and not line.startswith("---")) or (
            line.startswith("+") and not line.startswith("+++")
        ):
            diff_pair.append(line)
        if len(diff_pair) >= 2:
            break

    return diff_pair, "\n".join(diff), _changed_line_offset(diff)


def _changed_line_offset(diff: list[str]) -> int | None:
    """Offset of the first removed line within the unparsed function.

    The `@@` header marks where the context window starts — `n` lines *before*
    the mutation — so it is not itself the answer. Walk from there, advancing
    only on context and removed lines; added lines have no original counterpart.
    """
    cursor: int | None = None
    hunk_start: int | None = None
    for line in diff:
        m = re.match(r"^@@\s+-(\d+)", line)
        if m:
            cursor = int(m.group(1))
            if hunk_start is None:
                hunk_start = cursor
            continue
        if cursor is None:
            continue
        if line.startswith("-") and not line.startswith("---"):
            return cursor
        if line.startswith(" "):
            cursor += 1
    # Pure insertion (no removed line): the hunk start is the best anchor.
    return hunk_start


# ---------------------------------------------------------------------------
# Build mutant records from .meta + mutated source
# ---------------------------------------------------------------------------


def split_mutant_key(key: str) -> tuple[str, str, int] | None:
    """`pkg.mod.xǁBarǁfoo__mutmut_3` -> ("pkg.mod", "xǁBarǁfoo", 3).

    Parsed by partition rather than a regex: the module prefix is whatever mutmut
    derived from the file path, which can contain characters (`-` in
    `custom-addons`, for one) that a `[\\w.]+` pattern silently rejects.
    """
    if "__mutmut_" not in key:
        return None
    qualified, _, kind = key.rpartition("__mutmut_")
    if not kind.isdigit():
        return None
    # Strip the module prefix *before* checking the mangling: the prefix check
    # applies to the bare function name, not to `pkg.mod.xǁBarǁfoo`.
    module, _, mangled = qualified.rpartition(".")
    if not mangled.startswith(_MANGLED_PREFIXES):
        return None
    return module, mangled, int(kind)


def build_mutants(
    meta: dict,
    variants: dict[str, FunctionVariants],
    source_rel_path: str,
    def_lines: dict[str, int] | None = None,
) -> list[Mutant]:
    """Assemble Mutant records from `.meta` exit codes + AST diffs.

    `def_lines` maps human function name -> real `def` line in the original
    source (see `def_lines_from_source`). Omit it and `line` stays None rather
    than reporting the in-function offset as if it were a file line.
    """
    mutants: list[Mutant] = []
    exit_codes = meta.get("exit_code_by_key", {})
    def_lines = def_lines or {}

    for key, code in exit_codes.items():
        parsed = split_mutant_key(key)
        if parsed is None:
            continue
        _module, mangled, n = parsed
        fv = variants.get(mangled)
        if fv is None:
            continue

        diff_pair, raw_diff, offset = diff_for_variant(fv, n)
        before = diff_pair[0][1:] if diff_pair else ""
        after = diff_pair[1][1:] if len(diff_pair) > 1 else ""
        operator = infer_operator(before, after) if (before or after) else None
        human = human_function_name(mangled)

        mutants.append(
            Mutant(
                id=key,
                file=source_rel_path,
                line=def_lines.get(human),
                function=human,
                status=status_from_exit_code(code),
                operator=operator,
                diff_lines=diff_pair,
                raw_diff=raw_diff,
                covering_tests=[],  # filled in below
                line_offset_in_function=offset,
            )
        )

    return mutants


def attach_covering_tests(mutants: list[Mutant], stats_path: Path) -> None:
    """Populate `covering_tests` field from mutmut-stats.json.

    Stats keys are `<module>.<mangled>` (e.g. `pkg.mod.xǁBarǁfoo`). Join on the
    module-qualified name from `Mutant.id`, never the bare function name: the
    module prefix is the only thing separating same-named functions in
    different files (`validate`, `save`), whose test lists would otherwise swap.
    """
    if not stats_path.exists():
        return
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    tests_by_func = stats.get("tests_by_mangled_function_name", {})

    fn_index: dict[str, list[str]] = {}
    for key, tests in tests_by_func.items():
        _module, _, mangled = key.rpartition(".")
        if mangled.startswith(_MANGLED_PREFIXES):
            fn_index[key] = list(tests)

    for mut in mutants:
        parsed = split_mutant_key(mut.id)
        if parsed is None:
            continue
        module, mangled, _n = parsed
        qualified = f"{module}.{mangled}" if module else mangled
        mut.covering_tests = fn_index.get(qualified, [])


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


# Every status the exit-code table can produce. Counters are pre-seeded with
# these so a status can never be silently folded into "suspicious".
ALL_STATUSES = (
    "killed",
    "survived",
    "no tests",
    "timeout",
    "suspicious",
    "skipped",
    "caught by type check",
    "check was interrupted by user",
    "segfault",
    "not checked",
)


def _new_counter() -> dict[str, int]:
    return dict.fromkeys(ALL_STATUSES, 0) | {"total": 0}


def aggregate(mutants: list[Mutant]) -> dict:
    summary: dict[str, float] = _new_counter() | {
        "total": len(mutants),
        "mutation_score": 0.0,
    }
    by_operator: dict[str, dict[str, int]] = defaultdict(_new_counter)
    by_file: dict[str, dict] = defaultdict(lambda: _new_counter() | {"mutants": []})
    by_function: dict[str, dict[str, int]] = defaultdict(_new_counter)

    for mut in mutants:
        status = mut.status if mut.status in ALL_STATUSES else "suspicious"
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

    denom = sum(summary[s] for s in SCORED_STATUSES)
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


def format_summary(output: dict) -> list[str]:
    """Human summary lines for stderr.

    "no tests" gets its own line and is never folded into the score: a project
    with uncovered functions must not read as e.g. 94% while the untested count
    sits invisible. Silent omission is the exact trap the taxonomy exists for.
    """
    s = output["summary"]
    lines = [
        f"\nSummary: {s['killed']} killed, {s['survived']} survived, "
        f"{s['timeout']} timeout, {s['suspicious']} suspicious — "
        f"score: {s['mutation_score']}%",
    ]
    no_tests = s.get("no tests", 0)
    if no_tests:
        lines.append(
            f"  ⚠️  {no_tests} mutant(s) had NO covering test — untested code, "
            f"NOT counted in the score. Add coverage before trusting it."
        )

    lines.append("\nDistribution by operator (survived first):")
    sorted_ops = sorted(output["by_operator"].items(), key=lambda kv: kv[1].get("survived", 0), reverse=True)
    for op, counts in sorted_ops:
        if counts.get("survived", 0) or counts.get("killed", 0):
            lines.append(f"  {op:30s} survived={counts.get('survived', 0):4d}  killed={counts.get('killed', 0):4d}")

    lines.append("\nTop 10 functions by survivor count:")
    sorted_fns = sorted(output["by_function"].items(), key=lambda kv: kv[1].get("survived", 0), reverse=True)[:10]
    for fkey, counts in sorted_fns:
        if counts.get("survived", 0):
            lines.append(f"  {fkey:60s} survived={counts['survived']:4d}  killed={counts.get('killed', 0):4d}")
    return lines


def collect_mutants(mutants_dir: Path, pairs: list[tuple[Path, Path, str]]) -> list[Mutant]:
    """Build the full mutant list from discovered (mutated, meta, rel) triples.

    A corrupt `.meta` or an unparseable mutated file skips that source with a
    warning rather than aborting: one bad file must not sink a whole run.
    """
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
        # Real `def` lines come from the untouched original, which lives at the
        # same relative path outside mutants/ — never from the mutated copy.
        def_lines = def_lines_from_source(mutants_dir.parent / rel)
        all_mutants.extend(build_mutants(meta, variants, rel, def_lines))
    return all_mutants


def _build_arg_parser() -> argparse.ArgumentParser:
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
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()

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

    all_mutants = collect_mutants(args.mutants_dir, pairs)
    attach_covering_tests(all_mutants, args.mutants_dir / "mutmut-stats.json")

    output = aggregate(all_mutants)

    for line in format_summary(output):
        print(line, file=sys.stderr)

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

"""Tests for parse_results.py — the mutmut 3.5 result parser.

These lock down the four traps that made earlier versions of this script
report confident nonsense:
  * exit 0 means "survived", not "killed";
  * exit 33 means "no tests", not "killed";
  * methods are mangled `xǁClassǁmethod`, not `x_name`;
  * module prefixes can contain characters a `[\\w.]+` regex rejects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from parse_results import (
    Mutant,
    aggregate,
    attach_covering_tests,
    build_mutants,
    collect_mutants,
    def_lines_from_source,
    diff_for_variant,
    discover_meta_files,
    format_summary,
    human_function_name,
    infer_operator,
    main,
    parse_mutated_source,
    split_mutant_key,
    split_variant_name,
    status_from_exit_code,
)

# ---------------------------------------------------------------------------
# status_from_exit_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, "survived"),
        (1, "killed"),
        (3, "killed"),
        (5, "no tests"),
        (33, "no tests"),
        (34, "skipped"),
        (35, "suspicious"),
        (36, "timeout"),
        (37, "caught by type check"),
        (2, "check was interrupted by user"),
        (24, "timeout"),
        (-24, "timeout"),
        (152, "timeout"),
        (255, "timeout"),
        (-9, "segfault"),
        (-11, "segfault"),
        (None, "not checked"),
    ],
)
def test_status_from_exit_code_matches_mutmut_table(code: int | None, expected: str) -> None:
    assert status_from_exit_code(code) == expected


def test_exit_zero_is_survived_not_killed() -> None:
    """Tests passing under mutation means the mutation went undetected."""
    assert status_from_exit_code(0) == "survived"
    assert status_from_exit_code(0) != "killed"


def test_exit_33_is_no_tests_not_killed() -> None:
    """Counting 33 as a kill reports untested code as tested."""
    assert status_from_exit_code(33) == "no tests"
    assert status_from_exit_code(33) != "killed"


def test_unknown_exit_code_is_suspicious() -> None:
    assert status_from_exit_code(4242) == "suspicious"


# ---------------------------------------------------------------------------
# split_variant_name
# ---------------------------------------------------------------------------


def test_split_variant_name_module_level_function() -> None:
    assert split_variant_name("x_foo__mutmut_3") == ("x_foo", "3")


def test_split_variant_name_method_uses_class_separator() -> None:
    """Methods mangle to `xǁBarǁfoo`; missing these drops an OOP codebase."""
    assert split_variant_name("xǁBarǁfoo__mutmut_12") == ("xǁBarǁfoo", "12")


def test_split_variant_name_keeps_orig() -> None:
    assert split_variant_name("x_foo__mutmut_orig") == ("x_foo", "orig")


@pytest.mark.parametrize(
    "name",
    [
        "foo",  # the trampoline, not a variant
        "helper_function",
        "y_foo__mutmut_1",  # not mutmut-mangled
        "x_foo__mutmut_abc",  # neither a digit nor "orig"
    ],
)
def test_split_variant_name_rejects_non_variants(name: str) -> None:
    assert split_variant_name(name) is None


# ---------------------------------------------------------------------------
# human_function_name
# ---------------------------------------------------------------------------


def test_human_function_name_method() -> None:
    assert human_function_name("xǁBarǁfoo") == "Bar.foo"


def test_human_function_name_module_level_function() -> None:
    assert human_function_name("x_foo") == "foo"


def test_human_function_name_preserves_inner_underscores() -> None:
    assert human_function_name("x_parse_mutated_source") == "parse_mutated_source"


# ---------------------------------------------------------------------------
# split_mutant_key
# ---------------------------------------------------------------------------


def test_split_mutant_key_module_level_function() -> None:
    assert split_mutant_key("pkg.mod.x_foo__mutmut_3") == ("pkg.mod", "x_foo", 3)


def test_split_mutant_key_method() -> None:
    assert split_mutant_key("pkg.mod.xǁBarǁfoo__mutmut_7") == ("pkg.mod", "xǁBarǁfoo", 7)


def test_split_mutant_key_accepts_hyphenated_module_prefix() -> None:
    """A `[\\w.]+` regex silently rejected this; partitioning must not."""
    assert split_mutant_key("custom-addons.mod.xǁBarǁfoo__mutmut_1") == (
        "custom-addons.mod",
        "xǁBarǁfoo",
        1,
    )


def test_split_mutant_key_returns_int_index() -> None:
    parsed = split_mutant_key("pkg.mod.x_foo__mutmut_42")
    assert parsed is not None
    assert parsed[2] == 42


@pytest.mark.parametrize(
    "key",
    [
        "pkg.mod.foo",  # no variant suffix
        "pkg.mod.x_foo__mutmut_orig",  # orig is not a numbered mutant
        "pkg.mod.y_foo__mutmut_1",  # not mutmut-mangled
    ],
)
def test_split_mutant_key_rejects_non_mutant_keys(key: str) -> None:
    assert split_mutant_key(key) is None


# ---------------------------------------------------------------------------
# infer_operator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        # operator_keywords — control flow is NOT equivalent
        ("break", "return", "break_to_return"),
        ("continue", "break", "continue_to_break"),
        ("if a is b:", "if a is not b:", "keyword_is_to_is_not"),
        ("if a is not b:", "if a is b:", "keyword_is_not_to_is"),
        ("if k in d:", "if k not in d:", "keyword_in_to_not_in"),
        ("if k not in d:", "if k in d:", "keyword_not_in_to_in"),
        # operator_string
        ("s = 'admin'", "s = 'XXadminXX'", "string_literal"),
        ("s = 'Admin'", "s = 'admin'", "string_case"),
        # operator_arg_removal
        ("log.info('x', rec)", "log.info(None, rec)", "arg_to_None"),
        # relational / arithmetic / logical
        ("if a >= b:", "if a > b:", "ROR_ge_to_gt"),
        ("if a == b:", "if a != b:", "ROR_eq_to_ne"),
        ("x = a and b", "x = a or b", "LCR_and_to_or"),
        ("flag = True", "flag = False", "CRC_True_to_False"),
    ],
)
def test_infer_operator_classifies_real_mutmut_operators(before: str, after: str, expected: str) -> None:
    assert infer_operator(before, after) == expected


def test_infer_operator_arg_removal_beats_arithmetic() -> None:
    """A nulled arg keeps the original operator; ROR/AOR must not claim it."""
    assert infer_operator("f(a + b, c)", "f(None, c)") == "arg_to_None"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("msg = 'User is not active'", "msg = 'USER IS NOT ACTIVE'"),
        ("msg = 'Key not in mapping'", "msg = 'KEY NOT IN MAPPING'"),
        ("raise UserError('Item is not valid')", "raise UserError('ITEM IS NOT VALID')"),
    ],
)
def test_infer_operator_keywords_inside_string_literals_are_string_mutations(before: str, after: str) -> None:
    """`operator_string` upper-cases whole literals; that is noise, not a keyword flip.

    The keyword detector is case-sensitive and cannot see quotes, so it reads
    the vanished lowercase `is not` as a keyword removal and promotes a known
    -noise mutant into the real_test_gap pile.
    """
    assert infer_operator(before, after) == "string_case"


def test_infer_operator_real_keyword_flip_still_wins_over_string() -> None:
    """Guard the reorder: genuine keyword mutations must not become strings."""
    assert infer_operator("if a is not b:", "if a is b:") == "keyword_is_not_to_is"
    assert infer_operator("if k in d:", "if k not in d:") == "keyword_in_to_not_in"


def test_infer_operator_arg_dropped() -> None:
    assert infer_operator("record(account, amount)", "record(account)") == "arg_dropped"


def test_infer_operator_ge_to_gt_with_membership_is_not_bare_ror() -> None:
    """A removed `(>=, in) -> "ROR"` row fired on ordinary `>=` + `in` code.

    `if x >= 5 and y in items:` -> `if x > 5 and ...` must be ROR_ge_to_gt, not
    the bare, unactionable "ROR" that pre-empted it.
    """
    result = infer_operator("if x >= 5 and y in items:", "if x > 5 and y in items:")
    assert result == "ROR_ge_to_gt"
    assert result != "ROR"


def test_infer_operator_numeric_constant() -> None:
    assert infer_operator("MAX = 30", "MAX = 31") == "CRC_numeric"


def test_infer_operator_blank_diff_is_none() -> None:
    assert infer_operator("", "") is None


def test_infer_operator_unrecognized_is_unknown() -> None:
    assert infer_operator("foo(bar)", "baz(qux)") == "unknown"


# ---------------------------------------------------------------------------
# parse_mutated_source + diff_for_variant
# ---------------------------------------------------------------------------

# A miniature of what mutmut writes to mutants/<source>.py: renamed original,
# numbered variants, and a trampoline that must be ignored. Includes a method
# to prove ClassDef descent works.
MUTATED_SOURCE = '''
def x_calc__mutmut_orig(a, b):
    if a > b:
        return a
    return b

def x_calc__mutmut_1(a, b):
    if a >= b:
        return a
    return b

def calc(a, b):
    """Trampoline — dispatches on MUTANT_UNDER_TEST. Must be ignored."""
    return x_calc__mutmut_orig(a, b)

class Bar:
    def xǁBarǁfoo__mutmut_orig(self, n):
        return n + 1

    def xǁBarǁfoo__mutmut_1(self, n):
        return n - 1

    def foo(self, n):
        return self.xǁBarǁfoo__mutmut_orig(n)
'''


@pytest.fixture()
def mutated_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(MUTATED_SOURCE, encoding="utf-8")
    return path


def test_parse_mutated_source_groups_module_function(mutated_file: Path) -> None:
    variants = parse_mutated_source(mutated_file)
    assert "x_calc" in variants
    assert variants["x_calc"].orig is not None
    assert set(variants["x_calc"].variants) == {1}


def test_parse_mutated_source_finds_methods_inside_classes(mutated_file: Path) -> None:
    """The `x_`-only regex dropped every method — an OOP codebase read as empty."""
    variants = parse_mutated_source(mutated_file)
    assert "xǁBarǁfoo" in variants
    assert variants["xǁBarǁfoo"].orig is not None
    assert set(variants["xǁBarǁfoo"].variants) == {1}


def test_parse_mutated_source_ignores_trampolines(mutated_file: Path) -> None:
    variants = parse_mutated_source(mutated_file)
    assert "calc" not in variants
    assert "foo" not in variants


def test_diff_for_variant_finds_the_real_mutation(mutated_file: Path) -> None:
    """Unparsing under a common name; otherwise the def line is the only hunk."""
    variants = parse_mutated_source(mutated_file)
    diff_pair, raw_diff, offset = diff_for_variant(variants["x_calc"], 1)

    assert any("if a > b:" in line for line in diff_pair)
    assert any("if a >= b:" in line for line in diff_pair)
    # The def line must NOT appear as the change — that was the old bug.
    assert not any("__mutmut_" in line for line in diff_pair)
    assert "def calc" not in raw_diff.split("@@")[-1]
    assert offset is not None


def test_diff_for_variant_operator_is_classifiable(mutated_file: Path) -> None:
    """End-to-end: a real diff must yield a real operator, never 'unknown'."""
    variants = parse_mutated_source(mutated_file)
    diff_pair, _raw, _offset = diff_for_variant(variants["x_calc"], 1)
    before, after = diff_pair[0][1:], diff_pair[1][1:]
    assert infer_operator(before, after) == "ROR_gt_to_ge"


def test_diff_for_variant_offset_points_at_the_mutated_line(mutated_file: Path) -> None:
    """Not the @@ hunk start: that is `n` context lines before the mutation.

    Unparsed x_calc is:
        1  def calc(a, b):
        2      if a > b:      <- the mutation
        3          return a
        4      return b
    """
    variants = parse_mutated_source(mutated_file)
    _pair, _raw, offset = diff_for_variant(variants["x_calc"], 1)
    assert offset == 2


def test_diff_for_variant_missing_variant_returns_empty(mutated_file: Path) -> None:
    variants = parse_mutated_source(mutated_file)
    assert diff_for_variant(variants["x_calc"], 99) == ([], "", None)


def test_diff_for_variant_does_not_mutate_node_names(mutated_file: Path) -> None:
    """_unparse_under_name renames in place; it must restore the original."""
    variants = parse_mutated_source(mutated_file)
    fv = variants["x_calc"]
    diff_for_variant(fv, 1)
    assert fv.orig is not None
    assert fv.orig.name == "x_calc__mutmut_orig"
    assert fv.variants[1].name == "x_calc__mutmut_1"


def test_orig_lines_are_cached(mutated_file: Path) -> None:
    fv = parse_mutated_source(mutated_file)["x_calc"]
    assert fv.orig_lines() is fv.orig_lines()


# ---------------------------------------------------------------------------
# def_lines_from_source
# ---------------------------------------------------------------------------

ORIGINAL_SOURCE = """import os


def calc(a, b):
    # a comment that ast.unparse would drop
    if a > b:
        return a
    return b


class Bar:
    @property
    def foo(self):
        return 1
"""


def test_def_lines_from_source_reports_real_def_lines(tmp_path: Path) -> None:
    path = tmp_path / "orig.py"
    path.write_text(ORIGINAL_SOURCE, encoding="utf-8")
    lines = def_lines_from_source(path)
    assert lines["calc"] == 4


def test_def_lines_from_source_handles_methods_and_decorators(tmp_path: Path) -> None:
    """Dotted name for methods; lineno is the `def`, not the decorator."""
    path = tmp_path / "orig.py"
    path.write_text(ORIGINAL_SOURCE, encoding="utf-8")
    lines = def_lines_from_source(path)
    assert lines["Bar.foo"] == 13


def test_def_lines_from_source_missing_file_is_empty(tmp_path: Path) -> None:
    assert def_lines_from_source(tmp_path / "nope.py") == {}


def test_def_lines_from_source_unparseable_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("def (((", encoding="utf-8")
    assert def_lines_from_source(path) == {}


# ---------------------------------------------------------------------------
# build_mutants
# ---------------------------------------------------------------------------


def test_build_mutants_uses_real_def_line_not_diff_offset(mutated_file: Path) -> None:
    """`line` must be the function's def line in the source, not an offset."""
    variants = parse_mutated_source(mutated_file)
    meta = {"exit_code_by_key": {"pkg.sample.x_calc__mutmut_1": 0}}
    mutants = build_mutants(meta, variants, "pkg/sample.py", {"calc": 42})

    assert len(mutants) == 1
    assert mutants[0].line == 42
    assert mutants[0].line_offset_in_function is not None
    assert mutants[0].line != mutants[0].line_offset_in_function


def test_build_mutants_without_def_lines_reports_no_line(mutated_file: Path) -> None:
    """Better a missing line than the offset masquerading as a file line."""
    variants = parse_mutated_source(mutated_file)
    meta = {"exit_code_by_key": {"pkg.sample.x_calc__mutmut_1": 0}}
    mutants = build_mutants(meta, variants, "pkg/sample.py")
    assert mutants[0].line is None


def test_build_mutants_populates_status_and_operator(mutated_file: Path) -> None:
    variants = parse_mutated_source(mutated_file)
    meta = {"exit_code_by_key": {"pkg.sample.x_calc__mutmut_1": 0}}
    mutants = build_mutants(meta, variants, "pkg/sample.py", {"calc": 42})

    assert mutants[0].status == "survived"
    assert mutants[0].operator == "ROR_gt_to_ge"
    assert mutants[0].function == "calc"
    assert mutants[0].file == "pkg/sample.py"


def test_build_mutants_skips_unparseable_and_unknown_keys(mutated_file: Path) -> None:
    variants = parse_mutated_source(mutated_file)
    meta = {
        "exit_code_by_key": {
            "not-a-mutant-key": 0,  # unparseable
            "pkg.sample.x_missing__mutmut_1": 0,  # no such function
            "pkg.sample.x_calc__mutmut_1": 1,  # the only real one
        }
    }
    mutants = build_mutants(meta, variants, "pkg/sample.py", {})
    assert [m.id for m in mutants] == ["pkg.sample.x_calc__mutmut_1"]


# ---------------------------------------------------------------------------
# attach_covering_tests
# ---------------------------------------------------------------------------


def _mutant(mut_id: str, file: str, function: str) -> Mutant:
    return Mutant(
        id=mut_id,
        file=file,
        line=1,
        function=function,
        status="survived",
        operator="ROR_gt_to_ge",
    )


def test_attach_covering_tests_does_not_mix_same_named_functions(tmp_path: Path) -> None:
    """Two `validate`s in different modules must not swap covering tests."""
    stats = tmp_path / "mutmut-stats.json"
    stats.write_text(
        json.dumps(
            {
                "tests_by_mangled_function_name": {
                    "pkg.alpha.x_validate": ["test_alpha.py::test_a"],
                    "pkg.beta.x_validate": ["test_beta.py::test_b"],
                }
            }
        ),
        encoding="utf-8",
    )
    mutants = [
        _mutant("pkg.alpha.x_validate__mutmut_1", "pkg/alpha.py", "validate"),
        _mutant("pkg.beta.x_validate__mutmut_1", "pkg/beta.py", "validate"),
    ]
    attach_covering_tests(mutants, stats)

    assert mutants[0].covering_tests == ["test_alpha.py::test_a"]
    assert mutants[1].covering_tests == ["test_beta.py::test_b"]


def test_attach_covering_tests_matches_methods(tmp_path: Path) -> None:
    stats = tmp_path / "mutmut-stats.json"
    stats.write_text(
        json.dumps({"tests_by_mangled_function_name": {"pkg.mod.xǁBarǁfoo": ["test_mod.py::test_foo"]}}),
        encoding="utf-8",
    )
    mutants = [_mutant("pkg.mod.xǁBarǁfoo__mutmut_2", "pkg/mod.py", "Bar.foo")]
    attach_covering_tests(mutants, stats)
    assert mutants[0].covering_tests == ["test_mod.py::test_foo"]


def test_attach_covering_tests_unknown_function_gets_empty_list(tmp_path: Path) -> None:
    stats = tmp_path / "mutmut-stats.json"
    stats.write_text(json.dumps({"tests_by_mangled_function_name": {}}), encoding="utf-8")
    mutants = [_mutant("pkg.mod.x_foo__mutmut_1", "pkg/mod.py", "foo")]
    attach_covering_tests(mutants, stats)
    assert mutants[0].covering_tests == []


def test_attach_covering_tests_missing_stats_file_is_noop(tmp_path: Path) -> None:
    mutants = [_mutant("pkg.mod.x_foo__mutmut_1", "pkg/mod.py", "foo")]
    attach_covering_tests(mutants, tmp_path / "absent.json")
    assert mutants[0].covering_tests == []


def test_attach_covering_tests_corrupt_stats_file_is_noop(tmp_path: Path) -> None:
    stats = tmp_path / "mutmut-stats.json"
    stats.write_text("{not json", encoding="utf-8")
    mutants = [_mutant("pkg.mod.x_foo__mutmut_1", "pkg/mod.py", "foo")]
    attach_covering_tests(mutants, stats)
    assert mutants[0].covering_tests == []


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


def _status_mutant(status: str, function: str = "foo") -> Mutant:
    return Mutant(
        id=f"pkg.mod.x_{function}__mutmut_1",
        file="pkg/mod.py",
        line=1,
        function=function,
        status=status,
        operator="ROR_gt_to_ge",
    )


def test_aggregate_excludes_no_tests_from_mutation_score() -> None:
    """The central claim: 'no tests' is not a kill and must not inflate score."""
    result = aggregate([_status_mutant("killed"), _status_mutant("survived"), _status_mutant("no tests")])
    summary = result["summary"]

    assert summary["total"] == 3
    assert summary["no tests"] == 1
    # 1 killed / (1 killed + 1 survived) — the "no tests" mutant is excluded.
    assert summary["mutation_score"] == 50.0


def test_aggregate_score_would_be_inflated_if_no_tests_counted() -> None:
    """Guard the arithmetic: counting 'no tests' as killed would give 66.67."""
    result = aggregate([_status_mutant("killed"), _status_mutant("survived"), _status_mutant("no tests")])
    assert result["summary"]["mutation_score"] != pytest.approx(66.67, abs=0.01)


def test_aggregate_seeds_every_status_counter() -> None:
    """Pre-seeding stops a status being silently folded into 'suspicious'."""
    summary = aggregate([_status_mutant("killed")])["summary"]
    for status in ("killed", "survived", "no tests", "timeout", "suspicious", "skipped", "segfault"):
        assert status in summary
    assert summary["survived"] == 0


def test_aggregate_unknown_status_counted_as_suspicious() -> None:
    summary = aggregate([_status_mutant("banana")])["summary"]
    assert summary["suspicious"] == 1


def test_aggregate_empty_input_scores_zero_not_crash() -> None:
    summary = aggregate([])["summary"]
    assert summary["total"] == 0
    assert summary["mutation_score"] == 0.0


def test_aggregate_all_killed_is_100_percent() -> None:
    summary = aggregate([_status_mutant("killed"), _status_mutant("killed")])["summary"]
    assert summary["mutation_score"] == 100.0


def test_aggregate_only_no_tests_scores_zero() -> None:
    """No verdicts at all must not divide by zero, nor claim a perfect score."""
    summary = aggregate([_status_mutant("no tests")])["summary"]
    assert summary["mutation_score"] == 0.0


def test_aggregate_groups_by_operator_and_function() -> None:
    result = aggregate([_status_mutant("survived", "alpha"), _status_mutant("killed", "beta")])

    assert result["by_operator"]["ROR_gt_to_ge"]["total"] == 2
    assert result["by_file"]["pkg/mod.py"]["total"] == 2
    assert result["by_function"]["pkg/mod.py::alpha"]["survived"] == 1
    assert result["by_function"]["pkg/mod.py::beta"]["killed"] == 1


# ---------------------------------------------------------------------------
# format_summary — the human-facing report must not hide "no tests"
# ---------------------------------------------------------------------------


def test_format_summary_surfaces_no_tests_count() -> None:
    """A project with uncovered functions must show the untested count, not
    just a flattering score."""
    output = aggregate([_status_mutant("killed"), _status_mutant("no tests"), _status_mutant("no tests")])
    text = "\n".join(format_summary(output))

    assert "2 mutant(s) had NO covering test" in text
    assert "NOT counted in the score" in text


def test_format_summary_omits_no_tests_line_when_zero() -> None:
    output = aggregate([_status_mutant("killed"), _status_mutant("survived")])
    text = "\n".join(format_summary(output))
    assert "NO covering test" not in text


def test_format_summary_always_reports_score_and_operators() -> None:
    output = aggregate([_status_mutant("survived", "alpha")])
    text = "\n".join(format_summary(output))
    assert "score:" in text
    assert "ROR_gt_to_ge" in text


# ---------------------------------------------------------------------------
# collect_mutants / main — the on-disk layout mutmut actually produces
# ---------------------------------------------------------------------------

# Original file. The comment on line 2 is the point: it exists only here, so
# `def` line 1 + offset 2 != the real line 3. They must never be summed.
E2E_ORIGINAL = """def pick_larger(a, b):
    # only in the original — ast.unparse drops it
    if a > b:
        return a
    return b
"""

E2E_MUTATED = """def x_pick_larger__mutmut_orig(a, b):
    if a > b:
        return a
    return b

def x_pick_larger__mutmut_1(a, b):
    if a >= b:
        return a
    return b

def pick_larger(a, b):
    return x_pick_larger__mutmut_orig(a, b)
"""


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A minimal replica of a real `mutmut run` result tree."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(E2E_ORIGINAL, encoding="utf-8")

    mutants = tmp_path / "mutants" / "src"
    mutants.mkdir(parents=True)
    (mutants / "calc.py").write_text(E2E_MUTATED, encoding="utf-8")
    # Real mutmut keys look like `calc.x_pick_larger__mutmut_1`; exit 0 =
    # survived.
    (mutants / "calc.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"calc.x_pick_larger__mutmut_1": 0}}),
        encoding="utf-8",
    )
    return tmp_path


def test_collect_mutants_resolves_line_from_original_not_mutated_copy(project: Path) -> None:
    """The def line must come from src/calc.py, never from mutants/src/calc.py.

    In the mutated copy the same function starts at line 1; in the original it
    also starts at line 1 — so assert on the offset/comment interaction that
    distinguishes them: the real `if a > b:` is line 3, and line+offset gives 2.
    """
    mutants_dir = project / "mutants"
    pairs = discover_meta_files(mutants_dir)
    found = collect_mutants(mutants_dir, pairs)

    assert len(found) == 1
    mut = found[0]
    assert mut.file == "src/calc.py"
    assert mut.function == "pick_larger"
    assert mut.line == 1
    assert mut.status == "survived"
    assert mut.operator == "ROR_gt_to_ge"
    # The mutated statement really lives on line 3 of the original; neither the
    # offset nor line+offset-1 equals it. Hence they stay separate fields.
    assert mut.line_offset_in_function == 2
    assert mut.line + mut.line_offset_in_function - 1 != 3


def test_collect_mutants_skips_corrupt_meta_without_aborting(project: Path) -> None:
    """One unreadable file must not sink the whole run."""
    bad = project / "mutants" / "src" / "broken.py"
    bad.write_text("def x_f__mutmut_orig():\n    pass\n", encoding="utf-8")
    (project / "mutants" / "src" / "broken.py.meta").write_text("{not json", encoding="utf-8")

    mutants_dir = project / "mutants"
    found = collect_mutants(mutants_dir, discover_meta_files(mutants_dir))
    assert [m.function for m in found] == ["pick_larger"]


def test_collect_mutants_skips_unparseable_mutated_source(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A valid .meta beside a broken .py must warn and skip, not abort the run."""
    broken = project / "mutants" / "src" / "broken.py"
    broken.write_text("def (((", encoding="utf-8")
    (project / "mutants" / "src" / "broken.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"broken.x_f__mutmut_1": 0}}), encoding="utf-8"
    )

    mutants_dir = project / "mutants"
    found = collect_mutants(mutants_dir, discover_meta_files(mutants_dir))

    assert [m.function for m in found] == ["pick_larger"]
    assert "cannot parse mutated source" in capsys.readouterr().err


def test_collect_mutants_missing_original_still_reports_mutant(project: Path) -> None:
    """No original on disk -> no line, but the mutant is still reported."""
    (project / "src" / "calc.py").unlink()
    mutants_dir = project / "mutants"
    found = collect_mutants(mutants_dir, discover_meta_files(mutants_dir))
    assert len(found) == 1
    assert found[0].line is None


def test_discover_meta_files_pairs_meta_with_source(project: Path) -> None:
    pairs = discover_meta_files(project / "mutants")
    assert len(pairs) == 1
    mutated_py, meta_path, rel = pairs[0]
    assert rel == "src/calc.py"
    assert mutated_py.name == "calc.py"
    assert meta_path.name == "calc.py.meta"


def test_main_writes_report_and_returns_zero(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = project / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        ["parse_results.py", "--mutants-dir", str(project / "mutants"), "--out", str(out)],
    )
    assert main() == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"]["survived"] == 1
    assert report["summary"]["mutation_score"] == 0.0
    assert report["survived_mutants"][0]["line"] == 1
    assert report["survived_mutants"][0]["operator"] == "ROR_gt_to_ge"


def test_main_missing_mutants_dir_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["parse_results.py", "--mutants-dir", str(tmp_path / "nope")])
    assert main() == 2
    assert "mutants directory not found" in capsys.readouterr().err


def test_main_empty_mutants_dir_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "mutants"
    empty.mkdir()
    monkeypatch.setattr("sys.argv", ["parse_results.py", "--mutants-dir", str(empty)])
    assert main() == 2
    assert "no `*.py.meta` files found" in capsys.readouterr().err

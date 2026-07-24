"""Tests for triage_classifier.classify — the survived-mutant triage buckets.

The taxonomy is imported from parse_results, which is the only producer of
operator labels. These tests pin that contract from the consumer side: a label
ranked here but never emitted there (the drift that silently no-op'd every
rule in this module) must fail a test, not pass unnoticed.
"""

from __future__ import annotations

import pytest

from parse_results import HIGH_SIGNAL_OPERATORS, NOISE_OPERATORS, infer_operator
from triage_classifier import classify


def _mutant(**over: object) -> dict:
    base: dict = {
        "operator": "unknown",
        "file": "pkg/service.py",
        "diff_lines": ["-    x = 1", "+    x = 2"],
        "raw_diff": "",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# The taxonomy contract: ranked labels must be labels that actually exist
# ---------------------------------------------------------------------------


def test_every_high_signal_label_is_emittable_by_infer_operator() -> None:
    """A ranked-but-never-emitted label makes its rule dead code.

    Each label must be reachable from some real before/after pair; this is the
    check the old hand-copied set (AOR_plus_to_minus, CRC_boolean_swap, ...)
    would have failed.
    """
    samples = [
        ("if a > b:", "if a >= b:"),
        ("if a >= b:", "if a > b:"),
        ("if a < b:", "if a <= b:"),
        ("if a <= b:", "if a < b:"),
        ("if a == b:", "if a != b:"),
        ("if a != b:", "if a == b:"),
        ("x = a + b", "x = a - b"),
        ("x = a - b", "x = a + b"),
        ("x = a * b", "x = a / b"),
        ("x = a / b", "x = a * b"),
        ("x = a and b", "x = a or b"),
        ("x = a or b", "x = a and b"),
        ("if not a:", "if a:"),
        ("break", "return"),
        ("continue", "break"),
    ]
    emitted = {infer_operator(b, a) for b, a in samples}
    unreachable = {label for label in HIGH_SIGNAL_OPERATORS if label not in emitted}
    assert unreachable == set()


def test_every_noise_label_is_emittable_by_infer_operator() -> None:
    emitted = {
        infer_operator("s = 'admin'", "s = 'XXadminXX'"),
        infer_operator("s = 'Admin'", "s = 'admin'"),
    }
    assert set(NOISE_OPERATORS) <= emitted


def test_classifier_and_parser_share_one_taxonomy_object() -> None:
    """A local copy in this module is what caused the drift; forbid it."""
    import parse_results
    import triage_classifier

    assert triage_classifier.HIGH_SIGNAL_OPERATORS is parse_results.HIGH_SIGNAL_OPERATORS
    assert triage_classifier.NOISE_OPERATORS is parse_results.NOISE_OPERATORS


# ---------------------------------------------------------------------------
# Layer 3 — noise operators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operator", ["string_literal", "string_case"])
def test_noise_operators_are_known_noise(operator: str) -> None:
    result = classify(_mutant(operator=operator))
    assert result["category"] == "known_noise"
    assert result["confidence"] == "medium"


def test_string_case_is_noise_not_a_test_gap() -> None:
    """New mapping: case-only string edits were previously default real_test_gap."""
    assert classify(_mutant(operator="string_case"))["category"] == "known_noise"


# ---------------------------------------------------------------------------
# Layer 5 — high-signal operators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operator",
    ["AOR_add_to_sub", "AOR_sub_to_add", "LCR_and_to_or", "ROR_eq_to_ne", "ROR_ge_to_gt"],
)
def test_high_signal_operators_are_real_gaps(operator: str) -> None:
    result = classify(_mutant(operator=operator))
    assert result["category"] == "real_test_gap"
    assert result["confidence"] == "high"


@pytest.mark.parametrize("operator", ["break_to_return", "continue_to_break"])
def test_control_flow_keywords_are_high_signal_not_equivalent(operator: str) -> None:
    """Reverses the old break<->continue folklore: these change which iterations run."""
    result = classify(_mutant(operator=operator))
    assert result["category"] == "real_test_gap"
    assert result["confidence"] == "high"


# ---------------------------------------------------------------------------
# Layer 4 — boundary equivalence candidates
# ---------------------------------------------------------------------------


def test_boundary_operator_on_integer_is_equivalent_candidate() -> None:
    result = classify(_mutant(operator="ROR_gt_to_ge", diff_lines=["-    if n > 10:", "+    if n >= 10:"]))
    assert result["category"] == "equivalent_candidate"
    assert result["confidence"] == "low"
    assert "REVIEW" in result["action"]


def test_boundary_operator_without_integer_stays_high_signal() -> None:
    result = classify(_mutant(operator="ROR_gt_to_ge", diff_lines=["-    if a > b:", "+    if a >= b:"]))
    assert result["category"] == "real_test_gap"


# ---------------------------------------------------------------------------
# Layers 1 & 2 — file and line noise take precedence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "file_path",
    ["pkg/__init__.py", "pkg/version.py", "pkg/conftest.py", "pkg/settings.py", "app/migrations/0001_x.py"],
)
def test_noise_file_patterns_win_over_operator(file_path: str) -> None:
    """A high-signal operator in a noise file is still noise."""
    result = classify(_mutant(operator="AOR_add_to_sub", file=file_path))
    assert result["category"] == "known_noise"
    assert result["confidence"] == "high"


def test_logging_line_is_noise_even_for_high_signal_operator() -> None:
    result = classify(
        _mutant(
            operator="AOR_add_to_sub", diff_lines=["-    logger.info('n=%s', n + 1)", "+    logger.info('n=%s', n - 1)"]
        )
    )
    assert result["category"] == "known_noise"
    assert "line pattern" in result["reason"]


# ---------------------------------------------------------------------------
# Default bucket
# ---------------------------------------------------------------------------


def test_unknown_operator_defaults_to_low_confidence_gap() -> None:
    """Conservative default: never silently filter something out."""
    result = classify(_mutant(operator="unknown"))
    assert result["category"] == "real_test_gap"
    assert result["confidence"] == "low"


def test_missing_operator_key_is_treated_as_unknown() -> None:
    result = classify({"file": "pkg/service.py", "diff_lines": []})
    assert result["category"] == "real_test_gap"
    assert result["confidence"] == "low"


def test_every_result_carries_category_confidence_reason_action() -> None:
    result = classify(_mutant(operator="AOR_add_to_sub"))
    assert set(result) >= {"category", "confidence", "reason", "action"}
    assert all(isinstance(v, str) and v for v in result.values())


@pytest.mark.parametrize(
    "mutant",
    [
        _mutant(operator="string_literal"),  # Layer 3 noise
        _mutant(operator="string_case"),
        _mutant(operator="AOR_add_to_sub", file="pkg/__init__.py"),  # Layer 1
        _mutant(operator="AOR_add_to_sub", diff_lines=["-    logger.info(1)", "+    logger.info(2)"]),  # Layer 2
        _mutant(operator="ROR_gt_to_ge", diff_lines=["-    if n > 10:", "+    if n >= 10:"]),  # Layer 4
        _mutant(operator="AOR_add_to_sub"),  # Layer 5
        _mutant(operator="unknown"),  # default
    ],
)
def test_no_action_recommends_a_nonexistent_mutmut_knob(mutant: dict) -> None:
    """No bucket may point the user at config keys mutmut 3 silently ignores."""
    action = classify(mutant)["action"]
    for phantom in ("exclude_operators", "mutmut_config", "pre_mutation", "test_command", "--runner"):
        assert phantom not in action

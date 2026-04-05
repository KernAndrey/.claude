from __future__ import annotations

from hooks.pre_commit_review import MAX_DIFF_LINES, check_diff_size


def test_under_limit_returns_none() -> None:
    diff = "\n".join(f"line {i}" for i in range(MAX_DIFF_LINES))
    assert check_diff_size(diff) is None


def test_over_limit_returns_message() -> None:
    diff = "\n".join(f"line {i}" for i in range(MAX_DIFF_LINES + 1))
    result = check_diff_size(diff)
    assert result is not None
    assert "Split" in result
    assert str(MAX_DIFF_LINES) in result

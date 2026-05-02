"""Unit tests for ``validators.manifest``.

Tests inject synthetic git output via the ``runner`` parameter so we never
shell out to a real repo. Each ``make_diff`` builder yields a unified-diff
fragment that the validator's parsers walk just like real ``git diff``.
"""

from __future__ import annotations

import hashlib
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import config

from validators.manifest import (
    ValidationResult,
    collect_file_changes,
    parse_added_lines,
    parse_name_status,
    parse_numstat,
    validate,
)


# ---------------------------------------------------------------------------
# Synthetic-diff helpers
# ---------------------------------------------------------------------------


def _diff_for_modify(path: str, hunks: list[tuple[int, list[str]]]) -> str:
    """Build a unified-diff fragment for a modify. Each hunk is (new_start, [+/-/' ' lines])."""
    parts = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}"]
    for new_start, lines in hunks:
        old_count = sum(1 for ln in lines if ln.startswith((" ", "-")))
        new_count = sum(1 for ln in lines if ln.startswith((" ", "+")))
        parts.append(f"@@ -{new_start},{old_count} +{new_start},{new_count} @@")
        parts.extend(lines)
    return "\n".join(parts) + "\n"


def _diff_for_new_file(path: str, lines: list[str]) -> str:
    parts = [
        f"diff --git a/{path} b/{path}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    parts.extend("+" + ln for ln in lines)
    return "\n".join(parts) + "\n"


def _runner_factory(name_status_text: str, numstat_text: str) -> Callable[[list[str]], str]:
    def runner(args: list[str]) -> str:
        if "--name-status" in args:
            return name_status_text
        if "--numstat" in args:
            return numstat_text
        raise AssertionError(f"unexpected git args: {args}")

    return runner


def _hash(diff: str) -> str:
    return hashlib.sha256(diff.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


def test_parse_added_lines_modify() -> None:
    diff = _diff_for_modify(
        "foo.py",
        [(10, [" context", "+added1", "+added2", "-removed", " context2"])],
    )
    added = parse_added_lines(diff)
    # new-file line numbering: 10=context, 11=added1, 12=added2, 13=context2 (after the removed)
    assert added == {"foo.py": {11, 12}}


def test_parse_added_lines_new_file() -> None:
    diff = _diff_for_new_file("bar.py", ["one", "two", "three"])
    assert parse_added_lines(diff) == {"bar.py": {1, 2, 3}}


def test_parse_added_lines_skips_plus_plus_plus_header() -> None:
    diff = _diff_for_new_file("baz.py", ["x", "y"])
    assert "+++" in diff  # sanity
    assert parse_added_lines(diff) == {"baz.py": {1, 2}}


def test_parse_name_status_modify_and_rename() -> None:
    text = "M\tfoo.py\nA\tnew.py\nR100\told.py\tnew_name.py\nD\tgone.py\n"
    assert parse_name_status(text) == [
        ("M", "foo.py", None),
        ("A", "new.py", None),
        ("R", "new_name.py", "old.py"),
        ("D", "gone.py", None),
    ]


def test_parse_numstat_text_and_binary() -> None:
    text = "12\t3\tfoo.py\n-\t-\timg.png\n"
    assert parse_numstat(text) == {
        "foo.py": (12, 3, False),
        "img.png": (0, 0, True),
    }


def test_parse_numstat_rename_arrow() -> None:
    text = "5\t2\told => new\n"
    assert parse_numstat(text) == {"new": (5, 2, False)}


# ---------------------------------------------------------------------------
# Top-level validate() — happy path
# ---------------------------------------------------------------------------


def test_validate_happy_path(tmp_path: Path) -> None:
    diff = _diff_for_new_file("models/foo.py", ["a", "b", "c"])
    runner = _runner_factory("A\tmodels/foo.py\n", "3\t0\tmodels/foo.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: models
            rationale: new model
            files:
              - path: models/foo.py
                line_ranges: all
            review_lenses: [bugs, architecture]
    """)

    result = validate(manifest, diff, tmp_path, runner=runner)
    assert result.ok, result.to_text()
    assert result.errors == []


def test_validate_explicit_ranges(tmp_path: Path) -> None:
    diff = _diff_for_modify(
        "src/x.py",
        [(10, ["+a", "+b", "+c"])],
    )
    runner = _runner_factory("M\tsrc/x.py\n", "3\t0\tsrc/x.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            rationale: r
            files:
              - path: src/x.py
                line_ranges: [[10, 12]]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert result.ok, result.to_text()


# ---------------------------------------------------------------------------
# Top-level validate() — failure modes
# ---------------------------------------------------------------------------


def test_validate_stale_hash(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent("""
        version: 1
        diff_hash: 0000000000000000000000000000000000000000000000000000000000000000
        chunks:
          - id: c1
            files: [{path: a.py, line_ranges: all}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    codes = {e.code for e in result.errors}
    assert "stale_hash" in codes


def test_validate_malformed_yaml(tmp_path: Path) -> None:
    result = validate("not: valid: yaml: [", "", tmp_path, runner=lambda a: "")
    assert not result.ok
    assert any(e.code == "malformed_yaml" for e in result.errors)


def test_validate_empty_manifest(tmp_path: Path) -> None:
    result = validate("", "", tmp_path, runner=lambda a: "")
    assert any(e.code == "empty_manifest" for e in result.errors)


def test_validate_too_many_chunks(tmp_path: Path) -> None:
    # Generate MAX_CHUNKS + 1 trivial new files — exceeds the limit
    n = config.MAX_CHUNKS + 1
    diffs = [_diff_for_new_file(f"f{i}.py", ["x"]) for i in range(n)]
    diff = "".join(diffs)
    name_status = "".join(f"A\tf{i}.py\n" for i in range(n))
    numstat = "".join(f"1\t0\tf{i}.py\n" for i in range(n))
    runner = _runner_factory(name_status, numstat)

    chunk_lines = "\n".join(
        f"  - id: c{i}\n    files: [{{path: f{i}.py, line_ranges: all}}]\n    review_lenses: [bugs]" for i in range(n)
    )
    manifest = f"version: 1\ndiff_hash: {_hash(diff)}\nchunks:\n{chunk_lines}\n"

    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "too_many_chunks" for e in result.errors)


def test_validate_oversized_chunk(tmp_path: Path) -> None:
    # 301 added prod lines in one chunk
    lines = [f"line_{i}" for i in range(301)]
    diff = _diff_for_new_file("big.py", lines)
    runner = _runner_factory("A\tbig.py\n", f"{len(lines)}\t0\tbig.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: big
            files: [{{path: big.py, line_ranges: all}}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "oversized_chunk" for e in result.errors)


def test_validate_uncovered_file(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"]) + _diff_for_new_file("b.py", ["y"])
    runner = _runner_factory("A\ta.py\nA\tb.py\n", "1\t0\ta.py\n1\t0\tb.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    codes = {e.code for e in result.errors}
    assert "uncovered_file" in codes


def test_validate_unknown_file(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - {{path: a.py, line_ranges: all}}
              - {{path: ghost.py, line_ranges: all}}
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "unknown_file" and e.file_path == "ghost.py" for e in result.errors)


def test_validate_range_overlap_when_two_chunks_claim_same_line(tmp_path: Path) -> None:
    """Two chunks claiming the same file with overlapping line_ranges → range_overlap.
    Splitting a file with disjoint ranges is fine; two chunks owning the same
    added line is not."""
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
            review_lenses: [bugs]
          - id: c2
            files: [{{path: a.py, line_ranges: all}}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "range_overlap" for e in result.errors)
    # File-level multi-claim is no longer an error on its own.
    assert not any(e.code == "file_overlap" for e in result.errors)


def test_validate_split_file_by_disjoint_ranges_ok(tmp_path: Path) -> None:
    """A big file split across two chunks via disjoint line_ranges is the
    intended way to keep chunks under MAX_PROD_LINES."""
    lines = [f"line_{i}" for i in range(20)]
    diff = _diff_for_new_file("big.py", lines)
    runner = _runner_factory("A\tbig.py\n", f"{len(lines)}\t0\tbig.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: first_half
            files: [{{path: big.py, line_ranges: [[1, 10]]}}]
            review_lenses: [bugs]
          - id: second_half
            files: [{{path: big.py, line_ranges: [[11, 20]]}}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert result.ok, result.to_text()


def test_validate_aggregate_line_gap(tmp_path: Path) -> None:
    """Two chunks both claiming the same file but together missing some
    added lines → line_gap on the file (aggregate)."""
    diff = _diff_for_new_file("big.py", [f"line_{i}" for i in range(20)])
    runner = _runner_factory("A\tbig.py\n", "20\t0\tbig.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: first
            files: [{{path: big.py, line_ranges: [[1, 5]]}}]
            review_lenses: [bugs]
          - id: second
            files: [{{path: big.py, line_ranges: [[11, 15]]}}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    # Lines 6-10 and 16-20 left uncovered.
    assert any(e.code == "line_gap" for e in result.errors)


def test_validate_invalid_chunk_id(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    # Uppercase + dash both fail the [a-z][a-z0-9_]* contract.
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: Models-Layer
            files: [{{path: a.py, line_ranges: all}}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "invalid_chunk_id" for e in result.errors)


def test_validate_review_lenses_field_is_ignored(tmp_path: Path) -> None:
    """review_lenses was removed from the per-chunk schema (the per-chunk
    reviewer always covers all three lenses). The validator must accept
    manifests with or without the field — old YAML still validates."""
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    # Even an unknown lens value is now silently accepted: the field
    # is informational, not enforced.
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
            review_lenses: [bugs, fairy_dust]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert result.ok, result.to_text()


def test_validate_missing_related_file(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
            review_lenses: [bugs]
            related_files: [does_not_exist.py]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "missing_related_file" for e in result.errors)


def test_validate_related_file_present(tmp_path: Path) -> None:
    (tmp_path / "exists.py").write_text("# hi\n")
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        default_related_files: [exists.py]
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert result.ok, result.to_text()


def test_validate_line_gap(tmp_path: Path) -> None:
    diff = _diff_for_modify("src/x.py", [(10, ["+a", "+b", "+c"])])
    runner = _runner_factory("M\tsrc/x.py\n", "3\t0\tsrc/x.py\n")
    # Range claims only line 10, leaving 11 and 12 uncovered
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - {{path: src/x.py, line_ranges: [[10, 10]]}}
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "line_gap" for e in result.errors)


def test_validate_invalid_range(tmp_path: Path) -> None:
    diff = _diff_for_modify("src/x.py", [(10, ["+a"])])
    runner = _runner_factory("M\tsrc/x.py\n", "1\t0\tsrc/x.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: src/x.py, line_ranges: [[20, 5]]}}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "invalid_range" for e in result.errors)


def test_validate_rename_path_match(tmp_path: Path) -> None:
    # Diff text reflects new path; status row carries the rename.
    diff = _diff_for_modify("new/x.py", [(1, ["+touch"])])
    runner = _runner_factory("R100\told/x.py\tnew/x.py\n", "1\t0\tnew/x.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: new/x.py
                original_path: old/x.py
                line_ranges: [[1, 1]]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert result.ok, result.to_text()


def test_validate_rename_path_mismatch(tmp_path: Path) -> None:
    diff = _diff_for_modify("new/x.py", [(1, ["+touch"])])
    runner = _runner_factory("R100\told/x.py\tnew/x.py\n", "1\t0\tnew/x.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: new/x.py
                original_path: wrong/path.py
                line_ranges: [[1, 1]]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "rename_path_mismatch" for e in result.errors)


def test_validate_binary_file_path_listed(tmp_path: Path) -> None:
    # Binary diff has no hunks; we just need the +++/--- headers
    diff = "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
    runner = _runner_factory("A\timg.png\n", "-\t-\timg.png\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: img.png, line_ranges: []}}]
            review_lenses: [architecture]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert result.ok, result.to_text()


def test_validate_deleted_file(tmp_path: Path) -> None:
    diff = (
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-line one\n"
        "-line two\n"
    )
    runner = _runner_factory("D\told.py\n", "0\t2\told.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: old.py, line_ranges: []}}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert result.ok, result.to_text()


def test_validate_mode_change_only_file(tmp_path: Path) -> None:
    """T-status (mode-only change) has no content lines; validator skips it."""
    diff = "diff --git a/script.py b/script.py\nold mode 100644\nnew mode 100755\n"
    runner = _runner_factory("T\tscript.py\n", "0\t0\tscript.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: script.py, line_ranges: []}}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert result.ok, result.to_text()


def test_parse_added_lines_plus_header_inside_hunk_is_added_line() -> None:
    """C14 regression: a + line whose content is '++ b/foo.py' must not be
    misclassified as a file header when we are already inside a hunk."""
    diff = _diff_for_modify(
        "foo.py",
        [(1, ["+++ b/foo.py", " context"])],
    )
    added = parse_added_lines(diff)
    assert added == {"foo.py": {1}}


def test_validate_git_runner_failure_returns_error_not_raises(tmp_path: Path) -> None:
    """C18 regression: a failing git runner must produce a validation error,
    not let the exception escape validate()."""
    diff = _diff_for_new_file("a.py", ["x"])

    def failing_runner(args: list[str]) -> str:
        raise subprocess.CalledProcessError(128, ["git", "diff"], stderr="fatal: not a git repository")

    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
    """)
    result = validate(manifest, diff, tmp_path, runner=failing_runner)
    assert not result.ok
    assert any(e.code == "git_runner_failed" for e in result.errors)


def test_validate_git_runner_oserror_returns_error_not_raises(tmp_path: Path) -> None:
    """OSError from the git runner must also be caught and turned into a validation error."""
    diff = _diff_for_new_file("a.py", ["x"])

    def failing_runner(args: list[str]) -> str:
        raise OSError("permission denied")

    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
    """)
    result = validate(manifest, diff, tmp_path, runner=failing_runner)
    assert not result.ok
    assert any(e.code == "git_runner_failed" for e in result.errors)


def test_validate_test_file_not_counted_against_chunk_size(tmp_path: Path) -> None:
    # 400 lines of test code — would blow MAX_PROD_LINES=300 if counted.
    lines = [f"line_{i}" for i in range(400)]
    diff = _diff_for_new_file("tests/test_huge.py", lines)
    runner = _runner_factory("A\ttests/test_huge.py\n", f"{len(lines)}\t0\ttests/test_huge.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: tests
            files: [{{path: tests/test_huge.py, line_ranges: all}}]
            review_lenses: [tests]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert result.ok, result.to_text()


def test_to_text_lists_all_errors() -> None:
    res = ValidationResult()
    res.add("alpha", "first problem", chunk_id="c1")
    res.add("beta", "second problem", file_path="x.py")
    text = res.to_text()
    assert "first problem" in text
    assert "second problem" in text
    assert "alpha" in text and "beta" in text


def test_collect_file_changes_pairs_status_with_numstat() -> None:
    diff = _diff_for_new_file("a.py", ["x", "y"])
    runner = _runner_factory("A\ta.py\n", "2\t0\ta.py\n")
    changes = collect_file_changes(diff, runner=runner)
    assert len(changes) == 1
    fc = changes[0]
    assert fc.path == "a.py"
    assert fc.status == "A"
    assert fc.added == 2
    assert fc.is_binary is False
    assert fc.added_line_numbers == frozenset({1, 2})


# ---------------------------------------------------------------------------
# C2 — brace-form rename numstat
# ---------------------------------------------------------------------------


def test_parse_numstat_rename_brace_form() -> None:
    text = "5\t2\tsrc/{old.py => new.py}\n"
    assert parse_numstat(text) == {"src/new.py": (5, 2, False)}


def test_parse_numstat_rename_brace_form_arrow() -> None:
    text = "3\t1\tpath/{a -> b}_suffix.txt\n"
    assert parse_numstat(text) == {"path/b_suffix.txt": (3, 1, False)}


def test_parse_numstat_rename_simple_arrow_still_works() -> None:
    text = "4\t0\told.py -> new.py\n"
    assert parse_numstat(text) == {"new.py": (4, 0, False)}


# ---------------------------------------------------------------------------
# C4 — stale-hash error message points to real scaffolder
# ---------------------------------------------------------------------------


def test_validate_stale_hash_message_has_scaffolder_command(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent("""
        version: 1
        diff_hash: 0000000000000000000000000000000000000000000000000000000000000000
        chunks:
          - id: c1
            files: [{path: a.py, line_ranges: all}]
            review_lenses: [bugs]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    stale_errors = [e for e in result.errors if e.code == "stale_hash"]
    assert len(stale_errors) == 1
    assert "python3 ~/.claude/review/scripts/scaffold_manifest.py" in stale_errors[0].message
    assert "claude_review.generate_manifest" not in stale_errors[0].message


# ---------------------------------------------------------------------------
# C18-C30 — missing validation-branch coverage
# ---------------------------------------------------------------------------


def test_validate_top_level_scalar(tmp_path: Path) -> None:
    result = validate("just a string", "", tmp_path, runner=lambda a: "")
    assert any(e.code == "malformed_yaml" for e in result.errors)


def test_validate_top_level_list(tmp_path: Path) -> None:
    result = validate("- item1\n- item2\n", "", tmp_path, runner=lambda a: "")
    assert any(e.code == "malformed_yaml" for e in result.errors)


def test_validate_missing_chunks(tmp_path: Path) -> None:
    result = validate("version: 1\n", "", tmp_path, runner=lambda a: "")
    assert any(e.code == "no_chunks" for e in result.errors)


def test_validate_empty_chunks(tmp_path: Path) -> None:
    result = validate("version: 1\nchunks: []\n", "", tmp_path, runner=lambda a: "")
    assert any(e.code == "no_chunks" for e in result.errors)


def test_validate_malformed_chunk(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - not a mapping
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "malformed_chunk" for e in result.errors)


def test_validate_missing_chunk_id(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - rationale: missing id
            files: [{{path: a.py, line_ranges: all}}]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "missing_chunk_id" for e in result.errors)


def test_validate_duplicate_chunk_id(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "duplicate_chunk_id" for e in result.errors)


def test_validate_missing_files_key(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            rationale: no files
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "missing_files" for e in result.errors)


def test_validate_empty_files(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            rationale: empty files
            files: []
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "missing_files" for e in result.errors)


def test_validate_malformed_file_entry(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - not a mapping
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "malformed_file_entry" for e in result.errors)


def test_validate_missing_file_path(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - line_ranges: all
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "missing_file_path" for e in result.errors)


def test_validate_malformed_line_ranges(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x", "y", "z"])
    runner = _runner_factory("A\ta.py\n", "3\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: "not a list"
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "malformed_line_ranges" for e in result.errors)


def test_validate_malformed_range(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x", "y", "z"])
    runner = _runner_factory("A\ta.py\n", "3\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: [[1, 2, 3]]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "malformed_range" for e in result.errors)


def test_validate_range_outside_diff(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x", "y", "z"])
    runner = _runner_factory("A\ta.py\n", "3\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: [[100, 105]]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "range_outside_diff" for e in result.errors)


def test_validate_malformed_related_files_not_list(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
            related_files: "not a list"
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "malformed_related_files" for e in result.errors)


def test_validate_malformed_default_related_files_not_list(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        default_related_files: "not a list"
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "malformed_related_files" for e in result.errors)


def test_validate_malformed_related_files_entry_not_string(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")
    manifest = textwrap.dedent(f"""
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files: [{{path: a.py, line_ranges: all}}]
            related_files: [123]
    """)
    result = validate(manifest, diff, tmp_path, runner=runner)
    assert any(e.code == "malformed_related_files" for e in result.errors)

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hooks.pre_commit_review import (
    FANOUT_THRESHOLD,
    LENS_NAMES,
    MAX_DIFF_LINES,
    _aggregate_lens_outputs,
    _parse_opencode_json,
    _render_fanout_output,
    assign_finding_ids,
    check_diff_size,
    count_added_lines,
    count_criticals,
    is_well_formed,
    parse_arbiter_verdict,
    parse_verdict,
    run_opencode,
)


# ---------------------------------------------------------------------------
# count_criticals
# ---------------------------------------------------------------------------


def test_count_criticals_zero_on_empty() -> None:
    assert count_criticals("") == 0
    assert count_criticals("   \n  ") == 0


def test_count_criticals_bare_line() -> None:
    review = "[CRITICAL] foo.py:10 — trigger — consequence"
    assert count_criticals(review) == 1


def test_count_criticals_bullet_prefixes() -> None:
    review = (
        "- [CRITICAL] a.py:1 — foo\n"
        "* [CRITICAL] b.py:2 — bar\n"
        "  [CRITICAL] c.py:3 — baz"
    )
    assert count_criticals(review) == 3


def test_count_criticals_ignores_mid_line_mention() -> None:
    review = "Some prose mentioning [CRITICAL] inline does not count."
    assert count_criticals(review) == 0


def test_count_criticals_ignores_tag_inside_diff_quote() -> None:
    review = (
        "### Section 1\n"
        "- foo.py — 1 hunk — REVIEWED\n"
        "\n"
        "### Section 2\n"
        "#### Sweep 1 — Security\n"
        "No findings in this area.\n"
        "\n"
        "Note: the diff contains `# [CRITICAL] path not taken` as a comment.\n"
        "Summary: 0 CRITICAL, 0 WARNING across 1 files."
    )
    assert count_criticals(review) == 0


# ---------------------------------------------------------------------------
# parse_verdict — decision is purely derived from critical count
# ---------------------------------------------------------------------------


def _full_review(body: str, summary: str = "Summary: 0 CRITICAL, 0 WARNING across 1 files.") -> str:
    """Build a minimally-well-formed review body for tests.

    Prepends enough section scaffolding to clear the ``_MIN_WELL_FORMED_LINES``
    floor without making every test read like a real review.
    """
    return (
        "### Section 1 — File audit\n"
        "- foo.py — 1 hunk — REVIEWED\n"
        "### Section 2 — Coverage matrix and findings\n"
        f"{body}\n"
        f"{summary}"
    )


def test_is_well_formed_requires_summary_as_last_line() -> None:
    assert is_well_formed("") is False
    assert is_well_formed("some prose without terminator") is False
    # Summary-only response is rejected — no Sections 1 or 2.
    assert is_well_formed("Summary: 0 CRITICAL, 0 WARNING across 1 files.") is False
    # A real review with Summary last is accepted.
    assert is_well_formed(_full_review("No findings anywhere.")) is True


def test_is_well_formed_rejects_short_non_empty_review() -> None:
    """Rejects reviews with too few lines to have plausibly contained
    Sections 1 and 2 before the summary."""
    short = "a\nb\nSummary: 0 CRITICAL, 0 WARNING across 1 files."
    assert is_well_formed(short) is False


def test_is_well_formed_rejects_trailing_content_after_summary() -> None:
    """Contract: Summary must be the final non-empty line. Anything after
    it means the reviewer wrote past its stop signal."""
    with_trailing_block = _full_review("body line 1\nbody line 2") + "\nBLOCK"
    with_trailing_prose = _full_review("body line 1\nbody line 2") + "\n\nfoo"
    assert is_well_formed(with_trailing_block) is False
    assert is_well_formed(with_trailing_prose) is False
    assert is_well_formed(_full_review("body line 1\nbody line 2") + "\n   \n") is True


def test_is_well_formed_rejects_summary_mention_mid_line() -> None:
    assert is_well_formed("See Summary: in the docs but no terminator here") is False


def test_parse_verdict_empty_defaults_to_block() -> None:
    assert parse_verdict("") == "BLOCK"
    assert parse_verdict("   \n  \n  ") == "BLOCK"


def test_parse_verdict_malformed_non_empty_blocks() -> None:
    """Non-empty review without a Summary terminator cannot be trusted —
    treat as fail-closed BLOCK regardless of critical count."""
    assert parse_verdict("partial inventory, opencode crashed mid-write") == "BLOCK"
    assert parse_verdict("- [CRITICAL] foo.py:1 — bug (no summary)") == "BLOCK"


def test_parse_verdict_no_criticals_is_ok() -> None:
    review = _full_review("#### Sweep 1 — Security\nNo findings in this area.")
    assert parse_verdict(review) == "OK"


def test_parse_verdict_warning_only_is_ok() -> None:
    review = _full_review(
        "#### Sweep 1 — Security\n- [WARNING] foo.py:1 — maybe racy",
        summary="Summary: 0 CRITICAL, 1 WARNING across 1 files.",
    )
    assert parse_verdict(review) == "OK"


def test_parse_verdict_any_critical_blocks() -> None:
    review = _full_review(
        "#### Sweep 1 — Security\n- [CRITICAL] foo.py:1 — eval on user input — RCE",
        summary="Summary: 1 CRITICAL, 0 WARNING across 1 files.",
    )
    assert parse_verdict(review) == "BLOCK"


def test_parse_verdict_critical_tag_is_case_insensitive() -> None:
    """[CRITICAL] tag is matched case-insensitively so a model that writes
    `[Critical]` or `[critical]` still blocks the commit."""
    for variant in ("[Critical]", "[critical]", "[CRITICAL]"):
        review = _full_review(
            f"#### Sweep 1 — Security\n- {variant} foo.py:1 — bad",
            summary="Summary: 1 CRITICAL, 0 WARNING across 1 files.",
        )
        assert parse_verdict(review) == "BLOCK", variant


def test_parse_verdict_trailing_ok_or_block_is_malformed() -> None:
    """If the reviewer still writes OK/BLOCK after Summary (out of habit),
    the contract ("Summary is final line") is violated → fail-closed BLOCK
    rather than trust the count."""
    review = _full_review("#### Sweep 1 — Security\nNo findings in this area.") + "\nBLOCK"
    assert parse_verdict(review) == "BLOCK"  # malformed, not content-driven

    # [CRITICAL] alone without Summary is also malformed.
    review_without_summary = "- [CRITICAL] foo.py:1 — actually broken\nOK"
    assert parse_verdict(review_without_summary) == "BLOCK"


# ---------------------------------------------------------------------------
# check_diff_size
# ---------------------------------------------------------------------------


def test_under_limit_returns_none() -> None:
    diff = "\n".join(f"line {i}" for i in range(MAX_DIFF_LINES))
    assert check_diff_size(diff) is None


def test_over_limit_returns_message() -> None:
    diff = "\n".join(f"line {i}" for i in range(MAX_DIFF_LINES + 1))
    result = check_diff_size(diff)
    assert result is not None
    assert "Split" in result
    assert str(MAX_DIFF_LINES) in result


# ---------------------------------------------------------------------------
# opencode plumbing
# ---------------------------------------------------------------------------


def test_parse_opencode_json_extracts_last_text() -> None:
    raw = (
        '{"type":"step_start","timestamp":1}\n'
        '{"type":"text","timestamp":2,"part":{"type":"text","text":"partial"}}\n'
        '{"type":"text","timestamp":3,"part":{"type":"text","text":"[WARNING] foo\\n\\nSummary: 0 CRITICAL"}}\n'
        '{"type":"step_finish","timestamp":4}\n'
    )
    assert _parse_opencode_json(raw) == "partial\n[WARNING] foo\n\nSummary: 0 CRITICAL"


def test_parse_opencode_json_empty_output() -> None:
    assert _parse_opencode_json("") == ""
    assert _parse_opencode_json('{"type":"step_start"}\n') == ""


def test_run_opencode_builds_correct_command() -> None:
    json_output = '{"type":"text","part":{"type":"text","text":"done"}}\n'
    mock_result = MagicMock()
    mock_result.stdout = json_output
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("hooks.pre_commit_review.subprocess.run", return_value=mock_result) as mock_run:
        stdout, stderr, rc = run_opencode("sys", "user")

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "opencode"
    assert "run" in cmd
    assert "--pure" in cmd
    assert "--format" in cmd
    assert "json" in cmd
    assert "--model" in cmd
    assert "github-copilot/gpt-5.4" in cmd
    assert cmd[-1] == "sys\n\nuser"
    assert stdout == "done"
    assert rc == 0


def test_run_opencode_empty_system_prompt() -> None:
    json_output = '{"type":"text","part":{"type":"text","text":"done"}}\n'
    mock_result = MagicMock()
    mock_result.stdout = json_output
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("hooks.pre_commit_review.subprocess.run", return_value=mock_result) as mock_run:
        run_opencode("", "user only")

    cmd = mock_run.call_args[0][0]
    assert cmd[-1] == "user only"


# ---------------------------------------------------------------------------
# count_added_lines / FANOUT_THRESHOLD
# ---------------------------------------------------------------------------


def test_count_added_lines_skips_file_header() -> None:
    diff = (
        "diff --git a/foo b/foo\n"
        "+++ b/foo\n"  # file header, should be skipped
        "+added-1\n"
        "+added-2\n"
        "-removed\n"
        " context\n"
    )
    assert count_added_lines(diff) == 2


def test_count_added_lines_zero_on_no_added() -> None:
    diff = "diff --git a/foo b/foo\n-removed\n context\n"
    assert count_added_lines(diff) == 0


def test_fanout_threshold_sane_default() -> None:
    """Sanity check — threshold is the documented 150 added-line boundary."""
    assert FANOUT_THRESHOLD == 150
    assert set(LENS_NAMES) == {
        "security", "tests", "duplication", "performance",
        "rootcause", "complexity", "types",
    }


# ---------------------------------------------------------------------------
# assign_finding_ids
# ---------------------------------------------------------------------------


def test_assign_finding_ids_injects_stable_ids() -> None:
    review = (
        "- [CRITICAL] a.py:1 — foo\n"
        "- [CRITICAL] b.py:2 — bar\n"
        "[CRITICAL] c.py:3 — baz"
    )
    tagged, findings = assign_finding_ids(review)
    assert [f["id"] for f in findings] == ["F1", "F2", "F3"]
    assert "[F1]" in tagged and "[F2]" in tagged and "[F3]" in tagged
    # Original severity tag is preserved after id injection.
    assert tagged.count("[CRITICAL]") == 3


def test_assign_finding_ids_ignores_warnings() -> None:
    review = "- [WARNING] a.py:1 — minor\n- [CRITICAL] b.py:2 — real"
    tagged, findings = assign_finding_ids(review)
    assert len(findings) == 1
    assert findings[0]["id"] == "F1"
    assert "[F1] [CRITICAL]" in tagged
    # Warning line unchanged — arbiter does not evaluate warnings.
    assert "[F1] [WARNING]" not in tagged


def test_assign_finding_ids_empty_review() -> None:
    tagged, findings = assign_finding_ids("no findings here")
    assert findings == []
    assert tagged == "no findings here"


# ---------------------------------------------------------------------------
# parse_arbiter_verdict
# ---------------------------------------------------------------------------


def test_parse_arbiter_verdict_basic_split() -> None:
    raw = (
        "[UPHELD] F1 — cites a real line with real consequence.\n"
        "[OVERTURN] F2 — purely theoretical trigger.\n"
        "[UPHELD] F3 — SQL injection in newly-added query.\n"
        "Summary: 2 UPHELD, 1 OVERTURN."
    )
    upheld = parse_arbiter_verdict(raw, ["F1", "F2", "F3"])
    assert upheld == {"F1", "F3"}


def test_parse_arbiter_verdict_fail_open_on_missing_summary() -> None:
    """Arbiter output without the Summary terminator is malformed.
    Every finding must be upheld (fail-open) to avoid silently dropping
    valid findings."""
    raw = "[OVERTURN] F1 — trash\n[OVERTURN] F2 — noise"  # no Summary
    upheld = parse_arbiter_verdict(raw, ["F1", "F2"])
    assert upheld == {"F1", "F2"}


def test_parse_arbiter_verdict_missing_id_is_upheld() -> None:
    """If the arbiter forgot a finding's verdict line, fail-open for that id."""
    raw = "[OVERTURN] F1 — noise.\nSummary: 0 UPHELD, 1 OVERTURN."
    upheld = parse_arbiter_verdict(raw, ["F1", "F2"])
    assert upheld == {"F2"}  # F1 overturned explicitly, F2 missing → upheld


def test_parse_arbiter_verdict_empty_raw_is_all_upheld() -> None:
    assert parse_arbiter_verdict("", ["F1"]) == {"F1"}
    assert parse_arbiter_verdict("", []) == set()


def test_parse_arbiter_verdict_case_insensitive_tags() -> None:
    raw = (
        "[upheld] F1 — real.\n"
        "[Overturn] F2 — theoretical.\n"
        "Summary: 1 UPHELD, 1 OVERTURN."
    )
    assert parse_arbiter_verdict(raw, ["F1", "F2"]) == {"F1"}


# ---------------------------------------------------------------------------
# _aggregate_lens_outputs
# ---------------------------------------------------------------------------


def test_aggregate_lens_outputs_appends_global_summary() -> None:
    per_lens = [
        {
            "name": "security",
            "status": "ok",
            "review": "No findings in this lens.",
            "reviewer": "opencode",
            "error": "",
        },
        {
            "name": "tests",
            "status": "ok",
            "review": "- [CRITICAL] foo.py:1 — missing test\n- [WARNING] bar.py:5 — flaky",
            "reviewer": "opencode",
            "error": "",
        },
    ]
    aggregated = _aggregate_lens_outputs(per_lens)
    assert "## Lens: security" in aggregated
    assert "## Lens: tests" in aggregated
    assert "Summary: 1 CRITICAL, 1 WARNING across 2 lenses." in aggregated


def test_aggregate_lens_outputs_marks_unavailable_lenses() -> None:
    per_lens = [
        {
            "name": "security",
            "status": "timeout",
            "review": "",
            "reviewer": "opencode",
            "error": "opencode timeout",
        },
        {
            "name": "tests",
            "status": "ok",
            "review": "No findings in this lens.",
            "reviewer": "opencode",
            "error": "",
        },
    ]
    aggregated = _aggregate_lens_outputs(per_lens)
    assert "Lens unavailable: opencode timeout" in aggregated
    # Unavailable lens contributes zero findings to the total.
    assert "Summary: 0 CRITICAL, 0 WARNING across 2 lenses." in aggregated


# ---------------------------------------------------------------------------
# _render_fanout_output
# ---------------------------------------------------------------------------


def test_render_fanout_output_splits_upheld_and_overturned() -> None:
    per_lens = [
        {"name": "security", "status": "ok", "review": "none", "reviewer": "opencode", "error": ""}
    ]
    findings = [
        {"id": "F1", "line": "- [F1] [CRITICAL] a.py:1 — real"},
        {"id": "F2", "line": "- [F2] [CRITICAL] b.py:2 — theoretical"},
    ]
    arbiter = {
        "status": "ok",
        "upheld_ids": {"F1"},
        "raw": (
            "[UPHELD] F1 — real trigger and consequence.\n"
            "[OVERTURN] F2 — purely hypothetical.\n"
            "Summary: 1 UPHELD, 1 OVERTURN."
        ),
        "error": "",
    }
    rendered = _render_fanout_output(per_lens, findings, {"F1"}, arbiter)
    assert "Upheld findings (blocking)" in rendered
    assert "[F1] [CRITICAL] a.py:1" in rendered
    assert "Overturned findings (advisory" in rendered
    assert "[F2] [CRITICAL] b.py:2" in rendered
    # The arbiter rationale for F2 appears under the overturned block.
    assert "purely hypothetical" in rendered
    assert "Summary: 1 UPHELD, 1 OVERTURN" in rendered


def test_render_fanout_output_no_findings_shows_none() -> None:
    rendered = _render_fanout_output(
        [{"name": "security", "status": "ok", "review": "none",
          "reviewer": "opencode", "error": ""}],
        findings=[],
        upheld_ids=set(),
        arbiter={"status": "skipped", "upheld_ids": set(), "raw": "", "error": ""},
    )
    assert "_(none)_" in rendered
    assert "Summary: 0 UPHELD, 0 OVERTURN" in rendered

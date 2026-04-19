from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from hook import (
    FANOUT_THRESHOLD,
    LENS_NAMES,
    MAX_DIFF_LINES,
    _aggregate_lens_outputs,
    _parse_opencode_json,
    _render_fanout_output,
    applicable_lenses,
    assign_finding_ids,
    build_user_prompt,
    check_diff_size,
    count_added_lines,
    count_criticals,
    is_well_formed,
    parse_arbiter_verdict,
    parse_verdict,
    run_opencode,
    run_review,
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
    review = "- [CRITICAL] a.py:1 — foo\n* [CRITICAL] b.py:2 — bar\n  [CRITICAL] c.py:3 — baz"
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
        "#### Bugs\n"
        "No findings in this lens.\n"
        "\n"
        "Note: the diff contains `# [CRITICAL] path not taken` as a comment.\n"
        "Summary: 0 CRITICAL, 0 WARNING across 1 files."
    )
    assert count_criticals(review) == 0


# ---------------------------------------------------------------------------
# parse_verdict — decision is purely derived from critical count
# ---------------------------------------------------------------------------


def _full_review(body: str, summary: str = "Summary: 0 CRITICAL, 0 WARNING across 1 files.") -> str:
    """Build a minimally-well-formed review body for tests."""
    return f"### Section 1 — File audit\n- foo.py — 1 hunk — REVIEWED\n### Section 2 — Findings\n{body}\n{summary}"


def test_is_well_formed_requires_summary_as_last_line() -> None:
    assert is_well_formed("") is False
    assert is_well_formed("some prose without terminator") is False
    assert is_well_formed("Summary: 0 CRITICAL, 0 WARNING across 1 files.") is False
    assert is_well_formed(_full_review("No findings anywhere.")) is True


def test_is_well_formed_rejects_short_non_empty_review() -> None:
    short = "a\nb\nSummary: 0 CRITICAL, 0 WARNING across 1 files."
    assert is_well_formed(short) is False


def test_is_well_formed_rejects_trailing_content_after_summary() -> None:
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
    assert parse_verdict("partial inventory, opencode crashed mid-write") == "BLOCK"
    assert parse_verdict("- [CRITICAL] foo.py:1 — bug (no summary)") == "BLOCK"


def test_parse_verdict_no_criticals_is_ok() -> None:
    review = _full_review("#### Bugs\nNo findings in this lens.")
    assert parse_verdict(review) == "OK"


def test_parse_verdict_warning_only_is_ok() -> None:
    review = _full_review(
        "#### Bugs\n- [WARNING] foo.py:1 — maybe racy",
        summary="Summary: 0 CRITICAL, 1 WARNING across 1 files.",
    )
    assert parse_verdict(review) == "OK"


def test_parse_verdict_any_critical_blocks() -> None:
    review = _full_review(
        "#### Bugs\n- [CRITICAL] foo.py:1 — SQL injection — data leak",
        summary="Summary: 1 CRITICAL, 0 WARNING across 1 files.",
    )
    assert parse_verdict(review) == "BLOCK"


def test_parse_verdict_critical_tag_is_case_insensitive() -> None:
    for variant in ("[Critical]", "[critical]", "[CRITICAL]"):
        review = _full_review(
            f"#### Bugs\n- {variant} foo.py:1 — bad",
            summary="Summary: 1 CRITICAL, 0 WARNING across 1 files.",
        )
        assert parse_verdict(review) == "BLOCK", variant


def test_parse_verdict_trailing_ok_or_block_is_malformed() -> None:
    review = _full_review("#### Bugs\nNo findings in this lens.") + "\nBLOCK"
    assert parse_verdict(review) == "BLOCK"

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

    with patch("hook.subprocess.run", return_value=mock_result) as mock_run:
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

    with patch("hook.subprocess.run", return_value=mock_result) as mock_run:
        run_opencode("", "user only")

    cmd = mock_run.call_args[0][0]
    assert cmd[-1] == "user only"


# ---------------------------------------------------------------------------
# count_added_lines / FANOUT_THRESHOLD / LENS_NAMES
# ---------------------------------------------------------------------------


def test_count_added_lines_skips_file_header() -> None:
    diff = "diff --git a/foo b/foo\n+++ b/foo\n+added-1\n+added-2\n-removed\n context\n"
    assert count_added_lines(diff) == 2


def test_count_added_lines_zero_on_no_added() -> None:
    diff = "diff --git a/foo b/foo\n-removed\n context\n"
    assert count_added_lines(diff) == 0


def test_fanout_threshold_sane_default() -> None:
    assert FANOUT_THRESHOLD == 150
    # Three lenses: bugs, architecture, tests. Types was removed (ruff ANN
    # covers it); security/perf/rootcause folded into bugs; duplication/
    # complexity folded into architecture.
    assert set(LENS_NAMES) == {"bugs", "architecture", "tests"}
    assert LENS_NAMES == ("bugs", "architecture", "tests")  # order matters


# ---------------------------------------------------------------------------
# assign_finding_ids
# ---------------------------------------------------------------------------


def test_assign_finding_ids_injects_stable_ids() -> None:
    review = "- [CRITICAL] a.py:1 — foo\n- [CRITICAL] b.py:2 — bar\n[CRITICAL] c.py:3 — baz"
    tagged, findings = assign_finding_ids(review)
    assert [f["id"] for f in findings] == ["F1", "F2", "F3"]
    assert "[F1]" in tagged and "[F2]" in tagged and "[F3]" in tagged
    assert tagged.count("[CRITICAL]") == 3


def test_assign_finding_ids_ignores_warnings() -> None:
    review = "- [WARNING] a.py:1 — minor\n- [CRITICAL] b.py:2 — real"
    tagged, findings = assign_finding_ids(review)
    assert len(findings) == 1
    assert findings[0]["id"] == "F1"
    assert "[F1] [CRITICAL]" in tagged
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
    raw = "[OVERTURN] F1 — trash\n[OVERTURN] F2 — noise"
    upheld = parse_arbiter_verdict(raw, ["F1", "F2"])
    assert upheld == {"F1", "F2"}


def test_parse_arbiter_verdict_missing_id_is_upheld() -> None:
    raw = "[OVERTURN] F1 — noise.\nSummary: 0 UPHELD, 1 OVERTURN."
    upheld = parse_arbiter_verdict(raw, ["F1", "F2"])
    assert upheld == {"F2"}


def test_parse_arbiter_verdict_empty_raw_is_all_upheld() -> None:
    assert parse_arbiter_verdict("", ["F1"]) == {"F1"}
    assert parse_arbiter_verdict("", []) == set()


def test_parse_arbiter_verdict_case_insensitive_tags() -> None:
    raw = "[upheld] F1 — real.\n[Overturn] F2 — theoretical.\nSummary: 1 UPHELD, 1 OVERTURN."
    assert parse_arbiter_verdict(raw, ["F1", "F2"]) == {"F1"}


# ---------------------------------------------------------------------------
# _aggregate_lens_outputs
# ---------------------------------------------------------------------------


def test_aggregate_lens_outputs_appends_global_summary() -> None:
    per_lens = [
        {
            "name": "bugs",
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
    assert "## Lens: bugs" in aggregated
    assert "## Lens: tests" in aggregated
    assert "Summary: 1 CRITICAL, 1 WARNING across 2 lenses." in aggregated


def test_aggregate_lens_outputs_marks_unavailable_lenses() -> None:
    per_lens = [
        {
            "name": "bugs",
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
    assert "Summary: 0 CRITICAL, 0 WARNING across 2 lenses." in aggregated


def test_aggregate_lens_outputs_distinguishes_router_skip_from_failure() -> None:
    per_lens = [
        {
            "name": "bugs",
            "status": "ok",
            "review": "No findings in this lens.\nSummary: 0 C, 0 W",
            "reviewer": "opencode",
            "error": "",
        },
        {
            "name": "architecture",
            "status": "skipped_by_router",
            "review": "",
            "reviewer": None,
            "error": "no applicable files for this lens",
        },
        {"name": "tests", "status": "timeout", "review": "", "reviewer": "opencode", "error": "opencode timeout"},
    ]
    aggregated = _aggregate_lens_outputs(per_lens)
    assert "Skipped by router: no applicable files" in aggregated
    assert "Lens unavailable: opencode timeout" in aggregated


# ---------------------------------------------------------------------------
# _render_fanout_output
# ---------------------------------------------------------------------------


def test_render_fanout_output_splits_upheld_and_overturned() -> None:
    per_lens = [{"name": "bugs", "status": "ok", "review": "none", "reviewer": "opencode", "error": ""}]
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
    assert "purely hypothetical" in rendered
    assert "Summary: 1 UPHELD, 1 OVERTURN" in rendered


def test_render_fanout_output_no_findings_shows_none() -> None:
    rendered = _render_fanout_output(
        [{"name": "bugs", "status": "ok", "review": "none", "reviewer": "opencode", "error": ""}],
        findings=[],
        upheld_ids=set(),
        arbiter={"status": "skipped", "upheld_ids": set(), "raw": "", "error": ""},
    )
    assert "_(none)_" in rendered
    assert "Summary: 0 UPHELD, 0 OVERTURN" in rendered


# ---------------------------------------------------------------------------
# Lens router — applicable_lenses
# ---------------------------------------------------------------------------


def test_applicable_lenses_docs_only_returns_empty() -> None:
    """Docs-only diff: no lens applies — entire review is skipped."""
    files = "docs/architecture.md\ntasks/EC-013.md\nREADME.md"
    assert applicable_lenses(files) == []


def test_applicable_lenses_python_file_runs_all_three() -> None:
    files = "src/foo.py\nsrc/bar.py"
    assert applicable_lenses(files) == list(LENS_NAMES)


def test_applicable_lenses_typescript_runs_all_three() -> None:
    """With types-lens removed, TS now triggers all three lenses."""
    files = "web/src/foo.ts\nweb/src/bar.tsx"
    assert applicable_lenses(files) == list(LENS_NAMES)


def test_applicable_lenses_mixed_python_and_js_runs_all() -> None:
    files = "hooks/foo.py\nweb/src/foo.ts\nweb/src/bar.jsx"
    assert set(applicable_lenses(files)) == set(LENS_NAMES)


def test_applicable_lenses_config_only_runs_bugs() -> None:
    """TOML/YAML/JSON: only bugs lens (config-surprise scope). Architecture
    and tests need executable code."""
    files = "pyproject.toml\npackage.json\n.github/workflows/ci.yml"
    assert applicable_lenses(files) == ["bugs"]


def test_applicable_lenses_dockerfile_runs_bugs() -> None:
    """Dockerfile is matched by basename, not extension."""
    assert applicable_lenses("Dockerfile") == ["bugs"]
    assert applicable_lenses("docker-compose.yml") == ["bugs"]


def test_applicable_lenses_empty_string_returns_empty() -> None:
    """No files → no lens applies → full skip."""
    assert applicable_lenses("") == []


def test_applicable_lenses_shell_script_runs_all_three() -> None:
    files = "scripts/deploy.sh"
    assert set(applicable_lenses(files)) == set(LENS_NAMES)


def test_applicable_lenses_preserves_lens_names_order() -> None:
    files = "src/foo.py"
    result = applicable_lenses(files)
    assert result == list(LENS_NAMES)


def test_applicable_lenses_mixed_code_and_config_runs_all() -> None:
    """Code triggers all three, config is also OK for bugs — all three run."""
    files = "src/foo.py\npyproject.toml"
    assert set(applicable_lenses(files)) == set(LENS_NAMES)


# ---------------------------------------------------------------------------
# run_review pre-router skip
# ---------------------------------------------------------------------------


def test_run_review_skips_on_docs_only_diff() -> None:
    """Pre-router short-circuit: docs-only diff → SKIP, no LLM calls."""
    diff = "diff --git a/README.md b/README.md\n+++ b/README.md\n+A new line.\n"
    files = "README.md"
    with (
        patch("hook.save_log") as mock_log,
        patch("hook._run_single_call") as mock_single,
        patch("hook._run_fanout_with_arbiter") as mock_fanout,
    ):
        display, verdict = run_review(diff, files, is_merge=False)

    assert verdict == "SKIP"
    assert display is None
    mock_single.assert_not_called()
    mock_fanout.assert_not_called()
    # SKIP with explicit reason logged
    log_verdict = mock_log.call_args[0][0]
    assert log_verdict == "SKIP"


def test_run_review_runs_single_call_on_small_code_diff() -> None:
    """Small code diff → single-call path is selected."""
    diff = "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n+def bar():\n+    return 1\n"
    files = "foo.py"
    with (
        patch("hook._run_single_call", return_value=(None, "OK")) as mock_single,
        patch("hook._run_fanout_with_arbiter") as mock_fanout,
    ):
        run_review(diff, files, is_merge=False)

    mock_single.assert_called_once()
    mock_fanout.assert_not_called()


def test_run_review_runs_fanout_on_large_code_diff() -> None:
    """Diff above FANOUT_THRESHOLD → fan-out path is selected."""
    added = [f"+line-{i}" for i in range(FANOUT_THRESHOLD + 5)]
    diff = "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n" + "\n".join(added)
    files = "foo.py"
    with (
        patch("hook._run_single_call") as mock_single,
        patch("hook._run_fanout_with_arbiter", return_value=(None, "OK")) as mock_fanout,
    ):
        run_review(diff, files, is_merge=False)

    mock_fanout.assert_called_once()
    mock_single.assert_not_called()


# ---------------------------------------------------------------------------
# Commit message injection (CLAUDE_COMMIT_MSG)
# ---------------------------------------------------------------------------


def test_build_user_prompt_includes_commit_message_when_env_set() -> None:
    with patch.dict(os.environ, {"CLAUDE_COMMIT_MSG": "feat: add new feature"}):
        prompt = build_user_prompt("diff-body", "foo.py", is_merge=False)
    assert "Developer's commit message draft" in prompt
    assert "feat: add new feature" in prompt
    # Instruction to verify message against code is present.
    assert "verify claims against the diff" in prompt


def test_build_user_prompt_omits_commit_message_when_env_unset() -> None:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_COMMIT_MSG"}
    with patch.dict(os.environ, env, clear=True):
        prompt = build_user_prompt("diff-body", "foo.py", is_merge=False)
    assert "Developer's commit message draft" not in prompt


def test_build_user_prompt_omits_commit_message_when_env_whitespace() -> None:
    with patch.dict(os.environ, {"CLAUDE_COMMIT_MSG": "   \n  "}):
        prompt = build_user_prompt("diff-body", "foo.py", is_merge=False)
    assert "Developer's commit message draft" not in prompt


# ---------------------------------------------------------------------------
# _arbitrate_single_call_review — single-call + arbiter integration
# ---------------------------------------------------------------------------


_SINGLE_CALL_REVIEW_WITH_CRITICAL_AND_WARNING = (
    "### Section 1 — File audit and tool-use log\n"
    "- foo.py — REVIEWED\n"
    "### Section 2 — Findings\n"
    "- [CRITICAL] foo.py:10 — `eval(x)` — RCE.\n"
    "- [WARNING] foo.py:12 — minor style nit.\n"
    "Summary: 1 CRITICAL, 1 WARNING across 1 files."
)


def _mock_arbiter(upheld_ids: set[str]) -> dict:
    return {
        "status": "ok",
        "upheld_ids": upheld_ids,
        "raw": "(arbiter rationales)",
        "error": "",
    }


def test_arbitrate_returns_raw_review_when_arbiter_overturns_all() -> None:
    """When arbiter overturns every CRITICAL the verdict becomes OK and the
    raw reviewer output is returned so main() can surface [WARNING] lines."""
    from hook import _arbitrate_single_call_review

    with patch("hook.run_arbiter", return_value=_mock_arbiter(upheld_ids=set())), patch("hook.save_log"):
        display, verdict = _arbitrate_single_call_review(
            review=_SINGLE_CALL_REVIEW_WITH_CRITICAL_AND_WARNING,
            reviewer="opencode",
            diff="diff --git a/foo.py b/foo.py\n+eval(x)\n",
            files="foo.py",
        )

    assert verdict == "OK"
    assert "[WARNING] foo.py:12" in display
    assert "Upheld findings (blocking)" not in display


def test_arbitrate_returns_synthesized_display_on_block() -> None:
    """When arbiter UPHOLDs at least one critical the verdict is BLOCK and
    the synthesized display is returned."""
    from hook import _arbitrate_single_call_review

    with patch("hook.run_arbiter", return_value=_mock_arbiter(upheld_ids={"F1"})), patch("hook.save_log"):
        display, verdict = _arbitrate_single_call_review(
            review=_SINGLE_CALL_REVIEW_WITH_CRITICAL_AND_WARNING,
            reviewer="opencode",
            diff="diff --git a/foo.py b/foo.py\n+eval(x)\n",
            files="foo.py",
        )

    assert verdict == "BLOCK"
    assert "Upheld findings (blocking)" in display
    assert "[F1] [CRITICAL] foo.py:10" in display


def test_arbitrate_skips_arbiter_when_zero_criticals() -> None:
    """No criticals → no arbiter call → raw review returned with OK."""
    from hook import _arbitrate_single_call_review

    review = (
        "### Section 1 — File audit\n"
        "- foo.py — REVIEWED\n"
        "### Section 2 — Findings\n"
        "- [WARNING] foo.py:1 — style.\n"
        "Summary: 0 CRITICAL, 1 WARNING across 1 files."
    )
    with patch("hook.run_arbiter") as mock_arbiter, patch("hook.save_log"):
        display, verdict = _arbitrate_single_call_review(
            review=review,
            reviewer="opencode",
            diff="",
            files="foo.py",
        )

    assert verdict == "OK"
    assert display == review
    mock_arbiter.assert_not_called()


def test_arbitrate_blocks_malformed_review_without_calling_arbiter() -> None:
    """Malformed (no Summary terminator) → fail-closed BLOCK, no arbiter."""
    from hook import _arbitrate_single_call_review

    malformed = "- [CRITICAL] foo.py:1 — bug (no summary terminator)"
    with patch("hook.run_arbiter") as mock_arbiter, patch("hook.save_log"):
        display, verdict = _arbitrate_single_call_review(
            review=malformed,
            reviewer="opencode",
            diff="",
            files="foo.py",
        )

    assert verdict == "BLOCK"
    assert display == malformed
    mock_arbiter.assert_not_called()


# ---------------------------------------------------------------------------
# main() BLOCK-path developer guidance (fix-in-one-pass + trade-off channel)
# ---------------------------------------------------------------------------


def _invoke_main_on_block(
    review_text: str = "- [CRITICAL] foo.py:1 — bug\n\nSummary: 1 CRITICAL.",
) -> str:
    """Run hook.main() with a forced BLOCK verdict, return captured stderr."""
    import io
    import sys

    from hook import main as hook_main

    buf = io.StringIO()
    with (
        patch("hook.collect_diff", return_value=("diff-body", "foo.py", False)),
        patch("hook.run_review", return_value=(review_text, "BLOCK")),
        patch.object(sys, "stderr", buf),
    ):
        try:
            hook_main()
        except SystemExit as exc:
            assert exc.code == 1, f"BLOCK must exit 1, got {exc.code}"
        else:
            raise AssertionError("main() should have called sys.exit(1) on BLOCK")

    return buf.getvalue()


def test_main_block_emits_fix_in_one_pass_directive() -> None:
    """Regression: BLOCK output must include the directive telling the agent
    to address all CRITICAL+WARNING in one follow-up commit — preventing the
    iterative "sneak through" that burns reviewer budget."""
    stderr = _invoke_main_on_block()

    assert "Fix-in-one-pass" in stderr
    assert "EVERY [CRITICAL]" in stderr
    assert "EVERY [WARNING]" in stderr


def test_main_block_still_emits_tradeoff_channel_info() -> None:
    """Existing trade-off channel guidance must not regress when the new
    fix-in-one-pass directive is added to the BLOCK path."""
    stderr = _invoke_main_on_block()

    assert "Trade-off channel" in stderr
    assert "review-note:" in stderr


def test_main_block_renders_review_summary() -> None:
    """The upheld findings body itself must reach the developer on BLOCK."""
    review = "- [CRITICAL] foo.py:7 — regression\n\nSummary: 1 CRITICAL."
    stderr = _invoke_main_on_block(review)

    assert "Review BLOCKED this commit" in stderr
    assert "foo.py:7" in stderr

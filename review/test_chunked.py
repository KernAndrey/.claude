"""Unit tests for ``chunked.py``.

Mocks the reviewer subprocess via ``hook.run_reviewer`` so we never spawn
real backends. Focuses on the wiring contracts the advisor flagged:
ID re-tagging, validate-fail short-circuit, arbiter prompt augmentation,
job composition.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch


import chunked
import consolidation
from config import RunnerConfig


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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


def _runner_factory(name_status: str, numstat: str) -> Callable[[list[str]], str]:
    def runner(args: list[str]) -> str:
        if "--name-status" in args:
            return name_status
        if "--numstat" in args:
            return numstat
        raise AssertionError(f"unexpected git args: {args}")

    return runner


# ---------------------------------------------------------------------------
# manifest_present
# ---------------------------------------------------------------------------


def test_manifest_present_true(tmp_path: Path) -> None:
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text("version: 1\n")
    assert chunked.manifest_present(tmp_path) is True


def test_manifest_present_false_no_dir(tmp_path: Path) -> None:
    assert chunked.manifest_present(tmp_path) is False


def test_manifest_present_false_no_file(tmp_path: Path) -> None:
    (tmp_path / ".review").mkdir()
    assert chunked.manifest_present(tmp_path) is False


# ---------------------------------------------------------------------------
# _manifest_has_invariants — direct unit tests for all branches
# ---------------------------------------------------------------------------


def test_manifest_has_invariants_empty_string() -> None:
    assert chunked._manifest_has_invariants("") is False


def test_manifest_has_invariants_missing_key() -> None:
    assert chunked._manifest_has_invariants("version: 1\n") is False


def test_manifest_has_invariants_explicit_null() -> None:
    assert chunked._manifest_has_invariants("version: 1\ncross_chunk_invariants: null\n") is False


def test_manifest_has_invariants_empty_list() -> None:
    assert chunked._manifest_has_invariants("version: 1\ncross_chunk_invariants: []\n") is False


def test_manifest_has_invariants_malformed_yaml() -> None:
    assert chunked._manifest_has_invariants("version: 1\n  bad: [") is False


def test_manifest_has_invariants_top_level_list() -> None:
    assert chunked._manifest_has_invariants("- just a list\n") is False


def test_manifest_has_invariants_non_empty_list() -> None:
    manifest = "version: 1\ncross_chunk_invariants:\n  - one item\n"
    assert chunked._manifest_has_invariants(manifest) is True


# ---------------------------------------------------------------------------
# Validation short-circuit — no reviewer subprocess should be spawned
# ---------------------------------------------------------------------------


def test_validation_failure_short_circuits(tmp_path: Path) -> None:
    """When the manifest is invalid, no reviewer or arbiter is run."""
    (tmp_path / ".review").mkdir()
    diff = _diff_for_new_file("a.py", ["x"])
    # diff_hash deliberately wrong → stale_hash error
    manifest = textwrap.dedent("""
        version: 1
        diff_hash: 0000000000000000000000000000000000000000000000000000000000000000
        chunks:
          - id: c1
            files: [{path: a.py, line_ranges: all}]
            review_lenses: [bugs]
    """)
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    spawn_count = {"calls": 0}

    def fake_run_reviewer(*args: object, **kwargs: object) -> tuple[str, str, int]:
        spawn_count["calls"] += 1
        return ("", "", 0)

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("validators.manifest._default_git_runner", side_effect=runner),
    ):
        result = chunked.run_chunked_review(diff, "a.py", tmp_path)

    assert result.status == "manifest_invalid"
    assert spawn_count["calls"] == 0
    assert any(e.code == "stale_hash" for e in result.validation.errors)
    assert "stale_hash" in result.blocking_text


# ---------------------------------------------------------------------------
# ID re-tagging — chunk-prefixed and wholediff-prefixed
# ---------------------------------------------------------------------------


def test_chunk_job_retags_findings_with_chunk_and_backend() -> None:
    """A reviewer producing `[CRITICAL] ...` lines gets `[<chunk>-<backend>-F<n>]`
    prepended exactly as consolidation expects."""
    chunk = {"id": "models", "files": [{"path": "a.py", "line_ranges": "all"}], "review_lenses": ["bugs"]}
    backend_cfg = RunnerConfig("opencode", "x")
    fake_review = textwrap.dedent("""\
        - [CRITICAL] a.py:1 — `x = 1` — null deref
        - [WARNING]  a.py:2 — `y = 2` — soft warn
        - [CRITICAL] a.py:3 — `z = 3` — race condition
    """)

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        return (fake_review, "", 0)

    with patch("hook.run_reviewer", side_effect=fake_run_reviewer):
        jr = chunked._run_chunk_job(chunk, backend_cfg, "system", "cached", Path("/tmp"))

    assert jr.layer == "chunk"
    assert jr.chunk_id == "models"
    assert jr.backend == "opencode"
    assert len(jr.findings) == 2
    assert jr.findings[0]["id"] == "models-opencode-F1"
    assert jr.findings[1]["id"] == "models-opencode-F2"
    # Tagged text contains the prefix verbatim — what the arbiter will see.
    assert "[models-opencode-F1]" in jr.tagged_text
    assert "[models-opencode-F2]" in jr.tagged_text


def test_wholediff_job_retags_findings_with_lens_and_backend() -> None:
    backend_cfg = RunnerConfig("claude", "sonnet")
    fake_review = "- [CRITICAL] x.py:5 — `q()` — N+1\n"

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        return (fake_review, "", 0)

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("hook.build_lens_system_prompt", return_value="lens system"),
        patch("hook.build_user_prompt", return_value="diff user"),
    ):
        jr = chunked._run_wholediff_job("bugs", backend_cfg, "diff", "x.py", False)

    assert jr.layer == "wholediff"
    assert jr.lens == "bugs"
    assert jr.backend == "claude"
    assert len(jr.findings) == 1
    assert jr.findings[0]["id"] == "wholediff-bugs-claude-F1"
    assert "[wholediff-bugs-claude-F1]" in jr.tagged_text


# ---------------------------------------------------------------------------
# Reviewer failure modes are converted to synthetic blocking findings
# ---------------------------------------------------------------------------


def test_reviewer_timeout_emits_synthetic_critical() -> None:
    import subprocess

    chunk = {"id": "c1", "files": [{"path": "a.py", "line_ranges": "all"}], "review_lenses": ["bugs"]}
    backend_cfg = RunnerConfig("kimi", "kimi-x")

    def boom(*a: object, **kw: object) -> tuple[str, str, int]:
        raise subprocess.TimeoutExpired(cmd="kimi", timeout=5)

    # boom raises for every run_reviewer call, so the primary AND the claude
    # fallback both time out — only then does the synthetic CRITICAL fire.
    with patch("hook.run_reviewer", side_effect=boom):
        jr = chunked._run_chunk_job(chunk, backend_cfg, "system", "cached", Path("/tmp"))

    assert jr.status == "timeout"
    # The synthetic CRITICAL must still flow through assign_finding_ids and
    # carry a chunk-prefixed ID (primary's name, regex-clean) so the arbiter
    # sees a blocking placeholder.
    assert len(jr.findings) == 1
    assert jr.findings[0]["id"] == "c1-kimi-F1"
    assert "<reviewer timeout>" in jr.findings[0]["line"]
    # message names both backends so a debugger knows the fallback also failed
    assert "fallback" in jr.findings[0]["line"]


def test_reviewer_file_not_found_emits_synthetic_critical() -> None:
    """Missing backend binary must produce a synthetic blocking finding."""
    chunk = {"id": "c1", "files": [{"path": "a.py", "line_ranges": "all"}], "review_lenses": ["bugs"]}
    backend_cfg = RunnerConfig("missing", "m")

    def boom(*a: object, **kw: object) -> tuple[str, str, int]:
        raise FileNotFoundError("no such binary")

    with patch("hook.run_reviewer", side_effect=boom):
        jr = chunked._run_chunk_job(chunk, backend_cfg, "system", "cached", Path("/tmp"))

    assert jr.status == "unreachable"
    assert len(jr.findings) == 1
    assert jr.findings[0]["id"] == "c1-missing-F1"
    assert "reviewer unreachable" in jr.findings[0]["line"]
    assert "no such binary" in jr.findings[0]["line"]


def test_chunk_job_falls_back_to_claude_when_primary_fails() -> None:
    """Chunked-path gap closure: a primary (kimi) that fails with rc!=0
    (quota/error) must fall back to FALLBACK (claude) instead of silently
    contributing zero findings. The review is attributed to the backend that
    actually produced it (claude), so the finding ID prefix and stats are
    correct."""
    chunk = {"id": "c1", "files": [{"path": "a.py", "line_ranges": "all"}], "review_lenses": ["bugs"]}
    backend_cfg = RunnerConfig("kimi", "kimi-x")

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        if cfg.backend == "kimi":
            return "quota exceeded", "rate limited", 1  # primary fails (rc!=0)
        return "- [CRITICAL] a.py:1 — `x` — real bug from claude\n", "", 0  # fallback succeeds

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("chunked.FALLBACK", RunnerConfig("claude", "sonnet")),
    ):
        jr = chunked._run_chunk_job(chunk, backend_cfg, "system", "cached", Path("/tmp"))

    assert jr.status == "ok"
    assert jr.backend == "claude"  # attributed to the fallback, not kimi
    assert len(jr.findings) == 1
    assert jr.findings[0]["id"] == "c1-claude-F1"  # regex-clean prefix from used_backend
    assert "real bug from claude" in jr.findings[0]["line"]


# ---------------------------------------------------------------------------
# Arbiter user prompt carries the manifest section
# ---------------------------------------------------------------------------


def test_arbiter_user_prompt_includes_manifest() -> None:
    findings = [{"id": "models-opencode-F1", "line": "[models-opencode-F1] [CRITICAL] x"}]
    diff = "diff --git a/x b/x\n"
    manifest = "version: 1\nchunks: []\n"
    prompt = chunked._build_arbiter_user_prompt(diff, manifest, findings)
    assert "## Manifest" in prompt
    assert "version: 1" in prompt
    assert "## Findings to cluster + arbitrate" in prompt
    assert "[models-opencode-F1]" in prompt


def test_arbiter_skipped_when_no_findings() -> None:
    raw, status, err, attempts = chunked._run_chunked_arbiter("diff", "manifest", [])
    assert status == "skipped_no_findings"
    assert raw == ""
    assert err is None
    assert attempts == 0


def test_arbiter_not_skipped_when_invariants_present_despite_no_findings() -> None:
    """C6: invariant-only regressions must still reach the arbiter."""
    manifest = "version: 1\ncross_chunk_invariants:\n  - id: INV1\n    description: Always check this\n"
    with (
        patch("chunked._read_text", return_value="system prompt"),
        patch(
            "hook.run_reviewer",
            return_value=(
                "[CLUSTER C1] arbiter-INV1\n[UPHELD] C1\nSummary:\nChunked:\n",
                "",
                0,
            ),
        ),
    ):
        raw, status, err, attempts = chunked._run_chunked_arbiter("diff", manifest, [])
    assert status == "ran"
    assert "arbiter-INV1" in raw


def test_arbiter_missing_prompt_returns_unreachable() -> None:
    """C5: missing arbiter prompt must fail-open with a clear error."""
    with patch("chunked._read_text", return_value=""):
        raw, status, err, attempts = chunked._run_chunked_arbiter(
            "diff", "manifest", [{"id": "F1", "line": "- [F1] [CRITICAL] x"}]
        )
    assert status == "unreachable"
    assert err is not None
    assert attempts == 0


# ---------------------------------------------------------------------------
# Job composition: chunk_count × backends + lenses × backends
# ---------------------------------------------------------------------------


def test_build_jobs_layout(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    inputs = chunked._RunInputs(manifest_text="dummy")
    inputs.manifest = {
        "chunks": [
            {"id": "models", "files": [{"path": "a.py"}], "review_lenses": ["bugs"]},
            {"id": "security", "files": [{"path": "b.py"}], "review_lenses": ["bugs"]},
        ],
        "default_related_files": [],
    }
    inputs.validation = chunked.ValidationResult()  # ok=True default

    backends = [RunnerConfig("a", "m1"), RunnerConfig("b", "m2")]
    with (
        patch("chunked.CHUNKED_BACKENDS", backends),
        patch("chunked.config.ALLOWED_LENSES", frozenset({"bugs", "architecture", "tests"})),
    ):
        jobs = chunked._build_jobs(inputs, diff, "a.py b.py", False, tmp_path)

    # 2 chunks × 2 backends + 3 lenses × 2 backends = 4 + 6 = 10 jobs
    assert len(jobs) == 10


# ---------------------------------------------------------------------------
# Cached block prompt content
# ---------------------------------------------------------------------------


def test_cached_block_includes_diff_and_manifest(tmp_path: Path) -> None:
    block = chunked._build_cached_block(
        diff="diff --git a/x b/x\n+hi\n",
        manifest_text="version: 1\nchunks: []\n",
        repo_root=tmp_path,
        default_related=[],
    )
    assert "## Full staged diff" in block
    assert "diff --git a/x b/x" in block
    assert "## Manifest" in block
    assert "chunks: []" in block


def test_cached_block_includes_default_related(tmp_path: Path) -> None:
    (tmp_path / "shared.md").write_text("shared context content")
    block = chunked._build_cached_block(
        diff="diff",
        manifest_text="m",
        repo_root=tmp_path,
        default_related=["shared.md"],
    )
    assert "shared.md" in block
    assert "shared context content" in block


def test_cached_block_includes_claude_md_when_present(tmp_path: Path) -> None:
    """C20: repo CLAUDE.md must appear in the cached block when present."""
    (tmp_path / "CLAUDE.md").write_text("# Project rules\n\nBe careful.")
    block = chunked._build_cached_block(
        diff="diff",
        manifest_text="m",
        repo_root=tmp_path,
        default_related=[],
    )
    assert "## Project context (CLAUDE.md)" in block
    assert "Be careful." in block


def test_cached_block_omits_claude_md_when_absent(tmp_path: Path) -> None:
    """C20: missing CLAUDE.md must be handled cleanly (no section)."""
    block = chunked._build_cached_block(
        diff="diff",
        manifest_text="m",
        repo_root=tmp_path,
        default_related=[],
    )
    assert "## Project context (CLAUDE.md)" not in block


def test_chunk_variable_block_includes_rationale(tmp_path: Path) -> None:
    """The variable block surfaces chunk_id + rationale; review_lenses was
    removed from the schema (reviewer covers all three lenses regardless)."""
    chunk = {
        "id": "models",
        "rationale": "new ORM fields",
        "related_files": [],
    }
    block = chunked._build_chunk_variable_block(chunk, tmp_path)
    assert "chunk_id: models" in block
    assert "new ORM fields" in block
    # review_lenses must NOT appear — that field is ignored now.
    assert "review_lenses" not in block


def test_chunk_variable_block_includes_related_files(tmp_path: Path) -> None:
    """C3: non-empty related_files must inject file content into the prompt."""
    (tmp_path / "helper.py").write_text("def helper(): pass")
    chunk = {"id": "models", "related_files": ["helper.py"]}
    block = chunked._build_chunk_variable_block(chunk, tmp_path)
    assert "## Chunk-specific related files" in block
    assert "helper.py" in block
    assert "def helper(): pass" in block


def test_chunk_variable_block_empty_related_files_omits_section(tmp_path: Path) -> None:
    """C3: empty related_files must not produce a related-files section."""
    chunk = {"id": "models", "related_files": []}
    block = chunked._build_chunk_variable_block(chunk, tmp_path)
    assert "## Chunk-specific related files" not in block


# ---------------------------------------------------------------------------
# End-to-end success path
# ---------------------------------------------------------------------------


def test_run_chunked_review_end_to_end_ok(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    manifest = textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: all
        """
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        if cfg.backend == "fake":
            return "- [CRITICAL] a.py:1 — `x = 1` — bug\n", "", 0
        # arbiter
        return (
            "[CLUSTER C1] c1-fake-F1\n[UPHELD] C1\nSummary: 1 cluster, 1 upheld\nChunked: done\n",
            "",
            0,
        )

    def fake_read_text(path: Path) -> str:
        if path.name in ("chunk_review.md", "arbiter_multi.md"):
            return "system prompt"
        return ""

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("validators.manifest._default_git_runner", side_effect=runner),
        patch("chunked.CHUNKED_BACKENDS", [RunnerConfig("fake", "m")]),
        patch("chunked.config.ALLOWED_LENSES", frozenset()),
        patch("chunked._read_text", side_effect=fake_read_text),
    ):
        result = chunked.run_chunked_review(diff, "a.py", tmp_path)

    assert result.status == "ok"
    assert result.arbiter_status == "ran"
    assert len(result.clusters) == 1
    assert len(result.upheld_clusters) == 1
    assert result.upheld_clusters[0].cluster_id == "C1"
    assert result.findings_json_text
    assert "c1-fake-F1" in result.findings_json_text


# ---------------------------------------------------------------------------
# Infrastructure-failure path
# ---------------------------------------------------------------------------


def test_run_chunked_review_infrastructure_failure(tmp_path: Path) -> None:
    diff = _diff_for_new_file("a.py", ["x"])
    manifest = textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: all
        """
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        raise subprocess.TimeoutExpired(cmd="fake", timeout=5)

    def fake_read_text(path: Path) -> str:
        if path.name == "chunk_review.md":
            return "system prompt"
        return ""

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("validators.manifest._default_git_runner", side_effect=runner),
        patch("chunked.CHUNKED_BACKENDS", [RunnerConfig("fake", "m")]),
        patch("chunked.config.ALLOWED_LENSES", frozenset()),
        patch("chunked._read_text", side_effect=fake_read_text),
    ):
        result = chunked.run_chunked_review(diff, "a.py", tmp_path)

    assert result.status == "infrastructure_failure"
    assert result.arbiter_status == "skipped_infrastructure_failure"
    assert len(result.upheld_clusters) == 1
    assert result.upheld_clusters[0].cluster_id == "INFRA1"
    assert "no code was actually reviewed" in result.blocking_text


def test_run_chunked_review_empty_backends_is_infrastructure_failure(tmp_path: Path) -> None:
    """C22: empty CHUNKED_BACKENDS produces zero jobs → infrastructure failure."""
    diff = _diff_for_new_file("a.py", ["x"])
    manifest = textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: all
        """
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    with (
        patch("validators.manifest._default_git_runner", side_effect=runner),
        patch("chunked.CHUNKED_BACKENDS", []),
        patch("chunked.config.ALLOWED_LENSES", frozenset()),
    ):
        result = chunked.run_chunked_review(diff, "a.py", tmp_path)

    assert result.status == "infrastructure_failure"
    assert result.arbiter_status == "skipped_infrastructure_failure"
    assert "no reviewer jobs were built" in result.blocking_text


def test_all_error_jobs_go_to_arbiter_not_infrastructure_failure(tmp_path: Path) -> None:
    """C7: rc≠0 with parseable findings is a real review, not an infra failure."""
    diff = _diff_for_new_file("a.py", ["x"])
    manifest = textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: all
        """
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        if cfg.backend == "fake":
            # rc != 0 but parseable output → status="error" downstream
            return "- [CRITICAL] a.py:1 — `x = 1` — bug\n", "stderr", 1
        # arbiter
        return (
            "[CLUSTER C1] c1-fake-F1\n[UPHELD] C1\nSummary: 1 cluster, 1 upheld\nChunked: done\n",
            "",
            0,
        )

    def fake_read_text(path: Path) -> str:
        if path.name in ("chunk_review.md", "arbiter_multi.md"):
            return "system prompt"
        return ""

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("validators.manifest._default_git_runner", side_effect=runner),
        patch("chunked.CHUNKED_BACKENDS", [RunnerConfig("fake", "m")]),
        # FALLBACK=None isolates the rc!=0-handling path: this test asserts a
        # real review error (rc!=0 + parseable findings) reaches the arbiter,
        # which is the primary's behavior before any fallback kicks in.
        patch("chunked.FALLBACK", None),
        patch("chunked.config.ALLOWED_LENSES", frozenset()),
        patch("chunked._read_text", side_effect=fake_read_text),
    ):
        result = chunked.run_chunked_review(diff, "a.py", tmp_path)

    assert result.status == "ok"
    assert result.arbiter_status == "ran"


def test_mixed_error_and_timeout_falls_through_to_arbiter(tmp_path: Path) -> None:
    """Not all jobs are infra-only → arbiter must run."""
    diff = _diff_for_new_file("a.py", ["x"])
    manifest = textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: all
        """
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        if cfg.backend == "fake1":
            # rc != 0 with parseable output → status="error" downstream
            return "- [CRITICAL] a.py:1 — `x = 1` — bug\n", "stderr", 1
        if cfg.backend == "fake2":
            raise subprocess.TimeoutExpired(cmd="fake2", timeout=5)
        # arbiter
        return (
            "[CLUSTER C1] c1-fake1-F1\n[UPHELD] C1\nSummary: 1 cluster, 1 upheld\nChunked: done\n",
            "",
            0,
        )

    def fake_read_text(path: Path) -> str:
        if path.name in ("chunk_review.md", "arbiter_multi.md"):
            return "system prompt"
        return ""

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("validators.manifest._default_git_runner", side_effect=runner),
        patch(
            "chunked.CHUNKED_BACKENDS",
            [RunnerConfig("fake1", "m1"), RunnerConfig("fake2", "m2")],
        ),
        # FALLBACK=None: exercise per-job error/timeout handling directly,
        # without the safety net redirecting failed jobs to claude.
        patch("chunked.FALLBACK", None),
        patch("chunked.config.ALLOWED_LENSES", frozenset()),
        patch("chunked._read_text", side_effect=fake_read_text),
    ):
        result = chunked.run_chunked_review(diff, "a.py", tmp_path)

    assert result.status == "ok"
    assert result.arbiter_status == "ran"


def test_mixed_ok_and_error_falls_through_to_arbiter(tmp_path: Path) -> None:
    """Trivial mixed status — arbiter must run."""
    diff = _diff_for_new_file("a.py", ["x"])
    manifest = textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: all
        """
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        if cfg.backend == "fake1":
            return "- [CRITICAL] a.py:1 — `x = 1` — bug\n", "", 0
        if cfg.backend == "fake2":
            return "- [CRITICAL] a.py:1 — `x = 1` — bug\n", "stderr", 1
        # arbiter
        return (
            "[CLUSTER C1] c1-fake1-F1\n[UPHELD] C1\nSummary: 1 cluster, 1 upheld\nChunked: done\n",
            "",
            0,
        )

    def fake_read_text(path: Path) -> str:
        if path.name in ("chunk_review.md", "arbiter_multi.md"):
            return "system prompt"
        return ""

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("validators.manifest._default_git_runner", side_effect=runner),
        patch(
            "chunked.CHUNKED_BACKENDS",
            [RunnerConfig("fake1", "m1"), RunnerConfig("fake2", "m2")],
        ),
        patch("chunked.config.ALLOWED_LENSES", frozenset()),
        patch("chunked._read_text", side_effect=fake_read_text),
    ):
        result = chunked.run_chunked_review(diff, "a.py", tmp_path)

    assert result.status == "ok"
    assert result.arbiter_status == "ran"


def test_all_heterogeneous_infra_statuses_is_infrastructure_failure(tmp_path: Path) -> None:
    """Every status is in infra_only_statuses, even if heterogeneous → infrastructure_failure."""
    diff = _diff_for_new_file("a.py", ["x"])
    manifest = textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: all
        """
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        if cfg.backend == "fake1":
            raise subprocess.TimeoutExpired(cmd="fake1", timeout=5)
        if cfg.backend == "fake2":
            raise FileNotFoundError("no binary")
        if cfg.backend == "fake3":
            return "", "", 0
        # arbiter (unreachable for all-infra path)
        return (
            "[CLUSTER C1] c1-fake1-F1\n[UPHELD] C1\nSummary: 1 cluster, 1 upheld\nChunked: done\n",
            "",
            0,
        )

    def fake_read_text(path: Path) -> str:
        if path.name in ("chunk_review.md", "arbiter_multi.md"):
            return "system prompt"
        return ""

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("validators.manifest._default_git_runner", side_effect=runner),
        patch(
            "chunked.CHUNKED_BACKENDS",
            [
                RunnerConfig("fake1", "m1"),
                RunnerConfig("fake2", "m2"),
                RunnerConfig("fake3", "m3"),
            ],
        ),
        # FALLBACK=None: with a fallback every infra failure would be retried
        # on claude and stop being infra-only; this test pins the all-infra
        # detection that fires when there is no safety net.
        patch("chunked.FALLBACK", None),
        patch("chunked.config.ALLOWED_LENSES", frozenset()),
        patch("chunked._read_text", side_effect=fake_read_text),
    ):
        result = chunked.run_chunked_review(diff, "a.py", tmp_path)

    assert result.status == "infrastructure_failure"
    assert result.arbiter_status == "skipped_infrastructure_failure"
    assert len(result.upheld_clusters) == 1
    assert result.upheld_clusters[0].cluster_id == "INFRA1"


# ---------------------------------------------------------------------------
# Artifact persistence
# ---------------------------------------------------------------------------


def test_consolidate_arbiter_timeout_fails_open() -> None:
    """C21: arbiter timeout must convert every finding to an upheld singleton."""
    jr = _make_jr(findings=[{"id": "F1", "line": "- [F1] [CRITICAL] x"}])
    with patch("chunked._run_chunked_arbiter", return_value=("", "timeout", "timeout", 1)):
        raw, status, err, clusters = chunked._consolidate([jr], "diff", "manifest")
    assert status == "timeout"
    assert len(clusters) == 1
    assert clusters[0].upheld is True
    assert clusters[0].member_ids == ("F1",)


def test_consolidate_arbiter_unreachable_fails_open() -> None:
    """C21: arbiter unreachable must convert every finding to an upheld singleton."""
    jr = _make_jr(findings=[{"id": "F1", "line": "- [F1] [CRITICAL] x"}])
    with patch("chunked._run_chunked_arbiter", return_value=("", "unreachable", "unreachable", 1)):
        raw, status, err, clusters = chunked._consolidate([jr], "diff", "manifest")
    assert status == "unreachable"
    assert len(clusters) == 1
    assert clusters[0].upheld is True
    assert clusters[0].member_ids == ("F1",)


def test_consolidate_arbiter_error_fails_open() -> None:
    """C21: arbiter error (rc≠0) must convert every finding to an upheld singleton."""
    jr = _make_jr(findings=[{"id": "F1", "line": "- [F1] [CRITICAL] x"}])
    with patch("chunked._run_chunked_arbiter", return_value=("bad output", "error", "rc=1", 1)):
        raw, status, err, clusters = chunked._consolidate([jr], "diff", "manifest")
    assert status == "error"
    assert len(clusters) == 1
    assert clusters[0].upheld is True
    assert clusters[0].member_ids == ("F1",)


def test_write_artifacts(tmp_path: Path) -> None:
    jr = chunked.ReviewerJobResult(
        job_id="chunk:c1:fake",
        layer="chunk",
        chunk_id="c1",
        lens=None,
        backend="fake",
        status="ok",
        raw_text="raw review",
        tagged_text="tagged review",
        findings=[{"id": "c1-fake-F1", "line": "- [c1-fake-F1] [CRITICAL] x"}],
        rc=0,
        stderr="",
        duration=1.5,
    )
    cluster = consolidation.FindingCluster(
        cluster_id="C1",
        member_ids=("c1-fake-F1",),
        contributors=("fake",),
        canonical_line="- [c1-fake-F1] [CRITICAL] x",
        upheld=True,
        chunk_id="c1",
        invariant_id=None,
    )
    result = chunked.ChunkedResult(
        status="ok",
        validation=chunked.ValidationResult(),
        job_results=[jr],
        arbiter_raw="arbiter output",
        arbiter_status="ran",
        arbiter_error=None,
        clusters=[cluster],
        upheld_clusters=[cluster],
        blocking_text="1 BLOCKING",
        findings_json_text='{"metrics": {}}',
        metrics={"phase": "complete"},
        started_at=0.0,
        ended_at=1.0,
    )

    review_dir = chunked.write_artifacts(result, tmp_path)

    assert (review_dir / "findings.json").read_text() == '{"metrics": {}}'
    assert (review_dir / "blocking.txt").read_text() == "1 BLOCKING"
    assert (review_dir / "metrics.json").read_text() == json.dumps({"phase": "complete"}, indent=2)
    assert (review_dir / "arbiter_raw.txt").read_text() == "arbiter output"

    raw_dir = review_dir / "raw"
    assert (raw_dir / "chunk_c1_fake.txt").read_text() == "tagged review"
    raw_json = json.loads((raw_dir / "chunk_c1_fake.json").read_text())
    assert raw_json["job_id"] == "chunk:c1:fake"
    assert raw_json["findings"][0]["id"] == "c1-fake-F1"

    # Stale arbiter_raw.txt must be removed when the new run has no output.
    result2 = chunked.ChunkedResult(
        status="ok",
        validation=chunked.ValidationResult(),
        job_results=[jr],
        arbiter_raw="",
        arbiter_status="skipped_no_findings",
        arbiter_error=None,
        clusters=[],
        upheld_clusters=[],
        blocking_text="ok",
        findings_json_text="{}",
        metrics={},
        started_at=0.0,
        ended_at=1.0,
    )
    chunked.write_artifacts(result2, tmp_path)
    assert not (review_dir / "arbiter_raw.txt").exists()


# ---------------------------------------------------------------------------
# Persistence + retry (Step 2 of plan)
# ---------------------------------------------------------------------------


def _make_jr(
    job_id: str = "chunk:c1:fake",
    status: str = "ok",
    findings: list[dict] | None = None,
) -> chunked.ReviewerJobResult:
    return chunked.ReviewerJobResult(
        job_id=job_id,
        layer="chunk",
        chunk_id="c1",
        lens=None,
        backend="fake",
        status=status,
        raw_text="raw",
        tagged_text="tagged",
        findings=findings or [],
        rc=0,
        stderr="",
        duration=0.1,
        attempts=1,
    )


def test_persister_disabled_when_repo_root_is_none() -> None:
    """The default _NULL_PERSISTER (repo_root=None) must be a complete no-op."""
    p = chunked._RunPersister()
    p.reset_state_dirs()
    p.record_validation(chunked.ValidationResult())
    p.record_jobs_plan([{"index": 0}])
    p.record_job_complete(_make_jr())
    p.record_jobs_complete([_make_jr()])
    p.record_arbiter_input("prompt")
    p.record_arbiter_output("raw", "ran", None, 1)
    p.record_clusters([])
    p.record_retry_attempt("j1", "fake", 1, "ok", 0.1, "")
    p.record_crash("traceback")


def test_persister_writes_per_stage_files(tmp_path: Path) -> None:
    p = chunked._RunPersister(tmp_path)
    p.reset_state_dirs()
    p.record_validation(chunked.ValidationResult())
    p.record_jobs_plan([{"index": 0, "kind": "chunk"}])
    jr = _make_jr(findings=[{"id": "c1-fake-F1", "line": "- [c1-fake-F1] [CRITICAL] x"}])
    p.record_job_complete(jr)
    p.record_jobs_complete([jr])
    p.record_arbiter_input("ARBITER PROMPT")
    p.record_arbiter_output("CLUSTER C1", "ran", None, 1)
    p.record_clusters([])

    state = tmp_path / ".review" / "state"
    raw = tmp_path / ".review" / "raw"
    assert (state / "00_validation.json").exists()
    assert (state / "01_jobs_plan.json").exists()
    assert (state / "02_jobs_complete.json").exists()
    assert (state / "03_arbiter_input.txt").read_text() == "ARBITER PROMPT"
    assert (state / "04_arbiter_meta.json").exists()
    assert (state / "05_clusters.json").exists()
    assert (raw / "chunk_c1_fake.txt").read_text() == "tagged"
    raw_payload = json.loads((raw / "chunk_c1_fake.json").read_text())
    assert raw_payload["job_id"] == "chunk:c1:fake"
    assert raw_payload["findings"][0]["id"] == "c1-fake-F1"


def test_persister_reset_state_dirs_wipes_stale_files(tmp_path: Path) -> None:
    """reset_state_dirs() must wipe state/ and raw/ but leave manifest.yaml alone."""
    review_dir = tmp_path / ".review"
    review_dir.mkdir()
    (review_dir / "manifest.yaml").write_text("manifest content")
    (review_dir / "state").mkdir()
    (review_dir / "state" / "stale.json").write_text("stale")
    (review_dir / "raw").mkdir()
    (review_dir / "raw" / "old_job.txt").write_text("stale")

    p = chunked._RunPersister(tmp_path)
    p.reset_state_dirs()

    assert (review_dir / "manifest.yaml").read_text() == "manifest content"
    assert not (review_dir / "state" / "stale.json").exists()
    assert not (review_dir / "raw" / "old_job.txt").exists()
    assert (review_dir / "state").is_dir()
    assert (review_dir / "raw").is_dir()


def test_persister_retry_attempts_appended_to_jsonl(tmp_path: Path) -> None:
    p = chunked._RunPersister(tmp_path)
    p.reset_state_dirs()
    p.record_retry_attempt("j1", "fake", 1, "timeout", 5.0, "stderr A")
    p.record_retry_attempt("j1", "fake", 2, "ok", 1.5, "")
    p.record_retry_attempt("j2", "fake", 1, "empty_stdout", 2.0, "")

    jsonl = (tmp_path / ".review" / "state" / "retries.jsonl").read_text().splitlines()
    assert len(jsonl) == 3
    parsed = [json.loads(line) for line in jsonl]
    assert [(r["job_id"], r["attempt"], r["outcome"]) for r in parsed] == [
        ("j1", 1, "timeout"),
        ("j1", 2, "ok"),
        ("j2", 1, "empty_stdout"),
    ]


def test_run_with_retry_succeeds_second_attempt_after_timeout(tmp_path: Path) -> None:
    """First attempt times out, second returns ok — wrapper returns ok with attempts=2."""
    p = chunked._RunPersister(tmp_path)
    p.reset_state_dirs()
    sequence = iter(
        [
            ("", "", -1, "timeout", "fake"),
            ("review body", "", 0, "ok", "fake"),
        ]
    )

    def invoke() -> tuple[str, str, int, str, str]:
        return next(sequence)

    review, stderr, rc, status, attempts, used_backend = chunked._run_with_retry(invoke, "j1", "fake", p)

    assert review == "review body"
    assert status == "ok"
    assert attempts == 2
    assert used_backend == "fake"
    jsonl = (tmp_path / ".review" / "state" / "retries.jsonl").read_text().splitlines()
    assert len(jsonl) == 2
    assert json.loads(jsonl[0])["outcome"] == "timeout"
    assert json.loads(jsonl[1])["outcome"] == "ok"


def test_run_with_retry_empty_stdout_eligible_for_retry(tmp_path: Path) -> None:
    """rc=0 but empty stdout must trigger retry (closes the C6 hole)."""
    p = chunked._RunPersister(tmp_path)
    p.reset_state_dirs()
    calls = {"n": 0}

    def invoke() -> tuple[str, str, int, str, str]:
        calls["n"] += 1
        return ("", "", 0, "empty_stdout", "fake")

    review, stderr, rc, status, attempts, used_backend = chunked._run_with_retry(invoke, "j1", "fake", p)

    assert calls["n"] == 2
    assert status == "empty_stdout"
    assert attempts == 2


def test_run_with_retry_does_not_retry_real_review_failure(tmp_path: Path) -> None:
    """rc != 0 with non-empty output is a real review failure — do NOT retry."""
    p = chunked._RunPersister(tmp_path)
    p.reset_state_dirs()
    calls = {"n": 0}

    def invoke() -> tuple[str, str, int, str, str]:
        calls["n"] += 1
        return ("partial output", "stderr", 1, "error_rc", "fake")

    review, stderr, rc, status, attempts, used_backend = chunked._run_with_retry(invoke, "j1", "fake", p)

    assert calls["n"] == 1
    assert attempts == 1
    assert status == "error_rc"


def test_run_chunked_review_persists_state_files_on_success(tmp_path: Path) -> None:
    """After a happy-path run, every state/ checkpoint must exist on disk."""
    diff = _diff_for_new_file("a.py", ["x"])
    manifest = textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: all
        """
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        if cfg.backend == "fake":
            return "- [CRITICAL] a.py:1 — `x = 1` — bug\n", "", 0
        return ("[CLUSTER C1] c1-fake-F1\n[UPHELD] C1\nSummary:\nChunked:\n", "", 0)

    def fake_read_text(path: Path) -> str:
        if path.name in ("chunk_review.md", "arbiter_multi.md"):
            return "system prompt"
        return ""

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("validators.manifest._default_git_runner", side_effect=runner),
        patch("chunked.CHUNKED_BACKENDS", [RunnerConfig("fake", "m")]),
        patch("chunked.config.ALLOWED_LENSES", frozenset()),
        patch("chunked._read_text", side_effect=fake_read_text),
    ):
        chunked.run_chunked_review(diff, "a.py", tmp_path)

    state = tmp_path / ".review" / "state"
    for fname in (
        "00_validation.json",
        "01_jobs_plan.json",
        "02_jobs_complete.json",
        "03_arbiter_input.txt",
        "04_arbiter_meta.json",
        "05_clusters.json",
    ):
        assert (state / fname).exists(), f"missing {fname}"
    raw_files = sorted((tmp_path / ".review" / "raw").iterdir())
    assert any(f.suffix == ".txt" for f in raw_files)
    assert any(f.suffix == ".json" for f in raw_files)


def test_run_chunked_review_persists_crash_log_on_unexpected_exception(tmp_path: Path) -> None:
    """If something raises mid-pipeline, crash.log must capture the traceback."""
    diff = _diff_for_new_file("a.py", ["x"])
    manifest = textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: all
        """
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    def fake_read_text(path: Path) -> str:
        if path.name in ("chunk_review.md", "arbiter_multi.md"):
            return "system prompt"
        return ""

    def boom(*a: object, **kw: object) -> tuple[str, str, int]:
        raise RuntimeError("synthetic explosion")

    with (
        patch("hook.run_reviewer", side_effect=boom),
        patch("validators.manifest._default_git_runner", side_effect=runner),
        patch("chunked.CHUNKED_BACKENDS", [RunnerConfig("fake", "m")]),
        patch("chunked.config.ALLOWED_LENSES", frozenset()),
        patch("chunked._read_text", side_effect=fake_read_text),
    ):
        try:
            chunked.run_chunked_review(diff, "a.py", tmp_path)
        except RuntimeError:
            pass  # expected — pipeline re-raises after persisting the crash

    crash_log = tmp_path / ".review" / "state" / "crash.log"
    assert crash_log.exists()
    body = crash_log.read_text()
    assert "RuntimeError" in body
    assert "synthetic explosion" in body
    # Validation already passed before the crash — should be persisted.
    assert (tmp_path / ".review" / "state" / "00_validation.json").exists()


# ---------------------------------------------------------------------------
# C2 — binary file in related_files must not crash prompt assembly
# ---------------------------------------------------------------------------


def test_read_text_binary_file_returns_empty(tmp_path: Path) -> None:
    """C2: binary/non-UTF8 files in related_files must not crash prompt assembly."""
    binary_path = tmp_path / "binary.bin"
    binary_path.write_bytes(bytes(range(128, 256)))
    assert chunked._read_text(binary_path) == ""


# ---------------------------------------------------------------------------
# C3 — all-failed run with zero findings must block
# ---------------------------------------------------------------------------


def test_all_error_empty_output_is_infrastructure_failure(tmp_path: Path) -> None:
    """C3: every reviewer rc≠0 with empty output → infrastructure_failure."""
    diff = _diff_for_new_file("a.py", ["x"])
    manifest = textwrap.dedent(
        f"""\
        version: 1
        diff_hash: {_hash(diff)}
        chunks:
          - id: c1
            files:
              - path: a.py
                line_ranges: all
        """
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "manifest.yaml").write_text(manifest)
    runner = _runner_factory("A\ta.py\n", "1\t0\ta.py\n")

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        return "", "stderr msg", 1

    def fake_read_text(path: Path) -> str:
        if path.name == "chunk_review.md":
            return "system prompt"
        return ""

    with (
        patch("hook.run_reviewer", side_effect=fake_run_reviewer),
        patch("validators.manifest._default_git_runner", side_effect=runner),
        patch("chunked.CHUNKED_BACKENDS", [RunnerConfig("fake", "m")]),
        patch("chunked.config.ALLOWED_LENSES", frozenset()),
        patch("chunked._read_text", side_effect=fake_read_text),
    ):
        result = chunked.run_chunked_review(diff, "a.py", tmp_path)

    assert result.status == "infrastructure_failure"
    assert len(result.upheld_clusters) == 1
    assert result.upheld_clusters[0].cluster_id == "INFRA1"


# ---------------------------------------------------------------------------
# C4 — rc≠0 + empty stdout branch
# ---------------------------------------------------------------------------


def test_run_chunk_job_rc_nonzero_empty_stdout_maps_to_error() -> None:
    """C4: rc≠0 with empty stdout produces status='error' and zero findings."""
    chunk = {"id": "c1", "files": [{"path": "a.py", "line_ranges": "all"}], "review_lenses": ["bugs"]}
    backend_cfg = RunnerConfig("fake", "m")

    def fake_run_reviewer(cfg: RunnerConfig, sys_p: str, usr_p: str) -> tuple[str, str, int]:
        return "", "stderr", 2

    with patch("hook.run_reviewer", side_effect=fake_run_reviewer):
        jr = chunked._run_chunk_job(chunk, backend_cfg, "system", "cached", Path("/tmp"))

    assert jr.rc == 2
    assert jr.status == "error"
    assert jr.findings == []
    assert jr.raw_text == ""

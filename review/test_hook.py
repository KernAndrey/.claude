from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backends import (
    BACKENDS,
    Backend,
    ClaudeBackend,
    CodexBackend,
    KimiBackend,
    OpencodeBackend,
    _build_registry,
)
from config import CHUNKED_BACKENDS, FALLBACK, FANOUT_THRESHOLD, MAX_PROD_LINES, PRIMARIES, RunnerConfig
from hook import (
    ARBITER,
    LENS_APPLICABILITY,
    LENS_NAMES,
    _aggregate_lens_outputs,
    _check_staged_review_guard,
    _render_fanout_output,
    _run_chunked_path,
    _run_single_call,
    _verify_runner_configs,
    applicable_lenses,
    assign_finding_ids,
    build_user_prompt,
    check_diff_size,
    count_added_production_lines,
    count_criticals,
    extract_warning_lines,
    is_production_code,
    is_test_file,
    is_well_formed,
    parse_arbiter_verdict,
    parse_verdict,
    run_arbiter,
    run_review,
    run_reviewer,
    run_single_lens,
    run_with_fallback,
)


def _pair() -> tuple[RunnerConfig, RunnerConfig]:
    """Shared primary/fallback pair for fallback-routing tests."""
    return RunnerConfig("opencode", "p"), RunnerConfig("claude", "f")


# ---------------------------------------------------------------------------
# RunnerConfig dispatch — run_reviewer / run_with_fallback
# ---------------------------------------------------------------------------


def test_run_reviewer_dispatches_opencode() -> None:
    cfg = RunnerConfig("opencode", "model-x", timeout=42)
    with patch.object(BACKENDS["opencode"], "run", return_value=("out", "err", 0)) as m:
        result = run_reviewer(cfg, "sys", "user")
    m.assert_called_once_with("sys", "user", "model-x", 42)
    assert result == ("out", "err", 0)


def test_run_reviewer_dispatches_claude() -> None:
    cfg = RunnerConfig("claude", "sonnet", timeout=99)
    with patch.object(BACKENDS["claude"], "run", return_value=("out", "err", 0)) as m:
        result = run_reviewer(cfg, "sys", "user")
    m.assert_called_once_with("sys", "user", "sonnet", 99)
    assert result == ("out", "err", 0)


def test_run_reviewer_unknown_backend_raises_value_error() -> None:
    cfg = RunnerConfig("bogus", "x")
    try:
        run_reviewer(cfg, "s", "u")
    except ValueError as exc:
        assert "bogus" in str(exc)
        return
    raise AssertionError("expected ValueError for unknown backend")


def test_run_with_fallback_returns_primary_on_success() -> None:
    primary, fallback = _pair()
    with patch("hook.run_reviewer", return_value=("primary out", "", 0)) as m:
        review, stderr, rc, used = run_with_fallback(primary, fallback, "s", "u")
    assert m.call_count == 1
    assert m.call_args[0][0] is primary
    assert (review, stderr, rc, used) == ("primary out", "", 0, "opencode")


def test_run_with_fallback_falls_back_on_nonzero_rc() -> None:
    primary, fallback = _pair()
    seen: list[RunnerConfig] = []

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        seen.append(cfg)
        return ("", "boom", 1) if cfg is primary else ("fallback out", "", 0)

    with patch("hook.run_reviewer", side_effect=fake):
        review, _, _, used = run_with_fallback(primary, fallback, "s", "u")
    assert [c is primary for c in seen] == [True, False]
    assert (review, used) == ("fallback out", "claude")


def test_run_with_fallback_falls_back_on_empty_review() -> None:
    primary, fallback = _pair()

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        return ("   \n  ", "", 0) if cfg is primary else ("ok", "", 0)

    with patch("hook.run_reviewer", side_effect=fake):
        review, _, _, used = run_with_fallback(primary, fallback, "s", "u")
    assert (review, used) == ("ok", "claude")


def test_run_with_fallback_falls_back_on_timeout() -> None:
    primary, fallback = _pair()

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        if cfg is primary:
            raise subprocess.TimeoutExpired(cmd="opencode", timeout=1)
        return ("ok", "", 0)

    with patch("hook.run_reviewer", side_effect=fake):
        review, _, _, used = run_with_fallback(primary, fallback, "s", "u")
    assert (review, used) == ("ok", "claude")


def test_run_with_fallback_falls_back_on_filenotfound() -> None:
    primary, fallback = _pair()

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        if cfg is primary:
            raise FileNotFoundError("opencode binary missing")
        return ("ok", "", 0)

    with patch("hook.run_reviewer", side_effect=fake):
        review, _, _, used = run_with_fallback(primary, fallback, "s", "u")
    assert (review, used) == ("ok", "claude")


def test_run_with_fallback_falls_back_on_oserror() -> None:
    """OSError covers exec/permission errors broader than FileNotFoundError."""
    primary, fallback = _pair()

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        if cfg is primary:
            raise PermissionError("opencode not executable")  # subclass of OSError
        return ("ok", "", 0)

    with patch("hook.run_reviewer", side_effect=fake):
        review, _, _, used = run_with_fallback(primary, fallback, "s", "u")
    assert (review, used) == ("ok", "claude")


def test_run_with_fallback_no_fallback_returns_primary_failure() -> None:
    primary = RunnerConfig("opencode", "p")
    with patch("hook.run_reviewer", return_value=("", "boom", 1)) as m:
        review, stderr, rc, used = run_with_fallback(primary, None, "s", "u")
    assert m.call_count == 1
    assert (review, stderr, rc, used) == ("", "boom", 1, "opencode")


def test_run_with_fallback_no_fallback_reraises_timeout() -> None:
    primary = RunnerConfig("opencode", "p")

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=1)

    with patch("hook.run_reviewer", side_effect=fake):
        try:
            run_with_fallback(primary, None, "s", "u")
        except subprocess.TimeoutExpired:
            return
    raise AssertionError("expected TimeoutExpired re-raise when fallback is None")


# ---------------------------------------------------------------------------
# RunnerConfig validation — _verify_runner_configs
# ---------------------------------------------------------------------------


def test_verify_runner_configs_passes_on_default_config() -> None:
    _verify_runner_configs()  # default PRIMARY/FALLBACK/ARBITER are valid


def test_verify_runner_configs_raises_on_invalid_primary_backend() -> None:
    bogus = RunnerConfig("bogus-backend", "x")
    with patch("hook.PRIMARIES", [bogus]):
        try:
            _verify_runner_configs()
        except ValueError as exc:
            assert "PRIMARIES" in str(exc)
            assert "bogus-backend" in str(exc)
            return
    raise AssertionError("expected ValueError on invalid PRIMARIES entry")


def test_verify_runner_configs_raises_on_empty_primaries() -> None:
    """An empty PRIMARIES list is a misconfiguration (no reviewers to run)."""
    with patch("hook.PRIMARIES", []):
        try:
            _verify_runner_configs()
        except ValueError as exc:
            assert "PRIMARIES" in str(exc)
            return
    raise AssertionError("expected ValueError on empty PRIMARIES")


def test_verify_runner_configs_raises_on_duplicate_primaries() -> None:
    """Two PRIMARIES with the same (backend, model) is always a typo."""
    dup = RunnerConfig("opencode", "github-copilot/gpt-5.4")
    with patch("hook.PRIMARIES", [dup, dup]):
        try:
            _verify_runner_configs()
        except ValueError as exc:
            assert "duplicate" in str(exc).lower()
            return
    raise AssertionError("expected ValueError on duplicate PRIMARIES entry")


def test_verify_runner_configs_raises_on_invalid_fallback_backend() -> None:
    bogus = RunnerConfig("typo", "x")
    with patch("hook.FALLBACK", bogus):
        try:
            _verify_runner_configs()
        except ValueError as exc:
            assert "FALLBACK" in str(exc)
            return
    raise AssertionError("expected ValueError on invalid FALLBACK backend")


def test_verify_runner_configs_raises_on_invalid_arbiter_backend() -> None:
    bogus = RunnerConfig("nope", "x")
    with patch("hook.ARBITER", bogus):
        try:
            _verify_runner_configs()
        except ValueError as exc:
            assert "ARBITER" in str(exc)
            return
    raise AssertionError("expected ValueError on invalid ARBITER backend")


def test_verify_runner_configs_allows_none_fallback() -> None:
    """FALLBACK = None is a supported configuration (no fallback)."""
    with patch("hook.FALLBACK", None):
        _verify_runner_configs()  # must not raise


def test_verify_runner_configs_raises_on_invalid_chunked_backend() -> None:
    """A typo in CHUNKED_BACKENDS must be caught at startup, not when a large
    commit finally takes the chunked path."""
    bogus = RunnerConfig("typo-backend", "x")
    with patch("hook.CHUNKED_BACKENDS", [bogus]):
        try:
            _verify_runner_configs()
        except ValueError as exc:
            assert "CHUNKED_BACKENDS" in str(exc)
            assert "typo-backend" in str(exc)
            return
    raise AssertionError("expected ValueError on invalid CHUNKED_BACKENDS entry")


def test_lens_names_derived_from_lens_applicability() -> None:
    """LENS_NAMES is now derived from the registry — drift is structurally
    impossible. Smoke check that the derivation produced a non-empty tuple
    in the expected order."""
    assert tuple(LENS_APPLICABILITY) == LENS_NAMES
    assert len(LENS_NAMES) >= 1


# ---------------------------------------------------------------------------
# Backend registry contract — adding a new backend is single-touch-point
# ---------------------------------------------------------------------------


def test_backends_registry_keys_match_class_names() -> None:
    """Registry keys must equal each backend's declared name attribute."""
    for key, backend in BACKENDS.items():
        assert key == backend.name


def test_backends_registry_contains_default_backends() -> None:
    """opencode and claude are always present out of the box."""
    assert "opencode" in BACKENDS
    assert "claude" in BACKENDS
    assert isinstance(BACKENDS["opencode"], OpencodeBackend)
    assert isinstance(BACKENDS["claude"], ClaudeBackend)


def test_backend_subclass_must_implement_run() -> None:
    """ABC blocks instantiation of incomplete backends — protects against
    a future Backend subclass forgetting to implement run()."""

    class Incomplete(Backend):
        name = "incomplete"

    try:
        Incomplete()  # type: ignore[abstract]
    except TypeError:
        return
    raise AssertionError("expected TypeError on Backend subclass missing run()")


def test_build_registry_rejects_duplicate_backend_names() -> None:
    """A copy-paste mistake that forgets to rename ``name`` must fail at
    import time, not silently shadow an existing backend.

    Without this guard, ``BACKENDS`` last-write-wins on the dict
    comprehension, ``_verify_runner_configs`` still accepts the key,
    and ``run_reviewer`` dispatches the surviving backend with no
    startup error.
    """

    class FirstClaude(Backend):
        name = "claude"

        def run(
            self,
            system_prompt: str,
            user_prompt: str,
            model: str,
            timeout: int,
        ) -> tuple[str, str, int]:
            return "first", "", 0

    class SecondClaude(Backend):
        name = "claude"

        def run(
            self,
            system_prompt: str,
            user_prompt: str,
            model: str,
            timeout: int,
        ) -> tuple[str, str, int]:
            return "second", "", 0

    try:
        _build_registry(FirstClaude(), SecondClaude())
    except ValueError as exc:
        msg = str(exc)
        assert "claude" in msg
        assert "FirstClaude" in msg
        assert "SecondClaude" in msg
        return
    raise AssertionError("expected ValueError on duplicate backend name")


def test_build_registry_accepts_distinct_names() -> None:
    """Smoke check: distinct names build a registry with both entries."""

    class Foo(Backend):
        name = "foo"

        def run(
            self,
            system_prompt: str,
            user_prompt: str,
            model: str,
            timeout: int,
        ) -> tuple[str, str, int]:
            return "foo-out", "", 0

    class Bar(Backend):
        name = "bar"

        def run(
            self,
            system_prompt: str,
            user_prompt: str,
            model: str,
            timeout: int,
        ) -> tuple[str, str, int]:
            return "bar-out", "", 0

    foo, bar = Foo(), Bar()
    registry = _build_registry(foo, bar)
    assert registry == {"foo": foo, "bar": bar}


def test_run_reviewer_dispatches_to_custom_backend() -> None:
    """A new Backend subclass + registry entry is the entire recipe.

    Proves that run_reviewer routes through BACKENDS without any
    backend-name hardcoding — adding 'codex' or 'kimi' would work the
    same way.
    """
    calls: list[tuple[str, str, str, int]] = []

    class FakeBackend(Backend):
        name = "fake"

        def run(
            self,
            system_prompt: str,
            user_prompt: str,
            model: str,
            timeout: int,
        ) -> tuple[str, str, int]:
            calls.append((system_prompt, user_prompt, model, timeout))
            return "out", "", 0

    fake = FakeBackend()
    with patch.dict("hook.BACKENDS", {"fake": fake}):
        cfg = RunnerConfig("fake", "fake-model", timeout=42)
        out, err, rc = run_reviewer(cfg, "sys", "usr")
    assert (out, err, rc) == ("out", "", 0)
    assert calls == [("sys", "usr", "fake-model", 42)]


def test_verify_runner_configs_error_lists_registered_backends() -> None:
    """Error message must enumerate keys from BACKENDS, not a hardcoded set.

    Defends against a future revert from BACKENDS-driven validation back
    to a frozen literal set that drifts from the real registry.
    """
    bogus = RunnerConfig("ghost", "x")
    with patch("hook.PRIMARIES", [bogus]):
        try:
            _verify_runner_configs()
        except ValueError as exc:
            msg = str(exc)
            assert "ghost" in msg
            assert "must be one of" in msg
            for name in BACKENDS:
                assert name in msg, f"registered backend {name!r} missing from error message"
            return
    raise AssertionError("expected ValueError when PRIMARIES entry not in BACKENDS")


# ---------------------------------------------------------------------------
# Backend-runner contract — model + timeout propagation
# ---------------------------------------------------------------------------


def test_opencode_backend_forwards_timeout() -> None:
    """timeout from RunnerConfig must reach subprocess.run(..., timeout=)."""
    mock_result = MagicMock()
    mock_result.stdout = '{"type":"text","part":{"type":"text","text":"out"}}\n'
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("backends.subprocess.run", return_value=mock_result) as mock_run:
        OpencodeBackend().run("sys", "user", "model-x", 777)

    assert mock_run.call_args.kwargs["timeout"] == 777


def test_opencode_backend_pins_pre_commit_reviewer_agent() -> None:
    """The opencode invocation must pin --agent pre-commit-reviewer so the
    LLM cannot reach bash/edit/write tools and silently mutate the index
    during diff investigation. Pairs with the index snapshot/restore in
    ~/.claude/git-hooks/pre-commit — losing this pin widens the blast
    radius the snapshot has to roll back."""
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("backends.subprocess.run", return_value=mock_result) as mock_run:
        OpencodeBackend().run("sys", "user", "model-x", 60)

    cmd = mock_run.call_args[0][0]
    agent_idx = cmd.index("--agent")
    assert cmd[agent_idx + 1] == "pre-commit-reviewer"


def test_claude_backend_builds_correct_command() -> None:
    mock_result = MagicMock()
    mock_result.stdout = "claude review body"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("backends.subprocess.run", return_value=mock_result) as mock_run:
        stdout, stderr, rc = ClaudeBackend().run("sys", "user", "sonnet", 600)

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--model" in cmd
    assert "sonnet" in cmd
    assert "--system-prompt" in cmd
    assert "sys" in cmd  # system prompt value follows the flag
    assert mock_run.call_args.kwargs["input"] == "user"
    assert mock_run.call_args.kwargs["timeout"] == 600
    assert (stdout, stderr, rc) == ("claude review body", "", 0)


def test_claude_backend_omits_system_prompt_flag_when_empty() -> None:
    mock_result = MagicMock()
    mock_result.stdout = "ok"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("backends.subprocess.run", return_value=mock_result) as mock_run:
        ClaudeBackend().run("", "user", "sonnet", 600)

    cmd = mock_run.call_args[0][0]
    assert "--system-prompt" not in cmd


def test_claude_backend_forwards_timeout() -> None:
    mock_result = MagicMock()
    mock_result.stdout = "ok"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("backends.subprocess.run", return_value=mock_result) as mock_run:
        ClaudeBackend().run("sys", "user", "sonnet", 333)

    assert mock_run.call_args.kwargs["timeout"] == 333


def _capture_kimi_invocation(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> tuple[dict, AbstractContextManager[MagicMock]]:
    """Patch backends.subprocess.run and capture the real kimi-code invocation.

    KimiBackend stages the full prompt in a temp file and hands kimi a short
    ``-p`` instruction to Read it, then unlinks the file in ``finally``. The
    mock's side_effect runs *during* subprocess.run — before that unlink — so it
    reads the staged prompt back out. Returns ``(captured, ctx)`` where
    ``captured`` gains keys ``cmd`` / ``kwargs`` / ``prompt`` once the ``with``
    block runs.
    """
    captured: dict = {}

    def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        instruction = cmd[cmd.index("-p") + 1]
        path = instruction.split("Read the file ", 1)[1].split(" in full", 1)[0]
        captured["prompt"] = Path(path).read_text(encoding="utf-8")
        result = MagicMock()
        result.stdout = stdout
        result.stderr = stderr
        result.returncode = returncode
        return result

    return captured, patch("backends.subprocess.run", side_effect=side_effect)


def test_kimi_backend_builds_kimi_code_command() -> None:
    captured, ctx = _capture_kimi_invocation(stdout='{"role":"assistant","content":"body"}')
    with ctx:
        stdout, stderr, rc = KimiBackend().run("sys", "user", "kimi-code/k3", 600)

    cmd = captured["cmd"]
    assert cmd[0] == "kimi"
    assert cmd[cmd.index("--model") + 1] == "kimi-code/k3"
    # stream-json (not text): clean line-delimited JSON we parse deterministically.
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "-p" in cmd
    # The dead kimi-cli flags must never come back — kimi-code rejects them and
    # the review silently fell back to Sonnet for weeks when they were present.
    for dead in ("--quiet", "--agent-file", "--input-format"):
        assert dead not in cmd
    # Read-only containment is armed for THIS run only via the env flag that
    # review_readonly.py keys on — replaces the removed --agent-file allowlist.
    assert captured["kwargs"]["env"]["KIMI_REVIEW_READONLY"] == "1"
    # The full "<system>\n\n<user>" prompt is staged verbatim in the temp file
    # kimi is told to Read (argv would blow past MAX_ARG_STRLEN on big diffs).
    assert captured["prompt"] == "sys\n\nuser"
    assert captured["kwargs"]["timeout"] == 600
    # stdout is parsed out of the stream-json envelope.
    assert (stdout, stderr, rc) == ("body", "", 0)


def test_kimi_backend_parses_stream_json() -> None:
    """_parse_stream_json keeps assistant text, drops tool_calls / tool / meta."""
    stream = "\n".join(
        [
            '{"role":"assistant","tool_calls":[{"type":"function","id":"t1","function":{"name":"Read","arguments":"{}"}}]}',
            '{"role":"tool","tool_call_id":"t1","content":"raw file body — must be dropped"}',
            '{"role":"assistant","content":"- [CRITICAL] a.py:1 — boom"}',
            '{"role":"assistant","content":"Summary: 1 critical"}',
            '{"role":"meta","type":"session.resume_hint","content":"To resume: kimi -r x"}',
        ]
    )
    assert KimiBackend._parse_stream_json(stream) == "- [CRITICAL] a.py:1 — boom\nSummary: 1 critical"


def test_kimi_backend_empty_system_prompt_stages_user_only() -> None:
    """With an empty system_prompt, only the user prompt is staged (no preamble
    / leading-separator artefact) — mirrors OpencodeBackend's branch."""
    captured, ctx = _capture_kimi_invocation(stdout='{"role":"assistant","content":"ok"}')
    with ctx:
        KimiBackend().run("", "user", "kimi-code/k3", 60)

    assert captured["prompt"] == "user"


def test_kimi_backend_forwards_timeout() -> None:
    captured, ctx = _capture_kimi_invocation(stdout='{"role":"assistant","content":"ok"}')
    with ctx:
        KimiBackend().run("sys", "user", "kimi-code/k3", 333)

    assert captured["kwargs"]["timeout"] == 333


def _capture_codex_invocation(
    last_message: str | None = "review body",
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> tuple[dict, AbstractContextManager[MagicMock]]:
    """Patch backends.subprocess.run and capture the real codex invocation.

    Mirror image of :func:`_capture_kimi_invocation`. Kimi stages its prompt in a
    temp file and the helper *reads* it inside ``side_effect``; codex is the other
    direction — the backend passes an empty ``--output-last-message`` path and reads
    it back *after* ``subprocess.run`` returns, unlinking it in ``finally``. So this
    ``side_effect`` must **write** that file, standing in for the codex CLI. Writing
    it anywhere else (or not at all) makes every parse assertion see an empty file
    and pass for the wrong reason.

    ``last_message=None`` simulates codex never writing the file at all (crash,
    auth failure) as distinct from writing an empty one.

    Also pins the sandbox verdict to healthy: ``run`` calls ``selfcheck`` first
    and would otherwise fail fast without ever building the argv these tests
    assert on. Tests that want the broken path use ``_codex_sandbox(False)``.
    """
    captured: dict = {}

    def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        out_path = cmd[cmd.index("--output-last-message") + 1]
        captured["out_path"] = out_path
        if last_message is not None:
            Path(out_path).write_text(last_message, encoding="utf-8")
        else:
            Path(out_path).unlink(missing_ok=True)
        result = MagicMock()
        result.stdout = stdout
        result.stderr = stderr
        result.returncode = returncode
        return result

    @contextmanager
    def ctx() -> Iterator[MagicMock]:
        with (
            _codex_sandbox(healthy=True),
            patch("backends.subprocess.run", side_effect=side_effect) as mock_run,
        ):
            yield mock_run

    return captured, ctx()


def test_codex_backend_builds_exec_command() -> None:
    captured, ctx = _capture_codex_invocation(last_message="body")
    with ctx:
        stdout, stderr, rc = CodexBackend().run("sys", "user", "gpt-5.6-terra", 600)

    cmd = captured["cmd"]
    assert cmd[0] == "codex"
    assert cmd[1] == "exec"
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-terra"
    assert "--output-last-message" in cmd
    # No positional prompt: `codex exec` reads instructions from stdin when none is
    # given. A literal "-" would risk being taken as the prompt itself, with the
    # diff demoted to an appended <stdin> block — a silently garbage review.
    assert "-" not in cmd
    # Prompt travels via stdin, never argv (chunked prompts blow past ARG_MAX).
    assert captured["kwargs"]["input"] == "sys\n\nuser"
    assert captured["kwargs"]["timeout"] == 600
    assert (stdout, stderr, rc) == ("body", "", 0)


def test_codex_backend_pins_read_only_sandbox() -> None:
    """The codex invocation must pin --sandbox read-only so the reviewer cannot
    write to the tree or reach the network while investigating the diff. This is
    codex's native equivalent of opencode's --agent pin and kimi's
    KIMI_REVIEW_READONLY deny-hook, and it pairs with the index snapshot/restore
    in ~/.claude/git-hooks/pre-commit — losing it widens the blast radius the
    snapshot has to roll back."""
    captured, ctx = _capture_codex_invocation()
    with ctx:
        CodexBackend().run("sys", "user", "gpt-5.6-terra", 60)

    cmd = captured["cmd"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    # Never silently escalate out of the sandbox.
    for dangerous in ("--dangerously-bypass-approvals-and-sandbox", "--approve-for-me"):
        assert dangerous not in cmd


def test_codex_run_sends_exactly_the_probed_feature_flags() -> None:
    """`run` must send the same `--enable` flags `selfcheck` probes, and no others.

    Drift here is the subtle killer: the probe would certify a sandbox
    configuration the review never uses, so a green probe would stop meaning
    anything. Bites even while `_SANDBOX_FEATURE_ARGV` is empty — it then asserts
    `run` passes no feature flags at all, which is the current contract (the
    AppArmor profile makes the default bwrap sandbox work, and re-adding the
    deprecated landlock flag would hard-error on a future codex release).
    """
    captured, ctx = _capture_codex_invocation()
    with ctx:
        CodexBackend().run("sys", "user", "gpt-5.6-terra", 60)

    def enabled_features(argv: list[str] | tuple[str, ...]) -> list[str]:
        return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--enable"]

    assert enabled_features(captured["cmd"]) == enabled_features(CodexBackend._SANDBOX_FEATURE_ARGV)


@contextmanager
def _codex_sandbox(healthy: bool = True) -> Iterator[None]:
    """Force CodexBackend's cached sandbox verdict, and always restore it.

    The verdict is process-global by design (one probe per commit), so a test
    that leaves it set would silently change every later test.
    """
    import backends as backends_mod

    saved = backends_mod._CODEX_SANDBOX_CHECK
    backends_mod._CODEX_SANDBOX_CHECK = (None,) if healthy else ("sandbox is dead",)
    try:
        yield
    finally:
        backends_mod._CODEX_SANDBOX_CHECK = saved


def test_codex_selfcheck_probes_the_same_flags_run_uses() -> None:
    """The probe must exercise the flag combination `run` actually sends.

    If the two drifted, a green probe would certify a sandbox the review never
    uses — the failure this whole guard exists to prevent.
    """
    import backends as backends_mod

    mock_result = MagicMock()
    mock_result.stdout = CodexBackend._SELFCHECK_MARKER
    mock_result.stderr = ""
    mock_result.returncode = 0

    saved = backends_mod._CODEX_SANDBOX_CHECK
    backends_mod._CODEX_SANDBOX_CHECK = None
    try:
        with patch("backends.subprocess.run", return_value=mock_result) as mock_run:
            assert CodexBackend().selfcheck() is None
    finally:
        backends_mod._CODEX_SANDBOX_CHECK = saved

    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["codex", "sandbox"]
    for flag in CodexBackend._SANDBOX_FEATURE_ARGV:
        assert flag in cmd


def test_codex_selfcheck_rejects_rc0_without_marker() -> None:
    """rc==0 alone is not proof: the marker must reach stdout, which is what
    shows the sandboxed command really executed."""
    import backends as backends_mod

    mock_result = MagicMock()
    mock_result.stdout = ""  # command never ran
    mock_result.stderr = "bwrap: setting up uid map: Permission denied"
    mock_result.returncode = 0

    saved = backends_mod._CODEX_SANDBOX_CHECK
    backends_mod._CODEX_SANDBOX_CHECK = None
    try:
        with patch("backends.subprocess.run", return_value=mock_result):
            reason = CodexBackend().selfcheck()
    finally:
        backends_mod._CODEX_SANDBOX_CHECK = saved

    assert reason is not None
    assert "bwrap" in reason
    # orchestrator._invoke_single_call logs only stderr[:200]; the actionable fix must
    # survive that cut, so it sits at the front and the raw detail trails.
    assert "/etc/apparmor.d/codex-bwrap" in reason[:200]


def test_codex_selfcheck_caches_across_calls_and_instances() -> None:
    """One probe per process — the chunked path calls run() from many threads."""
    import backends as backends_mod

    mock_result = MagicMock()
    mock_result.stdout = CodexBackend._SELFCHECK_MARKER
    mock_result.stderr = ""
    mock_result.returncode = 0

    saved = backends_mod._CODEX_SANDBOX_CHECK
    backends_mod._CODEX_SANDBOX_CHECK = None
    try:
        with patch("backends.subprocess.run", return_value=mock_result) as mock_run:
            CodexBackend().selfcheck()
            CodexBackend().selfcheck()
            CodexBackend().selfcheck()
    finally:
        backends_mod._CODEX_SANDBOX_CHECK = saved

    assert mock_run.call_count == 1


def test_codex_selfcheck_reports_probe_timeout() -> None:
    """A hung probe must degrade to a reason string, not propagate.

    `selfcheck` is called from `run`, which promises never to raise on
    environment trouble — an escaping TimeoutExpired would surface as a review
    crash instead of a clean fallback to FALLBACK.
    """
    import backends as backends_mod

    saved = backends_mod._CODEX_SANDBOX_CHECK
    backends_mod._CODEX_SANDBOX_CHECK = None
    try:
        with patch(
            "backends.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["codex", "sandbox"], timeout=30),
        ):
            reason = CodexBackend().selfcheck()
    finally:
        backends_mod._CODEX_SANDBOX_CHECK = saved

    assert reason is not None
    assert "TimeoutExpired" in reason


def test_codex_selfcheck_reports_os_error() -> None:
    """Any OSError from the probe (permissions, ENOMEM, fork failure) becomes a
    reason string rather than escaping into the review pipeline."""
    import backends as backends_mod

    saved = backends_mod._CODEX_SANDBOX_CHECK
    backends_mod._CODEX_SANDBOX_CHECK = None
    try:
        with patch("backends.subprocess.run", side_effect=OSError("cannot fork")):
            reason = CodexBackend().selfcheck()
    finally:
        backends_mod._CODEX_SANDBOX_CHECK = saved

    assert reason is not None
    assert "cannot fork" in reason


def test_warn_on_unhealthy_backends_skips_unregistered_backend() -> None:
    """A backend name that is not in BACKENDS must be skipped silently here.

    `_verify_runner_configs` already raises a named error for that case; warning
    about it twice would just add noise, and looking up `None.selfcheck()` would
    crash the hook before the real review ever starts.
    """
    import hook

    messages: list[str] = []
    with (
        patch("hook.PRIMARIES", [RunnerConfig("ghost", "m")]),
        patch("hook.CHUNKED_BACKENDS", []),
        patch("hook.FALLBACK", None),
        patch("hook.ARBITER", None),
        patch("hook.warn", side_effect=messages.append),
    ):
        hook._warn_on_unhealthy_backends()  # must not raise

    assert messages == []


def test_codex_selfcheck_reports_missing_cli() -> None:
    import backends as backends_mod

    saved = backends_mod._CODEX_SANDBOX_CHECK
    backends_mod._CODEX_SANDBOX_CHECK = None
    try:
        with patch("backends.subprocess.run", side_effect=FileNotFoundError):
            reason = CodexBackend().selfcheck()
    finally:
        backends_mod._CODEX_SANDBOX_CHECK = saved

    assert reason is not None
    assert "not found" in reason


def test_codex_run_fails_fast_when_sandbox_broken() -> None:
    """A dead sandbox must fail the run so FALLBACK produces a real review.

    Returning empty review + rc!=0 is precisely what run_with_fallback keys on.
    The alternative — letting codex answer blind — yields a confident "0
    findings" that passes the gate.
    """
    with _codex_sandbox(healthy=False), patch("backends.subprocess.run") as mock_run:
        review, stderr, rc = CodexBackend().run("sys", "user", "gpt-5.6-terra", 60)

    assert (review, rc) == ("", 1)
    assert "sandbox is dead" in stderr
    mock_run.assert_not_called()  # never spend an API call on a blind review


def test_warn_on_unhealthy_backends_names_the_backend() -> None:
    """The early warn must name which backend is unhealthy and why."""
    import hook

    class SickBackend(Backend):
        name = "sick"

        def run(self, system_prompt: str, user_prompt: str, model: str, timeout: int) -> tuple[str, str, int]:
            return "", "", 0

        def selfcheck(self) -> str | None:
            return "sandbox cannot start"

    messages: list[str] = []
    with (
        patch.dict("hook.BACKENDS", {"sick": SickBackend()}),
        patch("hook.PRIMARIES", [RunnerConfig("sick", "m")]),
        patch("hook.CHUNKED_BACKENDS", []),
        patch("hook.FALLBACK", None),
        patch("hook.ARBITER", None),
        patch("hook.warn", side_effect=messages.append),
    ):
        hook._warn_on_unhealthy_backends()

    assert any("sick" in m and "sandbox cannot start" in m for m in messages)


def test_warn_on_unhealthy_backends_silent_when_all_healthy() -> None:
    """Default backends implement no selfcheck — no spurious warn, no probe."""
    import hook

    messages: list[str] = []
    with (
        patch("hook.PRIMARIES", [RunnerConfig("claude", "sonnet")]),
        patch("hook.CHUNKED_BACKENDS", []),
        patch("hook.FALLBACK", None),
        patch("hook.ARBITER", None),
        patch("hook.warn", side_effect=messages.append),
    ):
        hook._warn_on_unhealthy_backends()

    assert messages == []


def test_codex_backend_reads_review_from_last_message_file() -> None:
    """Review text comes from --output-last-message only; the JSONL event stream
    on stdout must never leak into the reviewer-text contract."""
    noise = '{"type":"item.completed","item":{"type":"reasoning","text":"thinking out loud"}}'
    _captured, ctx = _capture_codex_invocation(
        last_message="- [CRITICAL] a.py:1 — boom\nSummary: 1 critical",
        stdout=noise,
    )
    with ctx:
        review, _stderr, rc = CodexBackend().run("sys", "user", "gpt-5.6-terra", 60)

    assert review == "- [CRITICAL] a.py:1 — boom\nSummary: 1 critical"
    assert "thinking out loud" not in review
    assert rc == 0


def test_codex_backend_empty_system_prompt_sends_user_only() -> None:
    """With an empty system_prompt, only the user prompt is piped (no leading
    separator artefact) — mirrors the Opencode/Kimi branch."""
    captured, ctx = _capture_codex_invocation()
    with ctx:
        CodexBackend().run("", "user", "gpt-5.6-terra", 60)

    assert captured["kwargs"]["input"] == "user"


def test_codex_backend_forwards_timeout() -> None:
    captured, ctx = _capture_codex_invocation()
    with ctx:
        CodexBackend().run("sys", "user", "gpt-5.6-terra", 333)

    assert captured["kwargs"]["timeout"] == 333


def test_codex_backend_empty_output_file_returns_empty_review_with_stdout_tail() -> None:
    """No review text → empty string (so run_with_fallback fires), and the stdout
    tail is carried into stderr so the logged reason is readable. Codex reports
    quota/auth failures on stdout; without this the fallback reason would be blank."""
    _captured, ctx = _capture_codex_invocation(
        last_message="",
        stdout='{"type":"error","message":"You have hit your usage limit"}',
        stderr="",
        returncode=1,
    )
    with ctx:
        review, stderr, rc = CodexBackend().run("sys", "user", "gpt-5.6-terra", 60)

    assert review == ""
    assert "usage limit" in stderr
    assert rc == 1


def test_codex_backend_missing_output_file_returns_empty_review() -> None:
    """Codex dying before writing the file must degrade to an empty review, not
    an unhandled OSError that escapes the backend."""
    _captured, ctx = _capture_codex_invocation(last_message=None, stdout="", returncode=1)
    with ctx:
        review, _stderr, rc = CodexBackend().run("sys", "user", "gpt-5.6-terra", 60)

    assert review == ""
    assert rc == 1


def test_codex_backend_stdout_tail_is_trimmed() -> None:
    """A huge stdout must not be pasted wholesale into the markdown log.

    Only the stdout portion is bounded — real stderr is appended in full and
    deliberately kept FIRST, because ``orchestrator._invoke_single_call`` logs
    only ``stderr[:200]`` on the rc!=0 path. Keeping the precise signal at the
    front means it survives that truncation; when codex leaves stderr empty (it
    reports quota/auth failures as JSONL on stdout under ``--json``) the tail
    becomes the front and survives instead.
    """
    _captured, ctx = _capture_codex_invocation(last_message="", stdout="x" * 10_000, stderr="real stderr line")
    with ctx:
        _review, stderr, _rc = CodexBackend().run("sys", "user", "gpt-5.6-terra", 60)

    head, _, tail = stderr.partition("\n")
    assert head == "real stderr line"  # precise signal stays at the front
    assert tail == "x" * CodexBackend._STDOUT_TAIL_CHARS  # stdout portion bounded


def test_codex_backend_short_stdout_tail_is_not_truncated() -> None:
    """Below the cap the tail is carried whole — no off-by-one that clips the
    first character of a short quota message."""
    _captured, ctx = _capture_codex_invocation(last_message="", stdout="  quota exceeded  ")
    with ctx:
        _review, stderr, _rc = CodexBackend().run("sys", "user", "gpt-5.6-terra", 60)

    assert stderr == "quota exceeded"


def test_codex_backend_removes_output_file() -> None:
    """The staged --output-last-message file must not survive the call; the hook
    runs on every commit and would otherwise litter /tmp."""
    captured, ctx = _capture_codex_invocation()
    with ctx:
        CodexBackend().run("sys", "user", "gpt-5.6-terra", 60)

    assert not Path(captured["out_path"]).exists()


def test_codex_backend_unlink_failure_is_swallowed() -> None:
    """A failing cleanup must not take down an otherwise successful review."""
    _captured, ctx = _capture_codex_invocation(last_message="ok")
    with ctx, patch("backends.os.unlink", side_effect=OSError("boom")):
        review, _stderr, rc = CodexBackend().run("sys", "user", "gpt-5.6-terra", 60)

    assert (review, rc) == ("ok", 0)


# ---------------------------------------------------------------------------
# Fallback visibility — record_fallback / fallback_banner
# ---------------------------------------------------------------------------


def test_fallback_banner_none_when_no_fallback() -> None:
    """No fallback recorded → no banner (the common healthy path)."""
    import hook

    assert hook.fallback_banner() is None


def test_fallback_banner_surfaces_primary_failure_with_count() -> None:
    """A recorded fallback yields a loud banner; duplicate reasons collapse to a
    count. This is the signal that was missing when kimi silently ran on Sonnet
    for three weeks."""
    import hook

    hook.record_fallback("kimi", "claude/sonnet", "kimi exited rc=1 with error/empty output")
    hook.record_fallback("kimi", "claude/sonnet", "kimi exited rc=1 with error/empty output")
    banner = hook.fallback_banner()
    assert banner is not None
    assert "PRIMARY REVIEWER FAILED" in banner
    assert "kimi → claude/sonnet" in banner
    assert "(×2)" in banner


def test_emit_fallback_stderr_prints_banner(capsys: pytest.CaptureFixture[str]) -> None:
    import hook

    hook.record_fallback("kimi", "claude/sonnet", "kimi exited rc=1")
    hook.emit_fallback_stderr()
    assert "PRIMARY REVIEWER FAILED" in capsys.readouterr().err


def test_emit_fallback_stderr_silent_when_clean(capsys: pytest.CaptureFixture[str]) -> None:
    import hook

    hook.emit_fallback_stderr()
    assert capsys.readouterr().err == ""


def test_multi_backend_pipeline_prepends_banner_on_fallback() -> None:
    import hook

    hook.record_fallback("kimi", "claude/sonnet", "kimi exited rc=1")
    ok = MagicMock(status="ok", review_text="body")
    seen: dict[str, str] = {}
    with (
        patch("orchestrator.run_multi_backend", return_value=[ok]),
        patch("consolidation.consolidate", return_value=MagicMock(upheld_clusters=[])),
        patch("hook._render_consolidation_display", return_value="REVIEW BODY"),
        patch("hook._persist_run", side_effect=lambda v, f, d, disp, *a, **k: seen.__setitem__("d", disp)),
        patch("hook.extract_warning_lines", return_value=[]),
        patch("hook.info"),
    ):
        verdict = hook._run_multi_backend_pipeline("diff", "files", False)
    assert verdict == "OK"
    assert "PRIMARY REVIEWER FAILED" in seen["d"]
    assert "REVIEW BODY" in seen["d"]


def test_multi_backend_pipeline_no_banner_when_clean() -> None:
    import hook

    ok = MagicMock(status="ok", review_text="body")
    seen: dict[str, str] = {}
    with (
        patch("orchestrator.run_multi_backend", return_value=[ok]),
        patch("consolidation.consolidate", return_value=MagicMock(upheld_clusters=[])),
        patch("hook._render_consolidation_display", return_value="REVIEW BODY"),
        patch("hook._persist_run", side_effect=lambda v, f, d, disp, *a, **k: seen.__setitem__("d", disp)),
        patch("hook.extract_warning_lines", return_value=[]),
        patch("hook.info"),
    ):
        verdict = hook._run_multi_backend_pipeline("diff", "files", False)
    assert verdict == "OK"
    assert "PRIMARY REVIEWER FAILED" not in seen["d"]


def test_multi_backend_pipeline_empty_surfaces_fallback() -> None:
    import hook

    hook.record_fallback("kimi", "claude/sonnet", "kimi exited rc=1")
    bad = MagicMock(status="error", review_text="")
    seen: dict[str, str] = {}
    with (
        patch("orchestrator.run_multi_backend", return_value=[bad]),
        patch("consolidation.consolidate", return_value=MagicMock(upheld_clusters=[])),
        patch("hook._persist_run", side_effect=lambda v, f, d, disp, *a, **k: seen.__setitem__("d", disp)),
        patch("hook.error"),
    ):
        verdict = hook._run_multi_backend_pipeline("diff", "files", False)
    assert verdict == "EMPTY"
    assert "PRIMARY REVIEWER FAILED" in seen["d"]


def test_chunked_path_prepends_banner_on_fallback() -> None:
    import chunked
    import hook

    hook.record_fallback("kimi", "claude/sonnet", "kimi exited rc=1")
    result = chunked.ChunkedResult(
        status="ok",
        validation=chunked.ValidationResult(),
        job_results=[],
        arbiter_raw="",
        arbiter_status="ran",
        arbiter_error=None,
        clusters=[],
        upheld_clusters=[],
        blocking_text="",
        findings_json_text="{}",
        metrics={},
        started_at=0.0,
        ended_at=1.0,
    )
    seen: dict[str, str] = {}
    with (
        patch("chunked.run_chunked_review", return_value=result),
        patch("chunked.write_artifacts"),
        patch("hook.info"),
        patch("hook.save_log", side_effect=lambda v, **k: seen.__setitem__("review", k.get("review", ""))),
    ):
        verdict = hook._run_chunked_path("diff", "files", False)
    assert verdict == "OK"
    assert "PRIMARY REVIEWER FAILED" in seen["review"]


def test_parse_stream_json_skips_blank_and_malformed_lines() -> None:
    stream = "\n".join(["", "not json at all", '{"role":"assistant","content":"the review"}', "   "])
    assert KimiBackend._parse_stream_json(stream) == "the review"


def test_kimi_backend_unlink_failure_is_swallowed() -> None:
    """A failure to delete the temp prompt file must not break the run."""
    real_unlink = os.unlink

    def delete_then_raise(p: str) -> None:
        real_unlink(p)
        raise OSError("simulated cleanup failure")

    _captured, ctx = _capture_kimi_invocation(stdout='{"role":"assistant","content":"ok"}')
    with ctx, patch("backends.os.unlink", side_effect=delete_then_raise):
        stdout, _stderr, rc = KimiBackend().run("sys", "user", "kimi-code/k3", 60)
    assert (stdout, rc) == ("ok", 0)


def test_multi_backend_pipeline_records_orchestrator_fallback() -> None:
    """The orchestrator falls back via an appended FALLBACK result (not
    run_with_fallback), so the production path must bridge that into the
    accumulator itself — else the banner never fires on real commits."""
    import hook

    primary = MagicMock(status="error", review_text="", fallback_used=False, error="boom")
    primary.cfg = MagicMock(backend="kimi", model="m")
    fb = MagicMock(status="ok", review_text="body", fallback_used=True)
    fb.cfg = MagicMock(backend="claude", model="sonnet")
    seen: dict[str, str] = {}
    with (
        patch("orchestrator.run_multi_backend", return_value=[primary, fb]),
        patch("consolidation.consolidate", return_value=MagicMock(upheld_clusters=[])),
        patch("hook._render_consolidation_display", return_value="BODY"),
        patch("hook._persist_run", side_effect=lambda v, f, d, disp, *a, **k: seen.__setitem__("d", disp)),
        patch("hook.extract_warning_lines", return_value=[]),
        patch("hook.info"),
    ):
        verdict = hook._run_multi_backend_pipeline("diff", "files", False)
    assert verdict == "OK"
    # banner present with NO pre-recorded event — it was bridged from the results
    assert "PRIMARY REVIEWER FAILED" in seen["d"]


def test_run_with_fallback_records_fallback_event() -> None:
    """run_with_fallback must record the fallback so fallback_banner() surfaces it."""
    import hook
    from config import RunnerConfig

    primary = RunnerConfig("kimi", "m")
    fallback = RunnerConfig("claude", "sonnet")

    def fake_run_reviewer(cfg: RunnerConfig, _s: str, _u: str) -> tuple[str, str, int]:
        return ("", "boom", 1) if cfg is primary else ("review body", "", 0)

    with patch("hook.run_reviewer", side_effect=fake_run_reviewer), patch("hook.warn"):
        _review, _stderr, rc, used = hook.run_with_fallback(primary, fallback, "sys", "user")
    assert (used, rc) == ("claude", 0)
    assert any("kimi → claude" in e for e in hook._FALLBACK_EVENTS)


def test_run_review_surfaces_banner_on_legacy_path() -> None:
    """Legacy run_review surfaces a recorded fallback on its returned display too."""
    import hook

    hook.record_fallback("kimi", "claude/sonnet", "kimi exited rc=1")
    with (
        patch("hook.applicable_lenses", return_value=["bugs"]),
        patch("hook.count_added_production_lines", return_value=1),
        patch("hook._run_single_call", return_value=("REVIEW", "OK")),
        patch("hook.info"),
    ):
        display, verdict = hook.run_review("diff", "files", False)
    assert verdict == "OK"
    assert display is not None
    assert "PRIMARY REVIEWER FAILED" in display


def test_kimi_backend_empty_stdout_returns_empty_review() -> None:
    """An empty stdout from kimi yields an empty review, not a parse crash."""
    _captured, ctx = _capture_kimi_invocation(stdout="")
    with ctx:
        review, _stderr, rc = KimiBackend().run("sys", "user", "kimi-code/k3", 60)
    assert (review, rc) == ("", 0)


def test_parse_stream_json_ignores_non_string_content() -> None:
    """Non-string assistant `content` (null, list) is skipped, not concatenated."""
    stream = "\n".join(
        [
            '{"role":"assistant","content":null}',
            '{"role":"assistant","content":["a","b"]}',
            '{"role":"assistant","content":"real review"}',
        ]
    )
    assert KimiBackend._parse_stream_json(stream) == "real review"


def test_default_primaries_pin_codex_only() -> None:
    """The runtime composition of PRIMARIES is part of the user-facing
    contract — every commit on the owner's machine sees this list. A
    silent reorder, addition, or removal here would change real
    pre-commit behavior without any failing test. Pin the default so
    drift is caught at test time, not at commit time.

    Current default: codex only (the owner switched from Kimi K2.7 Code to
    the Codex CLI on 2026-08-07 after buying a subscription; requires
    `codex login`). The arbiter and total-failure fallback remain
    claude/sonnet, and CHUNKED_BACKENDS deliberately stays on kimi — see
    test_config_defaults_pinned."""
    assert len(PRIMARIES) == 1
    assert PRIMARIES[0].backend == "codex"
    # Pin the literal, not `_CODEX_MODEL` — asserting the constant against
    # itself passes for any value and would not catch a repin. The slug must
    # also stay in sync with a `slug` in ~/.codex/models_cache.json.
    assert PRIMARIES[0].model == "gpt-5.6-terra"


def test_config_defaults_pinned() -> None:
    """ALLOWED_LENSES and CHUNKED_BACKENDS are part of the runtime contract;
    drift changes real behavior without any failing test."""
    from config import ALLOWED_LENSES

    assert ALLOWED_LENSES == frozenset({"bugs", "architecture", "tests"})
    assert CHUNKED_BACKENDS
    for cfg in CHUNKED_BACKENDS:
        assert cfg.backend in BACKENDS
    # CHUNKED_BACKENDS deliberately diverges from PRIMARIES: the chunked path
    # has no fallback, so an error/timeout there becomes a synthetic [CRITICAL]
    # → false BLOCK. Codex is proven only on the small-commit path so far, so
    # big commits stay on kimi. Pinned so the divergence is a decision, not a
    # drift someone notices when a commit is wrongly blocked.
    assert [c.backend for c in CHUNKED_BACKENDS] == ["kimi"]


# ---------------------------------------------------------------------------
# Arbiter — routing through ARBITER RunnerConfig
# ---------------------------------------------------------------------------


def test_run_arbiter_routes_through_arbiter_config() -> None:
    """run_arbiter must pass ARBITER (not a hardcoded cfg) to run_reviewer.

    Output format follows _ARBITER_VERDICT_RE: ``[UPHELD] F<n>`` per line.
    """
    findings = [
        {"id": "F1", "line": "[F1] [CRITICAL] foo.py:1 — bar — baz"},
        {"id": "F2", "line": "[F2] [CRITICAL] foo.py:2 — baz — qux"},
    ]
    raw_output = "[UPHELD] F1\n[OVERTURN] F2\nSummary: 1 UPHELD, 1 OVERTURN."

    with (
        patch("hook.read_file", return_value="arbiter prompt body"),
        patch("hook.run_reviewer", return_value=(raw_output, "", 0)) as m,
    ):
        result = run_arbiter("a diff", findings)

    assert m.call_count == 1
    assert m.call_args[0][0] is ARBITER
    assert result["status"] == "ok"
    # Real parser path: F1 explicitly upheld, F2 explicitly overturned.
    # If the parser regressed, F2 would default to UPHELD and this would fail.
    assert result["upheld_ids"] == {"F1"}


def test_run_arbiter_timeout_upholds_everything() -> None:
    findings = [{"id": "F1", "line": "[F1] [CRITICAL] foo.py:1 — bar — baz"}]

    def fake(*_a: object, **_k: object) -> object:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    with (
        patch("hook.read_file", return_value="prompt"),
        patch("hook.run_reviewer", side_effect=fake),
    ):
        result = run_arbiter("a diff", findings)

    assert result["status"] == "unavailable"
    assert result["upheld_ids"] == {"F1"}


# ---------------------------------------------------------------------------
# Fallback re-raise — no-fallback path for FileNotFoundError/OSError
# ---------------------------------------------------------------------------


def test_run_with_fallback_no_fallback_reraises_filenotfound() -> None:
    """When FALLBACK is None and the primary binary is missing, the
    FileNotFoundError must propagate to the caller — not be swallowed."""
    primary = RunnerConfig("opencode", "p")

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        raise FileNotFoundError("no opencode on PATH")

    with patch("hook.run_reviewer", side_effect=fake):
        try:
            run_with_fallback(primary, None, "s", "u")
        except FileNotFoundError:
            return
    raise AssertionError("expected FileNotFoundError re-raise when fallback is None")


def test_run_with_fallback_no_fallback_reraises_oserror() -> None:
    primary = RunnerConfig("opencode", "p")

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        raise PermissionError("opencode not executable")

    with patch("hook.run_reviewer", side_effect=fake):
        try:
            run_with_fallback(primary, None, "s", "u")
        except PermissionError:
            return
    raise AssertionError("expected PermissionError re-raise when fallback is None")


def test_run_with_fallback_reraises_timeout_from_fallback_after_primary_failure() -> None:
    """Primary fails (nonzero rc), fallback then times out — the
    TimeoutExpired must propagate to the caller so ``_run_single_call``
    and ``run_single_lens`` can map it to the TIMEOUT status. A
    regression that swallows this branch would misclassify a fully-
    unavailable review pipeline as an ordinary reviewer failure.
    """
    primary, fallback = _pair()
    calls: list[RunnerConfig] = []

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        calls.append(cfg)
        if cfg is primary:
            return "", "primary boom", 1  # primary fails, triggers fallback
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    with patch("hook.run_reviewer", side_effect=fake):
        try:
            run_with_fallback(primary, fallback, "s", "u")
        except subprocess.TimeoutExpired:
            assert calls == [primary, fallback], f"expected primary then fallback; got {[c.backend for c in calls]!r}"
            return
    raise AssertionError("expected TimeoutExpired propagation when fallback also times out")


def test_run_with_fallback_reraises_oserror_from_fallback_after_primary_failure() -> None:
    """Primary fails (empty review), fallback then is unreachable — the
    OSError-class exception must propagate so callers surface "both
    runners unavailable" instead of taking the wrong fail-open path.
    """
    primary, fallback = _pair()
    calls: list[RunnerConfig] = []

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        calls.append(cfg)
        if cfg is primary:
            return "", "", 0  # primary returns empty review, triggers fallback
        raise FileNotFoundError("claude binary not on PATH")

    with patch("hook.run_reviewer", side_effect=fake):
        try:
            run_with_fallback(primary, fallback, "s", "u")
        except FileNotFoundError:
            assert calls == [primary, fallback], f"expected primary then fallback; got {[c.backend for c in calls]!r}"
            return
    raise AssertionError("expected FileNotFoundError propagation when fallback also unreachable")


def test_run_with_fallback_reraises_cascaded_timeout() -> None:
    """Primary times out, fallback also times out — TimeoutExpired must
    propagate. Distinct from the nonzero-rc-then-timeout case because
    the primary path goes through the ``except TimeoutExpired`` branch
    (sets ``primary_failed_reason``) instead of the rc check.
    """
    primary, fallback = _pair()

    def fake(cfg: RunnerConfig, sys_p: str, user_p: str) -> tuple[str, str, int]:
        raise subprocess.TimeoutExpired(cmd=cfg.backend, timeout=1)

    with patch("hook.run_reviewer", side_effect=fake):
        try:
            run_with_fallback(primary, fallback, "s", "u")
        except subprocess.TimeoutExpired:
            return
    raise AssertionError("expected TimeoutExpired when both runners time out")


# ---------------------------------------------------------------------------
# run_single_lens — failure-→-status mapping (fan-out path)
# ---------------------------------------------------------------------------


def test_run_single_lens_success_returns_ok_status() -> None:
    with patch("hook.run_with_fallback", return_value=("a real review body", "", 0, "opencode")):
        result = run_single_lens("bugs", "diff", "src/foo.py", False)
    assert result["status"] == "ok"
    assert result["review"] == "a real review body"
    assert result["reviewer"] == "opencode"
    assert result["error"] == ""


def test_run_single_lens_nonzero_rc_returns_error_status() -> None:
    with patch("hook.run_with_fallback", return_value=("partial", "stderr-msg", 7, "claude")):
        result = run_single_lens("bugs", "diff", "src/foo.py", False)
    assert result["status"] == "error"
    assert "rc=7" in result["error"]
    assert "stderr-msg" in result["error"]
    assert result["reviewer"] == "claude"


def test_run_single_lens_empty_review_returns_error_status() -> None:
    with patch("hook.run_with_fallback", return_value=("   \n  ", "", 0, "opencode")):
        result = run_single_lens("bugs", "diff", "src/foo.py", False)
    assert result["status"] == "error"
    assert result["reviewer"] == "opencode"


def test_run_single_lens_timeout_returns_timeout_status() -> None:
    def fake(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    with patch("hook.run_with_fallback", side_effect=fake):
        result = run_single_lens("bugs", "diff", "src/foo.py", False)
    assert result["status"] == "timeout"
    assert "timeout" in result["error"]
    # FALLBACK is configured (not None), so the timeout label points at the
    # backend that finally timed out — the fallback. Derived from config so a
    # backend swap in config.py does not break this behavioral assertion.
    assert FALLBACK is not None
    assert result["reviewer"] == FALLBACK.backend


def test_run_single_lens_timeout_with_no_fallback_labels_primary() -> None:
    """When FALLBACK = None and the sole reviewer times out, the
    timeout label must come from PRIMARY.backend, not crash on a
    ``None.backend`` access. Pins the
    ``timed_out = FALLBACK.backend if FALLBACK is not None else PRIMARY.backend``
    branch in run_single_lens.
    """

    def fake(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=1)

    with (
        patch("hook.run_with_fallback", side_effect=fake),
        patch("hook.FALLBACK", None),
    ):
        result = run_single_lens("bugs", "diff", "src/foo.py", False)

    assert result["status"] == "timeout"
    # PRIMARIES[0].backend, since FALLBACK is patched to None. Derived from
    # config so a backend swap in config.py does not break this behavioral
    # assertion.
    assert result["reviewer"] == PRIMARIES[0].backend
    assert PRIMARIES[0].backend in result["error"]


def test_run_single_lens_filenotfound_returns_error_status() -> None:
    def fake(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("no claude on PATH")

    with patch("hook.run_with_fallback", side_effect=fake):
        result = run_single_lens("bugs", "diff", "src/foo.py", False)
    assert result["status"] == "error"
    assert "both runners unavailable" in result["error"]
    assert result["reviewer"] is None


def test_run_single_lens_oserror_returns_error_status() -> None:
    def fake(*_args: object, **_kwargs: object) -> object:
        raise PermissionError("claude not executable")

    with patch("hook.run_with_fallback", side_effect=fake):
        result = run_single_lens("bugs", "diff", "src/foo.py", False)
    assert result["status"] == "error"
    assert "both runners unavailable" in result["error"]
    assert result["reviewer"] is None


def test_run_single_lens_missing_prompt_returns_error_status() -> None:
    """Error message must reference the real prompt files
    (``prompts/<lens>.md`` + ``prompts/common.md``), not a fictional
    ``lens_<lens>.md`` naming. If a prompt actually goes missing, the
    operator gets a path that ``ls`` can find."""
    with patch("hook.build_lens_system_prompt", return_value=""):
        result = run_single_lens("bugs", "diff", "src/foo.py", False)
    assert result["status"] == "error"
    assert "prompts/bugs.md" in result["error"]
    assert "prompts/common.md" in result["error"]
    assert result["reviewer"] is None


# ---------------------------------------------------------------------------
# _run_single_call — exception branches in single-call path
# ---------------------------------------------------------------------------


def test_run_single_call_timeout_returns_timeout_verdict() -> None:
    """When the single-call reviewer times out, the hook fails open
    with verdict TIMEOUT and review=None (commit allowed)."""

    def fake(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    with (
        patch("hook.build_system_prompt", return_value="sys"),
        patch("hook.run_with_fallback", side_effect=fake),
        patch("hook.save_log"),
    ):
        review, verdict = _run_single_call("diff", "src/foo.py", False)

    assert review is None
    assert verdict == "TIMEOUT"


def test_run_single_call_filenotfound_returns_skip_verdict() -> None:
    def fake(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("no claude on PATH")

    with (
        patch("hook.build_system_prompt", return_value="sys"),
        patch("hook.run_with_fallback", side_effect=fake),
        patch("hook.save_log"),
    ):
        review, verdict = _run_single_call("diff", "src/foo.py", False)

    assert review is None
    assert verdict == "SKIP"


def test_run_single_call_oserror_returns_skip_verdict() -> None:
    """Generic OSError (e.g., permission denied launching the binary)
    must take the same fail-open path as FileNotFoundError."""

    def fake(*_args: object, **_kwargs: object) -> object:
        raise PermissionError("claude not executable")

    with (
        patch("hook.build_system_prompt", return_value="sys"),
        patch("hook.run_with_fallback", side_effect=fake),
        patch("hook.save_log"),
    ):
        review, verdict = _run_single_call("diff", "src/foo.py", False)

    assert review is None
    assert verdict == "SKIP"


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


def _build_prod_diff(added_lines: int) -> str:
    body = "\n".join(f"+line {i}" for i in range(added_lines))
    return f"diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -0,0 +1,{added_lines} @@\n{body}"


def test_under_limit_returns_none() -> None:
    assert check_diff_size(_build_prod_diff(MAX_PROD_LINES)) is None


def test_over_limit_returns_message() -> None:
    result = check_diff_size(_build_prod_diff(MAX_PROD_LINES + 1))
    assert result is not None
    assert str(MAX_PROD_LINES) in result
    assert "production-code" in result
    # The message must point the writer at the chunked-review escape hatch.
    assert "manifest.yaml" in result
    assert "scaffold_manifest.py" in result


def test_test_file_diff_does_not_trip_cap() -> None:
    body = "\n".join(f"+line {i}" for i in range(MAX_PROD_LINES * 5))
    diff = (
        "diff --git a/tests/foo_test.py b/tests/foo_test.py\n"
        "--- a/tests/foo_test.py\n"
        "+++ b/tests/foo_test.py\n"
        f"@@ -0,0 +1,{MAX_PROD_LINES * 5} @@\n"
        f"{body}"
    )
    assert check_diff_size(diff) is None


# ---------------------------------------------------------------------------
# opencode plumbing
# ---------------------------------------------------------------------------


def test_opencode_backend_parses_json_text_events() -> None:
    raw = (
        '{"type":"step_start","timestamp":1}\n'
        '{"type":"text","timestamp":2,"part":{"type":"text","text":"partial"}}\n'
        '{"type":"text","timestamp":3,"part":{"type":"text","text":"[WARNING] foo\\n\\nSummary: 0 CRITICAL"}}\n'
        '{"type":"step_finish","timestamp":4}\n'
    )
    assert OpencodeBackend._parse_json(raw) == "partial\n[WARNING] foo\n\nSummary: 0 CRITICAL"


def test_opencode_backend_parses_empty_json_output() -> None:
    assert OpencodeBackend._parse_json("") == ""
    assert OpencodeBackend._parse_json('{"type":"step_start"}\n') == ""


def test_opencode_backend_parses_skips_malformed_and_non_text_lines() -> None:
    """A malformed JSON line in the middle of the stream is skipped, and
    valid non-text events (step_start, tool_call, ...) are filtered out
    — subsequent valid text events still come through.

    Defends the ``except json.JSONDecodeError: continue`` branch and
    the ``if event.get("type") == "text"`` filter against regressions
    that would abort parsing or drop subsequent text on a single bad
    line.
    """
    raw = (
        '{"type":"text","part":{"type":"text","text":"first"}}\n'
        "this is not valid json at all\n"
        '{"type":"step_start","timestamp":1}\n'
        '{"type":"tool_call","part":{"name":"Read"}}\n'
        '{"type":"text","part":{"type":"text","text":"second"}}\n'
    )
    assert OpencodeBackend._parse_json(raw) == "first\nsecond"


def test_opencode_backend_builds_correct_command() -> None:
    json_output = '{"type":"text","part":{"type":"text","text":"done"}}\n'
    mock_result = MagicMock()
    mock_result.stdout = json_output
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("backends.subprocess.run", return_value=mock_result) as mock_run:
        stdout, stderr, rc = OpencodeBackend().run("sys", "user", "github-copilot/gpt-5.4", 1200)

    cmd = mock_run.call_args[0][0]
    kwargs = mock_run.call_args.kwargs
    assert cmd[0] == "opencode"
    assert "run" in cmd
    assert "--pure" in cmd
    assert "--format" in cmd
    assert "json" in cmd
    assert "--model" in cmd
    assert "github-copilot/gpt-5.4" in cmd
    # Prompt must be piped via stdin, not argv — chunked-path prompts
    # routinely exceed Linux ARG_MAX (~128KB).
    assert "sys\n\nuser" not in cmd
    assert kwargs["input"] == "sys\n\nuser"
    assert stdout == "done"
    assert rc == 0


def test_opencode_backend_empty_system_prompt() -> None:
    json_output = '{"type":"text","part":{"type":"text","text":"done"}}\n'
    mock_result = MagicMock()
    mock_result.stdout = json_output
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("backends.subprocess.run", return_value=mock_result) as mock_run:
        OpencodeBackend().run("", "user only", "github-copilot/gpt-5.4", 1200)

    kwargs = mock_run.call_args.kwargs
    assert kwargs["input"] == "user only"


def test_opencode_backend_run_empty_stdout_short_circuits_parser() -> None:
    """When the CLI returns empty stdout, run() must return an empty
    review without invoking _parse_json — the ``if result.stdout``
    short-circuit. stderr and returncode still flow through to the
    caller so error reporting (rc=1, "no model found", ...) is not
    swallowed.
    """
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.stderr = "no model found"
    mock_result.returncode = 1

    sentinel_called: list[bool] = []
    real_parse = OpencodeBackend._parse_json

    def tracking_parse(raw: str) -> str:
        sentinel_called.append(True)
        return real_parse(raw)

    with (
        patch("backends.subprocess.run", return_value=mock_result),
        patch.object(OpencodeBackend, "_parse_json", staticmethod(tracking_parse)),
    ):
        review, stderr, rc = OpencodeBackend().run("sys", "user", "model-x", 600)

    assert review == ""
    assert stderr == "no model found"
    assert rc == 1
    assert sentinel_called == [], "_parse_json must not be called when stdout is empty"


# ---------------------------------------------------------------------------
# is_test_file / is_production_code / count_added_production_lines
# ---------------------------------------------------------------------------


def test_is_test_file_by_basename() -> None:
    assert is_test_file("test_foo.py")
    assert is_test_file("src/app/test_handler.py")
    assert is_test_file("pkg/foo_test.go")
    assert is_test_file("src/bar.test.ts")
    assert is_test_file("src/bar.test.tsx")
    assert is_test_file("src/baz.spec.js")
    assert is_test_file("com/Foo/BarTest.java")
    assert is_test_file("Module/FooTests.cs")


def test_is_test_file_by_path_segment() -> None:
    assert is_test_file("tests/unit/foo.py")
    assert is_test_file("test/foo.ts")
    assert is_test_file("src/__tests__/Button.tsx")
    assert is_test_file("spec/fixtures/data.py")
    assert is_test_file("pkg/testing/helpers.go")


def test_is_test_file_negative() -> None:
    assert not is_test_file("src/foo.py")
    assert not is_test_file("lib/main.ts")
    assert not is_test_file("pkg/server.go")
    assert not is_test_file("README.md")
    # "test" must be a path segment, not a substring of a regular name.
    assert not is_test_file("src/latest/foo.py")
    assert not is_test_file("src/contest/bar.py")


def test_is_production_code_positive() -> None:
    assert is_production_code("src/app.py")
    assert is_production_code("lib/main.ts")
    assert is_production_code("pkg/server.go")


def test_is_production_code_excludes_tests() -> None:
    assert not is_production_code("tests/test_foo.py")
    assert not is_production_code("pkg/foo_test.go")
    assert not is_production_code("src/__tests__/Button.tsx")
    assert not is_production_code("src/bar.spec.ts")


def test_is_production_code_excludes_non_code() -> None:
    assert not is_production_code("README.md")
    assert not is_production_code("config.yml")
    assert not is_production_code("data/fixture.json")
    assert not is_production_code("Dockerfile")


def test_count_added_production_lines_counts_only_prod() -> None:
    diff = (
        "diff --git a/src/lib.py b/src/lib.py\n"
        "--- a/src/lib.py\n"
        "+++ b/src/lib.py\n"
        "@@ -1,1 +1,3 @@\n"
        " ctx\n"
        "+def new_fn():\n"
        "+    return 1\n"
        "diff --git a/tests/test_lib.py b/tests/test_lib.py\n"
        "--- a/tests/test_lib.py\n"
        "+++ b/tests/test_lib.py\n"
        "@@ -1,1 +1,3 @@\n"
        "+def test_new():\n"
        "+    assert 1 == 1\n"
        "+    # pad\n"
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,1 +1,2 @@\n"
        "+new docs line\n"
    )
    assert count_added_production_lines(diff) == 2


def test_count_added_production_lines_skips_file_headers() -> None:
    diff = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n+added-1\n+added-2\n-removed\n context\n"
    assert count_added_production_lines(diff) == 2


def test_count_added_production_lines_handles_rename() -> None:
    diff = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 80%\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "--- a/old.py\n"
        "+++ b/new.py\n"
        "+x = 1\n"
        "+y = 2\n"
    )
    assert count_added_production_lines(diff) == 2


def test_count_added_production_lines_binary_file_does_not_leak() -> None:
    diff = (
        "diff --git a/first.py b/first.py\n"
        "--- a/first.py\n"
        "+++ b/first.py\n"
        "+prod_line = 1\n"
        "diff --git a/image.png b/image.png\n"
        "Binary files a/image.png and b/image.png differ\n"
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "+assert True\n"
    )
    # first.py → 1 prod line; image.png resets state; test_x.py excluded.
    assert count_added_production_lines(diff) == 1


def test_count_added_production_lines_dev_null_deletion() -> None:
    diff = (
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-removed\n"
        "-removed\n"
    )
    assert count_added_production_lines(diff) == 0


def test_count_added_production_lines_zero_on_no_added() -> None:
    diff = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n-removed\n context\n"
    assert count_added_production_lines(diff) == 0


def test_save_log_diff_stats_includes_prod_count(tmp_path: Path) -> None:
    """The Diff stats line records both total and prod-added counts."""
    from hook import save_log as _save_log

    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+a\n+b\n+c\n"
        "diff --git a/tests/foo_test.py b/tests/foo_test.py\n"
        "--- a/tests/foo_test.py\n"
        "+++ b/tests/foo_test.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+t\n+u\n"
    )
    with patch("hook.LOG_DIR", tmp_path):
        _save_log("OK", diff=diff)
    written = next(tmp_path.glob("*_OK.md")).read_text(encoding="utf-8")
    total = len(diff.splitlines())
    assert f"{total} lines in diff" in written
    assert "(3 added prod line(s))" in written


def test_fanout_threshold_sane_default() -> None:
    # Fan-out has been moved to the chunked path (chunked review of large
    # commits). The small-commit path (N < MAX_PROD_LINES) now runs a single
    # combined-prompt reviewer per backend — no fan-out. Setting the
    # threshold equal to MAX_PROD_LINES neutralizes fan-out for small
    # commits while keeping the run_fanout function reusable from chunked.py
    # for the whole-diff lens layer.
    assert FANOUT_THRESHOLD == MAX_PROD_LINES
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


def test_render_fanout_output_inlines_warning_lines_from_lenses() -> None:
    """Regression: warnings from lens reviews must appear verbatim in the
    BLOCK-path display so the fix-in-one-pass directive can reference
    them without pointing to an external log file."""
    per_lens = [
        {
            "name": "architecture",
            "status": "ok",
            "review": (
                "### Section 2 — Architecture findings\n"
                "- [WARNING] `foo.py:10` — `def frob()` — duplicates helper in bar.py\n"
                "- [WARNING] `foo.py:22` — `class X` — circular import risk\n"
                "Summary: 0 CRITICAL, 2 WARNING across 1 files."
            ),
            "reviewer": "opencode",
            "error": "",
        },
    ]
    findings = [{"id": "F1", "line": "- [F1] [CRITICAL] foo.py:5 — bug"}]
    arbiter = {
        "status": "ok",
        "upheld_ids": {"F1"},
        "raw": "[UPHELD] F1 — confirmed.\nSummary: 1 UPHELD, 0 OVERTURN.",
        "error": "",
    }
    rendered = _render_fanout_output(per_lens, findings, {"F1"}, arbiter)

    assert "duplicates helper in bar.py" in rendered
    assert "circular import risk" in rendered
    assert "Warnings: 2" in rendered
    assert "2 WARNING" in rendered  # summary line still accurate


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


def test_run_review_stays_single_call_on_tests_heavy_diff() -> None:
    """Large tests-only diff with tiny prod change → single-call path.

    Before this change the raw + count would push the commit to
    fan-out; after it, only added production-code lines gate the
    decision, so a test-fixture-heavy commit with <150 prod lines
    stays on the cheap path.
    """
    test_added = [f"+    assert_{i} = True" for i in range(FANOUT_THRESHOLD + 50)]
    prod_added = ["+def tiny():", "+    return 1"]
    diff = (
        "diff --git a/tests/test_big.py b/tests/test_big.py\n"
        "--- a/tests/test_big.py\n"
        "+++ b/tests/test_big.py\n" + "\n".join(test_added) + "\n"
        "diff --git a/src/lib.py b/src/lib.py\n"
        "--- a/src/lib.py\n"
        "+++ b/src/lib.py\n" + "\n".join(prod_added) + "\n"
    )
    files = "tests/test_big.py\nsrc/lib.py"
    with (
        patch("hook._run_single_call", return_value=(None, "OK")) as mock_single,
        patch("hook._run_fanout_with_arbiter") as mock_fanout,
    ):
        run_review(diff, files, is_merge=False)

    mock_single.assert_called_once()
    mock_fanout.assert_not_called()


def test_run_review_stays_single_call_on_config_heavy_diff() -> None:
    """Large config-only diff → single-call path.

    Config/data churn (e.g. vendored yaml, lockfiles) does not
    count toward FANOUT_THRESHOLD any more; the diff still reaches
    the ``bugs`` lens but through the cheap single-call path.
    """
    config_added = [f"+  key_{i}: value" for i in range(FANOUT_THRESHOLD + 20)]
    diff = (
        "diff --git a/deploy/config.yml b/deploy/config.yml\n"
        "--- a/deploy/config.yml\n"
        "+++ b/deploy/config.yml\n" + "\n".join(config_added) + "\n"
    )
    files = "deploy/config.yml"
    with (
        patch("hook._run_single_call", return_value=(None, "OK")) as mock_single,
        patch("hook._run_fanout_with_arbiter") as mock_fanout,
    ):
        run_review(diff, files, is_merge=False)

    mock_single.assert_called_once()
    mock_fanout.assert_not_called()


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


def test_arbitrate_block_display_inlines_warning_lines() -> None:
    """Regression: the single-call BLOCK display must render each
    `[WARNING]` line verbatim (not just the count), so the fix-in-one-
    pass directive's "address every [WARNING] above" is actually
    actionable without opening the log file."""
    from hook import _arbitrate_single_call_review

    review = (
        "### Section 1 — File audit\n"
        "- foo.py — REVIEWED\n"
        "### Section 2 — Findings\n"
        "- [CRITICAL] foo.py:10 — `eval(x)` — RCE.\n"
        "- [WARNING] foo.py:12 — `helper()` — duplicated in bar.py.\n"
        "- [WARNING] foo.py:30 — stale comment.\n"
        "Summary: 1 CRITICAL, 2 WARNING across 1 files."
    )
    with patch("hook.run_arbiter", return_value=_mock_arbiter(upheld_ids={"F1"})), patch("hook.save_log"):
        display, verdict = _arbitrate_single_call_review(
            review=review,
            reviewer="opencode",
            diff="diff --git a/foo.py b/foo.py\n+eval(x)\n",
            files="foo.py",
        )

    assert verdict == "BLOCK"
    assert "duplicated in bar.py" in display
    assert "stale comment" in display
    assert "Warnings: 2" in display
    assert "2 WARNING" in display  # summary line accurate
    # Old "(see log for detail)" placeholder must not appear
    assert "(see log for detail)" not in display


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

    from hook import error as hook_error
    from hook import main as hook_main

    buf = io.StringIO()

    def fake_pipeline(diff: str, files: str, is_merge: bool) -> str:
        # Mirror the production BLOCK code path: emit the developer-facing
        # banner + both directives so the assertions stay end-to-end.
        from hook import _BLOCK_FIX_DIRECTIVE, _BLOCK_TRADEOFF_DIRECTIVE
        from hook import info as hook_info

        hook_error(f"Review BLOCKED this commit:\n\n{review_text}")
        hook_info(_BLOCK_FIX_DIRECTIVE)
        hook_info(_BLOCK_TRADEOFF_DIRECTIVE)
        return "BLOCK"

    with (
        # Skip the chunked dispatch — these tests pin the legacy small-commit
        # main() path. The chunked path has its own coverage in test_chunked.py
        # and the dispatch helpers are tested separately.
        patch("hook._maybe_dispatch_chunked", return_value=None),
        patch("hook._check_staged_review_guard", return_value=None),
        patch("hook.run_gate", return_value=0),
        patch("hook.collect_diff", return_value=("diff-body", "foo.py", False)),
        patch("hook._run_multi_backend_pipeline", side_effect=fake_pipeline),
        patch("hook.applicable_lenses", return_value=["bugs"]),
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


def test_main_calls_verify_runner_configs_before_diff_collection() -> None:
    """Startup validation must run before any LLM-touching work.

    Without this guard call, a misconfigured backend in config.py
    (e.g. PRIMARY = RunnerConfig("opencod" /* typo */, ...)) would
    surface only when run_reviewer is finally called — and then bubble
    to main()'s broad fail-open ``except Exception``, silently
    skipping the entire review gate. This test pins the call order:
    if a future edit removes ``_verify_runner_configs()`` from main()
    or moves it after ``collect_diff()``, this test fails.
    """
    from hook import main as hook_main

    call_order: list[str] = []

    def fake_verify() -> None:
        call_order.append("verify")
        raise ValueError("PRIMARY has invalid backend 'bogus'; must be one of [...]")

    def fake_collect() -> None:
        call_order.append("collect")  # must NOT be reached
        return None

    captured: list[str] = []

    def fake_warn(msg: str) -> None:
        captured.append(msg)

    with (
        patch("hook._verify_runner_configs", side_effect=fake_verify),
        patch("hook.collect_diff", side_effect=fake_collect),
        patch("hook.warn", side_effect=fake_warn),
        patch("hook.save_log"),
    ):
        try:
            hook_main()
        except SystemExit as exc:
            # main()'s except Exception fail-open: exit 0, never block on
            # our own bug. The named warn-log identifies the misconfig.
            assert exc.code == 0
        else:
            raise AssertionError("hook.main() must call sys.exit() in fail-open path")

    assert call_order == ["verify"], f"verify must run before collect_diff; got call order {call_order!r}"
    assert any("bogus" in m or "ValueError" in m for m in captured), (
        f"warn-log must surface the misconfig; got {captured!r}"
    )


# ---------------------------------------------------------------------------
# Pre-flight gate integration
# ---------------------------------------------------------------------------


def test_main_preflight_gate_zero_proceeds() -> None:
    """Gate returns 0 → main() proceeds to _maybe_dispatch_chunked."""
    from hook import main as hook_main

    with (
        patch("hook._verify_runner_configs"),
        patch("hook._check_staged_review_guard"),
        patch("hook.COVERAGE_GATE", MagicMock(enabled=True)),
        patch("hook.run_gate", return_value=0),
        patch("hook._maybe_dispatch_chunked") as m_chunked,
        patch("hook.collect_diff", return_value=None),
    ):
        try:
            hook_main()
        except SystemExit as exc:
            assert exc.code == 0
        else:
            pass
    m_chunked.assert_called_once()


def test_main_preflight_gate_one_blocks() -> None:
    """Gate returns 1 → main() exits 1, _maybe_dispatch_chunked not called."""
    from hook import main as hook_main

    with (
        patch("hook._verify_runner_configs"),
        patch("hook._check_staged_review_guard"),
        patch("hook.COVERAGE_GATE", MagicMock(enabled=True)),
        patch("hook.run_gate", return_value=1),
        patch("hook._maybe_dispatch_chunked") as m_chunked,
    ):
        with pytest.raises(SystemExit) as exc_info:
            hook_main()
        assert exc_info.value.code == 1
    m_chunked.assert_not_called()


def test_main_preflight_gate_crash_exits_three() -> None:
    """Gate raises Exception → caught by inner try/except, exits 3 (not 0)."""
    from hook import main as hook_main

    with (
        patch("hook._verify_runner_configs"),
        patch("hook._check_staged_review_guard"),
        patch("hook.COVERAGE_GATE", MagicMock(enabled=True)),
        patch("hook.run_gate", side_effect=RuntimeError("boom")),
        patch("hook._maybe_dispatch_chunked") as m_chunked,
    ):
        with pytest.raises(SystemExit) as exc_info:
            hook_main()
        assert exc_info.value.code == 3
    m_chunked.assert_not_called()


def test_main_preflight_gate_disabled_skips_run_gate() -> None:
    """Gate disabled → run_gate NOT called, control proceeds."""
    from dataclasses import dataclass
    from hook import main as hook_main

    @dataclass(frozen=True)
    class FrozenGate:
        enabled: bool = False

    with (
        patch("hook._verify_runner_configs"),
        patch("hook._check_staged_review_guard"),
        patch("hook.COVERAGE_GATE", FrozenGate()),
        patch("hook.run_gate") as m_gate,
        patch("hook._maybe_dispatch_chunked") as m_chunked,
        patch("hook.collect_diff", return_value=None),
    ):
        try:
            hook_main()
        except SystemExit as exc:
            assert exc.code == 0
        else:
            pass
    m_gate.assert_not_called()
    m_chunked.assert_called_once()


def test_main_preflight_gate_setup_error_exits_two() -> None:
    """run_gate returns 2 → main() exits 2, _maybe_dispatch_chunked not called."""
    from hook import main as hook_main

    with (
        patch("hook._verify_runner_configs"),
        patch("hook._check_staged_review_guard"),
        patch("hook.COVERAGE_GATE", MagicMock(enabled=True)),
        patch("hook.run_gate", return_value=2),
        patch("hook._maybe_dispatch_chunked") as m_chunked,
    ):
        with pytest.raises(SystemExit) as exc_info:
            hook_main()
        assert exc_info.value.code == 2
    m_chunked.assert_not_called()


# ---------------------------------------------------------------------------
# Pre-review fast-path — skip the LLM review when the staged diff was already
# reviewed CLEAN (marker present) AND the workflow enabled the mode via env.
# Deterministic gates (run_gate, gitleaks/semgrep) are unaffected.
# ---------------------------------------------------------------------------


def test_fastpath_true_when_flag_and_marker_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from hook import _maybe_fastpath

    monkeypatch.setenv("SDD_REVIEW_FASTPATH", "1")
    with (
        patch("hook.get_staged_diff", return_value=("+payload\n", "")),
        patch("hook.get_staged_content_key", return_value="a" * 64),
        patch("hook.approvals.approval_exists", return_value=True) as m_exists,
        patch("hook.save_log") as m_log,
    ):
        assert _maybe_fastpath() is True
    m_exists.assert_called_once_with(Path.cwd(), "a" * 64)  # matches on the CONTENT key
    m_log.assert_called_once()


def test_get_staged_content_key_empty_on_git_failure() -> None:
    """git diff non-zero → "" (→ fast-path miss → full review, fail-safe)."""
    from hook import get_staged_content_key

    fake = MagicMock(returncode=128, stdout="")
    with patch("hook.subprocess.run", return_value=fake):
        assert get_staged_content_key() == ""


def test_fastpath_false_when_content_key_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """git failed to produce a content key ("") → no match, full review (fail-safe)."""
    from hook import _maybe_fastpath

    monkeypatch.setenv("SDD_REVIEW_FASTPATH", "1")
    with (
        patch("hook.get_staged_diff", return_value=("+payload\n", "")),
        patch("hook.get_staged_content_key", return_value=""),
        patch("hook.approvals.approval_exists", return_value=True) as m_exists,
    ):
        assert _maybe_fastpath() is False
    m_exists.assert_not_called()  # an empty key never consults the marker store


def test_fastpath_false_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from hook import _maybe_fastpath

    monkeypatch.delenv("SDD_REVIEW_FASTPATH", raising=False)
    with (
        patch("hook.get_staged_diff", return_value=("+payload\n", "")),
        patch("hook.approvals.approval_exists", return_value=True) as m_exists,
    ):
        assert _maybe_fastpath() is False
    m_exists.assert_not_called()  # flag gates even the lookup


def test_fastpath_false_when_no_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    from hook import _maybe_fastpath

    monkeypatch.setenv("SDD_REVIEW_FASTPATH", "1")
    with (
        patch("hook.get_staged_diff", return_value=("+unreviewed\n", "")),
        patch("hook.get_staged_content_key", return_value="b" * 64),
        patch("hook.approvals.approval_exists", return_value=False),
    ):
        assert _maybe_fastpath() is False


def test_fastpath_false_when_diff_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from hook import _maybe_fastpath

    monkeypatch.setenv("SDD_REVIEW_FASTPATH", "1")
    with (
        patch("hook.get_staged_diff", return_value=("", "")),
        patch("hook.approvals.approval_exists", return_value=True) as m_exists,
    ):
        assert _maybe_fastpath() is False
    m_exists.assert_not_called()


def test_main_fastpath_skips_llm_but_runs_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approved diff: run_gate runs, LLM pipeline + chunked dispatch do NOT, exit 0."""
    from hook import main as hook_main

    monkeypatch.setenv("SDD_REVIEW_FASTPATH", "1")
    with (
        patch("hook._verify_runner_configs"),
        patch("hook._check_staged_review_guard"),
        patch("hook.COVERAGE_GATE", MagicMock(enabled=True)),
        patch("hook.run_gate", return_value=0) as m_gate,
        patch("hook.get_staged_diff", return_value=("+payload\n", "")),
        patch("hook.get_staged_content_key", return_value="a" * 64),
        patch("hook.approvals.approval_exists", return_value=True),
        patch("hook._maybe_dispatch_chunked") as m_chunked,
        patch("hook._run_multi_backend_pipeline") as m_pipeline,
        patch("hook.save_log"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            hook_main()
    assert exc_info.value.code == 0
    m_gate.assert_called_once()  # deterministic gate still enforced
    m_chunked.assert_not_called()
    m_pipeline.assert_not_called()  # LLM review skipped


def test_main_no_marker_falls_through_to_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag set but diff NOT pre-approved → full review runs (fail-safe)."""
    from hook import main as hook_main

    monkeypatch.setenv("SDD_REVIEW_FASTPATH", "1")
    with (
        patch("hook._verify_runner_configs"),
        patch("hook._check_staged_review_guard"),
        patch("hook.COVERAGE_GATE", MagicMock(enabled=True)),
        patch("hook.run_gate", return_value=0),
        patch("hook.get_staged_diff", return_value=("+unreviewed\n", "")),
        patch("hook.get_staged_content_key", return_value="b" * 64),
        patch("hook.approvals.approval_exists", return_value=False),
        patch("hook._maybe_dispatch_chunked"),
        patch("hook.collect_diff", return_value=("+unreviewed\n", "x.py", False)),
        patch("hook.applicable_lenses", return_value=["bugs"]),
        patch("hook._run_multi_backend_pipeline", return_value="OK") as m_pipeline,
    ):
        with pytest.raises(SystemExit) as exc_info:
            hook_main()
    assert exc_info.value.code == 0
    m_pipeline.assert_called_once()  # review NOT skipped


# ---------------------------------------------------------------------------
# extract_warning_lines — surface [WARNING] detail to stderr, not just count
# ---------------------------------------------------------------------------


def test_extract_warning_lines_returns_full_line_text() -> None:
    review = (
        "- [CRITICAL] a.py:1 — bug\n"
        "- [WARNING] b.py:2 — `foo` — duplicated helper\n"
        "  - [WARNING] c.py:9 — stale TODO\n"
        "Summary: 1 CRITICAL, 2 WARNING across 1 files."
    )
    lines = extract_warning_lines(review)
    assert len(lines) == 2
    assert any("b.py:2" in ln and "duplicated helper" in ln for ln in lines)
    assert any("c.py:9" in ln and "stale TODO" in ln for ln in lines)


def test_extract_warning_lines_empty_when_none() -> None:
    assert extract_warning_lines("") == []
    assert extract_warning_lines("- [CRITICAL] only\nSummary: 1 CRITICAL.") == []


def test_extract_warning_lines_case_insensitive() -> None:
    assert extract_warning_lines("- [warning] lower-case tag") == ["- [warning] lower-case tag"]


def test_extract_warning_lines_ignores_mid_line_mention() -> None:
    """Prose or quoted diff text containing `[WARNING]` must not be
    mistaken for a finding line — only bullet/id-anchored lines count."""
    review = (
        "The arbiter treats [WARNING] lines as advisory.\n"
        "+    # [WARNING] legacy comment in diff\n"
        "- [WARNING] real.py:1 — actual finding\n"
        "Summary: 0 CRITICAL, 1 WARNING across 1 files."
    )
    lines = extract_warning_lines(review)
    assert lines == ["- [WARNING] real.py:1 — actual finding"]


def test_extract_warning_lines_accepts_finding_id_prefix() -> None:
    """Arbiter-tagged findings use a `[Fn]` prefix before `[WARNING]`
    — those must still be captured (mirrors `_CRITICAL_LINE_RE`)."""
    review = "- [F3] [WARNING] foo.py:4 — tagged finding\nSummary: 0 CRITICAL, 1 WARNING across 1 files."
    assert extract_warning_lines(review) == ["- [F3] [WARNING] foo.py:4 — tagged finding"]


# ---------------------------------------------------------------------------
# Warning detection is a single source of truth — every count/gate path
# must go through extract_warning_lines (no loose substring matches).
# ---------------------------------------------------------------------------


def _invoke_main_on_ok(review_text: str) -> str:
    """Run hook.main() with a forced OK verdict, return captured stderr.

    Used to exercise the OK-path banner gate that surfaces non-blocking
    warnings to the developer.
    """
    import io
    import sys

    from hook import main as hook_main
    from hook import warn as hook_warn

    buf = io.StringIO()

    def fake_pipeline(diff: str, files: str, is_merge: bool) -> str:
        # Mirror the OK-path banner gate from _run_multi_backend_pipeline.
        from hook import extract_warning_lines

        if extract_warning_lines(review_text):
            hook_warn(f"Review notes (non-blocking warnings):\n{review_text}")
        return "OK"

    with (
        patch("hook._maybe_dispatch_chunked", return_value=None),
        patch("hook._check_staged_review_guard", return_value=None),
        patch("hook.run_gate", return_value=0),
        patch("hook.collect_diff", return_value=("diff-body", "foo.py", False)),
        patch("hook._run_multi_backend_pipeline", side_effect=fake_pipeline),
        patch("hook.applicable_lenses", return_value=["bugs"]),
        patch.object(sys, "stderr", buf),
    ):
        try:
            hook_main()
        except SystemExit as exc:
            assert exc.code == 0, f"OK must exit 0, got {exc.code}"
        else:
            raise AssertionError("main() should have called sys.exit(0) on OK")

    return buf.getvalue()


def test_main_ok_banner_fires_on_real_warning_finding() -> None:
    review = (
        "### Section 2 — Findings\n- [WARNING] foo.py:1 — real warning\nSummary: 0 CRITICAL, 1 WARNING across 1 files."
    )
    stderr = _invoke_main_on_ok(review)
    assert "Review notes (non-blocking warnings)" in stderr


def test_main_ok_banner_skips_prose_mention_of_warning_tag() -> None:
    """Regression: prose or reviewer commentary that merely contains the
    string `[WARNING]` must NOT trigger the non-blocking banner, because
    the anchored warning regex is now the single source of truth."""
    review = (
        "### Section 2 — Findings\n"
        "The arbiter treats [WARNING] lines as advisory per the prompt.\n"
        "No findings in this lens.\n"
        "Summary: 0 CRITICAL, 0 WARNING across 1 files."
    )
    stderr = _invoke_main_on_ok(review)
    assert "Review notes (non-blocking warnings)" not in stderr


def test_aggregate_lens_outputs_warning_count_uses_anchored_regex() -> None:
    """`_aggregate_lens_outputs` must count only finding-shaped warnings,
    not mid-line mentions, so the aggregate Summary line is consistent
    with what gets surfaced by `extract_warning_lines()`."""
    per_lens = [
        {
            "name": "bugs",
            "status": "ok",
            "review": (
                "### Section 2 — Bugs findings\n"
                "- [WARNING] real.py:1 — actual finding\n"
                "The arbiter treats [WARNING] lines as advisory.\n"
                "Summary: 0 CRITICAL, 1 WARNING across 1 files."
            ),
            "reviewer": "opencode",
            "error": "",
        },
    ]
    aggregated = _aggregate_lens_outputs(per_lens)
    # Exactly 1 warning counted, not 2.
    assert "1 WARNING" in aggregated.splitlines()[-1]


# ---------------------------------------------------------------------------
# Multi-backend orchestration — orchestrator.py + consolidation.py + stats.py
# ---------------------------------------------------------------------------


def test_assign_finding_ids_with_prefix_attaches_backend_label() -> None:
    """Multi-backend mode tags IDs as `<backend>-Fn` so the arbiter
    can disambiguate which reviewer flagged what."""
    review = "- [CRITICAL] foo.py:1 — bug\n- [CRITICAL] bar.py:2 — bug2"
    tagged, findings = assign_finding_ids(review, prefix="opencode")
    assert "[opencode-F1]" in tagged
    assert "[opencode-F2]" in tagged
    assert [f["id"] for f in findings] == ["opencode-F1", "opencode-F2"]


def test_assign_finding_ids_no_prefix_keeps_bare_ids() -> None:
    """N==1 (default) mode keeps the legacy bare F1/F2 IDs so existing
    log layout and arbiter regex stay byte-for-byte stable."""
    review = "- [CRITICAL] foo.py:1 — bug"
    tagged, findings = assign_finding_ids(review)
    assert "[F1]" in tagged
    assert findings[0]["id"] == "F1"


def test_critical_regex_accepts_prefixed_id() -> None:
    """count_criticals must recognise both `[F1]` and `[opencode-F1]`."""
    bare = "- [F1] [CRITICAL] foo.py:1 — bug"
    prefixed = "- [opencode-F1] [CRITICAL] foo.py:1 — bug"
    assert count_criticals(bare) == 1
    assert count_criticals(prefixed) == 1


def test_warning_regex_accepts_prefixed_id() -> None:
    """extract_warning_lines must accept the same prefixed ID shape."""
    prefixed = "- [claude-F2] [WARNING] bar.py:7 — note"
    assert extract_warning_lines(prefixed) == [prefixed.strip()]


def test_run_multi_backend_runs_two_primaries_in_parallel() -> None:
    """Two backends both produce results; orchestrator preserves PRIMARIES order."""
    import time

    from orchestrator import run_multi_backend

    a = RunnerConfig("opencode", "model-a")
    b = RunnerConfig("claude", "model-b")
    findings = "### Section 1\nfile audit\n### Section 2\n- [CRITICAL] f.py:1 — bug\nSummary: 1 CRITICAL across 1 file."

    barrier_seen: list[float] = []

    def slow_backend_run(system: str, user: str, model: str, timeout: int) -> tuple[str, str, int]:
        barrier_seen.append(time.monotonic())
        time.sleep(0.05)
        return findings, "", 0

    fake_a = MagicMock()
    fake_a.run.side_effect = slow_backend_run
    fake_b = MagicMock()
    fake_b.run.side_effect = slow_backend_run

    with (
        patch("orchestrator.PRIMARIES", [a, b]),
        patch("orchestrator.FALLBACK", None),
        patch("hook.applicable_lenses", return_value=["bugs"]),
        patch("hook.count_added_production_lines", return_value=10),
        patch.dict("hook.BACKENDS", {"opencode": fake_a, "claude": fake_b}, clear=False),
    ):
        results = run_multi_backend("diff", "f.py", False)

    assert [r.cfg for r in results] == [a, b]
    assert all(r.status == "ok" for r in results)
    # Parallel execution → both backends started within 50ms of each other.
    assert max(barrier_seen) - min(barrier_seen) < 0.05


def test_run_multi_backend_one_fails_no_fallback_at_n_gt_1() -> None:
    """When one of two primaries fails, fallback must NOT trigger
    (the other backend still produced output)."""
    from orchestrator import run_multi_backend

    a = RunnerConfig("opencode", "m1")
    b = RunnerConfig("claude", "m2")
    fb = RunnerConfig("opencode", "fallback-model")
    findings = "### Section 1\nx\n### Section 2\n- [CRITICAL] f.py:1 — bug\nSummary: 1 CRITICAL."

    fake_a = MagicMock()
    fake_a.run.side_effect = subprocess.TimeoutExpired(cmd="opencode", timeout=1)
    fake_b = MagicMock()
    fake_b.run.return_value = (findings, "", 0)
    fake_fb = MagicMock()
    fake_fb.run.return_value = ("MUST NOT BE CALLED", "", 0)

    with (
        patch("orchestrator.PRIMARIES", [a, b]),
        patch("orchestrator.FALLBACK", fb),
        patch("hook.applicable_lenses", return_value=["bugs"]),
        patch("hook.count_added_production_lines", return_value=10),
        patch.dict("hook.BACKENDS", {"opencode": fake_a, "claude": fake_b}, clear=False),
    ):
        results = run_multi_backend("diff", "f.py", False)

    assert len(results) == 2
    assert results[0].status == "timeout"
    assert results[1].status == "ok"
    fake_fb.run.assert_not_called()
    assert all(not r.fallback_used for r in results)


def test_run_multi_backend_all_fail_triggers_fallback() -> None:
    """When every primary fails, FALLBACK fires once as a safety net."""
    from orchestrator import run_multi_backend

    # Three distinct backend names so each MagicMock owns its own registry slot.
    a = RunnerConfig("alpha", "m1")
    b = RunnerConfig("beta", "m2")
    fb = RunnerConfig("gamma", "fb-model")
    findings = "### Section 1\nx\n### Section 2\nNo findings.\nSummary: 0 CRITICAL."

    fake_a = MagicMock()
    fake_a.run.return_value = ("", "boom-a", 1)
    fake_b = MagicMock()
    fake_b.run.return_value = ("", "boom-b", 1)
    fake_fb = MagicMock()
    fake_fb.run.return_value = (findings, "", 0)

    with (
        patch("orchestrator.PRIMARIES", [a, b]),
        patch("orchestrator.FALLBACK", fb),
        patch("hook.applicable_lenses", return_value=["bugs"]),
        patch("hook.count_added_production_lines", return_value=10),
        patch.dict("hook.BACKENDS", {"alpha": fake_a, "beta": fake_b, "gamma": fake_fb}, clear=False),
    ):
        results = run_multi_backend("diff", "f.py", False)

    fake_fb.run.assert_called_once()
    assert len(results) == 3
    assert results[-1].fallback_used is True
    assert results[-1].status == "ok"
    assert {r.cfg.backend for r in results[:-1]} == {"alpha", "beta"}
    assert all(r.status != "ok" for r in results[:-1])


def test_run_multi_backend_all_fail_no_fallback_returns_failures() -> None:
    """With FALLBACK=None and every primary failing, the result list contains
    only the failed primaries (caller upstream goes fail-open)."""
    from orchestrator import run_multi_backend

    a = RunnerConfig("opencode", "m1")
    fake_a = MagicMock()
    fake_a.run.return_value = ("", "stderr", 1)

    with (
        patch("orchestrator.PRIMARIES", [a]),
        patch("orchestrator.FALLBACK", None),
        patch("hook.applicable_lenses", return_value=["bugs"]),
        patch("hook.count_added_production_lines", return_value=10),
        patch.dict("hook.BACKENDS", {"opencode": fake_a}, clear=False),
    ):
        results = run_multi_backend("diff", "f.py", False)

    assert len(results) == 1
    assert results[0].status == "error"
    assert results[0].fallback_used is False


def test_total_failure_reason_describes_failures() -> None:
    """The fallback reason string must name every failed primary so the
    user can spot which backend is unreliable."""
    from orchestrator import BackendReviewResult, total_failure_reason

    cfg_a = RunnerConfig("opencode", "m1")
    cfg_b = RunnerConfig("claude", "m2")
    r_a = BackendReviewResult(
        cfg_a, "opencode", "timeout", "", [], None, "opencode timeout after 1200s", 0.0, 1.0, False
    )
    r_b = BackendReviewResult(cfg_b, "claude", "error", "", [], None, "claude rc=1", 0.0, 1.0, False)

    reason = total_failure_reason([r_a, r_b])
    assert reason is not None
    assert "opencode/m1" in reason
    assert "claude/m2" in reason
    assert "timeout" in reason


def test_consolidate_no_findings_skips_arbiter() -> None:
    """When no backend produced [CRITICAL] findings, arbiter is not invoked."""
    from consolidation import consolidate
    from orchestrator import BackendReviewResult

    cfg = RunnerConfig("opencode", "m")
    r = BackendReviewResult(cfg, "opencode", "ok", "no criticals here", [], None, None, 0.0, 1.0, False)

    cons = consolidate([r], "diff")
    assert cons.clusters == []
    assert cons.upheld_clusters == []
    assert cons.arbiter_status == "skipped_no_findings"


def test_consolidate_clusters_duplicates_via_arbiter() -> None:
    """Multi-backend arbiter groups findings from different backends into
    one cluster and applies a single verdict."""
    from consolidation import consolidate
    from orchestrator import BackendReviewResult

    a = RunnerConfig("opencode", "m1")
    b = RunnerConfig("claude", "m2")
    findings_a = [{"id": "opencode-F1", "line": "[opencode-F1] [CRITICAL] f.py:1 — same bug"}]
    findings_b = [{"id": "claude-F1", "line": "[claude-F1] [CRITICAL] f.py:1 — same bug"}]
    r_a = BackendReviewResult(a, "opencode", "ok", "x", findings_a, None, None, 0.0, 1.0, False)
    r_b = BackendReviewResult(b, "claude", "ok", "x", findings_b, None, None, 0.0, 1.0, False)

    arbiter_raw = (
        "[CLUSTER C1] opencode-F1, claude-F1\n"
        "[UPHELD] C1 — same defect\n"
        "Summary: 1 UPHELD, 0 OVERTURN, 1 clusters total."
    )

    with (
        patch("consolidation.PRIMARIES", [a, b]),
        patch("hook.read_file", return_value="arbiter prompt body"),
        patch("hook.run_reviewer", return_value=(arbiter_raw, "", 0)),
    ):
        cons = consolidate([r_a, r_b], "diff body")

    assert len(cons.clusters) == 1
    assert cons.clusters[0].cluster_id == "C1"
    assert set(cons.clusters[0].member_ids) == {"opencode-F1", "claude-F1"}
    assert set(cons.clusters[0].contributors) == {"opencode", "claude"}
    assert cons.upheld_clusters == cons.clusters


def test_parse_multi_arbiter_output_fail_open_for_missing_finding() -> None:
    """If the arbiter forgets to cluster a finding, it gets a singleton
    UPHELD cluster — fail-open: better to over-block than to drop a
    flagged defect."""
    from consolidation import parse_multi_arbiter_output

    findings = [
        {"id": "opencode-F1", "line": "[opencode-F1] [CRITICAL] a"},
        {"id": "claude-F1", "line": "[claude-F1] [CRITICAL] b"},
    ]
    raw = "[CLUSTER C1] opencode-F1\n[UPHELD] C1 — real bug\nSummary: 1 UPHELD, 0 OVERTURN, 1 clusters total."
    clusters = parse_multi_arbiter_output(raw, findings)

    assert len(clusters) == 2
    forgotten = [c for c in clusters if "claude-F1" in c.member_ids]
    assert len(forgotten) == 1
    assert forgotten[0].upheld is True


def test_parse_multi_arbiter_chunked_id_grammars() -> None:
    """Chunk-prefixed and whole-diff lens IDs cluster into the same shape as
    legacy IDs; chunk_id is set when every member shares the same chunk and
    None when the cluster mixes layers."""
    from consolidation import parse_multi_arbiter_output

    findings = [
        {"id": "models-opencode-F1", "line": "[models-opencode-F1] [CRITICAL] sale_order.py:142"},
        {"id": "models-claude-F1", "line": "[models-claude-F1] [CRITICAL] sale_order.py:142"},
        {"id": "wholediff-bugs-kimi-F1", "line": "[wholediff-bugs-kimi-F1] [CRITICAL] across files"},
    ]
    raw = (
        "[CLUSTER C1] models-opencode-F1, models-claude-F1\n"
        "[CLUSTER C2] wholediff-bugs-kimi-F1\n"
        "[UPHELD] C1 — chunked agreement\n"
        "[OVERTURN] C2 — theoretical\n"
        "Summary: 1 UPHELD, 1 OVERTURN, 2 clusters total."
    )
    clusters = parse_multi_arbiter_output(raw, findings)

    by_id = {c.cluster_id: c for c in clusters}
    assert by_id["C1"].chunk_id == "models"
    assert set(by_id["C1"].contributors) == {"opencode", "claude"}
    assert by_id["C1"].upheld is True
    assert by_id["C2"].chunk_id is None  # whole-diff layer is not chunked
    assert by_id["C2"].contributors == ("kimi",)
    assert by_id["C2"].upheld is False


def test_parse_multi_arbiter_synthetic_invariant() -> None:
    """Cross-chunk invariants the arbiter raises itself land as synthetic
    clusters with invariant_id set and a rationale-derived canonical line."""
    from consolidation import parse_multi_arbiter_output

    findings = [
        {"id": "models-opencode-F1", "line": "[models-opencode-F1] [CRITICAL] x"},
    ]
    raw = (
        "[CLUSTER C1] models-opencode-F1\n"
        "[CLUSTER C2] arbiter-INV1\n"
        "[UPHELD] C1 — real bug\n"
        "[UPHELD] C2 — invariant violated: every new field needs an ACL row\n"
        "Summary: 2 UPHELD, 0 OVERTURN, 2 clusters total.\n"
        "Chunked: 1 invariant-violations, 0 cross-chunk-overturns."
    )
    clusters = parse_multi_arbiter_output(raw, findings)

    by_id = {c.cluster_id: c for c in clusters}
    assert by_id["C2"].invariant_id == "arbiter-INV1"
    assert by_id["C2"].upheld is True
    # Rationale gets pulled into canonical_line so display is informative.
    assert "every new field needs an ACL row" in by_id["C2"].canonical_line
    # Synthetic IDs report "arbiter" as the contributor.
    assert by_id["C2"].contributors == ("arbiter",)


def test_parse_multi_arbiter_mixed_chunk_cluster_has_no_chunk_id() -> None:
    """If reviewers in different chunks both flag the same defect, the
    cluster's chunk_id is None — it spans chunks."""
    from consolidation import parse_multi_arbiter_output

    findings = [
        {"id": "models-opencode-F1", "line": "[models-opencode-F1] [CRITICAL] x"},
        {"id": "security-claude-F1", "line": "[security-claude-F1] [CRITICAL] y"},
    ]
    raw = (
        "[CLUSTER C1] models-opencode-F1, security-claude-F1\n"
        "[UPHELD] C1 — same mechanism in both chunks\n"
        "Summary: 1 UPHELD, 0 OVERTURN, 1 clusters total."
    )
    clusters = parse_multi_arbiter_output(raw, findings)
    assert len(clusters) == 1
    assert clusters[0].chunk_id is None
    assert set(clusters[0].contributors) == {"opencode", "claude"}


def test_parse_multi_arbiter_output_duplicate_cluster_last_writer_wins() -> None:
    """Repeated [CLUSTER C<n>] lines: the LAST membership list wins and
    previous-list IDs are removed from the cluster (seen.discard)."""
    from consolidation import parse_multi_arbiter_output

    findings = [
        {"id": "F1", "line": "[F1] [CRITICAL] a.py:1 — bug"},
        {"id": "F2", "line": "[F2] [CRITICAL] a.py:2 — bug"},
        {"id": "F3", "line": "[F3] [CRITICAL] a.py:3 — bug"},
    ]
    raw = (
        "[CLUSTER C1] F1, F2\n"
        "[CLUSTER C1] F2, F3\n"
        "[UPHELD] C1 — real bug\n"
        "Summary: 1 UPHELD, 0 OVERTURN, 1 clusters total."
    )
    clusters = parse_multi_arbiter_output(raw, findings)
    by_id = {c.cluster_id: c for c in clusters}
    assert set(by_id["C1"].member_ids) == {"F2", "F3"}
    # F1 was discarded from C1 and gets its own singleton fail-open cluster.
    singletons = [c for c in clusters if "F1" in c.member_ids]
    assert len(singletons) == 1
    assert singletons[0].upheld is True


def test_consolidate_arbiter_failure_upholds_everything() -> None:
    """Arbiter timeout/missing prompt must fail-open: every finding is
    upheld in a singleton cluster (consistent with legacy behavior)."""
    from consolidation import consolidate
    from orchestrator import BackendReviewResult

    a = RunnerConfig("opencode", "m1")
    b = RunnerConfig("claude", "m2")
    findings_a = [{"id": "opencode-F1", "line": "[opencode-F1] [CRITICAL] f.py — x"}]
    findings_b = [{"id": "claude-F1", "line": "[claude-F1] [CRITICAL] g.py — y"}]
    r_a = BackendReviewResult(a, "opencode", "ok", "x", findings_a, None, None, 0.0, 1.0, False)
    r_b = BackendReviewResult(b, "claude", "ok", "x", findings_b, None, None, 0.0, 1.0, False)

    with (
        patch("consolidation.PRIMARIES", [a, b]),
        patch("hook.read_file", return_value="prompt"),
        patch("hook.run_reviewer", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)),
    ):
        cons = consolidate([r_a, r_b], "diff body")

    assert cons.arbiter_status == "timeout"
    assert len(cons.clusters) == 2
    assert all(c.upheld for c in cons.clusters)


def test_build_run_stats_schema_shape() -> None:
    """Stats object must contain the documented top-level keys + per-backend
    metrics so jq queries written against the schema keep working."""
    from consolidation import ConsolidationResult, FindingCluster
    from orchestrator import BackendReviewResult
    from stats import DiffStats, build_run_stats

    cfg = RunnerConfig("opencode", "m1")
    findings = [{"id": "opencode-F1", "line": "[opencode-F1] [CRITICAL] f.py — bug"}]
    r = BackendReviewResult(cfg, "opencode", "ok", "review body", findings, None, None, 0.0, 12.5, False)

    cluster = FindingCluster("C1", ("opencode-F1",), ("opencode",), findings[0]["line"], upheld=True)
    cons = ConsolidationResult(
        clusters=[cluster],
        upheld_clusters=[cluster],
        arbiter_status="ran",
        arbiter_error=None,
        arbiter_raw_output="raw",
        arbiter_started_at=0.0,
        arbiter_ended_at=2.0,
    )

    obj = build_run_stats(
        [r],
        cons,
        DiffStats(total_lines=10, added_prod_lines=5, files_count=1),
        verdict="BLOCK",
        project="proj",
        timestamp="2026-05-01T10:00:00",
    )
    assert obj["schema_version"] == 1
    assert obj["verdict"] == "BLOCK"
    assert obj["project"] == "proj"
    assert obj["diff"] == {"total_lines": 10, "added_prod_lines": 5, "files_count": 1}
    assert obj["fallback"] == {"triggered": False, "reason": None}
    assert len(obj["backends"]) == 1
    b = obj["backends"][0]
    assert b["upheld_findings"] == 1
    assert b["overturned_findings"] == 0
    assert b["solo_findings"] == 1
    assert b["consensus_findings"] == 0
    assert b["duration_seconds"] == 12.5


def test_save_stats_writes_sidecar_and_appends_jsonl(tmp_path: Path) -> None:
    """Two consecutive saves → two lines in stats.jsonl + two sidecar files."""
    import json as _json

    from stats import save as save_stats

    md1 = tmp_path / "2026-05-01_a_OK.md"
    md1.write_text("placeholder")
    md2 = tmp_path / "2026-05-01_b_BLOCK.md"
    md2.write_text("placeholder")
    aggregate = tmp_path / "stats.jsonl"

    obj1 = {"schema_version": 1, "verdict": "OK"}
    obj2 = {"schema_version": 1, "verdict": "BLOCK"}
    save_stats(obj1, md1, aggregate)
    save_stats(obj2, md2, aggregate)

    assert (tmp_path / "2026-05-01_a_OK.stats.json").exists()
    assert (tmp_path / "2026-05-01_b_BLOCK.stats.json").exists()
    lines = aggregate.read_text().strip().splitlines()
    assert len(lines) == 2
    assert _json.loads(lines[0])["verdict"] == "OK"
    assert _json.loads(lines[1])["verdict"] == "BLOCK"


def test_n1_backwards_compat_markdown_omits_multi_sections(tmp_path: Path) -> None:
    """At N==1 the markdown log must NOT grow new per-backend / consolidation
    sections. The user explicitly required byte-for-byte preservation of the
    classic single-backend log layout — this snapshot pins it.
    """
    from consolidation import ConsolidationResult, FindingCluster
    from hook import _persist_run
    from orchestrator import BackendReviewResult

    cfg = RunnerConfig("opencode", "m1")
    findings = [{"id": "F1", "line": "[F1] [CRITICAL] foo.py:1 — bug"}]
    r = BackendReviewResult(
        cfg,
        "opencode",
        "ok",
        "review body with finding\n[F1] [CRITICAL] foo.py:1 — bug",
        findings,
        None,
        None,
        0.0,
        1.0,
        False,
    )
    cluster = FindingCluster("C1", ("F1",), ("",), findings[0]["line"], upheld=True)
    cons = ConsolidationResult(
        clusters=[cluster],
        upheld_clusters=[cluster],
        arbiter_status="ran",
        arbiter_error=None,
        arbiter_raw_output="raw",
        arbiter_started_at=0.0,
        arbiter_ended_at=1.0,
    )

    captured: dict[str, object] = {}

    def fake_save_log(*args: object, **kwargs: object) -> Path | None:
        captured.update(kwargs)
        return tmp_path / "fake.md"

    def fake_save_stats(*args: object, **kwargs: object) -> None:
        return None

    with (
        patch("hook.save_log", side_effect=fake_save_log),
        patch("hook.LOG_DIR", tmp_path),
        patch("stats.save", side_effect=fake_save_stats),
    ):
        _persist_run("BLOCK", "foo.py", "diff body", "display body", [r], cons)

    # Backwards compat: at N==1, neither section is produced.
    assert captured.get("per_backend") is None, "N==1 must not emit a per-backend section in the markdown log"
    assert captured.get("consolidation") is None, "N==1 must not emit a consolidation section in the markdown log"

    # And the resulting markdown body confirms no new section headers leak in.
    body = "\n".join(
        _invoke_real_save_log_sections(
            verdict="BLOCK",
            files="foo.py",
            diff="diff body",
            review="display body",
            reviewer="opencode+arbiter",
            per_backend=None,
            consolidation=None,
        )
    )
    assert "## Per-backend results" not in body
    assert "## Consolidation" not in body
    # Sanity: existing sections are still there.
    assert "## Diff stats" in body
    assert "## Review output" in body


def _invoke_real_save_log_sections(**kwargs: object) -> list[str]:
    """Helper: drive `_build_log_sections` with a fixed timestamp/project."""
    from hook import _build_log_sections

    return _build_log_sections(
        project="proj",
        timestamp="2026-05-01_10-00-00",
        verdict=kwargs.get("verdict", "OK"),  # type: ignore[arg-type]
        files=kwargs.get("files", ""),  # type: ignore[arg-type]
        diff=kwargs.get("diff", ""),  # type: ignore[arg-type]
        review=kwargs.get("review", ""),  # type: ignore[arg-type]
        error_msg=kwargs.get("error_msg"),  # type: ignore[arg-type]
        diag=kwargs.get("diag"),  # type: ignore[arg-type]
        reviewer=kwargs.get("reviewer"),  # type: ignore[arg-type]
        per_lens=kwargs.get("per_lens"),  # type: ignore[arg-type]
        arbiter=kwargs.get("arbiter"),  # type: ignore[arg-type]
        per_backend=kwargs.get("per_backend"),  # type: ignore[arg-type]
        consolidation=kwargs.get("consolidation"),  # type: ignore[arg-type]
    )


def test_n_gt_1_markdown_includes_multi_sections() -> None:
    """At N>1 the markdown log must grow the per-backend + consolidation
    sections so the user can audit each reviewer's contribution."""
    from consolidation import ConsolidationResult, FindingCluster
    from orchestrator import BackendReviewResult

    cfg_a = RunnerConfig("opencode", "m1")
    cfg_b = RunnerConfig("claude", "m2")
    r_a = BackendReviewResult(cfg_a, "opencode", "ok", "alpha review", [], None, None, 0.0, 1.0, False)
    r_b = BackendReviewResult(cfg_b, "claude", "ok", "beta review", [], None, None, 0.0, 1.5, False)
    cluster = FindingCluster("C1", ("opencode-F1", "claude-F1"), ("opencode", "claude"), "shared bug", upheld=True)
    cons = ConsolidationResult(
        clusters=[cluster],
        upheld_clusters=[cluster],
        arbiter_status="ran",
        arbiter_error=None,
        arbiter_raw_output="r",
        arbiter_started_at=0.0,
        arbiter_ended_at=2.0,
    )

    body = "\n".join(
        _invoke_real_save_log_sections(
            verdict="BLOCK",
            files="foo.py",
            diff="diff body",
            review="display body",
            reviewer="opencode+claude+arbiter",
            per_backend=[r_a, r_b],
            consolidation=cons,
        )
    )
    assert "## Per-backend results" in body
    assert "## Consolidation" in body
    assert "opencode/m1" in body
    assert "claude/m2" in body


def test_aggregate_per_backend_computes_consensus_and_solo_rates() -> None:
    """The CLI summary view depends on these aggregates being consistent."""
    from stats import aggregate_per_backend

    rows = [
        {
            "backends": [
                {
                    "config": {"backend": "opencode", "model": "m1"},
                    "status": "ok",
                    "duration_seconds": 10.0,
                    "raw_findings": {"critical": 2, "warning": 0},
                    "consensus_findings": 1,
                    "solo_findings": 1,
                    "upheld_findings": 1,
                    "overturned_findings": 1,
                },
            ],
        },
        {
            "backends": [
                {
                    "config": {"backend": "opencode", "model": "m1"},
                    "status": "ok",
                    "duration_seconds": 30.0,
                    "raw_findings": {"critical": 4, "warning": 0},
                    "consensus_findings": 2,
                    "solo_findings": 2,
                    "upheld_findings": 3,
                    "overturned_findings": 1,
                },
            ],
        },
    ]
    out = aggregate_per_backend(rows)
    key = "opencode/m1"
    assert key in out
    m = out[key]
    assert m["runs"] == 2
    assert m["ok_count"] == 2
    assert m["success_rate"] == 1.0
    assert m["upheld_total"] == 4
    assert m["overturned_total"] == 2
    # 3/(3+1)=0.75, plus prior run 1/(1+1)=0.5 → combined upheld_rate = 4/6 ≈ 0.667
    assert abs(m["upheld_rate"] - 4 / 6) < 1e-3


# ---------------------------------------------------------------------------
# Chunked-path dispatch in main()
# ---------------------------------------------------------------------------


def test_check_staged_review_guard_blocks_when_review_is_staged() -> None:
    """A staged ``.review/`` path is a writer mistake — block the commit
    before anything else."""
    with (
        patch("hook.subprocess.check_output", return_value=".review/findings.json\nsrc/foo.py\n"),
        patch("hook.error") as mock_err,
        patch("hook.save_log") as mock_log,
    ):
        try:
            _check_staged_review_guard()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit")
    assert mock_err.called
    assert ".review/" in mock_err.call_args[0][0]
    assert mock_log.call_args[0][0] == "BLOCK"


def test_check_staged_review_guard_passes_clean_diff() -> None:
    with patch("hook.subprocess.check_output", return_value="src/foo.py\nsrc/bar.py\n"):
        # Should NOT raise / exit.
        _check_staged_review_guard()


def test_maybe_dispatch_chunked_skips_when_below_threshold() -> None:
    """Below threshold, the chunked path is bypassed and ``main()`` falls
    through to the existing flow regardless of manifest presence."""
    from hook import _maybe_dispatch_chunked

    small_diff = "diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -0,0 +1,5 @@\n+a\n+b\n+c\n+d\n+e\n"
    with (
        patch("hook.get_staged_diff", return_value=(small_diff, None)),
        patch("hook.count_added_production_lines", return_value=MAX_PROD_LINES - 1),
        patch("chunked.manifest_present", return_value=False),
    ):
        # Below threshold the function returns None (no sys.exit) so main()
        # falls through to the single-call path. A regression that routed to
        # the chunked path here would raise SystemExit instead.
        assert _maybe_dispatch_chunked() is None


def test_maybe_dispatch_chunked_auto_scaffolds_when_no_manifest_and_big() -> None:
    """At/above threshold without a manifest the hook auto-scaffolds and exits 1."""
    from hook import _maybe_dispatch_chunked

    big_diff = "diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -0,0 +1,400 @@\n" + "\n".join(f"+line_{i}" for i in range(400))
    with (
        patch("hook.get_staged_diff", return_value=(big_diff, None)),
        patch("hook.count_added_production_lines", return_value=MAX_PROD_LINES + 100),
        patch("chunked.manifest_present", return_value=False),
        patch("scripts.scaffold_manifest.build_scaffold", return_value="scaffold-yaml"),
        patch("scripts.scaffold_manifest.write_scaffold", return_value=Path("/tmp/.review/manifest.yaml")),
        patch("hook.save_log"),
        patch("hook.error"),
    ):
        try:
            _maybe_dispatch_chunked()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit=1 from auto-scaffold")


def test_maybe_dispatch_chunked_takes_chunked_path_at_exact_threshold() -> None:
    """n_prod == MAX_PROD_LINES must reach the manifest check (chunked path),
    not return early."""
    from hook import _maybe_dispatch_chunked

    diff = "diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -0,0 +1,300 @@\n" + "\n".join(f"+line_{i}" for i in range(300))
    with (
        patch("hook.get_staged_diff", return_value=(diff, None)),
        patch("hook.count_added_production_lines", return_value=MAX_PROD_LINES),
        patch("chunked.manifest_present", return_value=False),
        patch("scripts.scaffold_manifest.build_scaffold", return_value="scaffold-yaml"),
        patch("scripts.scaffold_manifest.write_scaffold", return_value=Path("/tmp/.review/manifest.yaml")),
        patch("hook.save_log"),
        patch("hook.error"),
    ):
        try:
            _maybe_dispatch_chunked()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit=1 from auto-scaffold at exact threshold")


def test_maybe_dispatch_chunked_runs_chunked_when_threshold_and_manifest() -> None:
    from hook import _maybe_dispatch_chunked

    big_diff = "diff --git a/x.py b/x.py\n"
    fake_result = MagicMock()
    fake_result.status = "ok"
    fake_result.upheld_clusters = []
    fake_result.clusters = []
    fake_result.job_results = []
    fake_result.metrics = {"wall_clock_seconds": 1.5}
    fake_result.arbiter_status = "ran"
    fake_result.blocking_text = "ok"

    with (
        patch("hook.get_staged_diff", return_value=(big_diff, None)),
        patch("hook.count_added_production_lines", return_value=MAX_PROD_LINES + 50),
        patch("chunked.manifest_present", return_value=True),
        patch("hook.get_staged_files", return_value="x.py"),
        patch("chunked.run_chunked_review", return_value=fake_result),
        patch("chunked.write_artifacts", return_value=Path("/tmp/.review")),
        patch("hook.save_log"),
    ):
        try:
            _maybe_dispatch_chunked()
        except SystemExit as e:
            # Empty upheld → exit 0
            assert e.code == 0
        else:
            raise AssertionError("expected SystemExit from chunked path")


def test_maybe_dispatch_chunked_blocks_on_upheld_clusters() -> None:
    from hook import _maybe_dispatch_chunked

    fake_result = MagicMock()
    fake_result.status = "ok"
    fake_result.upheld_clusters = [MagicMock()]
    fake_result.clusters = [MagicMock()]
    fake_result.job_results = []
    fake_result.metrics = {"wall_clock_seconds": 1.0}
    fake_result.arbiter_status = "ran"
    fake_result.blocking_text = "BLOCK"

    with (
        patch("hook.get_staged_diff", return_value=("diff --git a/x.py b/x.py\n", None)),
        patch("hook.count_added_production_lines", return_value=MAX_PROD_LINES + 100),
        patch("chunked.manifest_present", return_value=True),
        patch("hook.get_staged_files", return_value="x.py"),
        patch("chunked.run_chunked_review", return_value=fake_result),
        patch("chunked.write_artifacts", return_value=Path("/tmp/.review")),
        patch("hook.save_log"),
        patch("hook.error"),
    ):
        try:
            _maybe_dispatch_chunked()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit=1 on BLOCK")


def test_maybe_dispatch_chunked_blocks_on_invalid_manifest() -> None:
    from hook import _maybe_dispatch_chunked

    fake_result = MagicMock()
    fake_result.status = "manifest_invalid"
    fake_result.blocking_text = "manifest validation failed (1 error(s)):\n  - stale_hash..."
    fake_result.validation = MagicMock()

    with (
        patch("hook.get_staged_diff", return_value=("diff", None)),
        patch("hook.count_added_production_lines", return_value=MAX_PROD_LINES + 100),
        patch("chunked.manifest_present", return_value=True),
        patch("hook.get_staged_files", return_value="x.py"),
        patch("chunked.run_chunked_review", return_value=fake_result),
        patch("chunked.write_artifacts"),
        patch("hook.save_log"),
        patch("hook.error"),
    ):
        try:
            _maybe_dispatch_chunked()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit=1 on manifest_invalid")


# ---------------------------------------------------------------------------
# C7 — OSError during write_artifacts must not fail-open
# ---------------------------------------------------------------------------


def test_chunked_path_blocks_even_when_write_artifacts_fails() -> None:
    """C7: OSError during write_artifacts must not prevent BLOCK verdict."""
    import chunked as chunked_mod
    import consolidation as cons_mod

    cluster = cons_mod.FindingCluster(
        cluster_id="C1",
        member_ids=("F1",),
        contributors=("fake",),
        canonical_line="- [F1] [CRITICAL] x",
        upheld=True,
        chunk_id=None,
        invariant_id=None,
    )
    result = chunked_mod.ChunkedResult(
        status="ok",
        validation=chunked_mod.ValidationResult(),
        job_results=[],
        arbiter_raw="",
        arbiter_status="ran",
        arbiter_error=None,
        clusters=[cluster],
        upheld_clusters=[cluster],
        blocking_text="1 BLOCKING",
        findings_json_text="{}",
        metrics={},
        started_at=0.0,
        ended_at=1.0,
    )

    with (
        patch("chunked.run_chunked_review", return_value=result),
        patch("chunked.write_artifacts", side_effect=OSError("disk full")),
        patch("hook.info"),
        patch("hook.error"),
        patch("hook.save_log"),
    ):
        verdict = _run_chunked_path("diff", "files", False)

    assert verdict == "BLOCK"


# ---------------------------------------------------------------------------
# C13 — tighten .review/ guard to only accidental working artifacts
# ---------------------------------------------------------------------------


def test_check_staged_review_guard_allows_manifest_yaml() -> None:
    """C13: manifest.yaml is intentional and must be allowed."""
    out = ".review/manifest.yaml\n"
    with patch("subprocess.check_output", return_value=out):
        _check_staged_review_guard()


def test_check_staged_review_guard_rejects_findings_json() -> None:
    """C13: findings.json is a working artifact and must be rejected."""
    out = ".review/findings.json\n"
    with (
        patch("subprocess.check_output", return_value=out),
        patch("hook.error"),
        patch("hook.save_log"),
    ):
        try:
            _check_staged_review_guard()
        except SystemExit as exc:
            assert exc.code == 1
            return
    raise AssertionError("findings.json should be rejected")


def test_check_staged_review_guard_rejects_state_files() -> None:
    """C13: state/ artifacts are working files and must be rejected."""
    out = ".review/state/00_validation.json\n"
    with (
        patch("subprocess.check_output", return_value=out),
        patch("hook.error"),
        patch("hook.save_log"),
    ):
        try:
            _check_staged_review_guard()
        except SystemExit as exc:
            assert exc.code == 1
            return
    raise AssertionError("state/ files should be rejected")


def test_coverage_gate_config_default_enabled() -> None:
    """`CoverageGateConfig` is the kill-switch dataclass for the gate.
    Default state is enabled=True (opt-out semantics).
    """
    from config import CoverageGateConfig

    assert CoverageGateConfig().enabled is True


def test_coverage_gate_config_can_be_disabled() -> None:
    from config import CoverageGateConfig

    cfg = CoverageGateConfig(enabled=False)
    assert cfg.enabled is False

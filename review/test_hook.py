from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

from backends import BACKENDS, Backend, ClaudeBackend, OpencodeBackend, _build_registry
from config import FANOUT_THRESHOLD, MAX_PROD_LINES, RunnerConfig
from hook import (
    ARBITER,
    LENS_APPLICABILITY,
    LENS_NAMES,
    _aggregate_lens_outputs,
    _render_fanout_output,
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
    with patch("hook.PRIMARY", bogus):
        try:
            _verify_runner_configs()
        except ValueError as exc:
            assert "PRIMARY" in str(exc)
            assert "bogus-backend" in str(exc)
            return
    raise AssertionError("expected ValueError on invalid PRIMARY backend")


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
    with patch("hook.PRIMARY", bogus):
        try:
            _verify_runner_configs()
        except ValueError as exc:
            msg = str(exc)
            assert "ghost" in msg
            assert "must be one of" in msg
            for name in BACKENDS:
                assert name in msg, f"registered backend {name!r} missing from error message"
            return
    raise AssertionError("expected ValueError when PRIMARY backend not in BACKENDS")


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
    # Default config has FALLBACK=claude/sonnet, so the timeout label points
    # at the backend that finally timed out — claude.
    assert result["reviewer"] == "claude"


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
    # PRIMARY.backend (default config: opencode), since FALLBACK is None.
    assert result["reviewer"] == "opencode"
    assert "opencode" in result["error"]


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
    assert "Split" in result
    assert str(MAX_PROD_LINES) in result
    assert "production-code" in result


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


def test_opencode_backend_empty_system_prompt() -> None:
    json_output = '{"type":"text","part":{"type":"text","text":"done"}}\n'
    mock_result = MagicMock()
    mock_result.stdout = json_output
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("backends.subprocess.run", return_value=mock_result) as mock_run:
        OpencodeBackend().run("", "user only", "github-copilot/gpt-5.4", 1200)

    cmd = mock_run.call_args[0][0]
    assert cmd[-1] == "user only"


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


def test_save_log_diff_stats_includes_prod_count(tmp_path) -> None:
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
    assert FANOUT_THRESHOLD == 100
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

    buf = io.StringIO()
    with (
        patch("hook.collect_diff", return_value=("diff-body", "foo.py", False)),
        patch("hook.run_review", return_value=(review_text, "OK")),
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

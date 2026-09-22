"""Reviewer backend registry.

To add a new tool:

    1. Subclass ``Backend`` below.
    2. Implement ``run(system_prompt, user_prompt, model, timeout)``.
       The method is responsible for building the CLI argv, choosing
       how to deliver the system+user prompt (flag, argv, stdin, env),
       invoking the CLI via subprocess, and parsing stdout into the
       reviewer-text contract used by ``review/hook.py``.
       Return ``(stdout_text, stderr, returncode)``. ``run`` must be
       thread-safe — see the ``Backend.run`` docstring for the contract.
    3. Pass an instance to the ``_build_registry(...)`` call at the
       bottom of this file. The helper raises ``ValueError`` at import
       time if two backends declare the same ``name``, so a copy-paste
       that forgets to rename ``name`` fails loudly instead of
       silently shadowing.

That is the entire recipe. ``review/config.py`` and ``review/hook.py``
do not change. ``CodexBackend`` below is the most recent worked example.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod


class Backend(ABC):
    """One reviewer CLI: argv shape, IO method, stdout parsing."""

    name: str  # config-facing identifier; must match RunnerConfig.backend

    @abstractmethod
    def run(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout: int,
    ) -> tuple[str, str, int]:
        """Invoke the CLI. Return (review_text, stderr, returncode).

        Must be thread-safe: ``hook.run_fanout`` calls the same Backend
        instance from multiple worker threads concurrently. Keep all
        mutable state inside ``run`` locals; do not stash request-scoped
        data on ``self``.
        """

    def selfcheck(self) -> str | None:
        """Why this backend cannot produce a *trustworthy* review, or None.

        Not a liveness check — it guards the failure mode where a CLI
        happily returns rc=0 and plausible prose while its file-reading
        tools were silently dead, which no caller can detect from the
        return values. Default: no check, for backends whose failures are
        visible in ``run``'s return tuple.

        Must be cheap (no API call) and thread-safe; implementations
        should cache, since ``run`` may call this from many threads.
        """
        return None


class OpencodeBackend(Backend):
    name = "opencode"

    def run(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout: int,
    ) -> tuple[str, str, int]:
        full_prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
        # `--agent pre-commit-reviewer` pins opencode to a read-only tool set
        # (read/list/glob/grep) defined in ~/.config/opencode/agent/pre-commit-reviewer.md.
        # Without it opencode runs as the default `build` agent with full bash + edit
        # access and routinely mutates the index (`git add` of untracked files,
        # `git stash` round-trips that leave missing-blob refs) during diff
        # investigation. See ~/.claude/git-hooks/pre-commit for the corresponding
        # write-tree/read-tree backstop.
        #
        # Prompt is piped via stdin, not argv: chunked-path prompts (cached
        # block + manifest + diff) routinely exceed Linux ARG_MAX (~128KB)
        # and would fail with `[Errno 7] Argument list too long`. opencode
        # treats stdin as the user message when no positional `message` is
        # given.
        cmd = [
            "opencode",
            "run",
            "--pure",
            "--agent",
            "pre-commit-reviewer",
            "--model",
            model,
            "--format",
            "json",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=full_prompt,
        )
        review = self._parse_json(result.stdout) if result.stdout else ""
        return review, result.stderr.strip(), result.returncode

    @staticmethod
    def _parse_json(raw: str) -> str:
        """Extract concatenated text events from opencode --format json."""
        parts: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "text":
                text = event.get("part", {}).get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts) if parts else ""


class ClaudeBackend(Backend):
    name = "claude"

    def run(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout: int,
    ) -> tuple[str, str, int]:
        cmd = [
            "claude",
            "-p",
            "--model",
            model,
            "--no-session-persistence",
            "--tools",
            "Read,Grep,Glob",
            "--output-format",
            "text",
        ]
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        result = subprocess.run(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode


class KimiBackend(Backend):
    name = "kimi"

    @staticmethod
    def _parse_stream_json(raw: str) -> str:
        """Extract the reviewer's text from kimi-code ``--output-format stream-json``.

        Each stdout line is one JSON object tagged by ``role``:
          * ``{"role":"assistant","tool_calls":[...]}`` → a tool call, skip
          * ``{"role":"assistant","content":"..."}``    → reviewer text, keep
          * ``{"role":"tool"|"meta", ...}``             → tool result / footer, skip
        Concatenate every assistant ``content`` string in order — a long review
        may arrive as several assistant messages — to reconstruct the full text.
        Lines that are not JSON (defensive) are ignored.
        """
        parts: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("role") != "assistant":
                continue
            content = event.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content)
        return "\n".join(parts).strip()

    def run(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout: int,
    ) -> tuple[str, str, int]:
        # Kimi has no --system-prompt flag → concatenate system + user the same
        # way OpencodeBackend does.
        #
        # kimi-code (0.24.1) dropped --quiet / --agent-file / --input-format and
        # the stdin prompt channel: `-p` takes the prompt as a single argv value,
        # and review prompts (cached block + manifest + full diff) routinely
        # exceed Linux MAX_ARG_STRLEN (~128KB per arg) → `[Errno 7] Argument list
        # too long`. So the full prompt is staged in a temp file and kimi is given
        # a short instruction to Read it; kimi pulls it in via its Read tool (the
        # same file-driven pattern the kimi skill uses). Verified: kimi Reads an
        # absolute /tmp path from the repo-root cwd without --add-dir.
        #
        # Read-only containment: KIMI_REVIEW_READONLY=1 arms
        # ~/.kimi/hooks/review_readonly.py (a PreToolUse deny hook) for THIS run
        # only — it denies Write/Edit/Bash/FetchURL/WebSearch, replacing the
        # kimi-cli --agent-file allowlist that kimi-code removed. `-p` runs under
        # the `auto` permission policy (auto-approves tool calls), so this hook is
        # what keeps the reviewer from mutating the tree or reaching the network.
        # Pairs with the write-tree/read-tree index backstop in
        # ~/.claude/git-hooks/pre-commit. Thread-safe: all state is local; mkstemp
        # gives each concurrent chunked-path call its own file.
        #
        # stream-json (not text): `--output-format text` interleaves `•` reasoning
        # lines with the answer and a resume footer — painful to parse. stream-json
        # is line-delimited JSON we split cleanly in _parse_stream_json.
        full_prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
        fd, prompt_path = tempfile.mkstemp(suffix=".md", prefix="kimi-review-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(full_prompt)
            instruction = (
                f"Read the file {prompt_path} in full using the Read tool. It contains a "
                "code-review system prompt followed by the diff to review. Follow those "
                "instructions exactly and output ONLY the review text in the format they "
                "specify. Do not modify, create, or delete any file."
            )
            cmd = [
                "kimi",
                "-p",
                instruction,
                "--model",
                model,
                "--output-format",
                "stream-json",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "KIMI_REVIEW_READONLY": "1"},
            )
            review = self._parse_stream_json(result.stdout) if result.stdout else ""
            return review, result.stderr.strip(), result.returncode
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass


# Cached result of CodexBackend's sandbox probe, shared across every thread and
# every CodexBackend instance in the process. ``None`` = not probed yet;
# ``(None,)`` = probed, healthy; ``("reason",)`` = probed, broken. The one-tuple
# distinguishes "healthy" from "unprobed" without a separate flag.
_CODEX_SANDBOX_CHECK: tuple[str | None] | None = None
_CODEX_SANDBOX_LOCK = threading.Lock()


class CodexBackend(Backend):
    name = "codex"

    # How much stdout to attach to stderr when codex produces no review text.
    # Long enough to carry a quota / auth / rate-limit message, short enough
    # not to bury the real stderr in the markdown log.
    _STDOUT_TAIL_CHARS = 2000

    # Extra `--enable` feature flags shared by `run` and `selfcheck`. Single
    # source of truth on purpose: if the probe and the real invocation drifted,
    # the probe would certify a sandbox the review never actually uses.
    #
    # Empty, and that is load-bearing history rather than a default. Codex
    # bundles a NON-setuid bwrap that needs unprivileged user namespaces, and
    # Ubuntu 24.04 blocks those (kernel.apparmor_restrict_unprivileged_userns=1).
    # Every sandbox start then died ("Failed RTM_NEWADDR" / "setting up uid map:
    # Permission denied"), so every Read/Grep/Glob failed and the reviewer graded
    # the diff blind — while still returning rc=0, clean stderr and a plausible
    # "0 findings". `--enable use_legacy_landlock` was the stopgap; it is now
    # unnecessary because /etc/apparmor.d/codex-bwrap grants `userns` to that one
    # binary, so the supported bwrap path works.
    #
    # Do NOT re-add the landlock flag as insurance: `codex features list` marks it
    # `deprecated`, and an unknown feature flag is a HARD error (`Error: Unknown
    # feature flag`, rc=1), so pinning it would turn a future codex upgrade into a
    # total backend failure. `selfcheck` is the guard instead — it catches the
    # AppArmor profile going stale (e.g. an npm update moving the bwrap path).
    _SANDBOX_FEATURE_ARGV: tuple[str, ...] = ()

    _SELFCHECK_MARKER = "CODEX_SANDBOX_OK"
    _SELFCHECK_TIMEOUT = 30  # local sandbox spawn; measured at ~0.11s

    def selfcheck(self) -> str | None:
        """Probe the codex sandbox once per process; cache the verdict.

        Guards the exact failure this backend hit in the wild: codex's default
        bubblewrap sandbox cannot start (see the comment in ``run``), so every
        Read/Grep/Glob fails and the reviewer silently degrades to a diff-only
        review — while still returning rc=0, clean stderr and a plausible
        "0 findings" verdict. Nothing in ``run``'s return tuple distinguishes
        that from a real review, so a probe is the only way to catch it.

        Costs no API call and ~0.11s, once per process.
        """
        global _CODEX_SANDBOX_CHECK
        with _CODEX_SANDBOX_LOCK:
            if _CODEX_SANDBOX_CHECK is None:
                _CODEX_SANDBOX_CHECK = (self._probe_sandbox(),)
            return _CODEX_SANDBOX_CHECK[0]

    @classmethod
    def _probe_sandbox(cls) -> str | None:
        """Run a trivial command inside the sandbox; None if it really ran.

        Asserts the marker reached stdout rather than trusting rc alone — that
        proves the sandboxed command actually executed, not merely that codex
        exited cleanly.
        """
        cmd = ["codex", "sandbox", *cls._SANDBOX_FEATURE_ARGV, "echo", cls._SELFCHECK_MARKER]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=cls._SELFCHECK_TIMEOUT)
        except FileNotFoundError:
            return "codex CLI not found on PATH"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"codex sandbox probe failed: {type(exc).__name__}: {exc}"
        if result.returncode == 0 and cls._SELFCHECK_MARKER in result.stdout:
            return None
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        first = detail[0] if detail else f"rc={result.returncode}"
        # Fix first, diagnosis second, raw detail last. orchestrator._invoke_single_call
        # logs only stderr[:200], so anything past that vanishes from the markdown log —
        # the actionable sentence has to be at the front to survive the cut.
        return (
            "codex sandbox dead — check /etc/apparmor.d/codex-bwrap still matches the "
            "bundled bwrap path (npm updates move it); it grants the userns Ubuntu "
            "blocks by default. Reviewer would run blind (all file tools fail, yet "
            f"rc=0 + plausible review). Detail: {first}"
        )

    @staticmethod
    def _read_last_message(path: str) -> str:
        """Read the agent's final message from ``--output-last-message``.

        A missing or empty file yields ``""`` — the caller treats that as a
        failed run (see ``run``). Never falls back to raw stdout: with
        ``--json`` that is an event stream, and with plain output a human
        transcript of reasoning steps; either one would pollute the reviewer
        text contract that ``hook.py`` regex-parses.
        """
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    @classmethod
    def _stdout_tail(cls, stdout: str) -> str:
        """Last ``_STDOUT_TAIL_CHARS`` of stdout, for failure diagnostics."""
        tail = stdout.strip()
        return tail[-cls._STDOUT_TAIL_CHARS :] if len(tail) > cls._STDOUT_TAIL_CHARS else tail

    def run(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout: int,
    ) -> tuple[str, str, int]:
        # Codex has no --system-prompt flag → concatenate system + user the same
        # way OpencodeBackend and KimiBackend do.
        #
        # Prompt is piped via stdin with NO positional argument: `codex exec`
        # reads instructions from stdin when no prompt is given. The documented
        # `-` spelling is deliberately avoided — if a codex version treats `-`
        # as a literal prompt, stdin is appended as a `<stdin>` block and the
        # effective instruction becomes "-" plus the diff, i.e. a silently
        # garbage review rather than a loud failure. Piping also sidesteps the
        # ARG_MAX ceiling that forced KimiBackend's temp-file staging: chunked
        # prompts (cached block + manifest + diff) exceed ~128KB routinely.
        #
        # Read-only containment: `--sandbox read-only` is codex's native policy
        # for model-generated shell commands — no writes, no network. This is
        # the counterpart to opencode's `--agent pre-commit-reviewer` pin and
        # kimi's KIMI_REVIEW_READONLY deny-hook, and it pairs with the
        # write-tree/read-tree index backstop in ~/.claude/git-hooks/pre-commit.
        # `-m` / `-s` / `--ephemeral` are pinned explicitly so a stray setting in
        # ~/.codex/config.toml cannot override them. --ignore-user-config is NOT
        # used: it would also discard provider/MCP settings the user relies on.
        #
        # Review text is read from the --output-last-message file rather than
        # parsed out of stdout — codex writes that file itself (CLI machinery,
        # not a sandboxed model command), so it needs no parser and cannot pick
        # up transcript noise. --json only shapes stdout, which is used for
        # diagnostics alone.
        #
        # Thread-safe: all state is local; mkstemp gives each concurrent
        # chunked-path call its own output file.
        # A dead sandbox yields a confident, blind review (see selfcheck), which
        # no caller can detect. Fail the run instead: an empty review + rc!=0 is
        # exactly what run_with_fallback expects, so the commit gets a real
        # review from FALLBACK plus a visible fallback banner, rather than a
        # fabricated "0 findings — OK".
        sandbox_broken = self.selfcheck()
        if sandbox_broken:
            return "", sandbox_broken, 1

        full_prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
        fd, out_path = tempfile.mkstemp(suffix=".md", prefix="codex-review-")
        os.close(fd)  # codex writes this path itself; we only need the name
        try:
            cmd = [
                "codex",
                "exec",
                "--model",
                model,
                "--sandbox",
                "read-only",
                # Sandbox feature flags, normally empty — see _SANDBOX_FEATURE_ARGV.
                # `selfcheck` probes this exact combination.
                *self._SANDBOX_FEATURE_ARGV,
                "--ephemeral",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--json",
                "--output-last-message",
                out_path,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=full_prompt,
            )
            review = self._read_last_message(out_path)
            stderr = result.stderr.strip()
            if not review:
                # An empty review makes run_with_fallback treat this as a failed
                # primary. Carry a stdout tail into stderr so the logged reason is
                # readable: codex reports quota/auth problems on stdout, and those
                # are the likeliest first failure on a subscription-gated tier.
                tail = self._stdout_tail(result.stdout)
                if tail:
                    stderr = f"{stderr}\n{tail}".strip()
            return review, stderr, result.returncode
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass


def _build_registry(*backends: Backend) -> dict[str, Backend]:
    """Build the BACKENDS registry, rejecting duplicate ``name`` values.

    A plain ``{b.name: b for ...}`` comprehension would last-write-wins on
    a name collision — a copy-paste mistake that forgets to rename
    ``name`` would silently shadow an existing backend, and
    ``_verify_runner_configs`` would still accept the key.
    """
    registry: dict[str, Backend] = {}
    for b in backends:
        if b.name in registry:
            raise ValueError(
                f"duplicate backend name: {b.name!r} "
                f"(both {type(registry[b.name]).__name__} and {type(b).__name__} claim it)"
            )
        registry[b.name] = b
    return registry


# Registry. The single source of truth for backend names. Adding a new
# backend = adding one entry here (plus the class above).
# review-note: Backend ABC over a Callable map is deliberate — each backend
# owns its parser as a method (OpencodeBackend._parse_json) and the ABC
# enforces the contract via test_backend_subclass_must_implement_run; a
# Callable dict has no equivalent guard.
BACKENDS: dict[str, Backend] = _build_registry(OpencodeBackend(), ClaudeBackend(), KimiBackend(), CodexBackend())

"""Reviewer backend registry.

To add a new tool (e.g. Codex CLI, Kimi):

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
do not change.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
BACKENDS: dict[str, Backend] = _build_registry(OpencodeBackend(), ClaudeBackend(), KimiBackend())

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
import subprocess
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
        cmd = [
            "opencode",
            "run",
            "--pure",
            "--model",
            model,
            "--format",
            "json",
            full_prompt,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
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
BACKENDS: dict[str, Backend] = _build_registry(OpencodeBackend(), ClaudeBackend())

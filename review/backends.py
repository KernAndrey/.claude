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
from pathlib import Path

_KIMI_AGENT_FILE = str(Path(__file__).parent / "agents" / "kimi-pre-commit-reviewer.yaml")


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


_KIMI_PREAMBLE = (
    "**Important context for this review run.** kimi is invoked from a "
    "directory that does not contain the diff's source files. The diff in "
    "the user prompt below is the complete, authoritative artefact to "
    "review — its hunks already include surrounding context lines. Do "
    "**not** call `ReadFile`, `Glob`, `Grep`, or `Shell` to look up the "
    "diff's files; they are not on this filesystem and the resulting tool "
    "calls will return empty/wrong results and waste your token budget. "
    "Treat the diff as the entire source of truth and produce the full "
    "review (Section 1, Section 2 across all lenses, Section 3) in a "
    "single response."
)


class KimiBackend(Backend):
    name = "kimi"

    def run(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout: int,
    ) -> tuple[str, str, int]:
        # Kimi has no --system-prompt flag → concatenate (same way
        # OpencodeBackend does). Stdin is not a documented path for --prompt,
        # so the combined prompt rides on argv; combined size is well under
        # ARG_MAX even at FANOUT_THRESHOLD.
        # Prepend a kimi-specific preamble that overrides the generic
        # "use Read/Grep/Glob" instruction in prompts/combined.md — kimi
        # runs from ~/.claude, not the user's repo, so filesystem lookups
        # are wrong-tree and a previous live smoke had kimi spend 768s
        # chasing missing files before truncating mid-review.
        # review-note: keeping the preamble in prompt text is a deliberate
        # kimi-specific cost optimization. Passing `cwd=<repo>` to subprocess
        # would let kimi reach the files, but combined.md tells reviewers to
        # use Read/Grep/Glob — kimi would then spend tokens browsing instead
        # of reviewing the diff (the 768s smoke incident above). The "shared
        # prompt contract" refactor across backends is out of scope here.
        system_prompt = f"{_KIMI_PREAMBLE}\n\n{system_prompt}" if system_prompt else _KIMI_PREAMBLE
        # `system_prompt` is non-empty after the line above (either the
        # original prefixed with the preamble, or the bare preamble), so an
        # `else user_prompt` branch here would be unreachable.
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        # `--quiet` is `--print --output-format text --final-message-only` —
        # gives clean stdout (just the final assistant reply) instead of the
        # event-stream dump `--print` alone produces. `--print` implicitly
        # enables --yolo (auto-approve all tool calls), so `--agent-file`
        # loads our restricted agent definition (no Shell, no write tools,
        # no network egress) — the only thing keeping the reviewer from
        # mutating the working tree or exfiltrating data during diff
        # investigation. Symmetric with `opencode --agent pre-commit-reviewer`
        # and pairs with the write-tree/read-tree backstop in
        # ~/.claude/git-hooks/pre-commit.
        cmd = [
            "kimi",
            "--quiet",
            "--model",
            model,
            "--agent-file",
            _KIMI_AGENT_FILE,
            "--prompt",
            full_prompt,
        ]
        result = subprocess.run(
            cmd,
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
BACKENDS: dict[str, Backend] = _build_registry(OpencodeBackend(), ClaudeBackend(), KimiBackend())

"""pytest conftest — makes `hook` importable from this directory.

Works whether pytest is invoked from ~/.claude/ (via `pytest review/`)
or from ~/.claude/review/ directly.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _reset_fallback_events() -> None:
    """Clear hook's process-global fallback accumulator before each test so
    banner state never leaks between tests. Production resets it naturally —
    each commit runs the hook in a fresh process."""
    try:
        import hook

        hook._FALLBACK_EVENTS.clear()
    except Exception:  # noqa: BLE001 — best-effort test hygiene
        pass


@pytest.fixture(autouse=True)
def _pin_codex_sandbox_healthy() -> Iterator[None]:
    """Pre-seed CodexBackend's sandbox verdict as healthy for every test.

    Without this, any test that reaches ``hook.main()`` (which calls
    ``_warn_on_unhealthy_backends``) or ``CodexBackend.run`` outside the
    dedicated helpers would shell out to a real ``codex sandbox`` probe — making
    the suite slow, environment-dependent, and different on a machine with no
    codex installed. Restores the previous value afterwards so the probe's own
    tests (which reset it to None deliberately) stay isolated.
    """
    try:
        import backends
    except Exception:  # noqa: BLE001 — best-effort test hygiene
        yield
        return

    saved = backends._CODEX_SANDBOX_CHECK
    backends._CODEX_SANDBOX_CHECK = (None,)
    try:
        yield
    finally:
        backends._CODEX_SANDBOX_CHECK = saved

"""pytest conftest — makes `hook` importable from this directory.

Works whether pytest is invoked from ~/.claude/ (via `pytest review/`)
or from ~/.claude/review/ directly.
"""

from __future__ import annotations

import sys
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

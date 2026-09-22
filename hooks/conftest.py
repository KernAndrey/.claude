from __future__ import annotations

import io
import json
from collections.abc import Callable

import pytest


StdinSetter = Callable[[object], None]


@pytest.fixture
def stdin_payload(monkeypatch: pytest.MonkeyPatch) -> StdinSetter:
    def _set(payload: object) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr("sys.stdin", io.StringIO(text))

    return _set

"""pytest conftest — makes `hook` importable from this directory.

Works whether pytest is invoked from ~/.claude/ (via `pytest review/`)
or from ~/.claude/review/ directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

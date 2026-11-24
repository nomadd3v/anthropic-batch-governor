"""Shared pytest configuration for the batch-governor test suite.

Sets ``asyncio_mode=auto`` through pytest-asyncio so every ``async def``
test function runs without needing explicit marks. Also wires the repo root
onto ``sys.path`` so ``import server.*`` resolves when the package is run
without ``pip install -e .``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

"""Shared pytest setup: make ``clockd`` and the firmware modules importable."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = REPO_ROOT / "controller"
PICO = REPO_ROOT / "firmware" / "pico"

for path in (CONTROLLER, PICO, REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

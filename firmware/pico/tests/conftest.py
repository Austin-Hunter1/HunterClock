"""Make the firmware modules importable the way the Pico imports them.

On the Pico, ``motion``, ``protocol`` and ``core`` sit at the filesystem root
and import each other by bare name.  These tests put ``firmware/pico`` on the
path so they exercise exactly that, rather than a package shape the Pico does
not have.
"""

from __future__ import annotations

import sys
from pathlib import Path

PICO_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PICO_DIR.parents[1]

for path in (str(PICO_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

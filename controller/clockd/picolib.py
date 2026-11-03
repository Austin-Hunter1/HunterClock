"""Locate the firmware's pure-Python modules and import them.

``firmware/pico/motion.py`` and ``firmware/pico/protocol.py`` are the single
source of truth for the planner and the wire format.  They live in the firmware
tree because that is what gets copied onto the Pico, where files sit flat in the
filesystem root and cannot be a Python package.  The controller, the tests and
the simulator all import them through here so there is never a second copy to
keep in step.

Resolution order:

1. ``$WEASLEY_PICO_LIB``, if set -- for odd deployments and for CI.
2. ``firmware/pico`` relative to this file's repo checkout (two parents up),
   which is how ``deploy/install.sh`` installs the controller (editable, from
   the repo), and how the tests run.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path


def firmware_dir() -> Path:
    """Return the directory holding the Pico's pure-Python modules.

    Raises:
        RuntimeError: when it cannot be found, with the paths that were tried.
    """
    tried: list[Path] = []
    override = os.environ.get("WEASLEY_PICO_LIB")
    if override:
        candidate = Path(override).expanduser()
        tried.append(candidate)
        if (candidate / "motion.py").is_file():
            return candidate
    # clockd/picolib.py -> clockd -> controller -> repo root
    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root / "firmware" / "pico"
    tried.append(candidate)
    if (candidate / "motion.py").is_file():
        return candidate
    raise RuntimeError(
        "cannot find firmware/pico (motion.py); tried: "
        + ", ".join(str(p) for p in tried)
        + ". Set WEASLEY_PICO_LIB to the directory holding motion.py."
    )


def _load(name: str):
    directory = str(firmware_dir())
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return importlib.import_module(name)


def load_protocol():
    """Import and return the firmware's ``protocol`` module."""
    return _load("protocol")


def load_motion():
    """Import and return the firmware's ``motion`` module."""
    return _load("motion")


protocol = load_protocol()
motion = load_motion()

__all__ = ["firmware_dir", "load_motion", "load_protocol", "motion", "protocol"]

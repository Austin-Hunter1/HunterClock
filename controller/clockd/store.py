"""Small durable store for the things that must survive a restart.

Three kinds of fact live here, all keyed by person slug:

* the last **accepted** fix, so a restart does not blank the dial;
* the per-hand **zero offset** and **verified** flag -- with no home sensors,
  "this hand is at 12" is knowledge that only a person standing at the clock can
  create, and losing it means sending someone back to the clock;
* the last commanded **step position**, which lets the controller recognise that
  a Pico which came back with the same position is probably still truthful.

A single JSON file under ``var/`` is plenty: six people, written at most every
few seconds.  Writes go to a temporary file and are renamed into place, so a
brownout mid-write leaves the previous good file rather than half of a new one.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

#: Bumped if the on-disk shape changes incompatibly.
SCHEMA_VERSION = 1


class Store:
    """Persistent controller state in one JSON file.

    Args:
        path: The file to keep it in, normally ``var/state.json``.
        autosave: Save on every mutation.  Tests turn this off and call
            :meth:`save` by hand.
    """

    def __init__(self, path: Path | str, *, autosave: bool = True):
        self.path = Path(path)
        self.autosave = autosave
        self.data: dict[str, Any] = {"version": SCHEMA_VERSION, "people": {}, "saved_at": None}
        self.load_error: str | None = None
        self.saves = 0
        self.load()

    # -- file I/O --------------------------------------------------------

    def load(self) -> None:
        """Read the file if it exists.

        A corrupt file is not fatal: it is renamed aside and the store starts
        empty, because refusing to boot would mean the clock stops entirely over
        a lost zero offset.  :attr:`load_error` records what happened so the UI
        can say so.
        """
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.load_error = f"{self.path} is unreadable ({exc}); starting with empty state"
            self._quarantine()
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("people"), dict):
            self.load_error = f"{self.path} does not hold clock state; starting empty"
            self._quarantine()
            return
        self.data = {
            "version": raw.get("version", SCHEMA_VERSION),
            "people": raw["people"],
            "saved_at": raw.get("saved_at"),
        }

    def _quarantine(self) -> None:
        with contextlib.suppress(OSError):
            self.path.rename(self.path.with_suffix(self.path.suffix + ".corrupt"))

    def save(self) -> None:
        """Write the state atomically."""
        self.data["saved_at"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, indent=2, sort_keys=True)
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed by the `with` below
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # os.replace is atomic on both POSIX and Windows.
            os.replace(handle.name, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise
        self.saves += 1

    def _touched(self) -> None:
        if self.autosave:
            self.save()

    # -- per-person records ----------------------------------------------

    def person(self, slug: str) -> dict[str, Any]:
        """The record for one person, created empty if new."""
        people = self.data.setdefault("people", {})
        return people.setdefault(slug, {})

    def get_fix(self, slug: str) -> dict[str, Any] | None:
        """The last accepted fix, or ``None``."""
        fix = self.person(slug).get("last_fix")
        return dict(fix) if isinstance(fix, dict) else None

    def set_fix(self, slug: str, fix: dict[str, Any]) -> None:
        """Record the last accepted fix."""
        self.person(slug)["last_fix"] = dict(fix)
        self._touched()

    def get_zero(self, slug: str) -> float:
        """The hand's zero offset in degrees (0.0 if never set)."""
        value = self.person(slug).get("zero_offset_deg", 0.0)
        return float(value) if isinstance(value, (int, float)) else 0.0

    def set_zero(self, slug: str, offset_deg: float) -> None:
        """Record a zero offset in degrees."""
        self.person(slug)["zero_offset_deg"] = float(offset_deg)
        self._touched()

    def is_verified(self, slug: str) -> bool:
        """Has a person confirmed where this hand physically points?"""
        return bool(self.person(slug).get("verified", False))

    def set_verified(self, slug: str, verified: bool, *, when: float | None = None) -> None:
        """Record that a hand's position was (or is no longer) confirmed."""
        record = self.person(slug)
        record["verified"] = bool(verified)
        record["verified_at"] = (when if when is not None else time.time()) if verified else None
        self._touched()

    def verified_at(self, slug: str) -> float | None:
        """When the hand was last confirmed, as a unix timestamp."""
        value = self.person(slug).get("verified_at")
        return float(value) if isinstance(value, (int, float)) else None

    def get_steps(self, slug: str) -> int | None:
        """The last known commanded microstep position."""
        value = self.person(slug).get("steps")
        return int(value) if isinstance(value, (int, float)) else None

    def set_steps(self, slug: str, steps: int) -> None:
        """Record the commanded microstep position."""
        self.person(slug)["steps"] = int(steps)
        self._touched()

    def set_many(self, slug: str, **fields: Any) -> None:
        """Update several fields in one write."""
        self.person(slug).update(fields)
        self._touched()

    def all_people(self) -> dict[str, dict[str, Any]]:
        """Every stored record, for the diagnostic bundle."""
        return {slug: dict(record) for slug, record in self.data.get("people", {}).items()}

    def forget(self, slug: str) -> None:
        """Drop one person's record entirely."""
        self.data.get("people", {}).pop(slug, None)
        self._touched()

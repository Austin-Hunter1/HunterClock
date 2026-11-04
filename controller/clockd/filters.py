"""Deciding which fixes to believe and when a hand is worth moving.

Two separate questions, kept separate on purpose:

:func:`accept_fix`
    Is this position report worth storing at all?  Phones send nonsense -- a
    5 km cell-tower fix, a point from two days ago flushed out of a queue after
    airplane mode, a duplicate.  Accepting one of those swings a hand across the
    dial for no reason.

:func:`should_move`
    Given that the target angle changed, is the change worth driving the motor
    for?  The hands are open loop with no encoders, so every move is a chance to
    lose a step, and six motors twitching every thirty seconds would be both
    noisy and pointless.
"""

from __future__ import annotations

import math
from typing import NamedTuple

from .config import Filtering
from .state import Fix


class Decision(NamedTuple):
    """The outcome of a filter check.

    Attributes:
        accept: Whether to go ahead.
        reason: A short stable slug, shown in the UI and counted per person.
        detail: Human-readable explanation for the log.
    """

    accept: bool
    reason: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.accept


#: Rejection reasons, so the UI can show a stable badge.
REASON_OK = "ok"
REASON_ACCURACY = "accuracy"
REASON_TOO_OLD = "too_old"
REASON_OUT_OF_ORDER = "out_of_order"
REASON_BAD_COORDS = "bad_coords"
REASON_DUPLICATE = "duplicate"

#: Move-suppression reasons.
REASON_DEADBAND = "deadband"
REASON_TOO_SOON = "too_soon"
REASON_NO_TARGET = "no_target"


def accept_fix(
    fix: Fix,
    last: Fix | None,
    filtering: Filtering,
    *,
    now: float | None = None,
) -> Decision:
    """Decide whether to accept a new fix.

    Args:
        fix: The incoming fix.
        last: The last fix that was accepted for this person, if any.
        filtering: The configured limits.
        now: Current unix time, for tests.

    Returns:
        A :class:`Decision`.  ``reason`` is :data:`REASON_OK` on acceptance.
    """
    import time as _time

    now = now if now is not None else _time.time()

    if not _finite(fix.lat) or not _finite(fix.lon):
        return Decision(False, REASON_BAD_COORDS, "lat/lon is not a finite number")
    if not -90.0 <= fix.lat <= 90.0 or not -180.0 <= fix.lon <= 180.0:
        return Decision(
            False, REASON_BAD_COORDS, f"lat/lon out of range: {fix.lat:.4f}, {fix.lon:.4f}"
        )

    if _finite(fix.accuracy_m) and fix.accuracy_m > filtering.max_accuracy_m:
        return Decision(
            False,
            REASON_ACCURACY,
            f"accuracy {fix.accuracy_m:.0f} m is worse than "
            f"the {filtering.max_accuracy_m:.0f} m limit",
            )

    age = now - fix.ts
    if age > filtering.max_age_s:
        return Decision(
            False,
            REASON_TOO_OLD,
            f"fix is {age / 3600.0:.1f} h old, limit is {filtering.max_age_s / 3600.0:.1f} h",
        )

    if last is not None:
        if fix.ts < last.ts:
            return Decision(
                False,
                REASON_OUT_OF_ORDER,
                "fix is %.0f s older than the last accepted one" % (last.ts - fix.ts),
            )
        if (
            fix.ts == last.ts
            and fix.lat == last.lat
            and fix.lon == last.lon
            and fix.source == last.source
        ):
            return Decision(False, REASON_DUPLICATE, "identical to the last accepted fix")

    return Decision(True, REASON_OK)


def should_move(
    current_target_deg: float | None,
    new_target_deg: float | None,
    filtering: Filtering,
    *,
    last_move_at: float | None = None,
    now: float | None = None,
    force: bool = False,
) -> Decision:
    """Decide whether a change in target angle is worth a motor move.

    Args:
        current_target_deg: Where the hand is already heading, or ``None`` if it
            has never been commanded.
        new_target_deg: The newly computed target.
        filtering: The configured deadband and minimum interval.
        last_move_at: When this hand was last commanded, unix seconds.
        now: Current unix time.
        force: Skip the deadband and the interval -- what the UI's jog and
            "move to angle" buttons use, since a person asked for it directly.

    Returns:
        A :class:`Decision`.
    """
    import time as _time

    now = now if now is not None else _time.time()

    if new_target_deg is None:
        return Decision(False, REASON_NO_TARGET, "no target angle yet")
    if force:
        return Decision(True, REASON_OK, "forced")
    if current_target_deg is None:
        return Decision(True, REASON_OK, "first move")

    from .geo import wrap180

    change = abs(wrap180(new_target_deg - current_target_deg))
    if change < filtering.deadband_deg:
        return Decision(
            False,
            REASON_DEADBAND,
            f"{change:.2f} deg is inside the {filtering.deadband_deg:.2f} deg deadband",
        )
    if last_move_at is not None:
        since = now - last_move_at
        if since < filtering.min_move_interval_s:
            return Decision(
                False,
                REASON_TOO_SOON,
                f"only {since:.1f} s since the last move, minimum is "
                f"{filtering.min_move_interval_s:.1f} s",
            )
    return Decision(True, REASON_OK, f"{change:.2f} deg change")


def _finite(value: float | None) -> bool:
    """True when ``value`` is a real, finite number."""
    if value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))

"""Where the Globe page puts each person: ``PersonState.position``.

The page draws whatever the controller is acting on, so it has to follow the
same precedence as the hand: an active override beats the real fix.
"""

from __future__ import annotations

import time

from clockd.state import Fix, Override, PersonState


def person() -> PersonState:
    return PersonState(name="Evan", slug="evan", level=2, channel=1)


def fix(lat: float = 44.9, lon: float = -93.5) -> Fix:
    now = time.time()
    return Fix(lat=lat, lon=lon, source="hass", ts=now, received=now)


def test_nobody_seen_yet_has_no_position():
    assert person().position() == (None, None)


def test_the_last_fix_is_the_position():
    p = person()
    p.last_fix = fix(52.23, 21.01)
    assert p.position() == (52.23, 21.01)


def test_an_active_override_with_coordinates_wins():
    p = person()
    p.last_fix = fix(52.23, 21.01)
    p.override = Override(expires_at=time.time() + 60, lat=10.0, lon=20.0)
    assert p.position() == (10.0, 20.0)


def test_a_distance_only_override_hides_the_real_fix():
    # The hand is pointing at a made-up distance; showing the real fix on the
    # globe would disagree with the clock.
    p = person()
    p.last_fix = fix(52.23, 21.01)
    p.override = Override(expires_at=time.time() + 60, distance_km=500.0)
    assert p.position() == (None, None)


def test_an_expired_override_falls_back_to_the_fix():
    p = person()
    p.last_fix = fix(52.23, 21.01)
    p.override = Override(expires_at=time.time() - 1, lat=10.0, lon=20.0)
    assert p.position() == (52.23, 21.01)


def test_the_ui_dict_carries_the_position():
    p = person()
    p.last_fix = fix(52.23, 21.01)
    ui = p.to_ui(stale_after_s=3600)
    assert (ui["lat"], ui["lon"]) == (52.23, 21.01)

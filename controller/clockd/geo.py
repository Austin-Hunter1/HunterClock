"""Geography and the distance-to-angle mapping.

The mapping is the one implemented in ``birthday_clock.m`` at the repo root and
re-used by ``build_cad.py``; it is the single thing in this system that must not
drift, so it lives here on its own with no dependencies beyond ``math``:

.. code-block:: text

    d     = max(haversine(home, person), 1.0)        # km, R = 6371.0
    angle = log10(d) / log10(pi * 6371.0) * 360.0    # degrees clockwise from 12

12 o'clock (0 degrees) is home (<= 1 km) and 6 o'clock (180 degrees) is roughly
the antipode's half-way point on the log scale; a full 360 degrees corresponds to
the antipodal distance ``pi * R``.  The scale therefore wraps exactly once over
the range of possible distances on Earth.
"""

from __future__ import annotations

import math

#: Earth radius in km, as used by ``birthday_clock.m``.
EARTH_RADIUS_KM = 6371.0

#: Greatest great-circle distance between two points on Earth (antipode), km.
MAX_DISTANCE_KM = math.pi * EARTH_RADIUS_KM

#: Distances below this are treated as "home"; log10(1) == 0 so this maps to 0 deg.
MIN_DISTANCE_KM = 1.0

#: Denominator of the log map, == log10(pi * 6371) ~= 4.3014.
_LOG_SPAN = math.log10(MAX_DISTANCE_KM)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points in kilometres.

    Args:
        lat1: Latitude of the first point in degrees.
        lon1: Longitude of the first point in degrees.
        lat2: Latitude of the second point in degrees.
        lon2: Longitude of the second point in degrees.

    Returns:
        Distance in kilometres using ``R = 6371.0``.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_KM * c


def clamp_distance_km(distance_km: float) -> float:
    """Clamp a distance into the range the log map accepts.

    Distances under :data:`MIN_DISTANCE_KM` are raised to it (this is the
    ``people.Distance_km(people.Distance_km < 1) = 1`` line in the MATLAB) and
    distances beyond the antipode are clipped so the angle never exceeds 360.
    """
    return min(max(distance_km, MIN_DISTANCE_KM), MAX_DISTANCE_KM)


def distance_to_angle(distance_km: float) -> float:
    """Convert a distance from home into a hand angle.

    Args:
        distance_km: Great-circle distance from home in kilometres.

    Returns:
        Degrees clockwise from 12 o'clock, in ``[0.0, 360.0]``.
    """
    return math.log10(clamp_distance_km(distance_km)) / _LOG_SPAN * 360.0


def angle_to_distance(angle_deg: float) -> float:
    """Inverse of :func:`distance_to_angle`.

    Used by the UI's "fake distance" override and by the display nodes' ring
    fraction, so that one scale is defined in exactly one place.

    Args:
        angle_deg: Degrees clockwise from 12 o'clock.

    Returns:
        The distance in kilometres that maps to that angle.
    """
    return 10.0 ** (angle_deg / 360.0 * _LOG_SPAN)


def angle_for_position(
    home_lat: float, home_lon: float, lat: float, lon: float
) -> tuple[float, float]:
    """Distance and hand angle for a person's position.

    Returns:
        ``(distance_km, angle_deg)`` where the distance is the *unclamped*
        haversine distance (so the UI can show "0.2 km") and the angle is the
        clamped log mapping.
    """
    distance_km = haversine_km(home_lat, home_lon, lat, lon)
    return distance_km, distance_to_angle(distance_km)


def distance_fraction(distance_km: float) -> float:
    """Position on the log scale as a fraction in ``[0.0, 1.0]``.

    This is ``angle / 360`` and is what the ESP32 display ring fills.
    """
    return distance_to_angle(distance_km) / 360.0


def wrap360(angle_deg: float) -> float:
    """Normalise an angle into ``[0.0, 360.0)``.

    Guards the floating-point edge where a tiny negative angle (``-1e-14``, the
    kind :func:`shortest_path_target` produces) would come back as exactly
    ``360.0`` and show up in the UI as "360 degrees" instead of "0".
    """
    wrapped = angle_deg % 360.0
    return 0.0 if wrapped >= 360.0 else wrapped


def wrap180(delta_deg: float) -> float:
    """Normalise an angle difference into ``(-180.0, 180.0]``.

    This is the shortest signed rotation that gets you from one angle to
    another.  The hands have no cable wrap and no end stops, so the shortest
    path is always legal: 324 -> 2 degrees is ``+38``, not ``-322``.
    """
    delta = (delta_deg + 180.0) % 360.0 - 180.0
    # ``%`` in Python returns -180.0 for exactly 180.0; prefer the positive turn.
    return 180.0 if delta == -180.0 else delta


def shortest_path_target(current_deg: float, desired_deg: float) -> float:
    """Unwrapped target angle that reaches ``desired_deg`` by the shortest turn.

    The firmware tracks an unwrapped position that may be any number of turns
    away from zero.  Given where a hand is now (unwrapped) and the angle we want
    it to show (any representation), this returns the unwrapped angle to command.

    Args:
        current_deg: Current unwrapped hand angle in degrees.
        desired_deg: Desired hand angle; only its value modulo 360 matters.

    Returns:
        An unwrapped angle congruent to ``desired_deg`` modulo 360 that is
        within 180 degrees of ``current_deg``.
    """
    return current_deg + wrap180(desired_deg - current_deg)

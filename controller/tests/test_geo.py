"""The mapping vectors from spec/tracking-system.md section 3.

These are the numbers the whole clock is calibrated against, and they come from
``birthday_clock.m``.  Tolerances are the ones the spec asks for: distance
within 1 km, angle within 0.1 degree.
"""

from __future__ import annotations

import math

import pytest

from clockd import geo

HOME_LAT = 44.9109
HOME_LON = -93.5017

# person, lat, lon, expected km, expected degrees
VECTORS = [
    ("Becky", 44.9109, -93.5017, 1.0, 0.0),
    ("Elijah", 44.9537, -93.0900, 33.0, 126.8),
    ("Caleb", 41.8661, -88.1070, 552.0, 229.5),
    ("Austin", 40.0150, -105.2705, 1107.0, 254.8),
    ("Micah", 44.5646, -123.2620, 2338.0, 281.9),
    ("Evan", 52.2297, 21.0122, 7537.0, 324.5),
]


@pytest.mark.parametrize(("name", "lat", "lon", "expect_km", "expect_deg"), VECTORS)
def test_spec_vectors(name, lat, lon, expect_km, expect_deg):
    """Distance within 1 km and angle within 0.1 degree of the spec table."""
    distance_km, angle_deg = geo.angle_for_position(HOME_LAT, HOME_LON, lat, lon)
    clamped = geo.clamp_distance_km(distance_km)
    assert clamped == pytest.approx(expect_km, abs=1.0), name
    assert angle_deg == pytest.approx(expect_deg, abs=0.1), name


def test_home_is_clamped_to_zero_degrees():
    """Standing at home is 0 km but must map to 0 degrees, not -inf."""
    distance_km, angle_deg = geo.angle_for_position(HOME_LAT, HOME_LON, HOME_LAT, HOME_LON)
    assert distance_km == pytest.approx(0.0, abs=1e-9)
    assert angle_deg == 0.0


@pytest.mark.parametrize("distance_km", [0.0, 0.1, 0.999, 1.0])
def test_sub_kilometre_distances_all_map_to_twelve(distance_km):
    assert geo.distance_to_angle(distance_km) == 0.0


def test_antipode_is_one_full_turn():
    """The far side of the Earth is 360 degrees: the scale wraps exactly once."""
    assert geo.distance_to_angle(geo.MAX_DISTANCE_KM) == pytest.approx(360.0, abs=1e-9)
    # And a distance beyond it (numerically possible with a bad fix) is clipped.
    assert geo.distance_to_angle(geo.MAX_DISTANCE_KM * 2) == pytest.approx(360.0, abs=1e-9)


def test_six_oclock_is_the_log_midpoint():
    """180 degrees is 10**(log10(pi*R)/2) km, about 141 km -- not half the globe."""
    assert geo.angle_to_distance(180.0) == pytest.approx(math.sqrt(geo.MAX_DISTANCE_KM), rel=1e-9)


def test_matches_matlab_formula_directly():
    """Re-derive the formula the way birthday_clock.m writes it."""
    for _name, lat, lon, _km, _deg in VECTORS:
        d = max(geo.haversine_km(HOME_LAT, HOME_LON, lat, lon), 1.0)
        expected = math.log10(d) / math.log10(math.pi * 6371.0) * 360.0
        assert geo.distance_to_angle(d) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("distance_km", [1.0, 5.0, 33.0, 552.0, 7537.0, 20000.0])
def test_angle_and_distance_round_trip(distance_km):
    assert geo.angle_to_distance(geo.distance_to_angle(distance_km)) == pytest.approx(
        distance_km, rel=1e-9
    )


def test_haversine_is_symmetric_and_zero_on_itself():
    a = geo.haversine_km(44.9109, -93.5017, 52.2297, 21.0122)
    b = geo.haversine_km(52.2297, 21.0122, 44.9109, -93.5017)
    assert a == pytest.approx(b, rel=1e-12)
    assert geo.haversine_km(40.0, -105.0, 40.0, -105.0) == pytest.approx(0.0, abs=1e-12)


def test_haversine_known_arc_one_degree_of_latitude():
    """One degree of latitude is R * pi/180 ~= 111.195 km anywhere."""
    expected = geo.EARTH_RADIUS_KM * math.pi / 180.0
    assert geo.haversine_km(0.0, 0.0, 1.0, 0.0) == pytest.approx(expected, rel=1e-9)
    assert geo.haversine_km(60.0, 10.0, 61.0, 10.0) == pytest.approx(expected, rel=1e-9)


def test_haversine_crosses_the_antimeridian():
    """A fix just either side of 180 longitude is close, not a world apart."""
    near = geo.haversine_km(0.0, 179.9, 0.0, -179.9)
    assert near == pytest.approx(geo.EARTH_RADIUS_KM * math.radians(0.2), rel=1e-6)


def test_distance_fraction_is_the_display_ring():
    assert geo.distance_fraction(1.0) == 0.0
    assert geo.distance_fraction(geo.MAX_DISTANCE_KM) == pytest.approx(1.0)
    assert 0.0 < geo.distance_fraction(552.0) < 1.0


# -- shortest path -----------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (0.0, 0.0),
        (10.0, 10.0),
        (-10.0, -10.0),
        (350.0, -10.0),
        (-350.0, 10.0),
        (360.0, 0.0),
        (720.0, 0.0),
        (180.0, 180.0),
        (-180.0, 180.0),
        (181.0, -179.0),
    ],
)
def test_wrap180(delta, expected):
    assert geo.wrap180(delta) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("angle", "expected"),
    [(0.0, 0.0), (359.9, 359.9), (360.0, 0.0), (-1e-14, 0.0), (-10.0, 350.0), (725.0, 5.0)],
)
def test_wrap360(angle, expected):
    assert geo.wrap360(angle) == pytest.approx(expected)


def test_shortest_path_from_324_to_2_goes_forward():
    """The spec's example: 324 -> 2 degrees is a short forward move, not -322."""
    target = geo.shortest_path_target(324.0, 2.0)
    assert target == pytest.approx(362.0)
    assert target - 324.0 == pytest.approx(38.0)


def test_shortest_path_keeps_the_unwrapped_frame():
    """A hand that has been round three times stays in its own turn count."""
    current = 3 * 360.0 + 10.0  # 1090 degrees, showing 10
    target = geo.shortest_path_target(current, 350.0)
    assert target == pytest.approx(1070.0)  # went backwards 20 degrees
    assert geo.wrap360(target) == pytest.approx(350.0)


@pytest.mark.parametrize("current", [-1000.0, -0.5, 0.0, 12.3, 359.9, 1234.5])
@pytest.mark.parametrize("desired", [0.0, 1.0, 126.8, 180.0, 324.5, 359.99])
def test_shortest_path_is_never_more_than_half_a_turn(current, desired):
    target = geo.shortest_path_target(current, desired)
    assert abs(target - current) <= 180.0 + 1e-9
    # Compare as angles, so 0 and 360 count as equal.
    assert abs(geo.wrap180(target - desired)) < 1e-9

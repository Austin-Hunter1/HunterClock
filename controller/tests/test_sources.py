"""Location source parsers, against realistic payloads.

These are the messages that arrive from outside the system, so the tests use
payloads shaped like the real thing -- including the ones that are not
locations at all, which a phone emits constantly.
"""

from __future__ import annotations

import json
import time

import pytest

from clockd.config import example_config_path, load_config
from clockd.sources import parse_generic, parse_hass_state, parse_owntracks
from clockd.sources.owntracks import split_topic


@pytest.fixture
def config():
    return load_config(example_config_path())


def owntracks_payload(**overrides):
    """A realistic OwnTracks location message."""
    payload = {
        "_type": "location",
        "lat": 40.0150,
        "lon": -105.2705,
        "acc": 12,
        "tst": 1762204800,
        "batt": 82,
        "tid": "au",
        "alt": 1655,
        "vel": 0,
        "conn": "w",
        "created_at": 1762204801,
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


# -- OwnTracks ---------------------------------------------------------------


def test_owntracks_location(config):
    result = parse_owntracks("owntracks/austin/iphone", owntracks_payload(), config)
    assert result.error is None
    assert result.person.name == "Austin"
    assert result.fix.lat == pytest.approx(40.0150)
    assert result.fix.lon == pytest.approx(-105.2705)
    assert result.fix.accuracy_m == 12.0
    assert result.fix.battery == 82.0
    assert result.fix.ts == 1762204800
    assert result.fix.source == "owntracks"
    assert result.fix.topic == "owntracks/austin/iphone"


def test_owntracks_uses_the_phones_timestamp_not_arrival(config):
    """tst is when the fix was taken; that is what staleness must measure."""
    result = parse_owntracks("owntracks/austin/iphone", owntracks_payload(tst=1000), config)
    assert result.fix.ts == 1000
    assert result.fix.received > 1000


def test_owntracks_without_tst_falls_back_to_now(config):
    payload = json.dumps({"_type": "location", "lat": 1.0, "lon": 2.0}).encode()
    result = parse_owntracks("owntracks/austin/iphone", payload, config, now=5000.0)
    assert result.fix.ts == 5000.0


@pytest.mark.parametrize(
    "kind", ["lwt", "transition", "waypoint", "waypoints", "card", "beacon", "steps", "cmd"]
)
def test_owntracks_ignores_non_location_types(config, kind):
    """A phone emits these constantly; they must not be errors or fixes."""
    payload = json.dumps({"_type": kind, "lat": 0.0, "lon": 0.0, "tst": 1}).encode()
    result = parse_owntracks("owntracks/austin/iphone", payload, config)
    assert result.ignored
    assert result.fix is None
    assert result.error is None


def test_owntracks_transition_is_ignored_even_though_it_has_coordinates(config):
    """A geofence crossing is an event, not a position update."""
    payload = json.dumps(
        {
            "_type": "transition", "event": "enter",
            "lat": 44.9, "lon": -93.5, "tst": 1, "desc": "home",
        }
    ).encode()
    result = parse_owntracks("owntracks/austin/iphone", payload, config)
    assert result.ignored and result.fix is None


def test_owntracks_unknown_user_is_an_error(config):
    result = parse_owntracks("owntracks/stranger/iphone", owntracks_payload(), config)
    assert result.fix is None
    assert "no person configured" in result.error


def test_owntracks_location_without_coordinates_is_an_error(config):
    payload = json.dumps({"_type": "location", "tst": 1}).encode()
    result = parse_owntracks("owntracks/austin/iphone", payload, config)
    assert result.fix is None
    assert "no lat/lon" in result.error


def test_owntracks_bad_json_is_an_error(config):
    result = parse_owntracks("owntracks/austin/iphone", b"{not json", config)
    assert result.fix is None
    assert "JSON" in result.error


def test_owntracks_non_object_payload_is_an_error(config):
    result = parse_owntracks("owntracks/austin/iphone", b"[1,2,3]", config)
    assert result.fix is None
    assert "not a JSON object" in result.error


@pytest.mark.parametrize(
    "topic",
    [
        "owntracks/austin",
        "owntracks/austin/iphone/cmd",
        "owntracks",
        "other/austin/iphone",
        "owntracks/austin/iphone/step/1",
    ],
)
def test_owntracks_ignores_topics_of_the_wrong_shape(config, topic):
    result = parse_owntracks(topic, owntracks_payload(), config)
    assert result.ignored


def test_owntracks_string_numbers_are_accepted(config):
    """Some bridges stringify everything; a position is still a position."""
    payload = json.dumps({"_type": "location", "lat": "44.9109", "lon": "-93.5017"}).encode()
    result = parse_owntracks("owntracks/austin/iphone", payload, config)
    assert result.fix.lat == pytest.approx(44.9109)


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("owntracks/a/b", ("a", "b")),
        ("owntracks/becky/iPhone-12", ("becky", "iPhone-12")),
        ("owntracks/a", None),
        ("owntracks/a/b/c", None),
        ("owntracks//b", None),
    ],
)
def test_split_topic(topic, expected):
    assert split_topic(topic, "owntracks") == expected


# -- generic MQTT ------------------------------------------------------------


def test_generic_location(config):
    payload = json.dumps({"lat": 44.5646, "lon": -123.2620, "acc": 30, "ts": 1762204800}).encode()
    result = parse_generic("clock/location/micah", payload, config)
    assert result.person.name == "Micah"
    assert result.fix.lat == pytest.approx(44.5646)
    assert result.fix.accuracy_m == 30.0
    assert result.fix.source == "generic_mqtt"


def test_generic_accepts_distance_only(config):
    """A sender that knows only a distance can still drive a hand."""
    result = parse_generic("clock/location/micah", b'{"distance_km": 500}', config)
    assert result.fix.distance_km == 500.0
    # It keeps home's coordinates so the fix is still well formed.
    assert result.fix.lat == pytest.approx(config.home.lat)


def test_generic_negative_distance_is_clamped(config):
    result = parse_generic("clock/location/micah", b'{"distance_km": -5}', config)
    assert result.fix.distance_km == 0.0


def test_generic_accepts_alternative_field_names(config):
    payload = json.dumps(
        {"lat": 1.0, "lon": 2.0, "tst": 999, "accuracy_m": 50, "battery": 44}
    ).encode()
    result = parse_generic("clock/location/micah", payload, config)
    assert result.fix.ts == 999
    assert result.fix.accuracy_m == 50.0
    assert result.fix.battery == 44.0


def test_generic_unknown_person_is_an_error(config):
    result = parse_generic("clock/location/nobody", b'{"lat":1,"lon":2}', config)
    assert result.fix is None
    assert "no person named" in result.error


def test_generic_without_position_or_distance_is_an_error(config):
    result = parse_generic("clock/location/micah", b'{"batt": 50}', config)
    assert result.fix is None
    assert "neither lat/lon nor distance_km" in result.error


@pytest.mark.parametrize(
    "topic", ["clock/location", "clock/location/micah/extra", "other/micah", "clock/micah"]
)
def test_generic_ignores_topics_of_the_wrong_shape(config, topic):
    assert parse_generic(topic, b'{"lat":1,"lon":2}', config).ignored


def test_generic_empty_payload_is_an_error(config):
    result = parse_generic("clock/location/micah", b"", config)
    assert "empty payload" in result.error


# -- Home Assistant ----------------------------------------------------------


def test_hass_device_tracker(config):
    state = {
        "entity_id": "device_tracker.caleb_iphone",
        "state": "not_home",
        "attributes": {
            "source_type": "gps",
            "latitude": 41.8661,
            "longitude": -88.1070,
            "gps_accuracy": 25,
            "battery_level": 61,
            "friendly_name": "Caleb iPhone",
        },
        "last_updated": "2026-11-03T10:00:00+00:00",
    }
    result = parse_hass_state(state, config, "device_tracker.caleb_iphone")
    assert result.person.name == "Caleb"
    assert result.fix.lat == pytest.approx(41.8661)
    assert result.fix.accuracy_m == 25.0
    assert result.fix.battery == 61.0
    assert result.fix.source == "hass"
    assert result.fix.ts == pytest.approx(1793700000.0, abs=1)


def test_hass_router_tracker_is_an_error_not_a_position_at_home(config):
    """'home' with no coordinates must not become a real-looking fix at 12."""
    state = {"state": "home", "attributes": {"source_type": "router"}}
    result = parse_hass_state(state, config, "device_tracker.caleb_iphone")
    assert result.fix is None
    assert "no latitude/longitude" in result.error


def test_hass_unknown_entity_is_an_error(config):
    result = parse_hass_state({"state": "home"}, config, "device_tracker.nobody")
    assert result.fix is None
    assert "no person owns" in result.error


def test_hass_without_timestamp_uses_now(config):
    state = {"attributes": {"latitude": 1.0, "longitude": 2.0}}
    result = parse_hass_state(state, config, "device_tracker.caleb_iphone", now=4242.0)
    assert result.fix.ts == 4242.0


def test_hass_unparseable_timestamp_falls_back_to_now(config):
    state = {
        "attributes": {"latitude": 1.0, "longitude": 2.0},
        "last_updated": "not a timestamp",
    }
    result = parse_hass_state(state, config, "device_tracker.caleb_iphone", now=7.0)
    assert result.fix.ts == 7.0


def test_hass_zulu_timestamp(config):
    state = {
        "attributes": {"latitude": 1.0, "longitude": 2.0},
        "last_updated": "2026-11-03T10:00:00Z",
    }
    result = parse_hass_state(state, config, "device_tracker.caleb_iphone")
    assert result.fix.ts == pytest.approx(1793700000.0, abs=1)


def test_hass_non_object_state_is_an_error(config):
    result = parse_hass_state("unavailable", config, "device_tracker.caleb_iphone")
    assert result.fix is None
    assert "not an object" in result.error


def test_hass_token_is_never_read_from_config(config):
    """The token lives in the environment; the file only names the variable."""
    assert config.sources.hass.token_env == "HASS_TOKEN"
    assert not hasattr(config.sources.hass, "token")


# -- shared coercion ---------------------------------------------------------


@pytest.mark.parametrize("bad", [True, False, None, "", "abc", [], {}])
def test_booleans_and_junk_are_not_numbers(config, bad):
    payload = json.dumps({"lat": bad, "lon": 1.0}).encode()
    result = parse_generic("clock/location/micah", payload, config)
    assert result.fix is None


def test_accuracy_of_zero_is_kept_not_treated_as_missing(config):
    """A perfect fix reports acc 0; that must not read as 'unknown'."""
    payload = json.dumps({"lat": 1.0, "lon": 2.0, "acc": 0}).encode()
    result = parse_generic("clock/location/micah", payload, config)
    assert result.fix.accuracy_m == 0.0


def test_a_fix_timestamp_is_seconds_not_milliseconds(config):
    """Sanity check on the unit, since a ms timestamp would look centuries old."""
    payload = json.dumps({"lat": 1.0, "lon": 2.0, "ts": time.time()}).encode()
    result = parse_generic("clock/location/micah", payload, config)
    assert abs(result.fix.ts - time.time()) < 5

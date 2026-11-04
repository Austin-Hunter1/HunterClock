"""Configuration schema and validation."""

from __future__ import annotations

import pytest

from clockd.config import (
    ClockConfig,
    ConfigError,
    Person,
    dump_config,
    example_config_path,
    load_config,
    load_yaml,
    parse_config,
)

MINIMAL = {
    "version": 1,
    "people": [
        {"name": "Becky", "level": 1},
        {"name": "Evan", "level": 2},
    ],
}


def test_example_config_is_valid():
    """The committed example must always load; --sim falls back to it."""
    config = load_config(example_config_path())
    assert len(config.people) == 6
    assert [p.level for p in config.people] == [1, 2, 3, 4, 5, 6]


def test_example_config_contains_no_secrets():
    """A committed file must never hold a password, token or hash."""
    text = example_config_path().read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lstrip("- ").lower()
        value = value.strip().strip("\"'")
        if key.endswith("_env"):
            continue
        if any(word in key for word in ("password", "token", "secret", "key")):
            assert value == "", f"{key} must be empty in the example file"


def test_minimal_config_fills_in_defaults():
    config = parse_config(MINIMAL)
    assert config.home.lat == pytest.approx(44.9109)
    assert config.motion.max_speed == 800.0
    assert config.filtering.deadband_deg == 0.5
    assert config.mqtt.password_env == "MQTT_PASSWORD"


def test_unknown_keys_are_rejected():
    """A typo must be an error, not a silently ignored setting."""
    with pytest.raises(ConfigError) as excinfo:
        parse_config({**MINIMAL, "motionn": {}})
    assert "motionn" in str(excinfo.value)


def test_unknown_nested_key_is_rejected():
    with pytest.raises(ConfigError) as excinfo:
        parse_config({**MINIMAL, "motion": {"max_speeed": 100}})
    assert "max_speeed" in str(excinfo.value)


def test_people_are_required():
    with pytest.raises(ConfigError):
        parse_config({"version": 1, "people": []})


def test_at_most_six_people():
    people = [{"name": f"P{i}", "level": (i % 6) + 1} for i in range(7)]
    with pytest.raises(ConfigError) as excinfo:
        parse_config({"version": 1, "people": people})
    assert "six hands" in str(excinfo.value)


def test_duplicate_levels_are_rejected():
    """Two people on one hub level would fight over the same motor."""
    with pytest.raises(ConfigError) as excinfo:
        parse_config(
            {"version": 1, "people": [{"name": "A", "level": 2}, {"name": "B", "level": 2}]}
        )
    assert "level" in str(excinfo.value).lower()


def test_duplicate_names_are_rejected():
    with pytest.raises(ConfigError):
        parse_config(
            {"version": 1, "people": [{"name": "Evan", "level": 1}, {"name": "evan", "level": 2}]}
        )


@pytest.mark.parametrize("level", [0, 7, -1, 99])
def test_level_must_be_one_to_six(level):
    with pytest.raises(ConfigError):
        parse_config({"version": 1, "people": [{"name": "A", "level": level}]})


@pytest.mark.parametrize("color", ["red", "#fff", "#gggggg", ""])
def test_bad_colors_are_rejected(color):
    with pytest.raises(ConfigError):
        parse_config({"version": 1, "people": [{"name": "A", "level": 1, "color": color}]})


@pytest.mark.parametrize(("lat", "lon"), [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)])
def test_out_of_range_coordinates_are_rejected(lat, lon):
    with pytest.raises(ConfigError):
        parse_config({"version": 1, "home": {"lat": lat, "lon": lon}, "people": MINIMAL["people"]})


def test_fallback_needs_both_coordinates():
    with pytest.raises(ConfigError) as excinfo:
        parse_config(
            {"version": 1, "people": [{"name": "A", "level": 1, "fallback_lat": 44.0}]}
        )
    assert "together" in str(excinfo.value)


def test_speed_above_its_own_limit_is_rejected():
    """The ceiling exists to protect the tube stack; max_speed cannot exceed it."""
    with pytest.raises(ConfigError) as excinfo:
        parse_config({**MINIMAL, "motion": {"max_speed": 5000.0, "speed_limit": 2000.0}})
    assert "speed_limit" in str(excinfo.value)


def test_accel_above_its_own_limit_is_rejected():
    with pytest.raises(ConfigError):
        parse_config({**MINIMAL, "motion": {"accel": 99999.0, "accel_limit": 8000.0}})


def test_unknown_source_in_priority_is_rejected():
    with pytest.raises(ConfigError) as excinfo:
        parse_config({**MINIMAL, "sources": {"priority": ["owntracks", "carrier-pigeon"]}})
    assert "carrier-pigeon" in str(excinfo.value)


def test_duplicate_priority_entries_are_rejected():
    with pytest.raises(ConfigError):
        parse_config({**MINIMAL, "sources": {"priority": ["manual", "manual"]}})


def test_bad_yaml_is_a_config_error():
    with pytest.raises(ConfigError) as excinfo:
        load_yaml("people: [\n  - broken")
    assert "YAML" in str(excinfo.value)


def test_empty_config_is_an_error():
    with pytest.raises(ConfigError):
        parse_config(None)


def test_non_mapping_config_is_an_error():
    with pytest.raises(ConfigError):
        parse_config(["not", "a", "mapping"])


# -- lookups and conversions -------------------------------------------------


def test_channel_is_level_minus_one():
    """Hub level 1 drives Pico channel 0, which is STEP GP2 / DIR GP3."""
    config = parse_config(MINIMAL)
    assert config.person("becky").channel == 0
    assert config.person("evan").channel == 1


def test_person_lookup_is_case_insensitive_and_takes_slugs():
    config = load_config(example_config_path())
    assert config.person("Austin").name == "Austin"
    assert config.person("austin").name == "Austin"
    assert config.person("AUSTIN").name == "Austin"
    with pytest.raises(KeyError):
        config.person("nobody")


def test_person_for_channel():
    config = load_config(example_config_path())
    assert config.person_for_channel(0).name == "Becky"
    assert config.person_for_channel(5).name == "Micah"
    assert config.person_for_channel(9) is None


def test_owntracks_matching_handles_wildcards_and_case():
    config = load_config(example_config_path())
    assert config.person_for_owntracks("austin", "iphone").name == "Austin"
    assert config.person_for_owntracks("AUSTIN", "anything").name == "Austin"
    assert config.person_for_owntracks("stranger", "iphone") is None


def test_owntracks_exact_device_match():
    config = parse_config(
        {
            "version": 1,
            "people": [
                {"name": "A", "level": 1, "owntracks": {"user": "a", "device": "iphone"}},
            ],
        }
    )
    assert config.person_for_owntracks("a", "iphone").name == "A"
    assert config.person_for_owntracks("a", "ipad") is None


def test_angle_to_steps_round_trips():
    config = load_config(example_config_path())
    person = config.person("elijah")
    steps = config.angle_to_steps(person, 126.8)
    assert config.steps_to_angle(person, steps) == pytest.approx(126.8)


def test_invert_flips_the_direction_of_travel():
    """A hand wired with a reversed coil pair is fixed here, not with a soldering iron."""
    config = parse_config(
        {"version": 1, "people": [{"name": "A", "level": 1, "invert": True}]}
    )
    person = config.person("a")
    assert config.angle_to_steps(person, 90.0) == pytest.approx(-800.0)
    assert config.steps_to_angle(person, -800.0) == pytest.approx(90.0)


def test_zero_offset_shifts_the_commanded_angle():
    config = parse_config(
        {"version": 1, "people": [{"name": "A", "level": 1, "zero_offset_deg": 5.0}]}
    )
    person = config.person("a")
    # 0 degrees on the dial becomes 5 degrees of shaft travel.
    assert config.angle_to_steps(person, 0.0) == pytest.approx(5.0 * 3200 / 360)


def test_steps_per_rev_is_per_person():
    config = parse_config(
        {
            "version": 1,
            "people": [
                {"name": "A", "level": 1, "steps_per_rev": 3200},
                {"name": "B", "level": 2, "steps_per_rev": 6400},
            ],
        }
    )
    assert config.angle_to_steps(config.person("a"), 360.0) == pytest.approx(3200)
    assert config.angle_to_steps(config.person("b"), 360.0) == pytest.approx(6400)


def test_dump_round_trips():
    config = load_config(example_config_path())
    again = parse_config(load_yaml(dump_config(config)))
    assert again.model_dump() == config.model_dump()


def test_mqtt_password_comes_from_the_environment(monkeypatch):
    config = parse_config(MINIMAL)
    monkeypatch.delenv("MQTT_PASSWORD", raising=False)
    assert config.mqtt.password() is None
    monkeypatch.setenv("MQTT_PASSWORD", "shhh")
    assert config.mqtt.password() == "shhh"


def test_slug_is_lowercase_and_hyphenated():
    person = Person(name="  Great Aunt Mabel ", level=1)
    assert person.slug == "great-aunt-mabel"
    assert person.name == "Great Aunt Mabel"


def test_channels_is_always_six():
    """The hub has six levels whatever the people list says."""
    assert ClockConfig(people=[Person(name="A", level=1)]).channels == 6

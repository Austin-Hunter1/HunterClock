"""The Home Assistant source against a realistic fake Home Assistant.

Apple has no public Find My API, so the route from an iPhone to this clock runs
Find My -> iCloud -> Home Assistant's iCloud integration -> a ``device_tracker``
entity -> this source.  That makes the exact shape of a Home Assistant
``device_tracker`` payload part of the contract, so these tests serve the real
thing: the attribute names, the ``source_type``, the ISO timestamps and the
failure modes that integration actually produces.

The fake is a FastAPI app driven through httpx's ASGI transport, so the real
HTTP code path runs -- headers, status codes, JSON decoding -- without a port.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from clockd.sources.hass import HassSource

TOKEN = "a-long-lived-access-token"

# Positions from the spec's table, so the angles are checkable.
POSITIONS = {
    "device_tracker.becky_iphone": (44.9109, -93.5017, 65),
    "device_tracker.evan_iphone": (52.2297, 21.0122, 120),
    "device_tracker.elijah_iphone": (44.9537, -93.0900, 35),
    "device_tracker.caleb_iphone": (41.8661, -88.1070, 1500),
    "device_tracker.austin_iphone": (40.0150, -105.2705, 10),
    "device_tracker.micah_iphone": (44.5646, -123.2620, 65),
}


def device_tracker_state(entity_id: str, lat, lon, accuracy, **overrides):
    """A payload shaped like Home Assistant's iCloud integration produces.

    Attribute names matter: ``latitude``/``longitude``/``gps_accuracy`` are what
    a gps-type device_tracker exposes, and getting one wrong means a hand that
    never moves.
    """
    state = {
        "entity_id": entity_id,
        "state": "not_home",
        "attributes": {
            "source_type": "gps",
            "latitude": lat,
            "longitude": lon,
            "gps_accuracy": accuracy,
            "altitude": 280,
            "vertical_accuracy": 10,
            "battery_level": 82,
            "battery_status": "NotCharging",
            "device_status": "online",
            "low_power_mode": False,
            "device_name": entity_id.split(".")[1],
            "device_model": "iPhone15,2",
            "friendly_name": entity_id.split(".")[1].replace("_", " ").title(),
        },
        "last_changed": datetime.now(UTC).isoformat(),
        "last_updated": datetime.now(UTC).isoformat(),
        "context": {"id": "01HV", "parent_id": None, "user_id": None},
    }
    state["attributes"].update(overrides.pop("attributes", {}))
    state.update(overrides)
    return state


class FakeHomeAssistant:
    """A Home Assistant that serves whatever the test tells it to."""

    def __init__(self):
        self.states = {
            entity: device_tracker_state(entity, lat, lon, acc)
            for entity, (lat, lon, acc) in POSITIONS.items()
        }
        self.requests: list[str] = []
        self.fail_with: int | None = None
        self.app = FastAPI()

        @self.app.get("/api/states/{entity_id}")
        async def get_state(entity_id: str, authorization: str = Header(default="")):
            self.requests.append(entity_id)
            if authorization != f"Bearer {TOKEN}":
                # This is exactly what Home Assistant returns for a bad token,
                # and it is the most likely setup mistake.
                raise HTTPException(401, "Unauthorized")
            if self.fail_with:
                raise HTTPException(self.fail_with, "nope")
            if entity_id not in self.states:
                raise HTTPException(404, "Entity not found.")
            return self.states[entity_id]

    def client_factory(self):
        """An httpx client wired straight to the fake app."""
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://ha.test"
        )


@pytest.fixture
def hass(sim_config, monkeypatch):
    """A configured HassSource pointed at a fake Home Assistant."""
    monkeypatch.setenv("HASS_TOKEN", TOKEN)
    sim_config.sources.hass.enabled = True
    sim_config.sources.hass.url = "http://ha.test"
    fake = FakeHomeAssistant()
    fixes: list = []
    rejects: list = []
    source = HassSource(
        sim_config,
        lambda slug, fix: fixes.append((slug, fix)),
        lambda entity, reason: rejects.append((entity, reason)),
        client_factory=fake.client_factory,
    )
    source.fake = fake
    source.fixes_seen = fixes
    source.rejects = rejects
    return source


# -- the happy path ----------------------------------------------------------


async def test_polling_pulls_every_person(hass):
    accepted = await hass.poll_once()
    assert accepted == 6
    assert len(hass.fixes_seen) == 6
    assert {slug for slug, _ in hass.fixes_seen} == {
        "becky", "evan", "elijah", "caleb", "austin", "micah"
    }


async def test_coordinates_and_accuracy_survive_the_round_trip(hass):
    await hass.poll_once()
    by_slug = dict(hass.fixes_seen)
    assert by_slug["austin"].lat == pytest.approx(40.0150)
    assert by_slug["austin"].lon == pytest.approx(-105.2705)
    assert by_slug["austin"].accuracy_m == 10
    assert by_slug["austin"].battery == 82
    assert by_slug["austin"].source == "hass"


async def test_the_entity_id_is_carried_for_the_log(hass):
    await hass.poll_once()
    by_slug = dict(hass.fixes_seen)
    assert by_slug["evan"].topic == "device_tracker.evan_iphone"


async def test_the_bearer_token_is_sent(hass):
    await hass.poll_once()
    assert len(hass.fake.requests) == 6
    assert hass.errors == 0


async def test_a_wrong_token_is_reported_not_silently_ignored(hass, monkeypatch):
    """The most likely setup mistake, and it has to be visible in the UI."""
    monkeypatch.setenv("HASS_TOKEN", "wrong")
    accepted = await hass.poll_once()
    assert accepted == 0
    assert hass.errors == 6
    assert "401" in hass.last_error
    assert all("401" in reason for _entity, reason in hass.rejects)


async def test_no_token_means_no_polling_at_all(hass, monkeypatch):
    monkeypatch.delenv("HASS_TOKEN")
    assert await hass.poll_once() == 0
    assert hass.fake.requests == []


# -- the ways Find My actually fails -----------------------------------------


async def test_a_device_with_no_coordinates_is_rejected(hass):
    """Find My reports a device as home/away with no fix when it is offline.

    Turning that into a position at 12 o'clock would be indistinguishable from
    a real sighting at home, which is worse than showing nothing.
    """
    hass.fake.states["device_tracker.micah_iphone"] = {
        "entity_id": "device_tracker.micah_iphone",
        "state": "unavailable",
        "attributes": {"source_type": "gps", "friendly_name": "Micah iPhone"},
        "last_updated": datetime.now(UTC).isoformat(),
    }
    accepted = await hass.poll_once()
    assert accepted == 5
    assert any("micah" in entity for entity, _ in hass.rejects)
    assert any("no latitude/longitude" in reason for _e, reason in hass.rejects)


async def test_one_bad_entity_does_not_stop_the_others(hass):
    """A single phone dropping off Find My must not blind the whole clock."""
    del hass.fake.states["device_tracker.caleb_iphone"]
    accepted = await hass.poll_once()
    assert accepted == 5
    assert hass.errors == 1
    assert "404" in hass.last_error


async def test_home_assistant_being_down_is_survivable(hass):
    hass.fake.fail_with = 503
    assert await hass.poll_once() == 0
    assert hass.errors == 6
    # And it recovers on the next poll with no intervention.
    hass.fake.fail_with = None
    assert await hass.poll_once() == 6


async def test_a_coarse_find_my_fix_is_still_passed_to_the_filter(hass):
    """Find My can report kilometre-scale accuracy; the source hands it on and
    the engine's accuracy filter is what decides, so the two stay separable."""
    hass.fake.states["device_tracker.caleb_iphone"]["attributes"]["gps_accuracy"] = 5000
    await hass.poll_once()
    by_slug = dict(hass.fixes_seen)
    assert by_slug["caleb"].accuracy_m == 5000


async def test_the_timestamp_comes_from_home_assistant_not_from_now(hass):
    """Staleness has to measure when the phone was seen, not when we polled."""
    old = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    hass.fake.states["device_tracker.evan_iphone"]["last_updated"] = old.isoformat()
    await hass.poll_once()
    by_slug = dict(hass.fixes_seen)
    assert by_slug["evan"].ts == pytest.approx(old.timestamp(), abs=1)


async def test_only_configured_people_are_polled(sim_config, monkeypatch):
    """A device_tracker nobody is mapped to must not be fetched at all."""
    monkeypatch.setenv("HASS_TOKEN", TOKEN)
    sim_config.sources.hass.enabled = True
    for person in sim_config.people:
        person.hass_entity = None
    sim_config.people[0].hass_entity = "device_tracker.becky_iphone"
    fake = FakeHomeAssistant()
    source = HassSource(sim_config, lambda s, f: None, client_factory=fake.client_factory)
    await source.poll_once()
    assert fake.requests == ["device_tracker.becky_iphone"]


# -- end to end through the engine -------------------------------------------


async def test_find_my_positions_drive_the_hands(stack, monkeypatch):
    """The whole route: Home Assistant -> source -> engine -> microsteps."""
    monkeypatch.setenv("HASS_TOKEN", TOKEN)
    stack.config.sources.hass.enabled = True
    stack.config.sources.hass.url = "http://ha.test"
    fake = FakeHomeAssistant()
    source = stack.engine.sources["hass"]
    source.client_factory = fake.client_factory

    await stack.zero_all()
    accepted = await source.poll_once()
    await stack.settle(seconds=8.0)

    assert accepted == 6
    expected = {
        "becky": 0.0, "elijah": 126.8, "caleb": 229.5,
        "austin": 254.8, "micah": 281.9, "evan": 324.5,
    }
    for slug, angle in expected.items():
        state = stack.engine.state(slug)
        assert state.source == "hass", slug
        assert state.target_deg == pytest.approx(angle, abs=0.1), slug
        assert state.actual_deg == pytest.approx(angle, abs=0.6), slug


async def test_a_vague_find_my_fix_is_filtered_before_it_moves_a_hand(stack, monkeypatch):
    """Find My's coarse fixes are exactly what the accuracy limit is for."""
    monkeypatch.setenv("HASS_TOKEN", TOKEN)
    stack.config.sources.hass.enabled = True
    fake = FakeHomeAssistant()
    source = stack.engine.sources["hass"]
    source.client_factory = fake.client_factory

    await stack.zero_all()
    await source.poll_once()
    await stack.settle(seconds=8.0)
    settled = stack.engine.state("austin").target_deg

    # Now Find My loses the plot: a fix on the far side of the world, 9 km wide.
    fake.states["device_tracker.austin_iphone"]["attributes"].update(
        {"latitude": -31.95, "longitude": 115.86, "gps_accuracy": 9000}
    )
    fake.states["device_tracker.austin_iphone"]["last_updated"] = datetime.now(
        UTC
    ).isoformat()
    await source.poll_once()
    await asyncio.sleep(0.3)

    assert stack.engine.state("austin").target_deg == pytest.approx(settled)
    assert stack.engine.state("austin").last_reject_reason == "accuracy"


async def test_the_source_can_be_switched_off_from_the_ui(stack, monkeypatch):
    monkeypatch.setenv("HASS_TOKEN", TOKEN)
    stack.config.sources.hass.enabled = True
    fake = FakeHomeAssistant()
    stack.engine.sources["hass"].client_factory = fake.client_factory

    stack.engine.set_source_enabled("hass", False)
    assert not stack.engine.sources["hass"].enabled
    stack.engine.set_source_enabled("hass", True)
    assert stack.engine.sources["hass"].enabled


async def test_describe_shows_what_the_system_page_needs(hass):
    await hass.poll_once()
    described = hass.describe()
    assert described["enabled"] is True
    assert described["token_present"] is True
    assert described["entities"] == 6
    assert described["fixes"] == 6
    assert described["errors"] == 0

"""``tools/hass_check.py`` -- the tool somebody runs before editing the config.

This is the first thing anybody touches when wiring Find My to the clock, and
it is run exactly once, by somebody who does not yet know whether the problem
is the token, the integration, the entity name or the clock.  So it is served
a realistic Home Assistant over a real socket rather than a mocked client: the
whole point is to exercise the path it will actually take, headers and JSON
decoding included.

The payload shapes matter and are part of the contract.  A ``person`` entity
and a ``device_tracker`` entity carry the same ``latitude``/``longitude``, but
a person entity also has ``source`` (the tracker it is following right now)
and ``device_trackers`` (all of them), and those two attributes are what let
the tool tell somebody they have wired a hand to the fragile one.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

TOKEN = "a-long-lived-access-token"

# Austin in Boulder, 10 km out, as in the spec's table.
AUSTIN = (40.0150, -105.2705)
BECKY = (44.9109, -93.5017)


def device_tracker(entity_id, lat, lon, *, accuracy=30, state="not_home"):
    """What Home Assistant's iCloud integration exposes per device."""
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {
            "source_type": "gps",
            "latitude": lat,
            "longitude": lon,
            "gps_accuracy": accuracy,
            "battery_level": 82,
            "friendly_name": entity_id.split(".")[1],
        },
        "last_updated": "2026-11-24T00:00:00+00:00",
    }


def person(entity_id, lat, lon, *, source, trackers, accuracy=30):
    """What a Home Assistant ``person`` entity exposes.

    ``source`` is the tracker currently being followed and ``device_trackers``
    is every tracker that could be; both are real attributes and both are what
    make a person entity worth preferring.
    """
    return {
        "entity_id": entity_id,
        "state": "not_home",
        "attributes": {
            "editable": True,
            "id": entity_id.split(".")[1],
            "latitude": lat,
            "longitude": lon,
            "gps_accuracy": accuracy,
            "source": source,
            "device_trackers": trackers,
            "friendly_name": entity_id.split(".")[1].title(),
        },
        "last_updated": "2026-11-24T00:00:00+00:00",
    }


def router_tracker(entity_id):
    """A router-based tracker: knows home or away, and no more than that."""
    return {
        "entity_id": entity_id,
        "state": "home",
        "attributes": {"source_type": "router", "friendly_name": entity_id.split(".")[1]},
        "last_updated": "2026-11-24T00:00:00+00:00",
    }


@pytest.fixture
def fake_hass():
    """A real HTTP server speaking enough of Home Assistant's REST API."""
    states = [
        person(
            "person.austin",
            *AUSTIN,
            source="device_tracker.austin_iphone",
            trackers=["device_tracker.austin_iphone", "device_tracker.austin_app"],
        ),
        person(
            "person.becky",
            *BECKY,
            source="device_tracker.becky_iphone",
            trackers=["device_tracker.becky_iphone"],
        ),
        device_tracker("device_tracker.austin_iphone", *AUSTIN),
        device_tracker("device_tracker.austin_app", *AUSTIN, accuracy=12),
        device_tracker("device_tracker.becky_iphone", *BECKY),
        # A phone that Find My cannot currently see: home/away and nothing else.
        router_tracker("device_tracker.spare_ipad"),
        {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {}},
    ]
    holder = {"states": states, "token": TOKEN}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {holder['token']}":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"message":"Unauthorized"}')
                return
            if self.path == "/api/":
                body = json.dumps({"message": "API running."}).encode()
            elif self.path == "/api/states":
                body = json.dumps(holder["states"]).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    holder["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield holder
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def run_check(fake_hass, monkeypatch, capsys, tmp_path):
    """Run the tool against the fake and return ``(exit_code, output)``."""
    from clockd.config import dump_config, example_config_path, load_config

    def run(*, token=TOKEN, config_edits=None, argv=(), wire=True):
        config = load_config(example_config_path())
        if not wire:
            for who in config.people:
                who.hass_entity = None
        if config_edits is not None:
            config_edits(config)
        path = tmp_path / "clock.yaml"
        path.write_text(dump_config(config))
        monkeypatch.setenv("HASS_TOKEN", token)
        # No proxy: the fake is on localhost and a configured proxy would
        # swallow the request.
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.delenv(name, raising=False)
        import hass_check

        code = hass_check.main(
            ["--url", fake_hass["url"], "--config", str(path), *argv]
        )
        return code, capsys.readouterr().out

    return run


class TestReachability:
    """The first question: can it get there at all."""

    def test_a_good_token_connects(self, run_check):
        code, out = run_check()
        assert code == 0
        assert "Connected." in out

    def test_a_rejected_token_says_so_and_how_to_fix_it(self, run_check):
        code, out = run_check(token="not-the-token")
        assert code == 1
        assert "rejected the token" in out
        assert "Make a new one" in out

    def test_no_token_at_all_explains_where_to_get_one(self, run_check):
        code, out = run_check(token="")
        assert code == 1
        assert "Long-lived access tokens" in out

    def test_the_token_is_never_printed(self, run_check):
        # It goes in .env and into a terminal somebody may paste into an issue.
        for token in (TOKEN, "not-the-token"):
            _code, out = run_check(token=token)
            assert token not in out


class TestWhatItReports:
    """The useful part: which entities could drive a hand, and where to."""

    def test_person_entities_are_listed_first_and_recommended(self, run_check):
        code, out = run_check()
        assert code == 0
        assert out.index("PERSON ENTITIES") < out.index("DEVICE TRACKERS")
        assert "prefer these" in out

    def test_it_computes_the_hand_position_with_the_clock_s_own_mapping(self, run_check):
        from clockd.config import example_config_path, load_config

        config = load_config(example_config_path())
        distance, angle = config.angle_for(*AUSTIN)
        _code, out = run_check()
        # If this number is wrong here, it is wrong on the clock too.
        assert f"{distance:8.1f} km" in out
        assert f"{angle:7.1f}" in out

    def test_a_router_based_tracker_is_marked_unusable(self, run_check):
        _code, out = run_check()
        line = next(
            line for line in out.splitlines() if "device_tracker.spare_ipad" in line
        )
        assert "no coordinates" in line
        assert "state=home" in line

    def test_a_person_entity_says_which_tracker_it_is_following(self, run_check):
        # The line that reveals Find My has gone quiet and something else is
        # carrying the hand.
        _code, out = run_check()
        line = next(line for line in out.splitlines() if line.startswith("person.austin"))
        assert "via device_tracker.austin_iphone" in line

    def test_a_coarse_fix_is_flagged_as_one_the_clock_would_reject(
        self, run_check, fake_hass
    ):
        fake_hass["states"].append(
            device_tracker("device_tracker.cell_tower_only", *AUSTIN, accuracy=5000)
        )
        _code, out = run_check()
        line = next(
            line for line in out.splitlines() if "device_tracker.cell_tower_only" in line
        )
        assert "would be rejected" in line

    def test_entities_that_are_neither_are_left_out(self, run_check):
        _code, out = run_check()
        assert "sun.sun" not in out

    def test_it_fails_when_nothing_carries_a_position(self, run_check, fake_hass):
        fake_hass["states"] = [router_tracker("device_tracker.spare_ipad")]
        code, out = run_check()
        assert code == 1
        assert "0 entities carry coordinates" in out

    def test_an_empty_home_assistant_says_what_to_install(self, run_check, fake_hass):
        fake_hass["states"] = [{"entity_id": "sun.sun", "state": "x", "attributes": {}}]
        code, out = run_check()
        assert code == 1
        assert "not tracking anybody yet" in out
        assert "OwnTracks" in out


class TestTheAdviceItGives:
    """The diagnostic that is the reason for this change."""

    def test_it_spots_a_hand_wired_to_a_tracker_a_person_entity_covers(self, run_check):
        def wire_to_the_fragile_one(config):
            config.people[0].hass_entity = "device_tracker.austin_iphone"

        _code, out = run_check(wire=False, config_edits=wire_to_the_fragile_one)
        assert "pointed at one tracker when a person entity covers it" in out
        assert "use person.austin instead" in out

    def test_it_says_nothing_when_the_person_entity_is_already_used(self, run_check):
        def wire_to_the_good_one(config):
            config.people[0].hass_entity = "person.austin"

        _code, out = run_check(wire=False, config_edits=wire_to_the_good_one)
        assert "pointed at one tracker" not in out

    def test_a_tracker_no_person_entity_covers_is_not_complained_about(self, run_check):
        # Somebody with no person entity has to use a bare tracker, and being
        # nagged about it with no alternative to offer would be noise.
        def wire_to_an_uncovered_tracker(config):
            config.people[0].hass_entity = "device_tracker.spare_ipad"

        _code, out = run_check(wire=False, config_edits=wire_to_an_uncovered_tracker)
        assert "pointed at one tracker" not in out

    def test_people_with_no_entity_at_all_are_named(self, run_check):
        _code, out = run_check(wire=False)
        assert "No hass_entity set for:" in out

    def test_the_committed_example_config_uses_person_entities(self, run_check):
        """The default must be the good pattern, not the fragile one.

        The example is what install.sh copies to config/clock.yaml, so
        whatever it says is what most clocks will run.
        """
        _code, out = run_check()
        assert "pointed at one tracker" not in out

    def test_it_offers_the_entity_ids_to_paste_into_the_config(self, run_check):
        _code, out = run_check(wire=False)
        assert "hass_entity: person.austin" in out

    def test_an_entity_already_in_the_config_is_not_offered_again(self, run_check):
        # The unmapped device_trackers are still offered -- somebody may want
        # one -- but an entity already wired to a person is not repeated back.
        _code, out = run_check()
        offered = out.split("To use these, put them", 1)
        assert len(offered) == 2
        assert "hass_entity: person.austin" not in offered[1]
        assert "hass_entity: device_tracker.austin_app" in offered[1]

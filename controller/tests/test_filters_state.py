"""Filtering, the per-person state machine and persistence."""

from __future__ import annotations

import time

import pytest

from clockd.config import Filtering
from clockd.filters import (
    REASON_ACCURACY,
    REASON_BAD_COORDS,
    REASON_DEADBAND,
    REASON_DUPLICATE,
    REASON_OK,
    REASON_OUT_OF_ORDER,
    REASON_TOO_OLD,
    REASON_TOO_SOON,
    accept_fix,
    should_move,
)
from clockd.state import (
    STATUS_DISABLED,
    STATUS_FAULT,
    STATUS_OK,
    STATUS_OVERRIDE,
    STATUS_STALE,
    STATUS_UNVERIFIED,
    Fix,
    Override,
    PersonState,
)
from clockd.store import Store

NOW = 1_762_000_000.0


@pytest.fixture
def filtering():
    return Filtering()


def fix(**overrides):
    values = {"lat": 44.9109, "lon": -93.5017, "ts": NOW, "received": NOW, "source": "owntracks"}
    values.update(overrides)
    return Fix(**values)


# -- accept_fix --------------------------------------------------------------


def test_a_good_fix_is_accepted(filtering):
    decision = accept_fix(fix(accuracy_m=20), None, filtering, now=NOW)
    assert decision.accept
    assert decision.reason == REASON_OK
    assert bool(decision) is True


def test_a_vague_fix_is_rejected(filtering):
    """A 5 km cell-tower fix would swing a hand across the dial for nothing."""
    decision = accept_fix(fix(accuracy_m=5000), None, filtering, now=NOW)
    assert not decision.accept
    assert decision.reason == REASON_ACCURACY
    assert "5000" in decision.detail


def test_a_fix_at_exactly_the_accuracy_limit_is_accepted(filtering):
    assert accept_fix(fix(accuracy_m=2000.0), None, filtering, now=NOW).accept


def test_a_fix_with_no_accuracy_is_accepted(filtering):
    """Not every source reports accuracy; absence is not badness."""
    assert accept_fix(fix(accuracy_m=None), None, filtering, now=NOW).accept


def test_a_very_old_fix_is_rejected(filtering):
    """Airplane mode flushes a queue of day-old points when the phone wakes."""
    decision = accept_fix(fix(ts=NOW - 60 * 3600), None, filtering, now=NOW)
    assert not decision.accept
    assert decision.reason == REASON_TOO_OLD


def test_an_out_of_order_fix_is_rejected(filtering):
    last = fix(ts=NOW)
    decision = accept_fix(fix(ts=NOW - 600, lat=1.0, lon=1.0), last, filtering, now=NOW)
    assert not decision.accept
    assert decision.reason == REASON_OUT_OF_ORDER


def test_an_identical_repeat_is_rejected(filtering):
    last = fix()
    decision = accept_fix(fix(), last, filtering, now=NOW)
    assert not decision.accept
    assert decision.reason == REASON_DUPLICATE


def test_the_same_timestamp_from_a_different_place_is_accepted(filtering):
    """Two sources can legitimately report the same instant."""
    last = fix(source="owntracks")
    decision = accept_fix(fix(source="generic_mqtt", lat=1.0), last, filtering, now=NOW)
    assert decision.accept


def test_a_newer_fix_from_the_same_place_is_accepted(filtering):
    """Standing still still counts as a fresh sighting, which clears 'stale'."""
    last = fix(ts=NOW - 100)
    assert accept_fix(fix(ts=NOW), last, filtering, now=NOW).accept


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0),
     (float("nan"), 0.0), (float("inf"), 0.0)],
)
def test_impossible_coordinates_are_rejected(filtering, lat, lon):
    decision = accept_fix(fix(lat=lat, lon=lon), None, filtering, now=NOW)
    assert not decision.accept
    assert decision.reason == REASON_BAD_COORDS


def test_null_island_is_accepted_because_it_is_a_real_place(filtering):
    """0,0 is in the Gulf of Guinea. Suspicious, but not impossible."""
    assert accept_fix(fix(lat=0.0, lon=0.0), None, filtering, now=NOW).accept


def test_a_custom_accuracy_limit_is_honoured():
    strict = Filtering(max_accuracy_m=50.0)
    assert not accept_fix(fix(accuracy_m=100), None, strict, now=NOW).accept
    assert accept_fix(fix(accuracy_m=40), None, strict, now=NOW).accept


# -- should_move -------------------------------------------------------------


def test_the_first_move_always_goes(filtering):
    assert should_move(None, 120.0, filtering, now=NOW).accept


def test_no_target_means_no_move(filtering):
    assert not should_move(10.0, None, filtering, now=NOW).accept


def test_a_tiny_change_is_inside_the_deadband(filtering):
    decision = should_move(100.0, 100.3, filtering, now=NOW)
    assert not decision.accept
    assert decision.reason == REASON_DEADBAND


def test_a_change_at_the_deadband_edge_moves(filtering):
    assert should_move(100.0, 100.5, filtering, now=NOW).accept


def test_the_deadband_is_measured_the_short_way_round(filtering):
    """359.9 to 0.1 is 0.2 degrees, not 359.8."""
    decision = should_move(359.9, 0.1, filtering, now=NOW)
    assert not decision.accept
    assert decision.reason == REASON_DEADBAND


def test_moves_are_rate_limited(filtering):
    decision = should_move(0.0, 90.0, filtering, last_move_at=NOW - 1.0, now=NOW)
    assert not decision.accept
    assert decision.reason == REASON_TOO_SOON


def test_rate_limit_expires(filtering):
    assert should_move(0.0, 90.0, filtering, last_move_at=NOW - 10.0, now=NOW).accept


def test_force_bypasses_both_limits(filtering):
    """The UI's jog and move buttons are a person asking directly."""
    decision = should_move(100.0, 100.01, filtering, last_move_at=NOW, now=NOW, force=True)
    assert decision.accept
    assert decision.detail == "forced"


def test_force_still_needs_a_target(filtering):
    assert not should_move(0.0, None, filtering, force=True).accept


# -- the state machine -------------------------------------------------------


def person(**overrides):
    values = {
        "name": "Evan",
        "slug": "evan",
        "level": 2,
        "channel": 1,
        "verified": True,
        "last_seen": NOW,
        "distance_km": 7537.0,
        "target_deg": 324.5,
    }
    values.update(overrides)
    return PersonState(**values)


STALE_AFTER = 6 * 3600.0


def test_a_healthy_person_is_ok():
    assert person().status(STALE_AFTER, NOW) == STATUS_OK


def test_disabled_beats_everything():
    state = person(enabled=False, verified=False, fault="broken", link_down=True)
    assert state.status(STALE_AFTER, NOW) == STATUS_DISABLED


def test_fault_beats_unverified():
    assert person(verified=False, fault="move failed").status(STALE_AFTER, NOW) == STATUS_FAULT


def test_a_dropped_link_is_a_fault():
    assert person(link_down=True).status(STALE_AFTER, NOW) == STATUS_FAULT


def test_unverified_beats_override_and_stale():
    state = person(verified=False, last_seen=NOW - 10 * 3600,
                   override=Override(expires_at=NOW + 100))
    assert state.status(STALE_AFTER, NOW) == STATUS_UNVERIFIED


def test_override_beats_stale():
    state = person(last_seen=NOW - 10 * 3600, override=Override(expires_at=NOW + 100))
    assert state.status(STALE_AFTER, NOW) == STATUS_OVERRIDE


def test_an_expired_override_does_not_count():
    state = person(override=Override(expires_at=NOW - 1))
    assert state.status(STALE_AFTER, NOW) == STATUS_OK


def test_stale_after_the_timeout():
    assert person(last_seen=NOW - STALE_AFTER - 1).status(STALE_AFTER, NOW) == STATUS_STALE
    assert person(last_seen=NOW - STALE_AFTER + 1).status(STALE_AFTER, NOW) == STATUS_OK


def test_never_seen_is_not_stale():
    """Never having had a fix is a different thing from having an old one."""
    state = person(last_seen=None, distance_km=None, target_deg=None)
    assert not state.is_stale(STALE_AFTER, NOW)
    assert state.status(STALE_AFTER, NOW) == STATUS_OK


def test_every_status_is_in_the_documented_set():
    from clockd.state import STATUSES

    cases = [
        person(),
        person(enabled=False),
        person(fault="x"),
        person(verified=False),
        person(override=Override(expires_at=NOW + 10)),
        person(last_seen=NOW - 99999),
    ]
    for state in cases:
        assert state.status(STALE_AFTER, NOW) in STATUSES


# -- display vocabulary ------------------------------------------------------


def test_display_says_home_inside_a_kilometre():
    state = person(distance_km=0.4)
    assert state.display_status(STALE_AFTER, NOW) == "home"
    assert state.display_text(STALE_AFTER, NOW) == "HOME"


def test_display_says_away_with_a_distance():
    assert person(distance_km=7537.0).display_text(STALE_AFTER, NOW) == "7537 km away"
    assert person(distance_km=3.2).display_text(STALE_AFTER, NOW) == "3.2 km away"


def test_display_says_stale_when_old():
    state = person(last_seen=NOW - 99999)
    assert state.display_status(STALE_AFTER, NOW) == "stale"


def test_display_says_unknown_with_no_fix():
    state = person(last_seen=None, distance_km=None)
    assert state.display_status(STALE_AFTER, NOW) == "unknown"
    assert state.display_text(STALE_AFTER, NOW) == "no location yet"


def test_display_does_not_leak_unverified_to_the_screens():
    """'Unverified' is the owner's problem, not something to put on a portrait."""
    state = person(verified=False, distance_km=50.0)
    assert state.display_status(STALE_AFTER, NOW) == "away"


def test_display_payload_carries_the_ring_fraction():
    payload = person(distance_km=7537.0).to_display_payload(STALE_AFTER, NOW)
    assert payload["fraction"] == pytest.approx(324.5 / 360, abs=0.001)
    assert payload["status"] == "away"
    assert payload["name"] == "Evan"


def test_state_payload_shape():
    payload = person(actual_deg=324.4, accuracy_m=12.0, source="owntracks").to_state_payload(
        STALE_AFTER, NOW
    )
    assert set(payload) == {
        "name", "distance_km", "target_deg", "actual_deg", "status",
        "last_seen", "source", "accuracy_m", "verified", "moving", "level",
    }
    assert payload["last_seen"].endswith("+00:00")


def test_override_reports_its_remaining_time():
    override = Override(expires_at=NOW + 30)
    assert override.active(NOW)
    assert override.seconds_left(NOW) == pytest.approx(30)
    assert not override.active(NOW + 31)
    assert override.seconds_left(NOW + 100) == 0.0


def test_fix_round_trips_through_a_dict():
    original = fix(accuracy_m=10, battery=55, topic="owntracks/a/b")
    assert Fix.from_dict(original.to_dict()) == original


def test_fix_from_dict_ignores_unknown_keys():
    """An older stored fix with a since-removed field must still load."""
    data = fix().to_dict()
    data["something_new"] = 1
    assert Fix.from_dict(data).lat == pytest.approx(44.9109)


# -- the store ---------------------------------------------------------------


def test_store_persists_across_instances(tmp_path):
    path = tmp_path / "state.json"
    store = Store(path)
    store.set_zero("evan", 1.5)
    store.set_verified("evan", True, when=NOW)
    store.set_steps("evan", 1234)
    store.set_fix("evan", fix().to_dict())

    again = Store(path)
    assert again.get_zero("evan") == 1.5
    assert again.is_verified("evan")
    assert again.verified_at("evan") == NOW
    assert again.get_steps("evan") == 1234
    assert again.get_fix("evan")["lat"] == pytest.approx(44.9109)


def test_store_defaults_for_an_unknown_person(tmp_path):
    store = Store(tmp_path / "state.json")
    assert store.get_zero("nobody") == 0.0
    assert store.is_verified("nobody") is False
    assert store.get_steps("nobody") is None
    assert store.get_fix("nobody") is None


def test_a_corrupt_store_does_not_stop_the_clock(tmp_path):
    """Losing a zero offset is bad; refusing to boot over it is worse."""
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = Store(path)
    assert store.load_error is not None
    assert store.get_zero("evan") == 0.0
    assert path.with_suffix(".json.corrupt").exists()


def test_a_store_holding_the_wrong_shape_is_quarantined(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('["not", "a", "mapping"]', encoding="utf-8")
    store = Store(path)
    assert store.load_error is not None


def test_unverifying_clears_the_timestamp(tmp_path):
    store = Store(tmp_path / "state.json")
    store.set_verified("evan", True, when=NOW)
    store.set_verified("evan", False)
    assert store.verified_at("evan") is None
    assert not store.is_verified("evan")


def test_store_writes_are_atomic(tmp_path):
    """os.replace means a brownout leaves the previous good file, not half of one."""
    path = tmp_path / "state.json"
    store = Store(path)
    store.set_steps("evan", 1)
    store.set_steps("evan", 2)
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert Store(path).get_steps("evan") == 2


def test_forget_removes_a_person(tmp_path):
    store = Store(tmp_path / "state.json")
    store.set_steps("evan", 5)
    store.forget("evan")
    assert store.get_steps("evan") is None


def test_iso_formatting_handles_none():
    from clockd.state import iso

    assert iso(None) is None
    assert iso(NOW).startswith("2025-")


def test_seconds_since_seen():
    assert person(last_seen=NOW - 30).seconds_since_seen(NOW) == pytest.approx(30)
    assert person(last_seen=None).seconds_since_seen(NOW) is None


def test_to_ui_is_json_safe():
    import json

    payload = person(override=Override(expires_at=time.time() + 10)).to_ui(STALE_AFTER)
    json.dumps(payload)  # must not raise
    assert payload["slug"] == "evan"
    assert payload["override"] is not None

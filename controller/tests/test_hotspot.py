"""The fallback setup hotspot.

Everything here runs against :class:`clockd.hotspot.sim.FakeNmcli`, which
parses the same argv the real ``nmcli`` would and refuses what the real one
would refuse.  That is the point: none of this can be tried on the actual Pi
from here, so the next best thing is a fake strict enough that a malformed
command fails in CI instead of in Minnesota.

The scenarios that earn their keep are the ones about *coming back down*.  A
hotspot that latches on turns a five-minute router reboot into a clock that
is offline until somebody drives to it, because the Pi's single radio cannot
be an access point and a WiFi client at the same time.
"""

from __future__ import annotations

import pathlib

import pytest

from clockd.hotspot import nm
from clockd.hotspot.monitor import Action, HotspotMonitor, Observation, State
from clockd.hotspot.portal import (
    DETECTION_PATHS,
    PortalState,
    build_app,
    render_form,
    render_submitted,
    validate_password,
    validate_ssid,
)
from clockd.hotspot.service import HotspotService
from clockd.hotspot.settings import (
    PASSWORD_ENV,
    HotspotSettings,
    load_settings,
    suggest_password,
)
from clockd.hotspot.sim import FakeNetwork, FakeNmcli

HOUSE = "Minnetonka-5G"
HOUSE_PASSWORD = "correct-horse"
AP_PASSWORD = "a1b2c3d4e5f6"


@pytest.fixture
def ap_password(monkeypatch):
    """A valid AP passphrase in the environment, as ``.env`` would provide."""
    monkeypatch.setenv(PASSWORD_ENV, AP_PASSWORD)
    return AP_PASSWORD


@pytest.fixture
def settings():
    """Short timings so a test can cover a day of outages instantly."""
    return HotspotSettings(
        check_interval_s=1.0,
        offline_grace_s=60.0,
        hotspot_idle_s=120.0,
        hotspot_max_s=300.0,
        retry_wifi_s=30.0,
    )


@pytest.fixture
def air():
    """The networks around the clock."""
    return [
        FakeNetwork(HOUSE, HOUSE_PASSWORD, signal=78),
        FakeNetwork("Smith: Guest", None, signal=40, security=""),
        FakeNetwork("neighbour-2.4", "something-else", signal=22),
    ]


class Clock:
    """A clock a test can push forward."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------- nmcli terse


class TestTerseOutput:
    """``nmcli -t`` escaping, which a naive split gets wrong."""

    def test_plain_line_splits(self):
        assert nm.split_terse("wlan0:connected:Minnetonka-5G") == [
            "wlan0",
            "connected",
            "Minnetonka-5G",
        ]

    def test_a_colon_in_an_ssid_is_one_field(self):
        # A guest network called "Smith: Guest" is common, and splitting on
        # bare colons shifts every field after it.
        assert nm.split_terse(r"Smith\: Guest:40:") == ["Smith: Guest", "40", ""]

    def test_a_backslash_in_an_ssid_survives(self):
        assert nm.split_terse(r"back\\slash:1") == ["back\\slash", "1"]

    def test_empty_fields_are_kept(self):
        assert nm.split_terse("a::c") == ["a", "", "c"]

    def test_a_trailing_separator_yields_a_trailing_empty_field(self):
        assert nm.split_terse("a:b:") == ["a", "b", ""]

    def test_an_ssid_that_is_only_a_colon_round_trips(self):
        from clockd.hotspot.sim import _terse

        assert nm.split_terse(_terse([":", "70"])) == [":", "70"]


# ------------------------------------------------------------------ settings


class TestSettings:
    """The hotspot's own config, kept apart from ``config/clock.yaml``."""

    def test_defaults_are_usable_with_no_file_at_all(self, tmp_path):
        loaded = load_settings(tmp_path / "nope.yaml")
        assert loaded.ssid == "weasley-clock-setup"
        assert loaded.enabled

    def test_a_broken_file_falls_back_to_defaults_rather_than_raising(self, tmp_path):
        # The whole reason this config is separate from clockd's is that the
        # hotspot has to work when a config file is the thing that is broken.
        # Raising here would reproduce the failure it exists to rescue.
        path = tmp_path / "hotspot.yaml"
        path.write_text("ssid: [unclosed\n")
        assert load_settings(path).ssid == "weasley-clock-setup"

    def test_a_yaml_list_instead_of_a_mapping_falls_back(self, tmp_path):
        path = tmp_path / "hotspot.yaml"
        path.write_text("- one\n- two\n")
        assert load_settings(path).ssid == "weasley-clock-setup"

    def test_an_unknown_key_falls_back_rather_than_half_applying(self, tmp_path):
        path = tmp_path / "hotspot.yaml"
        path.write_text("ssid: fine\nnonsense: 3\n")
        assert load_settings(path).ssid == "weasley-clock-setup"

    def test_a_valid_file_is_applied(self, tmp_path):
        path = tmp_path / "hotspot.yaml"
        path.write_text("ssid: clock-fix-me\noffline_grace_s: 90\n")
        loaded = load_settings(path)
        assert loaded.ssid == "clock-fix-me"
        assert loaded.offline_grace_s == 90

    def test_no_password_is_no_password(self, monkeypatch):
        monkeypatch.delenv(PASSWORD_ENV, raising=False)
        assert HotspotSettings().password() is None
        assert PASSWORD_ENV in HotspotSettings().password_problem()

    @pytest.mark.parametrize("value", ["", "short", "1234567"])
    def test_a_passphrase_wpa2_would_reject_is_refused(self, monkeypatch, value):
        monkeypatch.setenv(PASSWORD_ENV, value)
        assert HotspotSettings().password() is None
        assert HotspotSettings().password_problem() is not None

    def test_a_passphrase_over_63_characters_is_refused(self, monkeypatch):
        monkeypatch.setenv(PASSWORD_ENV, "x" * 64)
        assert HotspotSettings().password() is None

    def test_a_normal_passphrase_is_accepted(self, ap_password):
        assert HotspotSettings().password() == AP_PASSWORD
        assert HotspotSettings().password_problem() is None

    def test_the_suggested_passphrase_is_typeable_on_a_phone(self):
        # Somebody is going to copy this onto a label and then type it in on a
        # stepladder. Lowercase hex has no l/1 or O/0 to get wrong.
        for _ in range(50):
            value = suggest_password()
            assert len(value) >= 8
            assert all(char in "0123456789abcdef" for char in value)

    def test_an_ssid_longer_than_32_bytes_is_refused(self):
        with pytest.raises(ValueError, match="32 bytes"):
            HotspotSettings(ssid="x" * 33)

    def test_a_multibyte_ssid_is_measured_in_bytes(self):
        # 802.11 counts bytes, so eleven Chinese characters is already 33.
        with pytest.raises(ValueError, match="32 bytes"):
            HotspotSettings(ssid="時" * 11)


# ------------------------------------------------------------------- monitor


class TestMonitor:
    """The decision to raise the access point, and to give it up again."""

    def test_online_stays_quiet(self, settings):
        monitor = HotspotMonitor(settings)
        decision = monitor.observe(Observation(now=0.0, online=True))
        assert decision.action is Action.NOTHING
        assert monitor.state is State.ONLINE

    def test_a_router_reboot_shorter_than_the_grace_period_costs_nothing(self, settings):
        # The thing that makes a fallback hotspot dangerous is raising it for
        # every blip. Four minutes of router reboot must not take the clock
        # off the network.
        monitor = HotspotMonitor(settings)
        monitor.observe(Observation(now=0.0, online=True))
        now = 0.0
        for _ in range(55):
            now += 1.0
            assert monitor.observe(Observation(now=now, online=False)).action is Action.NOTHING
        assert monitor.state is State.WAITING
        assert monitor.raises == 0
        # And when it comes back, nothing happened at all.
        assert monitor.observe(Observation(now=now + 1, online=True)).state is State.ONLINE
        assert monitor.raises == 0

    def test_offline_past_the_grace_period_raises_the_access_point(self, settings):
        monitor = HotspotMonitor(settings)
        monitor.observe(Observation(now=0.0, online=True))
        monitor.observe(Observation(now=1.0, online=False))
        decision = monitor.observe(Observation(now=70.0, online=False))
        assert decision.action is Action.RAISE_AP
        assert monitor.state is State.HOTSPOT
        assert monitor.raises == 1

    def test_an_access_point_nobody_visits_is_given_up(self, settings):
        monitor = _in_hotspot(settings)
        decision = monitor.observe(Observation(now=300.0, online=False, ap_active=True))
        assert decision.action is Action.LOWER_AP
        assert monitor.state is State.RETRYING
        assert monitor.give_ups == 1

    def test_somebody_using_the_portal_keeps_the_access_point_up(self, settings):
        monitor = _in_hotspot(settings)
        now = 100.0
        # A phone on the setup network fires captive-portal probes by itself,
        # so last_seen keeps moving while anybody is there.
        for _ in range(20):
            now += 30.0
            decision = monitor.observe(
                Observation(now=now, online=False, ap_active=True, portal_last_seen=now - 5)
            )
            if decision.action is Action.LOWER_AP:
                break
        # It still comes down eventually, at the cap: a phone that associated
        # and was carried away must not pin the radio for ever.
        assert monitor.state is State.RETRYING
        assert now - 100.0 >= settings.hotspot_max_s - 30.0

    def test_the_cap_applies_even_with_constant_activity(self, settings):
        monitor = _in_hotspot(settings)
        decision = monitor.observe(
            Observation(now=100.0 + settings.hotspot_max_s, online=False,
                        ap_active=True, portal_last_seen=100.0 + settings.hotspot_max_s)
        )
        assert decision.action is Action.LOWER_AP
        assert "cap" in decision.reason

    def test_after_giving_up_it_lets_wifi_retry_and_then_tries_again(self, settings):
        monitor = _in_hotspot(settings)
        monitor.observe(Observation(now=300.0, online=False, ap_active=True))
        assert monitor.state is State.RETRYING
        # Within the retry window: leave the radio alone.
        assert (
            monitor.observe(Observation(now=310.0, online=False)).action is Action.NOTHING
        )
        # Past it: put the access point back up.
        decision = monitor.observe(Observation(now=340.0, online=False))
        assert decision.action is Action.RAISE_AP
        assert monitor.state is State.HOTSPOT

    def test_wifi_coming_back_during_the_retry_window_ends_the_whole_thing(self, settings):
        monitor = _in_hotspot(settings)
        monitor.observe(Observation(now=300.0, online=False, ap_active=True))
        decision = monitor.observe(Observation(now=305.0, online=True))
        assert decision.action is Action.NOTHING
        assert monitor.state is State.ONLINE

    def test_an_access_point_that_fails_to_come_up_backs_off_instead_of_spinning(
        self, settings
    ):
        monitor = HotspotMonitor(settings)
        monitor.observe(Observation(now=0.0, online=True))
        monitor.observe(Observation(now=1.0, online=False))
        monitor.observe(Observation(now=70.0, online=False))
        assert monitor.state is State.HOTSPOT
        # nmcli said yes but the radio never came up.
        monitor.observe(Observation(now=71.0, online=False, ap_active=False))
        assert monitor.state is State.WAITING
        # It waits out the grace period again rather than retrying every poll.
        assert monitor.observe(Observation(now=90.0, online=False)).action is Action.NOTHING
        assert monitor.raises == 1

    def test_an_access_point_already_up_at_startup_is_adopted(self, settings):
        # The service was restarted while the AP was up. Adopting it starts
        # the idle timer; ignoring it would leave the AP up for ever.
        monitor = HotspotMonitor(settings)
        monitor.observe(Observation(now=0.0, online=False))
        monitor.observe(Observation(now=1.0, online=False, ap_active=True))
        assert monitor.state is State.HOTSPOT
        assert monitor.raises == 0

    def test_a_successful_join_reports_online_at_once(self, settings):
        monitor = _in_hotspot(settings)
        monitor.note_joined(200.0)
        assert monitor.state is State.ONLINE

    def test_being_blocked_means_never_touching_the_radio(self, settings):
        monitor = HotspotMonitor(settings, blocked="no password")
        for now in range(0, 10_000, 100):
            assert monitor.observe(Observation(now=now, online=False)).action is Action.NOTHING
        assert monitor.state is State.DISABLED
        assert monitor.raises == 0
        assert monitor.status()["blocked"] == "no password"

    def test_a_blocked_monitor_ignores_note_joined(self, settings):
        monitor = HotspotMonitor(settings, blocked="switched off")
        monitor.note_joined(1.0)
        assert monitor.state is State.DISABLED

    def test_it_never_asks_for_two_things_at_once(self, settings):
        """Fuzz the machine: every observation yields exactly one legal action."""
        import random

        random.seed(20261124)
        monitor = HotspotMonitor(settings)
        now = 0.0
        for _ in range(4000):
            now += random.choice([1.0, 5.0, 31.0, 61.0, 121.0, 301.0])
            observation = Observation(
                now=now,
                online=random.random() < 0.3,
                ap_active=monitor.state is State.HOTSPOT and random.random() < 0.9,
                portal_last_seen=(
                    now - random.choice([1.0, 200.0]) if random.random() < 0.5 else None
                ),
            )
            decision = monitor.observe(observation)
            assert decision.action in (Action.NOTHING, Action.RAISE_AP, Action.LOWER_AP)
            assert decision.state in set(State)
            # The access point is only ever raised from a state that has no
            # access point, and only lowered from one that has.
            if decision.action is Action.RAISE_AP:
                assert not observation.ap_active
            if decision.action is Action.LOWER_AP:
                assert observation.ap_active


def _in_hotspot(settings: HotspotSettings) -> HotspotMonitor:
    """A monitor that has just raised the access point, at t=100."""
    monitor = HotspotMonitor(settings)
    monitor.observe(Observation(now=0.0, online=True))
    monitor.observe(Observation(now=1.0, online=False))
    monitor.observe(Observation(now=100.0, online=False))
    assert monitor.state is State.HOTSPOT
    return monitor


# --------------------------------------------------------------- nmcli layer


class TestNetworkManagerLayer:
    """What we actually send to ``nmcli``, checked against a strict fake."""

    @pytest.fixture
    def fake(self, air):
        return FakeNmcli(air=list(air))

    @pytest.fixture
    def manager(self, fake):
        return nm.NetworkManager("wlan0", fake)

    async def test_a_scan_lists_the_networks_strongest_first(self, manager):
        networks = await manager.scan()
        assert [network.ssid for network in networks][0] == HOUSE
        assert [network.signal for network in networks] == sorted(
            (network.signal for network in networks), reverse=True
        )

    async def test_a_scan_reads_an_ssid_containing_a_colon(self, manager):
        networks = await manager.scan()
        guest = next(network for network in networks if network.ssid == "Smith: Guest")
        assert guest.open

    async def test_one_ssid_on_two_bands_is_offered_once(self, fake, manager):
        fake.air.append(FakeNetwork(HOUSE, HOUSE_PASSWORD, signal=35))
        networks = await manager.scan()
        matching = [network for network in networks if network.ssid == HOUSE]
        assert len(matching) == 1
        # The stronger of the two, so the list reads sensibly.
        assert matching[0].signal == 78

    async def test_a_hidden_network_is_not_offered(self, fake, manager):
        fake.air.append(FakeNetwork("", HOUSE_PASSWORD, signal=90))
        assert all(network.ssid for network in await manager.scan())

    async def test_a_failed_scan_is_an_empty_list_not_an_exception(self, air):
        fake = FakeNmcli(air=list(air), fail=(("device",),))
        assert await nm.NetworkManager("wlan0", fake).scan() == []

    async def test_the_ap_profile_never_autoconnects(self, fake, manager):
        # The single most dangerous setting in the package. An AP profile that
        # autoconnects can win the radio at boot, and then the clock is an
        # access point for ever and reachable by nobody.
        await manager.ensure_ap_profile("weasley-setup-ap", "weasley-clock-setup", AP_PASSWORD)
        profile = fake.profiles["weasley-setup-ap"]
        assert profile["connection.autoconnect"] == "no"
        assert profile["connection.autoconnect-priority"] == "0"
        assert profile["802-11-wireless.mode"] == "ap"
        assert profile["ipv4.method"] == "shared"

    async def test_an_ap_profile_edited_by_hand_is_put_back(self, fake, manager):
        await manager.ensure_ap_profile("weasley-setup-ap", "weasley-clock-setup", AP_PASSWORD)
        fake.profiles["weasley-setup-ap"]["connection.autoconnect"] = "yes"
        # Running again must correct it, not skip it because the profile exists.
        await manager.ensure_ap_profile("weasley-setup-ap", "weasley-clock-setup", AP_PASSWORD)
        assert fake.profiles["weasley-setup-ap"]["connection.autoconnect"] == "no"

    async def test_raising_the_ap_gives_the_interface_the_portal_address(self, fake, manager):
        await manager.ensure_ap_profile("weasley-setup-ap", "weasley-clock-setup", AP_PASSWORD)
        assert (await manager.up("weasley-setup-ap")).ok
        assert fake.addresses["wlan0"] == "10.42.0.1"

    async def test_joining_with_the_right_password_works(self, fake, manager):
        outcome = await manager.join(HOUSE, HOUSE_PASSWORD)
        assert outcome.ok, outcome.detail
        assert fake.active["wlan0"] == HOUSE
        assert fake.profiles[HOUSE]["connection.autoconnect-priority"] == "100"

    async def test_a_wrong_password_is_reported_in_plain_words(self, manager):
        outcome = await manager.join(HOUSE, "not-the-password")
        assert not outcome.ok
        assert outcome.detail == "the password was not accepted"

    async def test_a_wrong_password_leaves_no_profile_behind(self, fake, manager):
        # NetworkManager retries saved profiles for ever. One with a wrong
        # password and priority 100 would be preferred over a working network
        # indefinitely, so a failed join must clean up after itself.
        assert HOUSE not in fake.profiles
        await manager.join(HOUSE, "not-the-password")
        assert HOUSE not in fake.profiles

    async def test_a_network_that_is_not_there_says_so(self, manager):
        outcome = await manager.join("not-in-range", "whatever12")
        assert not outcome.ok
        assert "not in range" in outcome.detail

    async def test_an_existing_profile_is_modified_not_replaced(self, fake, manager):
        # Somebody may have set a static address or a hidden-network flag on
        # this profile deliberately. Deleting and recreating would lose it.
        fake.profiles[HOUSE] = {
            "type": "802-11-wireless",
            "802-11-wireless.ssid": HOUSE,
            "802-11-wireless.hidden": "yes",
            "802-11-wireless-security.psk": "the-old-password",
        }
        outcome = await manager.join(HOUSE, HOUSE_PASSWORD)
        assert outcome.ok
        assert fake.profiles[HOUSE]["802-11-wireless.hidden"] == "yes"
        assert fake.profiles[HOUSE]["802-11-wireless-security.psk"] == HOUSE_PASSWORD

    async def test_a_failed_join_does_not_delete_a_profile_it_did_not_create(
        self, fake, manager
    ):
        fake.profiles[HOUSE] = {
            "type": "802-11-wireless",
            "802-11-wireless.ssid": HOUSE,
            "802-11-wireless-security.psk": "the-old-password",
        }
        await manager.join(HOUSE, "still-wrong")
        assert HOUSE in fake.profiles

    async def test_an_open_network_can_be_joined_with_no_password(self, fake, manager):
        outcome = await manager.join("Smith: Guest", "")
        assert outcome.ok, outcome.detail
        assert fake.active["wlan0"] == "Smith: Guest"

    async def test_moving_a_wpa_profile_to_an_open_network_clears_key_mgmt(
        self, fake, manager
    ):
        # Leaving the old key-mgmt behind makes the join fail with a security
        # mismatch, which reads as "the network is broken" rather than
        # "the profile remembers being WPA".
        fake.profiles["Smith: Guest"] = {
            "type": "802-11-wireless",
            "802-11-wireless.ssid": "Smith: Guest",
            "802-11-wireless-security.key-mgmt": "wpa-psk",
            "802-11-wireless-security.psk": "stale",
        }
        outcome = await manager.join("Smith: Guest", "")
        assert outcome.ok, outcome.detail

    async def test_a_profile_named_like_an_option_is_not_parsed_as_one(self, fake, manager):
        # An SSID is whatever the neighbours typed into their router. Every
        # command selects a profile with `id <name>`, so nmcli cannot mistake
        # "--help" for a flag.
        fake.air.append(FakeNetwork("--help", "open-sesame", signal=50))
        outcome = await manager.join("--help", "open-sesame")
        assert outcome.ok, outcome.detail
        assert any(
            call[1:4] == ["connection", "up", "id"] for call in fake.calls
        ), "profile names must be selected with an explicit id"

    async def test_bringing_down_something_already_down_is_not_an_error(self, manager):
        assert (await manager.down("weasley-setup-ap")).ok

    async def test_online_excludes_a_tunnel(self, fake, manager):
        fake.profiles["tailscale0"] = {"type": "tun"}
        fake.active["tailscale0"] = "tailscale0"
        fake.addresses["tailscale0"] = "100.64.1.2"
        actives = await manager.active_connections()
        tunnel = next(active for active in actives if active.device == "tailscale0")
        assert not tunnel.carries_traffic

    async def test_a_connected_interface_with_no_lease_has_no_address(self, air):
        fake = FakeNmcli(air=list(air), dhcp=False)
        manager = nm.NetworkManager("wlan0", fake)
        await manager.join(HOUSE, HOUSE_PASSWORD)
        assert (await manager.device_state()).connected
        assert await manager.ip4() is None

    async def test_saved_wifi_profiles_come_back_highest_priority_first(self, fake, manager):
        await manager.join(HOUSE, HOUSE_PASSWORD, priority=100)
        fake.profiles["Boulder-House"] = {
            "type": "802-11-wireless",
            "connection.autoconnect-priority": "10",
        }
        assert [name for name, _ in await manager.wifi_profiles()][0] == HOUSE

    async def test_a_field_the_fake_does_not_model_is_rejected(self, fake):
        # Proves the fake is strict, which is what makes the rest of these
        # tests worth anything: a typo in a field name fails here.
        result = await fake(["nmcli", "-t", "-f", "NOSUCHFIELD", "device", "status"])
        assert not result.ok
        assert "invalid field" in result.message

    async def test_the_fake_rejects_a_command_nmcli_would_reject(self, fake):
        assert not (await fake(["nmcli", "widget", "dance"])).ok
        assert not (await fake(["nmcli", "connection", "sing"])).ok
        assert not (await fake(["nmcli", "connection", "up", "id"])).ok

    async def test_the_fake_rejects_a_setting_with_no_value(self, fake):
        result = await fake(
            ["nmcli", "connection", "add", "type", "wifi", "con-name", "x",
             "ssid", "x", "--", "ipv4.method"]
        )
        assert not result.ok


# ------------------------------------------------------------------- service


class FakePortal:
    """Stands in for uvicorn, recording when the portal was up."""

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.running = False
        self.bound: list[tuple[str, int]] = []
        #: Addresses that are not yet on the interface, so bind fails.
        self.refuse: set[str] = set()

    async def __call__(self, app, host, port):
        if host in self.refuse:
            raise OSError(99, "Cannot assign requested address")
        self.started += 1
        self.running = True
        self.bound.append((host, port))
        handle = FakePortal._Handle(self)
        return handle

    class _Handle:
        def __init__(self, outer):
            self.outer = outer

        async def stop(self):
            self.outer.stopped += 1
            self.outer.running = False


def _service(settings, fake, clock, portal=None):
    """A service wired to the fake NetworkManager and a fake clock."""
    async def no_sleep(_seconds):
        return None

    return HotspotService(
        settings,
        nm.NetworkManager(settings.interface, fake),
        serve=portal if portal is not None else FakePortal(),
        clock=clock,
        sleep=no_sleep,
    )


class TestService:
    """The whole thing, against the fake NetworkManager."""

    @pytest.fixture(autouse=True)
    def _nmcli_is_present(self, monkeypatch, ap_password):
        # `NetworkManager.present()` shells out to shutil.which, which is
        # false in CI; the fake is what stands in for the binary.
        monkeypatch.setattr(nm.NetworkManager, "present", staticmethod(lambda nmcli="nmcli": True))

    async def test_a_clock_on_the_wifi_is_left_alone(self, settings, air, ap_password):
        clock = Clock()
        fake = FakeNmcli(air=list(air))
        service = _service(settings, fake, clock)
        await service.manager.join(HOUSE, HOUSE_PASSWORD)
        before = len(fake.calls)
        for _ in range(20):
            clock.advance(settings.check_interval_s)
            assert await service.tick() is Action.NOTHING
        assert service.monitor.state is State.ONLINE
        # It polls, but it changes nothing.
        assert fake.active["wlan0"] == HOUSE
        assert before < len(fake.calls)

    async def test_a_wired_clock_never_raises_an_access_point(self, settings, air, ap_password):
        # A Pi on ethernet is reachable over Tailscale and needs no rescuing;
        # taking its WiFi away to serve a setup page would be pure harm.
        clock = Clock()
        fake = FakeNmcli(air=list(air), wired=True)
        service = _service(settings, fake, clock)
        for _ in range(50):
            clock.advance(60.0)
            await service.tick()
        assert service.monitor.state is State.ONLINE
        assert service.monitor.raises == 0

    async def test_the_access_point_goes_up_after_the_grace_period(
        self, settings, air, ap_password
    ):
        clock = Clock()
        fake = FakeNmcli(air=list(air))
        portal = FakePortal()
        service = _service(settings, fake, clock, portal)

        clock.advance(1.0)
        await service.tick()  # offline, start the grace period
        assert service.monitor.state is State.WAITING
        clock.advance(settings.offline_grace_s + 1)
        assert await service.tick() is Action.RAISE_AP

        assert fake.active["wlan0"] == settings.ap_profile
        assert fake.addresses["wlan0"] == "10.42.0.1"
        assert portal.running
        assert portal.bound == [(settings.portal_host, settings.portal_port)]

    async def test_it_scans_before_taking_the_radio_and_never_after(
        self, settings, air, ap_password
    ):
        # A single-radio Pi cannot scan from AP mode: it returns the stale
        # cache or knocks the access point over. So the list the portal
        # offers has to be captured on the way in.
        clock = Clock()
        fake = FakeNmcli(air=list(air))
        service = _service(settings, fake, clock)
        clock.advance(1.0)
        await service.tick()
        clock.advance(settings.offline_grace_s + 1)
        await service.tick()
        assert fake.scans == 1
        assert not fake.scanned_while_ap
        assert [network.ssid for network in service.state.networks][0] == HOUSE
        assert service.state.scanned_at == clock.now

        # And it keeps not scanning while the access point is up.
        for _ in range(5):
            clock.advance(settings.check_interval_s)
            await service.tick()
        assert fake.scans == 1
        assert not fake.scanned_while_ap

    async def test_the_shared_address_is_not_mistaken_for_being_online(
        self, settings, air, ap_password
    ):
        # The trap: `ipv4.method shared` gives wlan0 10.42.0.1 and
        # NetworkManager reports it `connected`. Counting that as online would
        # make the clock serve a setup page for ever.
        clock = Clock()
        fake = FakeNmcli(air=list(air))
        service = _service(settings, fake, clock)
        clock.advance(1.0)
        await service.tick()
        clock.advance(settings.offline_grace_s + 1)
        await service.tick()
        assert fake.addresses["wlan0"] == "10.42.0.1"
        observation = await service.observe()
        assert observation.ap_active
        assert not observation.online

    async def test_an_access_point_nobody_visits_is_dropped_and_wifi_retried(
        self, settings, air, ap_password
    ):
        clock = Clock()
        fake = FakeNmcli(air=list(air))
        portal = FakePortal()
        service = _service(settings, fake, clock, portal)
        clock.advance(1.0)
        await service.tick()
        clock.advance(settings.offline_grace_s + 1)
        await service.tick()
        assert portal.running

        clock.advance(settings.hotspot_idle_s + 1)
        assert await service.tick() is Action.LOWER_AP
        assert not portal.running
        assert "wlan0" not in fake.active
        assert service.monitor.state is State.RETRYING

    async def test_the_house_wifi_coming_back_on_its_own_is_picked_up(
        self, settings, air, ap_password
    ):
        """The scenario the whole cycle exists for: a long power cut.

        Nobody touches the clock.  The router is away for half an hour, the
        access point goes up, nobody ever joins it, and when the router comes
        back the clock finds it by itself.
        """
        clock = Clock()
        fake = FakeNmcli(air=[])  # the router is down: nothing in the air
        portal = FakePortal()
        service = _service(settings, fake, clock, portal)
        fake.profiles[HOUSE] = {
            "type": "802-11-wireless",
            "802-11-wireless.ssid": HOUSE,
            "802-11-wireless-security.psk": HOUSE_PASSWORD,
            "connection.autoconnect": "yes",
        }

        clock.advance(1.0)
        await service.tick()
        clock.advance(settings.offline_grace_s + 1)
        await service.tick()
        assert service.monitor.state is State.HOTSPOT

        # Half an hour of nobody coming. The access point goes up and down.
        for _ in range(60):
            clock.advance(30.0)
            await service.tick()
        assert service.monitor.give_ups >= 2

        # The router comes back, and NetworkManager finds the saved profile.
        fake.air.append(FakeNetwork(HOUSE, HOUSE_PASSWORD, signal=70))
        while service.monitor.state is not State.RETRYING:
            clock.advance(30.0)
            await service.tick()
        await service.manager.up(HOUSE)  # what NetworkManager's own retry does
        clock.advance(settings.check_interval_s)
        await service.tick()
        assert service.monitor.state is State.ONLINE
        assert not portal.running

    async def test_the_portal_binding_is_retried_until_the_address_appears(
        self, settings, air, ap_password
    ):
        # `ipv4.method shared` adds 10.42.0.1 a moment after the connection
        # reports active, so the first bind fails.
        clock = Clock()
        fake = FakeNmcli(air=list(air))
        portal = FakePortal()
        portal.refuse = {settings.portal_host}
        service = _service(settings, fake, clock, portal)

        async def relent(_seconds):
            portal.refuse.clear()

        service.sleep = relent
        clock.advance(1.0)
        await service.tick()
        clock.advance(settings.offline_grace_s + 1)
        await service.tick()
        assert portal.running

    async def test_a_bind_that_never_succeeds_gives_up_without_crashing(
        self, settings, air, ap_password
    ):
        clock = Clock()
        fake = FakeNmcli(air=list(air))
        portal = FakePortal()
        portal.refuse = {settings.portal_host}
        service = _service(settings, fake, clock, portal)

        async def tick_clock(_seconds):
            clock.advance(5.0)

        service.sleep = tick_clock
        clock.advance(1.0)
        await service.tick()
        clock.advance(settings.offline_grace_s + 1)
        await service.tick()
        # The access point is up even though the page is not, which is the
        # right way round: the log says so and the next cycle retries.
        assert not portal.running
        assert fake.active["wlan0"] == settings.ap_profile

    async def test_no_ap_password_means_the_radio_is_never_touched(
        self, settings, air, monkeypatch
    ):
        monkeypatch.delenv(PASSWORD_ENV, raising=False)
        clock = Clock()
        fake = FakeNmcli(air=list(air))
        service = _service(settings, fake, clock)
        assert service.monitor.state is State.DISABLED
        assert PASSWORD_ENV in service.monitor.blocked
        for _ in range(50):
            clock.advance(600.0)
            await service.tick()
        # Polling reads NetworkManager, which is harmless; what must never
        # happen is a command that changes anything.
        mutating = {"add", "modify", "up", "down", "delete"}
        assert not [call for call in fake.calls if mutating & set(call)]
        assert service.monitor.raises == 0

    async def test_being_switched_off_in_config_means_doing_nothing(self, air, ap_password):
        clock = Clock()
        fake = FakeNmcli(air=list(air))
        service = _service(HotspotSettings(enabled=False), fake, clock)
        assert service.monitor.state is State.DISABLED
        await service.tick()
        assert service.monitor.raises == 0

    async def test_no_nmcli_at_all_is_one_clear_line_not_a_crash(
        self, settings, monkeypatch, ap_password
    ):
        # The laptop and CI case. It must be inert, not noisy.
        monkeypatch.setattr(
            nm.NetworkManager, "present", staticmethod(lambda nmcli="nmcli": False)
        )
        service = HotspotService(settings, nm.NetworkManager("wlan0", FakeNmcli()))
        assert service.monitor.state is State.DISABLED
        assert "nmcli" in service.monitor.blocked
        await service.run()  # returns at once rather than looping

    async def test_stopping_the_service_leaves_the_radio_as_a_client(
        self, settings, air, ap_password
    ):
        # An upgrade restart must not leave the Pi as an access point with
        # nothing serving the page.
        clock = Clock()
        fake = FakeNmcli(air=list(air))
        portal = FakePortal()
        service = _service(settings, fake, clock, portal)
        clock.advance(1.0)
        await service.tick()
        clock.advance(settings.offline_grace_s + 1)
        await service.tick()
        assert fake.active["wlan0"] == settings.ap_profile

        await service.stop()
        assert "wlan0" not in fake.active
        assert not portal.running

    async def test_a_poll_that_throws_does_not_end_the_loop(self, settings, ap_password):
        clock = Clock()
        fake = FakeNmcli(air=[], fail=(("connection", "show"),))
        service = _service(settings, fake, clock)
        for _ in range(5):
            clock.advance(settings.check_interval_s)
            await service.tick()
        # nmcli is answering nothing useful, so it stays in WAITING rather
        # than raising an access point on no information.
        assert service.monitor.state in (State.WAITING, State.HOTSPOT)


# -------------------------------------------------------------------- portal


class TestValidation:
    """What the form accepts, in the words it says it in."""

    @pytest.mark.parametrize("value", ["", "   "])
    def test_no_network_is_refused(self, value):
        assert validate_ssid(value) is not None

    def test_a_network_name_with_a_newline_in_it_is_refused(self):
        assert validate_ssid("house\nname") is not None

    def test_an_overlong_network_name_is_refused(self):
        assert validate_ssid("x" * 33) is not None

    def test_a_normal_name_is_accepted(self):
        assert validate_ssid(" Minnetonka-5G ") is None

    def test_a_short_password_says_what_is_wrong(self):
        problem = validate_password("abc", network=None)
        assert problem is not None and "8 characters" in problem

    def test_an_open_network_needs_no_password(self):
        assert validate_password("", network=nm.Network("Guest", 40, "")) is None

    def test_a_secured_network_needs_one(self):
        assert validate_password("", network=nm.Network("House", 40, "WPA2")) is not None


class TestPortalPage:
    """The page itself, rendered."""

    @pytest.fixture
    def state(self, air):
        value = PortalState()
        value.set_scan(
            [nm.Network(network.ssid, network.signal, "" if network.open else network.security)
             for network in air],
            now=1_000_000.0,
        )
        return value

    def test_it_offers_the_networks_it_found(self, state, settings):
        html_text = render_form(state, settings, now=1_000_060.0)
        assert HOUSE in html_text
        assert "neighbour-2.4" in html_text

    def test_an_ssid_with_html_in_it_is_escaped(self, settings):
        state = PortalState()
        state.set_scan([nm.Network('<script>alert(1)</script>', 50, "WPA2")], now=0.0)
        html_text = render_form(state, settings, now=1.0)
        assert "<script>alert" not in html_text
        assert "&lt;script&gt;" in html_text

    def test_an_ssid_with_a_quote_cannot_break_out_of_the_option_value(self, settings):
        state = PortalState()
        state.set_scan([nm.Network('a" onfocus="x', 50, "WPA2")], now=0.0)
        html_text = render_form(state, settings, now=1.0)
        assert 'onfocus="x"' not in html_text
        assert "&quot;" in html_text

    def test_it_says_how_old_the_list_is(self, state, settings):
        # The list is minutes old by the time anyone reads it, because the
        # radio cannot scan while it is an access point. Saying so is what
        # makes the free-text box make sense.
        html_text = render_form(state, settings, now=1_000_000.0 + 240)
        assert "4 minutes ago" in html_text

    def test_there_is_always_a_way_to_type_a_name_that_was_not_found(self, state, settings):
        assert 'name=other' in render_form(state, settings, now=1_000_001.0)
        empty = render_form(PortalState(), settings, now=1.0)
        assert 'name=other' in empty

    def test_the_last_failure_is_shown_to_whoever_comes_back(self, state, settings):
        # The only feedback channel after a failed join is the setup network
        # reappearing, so the reason has to be waiting on the page.
        state.record(HOUSE, False, "the password was not accepted", now=1_000_010.0)
        html_text = render_form(state, settings, now=1_000_100.0)
        assert "did not work" in html_text
        assert "the password was not accepted" in html_text

    def test_there_is_a_show_password_control(self, state, settings):
        # Typing a long WPA passphrase blind on a phone, on a stepladder, is
        # the single most likely way for this to fail for no good reason.
        assert "Show the password" in render_form(state, settings, now=1.0)

    def test_the_submitted_page_explains_that_the_network_will_vanish(self, settings):
        html_text = render_submitted(HOUSE, settings)
        # This is the last page the phone can load, so it has to carry the
        # whole rest of the procedure.
        assert "disappear" in html_text
        assert settings.ssid in html_text
        assert "stays gone" in html_text
        assert "comes back" in html_text

    def test_the_page_pulls_in_nothing_from_the_internet(self, state, settings):
        # The device reading this has no route anywhere. A stylesheet or font
        # from a CDN would hang and then render unstyled.
        html_text = render_form(state, settings, now=1.0)
        for marker in ("http://", "https://", "//cdn", "src="):
            assert marker not in html_text, marker

    def test_the_page_needs_no_javascript_to_work(self, state, settings):
        html_text = render_form(state, settings, now=1.0)
        # One inline onclick for the show-password box, and no script tags.
        assert "<script" not in html_text
        assert "<form method=post action=/join>" in html_text


class TestPortalApp:
    """The portal over HTTP, through FastAPI's test client."""

    @pytest.fixture
    def pieces(self, air, settings, ap_password):
        from fastapi.testclient import TestClient

        clock = Clock()
        fake = FakeNmcli(air=list(air))
        manager = nm.NetworkManager("wlan0", fake)
        state = PortalState()
        state.set_scan(
            [nm.Network(network.ssid, network.signal, "" if network.open else network.security)
             for network in air],
            now=clock.now,
        )
        joins: list[tuple[str, object]] = []

        async def on_join(ssid, outcome):
            joins.append((ssid, outcome))

        app = build_app(state, settings, manager, on_join=on_join, now=clock)
        return TestClient(app), state, fake, joins, clock

    def test_the_root_page_loads(self, pieces):
        client, _state, _fake, _joins, _clock = pieces
        response = client.get("/")
        assert response.status_code == 200
        assert HOUSE in response.text

    def test_every_request_counts_as_somebody_being_there(self, pieces):
        client, state, _fake, _joins, clock = pieces
        state.last_seen = None
        clock.advance(500.0)
        client.get("/")
        assert state.last_seen == clock.now

    @pytest.mark.parametrize("path", DETECTION_PATHS)
    def test_a_connectivity_probe_is_redirected_so_the_page_pops_up(self, pieces, path):
        # Answering with the payload the OS expects would make the phone
        # decide it has internet and show nothing at all, leaving the person
        # to find an IP address by themselves.
        client, _state, _fake, _joins, _clock = pieces
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/"

    def test_a_probe_also_marks_activity(self, pieces):
        # This is what makes the idle timer work without counting associated
        # stations: a phone fires these within a second of joining.
        client, state, _fake, _joins, clock = pieces
        state.last_seen = None
        client.get("/generate_204", follow_redirects=False)
        assert state.last_seen is not None

    def test_any_other_url_lands_on_the_setup_page(self, pieces):
        client, _state, _fake, _joins, _clock = pieces
        response = client.get("/some/deep/link?a=1", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/"

    def test_a_good_join_is_accepted_and_performed(self, pieces):
        client, state, fake, joins, _clock = pieces
        response = client.post("/join", data={"ssid": HOUSE, "password": HOUSE_PASSWORD})
        assert response.status_code == 200
        assert "disappear" in response.text
        assert fake.active["wlan0"] == HOUSE
        assert state.last_attempt["ok"]
        assert joins and joins[0][0] == HOUSE

    def test_a_typed_name_wins_over_the_dropdown(self, pieces):
        client, _state, fake, _joins, _clock = pieces
        fake.air.append(FakeNetwork("typed-one", "typed-pass", signal=60))
        client.post(
            "/join",
            data={"ssid": HOUSE, "other": "typed-one", "password": "typed-pass"},
        )
        assert fake.active["wlan0"] == "typed-one"

    def test_a_wrong_password_is_recorded_for_the_next_visitor(self, pieces):
        client, state, fake, _joins, _clock = pieces
        client.post("/join", data={"ssid": HOUSE, "password": "wrong-one-here"})
        assert state.last_attempt["ok"] is False
        assert state.last_attempt["detail"] == "the password was not accepted"
        # And nothing was left behind for NetworkManager to retry for ever.
        assert HOUSE not in fake.profiles
        # The reason is on the page when they rejoin the setup network.
        assert "the password was not accepted" in client.get("/").text

    def test_a_short_password_is_refused_without_touching_the_radio(self, pieces):
        client, _state, fake, _joins, _clock = pieces
        response = client.post("/join", data={"ssid": HOUSE, "password": "abc"})
        assert response.status_code == 400
        assert "8 characters" in response.text
        assert "wlan0" not in fake.active

    def test_choosing_nothing_at_all_is_refused_politely(self, pieces):
        client, _state, _fake, _joins, _clock = pieces
        response = client.post("/join", data={"ssid": "", "password": "whatever12"})
        assert response.status_code == 400
        assert "Choose a network" in response.text

    def test_an_open_network_joins_with_no_password(self, pieces):
        client, _state, fake, _joins, _clock = pieces
        response = client.post("/join", data={"ssid": "Smith: Guest", "password": ""})
        assert response.status_code == 200
        assert fake.active["wlan0"] == "Smith: Guest"

    def test_the_status_page_leaks_no_passphrase(self, pieces):
        # The portal has no login -- the access point's WPA2 password is the
        # only gate -- so it must not hand out anything worth having.
        client, _state, fake, _joins, _clock = pieces
        client.post("/join", data={"ssid": HOUSE, "password": HOUSE_PASSWORD})
        body = client.get("/status").text
        assert HOUSE in body  # the name is fine
        assert HOUSE_PASSWORD not in body
        assert AP_PASSWORD not in body
        # And nowhere in the package do we ever ask nmcli to print secrets.
        assert not [call for call in fake.calls if "--show-secrets" in call]

    def test_no_module_in_the_package_imports_the_clock_config(self):
        """The hotspot must not depend on the thing it exists to rescue.

        ``clockd`` refuses to start on a ``config/clock.yaml`` that does not
        validate, which is correct for something that drives six motors and
        fatal for the way back in.  Checked by reading the imports rather
        than grepping the text, so the module docstrings can go on explaining
        why.
        """
        import ast

        import clockd.hotspot as package

        root = pathlib.Path(package.__file__).parent
        modules = sorted(root.glob("*.py"))
        assert len(modules) >= 6, "expected the whole package to be checked"
        for path in modules:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    # `from ..config import ...` is level 2, module "config".
                    assert not (node.level == 2 and (node.module or "").startswith("config")), (
                        f"{path.name} imports clockd.config"
                    )
                    assert not (node.module or "").startswith("clockd.config"), path.name
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("clockd.config"), path.name


class TestTheWholeRescue:
    """The journey the family actually makes, end to end."""

    async def test_someone_standing_at_the_clock_gets_it_back_online(
        self, settings, air, ap_password, monkeypatch
    ):
        """WiFi password changed; nobody technical within 2000 km.

        The clock drops off, raises its setup network, somebody joins it on a
        phone, picks the house WiFi, types the new password, and the clock
        rejoins and takes the setup network away again.
        """
        from fastapi.testclient import TestClient

        monkeypatch.setattr(
            nm.NetworkManager, "present", staticmethod(lambda nmcli="nmcli": True)
        )
        clock = Clock()
        # The house kept its name and changed its password.
        fake = FakeNmcli(air=[FakeNetwork(HOUSE, "the-new-password", signal=80)])
        fake.profiles[HOUSE] = {
            "type": "802-11-wireless",
            "802-11-wireless.ssid": HOUSE,
            "802-11-wireless-security.psk": "the-old-password",
            "connection.autoconnect": "yes",
            "connection.autoconnect-priority": "50",
        }
        portal = FakePortal()
        service = _service(settings, fake, clock, portal)

        # 1. It cannot get on, and waits out the grace period first.
        clock.advance(1.0)
        await service.tick()
        assert service.monitor.state is State.WAITING
        assert portal.started == 0

        # 2. The setup network goes up, with a scan taken on the way in.
        clock.advance(settings.offline_grace_s + 1)
        await service.tick()
        assert service.monitor.state is State.HOTSPOT
        assert fake.active["wlan0"] == settings.ap_profile
        assert portal.running

        # 3. A phone joins it. Its captive-portal probe pops the page up.
        app = build_app(
            service.state, settings, service.manager, on_join=service._joined, now=clock
        )
        client = TestClient(app)
        assert client.get("/generate_204", follow_redirects=False).status_code == 302
        page_text = client.get("/").text
        assert HOUSE in page_text

        # 4. First attempt: they mistype the password.
        client.post("/join", data={"ssid": HOUSE, "password": "the-wrng-password"})
        assert service.state.last_attempt["ok"] is False
        # The profile existed already, so it was modified rather than dropped.
        assert HOUSE in fake.profiles

        # The radio is now disconnected, so the monitor puts the setup
        # network back up after the grace period -- which is exactly what the
        # page promised would happen.
        clock.advance(settings.check_interval_s)
        await service.tick()
        assert service.monitor.state is State.WAITING
        clock.advance(settings.offline_grace_s + 1)
        await service.tick()
        assert service.monitor.state is State.HOTSPOT
        assert fake.active["wlan0"] == settings.ap_profile

        # 5. They rejoin, and the page tells them what went wrong.
        assert "did not work" in client.get("/").text

        # 6. Second attempt, right this time.
        client.post("/join", data={"ssid": HOUSE, "password": "the-new-password"})
        assert service.state.last_attempt["ok"] is True
        assert fake.active["wlan0"] == HOUSE
        assert fake.addresses["wlan0"] == "192.168.1.50"
        assert not portal.running

        # 7. The clock is back, and stays back. The new password is saved with
        #    a priority that beats the stale profiles, so a reboot finds it.
        assert fake.profiles[HOUSE]["802-11-wireless-security.psk"] == "the-new-password"
        assert int(fake.profiles[HOUSE]["connection.autoconnect-priority"]) == (
            settings.join_priority
        )
        for _ in range(30):
            clock.advance(60.0)
            assert await service.tick() is Action.NOTHING
        assert service.monitor.state is State.ONLINE

        # 8. And the access point profile that got us here still cannot ever
        #    autoconnect, which is what stops it stealing the radio at boot.
        assert fake.profiles[settings.ap_profile]["connection.autoconnect"] == "no"


class TestPortalCopyTracksTheSettings:
    """The page must not promise something the monitor does not do."""

    def test_the_quoted_wait_comes_from_the_timings(self):
        state = PortalState()
        settings = HotspotSettings(hotspot_idle_s=600.0, retry_wifi_s=180.0)
        assert "about 13 minutes" in render_form(state, settings, now=1.0)

        # Retune the timings and the page follows, rather than going stale.
        retuned = HotspotSettings(hotspot_idle_s=120.0, retry_wifi_s=60.0)
        assert "about 3 minutes" in render_form(state, retuned, now=1.0)

"""Alerting: the dead-man's switch, push notifications, debounce and cooldown.

No network: a fake httpx client records every request, so the tests assert on
what would have been sent.
"""

from __future__ import annotations

import pytest

from clockd.alerts import Alerter

NOW = 1_762_000_000.0


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class FakeClient:
    """Stands in for httpx.AsyncClient and records what it was asked to send."""

    def __init__(self, log, status_code=200, explode=False):
        self.log = log
        self.status_code = status_code
        self.explode = explode

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, **_kwargs):
        if self.explode:
            raise OSError("network is down")
        self.log.append(("GET", url, None, {}))
        return FakeResponse(self.status_code)

    async def post(self, url, content=None, headers=None, **_kwargs):
        if self.explode:
            raise OSError("network is down")
        self.log.append(("POST", url, content.decode() if content else None, headers or {}))
        return FakeResponse(self.status_code)


@pytest.fixture
def alerter(sim_config, monkeypatch):
    """An enabled alerter with both destinations set and a recording client."""
    from clockd.eventlog import EventLog

    sim_config.alerts.enabled = True
    monkeypatch.setenv("HEALTHCHECKS_URL", "https://hc.example/abc123")
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/weasley-test")
    sent: list = []
    instance = Alerter(sim_config, EventLog(100), client_factory=lambda: FakeClient(sent))
    instance.sent = sent
    return instance


# -- configuration -----------------------------------------------------------


async def test_disabled_by_default(sim_config):
    from clockd.eventlog import EventLog

    instance = Alerter(sim_config, EventLog(10))
    assert not instance.enabled
    assert not await instance.ping()
    assert not await instance.notify("t", "m")


async def test_urls_come_from_the_environment_only(alerter, monkeypatch):
    """A ping URL is a credential; it must never be in the YAML."""
    assert alerter.healthchecks_url() == "https://hc.example/abc123"
    assert alerter.ntfy_url() == "https://ntfy.sh/weasley-test"
    monkeypatch.delenv("HEALTHCHECKS_URL")
    monkeypatch.delenv("NTFY_URL")
    assert alerter.healthchecks_url() is None
    assert not alerter.configured()


async def test_enabled_without_a_url_says_so_rather_than_failing_silently(
    sim_config, monkeypatch
):
    from clockd.eventlog import EventLog

    sim_config.alerts.enabled = True
    monkeypatch.delenv("HEALTHCHECKS_URL", raising=False)
    monkeypatch.delenv("NTFY_URL", raising=False)
    events = EventLog(10)
    instance = Alerter(sim_config, events)
    await instance.start()
    assert any("neither" in e.message for e in events.events)


# -- sending -----------------------------------------------------------------


async def test_a_health_ping_is_a_plain_get(alerter):
    assert await alerter.ping()
    assert alerter.sent == [("GET", "https://hc.example/abc123", None, {})]
    assert alerter.pings_sent == 1
    assert alerter.last_ping_at is not None


@pytest.mark.parametrize("suffix", ["/fail", "/start"])
async def test_ping_suffixes(alerter, suffix):
    await alerter.ping(suffix)
    assert alerter.sent[0][1] == "https://hc.example/abc123" + suffix


async def test_a_notification_carries_title_priority_and_tags(alerter):
    assert await alerter.notify("Title here", "Body here", priority="high", tags="warning")
    method, url, body, headers = alerter.sent[0]
    assert method == "POST"
    assert url == "https://ntfy.sh/weasley-test"
    assert body == "Body here"
    assert headers["Title"] == "Title here"
    assert headers["Priority"] == "high"
    assert headers["Tags"] == "warning"


async def test_a_network_failure_is_counted_not_raised(sim_config, monkeypatch):
    """A clock that crashes because ntfy.sh is down is worse than no alerts."""
    from clockd.eventlog import EventLog

    sim_config.alerts.enabled = True
    monkeypatch.setenv("HEALTHCHECKS_URL", "https://hc.example/abc")
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/x")
    instance = Alerter(
        sim_config, EventLog(10), client_factory=lambda: FakeClient([], explode=True)
    )
    assert not await instance.ping()
    assert not await instance.notify("t", "m")
    assert instance.failures == 2
    assert "network is down" in instance.last_error


async def test_an_http_error_counts_as_a_failure(sim_config, monkeypatch):
    from clockd.eventlog import EventLog

    sim_config.alerts.enabled = True
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/x")
    instance = Alerter(
        sim_config, EventLog(10), client_factory=lambda: FakeClient([], status_code=500)
    )
    assert not await instance.notify("t", "m")
    assert instance.failures == 1
    assert "HTTP 500" in instance.last_error


# -- debounce and cooldown ---------------------------------------------------


async def test_a_brief_blip_never_notifies(alerter):
    """The Pico link drops during a firmware upload; that is not an emergency."""
    assert not await alerter.condition("pico-down", True, "down", "msg", now=NOW)
    assert not await alerter.condition("pico-down", True, "down", "msg", now=NOW + 30)
    assert not await alerter.condition("pico-down", False, "down", "msg", now=NOW + 40)
    assert alerter.notifications_sent == 0


async def test_a_sustained_condition_notifies_once(alerter):
    assert not await alerter.condition("pico-down", True, "down", "msg", now=NOW)
    assert await alerter.condition("pico-down", True, "down", "msg", now=NOW + 61)
    assert alerter.notifications_sent == 1
    # And not again while it is still true.
    assert not await alerter.condition("pico-down", True, "down", "msg", now=NOW + 120)
    assert not await alerter.condition("pico-down", True, "down", "msg", now=NOW + 300)
    assert alerter.notifications_sent == 1


async def test_a_flapping_link_does_not_become_hundreds_of_messages(alerter):
    """Ten up/down cycles in a minute produce no alert at all, not twenty."""
    time_now = NOW
    for _ in range(10):
        await alerter.condition("pico-down", True, "down", "msg", now=time_now)
        time_now += 3
        await alerter.condition("pico-down", False, "down", "msg", now=time_now)
        time_now += 3
    assert alerter.notifications_sent == 0


async def test_recovery_sends_one_message_and_resets(alerter):
    await alerter.condition("pico-down", True, "down", "msg", now=NOW)
    await alerter.condition("pico-down", True, "down", "msg", now=NOW + 61)
    assert alerter.notifications_sent == 1

    assert await alerter.condition("pico-down", False, "down", "msg", now=NOW + 100)
    assert alerter.notifications_sent == 2
    recovery = alerter.sent[-1]
    assert "recovered" in recovery[3]["Title"]
    assert recovery[3]["Priority"] == "low"

    # Having recovered, it can alert again straight away if it breaks again.
    await alerter.condition("pico-down", True, "down", "msg", now=NOW + 200)
    assert await alerter.condition("pico-down", True, "down", "msg", now=NOW + 300)


async def test_recovery_on_a_condition_that_never_fired_is_silent(alerter):
    assert not await alerter.condition("pico-down", False, "down", "msg", now=NOW)
    assert alerter.notifications_sent == 0


async def test_the_cooldown_is_per_condition(alerter):
    await alerter.condition("a", True, "A", "msg", now=NOW)
    await alerter.condition("b", True, "B", "msg", now=NOW)
    assert await alerter.condition("a", True, "A", "msg", now=NOW + 61)
    assert await alerter.condition("b", True, "B", "msg", now=NOW + 61)
    assert alerter.notifications_sent == 2


async def test_a_real_problem_also_fails_the_dead_mans_switch(alerter):
    """So an unread notification still surfaces through healthchecks."""
    await alerter.condition("pico-down", True, "down", "msg", now=NOW)
    await alerter.condition("pico-down", True, "down", "msg", now=NOW + 61)
    gets = [entry for entry in alerter.sent if entry[0] == "GET"]
    assert any(url.endswith("/fail") for _m, url, _b, _h in gets)


async def test_describe_reports_what_the_system_page_shows(alerter):
    await alerter.condition("pico-down", True, "down", "msg", now=NOW)
    await alerter.condition("pico-down", True, "down", "msg", now=NOW + 61)
    described = alerter.describe()
    assert described["enabled"] is True
    assert described["healthchecks"] is True
    assert described["ntfy"] is True
    assert described["active_conditions"] == ["pico-down"]


# -- integration with the engine --------------------------------------------


async def test_the_engine_alerts_when_the_pico_link_drops(stack, monkeypatch):
    stack.config.alerts.enabled = True
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/weasley-test")
    monkeypatch.delenv("HEALTHCHECKS_URL", raising=False)
    sent: list = []
    stack.engine.alerts.client_factory = lambda: FakeClient(sent)
    stack.engine.alerts.debounce_s = 0.0

    await stack.link.close()
    await stack.engine.check_alerts()

    titles = [entry[3].get("Title", "") for entry in sent if entry[0] == "POST"]
    assert any("Pico is not responding" in title for title in titles)


async def test_the_engine_does_not_alert_when_all_is_well(stack, monkeypatch):
    stack.config.alerts.enabled = True
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/weasley-test")
    sent: list = []
    stack.engine.alerts.client_factory = lambda: FakeClient(sent)
    stack.engine.alerts.debounce_s = 0.0

    await stack.zero_all()
    await stack.engine.check_alerts()
    assert not [entry for entry in sent if entry[0] == "POST"]


async def test_the_engine_alerts_when_no_hand_is_verified(stack, monkeypatch):
    """Every hand unverified means nothing will move; that is worth knowing."""
    stack.config.alerts.enabled = True
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/weasley-test")
    sent: list = []
    stack.engine.alerts.client_factory = lambda: FakeClient(sent)
    stack.engine.alerts.debounce_s = 0.0

    await stack.engine.check_alerts()
    titles = [entry[3].get("Title", "") for entry in sent if entry[0] == "POST"]
    assert any("re-zeroing" in title for title in titles)

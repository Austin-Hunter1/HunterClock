"""Outbound alerting: healthchecks.io and ntfy.sh (P2).

The clock is in another state. Without this, the way you find out it died is
that somebody mentions the hands have not moved in a fortnight.

Two independent mechanisms, both optional and both configured by environment
variable only — the URLs are credentials in URL form, so they never go in the
YAML:

**healthchecks.io** is a dead-man's switch. The controller pings a URL on a
timer; if the pings stop, healthchecks emails you. That is the one that catches
a Pi which has lost power, lost WiFi or hung — exactly the failures where the
controller cannot send you anything itself, because it is the thing that broke.

**ntfy.sh** is a push notification. The controller posts when something changes
state: the Pico link drops, the firmware reports a fault, the controller starts
or stops. That is the one that tells you *what* went wrong, when the Pi is still
alive enough to say so.

Both are rate limited, because a flapping USB cable must not turn into two
hundred notifications overnight.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

#: Shortest gap between two notifications about the same thing, in seconds.
#: A USB cable that reconnects every ten seconds is one problem, not 360.
DEFAULT_COOLDOWN_S = 900.0

#: How long a condition must hold before it is worth telling anyone about.
#: The Pico link blips during a firmware upload and on a reconnect; neither is
#: worth waking someone for.
DEFAULT_DEBOUNCE_S = 60.0

PRIORITY_LOW = "low"
PRIORITY_DEFAULT = "default"
PRIORITY_HIGH = "high"


class Alerter:
    """Sends health pings and push notifications, if configured.

    Args:
        config: The clock configuration.
        events: The event log, so alerts are visible in the UI too.
        client_factory: Builds the HTTP client; overridden in tests.
    """

    def __init__(self, config, events, *, client_factory=None):
        self.config = config
        self.events = events
        self.client_factory = client_factory
        self.cooldown_s = DEFAULT_COOLDOWN_S
        self.debounce_s = DEFAULT_DEBOUNCE_S

        self.pings_sent = 0
        self.notifications_sent = 0
        self.failures = 0
        self.last_error: str | None = None
        self.last_ping_at: float | None = None

        self._last_sent: dict[str, float] = {}
        self._pending: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    # -- configuration ---------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Is alerting switched on at all?"""
        return bool(self.config.alerts.enabled)

    def healthchecks_url(self) -> str | None:
        """The healthchecks.io ping URL, from the environment."""
        return os.environ.get(self.config.alerts.healthchecks_url_env) or None

    def ntfy_url(self) -> str | None:
        """The ntfy topic URL, from the environment."""
        return os.environ.get(self.config.alerts.ntfy_url_env) or None

    def configured(self) -> bool:
        """Is at least one destination actually set?"""
        return bool(self.healthchecks_url() or self.ntfy_url())

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Begin the periodic health ping, if configured."""
        if not self.enabled:
            return
        if not self.configured():
            self.events.system(
                "alerts are enabled but neither "
                f"{self.config.alerts.healthchecks_url_env} nor "
                f"{self.config.alerts.ntfy_url_env} is set"
            )
            return
        self._task = asyncio.create_task(self._ping_loop(), name="alerts")
        self.events.system("alerting started")

    async def stop(self) -> None:
        """Stop the ping loop."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _ping_loop(self) -> None:
        interval = self.config.alerts.interval_s
        while True:
            with contextlib.suppress(Exception):
                await self.ping()
            await asyncio.sleep(interval)

    # -- sending ---------------------------------------------------------

    def _client(self):
        if self.client_factory is not None:
            return self.client_factory()
        import httpx

        return httpx.AsyncClient(timeout=10.0)

    async def ping(self, suffix: str = "") -> bool:
        """Ping healthchecks.io.

        Args:
            suffix: ``""`` for a success ping, ``"/fail"`` to signal a problem,
                ``"/start"`` to mark the beginning of a run.

        Returns:
            True when the ping was delivered.
        """
        url = self.healthchecks_url()
        if not url or not self.enabled:
            return False
        target = url.rstrip("/") + suffix
        try:
            async with self._client() as client:
                response = await client.get(target)
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:
            self.failures += 1
            self.last_error = str(exc)
            log.warning("healthchecks ping failed: %s", exc)
            return False
        self.pings_sent += 1
        self.last_ping_at = time.time()
        self.last_error = None
        return True

    async def notify(
        self,
        title: str,
        message: str,
        *,
        priority: str = PRIORITY_DEFAULT,
        tags: str = "",
    ) -> bool:
        """Push a notification through ntfy.

        Returns:
            True when it was delivered.
        """
        url = self.ntfy_url()
        if not url or not self.enabled:
            return False
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        try:
            async with self._client() as client:
                response = await client.post(url, content=message.encode("utf-8"), headers=headers)
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:
            self.failures += 1
            self.last_error = str(exc)
            log.warning("ntfy notification failed: %s", exc)
            return False
        self.notifications_sent += 1
        self.last_error = None
        return True

    # -- the interesting part: deciding what is worth sending ------------

    def _cooled_down(self, key: str, now: float) -> bool:
        """Has enough time passed since the last alert about ``key``?"""
        last = self._last_sent.get(key)
        return last is None or (now - last) >= self.cooldown_s

    async def condition(
        self,
        key: str,
        active: bool,
        title: str,
        message: str,
        *,
        priority: str = PRIORITY_HIGH,
        tags: str = "warning",
        now: float | None = None,
    ) -> bool:
        """Report a condition that has become true, once it has held.

        Call this every time the condition is evaluated, true or false. It
        notifies only when the condition has been continuously true for
        ``debounce_s`` and nothing was sent about the same ``key`` within
        ``cooldown_s``.

        Args:
            key: Stable identifier for this condition, such as ``"pico-down"``.
            active: Whether the condition is true right now.
            title: Notification title.
            message: Notification body.
            priority: ntfy priority.
            tags: ntfy tags (emoji shortcodes).
            now: Current unix time, for tests.

        Returns:
            True when a notification was actually sent.
        """
        now = now if now is not None else time.time()

        if not active:
            # Recovered (or never started): forget it, and say so if we had
            # complained. A "back to normal" message is worth as much as the
            # warning was.
            if key in self._pending:
                del self._pending[key]
            if key in self._last_sent:
                del self._last_sent[key]
                await self.notify(
                    "Weasley Clock: recovered",
                    f"{title} is back to normal.",
                    priority=PRIORITY_LOW,
                    tags="white_check_mark",
                )
                self.events.system(f"alert cleared: {key}")
                return True
            return False

        since = self._pending.setdefault(key, now)
        if (now - since) < self.debounce_s:
            return False
        if not self._cooled_down(key, now):
            return False

        self._last_sent[key] = now
        sent = await self.notify(title, message, priority=priority, tags=tags)
        if sent:
            self.events.system(f"alert sent: {key}", key=key)
        # A problem worth telling a person about is worth failing the
        # dead-man's switch too, so an unread notification still surfaces.
        await self.ping("/fail")
        return sent

    def describe(self) -> dict[str, Any]:
        """Counters for the System page."""
        return {
            "enabled": self.enabled,
            "healthchecks": bool(self.healthchecks_url()),
            "ntfy": bool(self.ntfy_url()),
            "pings_sent": self.pings_sent,
            "notifications_sent": self.notifications_sent,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_ping_at": self.last_ping_at,
            "active_conditions": sorted(self._last_sent),
        }

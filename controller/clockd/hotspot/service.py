"""Wires the monitor, nmcli and the portal into one long-running service.

Runs as its own systemd unit (``deploy/clock-hotspot.service``), not inside
``clockd``.  The whole value of the thing is that it works when the
controller does not: a ``config/clock.yaml`` that fails to validate, a UI
password that was never set, a crash loop in the engine.  All of those stop
``clockd`` dead and none of them should stop the way back in.

The sequencing in :meth:`HotspotService.raise_ap` matters and is not
obvious:

1. **Scan first, while the radio is still a client.**  A single-radio Pi
   cannot scan usefully once it is an access point, so the list of networks
   the portal offers has to be captured on the way in.  The portal shows that
   list with its age and always offers a free-text box, because by the time
   somebody reads it the list is minutes old.
2. **Rewrite the AP profile every time**, so ``connection.autoconnect no``
   gets put back if anybody has edited it.
3. **Bring the AP up, then wait for its address.**  ``ipv4.method shared``
   assigns 10.42.0.1 a moment *after* the connection activates, so binding
   the portal immediately fails with ``EADDRNOTAVAIL``.  It is retried.

The portal binds to the access point's own address rather than to
``0.0.0.0``.  That is deliberate: a Pi sitting on ethernet with its WiFi down
would otherwise get a password-less setup page published on the house LAN.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .monitor import Action, HotspotMonitor, Observation, State
from .nm import JoinOutcome, NetworkManager
from .portal import PortalState, build_app
from .settings import HotspotSettings, load_settings

log = logging.getLogger(__name__)

#: How long to keep trying to bind the portal after the AP comes up.
BIND_TIMEOUT_S = 15.0


class HotspotService:
    """The whole fallback hotspot.

    Args:
        settings: Loaded from ``config/hotspot.yaml``, or defaults.
        manager: The nmcli layer.  Injected so tests can drive a fake
            NetworkManager instead.
        serve: Optional ``async (app, host, port) -> handle`` used to run the
            portal.  Defaults to uvicorn.  A handle has ``stop()``.
        clock: Time source.
        sleep: Awaitable sleep.  Both are injected so a test can put a day of
            outages through the loop without waiting for one.
    """

    def __init__(
        self,
        settings: HotspotSettings | None = None,
        manager: NetworkManager | None = None,
        *,
        serve=None,
        clock=time.time,
        sleep=None,
    ):
        self.settings = settings if settings is not None else load_settings()
        self.manager = (
            manager if manager is not None else NetworkManager(self.settings.interface)
        )
        self.clock = clock
        self.sleep = sleep if sleep is not None else asyncio.sleep
        self._serve = serve if serve is not None else _uvicorn_serve
        self.state = PortalState()
        self.monitor = HotspotMonitor(self.settings, blocked=self._blocked())
        self._server = None
        self._stopping = False
        self.polls = 0

    # -- why we might not be able to do anything -------------------------

    def _blocked(self) -> str | None:
        """A sentence if the hotspot cannot run, else ``None``."""
        if not self.settings.enabled:
            return "the setup hotspot is switched off in config/hotspot.yaml"
        if not NetworkManager.present():
            return (
                "nmcli is not installed, so there is no way to manage WiFi here "
                "(expected on a laptop; on Raspberry Pi OS Bookworm it is standard)"
            )
        problem = self.settings.password_problem()
        if problem is not None:
            return problem
        return None

    # -- observing -------------------------------------------------------

    async def observe(self) -> Observation:
        """Ask NetworkManager what is going on.

        ``online`` excludes our own access point.  ``ipv4.method shared``
        gives the interface 10.42.0.1 and NetworkManager reports it as
        ``connected``, so counting it would tell the monitor that raising the
        access point had fixed the problem, and the clock would serve a setup
        page for ever.
        """
        actives = await self.manager.active_connections()
        ap_profile = self.settings.ap_profile
        ap_active = any(
            active.name == ap_profile and active.device == self.settings.interface
            for active in actives
        )
        online = False
        for active in actives:
            if active.name == ap_profile or not active.carries_traffic:
                continue
            if await self.manager.ip4(active.device):
                online = True
                break
        return Observation(
            now=self.clock(),
            online=online,
            ap_active=ap_active,
            portal_last_seen=self.state.last_seen,
        )

    # -- acting ----------------------------------------------------------

    async def raise_ap(self) -> bool:
        """Scan, put the access point up and start the portal."""
        password = self.settings.password()
        if not password:  # pragma: no cover - the monitor is blocked without one
            log.error("hotspot: refusing to raise an access point with no password")
            return False

        log.info("hotspot: scanning before taking the radio for the access point")
        networks = await self.manager.scan()
        self.state.set_scan(networks, self.clock())
        log.info(
            "hotspot: %d network(s) in range: %s",
            len(networks),
            ", ".join(network.ssid for network in networks[:8]) or "none",
        )

        profile = await self.manager.ensure_ap_profile(
            self.settings.ap_profile, self.settings.ssid, password
        )
        if not profile.ok:
            log.error("hotspot: could not write the access point profile: %s", profile.message)
            return False

        up = await self.manager.up(self.settings.ap_profile)
        if not up.ok:
            log.error("hotspot: could not bring the access point up: %s", up.message)
            return False
        log.info("hotspot: access point %r is up", self.settings.ssid)

        await self.start_portal()
        return True

    async def lower_ap(self) -> None:
        """Stop the portal and drop the access point, freeing the radio."""
        await self.stop_portal()
        result = await self.manager.down(self.settings.ap_profile)
        if not result.ok:
            log.warning("hotspot: could not bring the access point down: %s", result.message)
        else:
            log.info("hotspot: access point down; letting WiFi retry the saved networks")

    async def start_portal(self) -> None:
        """Serve the portal on the access point's address.

        Retried, because ``ipv4.method shared`` adds 10.42.0.1 to the
        interface a moment after the connection reports itself active, and
        binding to an address the kernel does not have yet fails outright.
        """
        if self._server is not None:
            return
        app = build_app(
            self.state,
            self.settings,
            self.manager,
            on_join=self._joined,
            now=self.clock,
        )
        deadline = self.clock() + BIND_TIMEOUT_S
        delay = 0.5
        while True:
            try:
                self._server = await self._serve(
                    app, self.settings.portal_host, self.settings.portal_port
                )
                log.info(
                    "hotspot: setup page at http://%s:%d/",
                    self.settings.portal_host,
                    self.settings.portal_port,
                )
                return
            except OSError as exc:
                if self.clock() >= deadline:
                    log.error(
                        "hotspot: could not serve the setup page on %s:%d (%s). The access "
                        "point is up but there is nothing to show on it.",
                        self.settings.portal_host,
                        self.settings.portal_port,
                        exc,
                    )
                    return
                await self.sleep(delay)
                delay = min(2.0, delay * 2)

    async def stop_portal(self) -> None:
        """Shut the portal down, if it is running."""
        server = self._server
        self._server = None
        if server is None:
            return
        with contextlib.suppress(Exception):
            await server.stop()

    async def _joined(self, ssid: str, outcome: JoinOutcome) -> None:
        """Called by the portal once a join attempt has finished."""
        if not outcome.ok:
            # Leave everything alone. Bringing the client profile up will have
            # taken the access point down with it, so the monitor sees "not
            # online, no access point", goes back through the grace period and
            # puts the access point up again -- which is exactly what the page
            # told the person would happen.
            return
        self.monitor.note_joined(self.clock())
        await self.stop_portal()

    # -- the loop --------------------------------------------------------

    async def tick(self) -> Action:
        """One poll: observe, decide, act.  Returns the action taken."""
        self.polls += 1
        observation = await self.observe()
        decision = self.monitor.observe(observation)
        if decision.action is Action.RAISE_AP:
            await self.raise_ap()
        elif decision.action is Action.LOWER_AP:
            await self.lower_ap()
        return decision.action

    async def run(self) -> None:
        """Poll until cancelled."""
        if self.monitor.blocked:
            log.warning("hotspot: not running because %s", self.monitor.blocked)
            return
        log.info(
            "hotspot: watching %s; the setup network %r goes up after %.0fs offline",
            self.settings.interface,
            self.settings.ssid,
            self.settings.offline_grace_s,
        )
        try:
            while not self._stopping:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - nmcli misbehaving
                    log.warning("hotspot: poll failed (%s); carrying on", exc)
                await self.sleep(self.settings.check_interval_s)
        finally:
            await self.stop_portal()

    async def stop(self) -> None:
        """Ask the loop to finish and leave the radio as a client.

        The access point is dropped on the way out.  A service being stopped
        for an upgrade must not leave the Pi stranded as an access point with
        nothing serving the page.
        """
        self._stopping = True
        await self.stop_portal()
        if self.monitor.state is State.HOTSPOT:
            await self.manager.down(self.settings.ap_profile)


class _UvicornHandle:
    """A running uvicorn server that can be asked to stop."""

    def __init__(self, server, task: asyncio.Task):
        self.server = server
        self.task = task

    async def stop(self) -> None:
        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(self.task, timeout=10.0)
        if not self.task.done():  # pragma: no cover - uvicorn refusing to stop
            self.task.cancel()


async def _uvicorn_serve(app, host: str, port: int) -> _UvicornHandle:
    """Start uvicorn on host:port and return once the socket is listening.

    The socket is bound here, with the standard library, rather than by
    ``uvicorn.Config.bind_socket()``.  That method logs the error and calls
    ``sys.exit(1)``, so a bind to an address the kernel does not have yet --
    the normal case for the first second after ``ipv4.method shared``
    activates -- would take the whole service down instead of being retried.

    Raises:
        OSError: if the address cannot be bound, which is what
            :meth:`HotspotService.start_portal` retries on.
    """
    import socket

    import uvicorn

    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    sock.listen(16)
    sock.set_inheritable(True)

    config = uvicorn.Config(app, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.get_running_loop().create_task(
        server.serve(sockets=[sock]), name="hotspot-portal"
    )
    return _UvicornHandle(server, task)

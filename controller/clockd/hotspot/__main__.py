"""``python -m clockd.hotspot`` -- run the fallback setup hotspot.

This is what ``deploy/clock-hotspot.service`` starts.  It is a separate
process from ``clockd`` on purpose; see :mod:`clockd.hotspot`.

Two flags exist for checking the thing without waiting for a WiFi outage:

``--status``
    Print what NetworkManager currently says and what the monitor would make
    of it, then exit.  Changes nothing, so it is safe on the live clock.

``--once``
    Run a single poll and exit.  With ``--force`` it skips the grace period,
    which is how you prove the access point really comes up on this hardware
    without unplugging the router.  It will take the clock off the WiFi, so it
    says so and drops the access point again on exit.

``--demo``
    Serve the real portal against :mod:`clockd.hotspot.sim` on localhost, with
    no Pi, no WiFi and no root.  This is how to look at the page the family
    will actually see -- and it is worth looking at, because it is the only
    part of this project whose user is somebody standing on a stepladder with
    a torch rather than somebody who wrote it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from .monitor import State
from .nm import NetworkManager
from .service import HotspotService
from .settings import PASSWORD_ENV, load_settings, suggest_password

log = logging.getLogger("clockd.hotspot")


def build_parser() -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(
        prog="python -m clockd.hotspot",
        description="Fallback WiFi setup hotspot for the Weasley Clock.",
    )
    parser.add_argument("--config", help="path to hotspot.yaml (default config/hotspot.yaml)")
    parser.add_argument(
        "--status", action="store_true", help="print the current state and exit, changing nothing"
    )
    parser.add_argument("--once", action="store_true", help="run one poll and exit")
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --once, raise the access point now instead of waiting out the "
        "grace period. Takes the clock off the WiFi until you stop it.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="serve the portal against a fake NetworkManager on localhost, to look "
        "at the page without a Pi",
    )
    parser.add_argument(
        "--demo-port", type=int, default=8081, help="port for --demo (default 8081)"
    )
    parser.add_argument(
        "--suggest-password",
        action="store_true",
        help=f"print a passphrase to put in .env as {PASSWORD_ENV}, and exit",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


async def show_status(service: HotspotService) -> int:
    """Print what is going on, without touching anything."""
    settings = service.settings
    print(f"interface:     {settings.interface}")
    print(f"setup SSID:    {settings.ssid}")
    print(f"AP profile:    {settings.ap_profile}")
    print(f"nmcli present: {NetworkManager.present()}")
    problem = settings.password_problem()
    print(f"AP password:   {'set' if problem is None else problem}")
    if service.monitor.blocked:
        print(f"blocked:       {service.monitor.blocked}")
        return 1
    observation = await service.observe()
    print(f"online:        {observation.online}")
    print(f"AP up:         {observation.ap_active}")
    actives = await service.manager.active_connections()
    for active in actives:
        print(f"  active:      {active.name} ({active.type}) on {active.device}")
    saved = await service.manager.wifi_profiles()
    for name, priority in saved:
        print(f"  saved wifi:  {name} (priority {priority})")
    print(f"monitor state: {service.monitor.state.value}")
    return 0


async def demo(args: argparse.Namespace) -> int:
    """Serve the portal against the simulator, for looking at the page."""
    import uvicorn

    from .portal import PortalState, build_app
    from .sim import FakeNetwork, FakeNmcli

    settings = load_settings(args.config)
    air = [
        FakeNetwork("Minnetonka-5G", "correct-horse", signal=78),
        FakeNetwork("Minnetonka", "correct-horse", signal=61),
        FakeNetwork("Smith: Guest", None, signal=44, security=""),
        FakeNetwork("NETGEAR37", "whatever", signal=28),
    ]
    fake = FakeNmcli(air=air)
    manager = NetworkManager(settings.interface, fake)
    state = PortalState()
    state.set_scan(await manager.scan())
    app = build_app(state, settings, manager)
    print(f"Setup page at http://127.0.0.1:{args.demo_port}/  (ctrl-C to stop)")
    print("Nothing here touches real WiFi: the networks above are made up.")
    config = uvicorn.Config(app, host="127.0.0.1", port=args.demo_port, log_level="warning")
    await uvicorn.Server(config).serve()
    return 0


async def run(args: argparse.Namespace) -> int:
    """Do whatever the flags asked for."""
    if args.demo:
        return await demo(args)

    settings = load_settings(args.config)
    service = HotspotService(settings)

    if args.status:
        return await show_status(service)

    if args.once:
        if service.monitor.blocked:
            print(f"blocked: {service.monitor.blocked}", file=sys.stderr)
            return 1
        if args.force:
            print(
                f"Raising {settings.ssid!r} now. This takes the clock off the WiFi -- "
                "the radio cannot do both. Press ctrl-C to put it back.",
                file=sys.stderr,
            )
            raised = await service.raise_ap()
            if not raised:
                return 1
            try:
                # Hold it up so the SSID can actually be looked for on a phone,
                # rather than appearing and vanishing inside one poll.
                while True:
                    await asyncio.sleep(1.0)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                await service.lower_ap()
            return 0
        await service.tick()
        print(f"{service.monitor.state.value}: {service.monitor.last_reason}")
        return 0

    if service.monitor.blocked:
        # Not an error exit: a Pi with the hotspot deliberately switched off
        # should not have a unit in a failed state for ever.
        log.warning("not running because %s", service.monitor.blocked)
        return 0

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:  # pragma: no cover - Windows
            continue
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stopping.set)

    task = loop.create_task(service.run(), name="hotspot")
    await stopping.wait()
    await service.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    if service.monitor.state is State.HOTSPOT:  # pragma: no cover - belt and braces
        await service.manager.down(settings.ap_profile)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.suggest_password:
        print(suggest_password())
        return 0
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":
    sys.exit(main())

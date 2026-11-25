"""``clockd`` -- the controller entry point.

Two shapes, one code path:

``clockd --sim``
    A fake Pico inside this process, an in-memory broker, and optional fake
    location traffic.  No serial port, no Mosquitto, no hardware.  Runs the same
    on Windows and Linux, which is the point: the UI can be tried on a laptop
    before the clock is anywhere near a wall.

``clockd``
    The real thing on the Pi: ``/dev/ttyACM0`` and Mosquitto on localhost.

Everything in between -- the engine, the web UI, the filtering, the state
machine -- is identical, so what is exercised in simulation is what runs in the
house.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

from . import __version__
from .bus import Bus, MemoryBus, MqttBus
from .config import ClockConfig, ConfigError, load_config, repo_root
from .engine import Engine
from .eventlog import EventLog
from .picolink import PicoLink, SerialTransport, SimTransport
from .store import Store

log = logging.getLogger("clockd")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="clockd",
        description="Weasley Clock controller: locations in, six hand angles out.",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to clock.yaml")
    parser.add_argument(
        "--sim",
        action="store_true",
        help="fake Pico, in-memory broker and fake locations; no hardware needed",
    )
    parser.add_argument("--host", default=None, help="web UI bind address")
    parser.add_argument("--port", type=int, default=None, help="web UI port")
    parser.add_argument("--serial-port", default=None, help="override serial.port")
    parser.add_argument(
        "--mqtt-host",
        default=None,
        help="use a real broker even in --sim mode (for example localhost)",
    )
    parser.add_argument(
        "--sim-time-scale",
        type=float,
        default=1.0,
        help="speed up simulated time; 10 makes a 60 s sweep take 6 s",
    )
    parser.add_argument(
        "--sim-fixtures",
        action="store_true",
        help="in --sim, zero every hand and publish the reference positions at startup",
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING or ERROR")
    parser.add_argument("--no-web", action="store_true", help="run the engine without the UI")
    parser.add_argument("--version", action="version", version=f"clockd {__version__}")
    return parser


def load_dotenv(root: Path) -> None:
    """Load ``.env`` from the checkout, if python-dotenv is available.

    Secrets live in ``.env`` and never in the repo; systemd loads the same file
    through ``EnvironmentFile``, so this only matters when running by hand.
    """
    try:
        from dotenv import load_dotenv as _load
    except ImportError:
        return
    env_file = root / ".env"
    if env_file.is_file():
        _load(env_file)
        log.debug("loaded %s", env_file)


def make_bus(config: ClockConfig, *, sim: bool, mqtt_host: str | None) -> Bus:
    """Build the right bus for this run.

    In sim mode the in-memory broker is the default, but ``--mqtt-host`` opts
    into a real one, which is how the integration tests exercise Mosquitto when
    it happens to be installed.
    """
    if mqtt_host:
        return MqttBus(
            mqtt_host,
            config.mqtt.port,
            username=config.mqtt.username,
            password=config.mqtt.password(),
            client_id=config.mqtt.client_id,
            keepalive_s=config.mqtt.keepalive_s,
            tls=config.mqtt.tls,
        )
    if sim or not config.mqtt.enabled:
        return MemoryBus()
    password = config.mqtt.password()
    if config.mqtt.username and not password:
        log.warning(
            "MQTT username %r is set but %s is empty; the broker will probably "
            "refuse the connection",
            config.mqtt.username,
            config.mqtt.password_env,
        )
    return MqttBus(
        config.mqtt.host,
        config.mqtt.port,
        username=config.mqtt.username,
        password=password,
        client_id=config.mqtt.client_id,
        keepalive_s=config.mqtt.keepalive_s,
        tls=config.mqtt.tls,
    )


def make_link(
    config: ClockConfig, *, sim: bool, time_scale: float
) -> tuple[PicoLink, object | None]:
    """Build the Pico link and, in sim mode, the simulated Pico behind it.

    Returns:
        ``(link, sim_pico_or_None)``.
    """
    if sim:
        sys.path.insert(0, str(repo_root()))
        from tools.pico_sim import MonotonicClock, SimPico

        pico = SimPico(
            channels=config.channels,
            clock=MonotonicClock(time_scale),
            store=_sim_store(config),
        )
        # The firmware watchdog counts *simulated* seconds, so a scaled clock
        # has to be pinged proportionally more often in real time or the
        # simulated Pico decides the Pi has gone away.
        ping_interval = config.serial.ping_interval_s / max(time_scale, 1.0)
        link = PicoLink(
            lambda: SimTransport(pico),
            command_timeout_s=config.serial.command_timeout_s,
            max_retries=config.serial.max_retries,
            ping_interval_s=max(ping_interval, 0.05),
            reconnect_interval_s=config.serial.reconnect_interval_s,
            channels=config.channels,
        )
        return link, pico

    link = PicoLink(
        lambda: SerialTransport(config.serial.port, config.serial.baudrate),
        command_timeout_s=config.serial.command_timeout_s,
        max_retries=config.serial.max_retries,
        ping_interval_s=config.serial.ping_interval_s,
        reconnect_interval_s=config.serial.reconnect_interval_s,
        channels=config.channels,
    )
    return link, None


def _sim_store(config: ClockConfig):
    """A position store for the simulated Pico, kept under ``var/``."""
    from tools.pico_sim import JsonFileStore

    return JsonFileStore(config.var_path("sim-pico-positions.json"))


async def run(args: argparse.Namespace) -> int:
    """Start everything and run until interrupted."""
    root = repo_root()
    load_dotenv(root)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2

    if args.serial_port:
        config.serial.port = args.serial_port
    if args.host:
        config.web.host = args.host
    elif args.sim and config.web.host == "0.0.0.0":  # noqa: S104
        # A laptop demo should not quietly listen on every interface.
        config.web.host = "127.0.0.1"
    if args.port:
        config.web.port = args.port

    events = EventLog(path=config.log_path("events.log", root=root))
    store = Store(config.var_path("state.json", root=root))
    bus = make_bus(config, sim=args.sim, mqtt_host=args.mqtt_host)
    link, sim_pico = make_link(config, sim=args.sim, time_scale=args.sim_time_scale)
    engine = Engine(config, bus, link, store, events, sim=args.sim)
    link.on_connection_change = engine.on_link_change

    app = None
    server = None
    if not args.no_web:
        import uvicorn

        from .web import create_app
        from .web.auth import AuthError

        try:
            app = create_app(
                engine,
                sim=args.sim,
                config_path=args.config or None,
                repo_root=root,
            )
        except AuthError as exc:
            print(f"Refusing to start:\n{exc}", file=sys.stderr)
            return 2
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.web.host,
                port=config.web.port,
                log_level=args.log_level.lower(),
                access_log=False,
                lifespan="off",
            )
        )

    await engine.start()

    if args.sim and args.sim_fixtures:
        await _seed_simulation(engine)

    banner = [
        f"clockd {__version__} starting",
        "  mode     : %s" % ("SIMULATION (no hardware)" if args.sim else "live hardware"),
        f"  config   : {len(config.people)} people, home {config.home.name}",
        "  pico     : %s" % ("in-process simulator" if args.sim else config.serial.port),
        "  mqtt     : {}".format(
            "in-memory"
            if isinstance(bus, MemoryBus)
            else f"{config.mqtt.host}:{config.mqtt.port}"
        ),
    ]
    if not args.no_web:
        banner.append(f"  web UI   : http://{config.web.host}:{config.web.port}")
    print("\n".join(banner), flush=True)

    stop = asyncio.Event()

    def request_stop(*_args: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            # Windows: fall back to the default KeyboardInterrupt handling.
            signal.signal(sig, lambda *_: request_stop())

    serve_task = asyncio.create_task(server.serve()) if server else None
    try:
        if serve_task is not None:
            done, _pending = await asyncio.wait(
                {serve_task, asyncio.create_task(stop.wait())},
                return_when=asyncio.FIRST_COMPLETED,
            )
            del done
        else:
            await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.should_exit = True
        if serve_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(serve_task, timeout=5.0)
        await engine.stop()
        if sim_pico is not None:
            del sim_pico
    print("clockd stopped.", flush=True)
    return 0


async def _seed_simulation(engine: Engine) -> None:
    """Make a fresh ``--sim`` run look like a working clock.

    Zeroes and verifies every hand (as the Assist wizard would) and publishes
    each person's configured fallback position through the normal generic-MQTT
    path, so the dial has six hands on it within a couple of seconds instead of
    six 'unverified, no location' rows.
    """
    import json
    import time

    await asyncio.sleep(0.6)
    for slug in list(engine.people):
        with contextlib.suppress(Exception):
            await engine.set_zero(slug)
    for person in engine.config.people:
        if person.fallback_lat is None or person.fallback_lon is None:
            continue
        payload = json.dumps(
            {
                "lat": person.fallback_lat,
                "lon": person.fallback_lon,
                "acc": 25,
                "ts": time.time(),
            }
        )
        topic = "{}/{}".format(
            engine.config.sources.generic_mqtt.topic_prefix.strip("/"),
            person.slug,
        )
        await engine.bus.publish(topic, payload)
        await asyncio.sleep(0.05)
    engine.events.system("simulation seeded: hands zeroed and reference positions published")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the controller."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quieten the libraries that chatter at INFO.
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Everything that shells out to ``nmcli``, behind one injectable runner.

Raspberry Pi OS Bookworm manages WiFi with NetworkManager, so ``nmcli`` is
the whole hardware layer for this package: it raises the access point, scans,
saves profiles and brings them up.  None of that can run in a test, and none
of it can run on Windows, so every call goes through a :data:`Runner` -- an
async callable taking argv and returning a :class:`CommandResult`.  The real
one is :func:`subprocess_runner`; the tests pass a fake NetworkManager that
parses the same argv and would reject a malformed command the same way.

Two things here exist because of mistakes that are easy to make and expensive
to find on a Pi 2000 km away:

*Terse output is escaped, and SSIDs contain colons.*  ``nmcli -t`` separates
fields with ``:`` and escapes any literal colon in a value as ``\\:``.  A
plain ``line.split(":")`` therefore silently mangles a network called
``Jim: Guest`` into two fields and shifts everything after it.  See
:func:`split_terse`.

*A profile named after an SSID is not a safe positional argument.*  The
profile for a joined network is named after the network, and an SSID is
whatever the neighbours typed into their router -- including ``--help`` or
``-p``.  Every command here selects a profile with ``nmcli ... id <name>``
rather than leaving the name positional, so nmcli cannot mistake it for an
option.  Nothing is ever passed through a shell: see
:func:`subprocess_runner`.

*The AP profile must never autoconnect.*  If it does, NetworkManager may
bring the access point up at boot in preference to the house WiFi, and then
the clock is permanently an access point and permanently offline -- the exact
failure the hotspot exists to prevent, caused by the hotspot.
:meth:`NetworkManager.ensure_ap_profile` sets ``connection.autoconnect no``
every time it runs, not just at creation, so a profile edited by hand on the
Pi gets put back.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Sequence
from typing import NamedTuple

log = logging.getLogger(__name__)

#: ``nmcli`` field separator in terse mode.
SEP = ":"


class CommandResult(NamedTuple):
    """The outcome of one command."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """Whether the command succeeded."""
        return self.returncode == 0

    @property
    def message(self) -> str:
        """Whichever stream has something to say, for a log line."""
        return (self.stderr.strip() or self.stdout.strip() or f"exit {self.returncode}").strip()


#: An async callable that runs argv and returns the result.  Must not raise for
#: a non-zero exit; only for being unable to run the command at all.
Runner = Callable[[Sequence[str]], Awaitable[CommandResult]]


class Network(NamedTuple):
    """One WiFi network seen in a scan."""

    ssid: str
    signal: int = 0
    security: str = ""

    @property
    def open(self) -> bool:
        """Whether joining it needs no passphrase."""
        return not self.security.strip() or self.security.strip() == "--"


class DeviceState(NamedTuple):
    """What ``nmcli device status`` says about one interface."""

    device: str
    state: str = "unknown"
    connection: str = ""

    @property
    def connected(self) -> bool:
        """Whether the interface has an active connection."""
        return self.state.lower() == "connected"


class ActiveConnection(NamedTuple):
    """One connection NetworkManager has up, and the device carrying it."""

    name: str
    type: str = ""
    device: str = ""

    #: Connection types that can actually carry traffic off the clock.  A
    #: Tailscale ``tun`` shows up here too and is deliberately not in the
    #: list: a tailnet with no underlying network is not connectivity.
    CARRIERS = ("802-11-wireless", "802-3-ethernet", "wifi", "ethernet")

    @property
    def carries_traffic(self) -> bool:
        """Whether this is a real network rather than a tunnel or a bridge."""
        return self.type.strip().lower() in self.CARRIERS


def split_terse(line: str) -> list[str]:
    """Split one ``nmcli -t`` line into fields, honouring its escaping.

    ``nmcli`` escapes a literal ``:`` in a value as ``\\:`` and a literal
    backslash as ``\\\\``.  Splitting on bare colons breaks on any network
    whose name contains one, which is common enough on guest networks
    (``Smith: Guest``) to be worth getting right.

    Args:
        line: One line of terse output, without its trailing newline.

    Returns:
        The fields, unescaped.
    """
    fields: list[str] = []
    current: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if char == "\\" and index + 1 < length:
            current.append(line[index + 1])
            index += 2
            continue
        if char == SEP:
            fields.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    fields.append("".join(current))
    return fields


async def subprocess_runner(argv: Sequence[str]) -> CommandResult:
    """Run argv for real.  The only place in this package that touches the OS."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        return CommandResult(127, "", f"cannot run {argv[0]!r}: {exc}")
    out, err = await process.communicate()
    return CommandResult(
        process.returncode if process.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


class JoinOutcome(NamedTuple):
    """What came of trying to join a network."""

    ok: bool
    detail: str = ""
    profile: str = ""


class NetworkManager:
    """A small, purposeful slice of ``nmcli``.

    Args:
        interface: The WiFi interface, normally ``wlan0``.
        runner: How to run commands.  Defaults to :func:`subprocess_runner`.
        nmcli: Path or name of the binary, for a system that puts it somewhere
            unusual.
    """

    def __init__(
        self,
        interface: str = "wlan0",
        runner: Runner | None = None,
        *,
        nmcli: str = "nmcli",
    ):
        self.interface = interface
        self.run = runner if runner is not None else subprocess_runner
        self.nmcli = nmcli

    # -- plumbing --------------------------------------------------------

    async def _nmcli(self, *args: str) -> CommandResult:
        result = await self.run([self.nmcli, *args])
        if not result.ok:
            log.debug("nmcli %s -> %s", " ".join(args), result.message)
        return result

    @staticmethod
    def present(nmcli: str = "nmcli") -> bool:
        """Whether ``nmcli`` exists on this machine at all.

        Checked before anything else so that a laptop, a CI container or
        Windows gets one clear line in the log rather than a command-not-found
        for every poll.
        """
        return shutil.which(nmcli) is not None

    # -- reading ---------------------------------------------------------

    async def device_state(self) -> DeviceState:
        """The state of our interface."""
        result = await self._nmcli("-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status")
        if not result.ok:
            return DeviceState(self.interface, "unavailable", "")
        for line in result.stdout.splitlines():
            fields = split_terse(line.rstrip("\n"))
            if fields and fields[0] == self.interface:
                state = fields[1] if len(fields) > 1 else "unknown"
                connection = fields[2] if len(fields) > 2 else ""
                return DeviceState(self.interface, state, connection)
        return DeviceState(self.interface, "missing", "")

    async def ip4(self, device: str | None = None) -> str | None:
        """A device's first IPv4 address, or ``None``.  Defaults to our interface.

        An interface can sit in ``connected`` with no lease when a router is
        handing out nothing, which looks online and is not, so the service
        wants both the association and an address.
        """
        result = await self._nmcli(
            "-t", "-f", "IP4.ADDRESS", "device", "show", device or self.interface
        )
        if not result.ok:
            return None
        for line in result.stdout.splitlines():
            fields = split_terse(line.rstrip("\n"))
            if len(fields) >= 2 and fields[0].startswith("IP4.ADDRESS") and fields[1].strip():
                return fields[1].split("/")[0].strip() or None
        return None

    async def profiles(self) -> list[str]:
        """Names of every saved connection profile."""
        result = await self._nmcli("-t", "-f", "NAME", "connection", "show")
        if not result.ok:
            return []
        names = []
        for line in result.stdout.splitlines():
            fields = split_terse(line.rstrip("\n"))
            if fields and fields[0].strip():
                names.append(fields[0])
        return names

    async def wifi_profiles(self) -> list[tuple[str, int]]:
        """Saved WiFi profiles as ``(name, autoconnect_priority)``.

        Shown on the portal page so that whoever is standing at the clock can
        see the clock has in fact been told about their network, and under
        what name.
        """
        result = await self._nmcli(
            "-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY", "connection", "show"
        )
        if not result.ok:
            return []
        found: list[tuple[str, int]] = []
        for line in result.stdout.splitlines():
            fields = split_terse(line.rstrip("\n"))
            if len(fields) < 3 or "wireless" not in fields[1]:
                continue
            try:
                priority = int(fields[2])
            except ValueError:
                priority = 0
            found.append((fields[0], priority))
        found.sort(key=lambda item: (-item[1], item[0].lower()))
        return found

    async def scan(self, *, rescan: bool = True) -> list[Network]:
        """List the WiFi networks in range, strongest first.

        Called *before* the access point goes up, never while it is up: the
        Pi has one radio, and asking it to scan while it is acting as an AP
        either returns the stale cache or knocks the AP over.  The monitor
        therefore scans on its way into hotspot mode and the portal serves
        that list with its age, plus a box to type a name that is not on it.
        """
        args = ["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
        if rescan:
            args.insert(0, "--wait")
            args.insert(1, "10")
            args.append("--rescan")
            args.append("yes")
        result = await self._nmcli(*args)
        if not result.ok:
            log.warning("WiFi scan failed: %s", result.message)
            return []
        best: dict[str, Network] = {}
        for line in result.stdout.splitlines():
            fields = split_terse(line.rstrip("\n"))
            if not fields or not fields[0].strip():
                continue  # a hidden network reports an empty SSID; nothing to offer
            ssid = fields[0]
            try:
                signal = int(fields[1]) if len(fields) > 1 and fields[1].strip() else 0
            except ValueError:
                signal = 0
            security = fields[2] if len(fields) > 2 else ""
            existing = best.get(ssid)
            # One SSID can appear several times: 2.4 and 5 GHz, or a mesh with
            # several nodes. Keep the strongest so the list reads sensibly.
            if existing is None or signal > existing.signal:
                best[ssid] = Network(ssid, signal, security)
        return sorted(best.values(), key=lambda network: (-network.signal, network.ssid.lower()))

    # -- the access point ------------------------------------------------

    async def ensure_ap_profile(self, profile: str, ssid: str, password: str) -> CommandResult:
        """Create or correct the access point profile.

        Always rewrites the safety-critical fields rather than trusting what
        is on disk, because the one setting that must not drift is
        ``connection.autoconnect no``: an AP profile that autoconnects can
        win the interface at boot and leave the clock offline for good.

        ``ipv4.method shared`` is what makes this usable by someone with only
        a phone -- NetworkManager starts its own dnsmasq on 10.42.0.1, hands
        out leases, and the portal is reachable without anyone configuring an
        address.
        """
        settings = [
            "802-11-wireless.mode",
            "ap",
            "802-11-wireless.ssid",
            ssid,
            "802-11-wireless.band",
            "bg",
            "802-11-wireless-security.key-mgmt",
            "wpa-psk",
            "802-11-wireless-security.proto",
            "rsn",
            "802-11-wireless-security.psk",
            password,
            "ipv4.method",
            "shared",
            "ipv6.method",
            "ignore",
            "connection.autoconnect",
            "no",
            "connection.autoconnect-priority",
            "0",
        ]
        if profile in await self.profiles():
            return await self._nmcli("connection", "modify", "id", profile, *settings)
        return await self._nmcli(
            "connection",
            "add",
            "type",
            "wifi",
            "con-name",
            profile,
            "ifname",
            self.interface,
            "ssid",
            ssid,
            "--",
            *settings,
        )

    async def up(self, profile: str) -> CommandResult:
        """Activate a profile on our interface."""
        return await self._nmcli(
            "connection", "up", "id", profile, "ifname", self.interface
        )

    async def down(self, profile: str) -> CommandResult:
        """Deactivate a profile.

        A profile that is already down exits non-zero, which is not a problem
        worth propagating: the caller wanted it down and it is down.
        """
        result = await self._nmcli("connection", "down", "id", profile)
        if not result.ok and "not an active" in result.message.lower():
            return CommandResult(0, result.stdout, result.stderr)
        return result

    async def delete(self, profile: str) -> CommandResult:
        """Remove a saved profile."""
        return await self._nmcli("connection", "delete", "id", profile)

    async def active_connections(self) -> list[ActiveConnection]:
        """Every connection NetworkManager currently has up, on any device.

        Not just our WiFi interface: a Pi plugged into ethernet is reachable
        over Tailscale and needs no setup hotspot, and raising one would take
        its WiFi away for no reason.  :mod:`clockd.hotspot.service` decides
        what counts as online from this list.
        """
        result = await self._nmcli(
            "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"
        )
        if not result.ok:
            return []
        found: list[ActiveConnection] = []
        for line in result.stdout.splitlines():
            fields = split_terse(line.rstrip("\n"))
            if len(fields) < 3 or not fields[0].strip():
                continue
            found.append(ActiveConnection(fields[0], fields[1], fields[2]))
        return found

    # -- joining a real network -----------------------------------------

    async def join(
        self,
        ssid: str,
        password: str,
        *,
        priority: int = 100,
        profile: str | None = None,
    ) -> JoinOutcome:
        """Save a network and try to bring it up.

        The profile is named after the SSID, which is what
        ``deploy/add-wifi.sh`` does too, so the two ways of adding a network
        edit the same profile instead of quietly creating a second one that
        shadows the first.

        An existing profile of that name is *modified* rather than deleted and
        recreated.  Deleting it would throw away a working static address or a
        hidden-network flag that somebody set deliberately, and recreating it
        on a failed join would leave the clock worse off than before anyone
        touched it.

        On failure a profile this call *created* is deleted again.  Leaving it
        would be worse than having never tried: NetworkManager retries saved
        profiles indefinitely, and one with a wrong password and a high
        priority will be preferred over a working network for ever.

        Args:
            ssid: The network to join.
            password: Its passphrase; empty for an open network.
            priority: ``connection.autoconnect-priority`` for the profile.
            profile: Override the profile name.  Defaults to the SSID.

        Returns:
            A :class:`JoinOutcome`.
        """
        name = profile or ssid
        existing = await self.profiles()
        created = name not in existing

        if password:
            security = [
                "802-11-wireless-security.key-mgmt",
                "wpa-psk",
                "802-11-wireless-security.psk",
                password,
            ]
        else:
            # An open network. Clearing key-mgmt matters when we are modifying
            # a profile that used to be WPA: leaving the old key-mgmt in place
            # makes the join fail with a confusing security mismatch.
            security = ["802-11-wireless-security.key-mgmt", ""]

        common = [
            "802-11-wireless.ssid",
            ssid,
            "802-11-wireless.mode",
            "infrastructure",
            "ipv4.method",
            "auto",
            "connection.autoconnect",
            "yes",
            "connection.autoconnect-priority",
            str(priority),
            *security,
        ]

        if created:
            result = await self._nmcli(
                "connection",
                "add",
                "type",
                "wifi",
                "con-name",
                name,
                "ifname",
                self.interface,
                "ssid",
                ssid,
                "--",
                *common,
            )
        else:
            result = await self._nmcli("connection", "modify", "id", name, *common)

        if not result.ok:
            if created:
                await self.delete(name)
            return JoinOutcome(False, f"could not save the network: {result.message}", name)

        activate = await self.up(name)
        if not activate.ok:
            if created:
                await self.delete(name)
                return JoinOutcome(False, _explain(activate.message), name)
            log.warning(
                "join to %r failed and the profile already existed, so it keeps the "
                "password that was just entered: %s",
                name,
                activate.message,
            )
            return JoinOutcome(False, _explain(activate.message), name)

        return JoinOutcome(True, f"joined {ssid}", name)


def _explain(message: str) -> str:
    """Turn an nmcli activation failure into something a person can act on."""
    text = message.strip()
    lowered = text.lower()
    if "secrets were required" in lowered or "no secrets" in lowered or "802.1x" in lowered:
        return "the password was not accepted"
    if "timeout" in lowered or "timed out" in lowered:
        return "the clock could not reach that network before giving up"
    if "no suitable network" in lowered or "not found" in lowered or "no network" in lowered:
        return "that network was not in range"
    return text or "the join failed"

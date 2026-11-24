"""A fake NetworkManager, in the spirit of ``tools/pico_sim.py``.

Not a mock.  It parses the same ``nmcli`` argv the real binary would get,
including the global options and the terse field lists, and it refuses
anything it does not recognise with a non-zero exit the way ``nmcli`` does.
So if the code in :mod:`clockd.hotspot.nm` builds a command wrong -- a field
name that is not a field, a profile name left positional where it needs an
``id`` selector, a setting spelled the way an older nmcli spelled it -- the
tests fail here rather than on a Pi in Minnesota.

It also models the parts of the world that the awkward cases depend on:

* a radio that can be a client **or** an access point, never both, so
  bringing one connection up takes the other down;
* a wrong passphrase failing the way NetworkManager fails it, with
  ``Secrets were required, but not provided``, which is the string
  :func:`clockd.hotspot.nm._explain` turns into "the password was not
  accepted";
* a router that associates and then hands out no lease, which looks
  ``connected`` and is not online;
* an SSID with a colon in it, which is what the terse-output escaping in
  :func:`clockd.hotspot.nm.split_terse` exists for.

``python -m clockd.hotspot --demo`` runs the real portal against this, so the
page the family will see can be opened on a laptop with no Pi and no WiFi
involved at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .nm import CommandResult

#: What the real thing prints when a profile name does not exist.
UNKNOWN_CONNECTION = "Error: unknown connection '{}'."

#: What it prints when a passphrase is refused.  :func:`clockd.hotspot.nm._explain`
#: matches on this.
BAD_SECRETS = (
    "Error: Connection activation failed: Secrets were required, but not provided"
)


@dataclass
class FakeNetwork:
    """One access point in the air around the clock."""

    ssid: str
    password: str | None = "correct-horse"
    signal: int = 70
    security: str = "WPA2"

    @property
    def open(self) -> bool:
        return self.password is None


def _escape(value: str) -> str:
    """Escape a value the way ``nmcli -t`` does."""
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _terse(fields: Sequence[str]) -> str:
    return ":".join(_escape(str(field_)) for field_ in fields)


@dataclass
class FakeNmcli:
    """A NetworkManager that lives in a dict.

    Args:
        interface: The WiFi device it pretends to have.
        air: The networks in range.
        wired: Whether an ethernet connection is up, for the case where the Pi
            is plugged in and needs no hotspot at all.
        dhcp: Whether associating yields an IPv4 address.  ``False`` models a
            router that is up but handing out nothing.
        fail: Argv prefixes to refuse, as a tuple of words, for testing what
            happens when nmcli itself falls over.
    """

    interface: str = "wlan0"
    air: list[FakeNetwork] = field(default_factory=list)
    wired: bool = False
    dhcp: bool = True
    fail: tuple[tuple[str, ...], ...] = ()

    #: name -> settings, as nmcli stores them.
    profiles: dict[str, dict[str, str]] = field(default_factory=dict)
    #: device -> active profile name.
    active: dict[str, str] = field(default_factory=dict)
    #: device -> IPv4 address.
    addresses: dict[str, str] = field(default_factory=dict)
    #: Every argv this fake was asked to run, for assertions.
    calls: list[list[str]] = field(default_factory=list)
    #: How many scans have been run, to prove we do not scan while an AP is up.
    scans: int = 0
    #: Whether a scan was ever attempted while the radio was an access point.
    scanned_while_ap: bool = False

    def __post_init__(self) -> None:
        if self.wired:
            self.profiles.setdefault(
                "Wired connection 1", {"type": "802-3-ethernet", "connection.autoconnect": "yes"}
            )
            self.active["eth0"] = "Wired connection 1"
            self.addresses["eth0"] = "192.168.1.40"

    # -- the runner ------------------------------------------------------

    async def __call__(self, argv: Sequence[str]) -> CommandResult:
        """Run one command.  This is a :data:`clockd.hotspot.nm.Runner`."""
        words = list(argv)
        self.calls.append(words)
        if not words or words[0] != "nmcli":
            return CommandResult(127, "", f"{words[0] if words else ''}: command not found")
        try:
            return self._dispatch(words[1:])
        except _NmcliError as exc:
            return CommandResult(exc.code, "", str(exc))

    # -- argv parsing ----------------------------------------------------

    def _dispatch(self, args: list[str]) -> CommandResult:
        terse = False
        fields: list[str] = []
        index = 0
        # Global options come first, exactly as nmcli accepts them.
        while index < len(args):
            word = args[index]
            if word in ("-t", "--terse"):
                terse = True
                index += 1
            elif word in ("-f", "--fields"):
                if index + 1 >= len(args):
                    raise _NmcliError("Error: fields argument is missing.", 2)
                fields = args[index + 1].split(",")
                index += 2
            elif word == "--wait":
                if index + 1 >= len(args):
                    raise _NmcliError("Error: wait argument is missing.", 2)
                index += 2
            else:
                break
        rest = args[index:]
        if not rest:
            raise _NmcliError("Error: missing object.", 2)

        # Fail-injection is matched here, not on the raw argv: nmcli takes its
        # global options (-t, -f, --wait) *before* the object, so a prefix of
        # ("device",) does not appear at argv[1] for a scan.
        for prefix in self.fail:
            if tuple(rest[: len(prefix)]) == prefix:
                raise _NmcliError("Error: NetworkManager is not running.", 1)

        obj, command = rest[0], (rest[1] if len(rest) > 1 else "")
        tail = rest[2:]

        if obj in ("device", "dev"):
            return self._device(command, tail, terse, fields)
        if obj in ("connection", "con", "c"):
            return self._connection(command, tail, terse, fields)
        raise _NmcliError(f"Error: Object '{obj}' is unknown, try 'nmcli help'.", 2)

    # -- nmcli device ----------------------------------------------------

    def _device(
        self, command: str, tail: list[str], terse: bool, fields: list[str]
    ) -> CommandResult:
        if command == "status":
            self._require_fields(fields, {"DEVICE", "STATE", "CONNECTION", "TYPE"})
            lines = []
            for device in self._devices():
                lines.append(_terse(self._device_field(device, name) for name in fields))
            return CommandResult(0, "\n".join(lines) + "\n")

        if command == "show":
            if not tail:
                raise _NmcliError("Error: a device name is required.", 2)
            device = tail[0]
            if device not in self._devices():
                raise _NmcliError(f"Error: Device '{device}' not found.", 10)
            self._require_fields(fields, {"IP4.ADDRESS", "GENERAL.STATE"})
            lines = []
            for name in fields:
                if name == "IP4.ADDRESS":
                    address = self.addresses.get(device)
                    # nmcli numbers repeated properties: IP4.ADDRESS[1].
                    lines.append(_terse(["IP4.ADDRESS[1]", f"{address}/24" if address else ""]))
                else:
                    lines.append(_terse(["GENERAL.STATE", self._state(device)]))
            return CommandResult(0, "\n".join(lines) + "\n")

        if command == "wifi":
            if tail and tail[0] == "list":
                self.scans += 1
                if self._ap_up_on(self.interface):
                    # The real radio cannot do this. Record it so a test can
                    # prove we never ask: scanning from AP mode either returns
                    # the stale cache or knocks the access point over.
                    self.scanned_while_ap = True
                self._require_fields(fields, {"SSID", "SIGNAL", "SECURITY", "IN-USE", "BSSID"})
                lines = []
                for network in sorted(self.air, key=lambda n: -n.signal):
                    row = []
                    for name in fields:
                        if name == "SSID":
                            row.append(network.ssid)
                        elif name == "SIGNAL":
                            row.append(str(network.signal))
                        elif name == "SECURITY":
                            row.append("" if network.open else network.security)
                        elif name == "IN-USE":
                            row.append("")
                        else:
                            row.append("00:11:22:33:44:55")
                    lines.append(_terse(row))
                return CommandResult(0, "\n".join(lines) + "\n")
            raise _NmcliError(f"Error: invalid extra argument '{' '.join(tail)}'.", 2)

        raise _NmcliError(f"Error: 'device' command '{command}' is not valid.", 2)

    def _devices(self) -> list[str]:
        devices = [self.interface]
        if self.wired:
            devices.append("eth0")
        return devices

    def _state(self, device: str) -> str:
        return "connected" if device in self.active else "disconnected"

    def _device_field(self, device: str, name: str) -> str:
        if name == "DEVICE":
            return device
        if name == "STATE":
            return self._state(device)
        if name == "CONNECTION":
            return self.active.get(device, "")
        if name == "TYPE":
            return "ethernet" if device == "eth0" else "wifi"
        return ""

    # -- nmcli connection ------------------------------------------------

    def _connection(
        self, command: str, tail: list[str], terse: bool, fields: list[str]
    ) -> CommandResult:
        if command == "show":
            only_active = "--active" in tail
            self._require_fields(
                fields, {"NAME", "TYPE", "DEVICE", "UUID", "AUTOCONNECT-PRIORITY", "AUTOCONNECT"}
            )
            names = (
                [self.active[device] for device in sorted(self.active)]
                if only_active
                else sorted(self.profiles)
            )
            lines = []
            for name in names:
                lines.append(_terse(self._connection_field(name, field_) for field_ in fields))
            return CommandResult(0, "\n".join(lines) + ("\n" if lines else ""))

        if command == "add":
            return self._add(tail)
        if command == "modify":
            return self._modify(tail)
        if command in ("up", "down", "delete"):
            name = self._select(tail)
            return getattr(self, f"_{command}")(name, tail)

        raise _NmcliError(f"Error: 'connection' command '{command}' is not valid.", 2)

    def _connection_field(self, name: str, field_: str) -> str:
        profile = self.profiles.get(name, {})
        if field_ == "NAME":
            return name
        if field_ == "TYPE":
            return profile.get("type", "802-11-wireless")
        if field_ == "DEVICE":
            for device, active in self.active.items():
                if active == name:
                    return device
            return ""
        if field_ == "UUID":
            return f"uuid-{abs(hash(name)) % 10**8:08d}"
        if field_ == "AUTOCONNECT-PRIORITY":
            return profile.get("connection.autoconnect-priority", "0")
        if field_ == "AUTOCONNECT":
            return profile.get("connection.autoconnect", "yes")
        return ""

    def _select(self, tail: list[str]) -> str:
        """Pull the profile name out, honouring the ``id``/``uuid`` selector.

        ``nmcli connection up id <name>`` is the unambiguous form.  Accepting
        a bare positional name too is what the real thing does, and is what
        makes a profile called ``--help`` a problem worth avoiding.
        """
        words = [word for word in tail if word not in ("--active",)]
        if not words:
            raise _NmcliError("Error: a connection name is required.", 2)
        if words[0] in ("id", "uuid", "path", "apath"):
            if len(words) < 2:
                raise _NmcliError(f"Error: {words[0]} argument is missing.", 2)
            return words[1]
        if words[0].startswith("-"):
            raise _NmcliError(f"Error: Option '{words[0]}' is unknown.", 2)
        return words[0]

    def _add(self, tail: list[str]) -> CommandResult:
        if "--" in tail:
            head = tail[: tail.index("--")]
            settings = tail[tail.index("--") + 1 :]
        else:
            head, settings = tail, []
        values = _pairs(head)
        if values.get("type") not in ("wifi", "802-11-wireless"):
            raise _NmcliError("Error: 'type' is required.", 2)
        name = values.get("con-name") or values.get("ssid")
        if not name:
            raise _NmcliError("Error: 'con-name' or 'ssid' is required.", 2)
        if name in self.profiles:
            raise _NmcliError(f"Error: connection '{name}' already exists.", 2)
        profile: dict[str, str] = {"type": "802-11-wireless"}
        if values.get("ssid"):
            profile["802-11-wireless.ssid"] = values["ssid"]
        if values.get("ifname"):
            profile["connection.interface-name"] = values["ifname"]
        profile.update(_pairs(settings, strict=True))
        self.profiles[name] = profile
        return CommandResult(0, f"Connection '{name}' successfully added.\n")

    def _modify(self, tail: list[str]) -> CommandResult:
        name = self._select(tail)
        words = tail[tail.index(name) + 1 :]
        if name not in self.profiles:
            raise _NmcliError(UNKNOWN_CONNECTION.format(name), 10)
        self.profiles[name].update(_pairs(words, strict=True))
        return CommandResult(0, "")

    def _up(self, name: str, tail: list[str]) -> CommandResult:
        if name not in self.profiles:
            raise _NmcliError(UNKNOWN_CONNECTION.format(name), 10)
        profile = self.profiles[name]
        device = self.interface
        if "ifname" in tail:
            device = tail[tail.index("ifname") + 1]

        if profile.get("802-11-wireless.mode") == "ap":
            self._take_radio(device)
            self.active[device] = name
            if profile.get("ipv4.method") == "shared":
                self.addresses[device] = "10.42.0.1"
            return CommandResult(0, f"Connection successfully activated ({name}).\n")

        ssid = profile.get("802-11-wireless.ssid", name)
        network = next((item for item in self.air if item.ssid == ssid), None)
        if network is None:
            raise _NmcliError(
                f"Error: Connection activation failed: No suitable network with SSID "
                f"'{ssid}' found.",
                4,
            )
        given = profile.get("802-11-wireless-security.psk", "")
        if network.open:
            if profile.get("802-11-wireless-security.key-mgmt"):
                raise _NmcliError(
                    "Error: Connection activation failed: security mismatch.", 4
                )
        elif given != network.password:
            # Activating leaves the radio disconnected, which is what makes the
            # access point need putting back up after a failed join.
            self._take_radio(device)
            raise _NmcliError(BAD_SECRETS, 4)

        self._take_radio(device)
        self.active[device] = name
        if self.dhcp:
            self.addresses[device] = "192.168.1.50"
        return CommandResult(0, f"Connection successfully activated ({name}).\n")

    def _down(self, name: str, tail: list[str]) -> CommandResult:
        for device, active in list(self.active.items()):
            if active == name:
                del self.active[device]
                self.addresses.pop(device, None)
                return CommandResult(0, f"Connection '{name}' successfully deactivated.\n")
        raise _NmcliError(
            f"Error: '{name}' is not an active connection.\nError: not all active "
            "connections found.",
            10,
        )

    def _delete(self, name: str, tail: list[str]) -> CommandResult:
        if name not in self.profiles:
            raise _NmcliError(UNKNOWN_CONNECTION.format(name), 10)
        for device, active in list(self.active.items()):
            if active == name:
                del self.active[device]
                self.addresses.pop(device, None)
        del self.profiles[name]
        return CommandResult(0, f"Connection '{name}' successfully deleted.\n")

    # -- helpers ---------------------------------------------------------

    def _take_radio(self, device: str) -> None:
        """One radio: whatever was on it goes away."""
        self.active.pop(device, None)
        self.addresses.pop(device, None)

    def _ap_up_on(self, device: str) -> bool:
        name = self.active.get(device)
        if name is None:
            return False
        return self.profiles.get(name, {}).get("802-11-wireless.mode") == "ap"

    @staticmethod
    def _require_fields(fields: list[str], known: set[str]) -> None:
        """Reject a field this fake does not model.

        Deliberately strict.  A silent empty column would let a typo in a
        field name reach the Pi, where the symptom is a hotspot that never
        comes up and a log that says nothing.
        """
        if not fields:
            raise _NmcliError("Error: fields argument is missing.", 2)
        unknown = [name for name in fields if name.split("[")[0] not in known]
        if unknown:
            raise _NmcliError(
                "Error: invalid field '{}'; allowed fields: {}".format(
                    ", ".join(unknown), ", ".join(sorted(known))
                ),
                2,
            )


class _NmcliError(Exception):
    """An nmcli failure, with its exit code."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _pairs(words: Sequence[str], *, strict: bool = False) -> dict[str, str]:
    """Turn ``k v k v`` into a dict, the way nmcli reads settings.

    Args:
        words: The alternating keys and values.
        strict: Raise if there is an odd one left over.  Settings are strict
            because ``nmcli connection modify x ipv4.method`` with no value is
            an error, and silently dropping it would hide a real bug: an AP
            profile missing ``connection.autoconnect no``.
    """
    values: dict[str, str] = {}
    index = 0
    while index + 1 < len(words):
        values[words[index]] = words[index + 1]
        index += 2
    if index != len(words) and strict:
        raise _NmcliError(f"Error: value for '{words[index]}' is missing.", 2)
    return values

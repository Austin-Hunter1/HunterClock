"""Settings for the setup hotspot, kept deliberately apart from clockd's.

This is the only configuration in the project that is *not* part of
:class:`clockd.config.ClockConfig`, and the reason is the whole point of the
package: the hotspot has to work on a Pi where ``config/clock.yaml`` is the
thing that is broken.  A schema error in the clock's config must not also
take away the way to fix it, so this reads its own small file and, if that
file is missing or unreadable or nonsense, says so and carries on with
defaults.

The one thing it will not invent is the AP password.  An open access point
called ``weasley-clock-setup`` in a residential street is an invitation, and
anyone who joined it could drive the clock's motors and read the house WiFi
password straight back out of the page.  With no password configured the
monitor refuses to raise the AP at all and logs why.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger(__name__)

#: Environment variable holding the AP's WPA2 passphrase.  ``deploy/install.sh``
#: generates one into ``.env`` if there is none, because a password that has to
#: be invented by hand tends to end up being ``password``.
PASSWORD_ENV = "CLOCK_HOTSPOT_PASSWORD"

#: WPA2-PSK's own limits.  Shorter than 8 and nmcli rejects the profile; longer
#: than 63 and it is not a passphrase any more.
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 63


class HotspotSettings(BaseModel):
    """Everything the hotspot needs to know.

    The timings are the interesting part.  They are chosen so that the clock
    spends as little time as possible being an access point, because an access
    point is a clock that is offline:

    ``offline_grace_s``
        How long the Pi must be off the network before the AP goes up.  Five
        minutes rides out a router reboot, a power cut that brings the Pi back
        before the router, and NetworkManager's own retry backoff.

    ``hotspot_idle_s``
        With the AP up and nobody associated for this long, give up and go
        back to trying the saved networks.  This is the setting that stops a
        one-off outage turning into a clock that is an access point until
        somebody drives to Minnesota.

    ``hotspot_max_s``
        A ceiling on the AP even when a device *is* associated, for the phone
        that joined and was then put in a pocket and carried away.

    ``retry_wifi_s``
        How long to give NetworkManager to find a saved network after the AP
        comes down, before putting it back up.  Needs to cover a scan plus a
        DHCP lease on a slow router; three minutes is generous.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True

    #: What the person standing at the clock looks for in their WiFi list.
    #: Deliberately boring and self-describing; it is the only instruction
    #: they get if the label inside the cabinet has fallen off.
    ssid: str = "weasley-clock-setup"

    #: Name of the NetworkManager profile for the AP.  Never autoconnect: see
    #: :meth:`clockd.hotspot.nm.NetworkManager.ensure_ap_profile`.
    ap_profile: str = "weasley-setup-ap"

    interface: str = "wlan0"

    #: Where the portal listens.  ``ipv4.method shared`` puts NetworkManager's
    #: own dnsmasq on 10.42.0.1 and hands out leases on that /24.
    portal_host: str = "10.42.0.1"
    portal_port: int = Field(80, gt=0, lt=65536)

    check_interval_s: float = Field(20.0, gt=0)
    offline_grace_s: float = Field(300.0, ge=0)
    hotspot_idle_s: float = Field(600.0, gt=0)
    hotspot_max_s: float = Field(1800.0, gt=0)
    retry_wifi_s: float = Field(180.0, gt=0)

    #: Priority given to a network joined through the portal.  Higher than
    #: anything ``deploy/add-wifi.sh`` suggests, because a network someone
    #: typed in while standing at the clock is more current than one saved
    #: months ago in Boulder.
    join_priority: int = Field(100, ge=0, le=999)

    #: How long to wait for a join to come up before calling it failed.
    join_timeout_s: float = Field(45.0, gt=0)

    @field_validator("ssid")
    @classmethod
    def _check_ssid(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("ssid must not be empty")
        if len(text.encode("utf-8")) > 32:
            raise ValueError("an SSID is at most 32 bytes")
        return text

    @field_validator("ap_profile", "interface")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    def password(self) -> str | None:
        """The AP passphrase, from the environment only.

        Returns ``None`` when it is unset or too short to be a legal WPA2
        passphrase, which the monitor treats the same way: do not raise the AP.
        """
        value = os.environ.get(PASSWORD_ENV, "").strip()
        if not value:
            return None
        if not (MIN_PASSWORD_LEN <= len(value) <= MAX_PASSWORD_LEN):
            log.warning(
                "%s is set but is %d characters; WPA2 needs %d to %d, so the "
                "setup hotspot will not be raised",
                PASSWORD_ENV,
                len(value),
                MIN_PASSWORD_LEN,
                MAX_PASSWORD_LEN,
            )
            return None
        return value

    def password_problem(self) -> str | None:
        """A sentence for the logs and the status page, or ``None`` if fine."""
        value = os.environ.get(PASSWORD_ENV, "").strip()
        if not value:
            return (
                f"{PASSWORD_ENV} is not set, so there is no setup hotspot. Put a "
                f"passphrase of {MIN_PASSWORD_LEN}-{MAX_PASSWORD_LEN} characters in "
                ".env and write it on a label inside the cabinet."
            )
        if len(value) < MIN_PASSWORD_LEN:
            return f"{PASSWORD_ENV} is shorter than {MIN_PASSWORD_LEN} characters."
        if len(value) > MAX_PASSWORD_LEN:
            return f"{PASSWORD_ENV} is longer than {MAX_PASSWORD_LEN} characters."
        return None


def suggest_password() -> str:
    """A readable passphrase someone can copy onto a label and type on a phone.

    Hex rather than base64 and lowercase throughout: the person typing this is
    standing in front of a clock on a stepladder, probably holding a torch,
    and ``l`` against ``1`` or ``O`` against ``0`` is a support call.  Twelve
    hex characters is about 48 bits, which is far more than a passphrase
    guarded by having to be within WiFi range of the cabinet needs.
    """
    return secrets.token_hex(6)


def default_settings_path() -> Path:
    """``config/hotspot.yaml``, or ``$CLOCK_HOTSPOT_CONFIG`` if set."""
    override = os.environ.get("CLOCK_HOTSPOT_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / "config" / "hotspot.yaml"


def load_settings(path: Path | str | None = None) -> HotspotSettings:
    """Load the settings, falling back to defaults on any problem.

    Unlike :func:`clockd.config.load_config` this never raises.  A hotspot
    that refuses to start because its own config file has a typo in it is a
    hotspot that is not there on the day it is needed, so a bad file is a
    loud warning and the defaults, which are the values we actually want
    anyway -- the file exists to override the SSID and the timings, not to
    make the thing go.
    """
    target = Path(path) if path is not None else default_settings_path()
    if not target.is_file():
        log.info("no hotspot config at %s; using defaults", target)
        return HotspotSettings()
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.warning("cannot read %s (%s); using hotspot defaults", target, exc)
        return HotspotSettings()
    if data is None:
        return HotspotSettings()
    if not isinstance(data, dict):
        log.warning("%s is not a YAML mapping; using hotspot defaults", target)
        return HotspotSettings()
    try:
        return HotspotSettings.model_validate(data)
    except Exception as exc:
        log.warning("%s did not validate (%s); using hotspot defaults", target, exc)
        return HotspotSettings()

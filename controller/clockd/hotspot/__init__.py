"""Fallback setup hotspot for the Pi.

The clock lives in a family home in another state and is reached over
Tailscale.  That works until the house changes its WiFi password, at which
point the Pi is a brick: no screen, no keyboard, no network, and nobody on
site who can read an IP address off anything.  This package is the way back
in.  Someone standing at the clock joins a WiFi network the clock itself puts
up, a page appears on their phone, they pick the house network, type the new
password, and the clock rejoins and disappears again.

Three decisions shape everything here, and all three are about what happens
when nobody is watching:

**It is a separate service from clockd.**  ``clockd`` refuses to start on a
bad ``config/clock.yaml`` and refuses to start with no UI password, which is
correct for a thing that drives six motors and wrong for the thing that
rescues it.  The hotspot runs as its own systemd unit with its own tiny
config, so a controller in a crash loop still leaves a way in.  Nothing in
here imports :mod:`clockd.config`.

**It gives up on itself.**  A hotspot that stays up forever is worse than no
hotspot, because the Pi's single radio cannot be an access point and a client
at the same time: while the AP is up the clock is *definitely* offline.  So
the monitor cycles.  It raises the AP only after a grace period, drops it
again when nobody has joined for a while, and spends a few minutes letting
NetworkManager retry the saved networks before putting it back up.  A
five-minute router reboot therefore costs nothing, and an AP nobody ever
visits does not strand the clock.

**It never leaves a bad profile behind.**  ``nmcli device wifi connect`` saves
a connection profile even when the password turns out to be wrong, and
NetworkManager will then retry that known-bad profile forever, in preference
to the good one if its priority is higher.  Every failed join in
:mod:`.portal` deletes the profile it created.

Modules:
    :mod:`.settings`   its own config file, defaults, and the AP password
    :mod:`.nm`         everything that shells out to ``nmcli``
    :mod:`.monitor`    the state machine, pure and clock-driven
    :mod:`.portal`     the page the person standing at the clock sees
    :mod:`.service`    wires the three together and runs them
"""

from __future__ import annotations

from .monitor import HotspotMonitor, State
from .nm import CommandResult, Network, NetworkManager
from .settings import HotspotSettings, load_settings

__all__ = [
    "CommandResult",
    "HotspotMonitor",
    "HotspotSettings",
    "NetworkManager",
    "Network",
    "State",
    "load_settings",
]

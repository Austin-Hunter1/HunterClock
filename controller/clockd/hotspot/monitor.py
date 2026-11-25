"""The decision: when to raise the access point, and when to give it up.

This module performs no I/O and knows nothing about ``nmcli``.  It takes an
:class:`Observation` -- the time, whether the Pi is on a real network, whether
our access point is the thing holding the radio, and when the portal was last
touched -- and returns the one :class:`Action` to take.  Being pure is what
makes the awkward cases testable: a router that reboots for four minutes, a
phone that joins the AP and wanders off, an AP that fails to come up at all.

The cycle is the important part.  The Pi has one radio and cannot be an access
point and a WiFi client at once, so **while the setup hotspot is up the clock
is definitely offline**.  A hotspot that latches on and stays up has therefore
turned a temporary outage into a permanent one, and that is the objection
``PROGRESS.md`` recorded against building this at all.  The answer is that the
hotspot is never a destination:

.. code-block:: text

    ONLINE ──not online──> WAITING ──grace elapsed──> HOTSPOT
      ^                       |                        │   ^
      │                       └──online───┐            │   │
      │                                   │     idle, or capped
      └────────online─────────────── RETRYING <────────┘   │
                                          └───retry elapsed┘

Every path out of ``HOTSPOT`` leads back to trying the saved networks.  If
the house WiFi comes back on its own -- the router finished rebooting, the
power came back -- the clock finds it within ``hotspot_idle_s`` +
``retry_wifi_s`` of the AP going up, with nobody doing anything.

"Idle" is measured by requests to the portal, not by counting associated
stations.  Station counting needs ``iw`` and gets the answer wrong in the way
that matters: a phone can be associated while its owner has given up and gone
inside.  A phone that joins the AP, by contrast, fires its captive-portal
detection request within a second or two of associating, entirely
unprompted -- so portal traffic is both a more reliable signal that somebody
is actually there and one that needs no extra tools on the Pi.
"""

from __future__ import annotations

import enum
import logging
from typing import NamedTuple

from .settings import HotspotSettings

log = logging.getLogger(__name__)


class State(enum.StrEnum):
    """Where the monitor thinks it is."""

    #: On a real network.  Nothing to do, and the common case by a long way.
    ONLINE = "online"
    #: Off the network, waiting out the grace period before doing anything.
    WAITING = "waiting"
    #: The access point is up and the portal is serving.
    HOTSPOT = "hotspot"
    #: The access point has been dropped on purpose to let NetworkManager
    #: have another go at the saved networks.
    RETRYING = "retrying"
    #: Switched off, or unable to run: no ``nmcli``, or no AP password.
    DISABLED = "disabled"


class Action(enum.StrEnum):
    """What the caller should do about it."""

    NOTHING = "nothing"
    #: Scan, write the AP profile, bring it up, start the portal.
    RAISE_AP = "raise_ap"
    #: Bring the AP down and stop the portal, so the radio is free to scan
    #: and join.
    LOWER_AP = "lower_ap"


class Observation(NamedTuple):
    """What the world looks like right now.

    Attributes:
        now: Monotonic-ish seconds.  The monitor only ever subtracts these, so
            any consistent clock works, which is what lets tests run a day of
            outages in a millisecond.
        online: Associated with a *real* network and holding an IPv4 address.
            This must be false when the active connection is our own access
            point: ``ipv4.method shared`` gives the interface 10.42.0.1 and it
            reports as ``connected``, so a caller that forgets to exclude the
            AP profile will tell the monitor the AP is a success and the clock
            will stay an access point for ever.  :mod:`clockd.hotspot.service`
            is where that exclusion lives.
        ap_active: Our AP profile is the active connection on the interface.
        portal_last_seen: When the portal last served a request, or ``None``
            if it never has.
    """

    now: float
    online: bool
    ap_active: bool = False
    portal_last_seen: float | None = None


class Decision(NamedTuple):
    """The action, the state it leads to, and why -- the why goes in the log."""

    action: Action
    state: State
    reason: str = ""


class HotspotMonitor:
    """Tracks the state and decides the next action.

    Args:
        settings: Timings and names.
        blocked: A sentence explaining why the hotspot cannot run at all --
            no ``nmcli``, no AP password, switched off in config.  When set
            the monitor parks in :attr:`State.DISABLED` and never acts, but
            still reports that sentence from :meth:`status` so the reason
            ends up somewhere a person will read.
    """

    def __init__(self, settings: HotspotSettings, *, blocked: str | None = None):
        self.settings = settings
        self.blocked = blocked
        self.state = State.DISABLED if blocked else State.ONLINE
        #: When the current state was entered.
        self.since: float = 0.0
        #: How many times the AP has been raised since the service started.
        self.raises = 0
        #: How many times it has been dropped again without anyone joining.
        self.give_ups = 0
        self.last_reason = blocked or ""
        self._started = False

    # -- the state machine ----------------------------------------------

    def observe(self, observation: Observation) -> Decision:
        """Fold one observation in and return what to do.

        Calling this is what advances the machine; it is not a prediction.
        """
        if not self._started:
            self.since = observation.now
            self._started = True

        decision = self._decide(observation)
        if decision.state is not self.state:
            log.info(
                "hotspot: %s -> %s (%s)", self.state.value, decision.state.value, decision.reason
            )
            self.state = decision.state
            self.since = observation.now
        if decision.action is Action.RAISE_AP:
            self.raises += 1
        elif decision.action is Action.LOWER_AP:
            self.give_ups += 1
        self.last_reason = decision.reason
        return decision

    def _decide(self, observation: Observation) -> Decision:
        if self.blocked:
            return Decision(Action.NOTHING, State.DISABLED, self.blocked)

        elapsed = max(0.0, observation.now - self.since)
        settings = self.settings

        if self.state is State.ONLINE:
            if observation.online:
                return Decision(Action.NOTHING, State.ONLINE, "on a network")
            return Decision(Action.NOTHING, State.WAITING, "lost the network")

        if self.state is State.WAITING:
            if observation.online:
                return Decision(Action.NOTHING, State.ONLINE, "back on a network")
            if observation.ap_active:
                # The AP is up although we did not raise it this run -- a
                # restart of this service while it was up, most likely. Adopt
                # it rather than fighting it, so its timers start running.
                return Decision(
                    Action.NOTHING, State.HOTSPOT, "adopting an access point already up"
                )
            if elapsed >= settings.offline_grace_s:
                return Decision(
                    Action.RAISE_AP,
                    State.HOTSPOT,
                    f"offline for {elapsed:.0f}s, raising the setup hotspot",
                )
            remaining = settings.offline_grace_s - elapsed
            return Decision(
                Action.NOTHING, State.WAITING, f"offline, {remaining:.0f}s of grace left"
            )

        if self.state is State.HOTSPOT:
            if observation.online:
                # Unlikely with one radio, but if NetworkManager has somehow
                # got us onto a real network, that beats serving a portal.
                return Decision(Action.NOTHING, State.ONLINE, "on a network after all")
            if not observation.ap_active:
                # The AP went away without us asking: the raise failed, or
                # NetworkManager restarted. Go back through the grace period
                # rather than retrying immediately, which would spin.
                return Decision(
                    Action.NOTHING, State.WAITING, "the access point is not up; backing off"
                )
            if elapsed >= settings.hotspot_max_s:
                return Decision(
                    Action.LOWER_AP,
                    State.RETRYING,
                    f"access point up {elapsed:.0f}s, at the cap; retrying saved networks",
                )
            last_seen = observation.portal_last_seen
            idle_since = self.since if last_seen is None else max(self.since, last_seen)
            idle = max(0.0, observation.now - idle_since)
            if idle >= settings.hotspot_idle_s:
                return Decision(
                    Action.LOWER_AP,
                    State.RETRYING,
                    f"nobody has used the portal for {idle:.0f}s; retrying saved networks",
                )
            return Decision(Action.NOTHING, State.HOTSPOT, "access point up, portal serving")

        if self.state is State.RETRYING:
            if observation.online:
                return Decision(Action.NOTHING, State.ONLINE, "a saved network worked")
            if elapsed >= settings.retry_wifi_s:
                return Decision(
                    Action.RAISE_AP,
                    State.HOTSPOT,
                    "no saved network answered; putting the setup hotspot back up",
                )
            remaining = settings.retry_wifi_s - elapsed
            return Decision(
                Action.NOTHING, State.RETRYING, f"letting WiFi retry, {remaining:.0f}s left"
            )

        return Decision(Action.NOTHING, State.DISABLED, "disabled")

    # -- for the logs and the status page --------------------------------

    def note_joined(self, now: float) -> None:
        """Called when a join through the portal reports success.

        Moves straight to :attr:`State.ONLINE` rather than waiting for the
        next poll, so the AP is not reported as up while the clock is already
        back on the house network.
        """
        if self.blocked:
            return
        if self.state is not State.ONLINE:
            log.info("hotspot: %s -> online (a join through the portal worked)", self.state.value)
        self.state = State.ONLINE
        self.since = now

    def status(self) -> dict:
        """A snapshot for the status page and the journal."""
        return {
            "state": self.state.value,
            "reason": self.last_reason,
            "raises": self.raises,
            "give_ups": self.give_ups,
            "blocked": self.blocked,
            "ssid": self.settings.ssid,
        }

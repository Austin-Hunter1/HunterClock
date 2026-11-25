"""Location sources.

Four ways a position can reach the controller, in the order the spec gives:

1. :mod:`clockd.sources.owntracks` -- OwnTracks over MQTT, the primary source.
2. :mod:`clockd.sources.generic` -- our own ``clock/location/<person>`` topic,
   for anything else that can publish MQTT (a Home Assistant automation, a
   script, ``mosquitto_pub`` while testing).
3. :mod:`clockd.sources.hass` -- Home Assistant REST polling, optional.
4. Manual overrides from the UI, which are not a subscription at all and live
   in the engine.

Every source turns its own payload into a :class:`clockd.state.Fix` and hands it
to one callback.  The parsing is pure and tested separately from the plumbing,
because the payloads are the part that arrives from outside and surprises us.
"""

from __future__ import annotations

from .generic import GenericMqttSource, parse_generic
from .hass import HassSource, parse_hass_state
from .owntracks import OwnTracksSource, parse_owntracks

__all__ = [
    "GenericMqttSource",
    "HassSource",
    "OwnTracksSource",
    "ParseResult",
    "parse_generic",
    "parse_hass_state",
    "parse_owntracks",
]

from .common import ParseResult  # noqa: E402  (re-exported for convenience)

"""Configuration schema, loading and validation.

One YAML file describes the whole clock: where home is, who the six people are
and which hub level each one drives, how fast the hands may move, which location
sources are on, and how to reach MQTT and serve the UI.  Nothing about the site
is hard-coded -- the clock is going to a family home in Minnesota or Illinois
and the address is not final, so ``home`` is just another field.

Secrets are never in this file.  Passwords and tokens are named here only as
*environment variable names* (``password_env``, ``token_env``), and the UI
password is stored as a PBKDF2 hash.  ``config/clock.example.yaml`` is committed;
``config/clock.yaml`` and ``.env`` are gitignored.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import geo

#: Default hand colours, matching ``build_cad.py`` so the UI, the 3D model and
#: the physical hands agree.
DEFAULT_COLORS = {
    "Becky": "#4F7A4A",
    "Evan": "#5B5EA6",
    "Elijah": "#3E6470",
    "Caleb": "#A8672B",
    "Austin": "#8A4B6B",
    "Micah": "#B0562A",
}

#: Microsteps per revolution: 200 full steps at 1/16 microstepping.  Every hand
#: is mounted directly on its motor shaft (``docs/hardware-plan.html``), so one
#: motor revolution is one hand revolution -- there is no gearing anywhere.
DEFAULT_STEPS_PER_REV = 3200


class Strict(BaseModel):
    """Base model that rejects unknown keys, so a typo is an error not a shrug."""

    model_config = ConfigDict(extra="forbid")


class Home(Strict):
    """Where 12 o'clock is."""

    name: str = "Home"
    lat: float = Field(44.9109, ge=-90.0, le=90.0)
    lon: float = Field(-93.5017, ge=-180.0, le=180.0)


class OwnTracksId(Strict):
    """How to recognise a person in the OwnTracks topic tree."""

    user: str
    device: str = "+"


class Person(Strict):
    """One family member and the hand that points for them.

    Attributes:
        level: Hub level 1-6, which fixes the channel: channel = level - 1, and
            the STEP/DIR pins follow from ``docs/circuit-diagram.html``.
        invert: Flip the direction of travel.  Set this when a hand turns
            anticlockwise, which happens if the motor's coil pair is wired the
            other way round; it is cheaper to fix here than to resolder.
        steps_per_rev: Microsteps for one full turn of this hand.
        zero_offset_deg: Added to the computed angle before commanding the hand,
            for a hand that was zeroed slightly off 12.
        home_lat/home_lon: A fallback position used when no fix has ever been
            seen, so the UI has something to show on a cold start.
    """

    name: str
    level: int = Field(ge=1, le=6)
    color: str = "#888888"
    enabled: bool = True
    invert: bool = False
    steps_per_rev: int = Field(DEFAULT_STEPS_PER_REV, gt=0, le=200_000)
    zero_offset_deg: float = 0.0
    fallback_lat: float | None = Field(None, ge=-90.0, le=90.0)
    fallback_lon: float | None = Field(None, ge=-180.0, le=180.0)
    owntracks: OwnTracksId | None = None
    hass_entity: str | None = None

    @property
    def channel(self) -> int:
        """Pico channel index for this person's hand (level 1 is channel 0)."""
        return self.level - 1

    @property
    def slug(self) -> str:
        """Lowercase name, used in MQTT topics and URLs."""
        return self.name.strip().lower().replace(" ", "-")

    @field_validator("color")
    @classmethod
    def _check_color(cls, value: str) -> str:
        text = value.strip()
        if not (text.startswith("#") and len(text) == 7):
            raise ValueError("color must be a #rrggbb hex string")
        int(text[1:], 16)  # raises ValueError on non-hex
        return text

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def _fallback_is_a_pair(self) -> Person:
        if (self.fallback_lat is None) != (self.fallback_lon is None):
            raise ValueError("fallback_lat and fallback_lon must be given together")
        return self


class Motion(Strict):
    """Speed and acceleration, and the ceilings the UI sliders respect.

    ``max_speed`` and ``accel`` are in microsteps; at 3200 microsteps/rev the
    default 800 steps/s is a quarter turn per second, which looks deliberate
    rather than frantic and keeps well inside what a brass tube stack with no
    homing sensors can do without losing steps.
    """

    max_speed: float = Field(800.0, gt=0)
    accel: float = Field(1600.0, gt=0)
    speed_limit: float = Field(2000.0, gt=0)
    accel_limit: float = Field(8000.0, gt=0)
    idle_disable: bool = False
    watchdog_s: float = Field(10.0, ge=0)
    pos_interval_ms: int = Field(200, ge=0, le=60_000)
    #: Slow speed used by the zeroing wizard and the test sweep.
    assist_speed: float = Field(200.0, gt=0)

    @model_validator(mode="after")
    def _within_limits(self) -> Motion:
        if self.max_speed > self.speed_limit:
            raise ValueError(
                f"max_speed {self.max_speed:.1f} exceeds "
                f"speed_limit {self.speed_limit:.1f}"
            )
        if self.accel > self.accel_limit:
            raise ValueError(f"accel {self.accel:.1f} exceeds accel_limit {self.accel_limit:.1f}")
        if self.assist_speed > self.speed_limit:
            raise ValueError("assist_speed exceeds speed_limit")
        return self


class Filtering(Strict):
    """Which fixes to believe, and when to stop believing them.

    Attributes:
        max_accuracy_m: Reject a fix whose reported accuracy circle is larger
            than this.  A 5 km cell-tower fix would swing a hand across half the
            dial for nothing.
        deadband_deg: Do not move the hand for a change smaller than this.  The
            hands are open loop with no encoders, so every needless move is a
            chance to lose a step.
        min_move_interval_s: Shortest gap between two commanded moves per hand.
        stale_after_s: With no fix for this long, the person goes ``stale``: the
            hand stays where it is and the UI and the display both flag it.
        max_age_s: Reject a fix whose own timestamp is older than this, which
            is what arrives when a phone comes back from airplane mode and
            flushes a queue of old points.
    """

    max_accuracy_m: float = Field(2000.0, gt=0)
    deadband_deg: float = Field(0.5, ge=0)
    min_move_interval_s: float = Field(5.0, ge=0)
    stale_after_s: float = Field(6 * 3600.0, gt=0)
    max_age_s: float = Field(24 * 3600.0, gt=0)


class OwnTracksSource(Strict):
    """OwnTracks over MQTT: ``owntracks/<user>/<device>``."""

    enabled: bool = True
    topic_prefix: str = "owntracks"


class GenericMqttSource(Strict):
    """Our own simple topic: ``clock/location/<person>``."""

    enabled: bool = True
    topic_prefix: str = "clock/location"


class HassSource(Strict):
    """Home Assistant REST polling (optional, lowest priority source)."""

    enabled: bool = False
    url: str = "http://homeassistant.local:8123"
    token_env: str = "HASS_TOKEN"
    poll_interval_s: float = Field(120.0, gt=0)
    verify_tls: bool = True


class ManualSource(Strict):
    """UI overrides: a fake location or distance with an expiry."""

    enabled: bool = True
    default_ttl_s: float = Field(3600.0, gt=0)


class Sources(Strict):
    """All four location sources.  Each can be switched off from the UI."""

    owntracks: OwnTracksSource = OwnTracksSource()
    generic_mqtt: GenericMqttSource = GenericMqttSource()
    hass: HassSource = HassSource()
    manual: ManualSource = ManualSource()

    #: Which source wins when two have a recent fix.  Earlier is stronger.
    priority: list[str] = ["manual", "owntracks", "generic_mqtt", "hass"]

    @field_validator("priority")
    @classmethod
    def _known_sources(cls, value: list[str]) -> list[str]:
        known = {"manual", "owntracks", "generic_mqtt", "hass"}
        unknown = set(value) - known
        if unknown:
            raise ValueError("unknown source(s) in priority: {}".format(", ".join(sorted(unknown))))
        if len(set(value)) != len(value):
            raise ValueError("priority has duplicates")
        return value


class Mqtt(Strict):
    """Broker connection.  The password only ever comes from the environment."""

    enabled: bool = True
    host: str = "localhost"
    port: int = Field(1883, gt=0, lt=65536)
    username: str = "clock"
    password_env: str = "MQTT_PASSWORD"
    client_id: str = "clockd"
    base_topic: str = "clock"
    keepalive_s: int = Field(30, gt=0)
    tls: bool = False

    def password(self) -> str | None:
        """Read the password from the environment, or ``None`` if unset."""
        return os.environ.get(self.password_env) or None


class Serial(Strict):
    """The USB link to the Pico."""

    port: str = "/dev/ttyACM0"
    baudrate: int = Field(115200, gt=0)
    reconnect_interval_s: float = Field(2.0, gt=0)
    command_timeout_s: float = Field(3.0, gt=0)
    ping_interval_s: float = Field(3.0, gt=0)
    max_retries: int = Field(2, ge=0, le=10)


class Web(Strict):
    """The UI.  One shared password; it is reachable over Tailscale only.

    ``password_hash`` is a PBKDF2 string from ``python -m clockd.hashpw``.  In
    ``--sim`` mode with no hash set, ``CLOCK_UI_PASSWORD`` from the environment
    is accepted instead so a laptop demo needs no setup; the server logs loudly
    when it does that and refuses it outside sim mode.
    """

    host: str = "0.0.0.0"  # noqa: S104 - Tailscale-only; see RUNBOOK
    port: int = Field(8080, gt=0, lt=65536)
    password_hash: str = ""
    secret_key_env: str = "CLOCK_SECRET_KEY"
    session_hours: float = Field(720.0, gt=0)
    title: str = "Weasley Clock"


class Alerts(Strict):
    """Optional outbound pings when something breaks (P2)."""

    enabled: bool = False
    healthchecks_url_env: str = "HEALTHCHECKS_URL"
    ntfy_url_env: str = "NTFY_URL"
    interval_s: float = Field(300.0, gt=0)


class Paths(Strict):
    """Where state and logs live.  Relative paths resolve against the repo."""

    var_dir: str = "var"
    log_dir: str = "var/log"


class ClockConfig(Strict):
    """The whole configuration."""

    version: Literal[1] = 1
    site: str = "unset"
    home: Home = Home()
    people: list[Person]
    motion: Motion = Motion()
    filtering: Filtering = Filtering()
    sources: Sources = Sources()
    mqtt: Mqtt = Mqtt()
    serial: Serial = Serial()
    web: Web = Web()
    alerts: Alerts = Alerts()
    paths: Paths = Paths()

    @field_validator("people")
    @classmethod
    def _people_are_consistent(cls, value: list[Person]) -> list[Person]:
        if not value:
            raise ValueError("at least one person is required")
        if len(value) > 6:
            raise ValueError(f"the clock has six hands; got {len(value)} people")
        levels = [person.level for person in value]
        if len(set(levels)) != len(levels):
            duplicates = sorted({lv for lv in levels if levels.count(lv) > 1})
            raise ValueError(f"two people share hub level(s) {duplicates}")
        slugs = [person.slug for person in value]
        if len(set(slugs)) != len(slugs):
            raise ValueError("two people have the same name")
        return value

    # -- lookups ---------------------------------------------------------

    def person(self, name: str) -> Person:
        """Find a person by name or slug, case-insensitively."""
        wanted = name.strip().lower()
        for person in self.people:
            if person.slug == wanted or person.name.lower() == wanted:
                return person
        raise KeyError(f"no person named {name!r}")

    def person_for_channel(self, channel: int) -> Person | None:
        """The person driven by a Pico channel, or ``None`` if unassigned."""
        for person in self.people:
            if person.channel == channel:
                return person
        return None

    def person_for_owntracks(self, user: str, device: str) -> Person | None:
        """Match an ``owntracks/<user>/<device>`` topic to a person."""
        for person in self.people:
            ident = person.owntracks
            if ident is None:
                continue
            if ident.user.lower() != user.lower():
                continue
            if ident.device in ("+", "", "*") or ident.device.lower() == device.lower():
                return person
        return None

    @property
    def channels(self) -> int:
        """How many Pico channels the hub has: always six on this clock."""
        return 6

    def steps_per_degree(self, person: Person) -> float:
        """Microsteps per degree of hand travel for one person."""
        return person.steps_per_rev / 360.0

    def angle_to_steps(self, person: Person, angle_deg: float) -> float:
        """Convert a hand angle to an unwrapped microstep position.

        Applies the person's zero offset and direction inversion.  ``invert``
        flips the sense of rotation, which is what you reach for when a hand
        turns the wrong way because its coil pair is wired reversed.
        """
        adjusted = angle_deg + person.zero_offset_deg
        steps = adjusted * self.steps_per_degree(person)
        return -steps if person.invert else steps

    def steps_to_angle(self, person: Person, steps: float) -> float:
        """Inverse of :meth:`angle_to_steps`, in degrees (unwrapped)."""
        value = -steps if person.invert else steps
        return value / self.steps_per_degree(person) - person.zero_offset_deg

    def angle_for(self, lat: float, lon: float) -> tuple[float, float]:
        """Distance and angle for a position, relative to this clock's home."""
        return geo.angle_for_position(self.home.lat, self.home.lon, lat, lon)

    # -- paths -----------------------------------------------------------

    def var_path(self, *parts: str, root: Path | None = None) -> Path:
        """Resolve a path under ``paths.var_dir``, creating the directory."""
        base = Path(self.paths.var_dir)
        if not base.is_absolute():
            base = (root or repo_root()) / base
        base.mkdir(parents=True, exist_ok=True)
        return base.joinpath(*parts)

    def log_path(self, *parts: str, root: Path | None = None) -> Path:
        """Resolve a path under ``paths.log_dir``, creating the directory."""
        base = Path(self.paths.log_dir)
        if not base.is_absolute():
            base = (root or repo_root()) / base
        base.mkdir(parents=True, exist_ok=True)
        return base.joinpath(*parts)


def repo_root() -> Path:
    """The checkout root, two levels up from this file's package."""
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    """``config/clock.yaml`` in the checkout, or ``$CLOCK_CONFIG`` if set."""
    override = os.environ.get("CLOCK_CONFIG")
    if override:
        return Path(override).expanduser()
    return repo_root() / "config" / "clock.yaml"


def example_config_path() -> Path:
    """``config/clock.example.yaml`` in the checkout."""
    return repo_root() / "config" / "clock.example.yaml"


class ConfigError(Exception):
    """Configuration that cannot be loaded or does not validate."""


def parse_config(data: Any) -> ClockConfig:
    """Validate an already-parsed mapping into a :class:`ClockConfig`.

    Raises:
        ConfigError: with every validation problem, one per line.
    """
    if data is None:
        raise ConfigError("the configuration is empty")
    if not isinstance(data, dict):
        raise ConfigError("the configuration must be a YAML mapping")
    try:
        return ClockConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(format_validation_error(exc)) from exc


def format_validation_error(exc: Exception) -> str:
    """Turn a pydantic error into something a tired person can read."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    lines = []
    for error in errors():
        where = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
        lines.append("{}: {}".format(where, error.get("msg", "invalid")))
    return "\n".join(lines) or str(exc)


def load_yaml(text: str) -> Any:
    """Parse YAML text, raising :class:`ConfigError` on a syntax error."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML syntax error: {exc}") from exc


def load_config(
    path: Path | str | None = None, *, fall_back_to_example: bool = True
) -> ClockConfig:
    """Load and validate the configuration.

    Args:
        path: File to read.  Defaults to :func:`default_config_path`.
        fall_back_to_example: When the real config is missing, read
            ``config/clock.example.yaml`` instead.  This is what lets ``--sim``
            run on a fresh laptop checkout with no setup at all.

    Raises:
        ConfigError: if the file is missing (and no fallback), unparseable or
            invalid.
    """
    target = Path(path) if path else default_config_path()
    if not target.is_file():
        if fall_back_to_example and example_config_path().is_file():
            target = example_config_path()
        else:
            raise ConfigError(
                f"no configuration at {target} (copy config/clock.example.yaml to "
                "config/clock.yaml)"
            )
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from exc
    config = parse_config(load_yaml(text))
    return config


def dump_config(config: ClockConfig) -> str:
    """Serialise a configuration back to YAML."""
    data = config.model_dump(mode="json", exclude_none=False)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)

"""Check what Home Assistant is exposing, and whether the clock can use it.

    export HASS_TOKEN='...'
    python tools/hass_check.py --url http://homeassistant.local:8123

Run this before editing ``config/clock.yaml``. It answers the four questions
that otherwise turn into a hand that never moves:

1. Can the clock reach Home Assistant and is the token accepted?
2. Which entities exist, and which of them actually carry coordinates? A Find
   My device that is offline, or a tracker that works by router or geofence,
   reports ``home``/``not_home`` with no latitude at all -- and that cannot
   drive a hand.
3. Where would each one put its hand right now? The distance and angle are
   computed with the clock's own mapping, so a number that looks wrong here is
   wrong.
4. **Is each person pointed at the right kind of entity?**  Home Assistant has
   two that carry a position, and the difference decides how this clock behaves
   on the day Find My stops answering:

   ``person.austin``
       Home Assistant's own aggregate of everything that knows where Austin
       is, using whichever is currently reporting. Prefer this.
   ``device_tracker.austin_iphone``
       One specific tracker. When iCloud stops answering -- a 2FA re-prompt,
       Apple rate-limiting -- this goes quiet and the hand goes stale, even
       though the companion app on the same phone is still reporting.

   So this tool lists the person entities first, and says so when somebody is
   wired to a bare tracker that a person entity already covers.

It only reads. It changes nothing, and it never prints the token.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "controller"))

GREEN, YELLOW, RED, DIM, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def icon(ok: bool | None) -> str:
    """A status glyph: yes, no, or not sure."""
    if ok is None:
        return f"{YELLOW}?{OFF}"
    return f"{GREEN}OK{OFF}" if ok else f"{RED}no{OFF}"


def main(argv: list[str] | None = None) -> int:
    """Probe Home Assistant and report what the clock could use."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url", default=None, help="Home Assistant base URL (default: from config)"
    )
    parser.add_argument("--token-env", default="HASS_TOKEN")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    parser.add_argument("--all", action="store_true", help="list every entity, not just trackers")
    args = parser.parse_args(argv)

    import httpx
    from clockd.config import ConfigError, load_config

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"{RED}Configuration error:{OFF}\n{exc}", file=sys.stderr)
        return 2

    url = (args.url or config.sources.hass.url).rstrip("/")
    token = os.environ.get(args.token_env)

    print(f"Home Assistant : {url}")
    print(f"Token          : {args.token_env} is {'set' if token else 'NOT SET'}")
    print(f"Home           : {config.home.name} ({config.home.lat}, {config.home.lon})")
    print()

    if not token:
        print(f"{RED}No token.{OFF} Create a long-lived access token in Home Assistant:")
        print("  your profile (bottom left) -> Security -> Long-lived access tokens")
        print(f"  -> Create token, then put it in .env as {args.token_env}=...")
        return 1

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    verify = not args.insecure and config.sources.hass.verify_tls

    try:
        with httpx.Client(timeout=10.0, verify=verify) as client:
            alive = client.get(f"{url}/api/", headers=headers)
            if alive.status_code == 401:
                print(f"{RED}Home Assistant rejected the token (401).{OFF}")
                print("  The token is wrong, expired, or was revoked. Make a new one.")
                return 1
            if alive.status_code != 200:
                print(f"{RED}Unexpected reply from {url}/api/: HTTP {alive.status_code}{OFF}")
                return 1
            print(f"{GREEN}Connected.{OFF} {alive.json().get('message', '')}\n")

            states = client.get(f"{url}/api/states", headers=headers).json()
    except httpx.HTTPError as exc:
        print(f"{RED}Cannot reach Home Assistant:{OFF} {exc}")
        print("\n  Check the URL, and that the Pi can see Home Assistant on the network.")
        print("  If Home Assistant is on https with a self-signed certificate, try --insecure.")
        return 1

    people_entities = [s for s in states if str(s.get("entity_id", "")).startswith("person.")]
    trackers = [s for s in states if str(s.get("entity_id", "")).startswith("device_tracker.")]
    print(
        f"{len(states)} entities: {len(people_entities)} person, "
        f"{len(trackers)} device_tracker.\n"
    )

    if not people_entities and not trackers:
        print(f"{YELLOW}Nothing that knows where anybody is.{OFF}")
        print("  Home Assistant is not tracking anybody yet. Add the iCloud integration")
        print("  (Settings -> Devices & services -> Add integration -> Apple Account),")
        print("  or install OwnTracks on the phones instead -- see docs/RUNBOOK.md.")
        return 1

    mapped = {p.hass_entity: p.name for p in config.people if p.hass_entity}
    usable: set[str] = set()

    def show(group: list[dict], heading: str, note: str) -> None:
        """Print one table of entities."""
        if not group:
            return
        print(f"{heading}")
        print(f"{DIM}{note}{OFF}")
        print(f"{'entity_id':44s} {'usable':8s} {'distance':>10s} {'angle':>8s}  mapped to")
        print("-" * 96)
        for state in sorted(group, key=lambda s: s["entity_id"]):
            entity = state["entity_id"]
            attributes = state.get("attributes") or {}
            lat, lon = attributes.get("latitude"), attributes.get("longitude")
            person = mapped.get(entity)
            label = person or DIM + "not mapped" + OFF

            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                usable.add(entity)
                distance, angle = config.angle_for(float(lat), float(lon))
                accuracy = attributes.get("gps_accuracy")
                extra = ""
                if (
                    isinstance(accuracy, (int, float))
                    and accuracy > config.filtering.max_accuracy_m
                ):
                    extra = f"  {YELLOW}accuracy {accuracy:.0f} m - would be rejected{OFF}"
                # A person entity names the tracker it is following right now.
                # That is the line that tells you Find My has quietly stopped
                # and the companion app is carrying the hand.
                via = attributes.get("source")
                if isinstance(via, str) and via:
                    extra += f"  {DIM}via {via}{OFF}"
                print(
                    f"{entity:44s} {icon(True):17s} {distance:8.1f} km {angle:7.1f}°  "
                    f"{label}{extra}"
                )
            else:
                print(
                    f"{entity:44s} {icon(False):17s} {'-':>10s} {'-':>8s}  {label}"
                    f"  {DIM}state={state.get('state', '?')}, no coordinates{OFF}"
                )
        print()

    show(
        people_entities,
        "PERSON ENTITIES -- prefer these",
        "Home Assistant's aggregate of everything that knows where somebody is.\n"
        "Survives Find My going quiet, because it falls back to whatever else is\n"
        "still reporting for that person.",
    )
    show(
        trackers,
        "DEVICE TRACKERS -- one specific source each",
        "Use one of these only for somebody with no person entity. A tracker that\n"
        "stops reporting takes its hand stale with it; a person entity would not.",
    )

    print(f"{len(usable)} entities carry coordinates and could drive a hand.")

    missing = [p.name for p in config.people if not p.hass_entity]
    if missing:
        print(f"{YELLOW}No hass_entity set for: {', '.join(missing)}{OFF}")

    # The diagnostic worth having: somebody wired to a bare tracker that a
    # person entity already covers. It works today and is fragile in exactly
    # the way this source is known to be fragile.
    covered_by: dict[str, str] = {}
    for state in people_entities:
        for tracker in (state.get("attributes") or {}).get("device_trackers") or []:
            if isinstance(tracker, str):
                covered_by[tracker] = state["entity_id"]
    fragile = [
        (entity, covered_by[entity])
        for entity in mapped
        if entity.startswith("device_tracker.") and entity in covered_by
    ]
    if fragile:
        print()
        print(f"{YELLOW}These are pointed at one tracker when a person entity covers it:{OFF}")
        for entity, person_entity in sorted(fragile):
            print(f"    {mapped[entity]}: {entity}")
            print(f"      {GREEN}->{OFF} use {person_entity} instead")
        print(f"  {DIM}A person entity keeps working when Find My stops answering.{OFF}")

    unmapped_usable = sorted(entity for entity in usable if entity not in mapped)
    if unmapped_usable:
        print("\nTo use these, put them in config/clock.yaml under the right person:")
        for entity in unmapped_usable:
            print(f"    hass_entity: {entity}")

    print("\nThen set sources.hass.enabled: true and restart the controller.")
    print(f"{DIM}Find My is a supplement, not a foundation: it is rate limited by Apple and")
    print(f"breaks on a 2FA re-prompt. Keep OwnTracks on the phones that matter.{OFF}")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())

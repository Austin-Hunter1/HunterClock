"""Check what Home Assistant is exposing, and whether the clock can use it.

    export HASS_TOKEN='...'
    python tools/hass_check.py --url http://homeassistant.local:8123

Run this before editing ``config/clock.yaml``. It answers the three questions
that otherwise turn into a hand that never moves:

1. Can the clock reach Home Assistant and is the token accepted?
2. Which ``device_tracker`` entities exist, and which of them actually carry
   coordinates? A Find My device that is offline, or a tracker that works by
   router or geofence, reports ``home``/``not_home`` with no latitude at all --
   and that cannot drive a hand.
3. Where would each one put its hand right now? The distance and angle are
   computed with the clock's own mapping, so a number that looks wrong here is
   wrong.

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

    trackers = [s for s in states if str(s.get("entity_id", "")).startswith("device_tracker.")]
    print(f"{len(states)} entities, {len(trackers)} of them device_tracker.\n")

    if not trackers:
        print(f"{YELLOW}No device_tracker entities at all.{OFF}")
        print("  Home Assistant is not tracking anybody yet. Add the iCloud integration")
        print("  (Settings -> Devices & services -> Add integration -> Apple iCloud),")
        print("  or install OwnTracks on the phones instead -- see docs/RUNBOOK.md.")
        return 1

    mapped = {
        p.hass_entity: p.name for p in config.people if p.hass_entity
    }

    print(f"{'entity_id':44s} {'usable':8s} {'distance':>10s} {'angle':>8s}  mapped to")
    print("-" * 92)
    usable = 0
    for state in sorted(trackers, key=lambda s: s["entity_id"]):
        entity = state["entity_id"]
        attributes = state.get("attributes") or {}
        lat, lon = attributes.get("latitude"), attributes.get("longitude")
        person = mapped.get(entity)

        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            usable += 1
            distance, angle = config.angle_for(float(lat), float(lon))
            accuracy = attributes.get("gps_accuracy")
            note = ""
            if isinstance(accuracy, (int, float)) and accuracy > config.filtering.max_accuracy_m:
                note = f"  {YELLOW}accuracy {accuracy:.0f} m — would be rejected{OFF}"
            print(
                f"{entity:44s} {icon(True):17s} {distance:8.1f} km {angle:7.1f}°  "
                f"{person or DIM + 'not mapped' + OFF}{note}"
            )
        else:
            reason = state.get("state", "?")
            print(
                f"{entity:44s} {icon(False):17s} {'—':>10s} {'—':>8s}  "
                f"{person or DIM + 'not mapped' + OFF}"
                f"  {DIM}state={reason}, no coordinates{OFF}"
            )

    print()
    print(f"{usable} of {len(trackers)} trackers carry coordinates.")

    missing = [p.name for p in config.people if not p.hass_entity]
    if missing:
        print(f"{YELLOW}No hass_entity set for: {', '.join(missing)}{OFF}")

    unmapped_usable = [
        s["entity_id"]
        for s in trackers
        if s["entity_id"] not in mapped
        and isinstance((s.get("attributes") or {}).get("latitude"), (int, float))
    ]
    if unmapped_usable:
        print("\nTo use these, put them in config/clock.yaml:")
        for entity in unmapped_usable:
            print(f"    hass_entity: {entity}")

    print("\nThen set sources.hass.enabled: true and restart the controller.")
    print(f"{DIM}Find My is a supplement, not a foundation: it is rate limited by Apple and")
    print(f"breaks on a 2FA re-prompt. Keep OwnTracks on the phones that matter.{OFF}")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())

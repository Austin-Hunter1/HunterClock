"""Shared pieces for the location sources."""

from __future__ import annotations

import json
from typing import Any, NamedTuple

from ..config import Person
from ..state import Fix


class ParseResult(NamedTuple):
    """What a parser made of one payload.

    Exactly one of ``fix`` or ``error`` is set.  ``ignored`` marks a payload
    that is valid but not a location at all -- an OwnTracks ``_type: lwt``, say
    -- which must not be logged as an error or there would be noise for every
    phone every few minutes.
    """

    person: Person | None = None
    fix: Fix | None = None
    error: str | None = None
    ignored: bool = False


def decode_json(payload: bytes | str) -> tuple[Any, str | None]:
    """Decode a JSON payload.

    Returns:
        ``(value, error)``; ``error`` is a message when it could not be parsed.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8")
        except (UnicodeError, ValueError):
            return None, "payload is not valid UTF-8"
    text = payload.strip()
    if not text:
        return None, "empty payload"
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, f"payload is not JSON ({exc})"


def number(value: Any) -> float | None:
    """Coerce a JSON value to a float, or ``None`` if it is not a number.

    Accepts the strings some integrations send (``"44.9109"``), and rejects
    booleans, which are ints in Python and would otherwise sail through.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    if isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
        return result if result == result and abs(result) != float("inf") else None
    return None

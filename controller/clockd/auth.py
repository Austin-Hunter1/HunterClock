"""Password hashing and session cookies for the web UI.

One shared password, because the UI is reachable over Tailscale and the people
who use it are one family.  The password is stored as a PBKDF2-HMAC-SHA256 hash
with a random salt -- never in plaintext, and never in the repo.

Sessions are signed cookies: ``itsdangerous`` when it is installed, otherwise an
equivalent HMAC-SHA256 token built here, so the controller works on a Pi image
with one fewer dependency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

#: PBKDF2 rounds.  200k is comfortable on a Pi 4 (tens of milliseconds) and the
#: login happens once per browser per month.
ITERATIONS = 200_000
_PREFIX = "pbkdf2_sha256"


def hash_password(password: str, *, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    """Hash a password into a single self-describing string.

    Args:
        password: The plaintext password.
        iterations: PBKDF2 rounds.
        salt: Optional fixed salt, for tests only.

    Returns:
        ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``.
    """
    if not password:
        raise ValueError("password must not be empty")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "%s$%d$%s$%s" % (
        _PREFIX,
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a hash from :func:`hash_password`.

    Returns ``False`` for an empty password or a malformed hash rather than
    raising, so a half-configured install cannot be logged into by accident.
    """
    if not password or not encoded:
        return False
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != _PREFIX:
        return False
    try:
        iterations = int(parts[1])
        salt = base64.b64decode(parts[2])
        expected = base64.b64decode(parts[3])
    except (ValueError, TypeError):
        return False
    if iterations <= 0:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def secret_key(env_name: str = "CLOCK_SECRET_KEY") -> str:
    """Session signing key from the environment, or a per-process random one.

    A random key means a controller restart logs everyone out, which is only an
    inconvenience; it is better than shipping a default key that would let
    anyone forge a session.
    """
    return os.environ.get(env_name) or secrets.token_hex(32)


def _sign(key: str, payload: bytes) -> str:
    mac = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()
    return "%s.%s" % (
        base64.urlsafe_b64encode(payload).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(mac).decode("ascii").rstrip("="),
    )


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def make_session_token(key: str, *, max_age_s: float, subject: str = "owner") -> str:
    """Create a signed session token that expires on its own."""
    payload = json.dumps(
        {"sub": subject, "exp": int(time.time() + max_age_s)}, separators=(",", ":")
    ).encode("utf-8")
    return _sign(key, payload)


def read_session_token(key: str, token: str | None) -> dict | None:
    """Verify a session token and return its claims, or ``None`` if invalid.

    Rejects a bad signature, a malformed token and an expired one alike, and
    never raises on attacker-supplied input.
    """
    if not token or "." not in token:
        return None
    body, _, signature = token.rpartition(".")
    try:
        payload = _b64decode(body)
        provided = _b64decode(signature)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(provided, expected):
        return None
    try:
        claims = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    if float(claims.get("exp", 0)) < time.time():
        return None
    return claims

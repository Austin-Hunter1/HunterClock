"""Session authentication for the UI.

One shared password for the whole clock.  Three ways it can be configured, in
order:

1. ``web.password_hash`` in the YAML -- the real deployment.  Generate it with
   ``python -m clockd.hashpw``; the plaintext never touches the disk.
2. ``CLOCK_UI_PASSWORD`` in the environment -- hashed once at startup, held only
   in memory.  Convenient for a laptop.
3. Neither, in ``--sim`` mode -- no login at all, with a banner on every page
   saying so, and the server bound to localhost by default.

In live mode with no password configured the controller refuses to start, rather
than serving the clock's controls to the network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..auth import (
    hash_password,
    make_session_token,
    read_session_token,
    secret_key,
    verify_password,
)

#: Name of the session cookie.
COOKIE_NAME = "clockd_session"


class AuthError(Exception):
    """The UI cannot be served safely with this configuration."""


@dataclass
class Authenticator:
    """Decides who may use the UI.

    Attributes:
        password_hash: The PBKDF2 hash to check against, or ``""``.
        secret: Session signing key.
        session_hours: How long a login lasts.
        open_access: True when there is no password at all (sim only).
        source: Where the password came from, shown on the System page.
    """

    password_hash: str
    secret: str
    session_hours: float
    open_access: bool
    source: str

    @classmethod
    def from_config(cls, config, *, sim: bool) -> Authenticator:
        """Build an authenticator, or raise if the setup is unsafe.

        Raises:
            AuthError: live mode with no password configured.
        """
        web = config.web
        secret = secret_key(web.secret_key_env)
        if web.password_hash:
            return cls(web.password_hash, secret, web.session_hours, False, "config")
        env_password = os.environ.get("CLOCK_UI_PASSWORD", "")
        if env_password:
            return cls(
                hash_password(env_password),
                secret,
                web.session_hours,
                False,
                "CLOCK_UI_PASSWORD",
            )
        if sim:
            return cls("", secret, web.session_hours, True, "none (sim mode)")
        raise AuthError(
            "No UI password is set, so the controller will not start.\n"
            "Either run `python -m clockd.hashpw` and put the hash in "
            "config/clock.yaml under web.password_hash, or set CLOCK_UI_PASSWORD "
            "in .env. Use --sim to run without a password on a laptop."
        )

    def check(self, password: str) -> bool:
        """Is this the right password?"""
        if self.open_access:
            return True
        return verify_password(password, self.password_hash)

    def issue(self) -> str:
        """Create a session token for a successful login."""
        return make_session_token(self.secret, max_age_s=self.session_hours * 3600.0)

    def valid(self, token: str | None) -> bool:
        """Is this session token good?"""
        if self.open_access:
            return True
        return read_session_token(self.secret, token) is not None

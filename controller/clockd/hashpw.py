"""``python -m clockd.hashpw`` -- make a password hash for config/clock.yaml.

Prompts without echo, asks twice, and prints one line to paste into
``web.password_hash``.  The plaintext is never written anywhere.
"""

from __future__ import annotations

import getpass
import sys

from .auth import hash_password


def main(argv: list[str] | None = None) -> int:
    """Prompt for a password and print its hash."""
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv:
        # Given on the command line: convenient for scripts, but it lands in the
        # shell history, so say so.
        password = argv[0]
        print("warning: the password is now in your shell history", file=sys.stderr)
    else:
        password = getpass.getpass("UI password: ")
        again = getpass.getpass("Again: ")
        if password != again:
            print("passwords do not match", file=sys.stderr)
            return 1
    if not password:
        print("password must not be empty", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("warning: shorter than 8 characters", file=sys.stderr)
    print()
    print("Paste this into config/clock.yaml under web.password_hash:")
    print()
    print(f"  password_hash: \"{hash_password(password)}\"")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

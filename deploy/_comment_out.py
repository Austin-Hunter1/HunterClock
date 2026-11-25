#!/usr/bin/env python3
"""Comment out one Mosquitto directive in a config file, saying who owns it.

Used by ``deploy/mosquitto-setup.sh``.  Most of Mosquitto's settings are
single-valued: a second ``password_file`` is not an override but

    Error: Duplicate password_file value in configuration

and the broker refuses to start.  So on a broker somebody has already
configured, our drop-in has to defer rather than restate, and the line is
left in place commented so it is obvious what was inherited and from where.

    _comment_out.py <file> <directive> <where-it-is-already-declared>
"""

from __future__ import annotations

import pathlib
import re
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path, directive, where = pathlib.Path(argv[1]), argv[2], argv[3]
    pattern = re.compile(rf"\s*{re.escape(directive)}\s")
    lines = path.read_text(encoding="utf-8").splitlines()
    out = [
        f"# (already set in {where}, so left to it) {line}"
        if pattern.match(line)
        else line
        for line in lines
    ]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

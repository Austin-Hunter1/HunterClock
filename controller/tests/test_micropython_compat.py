"""Guard against CPython-only idioms in the Pico firmware.

MicroPython does not expose ``Exception.__init__``.  Calling it raised
``AttributeError: type object 'Exception' has no attribute '__init__'`` on the
first malformed line the Pico ever received, which dropped the firmware to the
REPL (found on real hardware).  Running the firmware under CPython cannot catch
this, so scan the source instead.
"""

import ast
from pathlib import Path

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware" / "pico"

BUILTIN_EXCEPTIONS = {
    "BaseException",
    "Exception",
    "ArithmeticError",
    "AttributeError",
    "IndexError",
    "KeyError",
    "LookupError",
    "OSError",
    "RuntimeError",
    "TypeError",
    "ValueError",
}


def _offenders(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__init__"
            and isinstance(node.value, ast.Name)
            and node.value.id in BUILTIN_EXCEPTIONS
        ):
            yield "%s:%d %s.__init__" % (path.name, node.lineno, node.value.id)


def test_no_builtin_exception_init_calls():
    found = []
    for path in sorted(FIRMWARE.glob("*.py")):
        found.extend(_offenders(path))
    assert not found, (
        "MicroPython has no Exception.__init__; set attributes directly instead: "
        + ", ".join(found)
    )

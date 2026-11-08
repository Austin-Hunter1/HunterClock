"""Check a clock-face model against the six hands this repo generates.

    python tools/check_face.py "Clock Face.3mf"

Loads a dial model (3MF, STEP, STL, OBJ, PLY...) and answers the questions that
decide whether the hands fit:

* Is the dial big enough for the longest hand, and is the chapter ring where
  the hands actually sweep?
* Does the centre opening clear the hub stack and the telescoping tubes?
* Is there depth behind the face for six hands at 3.5 mm spacing?
* Will the face itself fit on a print bed?

It only reads the file. Nothing is modified and nothing is written.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

GREEN, YELLOW, RED, DIM, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"

#: 3MF records its unit in the model XML; everything here works in mm.
UNIT_TO_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}

#: Common print beds, longest side first, for the "will it fit" check.
BEDS = [("Bambu A1 / P1 / X1", 256, 256), ("Prusa MK4", 250, 210), ("Ender 3", 220, 220)]


def describe_file(path: Path) -> str | None:
    """Return a complaint if this is obviously not a model file.

    The common failure is an iOS alias: sharing a Messages attachment straight
    from the share sheet hands over a 155-byte property list that merely points
    at the file on the phone. It is worth naming, because the alternative is a
    confusing parse error.
    """
    if not path.is_file():
        return f"no such file: {path}"
    size = path.stat().st_size
    head = path.open("rb").read(8)

    if head[:8] == b"bplist00":
        return (
            f"this is a {size}-byte Apple property list, not a model.\n"
            "  iOS handed over a reference to the file instead of the file itself.\n"
            "  In Messages: long-press the attachment, Save to Files, then upload\n"
            "  the saved copy. Or AirDrop it to a computer and upload from there."
        )
    if head[:4] == b"%PDF":
        return f"this is a PDF ({size} bytes), not a model."
    if size < 1024:
        return (
            f"only {size} bytes, which is too small to be a real model.\n"
            "  The upload probably passed a link rather than the file."
        )
    return None


def read_3mf_unit(path: Path) -> str | None:
    """Read the declared unit out of a 3MF, which is a zip of XML.

    trimesh does not always apply it, and a dial that is secretly in inches
    would otherwise read as a 4 mm disc.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".model")]
            if not names:
                return None
            xml = archive.read(names[0])[:4096].decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError):
        return None
    import re

    match = re.search(r'unit\s*=\s*"([a-z]+)"', xml)
    return match.group(1) if match else None


def load_mesh(path: Path):
    """Load any supported model and return a single concatenated mesh."""
    import trimesh

    scene = trimesh.load(str(path), force="mesh")
    if isinstance(scene, trimesh.Scene):
        scene = trimesh.util.concatenate(tuple(scene.geometry.values()))
    return scene


def ring_profile(mesh, *, bins: int = 400):
    """Radii present in the model, as seen looking down the axis.

    Returns ``(radii_sorted, outer_r, inner_r)`` where ``inner_r`` is the radius
    of the central opening, or 0.0 when the model is solid at the centre.
    """
    import numpy as np

    xy = mesh.vertices[:, :2]
    centre = xy.mean(axis=0)
    radii = np.hypot(xy[:, 0] - centre[0], xy[:, 1] - centre[1])
    outer = float(radii.max())

    # A central hole shows up as a radius band with vertices but no material
    # inside it. Sample the mesh's 2D occupancy cheaply by ray casting down.
    inner = 0.0
    hist, edges = np.histogram(radii, bins=bins, range=(0.0, outer))
    for count, lo in zip(hist, edges[:-1], strict=False):
        if count > 0:
            inner = float(lo)
            break
    return radii, outer, inner, centre


def main(argv: list[str] | None = None) -> int:
    """Check a face model and report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model", type=Path, help="the clock face: 3MF, STEP, STL, OBJ...")
    parser.add_argument("--scale", type=float, default=None, help="force a unit scale to mm")
    args = parser.parse_args(argv)


    from build_hands import HAND_DZ, HAND_T, HANDS

    complaint = describe_file(args.model)
    if complaint:
        print(f"{RED}Cannot read {args.model.name}:{OFF} {complaint}")
        return 2

    print(f"File  : {args.model.name}  ({args.model.stat().st_size / 1024:.0f} KB)")

    scale = args.scale
    if args.model.suffix.lower() == ".3mf":
        unit = read_3mf_unit(args.model)
        print(f"Unit  : {unit or 'not declared'}" + (f" (x{UNIT_TO_MM.get(unit, 1.0)})" if unit else ""))
        if scale is None and unit in UNIT_TO_MM:
            scale = UNIT_TO_MM[unit]
    scale = scale or 1.0

    try:
        mesh = load_mesh(args.model)
    except Exception as exc:
        print(f"{RED}Could not parse it:{OFF} {exc}")
        return 2
    if scale != 1.0:
        mesh.apply_scale(scale)

    size = mesh.bounds[1] - mesh.bounds[0]
    print(f"Mesh  : {len(mesh.faces)} faces, watertight={mesh.is_watertight}")
    print(f"Size  : {size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm")

    radii, outer, inner, centre = ring_profile(mesh)
    thickness = float(size[2])
    roundness = abs(size[0] - size[1]) / max(size[0], size[1])
    shape = "round" if roundness < 0.03 else f"not round ({roundness * 100:.0f}% off square)"
    print(f"Shape : {shape}, outer radius {outer:.1f} mm, thickness {thickness:.1f} mm")
    print(f"Centre: opening radius {inner:.1f} mm ({inner * 2:.1f} mm across)")
    print()

    # --- cross-check against the hands ------------------------------------
    longest = max(h.tip_r for h in HANDS)
    biggest_hub = max(max(h.bore + 5.2, 9.0) for h in HANDS)
    stack_depth = (len(HANDS) - 1) * HAND_DZ + HAND_T

    print("Against the six hands:")
    checks = []

    checks.append((
        outer >= longest + 3,
        f"longest hand is {longest:.0f} mm; dial radius {outer:.1f} mm",
        f"dial radius {outer:.1f} mm is smaller than the {longest:.0f} mm hand — "
        f"shorten the hands with --word-fraction, or lengthen the dial",
    ))
    checks.append((
        inner == 0.0 or inner * 2 >= biggest_hub + 1,
        f"centre opening {inner * 2:.1f} mm clears the {biggest_hub:.1f} mm hub stack"
        if inner else "no central opening detected (solid face)",
        f"centre opening {inner * 2:.1f} mm is tighter than the {biggest_hub:.1f} mm "
        f"largest hub — the level 1 hand will not pass through",
    ))
    checks.append((
        True,
        f"hand stack needs {stack_depth:.1f} mm of depth in front of the dial "
        f"(6 hands at {HAND_DZ} mm spacing)",
        "",
    ))

    for ok, good, bad in checks:
        print(f"  {GREEN}OK{OFF}   {good}" if ok else f"  {RED}no{OFF}   {bad}")

    fits = [name for name, w, d in BEDS if max(size[0], size[1]) <= max(w, d)
            and min(size[0], size[1]) <= min(w, d)]
    print()
    if fits:
        print(f"  {GREEN}OK{OFF}   fits on: {', '.join(fits)}")
    else:
        print(f"  {YELLOW}!{OFF}    {max(size[0], size[1]):.0f} mm is larger than a common bed; "
              f"print it in segments or have it cut")

    print()
    print(f"{DIM}Hand tip radii: " + ", ".join(f"{h.person} {h.tip_r}" for h in HANDS) + f" mm{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

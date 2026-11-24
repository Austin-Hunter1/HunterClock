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
import math
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


def flat_regions(mesh, *, z: float, upward: bool, tol: float = 0.01):
    """The 2D shape of the material at one horizontal face of the model.

    Collects the triangles lying in the plane ``z`` whose normal points the
    given way and unions them, which gives the real outline and the real holes
    -- unlike anything derived from a bounding box or a vertex centroid, which
    is meaningless on a part that is not centred on its own bounds.
    """
    import numpy as np
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    vertices, faces, normals = mesh.vertices, mesh.faces, mesh.face_normals
    centres = vertices[faces].mean(axis=1)[:, 2]
    chosen = np.abs(centres - z) < tol
    chosen &= (normals[:, 2] > 0.9) if upward else (normals[:, 2] < -0.9)
    triangles = [Polygon(vertices[tri][:, :2]) for tri in faces[chosen]]
    triangles = [t for t in triangles if t.is_valid and t.area > 1e-9]
    if not triangles:
        return None
    merged = unary_union(triangles)
    return max(getattr(merged, "geoms", [merged]), key=lambda g: g.area)


def circle_of(ring):
    """Centre, diameter and circularity of a ring, for identifying bores."""
    import numpy as np
    from shapely.geometry import Polygon

    polygon = Polygon(ring)
    centre = polygon.centroid
    points = np.asarray(ring.coords)[:, :2]
    radii = np.hypot(points[:, 0] - centre.x, points[:, 1] - centre.y)
    circularity = 4 * np.pi * polygon.area / max(polygon.length ** 2, 1e-9)
    return (float(centre.x), float(centre.y)), float(2 * radii.mean()), float(circularity)


def find_dial(mesh):
    """Locate the dial: its centre, its radius, and the holes through it.

    Looks for the largest circular flat region (a chapter ring is usually a
    raised or recessed disc). Falls back to the plate outline when the face is
    a plain sheet with nothing but holes in it.
    """

    z_levels = sorted({round(float(z), 3) for z in mesh.vertices[:, 2]})
    bottom = flat_regions(mesh, z=z_levels[0], upward=False)

    best = None
    for z in z_levels[1:]:
        region = flat_regions(mesh, z=z, upward=True)
        if region is None:
            continue
        _c, diameter, circularity = circle_of(region.exterior)
        if circularity > 0.95 and (best is None or diameter > best[1]):
            centre = region.centroid
            best = ((float(centre.x), float(centre.y)), diameter, z)

    holes = []
    if bottom is not None:
        for ring in bottom.interiors:
            centre, diameter, circularity = circle_of(ring)
            holes.append({"centre": centre, "d": diameter, "round": circularity > 0.95})

    if best is None and holes:
        # No disc: take the plate's own centre of area as the dial centre.
        centre = bottom.centroid
        best = ((float(centre.x), float(centre.y)), 0.0, z_levels[-1])

    return bottom, best, holes, z_levels


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
        factor = f" (x{UNIT_TO_MM.get(unit, 1.0)})" if unit else ""
        print(f"Unit  : {unit or 'not declared'}{factor}")
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

    bottom, disc, holes, z_levels = find_dial(mesh)
    thickness = float(size[2])
    print(f"Levels: {', '.join(f'{z:.3f}' for z in z_levels)} mm "
          f"({len(z_levels) - 1} step{'s' if len(z_levels) > 2 else ''})")

    if disc is None:
        print(f"{RED}Could not find a dial in this model.{OFF}")
        return 2
    (cx, cy), disc_d, _disc_z = disc
    disc_r = disc_d / 2.0
    if disc_r:
        print(f"Dial  : disc radius {disc_r:.1f} mm, centred at ({cx:.1f}, {cy:.1f})")
    else:
        print(f"Dial  : no raised disc; using plate centre ({cx:.1f}, {cy:.1f})")

    print(f"Holes : {len(holes)} through the plate")
    centre_hole = None
    for hole in sorted(holes, key=lambda h: math.hypot(h["centre"][0] - cx, h["centre"][1] - cy)):
        hx, hy = hole["centre"]
        offset = math.hypot(hx - cx, hy - cy)
        kind = "round" if hole["round"] else "shaped (decorative?)"
        label = ""
        if hole["round"] and offset < 1.0 and centre_hole is None:
            centre_hole = hole
            label = "  <- dial centre"
        print(f"        d={hole['d']:7.2f} mm at ({hx:7.1f},{hy:7.1f}), "
              f"{offset:6.1f} mm from centre, {kind}{label}")
    print()

    # --- cross-check against the hands ------------------------------------
    longest = max(h.tip_r for h in HANDS)
    shaft_od = 12.0  # motor 1's hollow shaft: the widest thing through the dial
    stack_depth = (len(HANDS) - 1) * HAND_DZ + HAND_T
    print("Against the six hands:")

    if disc_r:
        if outer_ok := disc_r >= longest + 3:
            print(f"  {GREEN}OK{OFF}   longest hand {longest:.0f} mm fits the "
                  f"{disc_r:.0f} mm dial, {disc_r - longest:.0f} mm to spare")
        else:
            print(f"  {RED}no{OFF}   longest hand {longest:.0f} mm against a "
                  f"{disc_r:.0f} mm dial")
        if outer_ok and disc_r - longest > 15:
            print(f"  {YELLOW}!{OFF}    but {disc_r - longest:.0f} mm of dial is left bare; "
                  f"hands could run to about {disc_r - 8:.0f} mm")

    if centre_hole is None:
        print(f"  {YELLOW}!{OFF}    no round hole at the dial centre -- "
              f"the arbor hole may still need drilling")
    elif centre_hole["d"] >= shaft_od + 0.5:
        print(f"  {GREEN}OK{OFF}   centre hole {centre_hole['d']:.2f} mm clears the "
              f"{shaft_od:.0f} mm shaft")
    else:
        print(f"  {RED}no{OFF}   centre hole is {centre_hole['d']:.2f} mm; the "
              f"{shaft_od:.0f} mm motor shaft will not pass. "
              f"Open it to at least {shaft_od + 0.7:.1f} mm")

    swept = [h for h in holes if h["round"] and h is not centre_hole
             and math.hypot(h["centre"][0] - cx, h["centre"][1] - cy) < longest]
    if swept:
        where = ", ".join(f"{h['d']:.1f} mm" for h in swept)
        print(f"  {YELLOW}!{OFF}    {len(swept)} other hole(s) ({where}) lie under the "
              f"hand sweep and will be visible")

    print(f"  {DIM}hand stack needs {stack_depth:.1f} mm of depth in front of the "
          f"{thickness:.2f} mm plate{OFF}")
    print()

    fits = [name for name, w, d in BEDS if max(size[0], size[1]) <= max(w, d)
            and min(size[0], size[1]) <= min(w, d)]
    print()
    if fits:
        print(f"  {GREEN}OK{OFF}   fits on: {', '.join(fits)}")
    else:
        print(f"  {YELLOW}!{OFF}    {max(size[0], size[1]):.0f} mm is larger than a common bed; "
              f"print it in segments or have it cut")

    print()
    tips = ", ".join(f"{h.person} {h.tip_r}" for h in HANDS)
    print(f"{DIM}Hand tip radii: {tips} mm{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

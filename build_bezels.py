"""Build the six display bezels: rings that frame a round screen against the dial.

    python build_bezels.py                 # one of each, plus a print plate
    python build_bezels.py --measure 40.2  # after measuring your actual module

``docs/hardware-plan.html`` budgets a "printed bezel / diffuser ring" per
screen, to be brass-finished with spray or a gilt pen so it matches the
cabinet's existing hardware. This builds it.

The screen is an AITRIP GC9A01, 1.28 in, 240x240. The bezel is a ring with a
rebate in the back: the module drops in from behind and seats against a
shoulder, so the front face hides the PCB edge and leaves the round glass
showing.

**Measure your module before printing six.** The defaults come from
``build_cad.py`` ("as ordered"), not from a part in hand, and GC9A01 carriers
differ between makers -- ``docs/circuit-diagram.html`` already warns that even
the header order varies. Print one, check it, then print the rest.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent
OUT_DIR = REPO / "cad_out" / "bezels"


@dataclass
class Bezel:
    """Every dimension of the bezel, in millimetres.

    Attributes:
        module_od: Diameter of the display module's PCB.
        module_t: Thickness of the module, board plus anything on the front.
        active_d: Diameter of the lit area. The aperture must not cover it.
        rim: Width of the visible ring around the module.
        front_t: Thickness of the front face the glass sits behind.
        fit: Diametral clearance in the rebate, so the module drops in.
        aperture_margin: How much wider than the lit area the hole is.
        header_notch: Width of the slot for the pin header, 0 for none.
        screw_d: Pilot hole for the mounting screws, 0 for none. The holes are
            blind and open to the *back*, so the brass-finished front face has
            no fastener showing on it. The module ends up sandwiched between
            the bezel and whatever it screws to.
        screw_depth: How deep those blind holes go.
        chamfer: Chamfer on the front outer edge, plain style only.
        style: Which moulding the front carries. See :func:`profile`.
        relief: How far the moulding stands above the plain web, mm.
    """

    module_od: float = 39.5
    module_t: float = 3.0
    active_d: float = 32.4
    rim: float = 4.0
    front_t: float = 1.6
    fit: float = 0.4
    aperture_margin: float = 0.6
    header_notch: float = 16.0
    screw_d: float = 1.7
    screw_depth: float = 3.0
    chamfer: float = 0.6
    style: str = "stepped"
    relief: float = 1.4

    @property
    def outer_d(self) -> float:
        """Outside diameter of the finished ring."""
        return self.module_od + 2 * self.rim

    @property
    def aperture_d(self) -> float:
        """The hole you see the screen through."""
        return self.active_d + self.aperture_margin

    @property
    def rebate_d(self) -> float:
        """Diameter of the pocket the module sits in."""
        return self.module_od + self.fit

    @property
    def rebate_depth(self) -> float:
        """Depth of that pocket."""
        return self.module_t + 0.3

    @property
    def web_t(self) -> float:
        """Thickness up to the plain front face, before any moulding."""
        return self.front_t + self.rebate_depth

    @property
    def total_t(self) -> float:
        """Overall thickness including the moulding."""
        return self.web_t + (0.0 if self.style == "plain" else self.relief)

    @property
    def shoulder(self) -> float:
        """Radial width of the lip the module seats against."""
        return (self.rebate_d - self.aperture_d) / 2.0

    @property
    def screw_circle(self) -> float:
        """Diameter of the circle the mounting screws sit on."""
        return self.module_od + self.rim

    def check(self) -> list[str]:
        """Return the reasons this bezel would not work, if any."""
        problems = []
        if self.aperture_d >= self.module_od:
            problems.append("the aperture is wider than the module: nothing would hold it")
        if self.shoulder < 1.0:
            problems.append(
                f"the seating lip is only {self.shoulder:.2f} mm wide; "
                f"the module would fall through. Reduce --aperture-margin or check active_d"
            )
        if self.rim < 2.0:
            problems.append(f"a {self.rim:.1f} mm rim is too thin to print and paint")
        if self.screw_d and self.screw_circle / 2 + self.screw_d / 2 + 1.0 > self.outer_d / 2:
            problems.append("the screw holes break out of the rim; widen --rim")
        if self.screw_d and self.screw_depth > self.web_t - 1.0:
            problems.append("the blind screw holes would break through the front face")
        if self.style not in STYLES:
            problems.append(f"unknown style {self.style!r}; choose from {', '.join(STYLES)}")
        if self.front_t < 0.8:
            problems.append("the front face is thinner than two layers")
        return problems


#: The mouldings on offer. All of them build *upward* from the plain web, so
#: the material under the glass is never thinned -- carving down into a 1.6 mm
#: web would leave 0.2 mm, which is two layers and a light leak.
STYLES = ("plain", "stepped", "ogee", "reeded", "ornate")

#: `ornate` imitates a Louis XV gilt picture frame: a dominant convex round,
#: a cove behind it, a small outer bead, and rosettes at the quarters. What it
#: deliberately does not attempt is the acanthus scrollwork such frames carry
#: between the rosettes -- on a 1.28 in screen that ornament would be a few
#: tenths of a millimetre, well under what a 0.4 mm nozzle resolves, and it
#: would print as mush. The repeating element here is a run of plain lobes,
#: which is coarse enough to survive printing and painting.
ORNATE_RIM = 7.0
ORNATE_RELIEF = 2.6


def _ornate_section(relief: float) -> list[tuple[float, float]]:
    """The gilt-frame section, as (fraction across the rim, height)."""
    section: list[tuple[float, float]] = [(0.10, 0.0), (0.15, 0.45), (0.21, 0.6)]
    # The dominant convex round, a half-ellipse from 0.21 to 0.70.
    start, end, low, high = 0.21, 0.70, 0.6, relief
    steps = 12
    for index in range(1, steps + 1):
        fraction = start + (end - start) * index / steps
        angle = math.pi * (fraction - start) / (end - start)
        section.append((fraction, low + (high - low) * math.sin(angle) ** 0.75))
    section += [
        (0.78, 0.55),   # cove
        (0.84, 0.5),
        (0.91, 1.45),   # outer bead
        (0.97, 0.9),
        (1.00, 0.55),
    ]
    return section


def profile(bezel: Bezel) -> list[tuple[float, float]]:
    """The cross-section to revolve, as (radius, height) from the back face.

    A clock bezel reads as turned brass because its section steps *down* toward
    the glass, the way a picture frame does. Every style here does that; they
    differ in how the step is shaped.
    """
    r_in = bezel.aperture_d / 2
    r_out = bezel.outer_d / 2
    web = bezel.web_t
    width = r_out - r_in
    points: list[tuple[float, float]] = [(r_in, 0.0), (r_in, web)]

    if bezel.style == "plain":
        points.append((r_out, web))
    elif bezel.style == "ornate":
        # fraction across the rim -> height above the web.
        # A gilt frame's section: narrow flat land at the sight edge, a big
        # convex round, a cove behind it, then a small bead at the back edge.
        for fraction, height in _ornate_section(bezel.relief):
            points.append((r_in + width * fraction, web + height))
    elif bezel.style in ("stepped", "reeded"):
        # Two concentric steps up to the outer land.
        half = bezel.relief / 2
        points += [
            (r_in + width * 0.30, web),
            (r_in + width * 0.30, web + half),
            (r_in + width * 0.60, web + half),
            (r_in + width * 0.60, web + bezel.relief),
            (r_out, web + bezel.relief),
        ]
    elif bezel.style == "ogee":
        # A smooth S-curve, the classic cyma recta of a moulding.
        # Start at 1: index 0 would repeat the (r_in, web) point already in
        # the list, and a zero-length edge makes the revolve fail outright.
        steps = 16
        for index in range(1, steps + 1):
            fraction = index / steps
            radius = r_in + width * fraction
            height = web + bezel.relief * (0.5 - 0.5 * math.cos(math.pi * fraction))
            points.append((radius, height))
    else:
        raise RuntimeError(f"unknown style {bezel.style!r}; choose from {', '.join(STYLES)}")

    points.append((r_out, 0.0))
    return points


def build(bezel: Bezel):
    """Build the bezel as a CadQuery solid."""
    import cadquery as cq

    # Revolve the section. Everything else is cut from the result.
    body = (
        cq.Workplane("XZ")
        .polyline(profile(bezel))
        .close()
        # The axis is given in the workplane's LOCAL frame. On an XZ
        # workplane the local y axis is global Z, which is the one to spin
        # about; passing (0,0,1) spins about global Y and yields a ring lying
        # on its side.
        .revolve(360, (0, 0, 0), (0, 1, 0))
    )

    top = bezel.total_t

    # Reeding: radial grooves cut into the outer land.
    if bezel.style == "reeded":
        inner = (bezel.outer_d / 2) - bezel.rim * 0.56
        outer = bezel.outer_d / 2 + 1.0
        count = 48
        groove = max((2 * math.pi * inner / count) * 0.45, 0.5)
        for index in range(count):
            angle = 360.0 * index / count
            cutter = (
                cq.Workplane("XY")
                .center((inner + outer) / 2, 0)
                .rect(outer - inner, groove)
                .extrude(0.55)
                .translate((0, 0, top - 0.55))
                .rotate((0, 0, 0), (0, 0, 1), angle)
            )
            body = body.cut(cutter)

    if bezel.style == "ornate":
        body = _cut_all(body, _ornament_cuts(bezel))

    # The pocket the module drops into, cut from the back (z = 0 is the back).
    body = body.cut(
        cq.Workplane("XY").circle(bezel.rebate_d / 2).extrude(bezel.rebate_depth)
    )

    # A slot through the rim so the pin header and its wires can pass. Without
    # it the module cannot sit flat, and on these carriers the header is
    # soldered to the edge of the board.
    if bezel.header_notch > 0:
        notch = (
            cq.Workplane("XY")
            .center(0, -bezel.outer_d / 4)
            .rect(bezel.header_notch, bezel.outer_d / 2)
            .extrude(bezel.rebate_depth)
        )
        body = body.cut(notch)

    # Blind mounting holes at 45 degrees, open to the back only, so nothing
    # shows on the finished front. 45 degrees keeps them clear of the notch.
    if bezel.screw_d > 0:
        radius = bezel.screw_circle / 2
        for angle in (45, 135, 225, 315):
            x = radius * math.cos(math.radians(angle))
            y = radius * math.sin(math.radians(angle))
            body = body.cut(
                cq.Workplane("XY")
                .center(x, y)
                .circle(bezel.screw_d / 2)
                .extrude(min(bezel.screw_depth, bezel.web_t - 1.0))
            )

    # Chamfer the plain style's hard outer edge. The mouldings shape their own.
    if bezel.style == "plain" and bezel.chamfer > 0:
        # A chamfer that will not apply is cosmetic, so never fail the build for it.
        with contextlib.suppress(Exception):
            body = body.faces(">Z").edges(
                cq.selectors.RadiusNthSelector(-1)
            ).chamfer(bezel.chamfer)
    return body


#: How many lobes run round the outer edge, and how deep the valleys bite.
ORNATE_LOBES = 24
ORNATE_LOBE_DEPTH = 1.3
#: Half-width, in degrees, of the plain cabochon left at each quarter, and how
#: far it stands proud of the surrounding lobes.
ORNATE_CARTOUCHE = 11.0
ORNATE_CARTOUCHE_RISE = 0.7


def _cartouche(theta: float) -> float:
    """How much of a quarter cabochon this angle falls in, 0 to 1."""
    quarter = math.pi / 2
    gap = abs(theta % quarter)
    gap = min(gap, quarter - gap)
    return math.exp(-((math.degrees(gap) / ORNATE_CARTOUCHE) ** 2))


def _lobe_radius(theta: float, peak: float) -> float:
    """Outer radius at this angle: scalloped, except at the four cartouches.

    The cabochons reach the full outer diameter and the lobes sit back from
    it, so the quarters read as bosses without the ring growing: 53.5 mm is
    already close to the 56 mm screen panel it has to sit on.
    """
    wave = (0.5 + 0.5 * math.cos(ORNATE_LOBES * theta)) ** 1.6
    block = _cartouche(theta)
    base = peak - ORNATE_CARTOUCHE_RISE
    return base - ORNATE_LOBE_DEPTH * wave * (1.0 - block) + ORNATE_CARTOUCHE_RISE * block


def _ornament_cuts(bezel: Bezel):
    """Scallop the outline and flute the round, leaving blocks at the quarters.

    The first attempt added the ornament as fused domes. Geometrically that was
    fine -- one solid, correct volume -- but every sphere contributed exactly
    one cracked edge where its seam met the fuse boundary, 56 of them, and the
    exported mesh was unprintable.

    Carving avoids that, and it is how the ornament is really made: a gadrooned
    moulding is a round with flutes cut across it, and the cartouches at the
    quarters are where the carver left the round alone. Cuts tessellate
    cleanly, which the reeded style already demonstrated.

    Two cuts do the work. The lobed outline matters most: a carved frame is
    never a plain circle, and the undulating edge is what the eye reads first,
    from across a room, long before any surface detail. It is also pure bulk
    geometry, so unlike scrollwork it survives a 0.4 mm nozzle intact.
    """
    import cadquery as cq

    peak = bezel.outer_d / 2
    top = bezel.total_t
    width = peak - bezel.aperture_d / 2
    inner = bezel.aperture_d / 2

    # 1. The scalloped outline, cut through the whole ring as a lobed hole in
    #    an oversized disc.
    samples = 16 * ORNATE_LOBES
    edge = []
    for index in range(samples):
        theta = 2 * math.pi * index / samples
        radius = _lobe_radius(theta, peak)
        edge.append((radius * math.cos(theta), radius * math.sin(theta)))
    scallop = (
        cq.Workplane("XY")
        .circle(peak + 10.0)
        .polyline(edge)
        .close()
        .extrude(top + 2.0)
        .translate((0, 0, -1.0))
        .val()
    )

    # 2. Flutes across the round, one per lobe valley so edge and surface
    #    agree. A cylinder laid along the radius bites deepest at the crown
    #    and fades out toward both edges, which is how a gadroon is cut.
    flute_r = 1.1
    from_r = inner + width * 0.17
    to_r = peak + 1.0  # out past the scallop valley, so lobe and flute are one
    axis_z = top + flute_r - 1.1
    cutters = [scallop]
    for index in range(ORNATE_LOBES):
        degrees = 360.0 * index / ORNATE_LOBES
        if _cartouche(math.radians(degrees)) > 0.25:
            continue  # this one falls on a cabochon; leave the round alone
        cutters.append(
            cq.Workplane("YZ")
            .circle(flute_r)
            .extrude(to_r - from_r)
            .translate((from_r, 0, axis_z))
            .rotate((0, 0, 0), (0, 0, 1), degrees)
            .val()
        )

    return cutters


def _cut_all(body, cutters):
    """Remove many small solids in one boolean."""
    import cadquery as cq

    if not cutters:
        return body
    cut = body.val().cut(*cutters)
    with contextlib.suppress(Exception):
        cut = cut.clean()
    return cq.Workplane(obj=cut)


def describe(bezel: Bezel) -> str:
    """A readable summary of the geometry."""
    return (
        f"  outer diameter   {bezel.outer_d:6.2f} mm\n"
        f"  aperture         {bezel.aperture_d:6.2f} mm  (lit area is {bezel.active_d:.2f})\n"
        f"  module pocket    {bezel.rebate_d:6.2f} mm x {bezel.rebate_depth:.2f} deep\n"
        f"  seating lip      {bezel.shoulder:6.2f} mm wide\n"
        f"  total thickness  {bezel.total_t:6.2f} mm"
        f"  ({bezel.web_t:.2f} web + {bezel.total_t - bezel.web_t:.2f} moulding)\n"
        f"  style            {bezel.style}\n"
        f"  screw circle     {bezel.screw_circle:6.2f} mm, 4 x {bezel.screw_d:.1f} mm blind "
        f"holes {bezel.screw_depth:.1f} deep, from the back\n"
        f"  header notch     {bezel.header_notch:6.1f} mm wide"
    )


def main(argv: list[str] | None = None) -> int:
    """Build the bezels and write the files."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--measure", type=float, default=None, metavar="OD",
                        help="your measured module diameter in mm, which is the one "
                             "dimension worth getting from a real part")
    parser.add_argument("--module-t", type=float, default=None, metavar="MM")
    parser.add_argument("--active-d", type=float, default=None, metavar="MM")
    parser.add_argument("--rim", type=float, default=None, metavar="MM")
    parser.add_argument("--aperture-margin", type=float, default=None, metavar="MM")
    parser.add_argument("--style", choices=STYLES, default="stepped",
                        help="which moulding the front carries")
    parser.add_argument("--relief", type=float, default=None, metavar="MM",
                        help="how far the moulding stands proud, mm")
    parser.add_argument("--no-notch", action="store_true")
    parser.add_argument("--no-screws", action="store_true")
    parser.add_argument("--count", type=int, default=6, help="how many on the plate")
    parser.add_argument("--bed", type=float, nargs=2, default=(248.0, 208.0), metavar=("X", "Y"))
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    bezel = Bezel()
    if args.measure is not None:
        bezel.module_od = args.measure
    if args.module_t is not None:
        bezel.module_t = args.module_t
    if args.active_d is not None:
        bezel.active_d = args.active_d
    if args.rim is not None:
        bezel.rim = args.rim
    if args.aperture_margin is not None:
        bezel.aperture_margin = args.aperture_margin
    bezel.style = args.style
    if args.style == "ornate":
        # Ornament needs room: a 4 mm rim leaves nothing for a rosette to sit on.
        if args.rim is None:
            bezel.rim = ORNATE_RIM
        if args.relief is None:
            bezel.relief = ORNATE_RELIEF
    if args.relief is not None:
        bezel.relief = args.relief
    if args.no_notch:
        bezel.header_notch = 0.0
    if args.no_screws:
        bezel.screw_d = 0.0

    print("Display bezel")
    print(describe(bezel))
    problems = bezel.check()
    if problems:
        print()
        for problem in problems:
            print(f"  REFUSING: {problem}", file=sys.stderr)
        return 1

    import cadquery as cq

    args.out.mkdir(parents=True, exist_ok=True)
    solid = build(bezel)
    single = args.out / f"bezel-{bezel.style}.stl"
    cq.exporters.export(solid, str(single), tolerance=0.01, angularTolerance=0.1)
    cq.exporters.export(solid, str(args.out / f"bezel-{bezel.style}.step"))
    print(f"\n  wrote {single.name}")

    # A plate of however many are wanted, packed into rows on the bed.
    pitch = bezel.outer_d + 4.0
    per_row = max(1, int((args.bed[0] - 4.0) // pitch))
    rows = -(-args.count // per_row)
    if rows * pitch + 4.0 > args.bed[1]:
        print(f"  {args.count} bezels will not fit a {args.bed[0]:.0f} x {args.bed[1]:.0f} mm bed",
              file=sys.stderr)
        return 1
    placed = []
    for index in range(args.count):
        column, row = index % per_row, index // per_row
        placed.append(solid.translate((4 + pitch * (column + 0.5), 4 + pitch * (row + 0.5), 0)))
    compound = cq.Compound.makeCompound([s.val() for s in placed])
    plate = args.out / f"bezel-plate-{bezel.style}-x{args.count}.stl"
    cq.exporters.export(compound, str(plate), tolerance=0.01, angularTolerance=0.1)
    cq.exporters.export(compound, str(args.out / f"bezel-plate-{bezel.style}-x{args.count}.3mf"))
    # Solid bodies for Fusion or other CAD, where the STL and 3MF are only meshes.
    cq.exporters.export(compound, str(args.out / f"bezel-plate-{bezel.style}-x{args.count}.step"))

    verify(plate, expect=args.count, bed=tuple(args.bed))
    # Report the mesh's real footprint, not the grid it was laid out on: with a
    # part-count below one full row the grid is much wider than the parts.
    import trimesh

    low, high = trimesh.load(str(plate)).bounds
    print(f"  wrote {plate.name}: {args.count} bezels, "
          f"{high[0] - low[0]:.0f} x {high[1] - low[1]:.0f} mm, every part watertight")
    return 0


def verify(path: Path, *, expect: int, bed) -> None:
    """Load the written mesh back and refuse anything a slicer would fight.

    Same discipline as the hands: the defect that mattered there existed only
    in the exported file, and every in-memory check passed while it was there.
    """
    import trimesh

    mesh = trimesh.load(str(path))
    parts = mesh.split(only_watertight=False)
    problems = []
    if len(parts) != expect:
        problems.append(f"{len(parts)} bodies, expected {expect}")
    for index, part in enumerate(parts, start=1):
        if not part.is_watertight:
            problems.append(f"bezel {index} is not watertight")
    low, high = mesh.bounds
    if abs(low[2]) > 1e-6:
        problems.append(f"does not sit on the bed (z starts at {low[2]:.3f})")
    if high[0] - low[0] > bed[0] or high[1] - low[1] > bed[1]:
        problems.append("footprint exceeds the bed")
    if problems:
        raise RuntimeError(f"{path.name} would not print cleanly: " + "; ".join(problems))


if __name__ == "__main__":
    raise SystemExit(main())

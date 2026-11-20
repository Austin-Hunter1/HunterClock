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
        chamfer: Chamfer on the front outer edge.
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
    def total_t(self) -> float:
        """Overall thickness of the bezel."""
        return self.front_t + self.rebate_depth

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
        if self.screw_d and self.screw_depth > self.total_t - 1.0:
            problems.append("the blind screw holes would break through the front face")
        if self.front_t < 0.8:
            problems.append("the front face is thinner than two layers")
        return problems


def build(bezel: Bezel):
    """Build the bezel as a CadQuery solid."""
    import cadquery as cq

    # The ring, aperture straight through.
    body = (
        cq.Workplane("XY")
        .circle(bezel.outer_d / 2)
        .circle(bezel.aperture_d / 2)
        .extrude(bezel.total_t)
    )

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
                .extrude(min(bezel.screw_depth, bezel.total_t - 1.0))
            )

    # Chamfer the front outer edge so it catches the light like turned brass.
    if bezel.chamfer > 0:
        # A chamfer that will not apply is cosmetic, so never fail the build for it.
        with contextlib.suppress(Exception):
            body = body.faces(">Z").edges(
                cq.selectors.RadiusNthSelector(-1)
            ).chamfer(bezel.chamfer)
    return body


def describe(bezel: Bezel) -> str:
    """A readable summary of the geometry."""
    return (
        f"  outer diameter   {bezel.outer_d:6.2f} mm\n"
        f"  aperture         {bezel.aperture_d:6.2f} mm  (lit area is {bezel.active_d:.2f})\n"
        f"  module pocket    {bezel.rebate_d:6.2f} mm x {bezel.rebate_depth:.2f} deep\n"
        f"  seating lip      {bezel.shoulder:6.2f} mm wide\n"
        f"  total thickness  {bezel.total_t:6.2f} mm\n"
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
    single = args.out / "bezel.stl"
    cq.exporters.export(solid, str(single), tolerance=0.01, angularTolerance=0.1)
    cq.exporters.export(solid, str(args.out / "bezel.step"))
    print(f"\n  wrote {single.name} and bezel.step")

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
    plate = args.out / f"bezel-plate-x{args.count}.stl"
    cq.exporters.export(compound, str(plate), tolerance=0.01, angularTolerance=0.1)
    cq.exporters.export(compound, str(args.out / f"bezel-plate-x{args.count}.3mf"))

    verify(plate, expect=args.count, bed=tuple(args.bed))
    width = per_row * pitch + 4
    height = rows * pitch + 4
    print(f"  wrote {plate.name}: {args.count} bezels, {width:.0f} x {height:.0f} mm, "
          f"every part watertight")
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

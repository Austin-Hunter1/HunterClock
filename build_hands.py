"""Build the six ambigram clock hands as CAD files.

    python build_hands.py                 # all six, every format
    python build_hands.py --only austin   # one name, while iterating
    python build_hands.py --svg-only      # trace and lay out, no 3D

Austin drew a rotational ambigram for each family member: a word that reads the
same when turned 180 degrees.  On a clock hand that sweeps the full circle, that
is exactly the right property -- the name reads correctly whichever way the hand
points, instead of being upside down for half of every revolution.

Two measured facts from ``tools/analyse_ambigrams.py`` drive everything here:

1. The artwork is **pixel-exact** under a 180 degree point reflection, not just
   legible upside down.  So the word's own centre of symmetry has to land on the
   hand's long axis, or that property is thrown away.  :func:`check_symmetry`
   asserts it survives the whole pipeline.

2. The glyphs are **3 to 10 separate pieces** each.  Extruding the traced glyph
   on its own would produce nine loose bits of plastic rather than a hand, so
   every design here is about what holds the pieces together.

Outputs land in ``cad_out/hands/`` (gitignored) as STEP, STL, SVG and DXF.
See ``docs/AMBIGRAM-HANDS.md`` for the design discussion.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from shapely import affinity
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import unary_union

REPO = Path(__file__).resolve().parent
ART_DIR = REPO / "cad" / "ambigrams"
OUT_DIR = REPO / "cad_out" / "hands"

# --- hardware, from build_cad.py and docs/circuit-diagram.html ---------------

#: Plate thickness of a hand, mm.  Matches build_cad.py's HAND_T.
HAND_T = 2.5

#: Vertical step between neighbouring hands, mm (build_cad.py HAND_DZ).  The
#: gap between one hand's front face and the next hand's back face is therefore
#: HAND_DZ - HAND_T = 1.0 mm, which is what caps the relief height.
HAND_DZ = 3.5

#: Height the raised name stands proud of the blade, mm.  Must stay under the
#: 1.0 mm inter-hand gap or hands foul each other as they cross.
RELIEF_H = 0.6

#: Clearance added to a bore so a printed hub slides onto its tube.
BORE_CLEARANCE = 0.15

#: Minimum printable feature, mm: two perimeters at a 0.4 mm nozzle.
MIN_FEATURE = 0.8

#: Wall thickness around a hub bore, mm.
HUB_WALL = 2.6

#: Extra offset beyond the bare merging distance, mm. The merging distance
#: alone produces tangent contacts, which are bridges of zero width; this is
#: what turns them into something that will actually hold.
HALO_BRIDGE = 0.45

#: Never go below this halo, mm, however well the word already hangs together.
HALO_MIN = 1.0

#: Smallest hub outer diameter, mm, so the small-bore levels still have
#: something to hold and somewhere to put a grub screw.
HUB_OD_MIN = 9.0


@dataclass
class HandSpec:
    """One hand: who it is for, what it mounts on, and how long it is.

    Attributes:
        level: Hub level 1-6.  Level 1 is the rearmost motor.
        person: The name in ``config/clock.example.yaml``.
        artwork: Filename in ``cad/ambigrams``.
        reads: What the ambigram actually says, which may differ from ``person``.
        mount_od: Outside diameter of the shaft or tube this hand grips, mm.
        tip_r: Distance from the axis to the hand's tip, mm (build_cad.py HAND_LEN).
        color: Hex colour, matching build_cad.py and the UI.
    """

    level: int
    person: str
    artwork: str
    reads: str
    mount_od: float
    tip_r: float
    color: str
    notes: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        """Lowercase person name, used for filenames."""
        return self.person.lower()

    @property
    def bore(self) -> float:
        """Hub bore diameter, mm."""
        return self.mount_od + BORE_CLEARANCE

    @property
    def hub_od(self) -> float:
        """Hub outer diameter, mm."""
        return max(self.bore + 2 * HUB_WALL, HUB_OD_MIN)

    @property
    def z(self) -> float:
        """Height of this hand's underside above hand 1's, mm."""
        return (self.level - 1) * HAND_DZ


# Mounting comes from build_cad.py: level 1 grips motor 1's 12 mm hollow shaft,
# levels 2-6 grip their telescoping brass tube.  Tip radii are HAND_LEN.
# Mom and Dad are Becky and Evan -- confirmed by Austin.
HANDS = [
    HandSpec(1, "Becky", "Mom_ambigram.png", "Mom", 12.00, 70, "#4F7A4A"),
    HandSpec(2, "Evan", "Dad_ambigram.png", "Dad", 7.14, 76, "#5B5EA6"),
    HandSpec(3, "Elijah", "Elijah_ambigram2.png", "Elijah", 6.35, 82, "#3E6470"),
    HandSpec(4, "Caleb", "Caleb_ambigram.png", "Caleb", 3.18, 88, "#A8672B"),
    HandSpec(5, "Austin", "Austin_ambigram.png", "Austin", 2.38, 94, "#8A4B6B"),
    HandSpec(6, "Micah", "Micah_ambigram.png", "Micah", 1.59, 100, "#B0562A"),
]


# --- tracing -----------------------------------------------------------------


def trace_artwork(path: Path, *, simplify_px: float = 0.4, min_area_px: float = 40.0):
    """Trace the ink in an ambigram into polygons.

    Args:
        path: The image file.
        simplify_px: Douglas-Peucker tolerance in pixels.  0.4 keeps the
            letterforms crisp while dropping the antialiasing stair-steps that
            would otherwise become thousands of CAD vertices.
        min_area_px: Drop pieces smaller than this; they are JPEG/antialias
            specks, not letters.

    Returns:
        A shapely geometry in millimetre-agnostic pixel units, y already
        flipped so that +y is up.

    Raises:
        RuntimeError: if the image cannot be read or holds no ink.
    """
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise RuntimeError(f"cannot read {path}")
    mask = (image < 128).astype(np.uint8)
    if not mask.any():
        raise RuntimeError(f"{path.name} has no ink below the threshold")

    # RETR_CCOMP gives a two-level hierarchy: outer boundaries and their holes,
    # which is exactly the structure of a letterform with counters.
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hierarchy is None:
        raise RuntimeError(f"{path.name} produced no contours")
    hierarchy = hierarchy[0]

    height = mask.shape[0]
    pieces = []
    for index, contour in enumerate(contours):
        if hierarchy[index][3] != -1:  # a hole; handled with its parent
            continue
        shell = _contour_to_ring(contour, height)
        if shell is None or abs(_signed_area(shell)) < min_area_px:
            continue
        holes = []
        child = hierarchy[index][2]
        while child != -1:
            ring = _contour_to_ring(contours[child], height)
            if ring is not None and abs(_signed_area(ring)) >= min_area_px:
                holes.append(ring)
            child = hierarchy[child][0]
        try:
            polygon = Polygon(shell, holes)
        except ValueError:
            continue
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty:
            continue
        pieces.append(polygon)

    if not pieces:
        raise RuntimeError(f"{path.name} traced to nothing")

    geometry = unary_union(pieces)
    if simplify_px > 0:
        geometry = geometry.simplify(simplify_px, preserve_topology=True)
    return _as_multipolygon(geometry)


def _contour_to_ring(contour, height: int) -> list[tuple[float, float]] | None:
    """Turn an OpenCV contour into a closed ring with +y upwards."""
    points = contour.reshape(-1, 2)
    if len(points) < 3:
        return None
    return [(float(x), float(height - y)) for x, y in points]


def _signed_area(ring: list[tuple[float, float]]) -> float:
    """Shoelace area of a ring."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _as_multipolygon(geometry) -> MultiPolygon:
    """Normalise any shapely result into a MultiPolygon."""
    if isinstance(geometry, Polygon):
        return MultiPolygon([geometry])
    if isinstance(geometry, MultiPolygon):
        return geometry
    parts = [g for g in getattr(geometry, "geoms", []) if isinstance(g, Polygon)]
    return MultiPolygon(parts)


# --- measurement -------------------------------------------------------------


def merge_offset(geometry: MultiPolygon, *, limit: float = 40.0) -> float:
    """Smallest outward offset that fuses every piece into one body.

    This is the number that decides whether the halo design works, and it is
    *not* the smallest gap between pieces: pieces chain together, so what
    matters is the last gap to close, not the first. Binary search on the
    buffer radius until the result is a single polygon.

    Returns:
        The offset in the geometry's own units, or ``limit`` if even that does
        not merge everything.
    """
    if len(geometry.geoms) <= 1:
        return 0.0
    low, high = 0.0, limit
    if _piece_count(geometry.buffer(high, quad_segs=8)) > 1:
        return limit
    for _ in range(22):
        mid = (low + high) / 2
        if _piece_count(geometry.buffer(mid, quad_segs=8)) == 1:
            high = mid
        else:
            low = mid
    return high


def _piece_count(geometry) -> int:
    """How many separate polygons a geometry holds."""
    if isinstance(geometry, Polygon):
        return 0 if geometry.is_empty else 1
    return len([g for g in getattr(geometry, "geoms", []) if not g.is_empty])


def min_stroke(geometry, *, probe: float = 0.05, limit: float = 6.0) -> float:
    """Approximate the thinnest stroke by eroding until the shape disappears.

    Binary search on a negative buffer: the largest erosion the shape survives
    is half the narrowest stroke.  Approximate, but it is the number that says
    whether a feature will print, and it needs no distance transform.
    """
    low, high = 0.0, limit
    for _ in range(24):
        mid = (low + high) / 2
        if mid < probe:
            break
        if geometry.buffer(-mid).is_empty:
            high = mid
        else:
            low = mid
    return 2 * low


def min_bridge(geometry, *, limit: float = 6.0) -> float:
    """Width of the narrowest neck holding the shape together, in mm.

    :func:`min_stroke` erodes until the shape vanishes, which says nothing
    about a thin bridge between two fat lobes -- erode a dumbbell and you get
    two blobs, not nothing. This instead finds the erosion at which the piece
    count first goes up, which is the bridge that would snap.

    Returns:
        Twice the largest erosion the shape stays connected through, or
        ``limit * 2`` if it never splits.
    """
    if _piece_count(geometry) != 1:
        return 0.0
    low, high = 0.0, limit
    if _piece_count(geometry.buffer(-high, quad_segs=8)) == 1:
        return limit * 2
    for _ in range(20):
        mid = (low + high) / 2
        eroded = geometry.buffer(-mid, quad_segs=8)
        if _piece_count(eroded) == 1:
            low = mid
        else:
            high = mid
    return 2 * low


def check_symmetry(geometry, centre: tuple[float, float], *, tolerance: float = 0.02) -> float:
    """Measure how well a geometry survives a 180 degree rotation about ``centre``.

    Returns:
        Intersection over union, 1.0 being a perfect point reflection.
    """
    rotated = affinity.rotate(geometry, 180.0, origin=centre)
    union = geometry.union(rotated).area
    if union <= 0:
        return 0.0
    return geometry.intersection(rotated).area / union


# --- hand construction -------------------------------------------------------


@dataclass
class Hand:
    """A finished hand, in millimetres, lying in the XY plane.

    The hub centre is the origin and the hand points along +x, so rotating the
    whole thing about the origin is the same thing the motor does.

    Attributes:
        spec: Which hand this is.
        blade: The solid plate, hub included, with the bore already cut.
        word: The name, as the shapes that get raised on the blade.
        style: ``"plaque"`` or ``"halo"``.
        scale: Millimetres per artwork pixel.
        report: Measurements for the build log.
    """

    spec: HandSpec
    blade: MultiPolygon
    word: MultiPolygon
    style: str
    scale: float
    report: dict = field(default_factory=dict)


def place_word(
    geometry: MultiPolygon, *, inner_r: float, outer_r: float
) -> tuple[MultiPolygon, float]:
    """Scale and move the traced artwork onto the hand axis.

    The word is scaled so it spans ``inner_r`` to ``outer_r`` along +x and is
    centred on y=0.  Centring matters: the artwork's own centre of symmetry has
    to sit on the hand's long axis, or the name stops reading when the hand
    turns 180 degrees.

    Returns:
        ``(placed_geometry, mm_per_pixel)``.
    """
    minx, miny, maxx, maxy = geometry.bounds
    width_px = maxx - minx
    if width_px <= 0:
        raise RuntimeError("artwork has no width")
    scale = (outer_r - inner_r) / width_px

    placed = affinity.scale(geometry, xfact=scale, yfact=scale, origin=(minx, miny))
    minx, miny, maxx, maxy = placed.bounds
    # Put the left edge at inner_r and the vertical centre on the axis.
    placed = affinity.translate(placed, xoff=inner_r - minx, yoff=-(miny + maxy) / 2.0)
    return _as_multipolygon(placed), scale


def hub_plate(spec: HandSpec, *, grub_screw: bool) -> MultiPolygon:
    """The hub disc, with an optional in-plane boss for a grub screw.

    The boss is radial rather than axial on purpose.  There is only 1.0 mm
    between one hand's front face and the next hand's back face, so an axial
    boss would foul its neighbour; a boss in the plane of the hand costs
    nothing.
    """
    disc = Point(0, 0).buffer(spec.hub_od / 2.0, quad_segs=64)
    if not grub_screw:
        return _as_multipolygon(disc)
    # A tab hanging off the hub at 90 degrees to the hand, so it never sits
    # under the word.
    tab_w = max(spec.hub_od * 0.55, 5.0)
    tab_r = spec.hub_od / 2.0 + 3.2
    tab = Polygon(
        [(-tab_w / 2, 0), (tab_w / 2, 0), (tab_w / 2, -tab_r), (-tab_w / 2, -tab_r)]
    )
    return _as_multipolygon(unary_union([disc, tab]))


def neck(spec: HandSpec, body: MultiPolygon, *, overlap: float = 2.5) -> Polygon:
    """A tapered spine from the hub out into the word.

    It would be convenient to assume the word starts at ``inner_r`` and run the
    neck to just past it, but a word's leftmost point can be a thin ascender
    well off the axis -- "Dad" is exactly that, and a neck built on the
    assumption missed the word entirely and left the hand in two pieces.

    So this measures instead: it walks out along the axis until the body
    actually has material within the neck's own half-width, then overshoots by
    ``overlap`` so the join has real width rather than touching at a tangent.
    """
    start = spec.hub_od / 2.0 - 0.5
    root = max(spec.hub_od * 0.42, 3.0)
    minx, miny, maxx, maxy = body.bounds
    half_height = (maxy - miny) / 2.0

    # Widen the neck's reach until it finds the word, rather than assuming.
    for fraction in (0.55, 0.7, 0.85, 1.0):
        tip = max(half_height * fraction, root * 0.6)
        strip = body.intersection(Polygon([(minx, -tip), (maxx, -tip), (maxx, tip), (minx, tip)]))
        if not strip.is_empty:
            join_x = strip.bounds[0] + overlap
            return Polygon([(start, -root), (start, root), (join_x, tip), (join_x, -tip)])

    # Nothing within the full height: fall back to reaching the centroid, which
    # is always inside a connected body.
    centroid = body.centroid
    tip = max(half_height * 0.5, root)
    return Polygon(
        [(start, -root), (start, root), (centroid.x, tip), (centroid.x, -tip)]
    )


def _max_radius(geometry) -> float:
    """Distance from the hub centre to the furthest point of a geometry."""
    return max(
        math.hypot(x, y) for ring in _rings(_as_multipolygon(geometry)) for x, y in ring
    )


def _plaque_plate(spec: HandSpec, word: MultiPolygon, *, inner_r: float, margin: float):
    """A tapered nameplate sized to hold ``word`` with a margin all round."""
    minx, miny, maxx, maxy = word.bounds
    half = (maxy - miny) / 2.0 + margin
    tip_half = half * 0.72
    return Polygon(
        [
            (inner_r - margin, -half),
            (inner_r - margin, half),
            (maxx + margin * 0.6, tip_half),
            (maxx + margin * 1.8, 0.0),
            (maxx + margin * 0.6, -tip_half),
        ]
    )


def build_hand(
    spec: HandSpec,
    *,
    style: str = "plaque",
    word_fraction: float = 0.74,
    halo: float | None = None,
    margin: float = 2.0,
    grub_screw: bool = True,
) -> Hand:
    """Build one hand from its artwork.

    Args:
        spec: Which hand.
        style: ``"plaque"`` puts the name in relief on a tapered nameplate;
            ``"halo"`` offsets the name outwards until its separate pieces
            merge, and uses that merged silhouette as the blade, so the hand's
            own outline is the word.
        word_fraction: How much of the tip radius the word spans.
        halo: Offset in mm for the ``halo`` style.  ``None`` picks the smallest
            offset that fuses this particular word, plus 0.3 mm of real bridge.
        margin: Blade margin around the word for the ``plaque`` style, mm.
        grub_screw: Add the radial boss and its hole.

    Returns:
        The finished :class:`Hand`.

    Raises:
        RuntimeError: if the result would not be one connected solid.
    """
    art = trace_artwork(ART_DIR / spec.artwork)
    merge_px = merge_offset(art)

    # The halo and the scale depend on each other: a bigger halo leaves less
    # room for the word, which shrinks the scale, which shrinks the gaps the
    # halo has to bridge. Solve it rather than guessing, or the word comes out
    # in two pieces -- which is exactly what the validation below caught.
    #
    #   halo = k * (span - halo) + bridge,  k = merge_px / width_px
    #   halo = (k * span + bridge) / (1 + k)
    width_px = art.bounds[2] - art.bounds[0]
    inner_r = spec.tip_r - word_fraction * spec.tip_r
    span = spec.tip_r - inner_r
    k = merge_px / width_px
    if halo is None:
        halo = max((k * span + HALO_BRIDGE) / (1 + k), HALO_MIN)
    tip_inset = halo if style == "halo" else margin * 1.6
    # What has to land on the design radius is the *furthest point from the hub*,
    # and on a word that is 25 mm tall that is a corner, not the tip of the last
    # letter. Solve for it: place the word, measure the real maximum radius,
    # shrink, repeat. Two or three passes is plenty.
    outer_r = spec.tip_r - tip_inset
    word = scale = outline = None
    for _ in range(8):
        word, scale = place_word(art, inner_r=inner_r, outer_r=outer_r)
        outline = (
            word.buffer(halo, quad_segs=16, join_style=1)
            if style == "halo"
            else _plaque_plate(spec, word, inner_r=inner_r, margin=margin)
        )
        reach = _max_radius(outline)
        if abs(reach - spec.tip_r) <= 0.05:
            break
        outer_r = inner_r + (outer_r - inner_r) * (spec.tip_r / reach)
    stroke_mm = min_stroke(word, limit=20.0)
    minx, miny, maxx, maxy = word.bounds
    hub = hub_plate(spec, grub_screw=grub_screw)

    if style == "halo":
        # Grow the word until the loose pieces touch, then use that as the
        # blade.  The relief on top is the word itself, so the outline reads as
        # the name from across the room and the letters read up close.
        needed = merge_px * scale
        if halo < needed:
            raise RuntimeError(
                f"{spec.person}: a halo of {halo:.2f} mm leaves the word in pieces; "
                f"it needs at least {needed:.2f} mm to fuse. Pass --halo {needed + 0.2:.1f}."
            )
        merged = _as_multipolygon(outline)
        if len(merged.geoms) != 1:
            raise RuntimeError(f"{spec.person}: halo {halo} mm did not fuse the word")
        body = unary_union([merged, hub, neck(spec, merged)])
    elif style == "plaque":
        plate = _as_multipolygon(outline)
        body = unary_union([plate, hub, neck(spec, plate)])
    else:
        raise RuntimeError(f"unknown style {style!r}")

    body = _as_multipolygon(body if body.is_valid else body.buffer(0))

    # Cut the bore, and the grub-screw hole if there is a boss for it.
    bore = Point(0, 0).buffer(spec.bore / 2.0, quad_segs=64)
    blade = body.difference(bore)
    screw_d = 3.0 if spec.mount_od >= 6.0 else 2.0
    if grub_screw:
        hole = Point(0, -(spec.hub_od / 2.0 + 1.4)).buffer(screw_d / 2.0, quad_segs=32)
        blade = blade.difference(hole)
    blade = _as_multipolygon(blade if blade.is_valid else blade.buffer(0))

    blade_stroke = min_bridge(blade, limit=6.0)
    achieved_tip = max(math.hypot(x, y) for ring in _rings(blade) for x, y in ring)

    if len(blade.geoms) != 1:
        raise RuntimeError(
            f"{spec.person}: the hand came out in {len(blade.geoms)} pieces. "
            f"For the halo style, raise --halo above {merge_px * scale:.2f} mm."
        )
    if blade_stroke < MIN_FEATURE:
        raise RuntimeError(
            f"{spec.person}: the narrowest bridge holding the hand together is "
            f"{blade_stroke:.2f} mm, under the {MIN_FEATURE} mm printable minimum. "
            f"Raise --halo or --word-fraction."
        )
    if abs(achieved_tip - spec.tip_r) > 0.6:
        raise RuntimeError(
            f"{spec.person}: tip radius is {achieved_tip:.2f} mm, not {spec.tip_r} mm"
        )

    report = {
        "pieces_in_artwork": len(art.geoms),
        "halo_mm": halo if style == "halo" else None,
        "min_blade_feature_mm": blade_stroke,
        "achieved_tip_r_mm": achieved_tip,
        "blade_pieces": len(blade.geoms),
        "mm_per_px": scale,
        "word_length_mm": maxx - minx,
        "word_height_mm": maxy - miny,
        "min_stroke_mm": stroke_mm,
        "merge_offset_px": merge_px,
        "halo_needed_mm": merge_px * scale,
        "blade_area_mm2": blade.area,
        "bore_mm": spec.bore,
        "hub_od_mm": spec.hub_od,
        "grip_length_mm": HAND_T,
        "screw": f"M{int(screw_d)}" if grub_screw else None,
        "tip_r_mm": outer_r,
    }
    hand = Hand(spec=spec, blade=blade, word=word, style=style, scale=scale, report=report)
    _measure_balance(hand)
    return hand


def _measure_balance(hand: Hand) -> None:
    """Record where the mass sits, and what it weighs.

    The steppers are open loop with no encoders: a hand heavy enough to slip
    under its own weight loses position silently until somebody re-zeroes it at
    the clock, so this is worth knowing before anything is printed.
    """
    blade = hand.blade
    volume_mm3 = blade.area * HAND_T + hand.word.area * RELIEF_H
    # A 2.5 mm plate is all perimeters and solid layers, so quote it solid:
    # PLA at 1.24 g/cm^3. Erring heavy is the right direction for a torque check.
    hand.report["volume_mm3"] = volume_mm3
    hand.report["mass_g_est"] = volume_mm3 / 1000.0 * 1.24
    centroid = blade.centroid
    hand.report["centroid_r_mm"] = math.hypot(centroid.x, centroid.y)
    hand.report["moment_g_mm"] = hand.report["mass_g_est"] * hand.report["centroid_r_mm"]


# --- export ------------------------------------------------------------------


def _rings(geometry: MultiPolygon):
    """Yield every ring of every polygon as a list of (x, y)."""
    for polygon in geometry.geoms:
        yield list(polygon.exterior.coords)
        for hole in polygon.interiors:
            yield list(hole.coords)


def _path_d(geometry: MultiPolygon, *, flip_y: bool = True) -> str:
    """SVG path data for a geometry, with the usual y-down flip."""
    parts = []
    for ring in _rings(geometry):
        points = [(x, -y if flip_y else y) for x, y in ring]
        head = f"M {points[0][0]:.3f} {points[0][1]:.3f}"
        tail = " ".join(f"L {x:.3f} {y:.3f}" for x, y in points[1:])
        parts.append(f"{head} {tail} Z")
    return " ".join(parts)


def write_svg(hand: Hand, path: Path, *, show_guides: bool = True) -> Path:
    """Write one hand as an SVG, at true size in millimetres.

    This is the cheap thing to look at before committing to 3D: if the word is
    unreadable or the blade is the wrong shape, it shows up here.
    """
    spec = hand.spec
    pad = 6.0
    minx, miny, maxx, maxy = hand.blade.bounds
    minx, miny = min(minx, -spec.hub_od), min(miny, -spec.hub_od)
    maxx, maxy = max(maxx, spec.hub_od), max(maxy, spec.hub_od)
    width, height = (maxx - minx) + 2 * pad, (maxy - miny) + 2 * pad

    guides = ""
    if show_guides:
        guides = (
            f'<circle cx="0" cy="0" r="{spec.tip_r:.2f}" fill="none" '
            f'stroke="#B9A98C" stroke-width="0.2" stroke-dasharray="2 2"/>'
            f'<line x1="{-spec.hub_od:.2f}" y1="0" x2="{spec.tip_r + 4:.2f}" y2="0" '
            f'stroke="#B9A98C" stroke-width="0.15" stroke-dasharray="3 3"/>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
     width="{width:.2f}mm" height="{height:.2f}mm"
     viewBox="{minx - pad:.3f} {-(maxy + pad):.3f} {width:.3f} {height:.3f}">
  <title>{spec.person} ({spec.reads}) — level {spec.level} clock hand, {hand.style}</title>
  <g>
    {guides}
    <path d="{_path_d(hand.blade)}" fill="{spec.color}" fill-rule="evenodd"
          stroke="#2B2419" stroke-width="0.25"/>
    <path d="{_path_d(hand.word)}" fill="#1a1a1a" fill-rule="evenodd" opacity="0.92"/>
  </g>
</svg>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
    return path


def write_sheet(hands: list[Hand], path: Path) -> Path:
    """Write every hand onto one sheet, laid out as they sit on the dial.

    Hands are drawn at their real staggered radii from a common centre, which
    is the quickest way to see whether the tips overlap or a word is too tall
    for its neighbours.
    """
    radius = max(h.spec.tip_r for h in hands) + 14
    rows = []
    for index, hand in enumerate(hands):
        angle = -index * 60.0  # spread them out so they can all be read
        rotated_blade = affinity.rotate(hand.blade, angle, origin=(0, 0))
        rotated_word = affinity.rotate(hand.word, angle, origin=(0, 0))
        rows.append(
            f'<path d="{_path_d(rotated_blade)}" fill="{hand.spec.color}" '
            f'fill-rule="evenodd" stroke="#2B2419" stroke-width="0.2" opacity="0.95"/>'
            f'<path d="{_path_d(rotated_word)}" fill="#1a1a1a" fill-rule="evenodd"/>'
        )
    body = "\n    ".join(rows)
    size = radius * 2
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
     width="{size:.1f}mm" height="{size:.1f}mm"
     viewBox="{-radius:.1f} {-radius:.1f} {size:.1f} {size:.1f}">
  <title>Weasley Clock — six ambigram hands at true size</title>
  <rect x="{-radius:.1f}" y="{-radius:.1f}" width="{size:.1f}" height="{size:.1f}" fill="#F7F3EA"/>
  <circle cx="0" cy="0" r="{radius - 12:.1f}" fill="#FBF7EE" stroke="#C8A464" stroke-width="0.6"/>
  <g>
    {body}
  </g>
</svg>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
    return path


def write_dxf(hand: Hand, path: Path) -> Path:
    """Write the blade outline as DXF, for laser cutting or CNC."""
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    doc.layers.add("BLADE", color=7)
    doc.layers.add("NAME", color=1)
    for ring in _rings(hand.blade):
        msp.add_lwpolyline(ring, close=True, dxfattribs={"layer": "BLADE"})
    for ring in _rings(hand.word):
        msp.add_lwpolyline(ring, close=True, dxfattribs={"layer": "NAME"})
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


# --- 3D ----------------------------------------------------------------------


def _cq_solid(geometry: MultiPolygon, thickness: float, z: float, *, tol: float = 0.06):
    """Extrude a shapely geometry into a CadQuery solid.

    Args:
        geometry: Polygons, possibly with holes.
        thickness: Extrusion height, mm.
        z: Workplane offset, mm.
        tol: Simplification tolerance, mm. OCC builds a spline through every
            vertex, so the traced outlines are thinned first; 0.06 mm is well
            under what a 0.4 mm nozzle resolves.

    Returns:
        A ``cadquery.Workplane``, or ``None`` if there was nothing to build.
    """
    import cadquery as cq

    simplified = _as_multipolygon(geometry.simplify(tol, preserve_topology=True))
    solid = None
    for polygon in simplified.geoms:
        if polygon.is_empty or polygon.area < 0.01:
            continue
        shell = list(polygon.exterior.coords)[:-1]
        part = cq.Workplane("XY").workplane(offset=z).polyline(shell).close().extrude(thickness)
        for hole in polygon.interiors:
            ring = list(hole.coords)[:-1]
            if len(ring) < 3:
                continue
            cutter = (
                cq.Workplane("XY")
                .workplane(offset=z - 0.5)
                .polyline(ring)
                .close()
                .extrude(thickness + 1.0)
            )
            part = part.cut(cutter)
        solid = part if solid is None else solid.union(part)
    return solid


def to_solid(hand: Hand):
    """Build the hand as a single CadQuery solid: plate plus raised name."""
    blade = _cq_solid(hand.blade, HAND_T, 0.0)
    if blade is None:
        raise RuntimeError(f"{hand.spec.person}: blade produced no solid")
    relief = _cq_solid(hand.word, RELIEF_H, HAND_T)
    return blade if relief is None else blade.union(relief)


def write_3d(hand: Hand, out_dir: Path) -> list[Path]:
    """Write STEP and STL for one hand.

    STEP is the file that opens in real CAD; STL goes straight to a slicer.
    """
    solid = to_solid(hand)
    stem = f"{hand.spec.level}-{hand.spec.slug}-{hand.style}"
    written = []
    step_path = out_dir / f"{stem}.step"
    stl_path = out_dir / f"{stem}.stl"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        import cadquery as cq

        cq.exporters.export(solid, str(step_path))
        written.append(step_path)
        cq.exporters.export(solid, str(stl_path), tolerance=0.01, angularTolerance=0.1)
        written.append(stl_path)
    except Exception as exc:  # pragma: no cover - depends on the OCC build
        raise RuntimeError(f"{hand.spec.person}: 3D export failed: {exc}") from exc
    return written


# --- command line ------------------------------------------------------------


def format_report(hand: Hand) -> str:
    """One line per hand for the build log."""
    r = hand.report
    halo = f"{r['halo_mm']:.2f}" if r.get("halo_mm") else "  -  "
    return (
        f"  level {hand.spec.level}  {hand.spec.person:<7s} reads {hand.spec.reads:<7s}"
        f" halo {halo}  bridge {r['min_blade_feature_mm']:5.2f} mm"
        f"  tip {r['achieved_tip_r_mm']:6.2f}/{hand.spec.tip_r}"
        f"  word {r['word_length_mm']:.0f}x{r['word_height_mm']:.0f} mm"
        f"  {r['mass_g_est']:4.1f} g  bore {r['bore_mm']:5.2f} {r['screw']}"
    )


def main(argv: list[str] | None = None) -> int:
    """Build the hands and write the files."""
    parser = argparse.ArgumentParser(
        description="Build the six ambigram clock hands as CAD files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Styles:\n"
            "  halo    the hand's own outline is the word (striking)\n"
            "  plaque  the word in relief on a tapered nameplate (strongest)\n"
        ),
    )
    parser.add_argument("--style", choices=("halo", "plaque", "both"), default="both")
    parser.add_argument("--only", default=None, help="one person, while iterating")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--svg-only", action="store_true", help="skip STEP and STL")
    parser.add_argument("--halo", type=float, default=None, help="override the halo, mm")
    parser.add_argument("--word-fraction", type=float, default=0.74)
    parser.add_argument("--no-grub-screw", action="store_true")
    args = parser.parse_args(argv)

    specs = HANDS
    if args.only:
        wanted = args.only.strip().lower()
        specs = [s for s in HANDS if s.slug == wanted or s.reads.lower() == wanted]
        if not specs:
            print(f"no hand called {args.only!r}", file=sys.stderr)
            return 2

    styles = ("halo", "plaque") if args.style == "both" else (args.style,)
    args.out.mkdir(parents=True, exist_ok=True)
    failures = 0

    for style in styles:
        print(f"\n{style}:")
        built = []
        for spec in specs:
            try:
                hand = build_hand(
                    spec,
                    style=style,
                    halo=args.halo,
                    word_fraction=args.word_fraction,
                    grub_screw=not args.no_grub_screw,
                )
            except RuntimeError as exc:
                print(f"  level {spec.level}  {spec.person:<7s} FAILED: {exc}", file=sys.stderr)
                failures += 1
                continue

            stem = f"{spec.level}-{spec.slug}-{style}"
            write_svg(hand, args.out / f"{stem}.svg")
            write_dxf(hand, args.out / f"{stem}.dxf")
            if not args.svg_only:
                try:
                    write_3d(hand, args.out)
                except RuntimeError as exc:
                    print(f"    3D export failed: {exc}", file=sys.stderr)
                    failures += 1
            print(format_report(hand))
            built.append(hand)

        if len(built) > 1:
            sheet = write_sheet(built, args.out / f"all-hands-{style}.svg")
            print(f"  sheet: {sheet.name}")

    print(f"\nFiles in {args.out}")
    if failures:
        print(f"{failures} failure(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

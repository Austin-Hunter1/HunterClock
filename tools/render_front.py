"""Render the front of the clock, with the hands at a set of distances.

    python tools/render_front.py
    python tools/render_front.py --size 1100 --out cad_out/front

Builds one scene from the parts that already exist -- the measured dial plate,
the six option-D hands, the six ornate bezels -- puts the hood and trunk
boards around them at Caleb's measurements, and renders it square on for each
of several hand positions.

The point is to see the log scale as angles rather than as a table. One decade
of distance is 83.7 degrees, so a hand at 10 km and a hand at 100 km are a
quarter-turn apart while 1000 km and 2000 km are barely a thumb's width.

Two things in the scene are assumptions rather than measurements, and are
marked as such wherever they appear:

* **The trunk.** Caleb measured the hood front board, not the trunk. Its width
  here is a typical longcase 11 in. The six displays go "in the gutted trunk"
  per the hardware plan, which does not say where, so they are laid out in two
  columns of three in the pairing ``build_cad.py`` already uses.
* **The hand colours.** The hands print in black PLA. They are coloured per
  person here only so six overlapping hands can be told apart.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "controller"))

from tools import preview3d  # noqa: E402
from tools.preview3d import BACKGROUND  # noqa: E402

IN = 25.4

#: Caleb's measurements of the hood front board, 2026-11-20.
BOARD_W, BOARD_H = 15.0 * IN, 19.0 * IN
SQUARE = 10.625 * IN
ARCH_D = 8.5 * IN
CUTOUT_UP = 1.25 * IN

#: Assumed, not measured: a typical longcase trunk.
TRUNK_W, TRUNK_H = 11.0 * IN, 6.0 * IN  # only enough to show the case carries on

#: The displays go in a column either side of the dial, three a side.
#:
#: They do not fit the board as measured. The hood front is 15 in wide and
#: the opening is 10-5/8 in, which leaves 55.5 mm of stile either side -- a
#: 53.5 mm bezel would clear it by a millimetre, into wood that is holding the
#: glass frame. So each column sits on a 75 mm wing added to the side of the
#: hood, taking the whole front to 20.9 in. The alternative is a wider front
#: board; either way the 15 in board cannot carry them on its own.
WING_W = 75.0
WING_H = SQUARE + 20.0
SCREEN_X = BOARD_W / 2 + WING_W / 2
SCREEN_PITCH, SCREEN_TOP = 70.0, 70.0
SCREEN_COLUMNS = {-1: ("Becky", "Elijah", "Caleb"), +1: ("Austin", "Micah", "Evan")}

#: Hub level order, which is the order the firmware uses for channels 0-5.
LEVELS = ("Becky", "Evan", "Elijah", "Caleb", "Austin", "Micah")
HAND_FILE = {
    "Becky": "1-becky-pointed", "Evan": "2-evan-pointed", "Elijah": "3-elijah-pointed",
    "Caleb": "4-caleb-pointed", "Austin": "5-austin-pointed", "Micah": "6-micah-pointed",
}
#: The hands read as gilt brass, with the raised name picked out. The accent
#: is per person by default: one colour for all six would be handsome and
#: unreadable the moment two hands cross. ``--accent`` overrides it.
GOLD = "#C69A2B"
PERSON_RGB = {
    "Becky": "#4F7A4A", "Evan": "#5B5EA6", "Elijah": "#3E6470",
    "Caleb": "#A8672B", "Austin": "#8A4B6B", "Micah": "#B0562A",
}

PLATE_TOP = 2.4
HAND_GAP = 3.5
#: The plate is 2.5 mm and the name stands 0.6 mm proud of it, so faces above
#: this are the lettering and get the accent.
HAND_T = 2.5
WALNUT = "#5E3B21"
TRUNK = "#53341D"
DIAL = "#CFC8B0"
BRASS = "#B08A3C"
GLASS = "#16181C"
INTERIOR = "#1A140E"
DISC = "#DBD5C1"
WING = "#6B462A"
ENGRAVE = "#595242"

#: The chapter ring. One decade of distance is 360/log10(pi*6371) = 83.7
#: degrees, and the ring is machined for that scale rather than for hours.
RING_IN, RING_OUT = 110.0, 122.0
DECADE_KM = (1.0, 10.0, 100.0, 1_000.0, 10_000.0)
DECADE_LABEL = ("1 km", "10", "100", "1 000", "10 000")

#: Home and the six real locations, from ``birthday_clock.m``.
HOME = (44.9109, -93.5017)  # Minnetonka, MN
WHERE = {
    "Becky": (44.9109, -93.5017),    # Minnetonka High School, MN
    "Evan": (52.2297, 21.0122),      # Warsaw, Poland
    "Elijah": (44.9537, -93.0900),   # St Paul, MN
    "Caleb": (41.8661, -88.1070),    # Wheaton, IL
    "Austin": (40.0150, -105.2705),  # Boulder, CO
    "Micah": (44.5646, -123.2620),   # Corvallis, OR
}


def views() -> list[tuple[str, str, dict[str, float]]]:
    """The positions to render, as distances in km per person.

    The first is the real one: the same six places ``birthday_clock.m`` plots,
    through the same haversine and the same log map, so the angles here are
    the angles that sim produces. The other two are there to read it by.
    """
    from clockd import geo

    actual = {
        person: geo.haversine_km(HOME[0], HOME[1], lat, lon)
        for person, (lat, lon) in WHERE.items()
    }
    return [
        ("actual", "Where everyone actually is", actual),
        ("all-home", "Everyone home", dict.fromkeys(LEVELS, 0.0)),
        ("decades", "One hand per decade",
         {"Becky": 1.0, "Evan": 10.0, "Elijah": 100.0,
          "Caleb": 1_000.0, "Austin": 10_000.0, "Micah": 3_000.0}),
    ]


def rgb(value: str) -> np.ndarray:
    """``#rrggbb`` to an RGB array in 0-1."""
    value = value.lstrip("#")
    return np.array([int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4)])


@dataclass
class Scene:
    """The parts that never move, plus the camera framing they imply."""

    fixed: list
    hands: dict
    bounds: tuple


def _prism(polygon, z0: float, z1: float):
    """Extrude a shapely polygon between two z heights."""
    import trimesh

    pieces = getattr(polygon, "geoms", None) or [polygon]
    solid = trimesh.util.concatenate([
        trimesh.creation.extrude_polygon(piece, height=z1 - z0) for piece in pieces
    ])
    solid.apply_translation((0, 0, z0))
    return solid


def hood_board():
    """The 15 x 19 in front board with the break-arch opening cut out of it.

    The dial centre is the origin, so the board's own extent follows from
    where the square section sits: 1.25 in off the bottom edge, and the dial
    disc centred in it.
    """
    from shapely.geometry import Point, box
    from shapely.ops import unary_union

    half = SQUARE / 2
    bottom = -half - CUTOUT_UP
    board = box(-BOARD_W / 2, bottom, BOARD_W / 2, bottom + BOARD_H)
    arch = Point(0, half).buffer(ARCH_D / 2, resolution=128).intersection(
        box(-ARCH_D, half, ARCH_D, half + ARCH_D)
    )
    opening = unary_union([box(-half, -half, half, half), arch])
    return _prism(board.difference(opening), 0.0, 19.0)


def trunk_board():
    """The trunk front below the hood. Width and height are assumed."""
    from shapely.geometry import box

    top = -SQUARE / 2 - CUTOUT_UP
    return _prism(box(-TRUNK_W / 2, top - TRUNK_H, TRUNK_W / 2, top), 0.0, 19.0)


def chapter_ring():
    """Tick marks for the log-distance scale, as geometry on the dial.

    Square on to the front there is no shading to reveal a 0.794 mm raised
    disc, so without the ring the dial is a blank tan square and the whole
    point -- what angle a distance comes out at -- cannot be read off the
    picture. Minor ticks every 10 degrees, major ones at the five decades.
    """
    from clockd import geo
    from shapely import affinity
    from shapely.geometry import box
    from shapely.ops import unary_union

    majors = {round(geo.distance_to_angle(km), 3) for km in DECADE_KM}
    ticks = []
    for step in range(36):
        angle = step * 10.0
        major = any(abs(angle - m) < 0.75 for m in majors)
        inner = RING_IN if major else RING_IN + 5.0
        width = 1.8 if major else 0.9
        tick = box(inner, -width / 2, RING_OUT, width / 2)
        ticks.append(affinity.rotate(tick, 90.0 - angle, origin=(0, 0)))
    for km in DECADE_KM:  # the decades do not land on the 10-degree grid
        angle = geo.distance_to_angle(km)
        tick = box(RING_IN - 4.0, -1.4, RING_OUT, 1.4)
        ticks.append(affinity.rotate(tick, 90.0 - angle, origin=(0, 0)))
    return _prism(unary_union(ticks), PLATE_TOP + 0.4, PLATE_TOP + 0.9)


def build_scene(root: Path) -> Scene:
    """Load every part once and place everything that does not depend on angle."""
    import trimesh
    from shapely.geometry import box

    plate = trimesh.load(str(root / "cad/face/Clock_Face_1.3mf"))
    plate = plate.to_geometry() if hasattr(plate, "to_geometry") else plate

    fixed = []
    # A backboard, so the arch cut-out and the dial's holes read as the dark
    # inside of a cabinet rather than as a hole in the page. It follows the
    # silhouette exactly, so it never shows past an edge.
    hood, trunk = hood_board(), trunk_board()
    square_bottom = -SQUARE / 2 - CUTOUT_UP
    interior = _prism(
        box(-BOARD_W / 2, square_bottom, BOARD_W / 2, square_bottom + BOARD_H),
        -42.0, -40.0,
    )
    fixed.append((interior, rgb(INTERIOR)))
    for board, colour in ((hood, WALNUT), (trunk, TRUNK)):
        board.apply_translation((0, 0, -19.0))  # behind the dial plate
        fixed.append((board, rgb(colour)))
    fixed.append((plate, rgb(DIAL)))
    # The dial disc, so the chapter ring has a face to sit on square on.
    # Sits under the chapter ring, which is 0.4 mm proud of it.
    disc = trimesh.creation.cylinder(radius=123.8, height=0.4, sections=256)
    disc.apply_translation((0, 0, PLATE_TOP + 0.2))
    fixed.append((disc, rgb(DISC)))
    fixed.append((chapter_ring(), rgb(ENGRAVE)))

    bezel = trimesh.load(str(root / "cad/bezels/bezel-ornate.stl"))
    for side, names in SCREEN_COLUMNS.items():
        inner, outer = side * BOARD_W / 2, side * (BOARD_W / 2 + WING_W)
        wing = _prism(
            box(min(inner, outer), -WING_H / 2, max(inner, outer), WING_H / 2),
            -19.0, 0.0,
        )
        fixed.append((wing, rgb(WING)))
        for index, _name in enumerate(names):
            x, y = side * SCREEN_X, SCREEN_TOP - index * SCREEN_PITCH
            ring = bezel.copy()
            ring.apply_translation((x, y, 0.0))
            fixed.append((ring, rgb(BRASS)))
            glass = trimesh.creation.cylinder(radius=16.2, height=1.0, sections=64)
            glass.apply_translation((x, y, 4.4))
            fixed.append((glass, rgb(GLASS)))

    hands = {}
    for level, person in enumerate(LEVELS, start=1):
        hand = trimesh.load(str(root / f"cad/hands/{HAND_FILE[person]}.stl"))
        hand.apply_translation((0, 0, PLATE_TOP + 1.5 + (level - 1) * HAND_GAP))
        hands[person] = split_relief(hand)

    probe = fixed + [(part, rgb("#000000"))
                     for pair in hands.values() for part in pair]
    return Scene(fixed=fixed, hands=hands, bounds=preview3d.scene_bounds(probe))


def split_relief(hand):
    """Separate a hand into its plate and the name standing proud of it.

    The two come out of ``build_hands.py`` as one solid, which is right for
    printing and wrong for looking at: the whole point of the lettering is
    that it catches the light differently. Faces above the plate's top are
    the name, so one height test parts them.
    """
    base_z = hand.bounds[0][2]
    high = hand.triangles_center[:, 2] > base_z + HAND_T + 0.05
    return hand.submesh([~high], append=True), hand.submesh([high], append=True)


def place(hand, angle_deg: float):
    """A copy of the hand pointing at ``angle_deg`` clockwise from 12 o'clock.

    The hands are modelled lying along +x with the bore at the origin, so a
    hand as built points at 3 o'clock, which is 90 degrees. Rotating by
    ``90 - angle`` counter-clockwise puts it where the dial wants it.
    """
    import trimesh

    turned = hand.copy()
    turned.apply_transform(
        trimesh.transformations.rotation_matrix(math.radians(90.0 - angle_deg), (0, 0, 1))
    )
    return turned


def render_view(scene: Scene, distances: dict[str, float], size: int,
                accent: str | None = None) -> np.ndarray:
    """One panel: the fixed parts plus the six hands at these distances."""
    from clockd import geo

    parts = list(scene.fixed)
    for person in LEVELS:
        angle = geo.distance_to_angle(distances[person])
        plate, letters = scene.hands[person]
        parts.append((place(plate, angle), rgb(GOLD)))
        parts.append((place(letters, angle), rgb(accent or PERSON_RGB[person])))
    return preview3d.render_scene(parts, size=size, bounds=scene.bounds)


def label_decades(draw, scene: Scene, size: int) -> None:
    """Write the decade distances against their ticks on the chapter ring."""
    from clockd import geo

    points = []
    for km in DECADE_KM:
        angle = math.radians(geo.distance_to_angle(km))
        points.append((math.sin(angle) * 131.0, math.cos(angle) * 131.0, PLATE_TOP))
    screen = preview3d.project(points, scene.bounds, size)
    face = font(max(9, size // 80))
    for (x, y), text in zip(screen, DECADE_LABEL, strict=True):
        draw.text((x, y), text, fill=(86, 74, 48), anchor="mm", font=face)


FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def font(points: int):
    """A real face at a real size; PIL's built-in bitmap font does not scale."""
    from PIL import ImageFont

    try:
        return ImageFont.truetype(FONT_PATH, points)
    except OSError:
        return ImageFont.load_default()


def caption(image: np.ndarray, title: str, lines: list[str], size: int, scene: Scene):
    """Crop to the clock, label the dial, and set the distances underneath."""
    from PIL import Image, ImageDraw

    rendered = Image.fromarray((np.clip(image, 0, 1) * 255).astype(np.uint8))
    draw = ImageDraw.Draw(rendered)
    label_decades(draw, scene, size)

    # The clock is tall and the frame is square, so crop the empty sides off
    # rather than render the page.
    ink = np.any(np.abs(image - BACKGROUND) > 0.01, axis=2)
    rows, cols = np.where(ink)
    pad = int(size * 0.03)
    left = max(int(cols.min()) - pad, 0)
    right = min(int(cols.max()) + pad, size - 1)
    top = max(int(rows.min()) - pad, 0)
    bottom = min(int(rows.max()) + pad, size - 1)
    rendered = rendered.crop((left, top, right + 1, bottom + 1))

    width = rendered.width
    head = int(size * 0.045)
    strip = int(size * 0.085)
    canvas = Image.new("RGB", (width, head + rendered.height + strip), (247, 244, 238))
    canvas.paste(rendered, (0, head))
    draw = ImageDraw.Draw(canvas)
    gap = int(size * 0.012)
    draw.text((gap, gap), title, fill=(26, 32, 40), font=font(max(13, size // 52)))
    y = head + rendered.height + gap // 2
    for line in lines:
        draw.text((gap, y), line, fill=(96, 108, 120), font=font(max(10, size // 72)))
        y += max(13, size // 58)
    return canvas


def main(argv: list[str] | None = None) -> int:
    """Render every view and a contact sheet of all of them."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("docs/img/front"))
    parser.add_argument("--size", type=int, default=900)
    parser.add_argument("--only", default=None, help="render just this view by slug")
    parser.add_argument(
        "--accent", default=None, metavar="HEX",
        help="one colour for every raised name, e.g. #1E2228, instead of per person",
    )
    args = parser.parse_args(argv)

    from clockd import geo
    from PIL import Image

    root = Path(__file__).resolve().parent.parent
    args.out.mkdir(parents=True, exist_ok=True)

    print("Clock front")
    print(f"  hood board    {BOARD_W:.0f} x {BOARD_H:.0f} mm  (measured)")
    print(f"  opening       {SQUARE:.0f} mm square + {ARCH_D:.0f} mm arch  (measured)")
    print(f"  trunk         {TRUNK_W:.0f} x {TRUNK_H:.0f} mm  (assumed)")

    scene = build_scene(root)
    print(f"  {len(scene.fixed)} fixed parts, 6 hands\n")

    panels = []
    for slug, title, distances in views():
        if args.only and slug != args.only:
            continue
        image = render_view(scene, distances, args.size, args.accent)
        lines = [
            "  ".join(
                f"{p} {geo.distance_to_angle(distances[p]):.0f}°"
                for p in LEVELS[i::2]
            )
            for i in (0, 1)
        ]
        panel = caption(image, title, lines, args.size, scene)
        path = args.out / f"front-{slug}.png"
        panel.save(path)
        panels.append(panel)
        print(f"  wrote {path.name}")

    if len(panels) > 1:
        cols = min(len(panels), 3)
        rows = -(-len(panels) // cols)
        w, h = panels[0].size
        sheet = Image.new("RGB", (cols * w, rows * h), (247, 244, 238))
        for index, panel in enumerate(panels):
            sheet.paste(panel, ((index % cols) * w, (index // cols) * h))
        path = args.out / "front-all.png"
        sheet.save(path)
        print(f"  wrote {path.name}: {len(panels)} panels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

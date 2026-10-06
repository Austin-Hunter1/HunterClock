"""
Weasley Clock -- parametric CAD model (CadQuery).

Builds an assembly of the mechanical concept in visualize_hardware_plan.py and
visualize_two_screen_layout.py:
  * grandfather-clock case outline (plinth, trunk, arched hood, swan-neck pediment)
  * round dial with six hands at the angles birthday_clock.m computes
    (Haversine distance from home -> log10 map -> 0..360 deg clockwise from 12)
  * six nested concentric hollow shafts, one per person, belt-driven by
    2x NEMA23 (outer shafts) + 4x NEMA17 (inner shafts) in a column down the trunk
  * two side panels flanking the dial, three round GC9A01 screens each

Dimensions are PLACEHOLDERS for concept review, in millimetres -- verify
against real parts before machining anything.

Coordinates: +z toward the viewer, +y up, dial centre at the origin,
dial front surface at z = 0.

Outputs (in ./cad_out): weasley_clock.step, weasley_clock.stl
"""

import math
import os

import cadquery as cq

# ---------------------------------------------------------------------------
# Distances / angles (same data and mapping as the other scripts)
# ---------------------------------------------------------------------------
HOME = (44.9109, -93.5017)
PEOPLE = {
    "Becky":  (44.9109, -93.5017),
    "Elijah": (44.9537, -93.0900),
    "Caleb":  (41.8661, -88.1070),
    "Austin": (40.0150, -105.2705),
    "Micah":  (44.5646, -123.2620),
    "Evan":   (52.2297, 21.0122),
}
NAMES = list(PEOPLE)
R_EARTH = 6371.0
MAX_DIST = math.pi * R_EARTH


def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def hand_angle_deg(name):
    d = max(haversine(*HOME, *PEOPLE[name]), 1.0)
    return math.log10(d) / math.log10(MAX_DIST) * 360.0


# ---------------------------------------------------------------------------
# Parameters (placeholders unless noted)
# ---------------------------------------------------------------------------
DIAL_R = 110.0
DIAL_T = 4.0
WALL = 1.0            # shaft wall thickness
OD0, OD_STEP = 26.0, 3.0   # outermost shaft OD, and reduction per nesting level
HAND_T = 2.5
HAND_Z0, HAND_DZ = 6.0, 3.5  # first hand height above dial, spacing between hands
HAND_LEN = [70, 76, 82, 88, 94, 100]   # tip radius per hand

# Drive: 2x NEMA23 on the outer shafts + 4x NEMA17 on the inner ones (hardware
# plan). Motors sit in a vertical column down the trunk, staggered in y and in
# depth so each belt runs in its own plane, each belting to its shaft.
# (STEP file, body length, shaft length, half flange width) -- from the vendor models
NEMA23 = ("motors/23HS18-2004H.STEP", 46.0, 25.0, 28.6)
NEMA17 = ("motors/17HS13-1504H.STEP", 34.0, 16.0, 21.2)
MOTORS = [NEMA23, NEMA23, NEMA17, NEMA17, NEMA17, NEMA17]
MOTOR_Y0, MOTOR_Y_STEP = -85.0, 62.0   # first motor axis below the clock axis, then spacing
BELT_PLANE0, BELT_PLANE_STEP = -55.0, 14.0
BELT_W = 8.0
MOTOR_PULLEY_R = 8.0

# Round display modules (GC9A01 1.28in, as ordered): 39.5 mm board, 32.4 mm glass.
# Two vertical panels flank the dial, three screens each (near cluster left, far right).
SCREEN_OD, SCREEN_GLASS, SCREEN_T = 39.5, 32.4, 3.0
PANEL_X, PANEL_W, PANEL_H, PANEL_T = 150.0, 56.0, 230.0, 6.0
SCREEN_PITCH = 70.0
SCREEN_COLUMNS = {-1: ["Becky", "Elijah", "Caleb"], +1: ["Austin", "Micah", "Evan"]}

# Grandfather-clock case outline, from the reference photos
FLOOR_Y = -1550.0
CASE_FRONT_Z, CASE_BACK_Z = 45.0, -270.0
CASE_WALL = 15.0
COLORS = ["#4F7A4A", "#3E6470", "#A8672B", "#8A4B6B", "#B0562A", "#5B5EA6"]
WALNUT = "#6B4426"


def tube(od, wall, z0, z1):
    return (cq.Workplane("XY").workplane(offset=z0)
            .circle(od / 2).circle(od / 2 - wall).extrude(z1 - z0))


def hand(od, od_next, z, length, angle_deg):
    """Tapered pointer with a hub, built pointing at 12 o'clock then rotated.

    The hub is as wide as its own shaft and bored for the next-inner shaft,
    which passes through it (od_next = 0 for the innermost, solid hub).
    """
    w_base, w_tip = 12.0, 2.0
    arm = (cq.Workplane("XY").workplane(offset=z)
           .polyline([(-w_base / 2, 0), (w_base / 2, 0), (w_tip / 2, length), (-w_tip / 2, length)])
           .close().extrude(HAND_T))
    h = cq.Workplane("XY").workplane(offset=z).circle(od / 2).extrude(HAND_T).union(arm)
    if od_next:
        h = h.cut(cq.Workplane("XY").workplane(offset=z - 1).circle(od_next / 2 + 0.25).extrude(HAND_T + 2))
    return h.rotate((0, 0, 0), (0, 0, 1), -angle_deg)  # clockwise from 12 o'clock


def load_motor(spec):
    """Vendor STEP, trimmed of its lead wires (which run ~90+ mm out the back)."""
    path, body, shaft, half = spec
    m = cq.importers.importStep(path)
    keep = (cq.Workplane("XY").workplane(offset=-body - 0.5)
            .rect(2 * half + 1, 2 * half + 1).extrude(body + 0.5 + shaft + 0.5))
    return m.intersect(keep)


def box(x0, x1, y0, y1, z0, z1):
    return (cq.Workplane("XY").workplane(offset=z0).center((x0 + x1) / 2, (y0 + y1) / 2)
            .rect(x1 - x0, y1 - y0).extrude(z1 - z0))


def open_front(solid):
    return solid.faces(">Z").shell(-CASE_WALL)


def add_case(asm):
    wood = cq.Color(WALNUT)
    depth = CASE_FRONT_Z - CASE_BACK_Z
    # plinth + trunk (hollow, open front so the drive is visible)
    asm.add(box(-270, 270, FLOOR_Y, FLOOR_Y + 120, CASE_BACK_Z, CASE_FRONT_Z),
            name="case_plinth", color=wood)
    asm.add(open_front(box(-240, 240, FLOOR_Y + 120, -300, CASE_BACK_Z + 20, CASE_FRONT_Z - 10)),
            name="case_trunk", color=wood)
    # hood: rectangle + arched top, hollow, open front
    hood = (cq.Workplane("XY").workplane(offset=CASE_BACK_Z)
            .moveTo(-280, -300).lineTo(280, -300).lineTo(280, 170)
            .threePointArc((0, 330), (-280, 170)).close().extrude(depth))
    asm.add(open_front(hood), name="case_hood", color=wood)
    # broken swan-neck pediment: two wings
    for sgn, nm in ((-1, "L"), (1, "R")):
        pts = [(sgn * 285, 170), (sgn * 285, 330), (sgn * 230, 440), (sgn * 140, 470),
               (sgn * 60, 430), (sgn * 45, 360), (sgn * 110, 380), (sgn * 190, 330),
               (sgn * 200, 280), (sgn * 120, 330), (0, 330)]
        wing = (cq.Workplane("XY").workplane(offset=CASE_BACK_Z + 40).polyline(pts).close()
                .extrude(depth - 80))
        asm.add(wing, name=f"case_pediment_{nm}", color=wood)
    # turned finial on top of the arch
    asm.add(cq.Workplane("XY").sphere(22).translate((0, 475, (CASE_BACK_Z + CASE_FRONT_Z) / 2)),
            name="case_finial", color=wood)
    # arched dial plate behind the ring (silvered plate in the reference photo)
    plate = (cq.Workplane("XY").workplane(offset=-DIAL_T - 4)
             .moveTo(-190, -190).lineTo(190, -190).lineTo(190, 130)
             .threePointArc((0, 310), (-190, 130)).close().extrude(4))
    asm.add(plate, name="dial_plate", color=cq.Color("#D9D2C0"))


def build():
    asm = cq.Assembly(name="weasley_clock")
    add_case(asm)

    dial = (cq.Workplane("XY").workplane(offset=-DIAL_T).circle(DIAL_R)
            .circle(OD0 / 2 + 1.0).extrude(DIAL_T))
    asm.add(dial, name="dial", color=cq.Color("#F7F3EA"))

    protos = {}
    for i, name in enumerate(NAMES):
        spec = MOTORS[i]
        protos.setdefault(spec[0], load_motor(spec))
        zb = BELT_PLANE0 - BELT_PLANE_STEP * i      # centre of this level's belt plane
        my = MOTOR_Y0 - MOTOR_Y_STEP * i            # motor axis, below the clock axis
        od = OD0 - OD_STEP * i
        od_next = od - OD_STEP if i < len(NAMES) - 1 else 0.0  # innermost hand has a solid hub
        hand_z = HAND_Z0 + HAND_DZ * i
        color = cq.Color(COLORS[i])

        # Hollow shaft from the belt pulley up to the hand's hub.
        # Outer shafts are shorter, so each hand sits on its own shaft end.
        asm.add(tube(od, WALL, zb - BELT_W / 2, hand_z), name=f"shaft_{name}", color=color)
        asm.add(hand(od, od_next, hand_z, HAND_LEN[i], hand_angle_deg(name)),
                name=f"hand_{name}", color=color)
        asm.add(cq.Workplane("XY").workplane(offset=zb - BELT_W / 2)
                .circle(od / 2 + 12).circle(od / 2).extrude(BELT_W),
                name=f"pulley_{name}", color=cq.Color("#B8B2A6"))

        # Motor: flange face flush with the bottom of the belt plane, body extending rearward
        face_z = zb - BELT_W / 2
        asm.add(protos[spec[0]].translate((0, my, face_z)),
                name=f"motor_{name}", color=cq.Color("#2B2419"))
        asm.add(cq.Workplane("XY").workplane(offset=face_z).center(0, my)
                .circle(MOTOR_PULLEY_R).extrude(BELT_W),
                name=f"motor_pulley_{name}", color=cq.Color("#B8B2A6"))

    # Side screen panels: bezel plate with three round modules each
    for side, names in SCREEN_COLUMNS.items():
        cx = side * PANEL_X
        asm.add(box(cx - PANEL_W / 2, cx + PANEL_W / 2, -PANEL_H / 2, PANEL_H / 2, 0, PANEL_T),
                name=f"screen_panel_{'L' if side < 0 else 'R'}", color=cq.Color("#8C6A3A"))
        for k, name in enumerate(names):
            sy = (1 - k) * SCREEN_PITCH
            asm.add(cq.Workplane("XY").workplane(offset=PANEL_T).center(cx, sy)
                    .circle(SCREEN_OD / 2).extrude(SCREEN_T),
                    name=f"screen_board_{name}", color=cq.Color(COLORS[NAMES.index(name)]))
            asm.add(cq.Workplane("XY").workplane(offset=PANEL_T + SCREEN_T).center(cx, sy)
                    .circle(SCREEN_GLASS / 2).extrude(0.6),
                    name=f"screen_glass_{name}", color=cq.Color("#241D15"))

    return asm


if __name__ == "__main__":
    os.makedirs("cad_out", exist_ok=True)
    assembly = build()
    assembly.save("cad_out/weasley_clock.step")
    cq.exporters.export(assembly.toCompound(), "cad_out/weasley_clock.stl")
    for n in NAMES:
        print(f"{n:7s} hand angle {hand_angle_deg(n):6.1f} deg")
    print("wrote cad_out/weasley_clock.step, cad_out/weasley_clock.stl")

"""
Weasley Clock -- parametric CAD model (CadQuery).

Builds an assembly of the mechanical concept shown in visualize_hardware_plan.py:
  * round dial (z = 0 is the front surface of the dial, +z toward the viewer)
  * six nested concentric hollow shafts, one per person
  * one hand per shaft, rotated to the same angle birthday_clock.m computes
    (Haversine distance from home -> log10 map -> 0..180 deg clockwise from 12)
  * six stepper motors (2x NEMA23, 4x NEMA17) in a ring behind the dial, each
    with a pulley on its shaft's rear end (belt drive is not modelled)

All dimensions are PLACEHOLDERS for concept review, in millimetres -- verify
against real parts before machining anything.

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
    return math.log10(d) / math.log10(MAX_DIST) * 180.0


# ---------------------------------------------------------------------------
# Parameters (placeholders)
# ---------------------------------------------------------------------------
DIAL_R = 160.0
DIAL_T = 4.0
WALL = 1.0            # shaft wall thickness
OD0, OD_STEP = 26.0, 3.0   # outermost shaft OD, and reduction per nesting level
HAND_T = 2.5
HAND_Z0, HAND_DZ = 6.0, 3.5  # first hand height above dial, spacing between hands
HAND_LEN = [95, 105, 115, 125, 135, 145]   # tip radius per hand
# In-line drive: all six motors share one axis parallel to the clock axis,
# stacked behind each other, each driving its shaft by belt at its own depth.
MOTOR_STEP = "motors/17HS13-1504H.STEP"   # body z -33.8..0, 5 mm shaft to z=+16, leads out the back
MOTOR_BODY, MOTOR_SHAFT = 34.0, 16.0
MOTOR_OFFSET_Y = -65.0      # motor axis position relative to the clock axis
DEPTH0, DEPTH_STEP = 55.0, 55.0   # belt-plane depth behind dial; step > body + shaft so motors don't collide
BELT_W = 8.0
MOTOR_PULLEY_R = 8.0
# Round display modules (GC9A01 1.28in): 39.5 mm board, 32.4 mm active area.
# They sit on the left half of the dial, which the hands (0-180 deg clockwise) never sweep.
SCREEN_OD, SCREEN_GLASS, SCREEN_T = 39.5, 32.4, 3.0
SCREEN_RING_R = 115.0
SCREEN_ANGLES = [205 + 26 * k for k in range(6)]   # deg clockwise from 12 o'clock
COLORS = ["#4F7A4A", "#3E6470", "#A8672B", "#8A4B6B", "#B0562A", "#5B5EA6"]


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


def load_motor():
    """Vendor 17HS13-1504H STEP, trimmed of its lead wires (which run ~90 mm out the back)."""
    m = cq.importers.importStep(MOTOR_STEP)
    keep = (cq.Workplane("XY").workplane(offset=-MOTOR_BODY - 0.5)
            .rect(45, 45).extrude(MOTOR_BODY + 0.5 + MOTOR_SHAFT + 0.5))
    return m.intersect(keep)


def build():
    asm = cq.Assembly(name="weasley_clock")

    dial = (cq.Workplane("XY").workplane(offset=-DIAL_T).circle(DIAL_R)
            .circle(OD0 / 2 + 1.0).extrude(DIAL_T))
    asm.add(dial, name="dial", color=cq.Color("#F7F3EA"))

    motor_proto = load_motor()

    for i, name in enumerate(NAMES):
        zb = -(DEPTH0 + DEPTH_STEP * i)     # centre of this level's belt plane
        od = OD0 - OD_STEP * i
        od_next = od - OD_STEP if i < len(NAMES) - 1 else 0.0  # innermost hand has a solid hub
        hand_z = HAND_Z0 + HAND_DZ * i
        color = cq.Color(COLORS[i])

        # Hollow shaft from the belt pulley up to the hand's hub.
        # Outer shafts are shorter, so each hand sits on its own shaft end.
        asm.add(tube(od, WALL, zb - BELT_W / 2, hand_z), name=f"shaft_{name}", color=color)
        asm.add(hand(od, od_next, hand_z, HAND_LEN[i], hand_angle_deg(name)),
                name=f"hand_{name}", color=color)

        # Driven pulley on the shaft
        asm.add(cq.Workplane("XY").workplane(offset=zb - BELT_W / 2)
                .circle(od / 2 + 12).circle(od / 2).extrude(BELT_W),
                name=f"pulley_{name}", color=cq.Color("#B8B2A6"))

        # Motor: face flush with the bottom of the belt plane, body extending rearward
        face_z = zb - BELT_W / 2
        asm.add(motor_proto.translate((0, MOTOR_OFFSET_Y, face_z)),
                name=f"motor_{name}", color=cq.Color("#2B2419"))
        asm.add(cq.Workplane("XY").workplane(offset=face_z).center(0, MOTOR_OFFSET_Y)
                .circle(MOTOR_PULLEY_R).extrude(BELT_W),
                name=f"motor_pulley_{name}", color=cq.Color("#B8B2A6"))

        # Round display module on the dial's front face
        ang = math.radians(SCREEN_ANGLES[i])
        sx, sy = SCREEN_RING_R * math.sin(ang), SCREEN_RING_R * math.cos(ang)
        asm.add(cq.Workplane("XY").center(sx, sy).circle(SCREEN_OD / 2).extrude(SCREEN_T),
                name=f"screen_board_{name}", color=color)
        asm.add(cq.Workplane("XY").workplane(offset=SCREEN_T).center(sx, sy)
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

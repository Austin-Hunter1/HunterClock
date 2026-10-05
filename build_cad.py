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
MOTOR_RING_R = 90.0
# (depth behind dial of the shaft's rear end, frame size, body length)
LEVELS = [
    (55,  57.0, 56.0),   # NEMA23
    (105, 57.0, 56.0),   # NEMA23
    (155, 42.3, 40.0),   # NEMA17
    (205, 42.3, 40.0),
    (255, 42.3, 40.0),
    (310, 42.3, 40.0),
]
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


def build():
    asm = cq.Assembly(name="weasley_clock")

    dial = (cq.Workplane("XY").workplane(offset=-DIAL_T).circle(DIAL_R)
            .circle(OD0 / 2 + 1.0).extrude(DIAL_T))
    asm.add(dial, name="dial", color=cq.Color("#F7F3EA"))

    for i, name in enumerate(NAMES):
        depth, frame, body_len = LEVELS[i]
        od = OD0 - OD_STEP * i
        od_next = od - OD_STEP if i < len(NAMES) - 1 else 0.0  # innermost hand has a solid hub
        hand_z = HAND_Z0 + HAND_DZ * i
        color = cq.Color(COLORS[i])

        # Hollow shaft: from rear (pulley end) up through the hand's hub.
        # Outer shafts are shorter, so each hand sits on its own shaft end.
        shaft = tube(od, WALL, -depth, hand_z)
        asm.add(shaft, name=f"shaft_{name}", color=color)

        hnd = hand(od, od_next, hand_z, HAND_LEN[i], hand_angle_deg(name))
        asm.add(hnd, name=f"hand_{name}", color=color)

        # Pulley on the shaft's rear end
        pulley = (cq.Workplane("XY").workplane(offset=-depth - 8)
                  .circle(od / 2 + 12).circle(od / 2).extrude(8))
        asm.add(pulley, name=f"pulley_{name}", color=cq.Color("#B8B2A6"))

        # Motor offset radially (belt-driven), shaft axis parallel to z
        a = math.radians(60 * i)
        mx, my = MOTOR_RING_R * math.cos(a), MOTOR_RING_R * math.sin(a)
        motor = (cq.Workplane("XY").workplane(offset=-depth - body_len)
                 .center(mx, my).rect(frame, frame).extrude(body_len))
        asm.add(motor, name=f"motor_{name}", color=cq.Color("#2B2419"))

    return asm


if __name__ == "__main__":
    os.makedirs("cad_out", exist_ok=True)
    assembly = build()
    assembly.save("cad_out/weasley_clock.step")
    cq.exporters.export(assembly.toCompound(), "cad_out/weasley_clock.stl")
    for n in NAMES:
        print(f"{n:7s} hand angle {hand_angle_deg(n):6.1f} deg")
    print("wrote cad_out/weasley_clock.step, cad_out/weasley_clock.stl")

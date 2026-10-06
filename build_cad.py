"""
Weasley Clock -- parametric CAD model (CadQuery).

Assembly of the mechanical concept in the Hardware Plan:
  * grandfather-clock case outline (plinth, trunk, arched hood, swan-neck pediment)
  * six hollow-shaft steppers chained nose to tail along the clock axis behind the
    dial (2x NEMA23 23HS18-2004H in front, 4x NEMA17 17HS13-1504H behind), using the
    vendor STEP models; each motor's shaft passes through the hollow bores of the
    motors in front of it, carried by telescoping brass tubes
  * six hands at the angles birthday_clock.m computes
    (Haversine distance from home -> log10 map -> 0..360 deg clockwise from 12)
  * two side panels flanking the dial, three round GC9A01 screens each

Dimensions other than the vendor motors are PLACEHOLDERS for concept review, in
millimetres -- verify against real parts before machining anything.

Coordinates: +z toward the viewer, +y up, dial centre at the origin, dial front
surface at z = 0.

Outputs (in ./cad_out): weasley_clock.step, .stl, .glb (colored)
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
HAND_T = 2.5
HAND_DZ = 3.5                          # height step between hands
HAND_LEN = [70, 76, 82, 88, 94, 100]   # tip radius per level
PLATE_GAP = 10.0                       # clearance between the dial plate and the front motor flange

# Motors, from the vendor STEP files (z = 0 at the front mounting face, +z out the front).
# body: flange face to rear of body; front/rear: shaft tip distance from the face;
# shaft_od / bore: hollow-shaft dimensions; half: half the flange width.
NEMA23 = dict(path="motors/23HS18-2004H.STEP", body=47.0, front=25.0, rear=58.0, shaft_od=12.0, bore=8.0, half=28.6)
NEMA17 = dict(path="motors/17HS13-1504H.STEP", body=34.0, front=16.0, rear=43.0, shaft_od=8.0, bore=4.0, half=21.2)

# Level 1 is the front motor. Becky and Evan (most frequent movers) take the two NEMA23
# levels, Elijah the stiffest NEMA17 level, then Caleb, Austin, Micah (order not yet decided).
LEVELS = [("Becky", NEMA23), ("Evan", NEMA23), ("Elijah", NEMA17),
          ("Caleb", NEMA17), ("Austin", NEMA17), ("Micah", NEMA17)]
COUPLING_GAP = 4.0     # between one motor's rear shaft tip and the next motor's front shaft tip

# Telescoping brass tube OD per level (K&S, inches converted to mm). Level 1 needs none:
# its hand mounts on the motor-1 shaft.
TUBE_OD = [None, 7.14, 6.35, 3.18, 2.38, 1.59]
TUBE_WALL = 0.36       # .014 in

# Round display modules (GC9A01 1.28in, as ordered): 39.5 mm board, 32.4 mm glass.
# Two vertical panels flank the dial, three screens each (near cluster left, far right).
SCREEN_OD, SCREEN_GLASS, SCREEN_T = 39.5, 32.4, 3.0
PANEL_X, PANEL_W, PANEL_H, PANEL_T = 150.0, 56.0, 230.0, 6.0
SCREEN_PITCH = 70.0
SCREEN_COLUMNS = {-1: ["Becky", "Elijah", "Caleb"], +1: ["Austin", "Micah", "Evan"]}

# Grandfather-clock case outline, from the reference photos. The depth is set from the
# motor stack (about 420 mm), at the deep end of the Howard Miller range (343-527 mm).
FLOOR_Y = -1550.0
CASE_FRONT_Z, CASE_BACK_Z = 45.0, -470.0
CASE_WALL = 15.0
COLORS = {"Becky": "#4F7A4A", "Elijah": "#3E6470", "Caleb": "#A8672B",
          "Austin": "#8A4B6B", "Micah": "#B0562A", "Evan": "#5B5EA6"}
WALNUT = "#6B4426"
MOTOR_GREY = "#8C857A"


def tube(od, wall, z0, z1):
    return (cq.Workplane("XY").workplane(offset=z0)
            .circle(od / 2).circle(od / 2 - wall).extrude(z1 - z0))


def hand(hub_od, bore, z, length, angle_deg):
    """Tapered pointer with a hub, built pointing at 12 o'clock then rotated.

    bore is the clearance hole for the next-inner tube passing through the hub
    (0 for the innermost, solid hub).
    """
    w_base, w_tip = 12.0, 2.0
    arm = (cq.Workplane("XY").workplane(offset=z)
           .polyline([(-w_base / 2, 0), (w_base / 2, 0), (w_tip / 2, length), (-w_tip / 2, length)])
           .close().extrude(HAND_T))
    h = cq.Workplane("XY").workplane(offset=z).circle(hub_od / 2).extrude(HAND_T).union(arm)
    if bore:
        h = h.cut(cq.Workplane("XY").workplane(offset=z - 1).circle(bore / 2).extrude(HAND_T + 2))
    return h.rotate((0, 0, 0), (0, 0, 1), -angle_deg)  # clockwise from 12 o'clock


def load_motor(m):
    """Vendor STEP, trimmed of its lead wires (which run ~90+ mm out the back).

    Keeps the body block, the shaft along the axis (front and rear), and drops the rest.
    The hollow bore is preserved.
    """
    solid = cq.importers.importStep(m["path"])
    half = m["half"] + 0.5
    body = (cq.Workplane("XY").workplane(offset=-m["body"] - 0.3)
            .rect(2 * half, 2 * half).extrude(m["body"] + 0.3 + m["front"] + 0.5))
    rear = (cq.Workplane("XY").workplane(offset=-m["rear"] - 0.3)
            .circle(m["shaft_od"] / 2 + 0.5).extrude(m["rear"] - m["body"] + 1.0))
    return solid.intersect(body.union(rear))


def box(x0, x1, y0, y1, z0, z1):
    return (cq.Workplane("XY").workplane(offset=z0).center((x0 + x1) / 2, (y0 + y1) / 2)
            .rect(x1 - x0, y1 - y0).extrude(z1 - z0))


def open_front(solid):
    return solid.faces(">Z").shell(-CASE_WALL)


def add_case(asm):
    wood = cq.Color(WALNUT)
    depth = CASE_FRONT_Z - CASE_BACK_Z
    # plinth + trunk (hollow, open front)
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


def build():
    asm = cq.Assembly(name="weasley_clock")
    add_case(asm)

    # --- stack layout (all z positions, front to back) -------------------------------
    face = []              # flange face z of each motor
    z = -(DIAL_T + 4 + PLATE_GAP)       # motor 1 sits just behind the dial plate
    for i, (_, m) in enumerate(LEVELS):
        if i:
            prev = LEVELS[i - 1][1]
            z = face[-1] - prev["rear"] - COUPLING_GAP - m["front"]
        face.append(z)
    tip1 = face[0] + LEVELS[0][1]["front"]      # front tip of motor 1's shaft
    hand_z = [tip1 + HAND_DZ * i for i in range(6)]

    # --- dial and plate (holes for the front motor shaft) -----------------------------
    hole_r = LEVELS[0][1]["shaft_od"] / 2 + 2
    asm.add(cq.Workplane("XY").workplane(offset=-DIAL_T).circle(DIAL_R).circle(hole_r).extrude(DIAL_T),
            name="dial", color=cq.Color("#F7F3EA"))
    plate = (cq.Workplane("XY").workplane(offset=-DIAL_T - 4)
             .moveTo(-190, -190).lineTo(190, -190).lineTo(190, 130)
             .threePointArc((0, 310), (-190, 130)).close().extrude(4)
             .cut(cq.Workplane("XY").workplane(offset=-DIAL_T - 5).circle(hole_r).extrude(6)))
    asm.add(plate, name="dial_plate", color=cq.Color("#D9D2C0"))

    # --- motors, tubes and hands ------------------------------------------------------
    protos = {}
    for i, (name, m) in enumerate(LEVELS):
        protos.setdefault(m["path"], load_motor(m))
        asm.add(protos[m["path"]].translate((0, 0, face[i])), name=f"motor_{name}", color=cq.Color(MOTOR_GREY))

        color = cq.Color(COLORS[name])
        # tube from this motor's front shaft tip up to its hand (level 1: hand sits on the shaft)
        if TUBE_OD[i]:
            asm.add(tube(TUBE_OD[i], TUBE_WALL, face[i] + m["front"], hand_z[i]),
                    name=f"tube_{name}", color=color)
            hub_od = TUBE_OD[i] + 3
        else:
            hub_od = m["shaft_od"] + 3
        next_od = TUBE_OD[i + 1] if i + 1 < len(LEVELS) else 0.0
        asm.add(hand(hub_od, next_od + 0.3 if next_od else 0.0, hand_z[i], HAND_LEN[i], hand_angle_deg(name)),
                name=f"hand_{name}", color=color)

    # --- side screen panels: bezel plate with three round modules each ----------------
    for side, names in SCREEN_COLUMNS.items():
        cx = side * PANEL_X
        asm.add(box(cx - PANEL_W / 2, cx + PANEL_W / 2, -PANEL_H / 2, PANEL_H / 2, 0, PANEL_T),
                name=f"screen_panel_{'L' if side < 0 else 'R'}", color=cq.Color("#8C6A3A"))
        for k, name in enumerate(names):
            sy = (1 - k) * SCREEN_PITCH
            asm.add(cq.Workplane("XY").workplane(offset=PANEL_T).center(cx, sy)
                    .circle(SCREEN_OD / 2).extrude(SCREEN_T),
                    name=f"screen_board_{name}", color=cq.Color(COLORS[name]))
            asm.add(cq.Workplane("XY").workplane(offset=PANEL_T + SCREEN_T).center(cx, sy)
                    .circle(SCREEN_GLASS / 2).extrude(0.6),
                    name=f"screen_glass_{name}", color=cq.Color("#241D15"))

    last = LEVELS[-1][1]
    rear_tip = face[-1] - last["rear"]
    print(f"stack: front tip z={tip1:+.1f}, rear tip z={rear_tip:+.1f}, length {tip1 - rear_tip:.0f} mm")
    for (name, m), f in zip(LEVELS, face):
        print(f"  {name:7s} motor face z={f:+.1f}")
    return asm


if __name__ == "__main__":
    os.makedirs("cad_out", exist_ok=True)
    assembly = build()
    assembly.save("cad_out/weasley_clock.step")
    cq.exporters.export(assembly.toCompound(), "cad_out/weasley_clock.stl", tolerance=0.3, angularTolerance=0.3)
    assembly.save("cad_out/weasley_clock.glb", exportType="GLTF")
    for n in NAMES:
        print(f"{n:7s} hand angle {hand_angle_deg(n):6.1f} deg")
    print("wrote cad_out/weasley_clock.step, .stl, .glb")

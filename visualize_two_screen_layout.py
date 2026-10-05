"""
Weasley Clock -- front-of-cabinet mockup with the two-rectangular-screen
display architecture: the actual dial (6-shaft hub, hands at their real
computed angles) flanked by two vertical panels, each listing 3 people.

Uses the *360-degree* mapping now locked into birthday_clock.m (0 deg =
home/12 o'clock, sweeping the full circle rather than just the top half),
so hand positions here match the current design, not the earlier 0-180 one.
"""

import math
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.path import Path
import numpy as np

plt.rcParams["font.family"] = "serif"

# ---------------------------------------------------------------------------
# Data (mirrors birthday_clock.m, including the *360 mapping)
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
CITY = {"Becky": "Minnetonka, MN", "Elijah": "St. Paul, MN", "Caleb": "Wheaton, IL",
        "Austin": "Boulder, CO", "Micah": "Corvallis, OR", "Evan": "Warsaw, PL"}

R_EARTH = 6371.0
MAX_DIST = math.pi * R_EARTH


def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


distances = {n: max(haversine(*HOME, *c), 1.0) for n, c in PEOPLE.items()}
min_log, max_log = math.log10(1), math.log10(MAX_DIST)
angles_deg = {n: (math.log10(d) - min_log) / (max_log - min_log) * 360 for n, d in distances.items()}

NAMES = ["Becky", "Elijah", "Caleb", "Austin", "Micah", "Evan"]
PALETTE = ["#4F7A4A", "#3E6470", "#A8672B", "#8A4B6B", "#B0562A", "#5B5EA6"]
COLOR = dict(zip(NAMES, PALETTE))
LEFT_SCREEN = ["Becky", "Elijah", "Caleb"]     # near cluster (MN / IL)
RIGHT_SCREEN = ["Austin", "Micah", "Evan"]     # far cluster (CO / OR / PL)

INK = "#2B2419"
MUTED = "#8A7C68"
PAPER = "#F7F3EA"
WALNUT = "#5A3A22"
WALNUT_DK = "#4A2E1A"
BRASS = "#C9A876"
BRASS_DK = "#8C6A3A"
FACE = "#EDE4D2"
SCREEN_BG = "#241D15"


def polar_xy(cx, cy, r, angle_deg):
    """theta measured clockwise from straight up, matching the clock convention."""
    t = math.radians(angle_deg)
    return cx + r * math.sin(t), cy + r * math.cos(t)


fig, ax = plt.subplots(figsize=(12.5, 13.5), facecolor=PAPER)
ax.set_xlim(-6.3, 6.3)
ax.set_ylim(-0.8, 11.3)
ax.set_aspect("equal")
ax.axis("off")

fig.suptitle("The Weasley Clock — Face + Two-Screen Layout", fontsize=21, fontweight="bold",
             color=INK, y=0.985)
fig.text(0.5, 0.955, "Six hands share one hub at the center; each side panel lists three people, color-matched to their hand",
          ha="center", fontsize=11, color=MUTED, style="italic")

# --- Cabinet hood (walnut arch) --------------------------------------------
ax.add_patch(mpatches.Rectangle((-5.4, 0), 10.8, 8.0, facecolor=WALNUT, edgecolor="none", zorder=0))
ax.add_patch(mpatches.Wedge((0, 8.0), 5.4, 0, 180, facecolor=WALNUT, edgecolor="none", zorder=0))
ax.add_patch(mpatches.Rectangle((-5.4, 0), 10.8, 8.0, facecolor="none", edgecolor=WALNUT_DK, linewidth=3, zorder=1))
ax.add_patch(mpatches.Arc((0, 8.0), 10.8, 10.8, theta1=0, theta2=180, edgecolor=WALNUT_DK, linewidth=3, zorder=1))

# --- Center dial: hub + 6 hands ---------------------------------------------
DIAL_CX, DIAL_CY, DIAL_R = 0, 5.3, 2.35

ax.add_patch(mpatches.Circle((DIAL_CX, DIAL_CY), DIAL_R + 0.22, facecolor=BRASS_DK, edgecolor="none", zorder=2))
ax.add_patch(mpatches.Circle((DIAL_CX, DIAL_CY), DIAL_R + 0.1, facecolor=BRASS, edgecolor="none", zorder=2))
ax.add_patch(mpatches.Circle((DIAL_CX, DIAL_CY), DIAL_R, facecolor=FACE, edgecolor=INK, linewidth=1.3, zorder=3))

# hour ticks + numerals, all the way around (the *360 mapping uses the full face)
for h in range(12):
    ang = h * 30
    x1, y1 = polar_xy(DIAL_CX, DIAL_CY, DIAL_R - 0.12, ang)
    x2, y2 = polar_xy(DIAL_CX, DIAL_CY, DIAL_R - 0.02, ang)
    ax.plot([x1, x2], [y1, y2], color=INK, lw=1.6, zorder=4)
    lx, ly = polar_xy(DIAL_CX, DIAL_CY, DIAL_R - 0.42, ang)
    label = 12 if h == 0 else h
    ax.text(lx, ly, str(label), ha="center", va="center", fontsize=11, color=BRASS_DK, fontweight="bold", zorder=4)

# hub boss
ax.add_patch(mpatches.Circle((DIAL_CX, DIAL_CY), 0.16, facecolor=BRASS_DK, edgecolor=INK, linewidth=0.8, zorder=6))

radii = np.linspace(DIAL_R * 0.42, DIAL_R * 0.88, len(NAMES))
for name, r in zip(NAMES, radii):
    x, y = polar_xy(DIAL_CX, DIAL_CY, r, angles_deg[name])
    c = COLOR[name]
    ax.plot([DIAL_CX, x], [DIAL_CY, y], color=c, lw=2.6, solid_capstyle="round", zorder=5)
    ax.plot(x, y, "o", color=c, markersize=6, zorder=6)

ax.text(DIAL_CX, DIAL_CY - DIAL_R - 0.62, "single 6-shaft hub, original dial rebuilt around it",
         ha="center", fontsize=8.5, color=MUTED, style="italic")


# --- Side screens ------------------------------------------------------------
def side_screen(cx, names, label):
    w, h = 1.9, 5.6
    ax.add_patch(mpatches.FancyBboxPatch((cx - w / 2, DIAL_CY - h / 2), w, h,
                                          boxstyle="round,pad=0.02,rounding_size=0.12",
                                          facecolor=BRASS_DK, edgecolor="none", zorder=2))
    ax.add_patch(mpatches.FancyBboxPatch((cx - w / 2 + 0.08, DIAL_CY - h / 2 + 0.08), w - 0.16, h - 0.16,
                                          boxstyle="round,pad=0.02,rounding_size=0.1",
                                          facecolor=SCREEN_BG, edgecolor=BRASS, linewidth=1.2, zorder=3))
    cell_h = (h - 0.32) / 3
    top = DIAL_CY + h / 2 - 0.16
    for i, name in enumerate(names):
        cy_cell = top - cell_h * (i + 0.5)
        c = COLOR[name]
        dist = distances[name]
        status = "HOME" if dist <= 1.5 else f"{dist:,.0f} km"
        if i > 0:
            ax.plot([cx - w / 2 + 0.18, cx + w / 2 - 0.18], [top - cell_h * i, top - cell_h * i],
                     color="#3A3226", lw=0.8, zorder=4)
        ax.add_patch(mpatches.Circle((cx, cy_cell + 0.42), 0.34, facecolor=c, alpha=0.75, edgecolor="none", zorder=4))
        ax.text(cx, cy_cell + 0.03, name, ha="center", va="center", fontsize=11, color="#F3E8D2",
                 fontweight="bold", zorder=4)
        ax.text(cx, cy_cell - 0.24, status, ha="center", va="center", fontsize=8.5, color=c,
                 fontweight="bold", zorder=4)
        ax.text(cx, cy_cell - 0.44, CITY[name], ha="center", va="center", fontsize=6.8, color=MUTED,
                 style="italic", zorder=4)
    ax.text(cx, DIAL_CY - h / 2 - 0.3, label, ha="center", fontsize=8.5, color=BRASS, style="italic")


side_screen(-4.05, LEFT_SCREEN, "near cluster")
side_screen(4.05, RIGHT_SCREEN, "far cluster")

ax.text(0, 0.35, "cabinet trunk continues below (hub electronics, pendulum removed)",
         ha="center", fontsize=8.5, color="#C9BBA0", style="italic")

fig.subplots_adjust(left=0.02, right=0.98, top=0.93, bottom=0.02)
fig.savefig("weasley_clock_face_and_screens.png", dpi=160, facecolor=PAPER)
print("wrote weasley_clock_face_and_screens.png")

for n in NAMES:
    print(f"{n:8s} {distances[n]:>8.0f} km  angle={angles_deg[n]:6.1f} deg")

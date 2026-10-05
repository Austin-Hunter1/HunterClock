"""
Weasley Clock -- hardware plan visualization.

Two figures:
  1. weasley_clock_overview.png  - system architecture, hub cross-section, front clock face
  2. weasley_clock_displays.png  - mockup of the round per-person "moving portrait" screens

Distances are computed with the same Haversine + log10 mapping used in birthday_clock.m,
so the angles/labels shown here match the physical design, not placeholder numbers.
"""

import math
from datetime import datetime

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.path import Path
import numpy as np

plt.rcParams["font.family"] = "serif"

# ---------------------------------------------------------------------------
# 1. Distance data (mirrors birthday_clock.m)
# ---------------------------------------------------------------------------
HOME = (44.9109, -93.5017)  # Minnetonka, MN

PEOPLE = {
    "Becky":  (44.9109, -93.5017),   # near home / Minnetonka HS
    "Elijah": (44.9537, -93.0900),   # St. Paul, MN
    "Caleb":  (41.8661, -88.1070),   # Wheaton, IL
    "Austin": (40.0150, -105.2705),  # Boulder, CO
    "Micah":  (44.5646, -123.2620),  # Corvallis, OR
    "Evan":   (52.2297, 21.0122),    # Warsaw, Poland
}
CITY_LABEL = {
    "Becky": "Minnetonka, MN", "Elijah": "St. Paul, MN", "Caleb": "Wheaton, IL",
    "Austin": "Boulder, CO", "Micah": "Corvallis, OR", "Evan": "Warsaw, PL",
}

R_EARTH = 6371.0
MAX_DIST = math.pi * R_EARTH


def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


distances = {n: max(haversine(*HOME, *c), 1.0) for n, c in PEOPLE.items()}
min_log, max_log = math.log10(1), math.log10(MAX_DIST)
# 0 deg = 12 o'clock (home), 180 deg = 6 o'clock (antipode) -- the documented design.
angles_deg = {n: (math.log10(d) - min_log) / (max_log - min_log) * 180 for n, d in distances.items()}

NAMES = ["Becky", "Elijah", "Caleb", "Austin", "Micah", "Evan"]
PALETTE = ["#4F7A4A", "#3E6470", "#A8672B", "#8A4B6B", "#B0562A", "#5B5EA6"]
COLOR = dict(zip(NAMES, PALETTE))

INK = "#2B2419"
MUTED = "#8A7C68"
PAPER = "#F7F3EA"
SURFACE = "#FFFFFF"
LINE = "#DED2BC"
ACCENT = "#A8672B"


# ---------------------------------------------------------------------------
# 2. Figure 1: system overview
# ---------------------------------------------------------------------------
fig = plt.figure(figsize=(15, 11), facecolor=PAPER)
gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.6], hspace=0.38, wspace=0.28,
                       left=0.05, right=0.97, top=0.90, bottom=0.10)

fig.suptitle("The Weasley Clock — System Overview", fontsize=22, fontweight="bold",
             color=INK, y=0.965)
fig.text(0.5, 0.925, "Location → distance → hand angle, plus the display pipeline that runs beside it",
          ha="center", fontsize=11.5, color=MUTED, style="italic")

# --- Panel A: architecture flow (top, full width) -------------------------
ax_arch = fig.add_subplot(gs[0, :])
ax_arch.set_xlim(0, 10)
ax_arch.set_ylim(0, 3)
ax_arch.axis("off")


def box(ax, x, y, w, h, text, fc=SURFACE, ec=ACCENT, fontsize=9.5, tcolor=INK):
    p = mpatches.FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                 linewidth=1.4, edgecolor=ec, facecolor=fc, zorder=3)
    ax.add_patch(p)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fontsize,
             color=tcolor, wrap=True, zorder=4)
    return (x, y, w, h)


def arrow(ax, b1, b2, side1="right", side2="left", color=MUTED, style="-|>"):
    x1, y1, w1, h1 = b1
    x2, y2, w2, h2 = b2
    pts = {"right": (x1 + w1, y1 + h1 / 2), "left": (x2, y2 + h2 / 2),
           "bottom": (x1 + w1 / 2, y1), "top": (x2 + w2 / 2, y2 + h2)}
    p1 = pts[side1] if side1 in ("right", "left") else (x1 + w1 / 2, y1)
    p2 = pts[side2] if side2 in ("right", "left") else (x2 + w2 / 2, y2 + h2)
    ax.annotate("", xy=p2, xytext=p1,
                arrowprops=dict(arrowstyle=style, color=color, lw=1.6, shrinkA=2, shrinkB=2), zorder=2)


ax_arch.set_xlim(0, 10.6)
b_phone = box(ax_arch, 0.2, 1.1, 1.7, 0.8, "Family phones\nFind My / OwnTracks")
b_ha    = box(ax_arch, 2.3, 1.1, 1.5, 0.8, "Home Assistant\nperson tracker")
b_mqtt  = box(ax_arch, 4.2, 1.1, 1.6, 0.8, "MQTT broker\nMosquitto")
b_pi    = box(ax_arch, 6.2, 1.1, 1.7, 0.8, "Controller\nRaspberry Pi 4", ec=INK)
b_pico  = box(ax_arch, 8.4, 1.9, 1.9, 0.75, "Motor MCU\nPico + PIO", fc="#EFE3CE")
b_hub   = box(ax_arch, 8.4, 0.35, 1.9, 0.75, "6× ESP32\ndisplay nodes", fc="#EFE3CE")

arrow(ax_arch, b_phone, b_ha)
arrow(ax_arch, b_ha, b_mqtt)
arrow(ax_arch, b_mqtt, b_pi)
arrow(ax_arch, b_pi, b_pico)
arrow(ax_arch, b_pi, b_hub)
ax_arch.text(6.2 + 1.7 / 2, 0.92, "USB serial", ha="center", fontsize=8, color=MUTED, style="italic")
ax_arch.text(8.4 + 1.9 / 2, 2.72, "drives the 6-shaft hub", ha="center", fontsize=8, color=MUTED, style="italic")
ax_arch.text(8.4 + 1.9 / 2, 0.19, "WiFi / MQTT — no wiring to Pi", ha="center", fontsize=8, color=MUTED, style="italic")

# --- Panel B: hub cross-section (bottom-left) ------------------------------
ax_hub = fig.add_subplot(gs[1, 0])
ax_hub.set_facecolor(SURFACE)
ax_hub.set_xlim(-30, 340)
ax_hub.set_ylim(-155, 165)
ax_hub.set_title("Hub cross-section (side view, schematic)", fontsize=12.5, fontweight="bold", color=INK, pad=10)
ax_hub.set_xlabel("depth behind the original dial (mm)  —  not to scale", fontsize=8.5, color=MUTED, labelpad=8)
ax_hub.set_yticks([])
for s in ("top", "right", "left"):
    ax_hub.spines[s].set_visible(False)
ax_hub.axvline(0, color=INK, lw=2)
ax_hub.text(0, 150, "face", ha="center", fontsize=9, color=INK, fontweight="bold")

# 6 nesting levels: motor position (depth), frame, standoff (visual y-offset), person
levels = [
    (1, 55,  "NEMA23", 112),
    (2, 105, "NEMA23", 67),
    (3, 155, "NEMA17", 22),
    (4, 205, "NEMA17", -22),
    (5, 255, "NEMA17", -67),
    (6, 310, "NEMA17", -112),
]
order = NAMES  # illustrative assignment only -- unresolved per the open-decisions list
for (lvl, depth, frame, y), name in zip(levels, order):
    c = COLOR[name]
    tube_w = 7 - lvl * 0.7
    ax_hub.plot([0, depth], [y, y], color=c, lw=tube_w, solid_capstyle="butt", zorder=2, alpha=0.9)
    motor_w = 34 if frame == "NEMA23" else 26
    motor_h = 34 if frame == "NEMA23" else 26
    ax_hub.add_patch(mpatches.FancyBboxPatch((depth - motor_w / 2, y - motor_h / 2), motor_w, motor_h,
                                              boxstyle="round,pad=0.01,rounding_size=3",
                                              linewidth=1.2, edgecolor=INK, facecolor="#EFE3CE", zorder=3))
    ax_hub.text(depth, y - motor_h / 2 - 11, frame, ha="center", fontsize=6.8, color=MUTED)
    ax_hub.text(-10, y, name, ha="right", va="center", fontsize=8.5, color=c, fontweight="bold")

ax_hub.text(330, -140, "4× NEMA17 (inner)", ha="right", fontsize=8, color=MUTED)
ax_hub.text(10, 140, "2× NEMA23 (outer)", ha="left", fontsize=8, color=MUTED)

# --- Panel C: front clock face (bottom-right, polar) -----------------------
ax_face = fig.add_subplot(gs[1, 1], projection="polar")
ax_face.set_facecolor(SURFACE)
ax_face.set_theta_zero_location("N")
ax_face.set_theta_direction(-1)
ax_face.set_thetamin(0)
ax_face.set_thetamax(360)
ax_face.set_ylim(0, 1.25)
ax_face.set_rticks([])
ax_face.set_xticks(np.deg2rad([0, 30, 60, 90, 120, 150, 180]))
ax_face.set_xticklabels(["12", "1", "2", "3", "4", "5", "6"], fontsize=10, color=INK)
ax_face.spines["polar"].set_color(LINE)
ax_face.grid(color=LINE, alpha=0.6)
ax_face.set_title("Front face — all 6 hands, live angles", fontsize=12.5, fontweight="bold", color=INK, pad=18)

radii = np.linspace(0.5, 1.0, len(NAMES))
for name, r in zip(NAMES, radii):
    theta = math.radians(angles_deg[name])
    c = COLOR[name]
    ax_face.plot([0, theta], [0, r], "-o", color=c, lw=2.4, markersize=6, markerfacecolor=c)
    ax_face.text(theta, r + 0.12, f"{name}\n{distances[name]:,.0f} km", ha="center", va="center",
                 fontsize=8, color=c, fontweight="bold")

fig.text(0.5, 0.025,
          f"Distances via Haversine from Minnetonka, MN · log₁₀ scale, 1 km → 12:00, "
          f"{MAX_DIST:,.0f} km (antipode) → 6:00",
          ha="center", fontsize=8.5, color=MUTED)

fig.savefig("weasley_clock_overview.png", dpi=160, facecolor=PAPER)
print("wrote weasley_clock_overview.png")


# ---------------------------------------------------------------------------
# 3. Figure 2: side-screen ("moving portrait") mockups
# ---------------------------------------------------------------------------
def draw_portrait_bust(ax, cx, cy, r, tone):
    """Abstract placeholder for a looping portrait animation -- no real photos."""
    head = mpatches.Circle((cx, cy + r * 0.12), r * 0.34, facecolor=tone, edgecolor="none", zorder=2, alpha=0.9)
    verts = [(cx - r * 0.62, cy - r * 0.62), (cx + r * 0.62, cy - r * 0.62),
              (cx + r * 0.4, cy - r * 0.1), (cx - r * 0.4, cy - r * 0.1), (cx - r * 0.62, cy - r * 0.62)]
    shoulders = mpatches.PathPatch(Path(verts), facecolor=tone, edgecolor="none", zorder=2, alpha=0.9)
    ax.add_patch(shoulders)
    ax.add_patch(head)


def distance_ring(ax, cx, cy, r, frac, color):
    """Thin arc gauge echoing the physical hand's log-distance position.

    Gap sits at the bottom (centered on -90 deg) so it never crosses the
    name/status text mounted below the portrait; the ring sweeps the long
    way around (counter-clockwise from the right edge of the gap) so the
    filled portion always starts adjacent to the gap.
    """
    gap_half = math.radians(24)
    start = -math.pi / 2 + gap_half
    end = start + (2 * math.pi - 2 * gap_half)
    theta = np.linspace(start, end, 200)
    ax.plot(cx + r * np.cos(theta), cy + r * np.sin(theta), color=LINE, lw=4, solid_capstyle="round", zorder=3)
    t2 = np.linspace(start, start + frac * (end - start), max(int(200 * frac), 2))
    ax.plot(cx + r * np.cos(t2), cy + r * np.sin(t2), color=color, lw=4, solid_capstyle="round", zorder=4)
    mx, my = cx + r * np.cos(t2[-1]), cy + r * np.sin(t2[-1])
    ax.plot(mx, my, "o", color=color, markersize=7, zorder=5)


STATES = [
    dict(name="Becky", city="Minnetonka, MN", dist=distances["Becky"], tone="#C9A876",
         status="HOME", updated="just now"),
    dict(name="Caleb", city="Wheaton, IL", dist=distances["Caleb"], tone="#B98A6A",
         status=f"{distances['Caleb']:,.0f} km away", updated="2 min ago"),
    dict(name="Evan", city="Warsaw, PL", dist=distances["Evan"], tone="#8C6E58",
         status=f"{distances['Evan']:,.0f} km away", updated="41 min ago"),
]

fig2, axes = plt.subplots(1, 3, figsize=(13.5, 6.1), facecolor=PAPER)
fig2.suptitle("Per-Person Side Screen — Round Display Concept", fontsize=19, fontweight="bold", color=INK, y=0.98)
fig2.text(0.5, 0.90, "Same round GC9A01 module and layout for all 6 — only the portrait, distance ring, and status text change",
           ha="center", fontsize=10.5, color=MUTED, style="italic")

for ax, s in zip(axes, STATES):
    ax.set_xlim(-1.35, 1.35)
    ax.set_ylim(-1.35, 1.55)
    ax.set_aspect("equal")
    ax.axis("off")

    c = COLOR[s["name"]]
    frac = (math.log10(max(s["dist"], 1)) - min_log) / (max_log - min_log)
    ring_color = "#4F7A4A" if s["status"] == "HOME" else ("#B0562A" if frac > 0.55 else "#C9932E")

    # brass bezel
    ax.add_patch(mpatches.Circle((0, 0), 1.18, facecolor="none", edgecolor="#8C6A3A", linewidth=10, zorder=1))
    ax.add_patch(mpatches.Circle((0, 0), 1.18, facecolor="none", edgecolor="#D9B26B", linewidth=2, zorder=1))
    # screen
    ax.add_patch(mpatches.Circle((0, 0), 1.02, facecolor="#241D15", edgecolor="none", zorder=1))
    # vignette
    ax.add_patch(mpatches.Circle((0, 0.05), 0.98, facecolor="#2E2419", edgecolor="none", zorder=1, alpha=0.6))

    # Everything below is drawn INSIDE the round screen (radius 1.02) --
    # a real round TFT has nothing to show past its own glass.
    draw_portrait_bust(ax, 0, 0.32, 0.42, s["tone"])
    distance_ring(ax, 0, 0.02, 0.92, min(max(frac, 0.02), 1.0), ring_color)

    ax.text(0, -0.34, s["name"], ha="center", va="center", fontsize=14, color="#F3E8D2", fontweight="bold")
    ax.text(0, -0.56, s["status"], ha="center", va="center", fontsize=9.5, color=ring_color, fontweight="bold")
    ax.text(0, -0.74, f"updated {s['updated']}", ha="center", va="center", fontsize=7, color=MUTED)
    ax.text(0, 1.42, s["city"], ha="center", va="center", fontsize=9.5, color=MUTED, style="italic")

fig2.subplots_adjust(left=0.03, right=0.97, top=0.80, bottom=0.06, wspace=0.15)
fig2.savefig("weasley_clock_displays.png", dpi=160, facecolor=PAPER)
print("wrote weasley_clock_displays.png")

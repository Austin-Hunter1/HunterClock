"""Figures for the design PDF (run from the repo root: python docs/make_figures.py)."""
import math

import matplotlib.pyplot as plt
import numpy as np

HOME = (44.9109, -93.5017)
PEOPLE = {"Becky": (44.9109, -93.5017), "Elijah": (44.9537, -93.0900), "Caleb": (41.8661, -88.1070),
          "Austin": (40.0150, -105.2705), "Micah": (44.5646, -123.2620), "Evan": (52.2297, 21.0122)}
COLOR = {"Becky": "#4F7A4A", "Elijah": "#3E6470", "Caleb": "#A8672B",
         "Austin": "#8A4B6B", "Micah": "#B0562A", "Evan": "#5B5EA6"}
R = 6371.0
MAXD = math.pi * R


def hav(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def angle(d):
    return math.log10(max(d, 1.0)) / math.log10(MAXD) * 360.0


INK, MUTED, LINE = "#2B2419", "#7A6E5C", "#D9CFBD"
plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": INK, "axes.labelcolor": INK,
                     "xtick.color": MUTED, "ytick.color": MUTED})

fig, ax = plt.subplots(figsize=(8.6, 4.9), facecolor="white")
d = np.logspace(0, math.log10(MAXD), 400)
ax.plot(d, [angle(x) for x in d], color=INK, lw=1.6, zorder=2)
ax.set_xscale("log")
ax.set_xlim(0.7, MAXD * 1.6)
ax.set_ylim(0, 360)
ax.set_yticks(range(0, 361, 60))
ax.set_xlabel("Distance from home (km, log scale)")
ax.set_ylabel("Hand angle (degrees clockwise from 12)")
ax.grid(True, which="major", color=LINE, lw=0.7)
ax.grid(True, which="minor", axis="x", color=LINE, lw=0.3, alpha=0.6)
ax.set_axisbelow(True)
for s in ("top",):
    ax.spines[s].set_visible(False)

# right axis: the same angle read as a clock time (30 degrees per hour)
sec = ax.secondary_yaxis("right", functions=(lambda a: a / 30.0, lambda h: h * 30.0))
sec.set_yticks([0, 2, 4, 6, 8, 10, 12])
sec.set_yticklabels(["12", "2", "4", "6", "8", "10", "12"])
sec.set_ylabel("Same angle as a clock reading (o'clock)")

for name, c in PEOPLE.items():
    dist = max(hav(*HOME, *c), 1.0)
    a = angle(dist)
    ax.axvline(dist, color=COLOR[name], lw=1.6, ls="--", zorder=1)
    ax.plot([dist], [a], "o", color=COLOR[name], ms=7, zorder=3, mec="white", mew=1)
    label = f"{name} \u00b7 home \u00b7 0\u00b0" if name == "Becky" else f"{name} \u00b7 {dist:,.0f} km \u00b7 {a:.0f}\u00b0"
    ax.text(dist * 1.07, 6, label, rotation=90, va="bottom", ha="left", fontsize=8, color=COLOR[name], fontweight="bold")

ax.text(0.02, 0.96, "Scale ends at 20,015 km (the antipode) = 360\u00b0", transform=ax.transAxes, ha="left", va="top", fontsize=8, color=MUTED)
fig.tight_layout()
fig.savefig("docs/img/distance_vs_angle.png", dpi=200)
print("wrote docs/img/distance_vs_angle.png")

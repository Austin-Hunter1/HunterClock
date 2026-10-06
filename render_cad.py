"""Render preview PNGs of cad_out/weasley_clock.stl (no CAD libs needed).

STL coordinates: +z toward the viewer, +y up. Matplotlib plots (x, -z, y) so the
front of the clock faces the camera at azim=-90.
"""
import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

raw = open("cad_out/weasley_clock.stl", "rb").read()
n = int(np.frombuffer(raw[80:84], "<u4")[0])
rec = np.frombuffer(raw[84:84 + n * 50], dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
tris = rec["v"][:, :, [0, 2, 1]] * np.array([1, -1, 1])
norms = rec["n"][:, [0, 2, 1]] * np.array([1, -1, 1])
light = np.array([0.3, -0.6, 0.7]); light /= np.linalg.norm(light)
shade = 0.35 + 0.65 * np.clip(norms @ light, 0, 1)
cols = np.c_[shade * 0.85, shade * 0.72, shade * 0.5, np.ones(n)]

# (title, elev, azim, y-range of geometry to keep (clock-up axis = plot z))
VIEWS = [("Full case, front", 3, -90, None),
         ("Hood close-up, front 3/4", 18, -65, (-450, 600)),
         ("Drive column, front 3/4 (hood hidden)", 12, -50, (-1100, -250))]

fig = plt.figure(figsize=(20, 9), facecolor="#F7F3EA")
for k, (title, elev, azim, yr) in enumerate(VIEWS):
    ax = fig.add_subplot(1, 3, k + 1, projection="3d")
    keep = np.ones(n, bool)
    if yr:
        cy = tris[:, :, 2].mean(1)
        keep = (cy > yr[0]) & (cy < yr[1])
    t, c = tris[keep], cols[keep]
    ax.add_collection3d(Poly3DCollection(t, facecolors=c, linewidths=0))
    pts = t.reshape(-1, 3)
    lo, hi = pts.min(0), pts.max(0)
    ctr, r = (lo + hi) / 2, (hi - lo).max() / 2
    ax.set_xlim(ctr[0] - r, ctr[0] + r); ax.set_ylim(ctr[1] - r, ctr[1] + r); ax.set_zlim(ctr[2] - r, ctr[2] + r)
    ax.set_box_aspect((1, 1, 1)); ax.view_init(elev, azim); ax.set_axis_off(); ax.set_title(title)
fig.savefig("cad_out/weasley_clock_preview.png", dpi=110, facecolor=fig.get_facecolor(), bbox_inches="tight")

"""Render preview PNGs of cad_out/weasley_clock.stl (no CAD libs needed)."""
import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

raw = open("cad_out/weasley_clock.stl", "rb").read()
if raw[:5] == b"solid" and b"facet" in raw[:300]:
    raise SystemExit("ASCII STL not supported")
n = int(np.frombuffer(raw[80:84], "<u4")[0])
rec = np.frombuffer(raw[84:84 + n * 50], dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
tris, norms = rec["v"], rec["n"]
light = np.array([0.4, -0.5, 0.8]); light /= np.linalg.norm(light)
shade = 0.35 + 0.65 * np.clip(norms @ light, 0, 1)
cols = np.c_[shade * 0.85, shade * 0.72, shade * 0.5, np.ones(n)]

fig = plt.figure(figsize=(16, 7), facecolor="#F7F3EA")
for k, (elev, azim, title) in enumerate([(25, -60, "Front 3/4"), (15, 150, "Rear 3/4 (motors)")]):
    ax = fig.add_subplot(1, 2, k + 1, projection="3d")
    ax.add_collection3d(Poly3DCollection(tris, facecolors=cols, linewidths=0))
    lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0)
    c, r = (lo + hi) / 2, (hi - lo).max() / 2
    ax.set_xlim(c[0] - r, c[0] + r); ax.set_ylim(c[1] - r, c[1] + r); ax.set_zlim(c[2] - r, c[2] + r)
    ax.set_box_aspect((1, 1, 1)); ax.view_init(elev, azim); ax.set_axis_off(); ax.set_title(title)
fig.savefig("cad_out/weasley_clock_preview.png", dpi=130, facecolor=fig.get_facecolor(), bbox_inches="tight")

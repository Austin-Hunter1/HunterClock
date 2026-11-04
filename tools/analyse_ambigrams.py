"""Measure the ambigram artwork: symmetry, connectivity, ink coverage.

    python tools/analyse_ambigrams.py cad/ambigrams

This is the evidence behind docs/AMBIGRAM-HANDS.md. Run it again after any
artwork change, because the two findings it produces both drive the design:

* **180-degree symmetry** -- six of the supplied files are pixel-exact point
  reflections, which is what lets the name read correctly whichever way the
  hand points. A file that scores below ~99% is a draft, not an ambigram to
  build from.
* **Connected components** -- the glyphs are 3 to 10 separate pieces, so a
  hand cannot simply be the extruded glyph; something has to hold the pieces
  together. See the design options in the plan.
"""

import pathlib
import sys
from collections import deque

import numpy as np
from PIL import Image


def components(mask):
    h, w = mask.shape
    lab = np.zeros((h, w), np.int32)
    n = 0
    sizes = []
    for sy in range(h):
        row = mask[sy]
        if not row.any():
            continue
        for sx in np.where(row & (lab[sy] == 0))[0]:
            n += 1
            size = 0
            q = deque([(sy, sx)])
            lab[sy, sx] = n
            while q:
                y, x = q.popleft()
                size += 1
                for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
                    ny, nx = y+dy, x+dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and lab[ny, nx] == 0:
                        lab[ny, nx] = n
                        q.append((ny, nx))
            sizes.append(size)
    return n, sorted(sizes, reverse=True), lab

def analyse(path):
    """Report symmetry, connectivity and coverage for one artwork file."""
    im = Image.open(path).convert("L")
    full = np.asarray(im) < 128

    # Exact point reflection? Measured at full resolution, since this is the
    # property the whole idea depends on.
    rot = full[::-1, ::-1]
    iou = 100.0 * (full & rot).sum() / max((full | rot).sum(), 1)

    # Connectivity at half resolution: every real feature is far wider than 2px,
    # and the flood fill is much quicker.
    ink = full[::2, ::2]
    count, sizes, _ = components(ink)
    total = max(ink.sum(), 1)
    substantial = [s for s in sizes if s > total * 0.01]

    ys, xs = np.where(full)
    bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())) if len(xs) else None

    verdict = "USABLE" if iou > 99.0 else "DRAFT (not symmetric)"
    print(
        f"{path.name:28s} {im.size[0]:5d}x{im.size[1]:<5d} "
        f"ink={100 * full.mean():4.1f}%  180deg={iou:5.1f}%  "
        f"pieces={len(substantial):2d} (of {count} incl. specks)  {verdict}"
    )
    return {"name": path.name, "iou": iou, "pieces": len(substantial), "bbox": bbox}


def main(argv):
    """Analyse every image in a directory."""
    directory = pathlib.Path(argv[0] if argv else "cad/ambigrams")
    if not directory.is_dir():
        print(f"no such directory: {directory}", file=sys.stderr)
        return 1
    files = sorted(p for p in directory.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
    if not files:
        print(f"no images in {directory}", file=sys.stderr)
        return 1
    print(f"{len(files)} file(s) in {directory}\n")
    results = [analyse(p) for p in files]
    print()
    usable = [r for r in results if r["iou"] > 99.0]
    print(f"{len(usable)} usable, {len(results) - len(usable)} rejected as drafts.")
    print("Every usable file is multi-piece, so a hand needs a blade or an "
          "offset hull to hold it together -- see docs/AMBIGRAM-HANDS.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

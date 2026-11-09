# The clock face, measured

`Clock_Face_1.3mf` is Austin's dial, as drawn. Everything below was measured
from it with `tools/check_face.py` and the geometry extraction in
`cad_out/face-check`; nothing here is assumed.

```bash
python tools/check_face.py cad/face/Clock_Face_1.3mf
```

## What it is

A break-arch longcase dial plate, drawn in inches and exported in millimetres.

| | |
|---|---|
| Plate | 279.4 × 393.6 mm (**11.00 × 15.50 in**) |
| Thickness | 2.381 mm (**3/32 in**) |
| Base sheet | 1.587 mm (**1/16 in**) |
| Dial disc | 247.65 mm diameter (**9.75 in**), raised 0.794 mm (1/32 in) |
| Dial centre | exactly (0, 0) in the model |
| Mesh | 1236 faces, watertight |

Four openings, all on the vertical centreline:

| Diameter | Position | What it is |
|---|---|---|
| **11.11 mm** (7/16 in) | (0, 0) | the dial centre — the hand arbor |
| 4.76 mm (3/16 in) | (0, +36.5) | above centre |
| 9.52 mm (3/8 in) | (0, −41.3) | below centre |
| 99.4 mm, shaped | (0, +180.5) | the decorative arch cut-out |

## Three findings

### 1. The centre hole is too small — 11.11 mm against a 12 mm shaft

Level 1's hand mounts on motor 1's hollow shaft, which is **12.0 mm** across
(`build_cad.py`, `NEMA23["shaft_od"]`). That shaft has to pass through the dial
to reach the hand in front of it. It will not go through 11.11 mm.

**Open the centre hole to 12.7 mm (1/2 in).** That is a standard drill, leaves
0.7 mm of clearance, and is the only change the dial actually needs.

Nothing else passes through: every telescoping tube (7.14 mm and smaller) fits
the existing hole, and the hubs all sit in front of the plate.

### 2. The hands are short for this dial

They were sized against `build_cad.py`'s placeholder 110 mm dial. The real disc
is **123.8 mm**, so the longest hand stops 24 mm short and leaves a bare ring.

Compare `dial-hands-current.svg` with `dial-hands-scaled.svg`. Scaled runs
85–115 mm instead of 70–100 and still leaves 9 mm of chapter ring. One change
in `build_hands.py`:

```python
HandSpec(1, "Becky",  "Mom_ambigram.png",     "Mom",     12.00,  85, "#4F7A4A"),
HandSpec(2, "Evan",   "Dad_ambigram.png",     "Dad",      7.14,  91, "#5B5EA6"),
HandSpec(3, "Elijah", "Elijah_ambigram2.png", "Elijah",   6.35,  97, "#3E6470"),
HandSpec(4, "Caleb",  "Caleb_ambigram.png",   "Caleb",    3.18, 103, "#A8672B"),
HandSpec(5, "Austin", "Austin_ambigram.png",  "Austin",   2.38, 109, "#8A4B6B"),
HandSpec(6, "Micah",  "Micah_ambigram.png",   "Micah",    1.59, 115, "#B0562A"),
```

then `python build_hands.py`. The halo, bridge and tip-radius checks all re-run,
so a size that does not work will fail rather than print badly.

### 3. Two old holes sit under the hands

The 4.76 mm and 9.52 mm holes are 36.5 mm and 41.3 mm from centre, so every
hand sweeps over both. Harmless mechanically — they are behind the hands — but
they will be visible on a finished dial, and the 9.52 mm one is hard to miss.

They look like they came from the original movement, which
`docs/hardware-plan.html` says is being removed entirely. Fill them, or move
the chapter ring artwork to cover them.

## Not checked

* **Depth behind the dial.** The hand stack needs 20 mm in front of the plate
  (six hands at 3.5 mm). This file is the plate alone, so there is nothing here
  to check that against; it depends on the hood.
* **Printing the plate.** At 394 mm it is larger than a common bed. It is
  drawn as 3/32 in sheet, which suggests it was meant to be cut rather than
  printed.
* **The chapter ring artwork.** No engraved scale in this file — the dial disc
  is blank. The log scale still has to be printed or engraved onto it, and
  `docs/clock-face.html` has the decade positions: each decade of distance is
  83.7°, with 1 km at 12 o'clock.

# Ambigram clock hands — design plan

Austin supplied six rotational ambigrams, one per family member. This is the
plan for turning them into CAD files for the six clock hands, with the analysis
already done and the decisions that still need Austin's answer.

**Status: planned, not built.** No CAD has been generated yet. Everything below
that is measured is marked as measured; everything else is a proposal.

---

## 1. What is in `cad/ambigrams/`

Seven files came in the archive; six are usable.

| File | Name | Size (px) | 180° symmetric | Use |
|---|---|---|---|---|
| `Austin_ambigram.png` | Austin | 1200×600 | **yes, exact** | yes |
| `Caleb_ambigram.png` | Caleb | 1000×600 | **yes, exact** | yes |
| `Micah_ambigram.png` | Micah | 1300×600 | **yes, exact** | yes |
| `Mom_ambigram.png` | Mom | 1100×600 | **yes, exact** | yes |
| `Dad_ambigram.png` | Dad | 900×600 | **yes, exact** | yes |
| `Elijah_ambigram2.png` | Elijah | 1100×600 | **yes, exact** | yes |
| `Elijah_ambigram_draft.jpg` | Elijah | 736×371 | no (49%) | **no** — earlier draft |

Two files arrived with a `.jpg` extension but PNG content; they have been
renamed to match what they actually are.

### Measured, not assumed

Rotational symmetry was checked by rotating the ink mask 180° and taking the
intersection over union. Six files score **100.0%** — these are not merely
ambigrams that *read* the same upside down, they are pixel-exact point
reflections. `Elijah_ambigram_draft.jpg` scores 49.2% and is a different,
rougher drawing; it is kept only for reference.

All six good files are black (`L < 128`) on a flat pastel background, 600 px
tall, with ink covering 13–16% of the frame.

### Why the symmetry matters here

A clock hand sweeps the full 360°. For half of every revolution a normal name
would be upside down. Because these are exact point reflections, the name reads
correctly at **every** hand angle that is a 180° pair — in particular it reads
right way up whether the hand points left or right. That is the whole reason
this is a good idea, and it is worth not throwing away in the CAD.

**Consequence for the build:** the word must be placed so that its own centre of
symmetry sits on the hand's long axis. If the word is offset or skewed, the
upside-down reading breaks.

---

## 2. The problem that shapes the design

**The ambigrams are not single connected shapes.** Measured with a connected
component count on the ink mask (2× downsampled):

| Name | Separate pieces larger than 1% of the ink |
|---|---|
| Austin | 9 |
| Micah | 10 |
| Elijah | 10 |
| Mom | 7 |
| Caleb | 5 |
| Dad | 3 |

So the naive approach — trace the glyph, extrude it 2.5 mm, done — produces
**nine loose bits of plastic**, not a hand. The i-dots alone are free-floating.

Every viable design has to answer: what holds the pieces together?

---

## 3. Design options

### A. Raised relief on a solid blade (recommended)

The hand is a solid tapered blade; the name stands proud of it by ~0.8 mm.
Every piece is supported by the blade beneath it.

* **Strong.** One solid body, no thin isolated features.
* **Prints cleanly** flat on the bed with no supports.
* **Reads well** if the relief is painted, or printed with a filament swap at
  the relief's first layer (a one-line pause in the G-code, no multi-material
  hardware needed).
* Blade silhouette can be an *offset hull* of the word rather than a plain
  taper, so the hand still reads as word-shaped from across the room.

### B. Offset-merged silhouette, word cut as relief (most striking)

Offset the word outward by ~0.6 mm per side so the separate pieces **merge into
one connected silhouette**, use that as the blade, then emboss or engrave the
true word on top. The hand's outline *is* the name.

* Visually the best: at a distance you see a word-shaped hand, close up you read
  it.
* Needs the offset to be large enough to bridge every gap and small enough that
  the letters stay distinct. **This needs measuring per name** — see §6.
* Risk: thin bridges between letters become the weak points.

### C. Through-cut stencil

Cut the name through a solid blade. Rejected: the counters (the enclosed bowl of
an *a*, *o*, *e*) drop out, so each would need a bridge, and bridges across
letterforms look like a mistake rather than a choice.

**Recommendation: build A and B from the same script** with a flag, print one of
each in PLA, and decide by looking at them. They share 90% of the pipeline.

---

## 4. Mounting — fixed by the hardware

From `build_cad.py` and `docs/hardware-plan.html`. These are not negotiable;
the hub of each hand has to match its level.

| Level | Person | Mounts on | Bore (mm) | Tip radius (mm) |
|---|---|---|---|---|
| 1 | Becky | Motor 1 shaft (NEMA23) | 12.0 OD shaft | 70 |
| 2 | Evan | Tube 2 | 7.14 | 76 |
| 3 | Elijah | Tube 3 | 6.35 | 82 |
| 4 | Caleb | Tube 4 | 3.18 | 88 |
| 5 | Austin | Tube 5 | 2.38 | 94 |
| 6 | Micah | Tube 6 | 1.59 | 100 |

Hand thickness is 2.5 mm and the vertical step between hands is 3.5 mm, so
**relief height must stay under about 1.0 mm** or hands will foul each other as
they cross. That is the hard constraint on option A.

Levels 4–6 have tiny bores (3.18, 2.38, 1.59 mm). A printed hub on a 1.59 mm
tube is fragile and will not hold by friction alone — those three want either a
brass insert, a set screw, or adhesive. **Design the hub with a flat boss for a
grub screw rather than relying on an interference fit.**

---

## 5. Balance, which matters more than it looks

The motors are open loop with no encoders. A hand that is heavy and
off-centre loads the motor differently at different angles, and a stepper that
slips loses position silently until somebody re-zeroes it at the clock.

So the script must:

* report the **area centroid** of each finished hand and its distance from the
  hub axis;
* offer a **counterweight tail** opposite the word — a short stub whose area
  balances the moment — as an option;
* report **estimated mass** at a given density and infill, so the four NEMA17
  levels can be checked against their holding torque.

A word-shaped hand 30 mm tall and 85 mm long in 2.5 mm PLA is on the order of
15–20 g with light infill. That is probably fine, but it should be computed
rather than hoped for.

---

## 6. The pipeline to build

Script: `build_hands.py` at the repo root, alongside `build_cad.py`, sharing its
conventions.

```
cad/ambigrams/*.png
   │
   ├─ 1. load, threshold at L<128, drop specks under ~0.05% of ink
   ├─ 2. trace contours (marching squares) → outer rings + holes
   ├─ 3. simplify (Douglas–Peucker, ~0.3 px) and smooth
   ├─ 4. scale to the target word length for that level
   ├─ 5. MEASURE: min stroke width, min gap between pieces, bbox, centroid
   ├─ 6. build the blade (option A taper, or option B offset hull)
   ├─ 7. add hub + bore + grub-screw boss, union everything
   ├─ 8. VALIDATE (see below) — fail loudly, do not emit a broken hand
   └─ 9. export
         ├─ STEP  (cadquery)        — the real CAD file, opens in anything
         ├─ STL   (trimesh)         — straight to a slicer
         ├─ SVG                     — laser cutting, or checking the outline
         └─ DXF   (ezdxf)           — CNC / laser, and for the dial maker
```

### Validation the script must do, and refuse to export without

1. **One connected solid.** Count components after the union; anything other
   than 1 is a bug, and silently exporting it wastes a print.
2. **Minimum feature width** ≥ 0.8 mm (two perimeters at a 0.4 mm nozzle).
   Report the thinnest stroke and where it is.
3. **Relief height** ≤ 1.0 mm, from the 3.5 mm inter-hand step.
4. **Bore** matches the level's tube within the configured clearance.
5. **Point symmetry preserved**: rotate the finished word outline 180° about
   the hand axis and confirm it still matches. This is the thing most likely to
   be broken silently by a scale or offset step.
6. **Tip radius** equals the level's value, so the staggered radii still work
   and the tips never overlap.

### Libraries — all verified installed in this container

`shapely` 2.2.0 (booleans, offsets), `trimesh` 5.2.0 (extrude, STL),
`mapbox-earcut` (triangulation), `ezdxf` 1.4.5 (DXF), `svgwrite`, `numpy`,
`Pillow`. `cadquery` was being installed for STEP output when this was written —
if it is unavailable, STL+SVG+DXF still cover printing and laser cutting, and
STEP can be added later without touching the tracing code.

A contour tracer is written by hand rather than shelling out to `potrace`, which
is not installed and would add a system dependency for something that is about
forty lines of marching squares.

---

## 7. Decisions needed from Austin

1. **Who are Mom and Dad?** The clock's six people are Becky, Evan, Elijah,
   Caleb, Austin and Micah. Four ambigrams are first names; the other two are
   `Mom` and `Dad`. The obvious reading is **Mom → Becky (level 1), Dad → Evan
   (level 2)**, but that is an inference and getting it backwards would put the
   wrong name on the wrong hand on a finished clock. Please confirm.
   Also: should the hands say "Mom" and "Dad", or should new ambigrams be drawn
   saying "Becky" and "Evan"? The clock face and the UI say Becky and Evan.
2. **Option A or B** (§3), or print one of each and decide by eye.
3. **Finish.** Painted relief, filament swap, or plain single-colour?
4. **Word length as a fraction of the hand.** Proposal: the word occupies the
   outer 75% of the tip radius, with a plain neck from the hub. A longer word
   is more legible; a shorter one looks more like a clock hand.
5. **Counterweight tail** — yes or no (§5).

---

## 8. Order of work for the next session

1. Confirm §7.1 — nothing else is blocked by it, but the file naming is.
2. Write the tracer and dump each name as SVG at its real size. **Look at the
   SVGs before going near 3D.** That is the cheap step to get right.
3. Measure min stroke and min gap per name (§6.5). These decide whether option B
   is viable and what the offset has to be.
4. Build option A for one name — Austin, level 5, which has a 2.38 mm bore and
   is therefore the second most awkward hub.
5. Validate, export STEP/STL/SVG/DXF, render a preview image the way
   `render_cad.py` does for the existing model.
6. Only then do the other five, and only then option B.
7. Add the hands to `build_cad.py`'s assembly so they show up in the viewer at
   `docs/viewer/` with the rest of the clock.

**Do not generate six hands before one has been looked at.** The failure modes
here (a word that falls apart, a bore that does not fit, a relief that fouls the
next hand) all repeat six times otherwise.

---

## 9. What this does not change

The tracking system is independent of this. Hand angles, the mapping, the
firmware and the controller all work the same whatever shape the hands are. The
only coupling is `steps_per_rev` and the per-person `invert` flag, neither of
which the artwork affects.

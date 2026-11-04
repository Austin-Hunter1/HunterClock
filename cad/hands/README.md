# Ambigram clock hands

Six hands, one per person, each shaped like that person's name. Generated from
`cad/ambigrams/` by `build_hands.py` at the repo root.

```bash
python build_hands.py                 # all six, both styles, every format
python build_hands.py --only austin   # one, while iterating
python build_hands.py --svg-only      # just look at the outlines first
```

The script writes to `cad_out/hands/` (gitignored). The files here are a
published copy: every STL, SVG and DXF, plus STEP for the recommended `halo`
style. Re-run the script and copy across if you change anything.

---

## What to print

**Start with `halo`.** The hand's outline is the name, with the letters raised
0.6 mm on top. `plaque` is the same name in relief on a tapered nameplate —
stronger and duller. Print one of each at level 5 (Austin) and decide by eye.

| File | Use |
|---|---|
| `*.stl` | straight into a slicer |
| `*.step` | real CAD — opens in Fusion, FreeCAD, SolidWorks, Onshape |
| `*.svg` | true size, for checking the outline or laser cutting |
| `*.dxf` | laser / CNC, two layers: `BLADE` and `NAME` |
| `all-hands-*.svg` | all six on one dial, to check tips and proportions |

## The six

| File | Person | Reads | Level | Tip radius | Bore | Grub screw | Mass |
|---|---|---|---|---|---|---|---|
| `1-becky-*` | Becky | **Mom** | 1 | 70 mm | 12.15 mm | M3 | 3.2 g |
| `2-evan-*` | Evan | **Dad** | 2 | 76 mm | 7.29 mm | M3 | 5.0 g |
| `3-elijah-*` | Elijah | Elijah | 3 | 82 mm | 6.50 mm | M3 | 6.0 g |
| `4-caleb-*` | Caleb | Caleb | 4 | 88 mm | 3.33 mm | M2 | 7.1 g |
| `5-austin-*` | Austin | Austin | 5 | 94 mm | 2.53 mm | M2 | 5.2 g |
| `6-micah-*` | Micah | Micah | 6 | 100 mm | 1.74 mm | M2 | 5.6 g |

Levels 1 and 2 carry the ambigrams that read **Mom** and **Dad**, which is what
Austin drew. The dial, the config and the UI call them Becky and Evan; only the
hands say Mom and Dad. If you would rather they matched, draw two more
ambigrams and drop them in — nothing else changes.

## Printing

* **Flat on the bed**, name upwards. No supports.
* **0.2 mm layers**, 3 perimeters, 30% infill. The plate is 2.5 mm, so it comes
  out nearly solid anyway.
* **Two colours without a multi-material printer:** the relief starts at exactly
  2.5 mm. Tell the slicer to pause at that height and swap filament. Everything
  above is the name.
* Alternatively print in one colour and paint the raised faces — a brush loaded
  lightly and dragged flat over the relief catches only the letters.

## Fitting

The hub grip is only as long as the hand is thick (2.5 mm), because the hands
sit 3.5 mm apart and an axial boss would foul its neighbour. The grub-screw tab
sits at 90° to the hand so it never sits under the word.

* **Levels 1–3** (12.15, 7.29, 6.50 mm bores): an M3 grub screw holds fine.
* **Levels 4–6** (3.33, 2.53, 1.74 mm): an M2 screw will crush thin brass tube.
  Use adhesive, or solder a collar to the tube and screw the hand to that.
  Level 6 on 1.59 mm tube is the one to be careful with.

Check the bore against your actual tube before printing six. Brass tube runs to
tolerance and PLA shrinks; the bores include 0.15 mm clearance, which is a
starting point, not a guarantee.

## Balance

Masses are 3–7 g, well inside what a NEMA17 holds at any angle, so no
counterweights. The script reports the centroid radius per hand if you change
the proportions enough to want checking.

## If you change the artwork

`tools/analyse_ambigrams.py` checks a new file is a true 180° ambigram before
you build from it. `build_hands.py` then refuses to export a hand that comes out
in pieces, has a bridge under 0.8 mm, or misses its tip radius — so a bad
change fails loudly instead of wasting a print.

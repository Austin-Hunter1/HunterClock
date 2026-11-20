# Ambigram clock hands — print these

Six hands, each shaped like one person's name, generated from `cad/ambigrams/`
by `build_hands.py`. **Built for option D** (off-axis motors driving the tubes
with belts) and sized to the measured dial.

## Print this one file

| File | For |
|---|---|
| **`print-plate-option-d-halo.3mf`** | all six on one bed, 256 × 256 mm or bigger |
| `print-plate-option-d-halo.stl` | same, if your slicer prefers STL |
| `print-plate-option-d-1of2-halo.3mf` + `2of2` | two plates, for a 220 × 220 mm bed |
| `print-plate-option-d-plaque.stl` | the plaque style instead, all six |

The plate is **119 × 226 mm, 3.1 mm tall, 43 g of PLA**, six separate solids in
one file. Your slicer will show six parts it can colour or move individually;
there is nothing to split.

Every part has been checked after export: watertight, consistent winding,
sitting flat on the bed, no part overlapping another.

## Settings

* **Flat on the bed, name upwards. No supports.**
* 0.2 mm layers, 3 perimeters, 30 % infill. At 2.5 mm it comes out near solid.
* **Two colours with one extruder:** the relief starts at exactly 2.5 mm. Tell
  the slicer to pause there and change filament. Everything above is the name.
* Or print in one colour and drag a lightly loaded brush flat across the
  relief, which catches only the letters.

## The six

| File | Person | Reads | Level | Tip radius | Bore | Screw | Mass |
|---|---|---|---|---|---|---|---|
| `1-becky-*` | Becky | **Mom** | 1 | 85 mm | 7.94 mm | M3 | 4.2 g |
| `2-evan-*` | Evan | **Dad** | 2 | 91 mm | 7.14 mm | M3 | 6.9 g |
| `3-elijah-*` | Elijah | Elijah | 3 | 97 mm | 6.35 mm | M3 | 8.2 g |
| `4-caleb-*` | Caleb | Caleb | 4 | 103 mm | 5.56 mm | M2 | 9.7 g |
| `5-austin-*` | Austin | Austin | 5 | 109 mm | 4.76 mm | M2 | 6.8 g |
| `6-micah-*` | Micah | Micah | 6 | 115 mm | 3.97 mm | M2 | 7.2 g |

## Two things decided for you, both reversible

**The bores are the option D tube stack.** With no motor bores for the tubes to
thread through, six consecutive K&S sizes nest cleanly on a 0.36 mm wall:
5/16, 9/32, 1/4, 7/32, 3/16, 5/32 in. Level 6 goes from 1.59 mm of brass to
3.97 mm, which is a real gain in stiffness. The outermost at 7.94 mm still
passes the dial's centre hole once that is opened to 12.7 mm.

Bores are cut at the **nominal tube OD with no clearance added**. A printed
hole comes out slightly undersize anyway, so expect a light ream to fit — and
reaming a hole out is easy where shrinking one is not. **Test-fit one hand
before printing the rest.**

**The tip radii fill the measured dial.** Caleb's dial is a 9.75 in disc,
123.8 mm radius. The old 70–100 mm radii were sized against `build_cad.py`'s
placeholder 110 mm dial and left 24 mm of bare dial; these run 85–115 mm with
9 mm of chapter ring to spare.

Both live in `OPTION_D` in `build_hands.py`. Change and re-run:

```bash
python build_hands.py --preset option-d --plate              # 256 mm bed
python build_hands.py --preset option-d --plate --bed 220 220  # two plates
python build_hands.py --preset as-drawn                      # the old sizes
```

## If you change anything

The build refuses to export a hand that comes out in pieces, has a bridge under
0.8 mm, or misses its tip radius, and the plate writer loads its own STL back
and refuses a mesh that is not watertight. That last check exists because it
caught a real defect: two letter pieces touching at exactly one point extrude
into an edge shared by four faces, and every in-memory check passed happily
while the exported mesh was unprintable.

# Display bezels

Six rings that frame the round GC9A01 screens against the dial, from
`build_bezels.py`. `docs/hardware-plan.html` budgets these as the "printed
bezel / diffuser ring", to be brass-finished with spray or a gilt pen so they
match the cabinet's existing hardware.

## Print this

| File | What |
|---|---|
| **`bezel-plate-x6.stl`** | all six, 210 × 107 mm, 22 g — fits a Prusa MK4 |
| `bezel-plate-x6.3mf` | same, if you are slicing it yourself |
| `bezel.stl` / `.step` | one bezel, 3.6 g, for a test fit |

**Print one first.** See the warning below.

## The design

| | |
|---|---|
| Outer diameter | 47.50 mm |
| Aperture | 33.00 mm (the lit area is 32.40, so no pixels are covered) |
| Module pocket | 39.90 mm across × 3.30 deep, open at the back |
| Seating lip | 3.45 mm wide |
| Total thickness | 4.90 mm |
| Mounting | 4 × 1.7 mm blind holes, 3 mm deep, on a 43.5 mm circle |
| Header slot | 16 mm wide through the rim |

The module drops into the pocket from behind and seats against the lip, so the
front face hides the PCB edge and leaves the round glass showing.

**The screws go in from the back.** The holes are blind, so nothing shows on
the finished front — which matters when the ring is brass-finished. The module
ends up sandwiched between the bezel and whatever the bezel screws to. 1.7 mm
takes an M2 self-tapper straight into the plastic.

The 16 mm slot in the rim is for the pin header, which on these carriers is
soldered to the edge of the board. Without it the module cannot sit flat.

## Measure your module before printing six

The dimensions above come from `build_cad.py`, which records them as
"as ordered" — from a product listing, not from a part in hand. GC9A01
carriers differ between makers, and `docs/circuit-diagram.html` already warns
that even the header order varies.

So: print `bezel.stl`, drop a real module in, and check three things.

1. **Does it drop into the pocket?** Too tight: raise `--fit`. Too loose:
   lower it.
2. **Does the header clear the slot?** If the header is somewhere else on your
   board, the slot needs moving — tell me where it sits and I will rotate it.
3. **Does the aperture cover any pixels?** It should not: 33.0 mm against a
   32.4 mm lit area. If the bezel crops the image, raise `--aperture-margin`.

Then rebuild:

```bash
python build_bezels.py --measure 40.2        # your measured PCB diameter
python build_bezels.py --measure 40.2 --active-d 32.4 --rim 5
python build_bezels.py --help                # every dimension is a flag
```

The build refuses to export a bezel whose seating lip is under 1 mm, whose rim
is too thin to print and paint, whose screw holes break out of the rim, or
whose blind holes would break through the front face — so a bad number fails
loudly rather than wasting six prints. It also loads the finished plate back
and checks every part is watertight and flat on the bed.

## Still open

**How these mount is not designed yet.** The bezel gives you a 43.5 mm screw
circle and a clean front; what it screws *to* depends on where the screens sit,
and that follows from the option D layout behind the dial, which is the next
piece of mechanical work. Nothing here forecloses it.

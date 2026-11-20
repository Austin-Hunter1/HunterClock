# CU Idea Forge print request

Everything needed to fill in the intake form, and the two files to upload.

## Upload these two files

| File | Size | Contains |
|---|---|---|
| `cad/hands/print-plate-mk4-1of2-halo.stl` | 172.5 × 162.2 × 7.5 mm | 3 clock hands + 3 bezels |
| `cad/hands/print-plate-mk4-2of2-halo.stl` | 172.5 × 176.8 × 7.5 mm | 3 clock hands + 3 bezels |

Between them that is the whole front of the clock — 12 parts, nothing else to
print. STL rather than 3MF because the form asks for a file a service can read
and STL is read by everything.

## The form, field by field

| Field | Answer |
|---|---|
| Requested completion date | 11/21/2026 |
| File upload | the two STLs above |
| Purpose of request | Student Club |
| Department | Smead Aerospace Engineering |
| Course | NA |
| Material | PLA |
| Colour | Black |
| Size / scaling | **100%, no scaling.** Files are modelled in millimetres. |
| Quantity | 1 of each file |

### On scaling

Say 100% and nothing else. STL carries no units, so a slicer has to assume
them, and the usual failure is a file read as inches and scaled by 25.4. The
check that catches it: **plate 1 must measure 172.5 mm across the bed.** If it
comes up as 6.8 or 4382, the units were guessed wrong.

These are functional parts that have to fit real hardware, so a "make it fit
the plate" rescale would ruin them.

## Description (paste this in)

> Two print plates for a Weasley-clock build — a grandfather clock whose six
> hands point to where each family member is. Each plate holds three clock
> hands and three circular display bezels; the two plates together are the
> complete set of 12 parts.
>
> Print at 100% — the files are in millimetres and must not be rescaled, as
> every part has to fit existing hardware. Plate 1 measures 172.5 × 162.2 mm
> and plate 2 measures 176.8 mm deep; both fit a 250 × 210 bed with room for a
> skirt. Maximum part height is 7.5 mm.
>
> Both plates are already oriented and laid flat, so please print as-is.
> Nothing overhangs, so no supports are needed anywhere. 0.2 mm layers in
> black PLA are ideal. Roughly 70 cm³ of material across both plates if solid,
> considerably less at a normal infill — standard infill and perimeters are
> fine, these are decorative parts carrying almost no load.
>
> Two features do matter dimensionally, and both are holes rather than
> outside surfaces, so a little elephant's foot on the first layer is harmless:
>
> 1. The round bezels have a 39.9 mm recess on their underside that a 39.5 mm
>    circuit board drops into. It needs to stay round and not close up.
> 2. Each hand has a round bore at its wide end, between 3.97 mm and 7.94 mm
>    depending on the part, which slides onto a brass tube. These also need to
>    stay round and not undersize.
>
> If your process typically runs holes a few tenths undersize, that is fine —
> they will be reamed by hand — but please let me know so I can plan for it.
> The parts will be primed and painted after printing, so surface finish and
> visible layer lines are not a concern.

## Untested on hardware

Nothing here has been printed and no display module or brass tube has been
test-fitted. The dimensions come from product listings and from measurements
of the cabinet, not from parts in hand. The two fits called out above are the
ones to check on the first print before committing to the rest.

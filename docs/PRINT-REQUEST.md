# CU Idea Forge print request

Everything needed to fill in the intake form, and the two files to upload.

## Upload these two files

| File | Size | Contains |
|---|---|---|
| `cad/hands/print-plate-mk4-1of2-pointed.stl` | 172.5 × 136.3 × 7.5 mm | 3 clock hands + 3 bezels |
| `cad/hands/print-plate-mk4-2of2-pointed.stl` | 172.5 × 136.3 × 7.5 mm | 3 clock hands + 3 bezels |

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
check that catches it: **each plate must measure 172.5 mm across the bed.** If it
comes up as 6.8 or 4382, the units were guessed wrong.

These are functional parts that have to fit real hardware, so a "make it fit
the plate" rescale would ruin them.

## Description (paste this in)

> Two plates for a clock build: 12 parts in all — six clock hands and six
> round display bezels.
>
> Print at 100%. The files are in millimetres and must not be rescaled; as a
> check, each plate should measure 172.5 mm across. Both plates are laid flat and
> need no supports anywhere. 0.2 mm layers, black PLA, standard infill. The
> parts get painted afterwards, so surface finish does not matter.
>
> Two holes need to stay round and not undersize: a 39.9 mm recess on the back
> of each bezel, and a 3.97–7.94 mm bore in each hand. If your process runs
> holes a few tenths small that is fine, just let me know.

## Untested on hardware

Nothing here has been printed and no display module or brass tube has been
test-fitted. The dimensions come from product listings and from measurements
of the cabinet, not from parts in hand. The two fits called out above are the
ones to check on the first print before committing to the rest.

# The cabinet does not fit the motor stack

Caleb measured the real clock on 2026-11-20. One of those numbers breaks the
mechanical design, and it is better to know now than after the tubes are cut.

## The measurements

| | |
|---|---|
| Total depth | **9 in** (the block on the back is 1 in and could come off, so 10 in) |
| Mounting blocks | 1 × 2.5 in, about 2.5 in tall |
| Hood front board | 15 × 19 in |
| Cutout | 1.25 in off the bottom edge |
| Square section of the cutout | 10-5/8 × 10-5/8 in |
| Arch above it | 8.5 in diameter |

## The problem: 16.34 in of motors into 9 in of cabinet

`build_cad.py`'s own arithmetic, replayed:

```
motor 1  Becky   NEMA23   flange at  -18 mm   back at  -76 mm
motor 2  Evan    NEMA23   flange at -105 mm   back at -163 mm
motor 3  Elijah  NEMA17   flange at -183 mm   back at -226 mm
motor 4  Caleb   NEMA17   flange at -246 mm   back at -289 mm
motor 5  Austin  NEMA17   flange at -309 mm   back at -352 mm
motor 6  Micah   NEMA17   flange at -372 mm   back at -415 mm
```

**415 mm = 16.34 in behind the dial. There are 229 mm = 9 in.
Short by 186 mm = 7.3 in.** See `cad/face/depth-fit.svg`.

The pitch is `previous motor's rear shaft tip + 4 mm + next motor's front
shaft`: 87 mm between the two NEMA23s and 63 mm thereafter. Nothing about that
is wasteful — it is what nose-to-tail hollow-shaft motors cost.

### Why the plan missed it

`docs/hardware-plan.html` reasoned the depth objection away:

> The original movement, weights, and pendulum are coming out entirely, which
> removed the depth objection to the single hub: instead of a few inches behind
> the dial plate, the whole trunk cavity (previously filled by the weight-drop
> channels) is available, so all three layouts fit.

The trunk cavity is **below** the dial, not behind it. Gutting it frees
vertical space; the case is still only 9 in front to back. `build_cad.py` then
assumed 343–527 mm of case depth, "at the deep end of the Howard Miller range" —
the real cabinet is 229 mm, below the bottom of that range.

### Trimming does not rescue it

| | depth | fits 9 in |
|---|---|---|
| Six coaxial, as designed | 415 mm / 16.34 in | no |
| Rear shaft stubs cut flush with the bodies | 357 mm / 14.06 in | no |
| That, plus the coupling gap down to 2 mm | 347 mm / 13.66 in | no |
| All six as NEMA17 (needs an 11 mm bore nobody sells) | 376 mm / 14.80 in | no |
| **Three coaxial** | **226 mm / 8.90 in** | **yes** |
| Three, rear stubs cut flush | 195 mm / 7.68 in | yes |
| Six hypothetical 20 mm pancake hollow-shafts | 218 mm / 8.58 in | yes |

## Four ways out

### A. Two chains of three — uses exactly the motors already bought

`docs/hardware-plan.html` costed this and rejected it on looks ("the
single-pivot look won"). The cabinet has now made the decision differently.

A chain of **NEMA23 + NEMA17 + NEMA17** works for the bore cascade: hand 1 on
the NEMA23's 12 mm shaft, tube 2 at 7.14 mm through the 8 mm NEMA23 bore, tube 3
at 3.18 mm through that and the 4 mm NEMA17 bore. Two such chains need
2 × NEMA23 + 4 × NEMA17 — **precisely StepperOnline order 330093**.

Depth 226 mm (8.90 in), or 195 mm with the rear stubs trimmed. Fits as it
stands, comfortably with the back block off.

Cost: two pivot points on the dial instead of one. On a 9.75 in dial two hubs
would sit a few inches apart, three hands each.

### B. Extend the case backwards

Add a box behind the hood. Keeps the single pivot and every part already
bought, at the price of 8 in of furniture sticking out the back. The clock
stands against a wall, so this is less absurd than it sounds, but it is still
8 in.

### C. Source short hollow-shaft steppers

Six 20 mm-body pancake hollow-shafts come to about 218 mm (8.58 in) and keep
the single pivot. **Whether they exist with the right bores is the question** —
`docs/hardware-plan.html` already found the 17HS13-1504H was StepperOnline's
*only* NEMA17 hollow-shaft option, "not a range to pick from". The $173 of
motors already bought would become spares.

### D. Move the motors off-axis and drive the tubes with belts

Keep the six nested tubes at the dial, end each in a pulley at its own depth,
and put the six motors around the perimeter, parallel to the dial. The tube
stack plus pulleys is on the order of 100 mm; the motors add no depth because
they sit radially.

Best outcome: single pivot, shallowest of all, and the bought motors still
work (a hollow shaft is harmless when driving a pulley). Most fabrication work
by a distance, and six belts is six more things to tension.

**Recommendation: A.** It fits today, it uses what is already paid for, and it
is the option the plan had already thought through. D is better if there is
appetite for the machining.

## The dial plate is fine

Measured against `cad/face/Clock_Face_1.3mf`:

| | CAD plate | Caleb's opening | |
|---|---|---|---|
| Square section | 11.00 in | 10.625 in | 0.188 in overlap per side |
| Height | 15.50 in | 14.875 in | 0.62 in margin total |
| Dial disc | 9.75 in | — | 0.44 in of plate visible each side |

The disc's top is 4.875 in above centre and the square opening's top is
5.3 in above it, so the arch never crops the dial. Mount the plate with its
dial centre **6.56 in above the bottom of the board** (the centre of the square
opening) and it lines up.

Still outstanding from `cad/face/README.md`: the centre hole is 11.11 mm and
motor 1's shaft is 12.0 mm, so **open it to 12.7 mm**. That holds under option
A as well, since each chain still has a NEMA23 at the front.

## None of this touches the software

Six hands are still six channels on the same Pico, the same pins, the same
protocol. Under option A the only change is physical: `level` in
`config/clock.yaml` keeps mapping each person to a channel, and the two hubs
would simply be levels 1–3 and 4–6. The controller, the firmware, the UI and
the tests are unaffected.

The ambigram hands are unaffected too, except that two hubs means two sets of
tip radii — `build_hands.py` takes those per hand already.

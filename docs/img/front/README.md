# Front renders

From `python tools/render_front.py`. Square on to the clock, the six hands at
four sets of distances.

| File | What |
|---|---|
| `front-all.png` | all four panels on one sheet |
| `front-all-home.png` | everyone home — six hands stacked at 12 o'clock |
| `front-decades.png` | one hand per decade, so the log scale reads as angles |
| `front-a-tuesday.png` | ordinary distances, 2–14 km |
| `front-austin-away.png` | Austin in Boulder, everyone else home |

Built from the parts that exist: the measured `Clock_Face_1.3mf` dial plate,
the six option-D hands, the six ornate bezels. The hood board and its opening
are Caleb's measurements.

## What is assumed, not measured

* **The display wings.** The hood front is 15 in wide and the opening is
  10-5/8 in, leaving 55.5 mm of stile either side. A 53.5 mm bezel clears that
  by a millimetre, into the wood holding the glass frame. The renders add a
  75 mm wing to each side of the hood, which takes the whole front to 20.9 in.
* **The trunk.** Not measured. Drawn 11 in wide and short, only to show the
  case carries on below the hood.
* **The chapter ring.** The hardware plan calls for one machined for the log
  scale but does not draw it. These ticks are every 10 degrees with the five
  decades marked, which is the scale `clockd.geo` actually implements.
* **The hand colours.** The hands print in black PLA. They are coloured per
  person here only so six overlapping hands can be told apart.

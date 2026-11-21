# Front renders

From `python tools/render_front.py`. Square on to the clock, with the hands
where `birthday_clock.m` puts them.

| File | What |
|---|---|
| `front-actual.png` | the real positions, from the MATLAB sim's six locations |
| `front-all-home.png` | everyone home — six hands stacked at 12 o'clock |
| `front-decades.png` | one hand per decade, so the log scale reads as angles |
| `front-all.png` | all three on one sheet |

Built from the parts that exist: the measured `Clock_Face_1.3mf` dial plate,
the six option-D hands, the six ornate bezels. The hood board and its opening
are Caleb's measurements.

## The positions

`birthday_clock.m`'s own table, through `clockd.geo` — the same haversine and
the same log map, so these are the angles that sim produces. Home is
Minnetonka, MN.

| | Where | Distance | Angle |
|---|---|---|---|
| Becky | Minnetonka High School, MN | 0 km | 0° |
| Elijah | St Paul, MN | 33 km | 127° |
| Caleb | Wheaton, IL | 552 km | 229° |
| Austin | Boulder, CO | 1 107 km | 255° |
| Micah | Corvallis, OR | 2 338 km | 282° |
| Evan | Warsaw, Poland | 7 537 km | 325° |

## Colour

The hands are gilt with the raised name picked out in that person's colour.
One accent colour for all six would be handsome and unreadable the moment two
hands cross, so the accent is per person; `--accent "#1E2228"` forces a single
colour. The dial is silvered so the gilt separates from it.

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
* **The finish.** The hands print in black PLA; the gilt and the accents are
  what they would look like painted, not what comes off the printer.

# Runbook

The clock is in a house in another state. Nobody there is technical. This is how
to install it, check on it, and fix it from Boulder.

* **UI:** `http://weasley-clock:8080/` over Tailscale
* **Shell:** `ssh hunterboys@weasley-clock`
* **Service:** `clockd`, under systemd, `Restart=always`
* **Logs:** `journalctl -u clockd -f`

---

## 1. First-time install

Do all of this **before the Pi travels.** Once it is in Minnesota there is no
second way in.

### 1.1 Flash and reach the Pi

Raspberry Pi OS Lite 64-bit (Bookworm). In Raspberry Pi Imager's advanced
options set the hostname to `weasley-clock`, the user to `hunterboys`, and
enable SSH. That gets you in without a screen.

### 1.2 Save every WiFi network it will ever see

```bash
./deploy/add-wifi.sh "Your-Boulder-WiFi"   'password'  10
./deploy/add-wifi.sh "Destination-House"   'password'  50
```

Higher priority wins when both are in range. Get the destination network's name
and password from whoever lives there **now**, not when the clock arrives.

Saving both is the first line of defence, not the only one: if the destination
house later changes its password, every saved network is wrong at once. Section
11 is the way back in when that happens, and it needs a label written before
the clock travels.

### 1.3 Join Tailscale

```bash
./deploy/tailscale-setup.sh
```

Then, in the Tailscale admin console, open `weasley-clock` and **disable key
expiry**. Without that it drops off the tailnet in about six months and you lose
the clock.

### 1.4 Install the controller

```bash
git clone https://github.com/Austin-Hunter1/HunterClock
cd HunterClock
./deploy/install.sh
```

It creates `.venv`, installs the controller, copies `config/clock.example.yaml`
to `config/clock.yaml` and `.env.example` to `.env`, adds `hunterboys` to
`dialout`, installs both systemd units, turns on persistent journald and the
Pi's hardware watchdog. It is safe to run again after any `git pull`.

**It prints a hotspot passphrase once, in yellow. Write it down.** That is the
only way back into the clock if the house WiFi password ever changes; section
11 is what it is for. It is saved in `.env` as `CLOCK_HOTSPOT_PASSWORD` and is
not regenerated on later runs, so you can fetch it again — but only while you
can still reach the Pi.

### 1.5 Secrets

```bash
.venv/bin/python -m clockd.hashpw        # paste the hash into config/clock.yaml
nano .env                                # MQTT_PASSWORD and CLOCK_SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"   # for CLOCK_SECRET_KEY
```

`.env` is mode 600 and gitignored. Nothing secret ever goes in
`config/clock.yaml` except the password *hash*.

`.env` also holds `CLOCK_HOTSPOT_PASSWORD`, which `install.sh` generated. It is
the one secret here that has to exist in the physical world too — on a label
inside the cabinet (section 11).

### 1.5a Set up the broker

```bash
./deploy/mosquitto-setup.sh
```

**Do not skip this because Mosquitto is "already running".** A Mosquitto 2.x
with no listener configured binds loopback only and refuses anonymous clients.
The controller is on localhost so it works perfectly, and the six ESP32 display
nodes are on the house WiFi and cannot connect at all. The symptom is six dark
bezels and no error anywhere.

The script writes a listener, two accounts and an ACL, generates the passwords
into `.env`, and then proves it works by publishing and subscribing as both
accounts — including checking that the display account is *refused* where it
should be. A broker that starts is not the same as a broker that works.

| Account | Who | Can do |
|---|---|---|
| `clock` | the controller | everything under `clock/`, read `owntracks/` |
| `display` | the six ESP32 nodes | read their display topic, write their own status |
| `phone` | OwnTracks, if used | write `owntracks/` and nothing else |

The split exists because each display node keeps its MQTT password in a plain
`config.json` on its own flash, and anyone who can unscrew a bezel can read it.
With one shared account that password could publish to
`clock/location/<person>` and move every hand on the clock. With the roles
split, the worst it does is repaint a display.

**After running it, change each node's `config.json`** from `"mqtt_user":
"clock"` to `"mqtt_user": "display"` with the password the script prints. The
shipped example still says `clock`.

The ACL is tested against a real broker in `controller/tests/test_mosquitto_acl.py`.

### 1.6 Edit the config

In `config/clock.yaml`:

* `home.lat` / `home.lon` — **the destination house**, not Minnetonka, unless
  that is where it is going. Everything on the dial is measured from here.
* `site` — free text, shows in the UI header.
* `people` — who is on which hub level. Level 1 is the front motor (STEP GP2).
* `people[].owntracks.user` — must match the phone's OwnTracks username.

### 1.7 Start it

```bash
sudo systemctl restart clockd
journalctl -u clockd -f
```

Open `http://weasley-clock:8080/`. Every hand will say **unverified** — that is
correct, and section 4 is how it is fixed.

### 1.8 Phones

Install OwnTracks on each phone. Mode: MQTT. Host `weasley-clock`, port 1883,
username `clock`, the password from `.env`, device ID anything, **username must
match `owntracks.user` in the config**. It publishes to
`owntracks/<user>/<device>`, which is what the controller subscribes to.

---

## 2. The ten-minute remote health check

Do this monthly, or whenever someone says the clock looks wrong.

1. **Open the UI.** If it does not load, go to section 6.
2. **The banner.** `Pico connected · fw 0.1.0 · Drivers enabled · MQTT connected`.
   Anything red is the answer.
3. **The dial.** Six hands. Compare against where you know people are.
4. **The table.** `Last seen` under a few hours for anyone whose phone is on.
   `Status` should be `ok`. Anyone `stale` has a phone that has stopped
   reporting — usually OwnTracks killed in the background, not the clock.
5. **System page.** CPU under 70 °C, disk over 1 GB free, `clockd: active`,
   `mosquitto: active`, Tailscale `Running`.
6. **Logs page.** Filter to `error`. A steady trickle of rejected payloads from
   one person means their phone is misconfigured; a trickle of serial timeouts
   means the USB link is unhappy.
7. **Move something.** Control → jog any hand ±1°, watch `Actual` change, then
   **All to live positions**. If a hand moves on the dial but not in the room,
   that is a mechanical or wiring problem, not software.

### 2.1 Getting told instead of checking

Section 2 is what to do when you remember. Alerting is what tells you when you
do not. Both are optional, off by default, and set in `.env` (never in
`config/clock.yaml`, because a ping URL is a credential):

```bash
# in .env
HEALTHCHECKS_URL=https://hc-ping.com/your-uuid-here
NTFY_URL=https://ntfy.sh/weasley-clock-7f3a9c2e
```

and in `config/clock.yaml`:

```yaml
alerts:
  enabled: true
  interval_s: 300.0
```

**healthchecks.io** is the dead-man's switch. Make a check with a period of
10 minutes and a grace of 20, and it emails you when the pings stop. This is
the only thing that catches a Pi that has lost power, lost its WiFi or hung,
because in those cases the controller cannot send you anything — it is the
thing that broke.

**ntfy.sh** tells you *what* broke while the Pi is still alive to say so: the
Pico link down, a firmware fault, every hand unverified, every person gone
stale. Pick an unguessable topic name — anyone who knows it can read your
notifications. Install the ntfy app and subscribe to that topic.

A condition has to hold for a minute before anything is sent, and nothing is
sent twice about the same thing within fifteen minutes, so a flapping USB cable
is one message rather than hundreds. You get a single "recovered" message when
it clears. The System page shows the counters and any condition currently
active.

---

## 2.2 Connecting Find My

This is the section to read if the question is "can the clock just use Find My
iPhone?". The short answer is yes, but not directly, and not as the only thing.

**Apple has no public Find My API.** There is no key you can apply for, no
endpoint, no OAuth. Everything that claims to read Find My is doing one of two
things: logging into iCloud with your Apple Account password and pretending to
be a browser, or joining Apple's Find My *network* with reverse-engineered
crypto. The first is what gets accounts locked and is not going in this
project. So "Find My" here means **Home Assistant does the talking to Apple,
and the clock reads a clean entity out of Home Assistant.**

Three routes. They are not alternatives — route 1 is the foundation, route 2
is the one that needs nothing installed on the phones, route 3 is the escape
hatch. Run all three if you like; `sources.priority` decides who wins.

| | What it needs | Reliability | Battery | Use it for |
|---|---|---|---|---|
| **1. OwnTracks** | an app on each phone, Tailscale on each phone | high | you control the interval | everyone who will install an app |
| **2. Find My via Home Assistant** | a Home Assistant, an Apple Account | breaks every few weeks | Apple's | everyone who will not |
| **3. Generic MQTT** | anything that can POST or publish | whatever you build | — | one-offs and odd cases |

### Route 1 — OwnTracks. Use this one.

A free app on each phone that publishes straight to the Pi's MQTT broker. No
Apple account, no middleman, no rate limits, and it is the source this clock
was built around.

On each phone: install OwnTracks, then **Settings → Connection**:

| Field | Value |
|---|---|
| Mode | MQTT |
| Host | `weasley-clock` (or the Pi's Tailscale IP) |
| Port | 1883 |
| Username | `clock` |
| Password | the one in `.env` |
| Device ID | anything; `iphone` is fine |
| **Username (identification)** | **must match `owntracks.user` in `config/clock.yaml`** |

That last row is the one people get wrong. The topic is
`owntracks/<user>/<device>` and the controller matches on `<user>`.

Set the app to **Significant Changes**, not Manual. Then walk outside and watch
the Logs page: the fix should arrive, be accepted, and command a move.

The catch: the phones have to reach the broker. Inside the house that is the
local network; outside it they need Tailscale on the phone too, or the broker
exposed to the internet, which you do not want. **Install Tailscale on the
phones.** It is free for personal use and it is the same thing you already
need to reach the clock yourself.

### Route 2 — Find My, through Home Assistant

What `docs/hardware-plan.html` planned for, and worth having, because everyone
already carries an iPhone and nobody has to keep an app alive.

**1. Run Home Assistant somewhere.** It can be the same Pi, but a separate box
is kinder: Home Assistant is a much bigger thing than this controller, and you
do not want its upgrades taking the clock down with them.

**2. Add the Apple Account integration.** Settings → Devices & services → Add
integration → **Apple iCloud** (Apple now calls an Apple ID an "Apple
Account"; Home Assistant's integration is still listed under iCloud). It wants
the Apple Account and a six-digit code from a trusted device. Each person needs
their own account added, or one Family Sharing account that can see the others.

Two things Home Assistant's own documentation says about it, both of which
matter here:

* **The device must be registered with Find My.** A phone with Find My
  iPhone switched off is invisible to this route entirely.
* **It polls on a dynamic interval**, not a fixed one — Home Assistant varies
  it per device based on which zone the device is in, how far from home it is
  and its battery level. So a phone that has gone a long way away is not
  necessarily reporting more often, and a slow-moving hand is not by itself a
  fault.

You may also get mail from Apple saying someone signed into your account. That
is Home Assistant, and Home Assistant's docs warn about it.

**3. Check what you got, before editing any config.**

```bash
export HASS_TOKEN='...'
python tools/hass_check.py --url http://homeassistant.local:8123
```

The token comes from Home Assistant: your profile (bottom left) → Security →
Long-lived access tokens. The tool prints every entity that could drive a
hand, whether it carries coordinates, and **where each one would put its hand
right now using the clock's own mapping** — so a number that looks wrong there
is wrong. It only reads, and it never prints the token.

A line reading `state=home, no coordinates` is a tracker that cannot drive a
hand: a router-based one, or a phone that is offline.

**4. Point each person at a `person.` entity, not a `device_tracker.`**

This is the part that is easy to get wrong and only hurts months later.

Home Assistant has two kinds of entity that carry a position:

`device_tracker.austin_iphone`
: One specific source. When iCloud stops answering — see below — this goes
  quiet, and Austin's hand goes stale even though the Home Assistant companion
  app on the same phone is still reporting perfectly well.

`person.austin`
: Home Assistant's own aggregate of *everything* that knows where Austin is,
  following whichever is currently reporting. When Find My stops answering it
  falls back by itself.

So `config/clock.example.yaml` points at `person.<name>`, and `hass_check.py`
will tell you if you have wired somebody to a bare tracker that a person entity
already covers.

**The one thing you give up** by doing this: a person entity carries no battery
level, because it has no single device to have a battery. The hand still moves
correctly; the UI just will not show that phone's percentage from this source.
OwnTracks still reports battery. If you would rather have the battery reading
than the fallback, point at the tracker — but know which you are choosing.

The clock records which tracker a person entity is following, so the Logs page
shows `person.austin via device_tracker.austin_iphone`. That line is how you
find out Find My quietly stopped three weeks ago and the companion app has
been carrying the hand.

Put the entity ids into `config/clock.yaml` under each person's `hass_entity`,
set `sources.hass.enabled: true`, and restart.

**5. Expect this to break every few weeks.** Apple rate-limits iCloud, and a
two-factor re-prompt stops it dead until somebody re-authenticates in Home
Assistant. `docs/hardware-plan.html` said the same, which is why this is route
2 and not route 1.

When it does break:

* the hands go `stale` and **hold their last position** — they do not jump to
  nonsense, and they do not go to 12;
* Home Assistant shows the integration needing re-authentication;
* fix it there (enter a fresh six-digit code), and the clock picks up again on
  its next poll with no restart.

If you set up alerting (§2.1), a `stale` person is one of the conditions that
can notify you, so you find out without looking.

#### What about iCloud3, hass-FindMy and the rest?

There are third-party Home Assistant integrations that go at Apple harder than
the official one — iCloud3, `hass-FindMy`, and others. Some actively request a
location rather than reading whatever Apple last published; some reach Apple's
Find My *network* and can see AirTags.

They are not wired in here and this project does not recommend one. They are
community integrations with their own update cadence and their own ways of
annoying Apple, and the clock cannot tell the difference anyway: **anything
that produces a Home Assistant entity with `latitude` and `longitude` works
with route 2 unchanged.** If you install one, point `hass_entity` at whatever
entity it creates and run `hass_check.py` to confirm the clock can read it.

One caution if you go looking: a project's README is marketing. At least one
claims the official integration only reads a cached position, which Home
Assistant's own documentation contradicts. Check against the official docs
before switching away from something that works.

### Route 3 — anything else, through generic MQTT

Any other source of a position — an Apple Shortcuts automation, a Home
Assistant automation that pushes instead of waiting to be polled, a script:

```bash
mosquitto_pub -h weasley-clock -u clock -P "$MQTT_PASSWORD" \
  -t clock/location/austin \
  -m '{"lat":40.0150,"lon":-105.2705,"acc":30}'
```

A Home Assistant automation doing the same, which **skips the polling
entirely** — the clock hears about a move when it happens instead of up to
`poll_interval_s` later:

```yaml
automation:
  - alias: Push Austin to the clock
    trigger:
      - platform: state
        entity_id: person.austin
    action:
      - service: mqtt.publish
        data:
          topic: clock/location/austin
          payload: >-
            {"lat": {{ state_attr('person.austin','latitude') }},
             "lon": {{ state_attr('person.austin','longitude') }},
             "acc": {{ state_attr('person.austin','gps_accuracy') | default(100) }}}
```

Guard it against the attribute being `null`, or you will publish
`{"lat": None}` every time that phone goes offline:

```yaml
    condition:
      - condition: template
        value_template: "{{ state_attr('person.austin','latitude') is not none }}"
```

### Which source wins

All of them can run at once. `sources.priority` decides, defaulting to
`[manual, owntracks, generic_mqtt, hass]` — a manual override beats everything,
then OwnTracks, with Find My last because it is the least reliable. In practice
whichever source produces an acceptable fix most recently moves the hand, and
the filters reject the stale and the vague wherever they came from.

The Simulate page has a switch per source, so you can turn Find My off for an
afternoon and see whether OwnTracks alone keeps up.

### What to expect once it is live

* **Find My is not continuous.** A stationary phone may not report for a long
  time. That is normal; `stale_after_s` (6 h) decides when to flag it.
* **Accuracy varies hugely.** A phone on WiFi reports tens of metres, one on a
  cell tower can report kilometres. Anything worse than `max_accuracy_m`
  (2000 m) is rejected, and `hass_check.py` flags those in advance.
* **A hand only moves for a change over 0.5 degrees.** On a log scale that is a
  real distance at range: crossing town moves a hand, crossing a room does not.
* **Nobody's password is in this repo.** The Home Assistant token lives in
  `.env` as `HASS_TOKEN`, the broker password as `MQTT_PASSWORD`, and the
  Apple Account credentials never leave Home Assistant.

---

## 3. What each status means

| Status | Means | Do |
|---|---|---|
| `ok` | Recent fix, hand verified and driven | nothing |
| `stale` | No fix for 6 h. The hand holds its last position | check that person's phone, not the clock |
| `override` | A manual fake position is in force | Simulate → Clear, if you forgot about it |
| `unverified` | Nobody has confirmed where this hand points | section 4 |
| `fault` | Cannot be driven — no Pico link, or the firmware reported one | sections 6 and 7 |
| `disabled` | Switched off in `config/clock.yaml` | nothing, if intended |

`stale` almost always means a phone, not a clock. iOS suspends OwnTracks
aggressively; `docs/hardware-plan.html` notes the same about Home Assistant's
iCloud integration, which is why it is the lowest-priority source here.

---

## 4. Zeroing the hands (needs someone at the clock)

Every hand starts `unverified`, and goes back to `unverified` after a power cut,
a firmware upload or an e-stop. Until it is cleared, that hand **will not move**
from location data. That is deliberate: see `docs/ARCHITECTURE.md`.

Send whoever is at the house this link:

```
http://weasley-clock:8080/assist
```

They need to be on the tailnet, or on the house WiFi with the Pi's local address.
The page is written for someone who has never seen the project: big buttons,
one hand at a time, plain language. It takes about a minute per hand.

What they do: find the hand in the colour shown, tap the arrows until it points
straight up at 12, tap **This hand is at 12**. Repeat for all six, then tap
**Put every hand where it belongs**.

You can watch the whole thing from Boulder on the Dashboard while they do it.

If they cannot get to the page, you can do it yourself over the phone: Control →
jog, while they tell you which way the hand is pointing. Slower, but it works.

### Never turn a hand by hand

The wizard jogs the motor with the arrow buttons, and that is the only way to
move a hand. A stepper that is energised holds its position hard, and a hand
is a printed part pressed onto a 5 mm shaft: forcing it shears the boss, or
slips the rotor and silently throws away the zero you just set.

**Check the drivers are really released before touching anything.** Control →
drivers off should free the hands. On a bench build where the A4988s' shared
ENABLE line has been bridged to ground rather than driven by GP14, it does
not: the Pico's `set_enable` becomes a no-op, every driver stays energised,
and **the UI still reports "drivers disabled"**. The page is not lying on
purpose; it is reporting the command it sent, and nothing tells it the pin is
strapped. Two other things follow from the same bridge: `motion.idle_disable`
does nothing, and six 1.5 A steppers are held energised continuously, which is
real heat inside a closed wooden cabinet.

If you are not certain which way a particular board is wired, assume the
motors are live.

---

## 5. Recovering from a power cut or brownout

What happens on its own:

1. The Pi boots, systemd starts `clockd`.
2. The Pico boots and sends `needs_verification`.
3. The controller marks every hand `unverified` and stops driving them.
4. The last fixes, zero offsets and verified flags are read back from
   `var/state.json` — but the verified flags are then cleared, because a hand
   could have been knocked while the power was off.

So: **the clock comes back by itself, but the hands stay put until someone
re-zeros them.** Section 4.

If the hands are only slightly off and you trust them, you can mark them
verified without a trip: Control → **Mark verified** per hand. Only do this if
the dial visually agrees with the table — you are vouching for the position.

---

## 6. The UI will not load

In order:

1. **Is the Pi on the tailnet?** `tailscale status` from your laptop. If
   `weasley-clock` is absent, it is powered off, off the WiFi, or its key
   expired (section 1.3).
2. **Can you ssh?** `ssh hunterboys@weasley-clock`. If yes, the Pi is fine and
   the problem is `clockd`.
3. **Is the service running?**
   ```bash
   systemctl status clockd
   journalctl -u clockd -n 80 --no-pager
   ```
4. **Common causes in the log:**
   * `Refusing to start: No UI password is set` — section 1.5.
   * `Configuration error:` — a bad edit. `git checkout config/clock.yaml`, or
     restore the `.bak` the Config page made.
   * `Address already in use` — an older copy is still running: `sudo systemctl restart clockd`.
5. **Nothing in the log at all?** The Pi may have rebooted mid-write.
   `sudo systemctl restart clockd`.

If you cannot reach the Pi at all, you need someone at the house to power-cycle
it. The hardware watchdog should have done that already if the kernel hung.

---

## 7. The Pico link is down

Banner says `Pico NOT CONNECTED`.

```bash
ls -l /dev/ttyACM*          # is it there at all?
groups hunterboys           # must include dialout
journalctl -u clockd | grep -i pico | tail -20
```

* **No `/dev/ttyACM0`** — the USB cable, or the Pico has lost its firmware.
  Someone at the house can reseat the cable. If it enumerates as `ttyACM1`
  after a replug, switch `serial.port` to `/dev/weasley-pico` (the udev rule
  `install.sh` adds gives it a stable name).
* **Present but no replies** — the firmware is not running. Re-upload it:
  System page → **Upload firmware to the Pico**, or
  `make firmware-upload PICO_PORT=/dev/ttyACM0`. Every hand becomes unverified
  afterwards.
* **Timeouts under load** — look for `watchdog` faults in the log. If the Pi
  stops pinging for 10 s the firmware stops the hands on purpose; that is the
  safety net working, and the cause is usually the Pi being busy or the USB
  link dropping.

The UI keeps working throughout and says what is wrong. The hands just stop.

---

## 8. A hand is in the wrong place

| Symptom | Likely cause | Fix |
|---|---|---|
| Points at the mirror angle | wrong direction | `invert: true` for that person |
| Consistently off by the same amount | zero drifted | re-zero on `/assist` |
| Moves then drifts further every time | losing steps | lower `motion.max_speed`, check the A4988 Vref, check the tube for binding |
| Does not move at all, others do | that channel's wiring, or `enabled: false` | Control → jog it; check STEP/DIR against `docs/circuit-diagram.html` |
| All six stopped | drivers released, or the watchdog tripped | Control → enable drivers |
| Moves in the UI but not in the room | motor, driver or coupling | mechanical; the software did its part |

**Test sweep** on the Control page is the quickest triage: one turn and back,
slowly. It exercises wiring, direction, current limit and the tube stack in
about thirty seconds.

---

## 9. Updating

```
System page → Update from GitHub → (wait) → Restart controller
```

It records the commit it was on first, so **Roll back** has somewhere to go.
Or from a shell:

```bash
cd ~/HunterClock
git pull
./deploy/install.sh          # picks up new dependencies
sudo systemctl restart clockd
```

### Rolling back

System page → **Roll back**, then **Restart controller**. Or:

```bash
cd ~/HunterClock
git checkout --detach "$(cat var/rollback-commit)"
sudo systemctl restart clockd
```

To get back onto the branch afterwards: `git checkout main`.

**Check after any update:** the banner is green, the Pico firmware version is
what you expect, and one hand still jogs.

---

## 10. Getting help, and sending the evidence

System page → **Download diagnostic bundle**. One zip with:

* `snapshot.json` — the full live state
* `events.log` — everything the controller has done
* `system.json` — host health, versions, services, Tailscale
* `config.sanitised.yaml` — the config with every secret-looking value removed
* `store.json`, `mqtt-recent.json`, `environment.txt` (variable **names** only)

It is built to be sent to someone, so it is safe to send. A test asserts that a
real MQTT password and a real password hash are both absent from it.

---

## 11. The clock has fallen off the WiFi

If the house changes its WiFi password, Tailscale goes with it and there is no
way in at all: no screen, no keyboard, no network, and nobody on site who can
read an IP address off anything. That is what the fallback setup hotspot is
for. It is the only part of this system designed to be operated by somebody who
has never heard of it.

### What it does on its own

Nothing, almost all the time. The clock watches its own network and only acts
when it has been off for **five minutes**:

```
on the WiFi ──falls off──> wait 5 min ──> puts up "weasley-clock-setup"
     ^                          |                      │        ^
     │                          └──comes back──┐       │        │
     │                                         │  nobody comes for 10 min
     └─────────comes back───────── retries saved networks <─────┘
                              for 3 min, then puts it back up
```

The cycling is the important part, and it is why this is safe to leave running.
**The Pi has one WiFi radio**, so while the setup network is up the clock is
offline by definition. A hotspot that latched on would turn a five-minute
router reboot into a clock that is dead until somebody drives to it. Instead it
keeps giving up and retrying, so:

* a router reboot or a brief outage never raises it at all;
* a long power cut raises it, nobody comes, and when the router returns the
  clock finds it **by itself** within about thirteen minutes;
* it only stays up while somebody is actually using the page.

It also never raises the hotspot if the Pi is on **ethernet** — a wired clock
is reachable over Tailscale and needs no rescuing.

### Before the clock travels

**1. Check the passphrase exists.** `deploy/install.sh` generates one into
`.env` as `CLOCK_HOTSPOT_PASSWORD` and prints it once. If you need it again:

```bash
grep CLOCK_HOTSPOT_PASSWORD ~/HunterClock/.env
```

With no passphrase set the clock **will not raise the hotspot at all**, which
is deliberate: an open network called `weasley-clock-setup` would let any
passer-by read the house WiFi password straight back out of the setup page.

**2. Write the label and put it inside the cabinet.** Not in a password
manager, not in this repo — on a piece of paper where the person who needs it
will be standing:

```
    If the clock stops working:
      join WiFi network:  weasley-clock-setup
      password:           <the one from .env>
      a page will appear. Pick the house WiFi and type its password.
```

**3. Prove it comes up on this actual Pi**, because none of this has ever run
on real hardware:

```bash
sudo ~/HunterClock/.venv/bin/python -m clockd.hotspot --once --force
```

That raises the access point immediately and holds it until you press ctrl-C.
**It takes the clock off the WiFi while it runs**, so do this over a wired
connection or while standing next to it, not over Tailscale from another state.
Look for the network on a phone, join it, check the page appears on its own,
then ctrl-C and confirm the clock rejoins the WiFi.

### Talking somebody through it on the phone

This is the script. It needs no technical vocabulary and no shell.

1. "On your phone, open WiFi settings and look for a network called
   **weasley-clock-setup**." *(If it is not there, see below.)*
2. "Join it. The password is on the label inside the clock."
3. "A page should pop up by itself. If it does not, open a browser and go to
   **10.42.0.1**."
4. "Pick your home WiFi from the list and type its password. There is a
   checkbox to show the password — use it."
5. "Press **Connect the clock**. The setup network will disappear. That is
   what is supposed to happen."
6. "If the setup network comes back in a few minutes, the password was not
   right. Join it again and the page will say what went wrong."

**You cannot watch this happen.** Joining the house network tears down the
access point that was serving the page, so there is no "success" screen and
there cannot be one. The feedback is whether the setup network stays gone.

Give it a couple of minutes, then check whether the clock is back on Tailscale.

### If the setup network is not there

In order:

* **Has it been five minutes?** It will not appear sooner.
* **Is the clock actually off the WiFi?** If it is on ethernet, or the WiFi is
  fine and only the internet is down, the hotspot deliberately does not come
  up — and would not help, because it fixes credentials, not an ISP.
* **Is there power?** The hands will be somewhere, but a dark Pi is a dark Pi.
  Ask them to unplug it for ten seconds and plug it back in, then wait the
  five minutes.
* **Is `CLOCK_HOTSPOT_PASSWORD` set?** With none, there is no hotspot. You
  cannot fix this remotely if you are already locked out — it is why step 1
  above is step 1.

### Checking on it remotely, while you still can

```bash
# what it would do right now; changes nothing, safe on the live clock
~/HunterClock/.venv/bin/python -m clockd.hotspot --status

# has it ever had to act?
journalctl -u clock-hotspot -n 50 --no-pager
```

`--status` prints whether `nmcli` is present, whether the passphrase is set,
whether the clock is online, and which networks are saved with what priority.
If it says `blocked:` anything, the hotspot is not going to happen and that
line says why.

To see the page itself without a Pi — worth doing once, so you know what you
are describing over the phone:

```bash
python -m clockd.hotspot --demo      # then open http://127.0.0.1:8081/
```

### Turning it off

Tailscale plus two pre-saved networks (§1.2) covers much of the same ground. If
you would rather not have a service that can take the clock off the WiFi:

```bash
sudo systemctl disable --now clock-hotspot
```

or set `enabled: false` in `config/hotspot.yaml`.

### What has not been tested

No Pi has run any of this. In particular, whether the Pi's WiFi firmware will
act as an access point at all on this board, whether the captive-portal popup
fires on a given iOS or Android version, and whether the timings are right are
all unverified. `PROGRESS.md` keeps the full list.

---

## 12. Reaching someone on site

Keep this filled in:

| Who | Phone | Notes |
|---|---|---|
| | | can power-cycle the Pi |
| | | can open `/assist` on a phone |
| | | knows the WiFi password |
| | | can find the label inside the cabinet (section 11) |

What to ask them for, in order of how often it helps:

1. "Is the little green light on the Pi on?"
2. "Can you unplug the Pi for ten seconds and plug it back in?"
3. "Can you open this link on your phone and tap the buttons?" (`/assist`)
4. "Is the USB cable between the two small boards still plugged in at both ends?"

Do not ask them to type commands. If it needs a shell, it needs Tailscale.

If Tailscale is down because the clock is off the WiFi, that is section 11:
the clock puts up its own network and somebody standing there can fix it from
a phone. That is the one case where somebody on site can do something you
cannot do from Boulder.

---

## 13. Quick reference

```bash
# status and logs
systemctl status clockd
journalctl -u clockd -f
journalctl -u clockd --since "1 hour ago" | grep -i error

# restart
sudo systemctl restart clockd

# the broker
./deploy/mosquitto-setup.sh --show          # which accounts exist
journalctl -u mosquitto -f                  # who is connecting and failing

# watch MQTT
mosquitto_sub -h localhost -u clock -P "$MQTT_PASSWORD" -t 'clock/#' -v

# send a fake location
mosquitto_pub -h localhost -u clock -P "$MQTT_PASSWORD" \
  -t clock/location/austin -m '{"lat":40.0150,"lon":-105.2705,"acc":30}'

# is the Pico there
ls -l /dev/ttyACM* /dev/weasley-pico

# re-upload the firmware
cd ~/HunterClock && make firmware-upload

# the fallback setup hotspot (section 11)
python -m clockd.hotspot --status           # what it would do; changes nothing
journalctl -u clock-hotspot -n 50           # has it ever had to act?
python -m clockd.hotspot --demo             # look at the page, no Pi needed
sudo python -m clockd.hotspot --once --force  # raise it NOW (takes WiFi down)

# try it with no hardware at all
.venv/bin/python -m clockd.cli --sim --sim-fixtures --port 8081
```

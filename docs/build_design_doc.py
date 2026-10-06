"""Build docs/Weasley_Clock_Design.pdf (run from the repo root: python docs/build_design_doc.py)."""
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Table, TableStyle

REPO = "https://github.com/Austin-Hunter1/HunterClock"
VIEWER = "https://claude.ai/artifact/VGXRxYqbKpFKdoPEkXQQYK"
PLAN = "https://claude.ai/artifact/7U4RLGXsm4w6TAcJvgQopS"
CIRCUIT = "https://claude.ai/artifact/6CB7icqFPn5Q8jYG5KjDA8"
CHECKLIST = "https://claude.ai/artifact/RxCKqw6WKXXkBbC6VEKhYf"

INK = colors.HexColor("#2B2419")
MUTED = colors.HexColor("#7A6E5C")
LINE = colors.HexColor("#D9CFBD")
ACCENT = colors.HexColor("#8C5A2B")
TINT = colors.HexColor("#F3EDE0")
PERSON = {"Becky": "#4F7A4A", "Elijah": "#3E6470", "Caleb": "#A8672B",
          "Austin": "#8A4B6B", "Micah": "#B0562A", "Evan": "#5B5EA6"}

body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14.5, textColor=INK, alignment=TA_LEFT, spaceAfter=6)
small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=11.5, textColor=MUTED)
h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=INK, spaceBefore=14, spaceAfter=6, keepWithNext=1)
h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=ACCENT, spaceBefore=8, spaceAfter=3, keepWithNext=1)
title = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=26, leading=30, textColor=INK)
sub = ParagraphStyle("sub", parent=body, fontSize=11.5, leading=16, textColor=MUTED, spaceAfter=10)
cell = ParagraphStyle("cell", parent=body, fontSize=8.8, leading=11.5, spaceAfter=0)
cellb = ParagraphStyle("cellb", parent=cell, fontName="Helvetica-Bold")
bullet = ParagraphStyle("bullet", parent=body, leftIndent=14, bulletIndent=2, spaceAfter=3)


def link(url, text=None):
    return f'<a href="{url}" color="#8C5A2B"><u>{text or url}</u></a>'


def img(path, width):
    w, h = PILImage.open(path).size
    return Image(path, width=width, height=width * h / w)


def table(rows, widths, head=True):
    data = [[Paragraph(str(c), cellb if (head and i == 0) else cell) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if head else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
          ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    if head:
        st += [("BACKGROUND", (0, 0), (-1, 0), TINT), ("LINEBELOW", (0, 0), (-1, 0), 0.8, ACCENT)]
    t.setStyle(TableStyle(st))
    return KeepTogether(t)


def bullets(items):
    return [Paragraph(i, bullet, bulletText="•") for i in items]


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.8 * inch, 0.5 * inch, "Weasley Clock design notes")
    canvas.drawRightString(letter[0] - 0.8 * inch, 0.5 * inch, f"Page {doc.page}")
    canvas.restoreState()


def person(n):
    return f'<font color="{PERSON[n]}"><b>{n}</b></font>'


story = []
W = letter[0] - 1.6 * inch

# ---------------------------------------------------------------- cover block
story += [Paragraph("The Weasley Clock", title),
          Paragraph("Design notes for the family build: where the idea comes from, how it is put together, and what is left to do.", sub)]
story.append(table([
    ["Hardware plan", link(PLAN, "Weasley Clock Hardware Plan") + " (architecture, parts, costs, build phases)"],
    ["Order checklist", link(CHECKLIST, "Weasley Clock Order Checklist")],
    ["Circuit diagram", link(CIRCUIT, "Weasley Clock Circuit Diagram") + " (pin-level wiring)"],
    ["3D model (interactive)", link(VIEWER, "Weasley Clock CAD viewer") + " (case, dial, hands, screens and the motor stack)"],
    ["Code and CAD files", link(REPO)],
], [1.5 * inch, W - 1.5 * inch], head=False))
story.append(Paragraph("The pages and the repository are private. Austin needs to share each page from its Share menu and add each brother as a collaborator on GitHub before the links open for them.", small))

# ---------------------------------------------------------------- 1 inspiration
story.append(Paragraph("1. Inspiration", h1))
story.append(Paragraph(
    "The Weasley Clock comes from the Harry Potter books. The Weasley family keeps a clock at the Burrow with no numbers on it. "
    "Instead, each family member has a hand of their own, and the dial is marked with places and situations such as home, school, work and "
    "travelling, down to \"mortal peril\" on the worst days. The hands move as the family moves, so anyone in the house can see where everyone is at a glance.", body))
story.append(Paragraph(
    "The wizarding world adds a second idea: pictures move. A portrait or a photograph is a small living loop of the person in it.", body))
story.append(Paragraph(
    "Our clock keeps both ideas and changes one. <b>The hands show how far each person is from home</b>, not which category they are in: 12 o'clock means home, "
    "and the farther away someone is, the farther their hand sweeps around the dial. <b>The moving pictures sit on the outside of the clock face</b>, in "
    "round screens beside the dial, each looping a short real video of its person. The case is our own family grandfather clock "
    "(a Howard Miller triple-weight moon-phase model), gutted of its original movement, weights and pendulum.", body))

# ---------------------------------------------------------------- 2 mapping
story.append(Paragraph("2. How distance becomes a hand angle", h1))
story.append(Paragraph(
    "Distance comes from the Haversine formula, clamped to a minimum of 1 km. A log<super>10</super> scale then maps it so that 1 km sits at "
    "0° (12 o'clock) and the antipode, about 20,015 km, sits at 360° (back to 12). The scale is logarithmic so a hand moves "
    "visibly for a 30 km difference and for a 7,000 km one. In log space the mapping is a straight line, and each person is a vertical line at their distance. "
    "The source is birthday_clock.m; the Python scripts and CAD model use the same data.", body))
story.append(img("docs/img/distance_vs_angle.png", W))
story.append(Paragraph("Hand angle against distance from home. The right axis reads the same angle as a clock time, at 30 degrees per hour.", small))
rows = [["Person", "Location", "Distance (km)", "Hand angle"]]
for n, c, d, a in [("Becky", "Minnetonka, MN", "1 (home)", "0.0°"), ("Elijah", "St. Paul, MN", "33", "126.8°"),
                   ("Caleb", "Wheaton, IL", "552", "229.5°"), ("Austin", "Boulder, CO", "1,107", "254.8°"),
                   ("Micah", "Corvallis, OR", "2,338", "281.9°"), ("Evan", "Warsaw, PL", "7,537", "324.5°")]:
    rows.append([person(n), c, d, a])
story.append(table(rows, [1.3 * inch, 2.0 * inch, 1.5 * inch, 1.3 * inch]))
story.append(Paragraph("Angles are measured clockwise from 12 o'clock.", small))

# ---------------------------------------------------------------- 3 mechanical
story.append(Paragraph("3. Mechanical design", h1))
story.append(Paragraph("One hub, six hands", h2))
story.append(Paragraph(
    "All six hands pivot from one centre point, so the hands sit on concentric shafts. Six hollow-shaft stepper motors are chained nose to tail "
    "along the depth axis behind the dial. Each motor's shaft passes through the hollow bores of the motors behind it, and a telescoping brass tube "
    "carries it forward to its hand. The result is the single-pivot look of the show clock, with the whole trunk cavity of the case available for the stack.", body))
story.append(img("docs/img/weasley_clock_overview.png", W))
story.append(Paragraph("System overview and hub cross-section from the hardware plan (visualize_hardware_plan.py). "
                       "The face in the third panel uses the older 180-degree mapping.", small))
story.append(Paragraph("Motors and shafts", h2))
story.append(table([
    ["Level", "Hand", "Motor", "Bore / shaft OD", "Tube to the hand"],
    ["1 (front)", f"{person('Becky')} or {person('Evan')}", "NEMA23 hollow-shaft, 23HS18-2004H", "8 mm / 12 mm", "None; the hand mounts on the motor shaft"],
    ["2", f"{person('Evan')} or {person('Becky')}", "NEMA23 hollow-shaft, 23HS18-2004H", "8 mm / 12 mm", "9/32 in (7.1 mm) brass"],
    ["3", person("Elijah"), "NEMA17 hollow-shaft, 17HS13-1504H", "4 mm / 8 mm", "1/4 in (6.4 mm) brass"],
    ["4", f"{person('Caleb')}, {person('Austin')} or {person('Micah')}", "NEMA17 hollow-shaft, 17HS13-1504H", "4 mm / 8 mm", "1/8 in (3.2 mm) brass"],
    ["5", "(same three)", "NEMA17 hollow-shaft, 17HS13-1504H", "4 mm / 8 mm", "3/32 in (2.4 mm) brass"],
    ["6 (rear)", "(same three)", "NEMA17 hollow-shaft, 17HS13-1504H", "4 mm / 8 mm", "1/16 in (1.6 mm) brass"],
], [0.75 * inch, 1.3 * inch, 2.0 * inch, 1.0 * inch, W - 5.05 * inch]))
story.append(Paragraph(
    "Becky, Evan and Elijah move the most, so they get the three stiffest shafts. Tubes are K&amp;S telescoping brass. "
    "Between tubes 3 and 4 the diameter drops by more than one standard step, because tube 4 must clear the 4 mm bore of the level 3 motor. "
    "That one interface needs a small guide bushing; every other interface is a standard consecutive size. "
    "Which of Becky and Evan takes level 1, and the order of Caleb, Austin and Micah, are not yet decided.", small))
story.append(img("docs/img/view_drive.jpg", W * 0.62))
story.append(Paragraph("The motor stack seen from the side with the case hidden: two NEMA23 motors in front, four NEMA17 behind, shafts passing through each other's bores.", small))
story.append(Paragraph(
    "The hardware plan estimated a stack depth of 300 to 320 mm. In the CAD, using the vendor models, it comes to about 420 mm, "
    "from the front tip of the first motor shaft to the rear tip of the last, because each hollow shaft sticks out of both ends of its motor "
    "(25 mm front and 58 mm rear on the NEMA23, 16 mm and 43 mm on the NEMA17) and 4 mm is left between motors for the couplings. "
    "That is deeper than the plan assumed, so the case measurements matter. A jam anywhere in the stack can bind every hand behind it; "
    "we accepted that in exchange for the single-pivot look.", body))

# ---------------------------------------------------------------- 4 screens
story.append(Paragraph("4. Screens and moving pictures", h1))
story.append(Paragraph(
    "Six round GC9A01 displays (1.28 in, 240 by 240 pixels, SPI), one per person, stand beside the clock face, three on each side. "
    "Each has its own ESP32 and joins the same Wi-Fi as the controller, so there are no wires back to the Pi. "
    "The content is real recorded footage of each person: a 3 to 5 second loop, cropped to a circle, resized to 240 by 240 and "
    "re-encoded as a short frame sequence that fits in the ESP32's flash. A screen also changes state (home, travelling, unknown) on an MQTT command. "
    "In the CAD each module is 39.5 mm across with a 32.4 mm active area.", body))
story.append(img("docs/img/view_hood.jpg", W * 0.5))
story.append(Paragraph("Dial, hands and side screens, from the 3D viewer.", small))

story.append(Paragraph("5. Case", h1))
story.append(Paragraph(
    "A basic outline taken from the reference photos of the family grandfather clock: a plinth, a hollow trunk, an arched hood "
    "with a broken swan-neck pediment and finial, and an arched dial plate. The original movement, weights and pendulum come out. "
    "The motor stack runs straight back from the dial, and the dial's brass corner pieces "
    "can be reused around a new chapter ring. The outline is a rough shape, about 2 m tall and 495 mm deep (sized to hold the motor stack), not measured from the real case.", body))
story.append(img("docs/img/view_front.jpg", W * 0.36))
story.append(Paragraph("Full case from the front, from the 3D viewer.", small))

# ---------------------------------------------------------------- 6 electronics
story.append(Paragraph("6. Electronics and parts", h1))
story.append(Paragraph(
    "Family phones (Find My, with OwnTracks as a fallback) report to Home Assistant, which publishes over MQTT to a Raspberry Pi 4 controller. "
    "The Pi does the Haversine math and routing only. It sends one short command per person over USB serial to a Raspberry Pi Pico, "
    "which generates all six motors' step pulses in hardware and drives six A4988 stepper drivers from a 12 V supply. "
    "The Pi also updates the six ESP32 display nodes over Wi-Fi. Wiring is in the " + link(CIRCUIT, "circuit diagram") + ".", body))
story.append(Paragraph(
    "<b>Order checklist:</b> " + link(CHECKLIST) + ". It lists every part by order, with prices, sources and order numbers, "
    "and has check boxes for tracking what has been bought. Its check marks and notes are saved only in the browser they are made in.", body))
story.append(table([
    ["Module", "Estimated cost"],
    ["Controller and networking (Pi 4, Pico, SD card)", "$119 to $159"],
    ["Six-shaft hub (6 hollow-shaft motors, drivers, tubing, bearings, dial, hands)", "$305"],
    ["Six display nodes (ESP32, GC9A01 screen, bezel, cable)", "$82"],
    ["Power and wiring", "$74"],
    ["Fabrication and mounting", "$40"],
    ["<b>Total</b>", "<b>$620 to $660</b>"],
], [W - 1.6 * inch, 1.6 * inch]))
story.append(Paragraph("Orders placed: Amazon on October 5, 2026 ($147.00: drivers, ESP32s, screens, Pico, power supplies, capacitors, USB charger, breadboards) "
                       "and StepperOnline order 330093 on October 6 (all six hollow-shaft motors and the prototype motor, shipped by DHL from China). "
                       "Still to buy: Pi 4, SD card, extra tubing and bearings, and the fabrication parts.", small))

# ---------------------------------------------------------------- 7 todo
story.append(Paragraph("7. To Do Before Thanksgiving", h1))
story += bullets([
    "Find case dimensions specifically",
    "Design hands &amp; 3D print (see ambigrams for potential hand internals)",
    "Record videos of each person for moving picture frame",
    "Finish code controlling dynamics and updates and visual displays",
])
story.append(Paragraph(
    "The case dimensions decide whether the hub fits: hood depth, trunk depth, and the narrowest interior width. "
    "The CAD stack is about 420 mm deep and the NEMA23 frame is 57 mm square, the widest part of it.", small))

story.append(Paragraph("Files and rebuilding the CAD", h1))
story.append(Paragraph(
    f'Edit build_cad.py in the {link(REPO, "repository")} and push to main. The "Build CAD" workflow regenerates the STEP, STL and GLB files, '
    f'which can be downloaded from the run on the {link(REPO + "/actions", "Actions tab")}. Motor models are in the motors folder.', body))

doc = SimpleDocTemplate("docs/Weasley_Clock_Design.pdf", pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                        topMargin=0.8 * inch, bottomMargin=0.8 * inch, title="The Weasley Clock - Design Notes",
                        author="Austin Hunter")
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print("wrote docs/Weasley_Clock_Design.pdf")

"""Build docs/Weasley_Clock_Design.pdf (run from the repo root: python docs/build_design_doc.py)."""
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)
from PIL import Image as PILImage

REPO = "https://github.com/Austin-Hunter1/HunterClock"
VIEWER = "https://claude.ai/artifact/VGXRxYqbKpFKdoPEkXQQYK"

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


story = []
W = letter[0] - 1.6 * inch

# ---------------------------------------------------------------- cover block
story += [Paragraph("The Weasley Clock", title),
          Paragraph("Design notes for the family build: what it does, how it is put together, what is decided and what is still open.", sub)]
story.append(table([
    ["3D model (interactive)", link(VIEWER, "Open the viewer")],
    ["Source code and CAD files", link(REPO)],
    ["CAD build", f'{link(REPO + "/actions", "GitHub Actions")}: run "Build CAD", download the weasley-clock-cad artifact (STEP, STL, GLB)'],
], [1.6 * inch, W - 1.6 * inch], head=False))
story.append(Paragraph("The repository is private and the viewer link opens only for people it has been shared with. Austin needs to add each brother as a collaborator on GitHub and share the viewer from its Share menu.", small))

# ---------------------------------------------------------------- 1 concept
story.append(Paragraph("1. What it is", h1))
story.append(Paragraph(
    "A grandfather clock rebuilt as a family distance clock. Instead of hours and minutes, six hands, one per person, "
    "sit on one shared hub. Each hand points to how far that person is from home (Minnetonka, MN). "
    "12 o'clock means home. Six flanking screens show each person's name, city and distance.", body))
story.append(img("docs/img/weasley_clock_face_and_screens.png", W * 0.6))
story.append(Paragraph("Face and two-screen layout (mockup from visualize_two_screen_layout.py).", small))

# ---------------------------------------------------------------- 2 mapping
story.append(Paragraph("2. How distance becomes a hand angle", h1))
story.append(Paragraph(
    "Distance comes from the Haversine formula, clamped to a minimum of 1 km. A log<super>10</super> scale then maps it so that 1 km sits at "
    "0° (12 o'clock) and the antipode, about 20,015 km, sits at 360° (back to 12). The scale is logarithmic so a hand moves "
    "visibly for a 30 km difference and for a 7,000 km one. The full 360° sweep is the locked design; the older 180° "
    "version in the first sketches is superseded. The source is birthday_clock.m, and the Python scripts and CAD model use the same data.", body))
rows = [["Person", "Location", "Distance (km)", "Hand angle"]]
for n, c, d, a in [("Becky", "Minnetonka, MN", "1 (home)", "0.0°"), ("Elijah", "St. Paul, MN", "33", "126.8°"),
                   ("Caleb", "Wheaton, IL", "552", "229.5°"), ("Austin", "Boulder, CO", "1,107", "254.8°"),
                   ("Micah", "Corvallis, OR", "2,338", "281.9°"), ("Evan", "Warsaw, PL", "7,537", "324.5°")]:
    rows.append([f'<font color="{PERSON[n]}"><b>{n}</b></font>', c, d, a])
story.append(table(rows, [1.3 * inch, 2.0 * inch, 1.5 * inch, 1.3 * inch]))
story.append(Paragraph("Angles are measured clockwise from 12 o'clock.", small))

# ---------------------------------------------------------------- 3 mechanical
story.append(Paragraph("3. Mechanical design", h1))
story.append(Paragraph("The hub", h2))
story.append(Paragraph(
    "Six hollow shafts nest inside one another on a single axis, so all six hands pivot about the same point. "
    "The outermost shaft belongs to the shallowest hand. Each hand is mounted on the end of its own shaft, stacked a few millimetres "
    "apart in front of the dial. Each shaft is turned by its own stepper motor.", body))
story.append(img("docs/img/weasley_clock_overview.png", W))
story.append(Paragraph("System overview and hub cross-section from the hardware plan (visualize_hardware_plan.py). "
                       "The 180° face in the third panel is the older mapping.", small))
story.append(Paragraph("Motors and drive", h2))
story.append(Paragraph(
    "Per the hardware plan, the two outer shafts use NEMA23 motors (23HS18-2004H) and the four inner shafts use NEMA17 motors (17HS13-1504H). "
    "Solid-shaft motors cannot all sit on the clock's axis, because the inner shafts would have to pass through them. "
    "The CAD therefore puts the six motors in a vertical column down the trunk behind the dial. Each motor belts to a pulley "
    "on its own shaft, in its own plane, so the belts do not touch.", body))
story.append(table([
    ["Level", "Person", "Shaft OD", "Motor", "Belt plane (behind dial)", "Motor axis (below clock axis)"],
    ["1 (outer)", "Becky", "26 mm", "NEMA23", "55 mm", "85 mm"],
    ["2", "Elijah", "23 mm", "NEMA23", "69 mm", "147 mm"],
    ["3", "Caleb", "20 mm", "NEMA17", "83 mm", "209 mm"],
    ["4", "Austin", "17 mm", "NEMA17", "97 mm", "271 mm"],
    ["5", "Micah", "14 mm", "NEMA17", "111 mm", "333 mm"],
    ["6 (inner)", "Evan", "11 mm", "NEMA17", "125 mm", "395 mm"],
], [0.8 * inch, 0.8 * inch, 0.8 * inch, 0.8 * inch, 1.6 * inch, 2.1 * inch]))
story.append(Paragraph("The person-to-level assignment is a placeholder. All sizes in this table are placeholders until parts are in hand.", small))
story.append(img("docs/img/view_drive.jpg", W * 0.5))
story.append(Paragraph("Drive column with the case hidden, from the 3D viewer. The two NEMA23 motors are behind the dial plate in this view.", small))

# ---------------------------------------------------------------- 4 screens + case
story.append(Paragraph("4. Screens", h1))
story.append(Paragraph(
    "Six round GC9A01 displays (1.28 in, 240×240, SPI) are on order. They are mounted in two vertical panels, one on each side of the dial, "
    "three per panel. The left panel holds the near cluster (Becky, Elijah, Caleb) and the right panel holds the far cluster "
    "(Austin, Micah, Evan). Each screen shows the person's portrait or status, name, distance and last update. "
    "In the CAD each module is 39.5 mm across with a 32.4 mm active area.", body))
story.append(img("docs/img/view_hood.jpg", W * 0.66))
story.append(Paragraph("Dial, hands and side screen panels, from the 3D viewer.", small))

story.append(Paragraph("5. Case", h1))
story.append(Paragraph(
    "A basic outline taken from the reference photos of the family grandfather clock: a plinth, a hollow trunk, an arched hood "
    "with a broken swan-neck pediment and finial, and an arched dial plate. The pendulum and weights are removed so the trunk can hold the "
    "motor column and electronics. It is about 2 m tall. This is a rough shape for checking fit, not a finished design.", body))
story.append(img("docs/img/view_front.jpg", W * 0.5))
story.append(Paragraph("Full case from the front, from the 3D viewer.", small))

# ---------------------------------------------------------------- 6 electronics
story.append(Paragraph("6. Electronics", h1))
story.append(Paragraph(
    "Data flow from the plan: family phones (Find My or OwnTracks) report to Home Assistant, which publishes over MQTT to a Raspberry Pi 4 "
    "controller. The Pi computes the angles, drives the six steppers through a Raspberry Pi Pico over USB serial, and updates the six ESP32 display nodes over Wi-Fi. "
    "Parts ordered on October 5, 2026:", body))
story += bullets([
    "10 stepper driver modules with heat sinks (six needed, four spare)",
    "6 ESP32 development boards, one per screen",
    "3-pack of GC9A01 round displays, quantity 2 (six screens)",
    "Raspberry Pi Pico starter kit, for the motor controller",
    "12 V 2 A supply, DC jacks and 100 µF capacitors, for the motor supply",
    "5 V 3 A USB-C supply for the Raspberry Pi 4, plus a 10-port USB charger",
    "Solderable breadboards for the driver wiring",
])
story.append(Paragraph("Not on that order: the stepper motors themselves, the Raspberry Pi 4, belts and pulleys, and mounting hardware. "
                       "A 12 V 2 A supply is likely too small for six steppers, especially the NEMA23 pair; confirm the current budget before wiring.", body))

# ---------------------------------------------------------------- 7 open
story.append(Paragraph("7. Open questions", h1))
story += bullets([
    "Who is on which shaft level. The CAD uses list order as a placeholder.",
    "Whether belts and a column of offset motors is how we want to drive the nested shafts, or whether we want hollow-shaft motors or gears.",
    "Motor torque on the outer hands, now that the NEMA23 pair is on the outermost shafts.",
    "Case dimensions, dial size (110 mm radius is a guess) and how the hub mounts to the case.",
    "Hand design: the CAD hands are plain tapered pointers.",
    "Screen content and how portraits animate.",
])

story.append(Paragraph("How to rebuild the CAD", h1))
story.append(Paragraph(
    f'Edit build_cad.py in the repository and push to main. The "Build CAD" workflow regenerates the STEP, STL and GLB files. '
    f'Download them from the workflow run, or run it by hand from the {link(REPO + "/actions", "Actions tab")}. '
    "Motor models live in the motors folder.", body))

doc = SimpleDocTemplate("docs/Weasley_Clock_Design.pdf", pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                        topMargin=0.8 * inch, bottomMargin=0.8 * inch, title="The Weasley Clock - Design Notes",
                        author="Austin Hunter")
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print("wrote docs/Weasley_Clock_Design.pdf")

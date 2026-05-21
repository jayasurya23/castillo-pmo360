"""
Castillo-branded PDF for meeting minutes.

Layout matches the reference deliverable (e.g. ``Meeting Minutes - E-Light -
04-22-26.pdf``):
  - Red banner on every page (top + thin bar on continuation pages)
  - First-page header: "PMO 360™" on the left, "Meeting Minutes" + date right
  - Red section headings (Attendees, Agenda, Discussion Points, Action Items,
    Closing Remarks)
  - Bulleted attendee lines grouped by org
  - Bulleted agenda items in bold
  - Bulleted discussion points with bold labels and nested sub-points
  - Action items table with colored status pills (Open/Pending/Completed/Cancelled)
  - Footer "Castillo Engineering | Project Management Office | Confidential"
    on the left and the meeting date on the right, on every page

Phase 3 ``add_status_form_fields`` is still a no-op stub.
"""
from io import BytesIO
from pathlib import Path
from typing import Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
    Table, TableStyle, ListFlowable, ListItem, KeepTogether, Flowable,
)

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

from db.models import Meeting
from config import BrandColors


# ============================================================
# Brand-asset paths
# ============================================================
# Logo files live in <repo>/assets/logo. Each PDF generation calls
# `_get_logo_reader` which lazily wraps the file in a ReportLab ImageReader;
# the result is cached at module level so repeated draws share the parsed
# image data instead of reparsing the PNG every page.
_LOGO_DIR = Path(__file__).resolve().parent.parent / "assets" / "logo"
_LOGO_PATHS = {
    "pmo360":         _LOGO_DIR / "pmo360_logo.png",
    "castillo_white": _LOGO_DIR / "Castillo_logo.png",
    "castillo_color": _LOGO_DIR / "Castillo_logo_color.png",
}
_LOGO_CACHE: dict[str, ImageReader] = {}


def _get_logo_reader(name: str) -> Optional[ImageReader]:
    """Return a ReportLab ImageReader for the named logo, or None if the
    file doesn't exist. Cached for the lifetime of the process."""
    if name in _LOGO_CACHE:
        return _LOGO_CACHE[name]
    path = _LOGO_PATHS.get(name)
    if path and path.exists():
        reader = ImageReader(str(path))
        _LOGO_CACHE[name] = reader
        return reader
    return None


# ============================================================
# Font registration — try to use Jost, fall back to Helvetica
# ============================================================
def _try_register_jost() -> tuple[str, str]:
    """Look for Jost-Regular / Jost-Bold TTFs in common locations and
    register them with ReportLab. Returns (regular_font_name, bold_font_name).
    Falls back to Helvetica when not found."""
    import os
    user_fonts = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
    repo_fonts = Path(__file__).resolve().parent.parent / "templates" / "fonts"

    candidates_reg = [
        repo_fonts / "Jost-Regular.ttf",
        Path(r"C:\Windows\Fonts\Jost-Regular.ttf"),
        user_fonts / "Jost-Regular.ttf",
        Path(r"C:\Windows\Fonts\Jost.ttf"),
    ]
    candidates_bold = [
        repo_fonts / "Jost-Bold.ttf",
        Path(r"C:\Windows\Fonts\Jost-Bold.ttf"),
        user_fonts / "Jost-Bold.ttf",
    ]

    reg_path = next((p for p in candidates_reg if p.exists()), None)
    bold_path = next((p for p in candidates_bold if p.exists()), None)

    if reg_path:
        try:
            pdfmetrics.registerFont(TTFont("Jost", str(reg_path)))
            if bold_path:
                pdfmetrics.registerFont(TTFont("Jost-Bold", str(bold_path)))
            else:
                # No bold TTF — let ReportLab synthesize from the regular file
                pdfmetrics.registerFont(TTFont("Jost-Bold", str(reg_path)))
            from reportlab.pdfbase.pdfmetrics import registerFontFamily
            registerFontFamily(
                "Jost", normal="Jost", bold="Jost-Bold",
                italic="Jost", boldItalic="Jost-Bold",
            )
            return ("Jost", "Jost-Bold")
        except Exception:
            pass
    return ("Helvetica", "Helvetica-Bold")


PRIMARY_FONT, PRIMARY_BOLD = _try_register_jost()


# ============================================================
# Colors
# ============================================================
RED = HexColor(BrandColors.RED)
DARK_RED = HexColor("#8B1F2B")            # banner background — slightly darker
NEAR_BLACK = HexColor(BrandColors.NEAR_BLACK)
DARK_GRAY = HexColor(BrandColors.DARK_GRAY)
LIGHT_GRAY = HexColor(BrandColors.LIGHT_GRAY)
NEAR_WHITE = HexColor(BrandColors.NEAR_WHITE)

# Status pill colors — pulled from the Castillo secondary palette.
# Each entry is (background, text-color) so the cancelled pill (light gray)
# uses dark text for contrast while the others use white.
STATUS_COLORS = {
    "open":      HexColor("#1aa6c9"),    # blue
    "pending":   HexColor("#c7bb2e"),    # gold
    "completed": HexColor("#278747"),    # green
    "cancelled": HexColor("#e6e7e8"),    # light gray
}
# Black text across all statuses: the AcroForm dropdown popover in Acrobat
# uses the field's text color for the option list too, so a white textColor
# would render white-on-white when the dropdown is open. Black reads on
# every pill background (blue / gold / green / light-gray).
STATUS_TEXT_COLORS = {
    "open":      HexColor("#1a1a1a"),
    "pending":   HexColor("#1a1a1a"),
    "completed": HexColor("#1a1a1a"),
    "cancelled": HexColor("#1a1a1a"),
}


# ============================================================
# Paragraph styles
# ============================================================
def _styles() -> dict:
    return {
        "project": ParagraphStyle(
            "project", fontName=PRIMARY_BOLD, fontSize=20,
            textColor=NEAR_BLACK, spaceAfter=3, leading=24,
        ),
        "kv": ParagraphStyle(
            "kv", fontName=PRIMARY_FONT, fontSize=10, textColor=NEAR_BLACK,
            leading=14,
        ),
        "section": ParagraphStyle(
            "section", fontName=PRIMARY_BOLD, fontSize=15, textColor=RED,
            spaceBefore=12, spaceAfter=5, leading=19,
        ),
        "attendee_line": ParagraphStyle(
            "attendee_line", fontName=PRIMARY_FONT, fontSize=10,
            textColor=NEAR_BLACK, leading=14, spaceAfter=3,
        ),
        "bullet": ParagraphStyle(
            "bullet", fontName=PRIMARY_FONT, fontSize=10, textColor=NEAR_BLACK,
            leading=14, spaceAfter=2,
        ),
        "subbullet": ParagraphStyle(
            "subbullet", fontName=PRIMARY_FONT, fontSize=10, textColor=NEAR_BLACK,
            leading=14, spaceAfter=2,
        ),
        "closing": ParagraphStyle(
            "closing", fontName=PRIMARY_FONT, fontSize=10, textColor=NEAR_BLACK,
            leading=14,
        ),
        "table_body": ParagraphStyle(
            "table_body", fontName=PRIMARY_FONT, fontSize=9,
            textColor=NEAR_BLACK, leading=12,
        ),
        "table_status": ParagraphStyle(
            "table_status", fontName=PRIMARY_BOLD, fontSize=10,
            textColor=white, leading=12, alignment=TA_LEFT,
        ),
    }


# ============================================================
# Page-template canvas painting
# ============================================================
def _draw_header_band(canvas, doc):
    """Top red bar on every page. The first page has it taller and is followed
    by the PMO 360 + page-title block — both drawn by this function. The
    page title text ('Meeting Minutes' / 'Pre-Meeting Coordination Agenda' /
    etc.) and date string come from ``doc._page_title`` / ``doc._meeting_date_str``."""
    width, height = letter
    canvas.saveState()
    page_title = getattr(doc, "_page_title", "Meeting Minutes")

    if doc.page == 1:
        # Tall band at the top + page title block underneath
        band_h = 36
        canvas.setFillColor(DARK_RED)
        canvas.rect(0, height - band_h, width, band_h, fill=1, stroke=0)
        # Top-right: Castillo white logo on the red banner (replaces the
        # "Castillo Engineering" text). Aspect ratio 2.82:1.
        castillo_img = _get_logo_reader("castillo_white")
        if castillo_img is not None:
            logo_h = 26
            logo_w = logo_h * 2.82
            canvas.drawImage(
                castillo_img,
                width - logo_w - 12,
                height - band_h + (band_h - logo_h) / 2,
                width=logo_w, height=logo_h,
                mask="auto",
            )
        else:
            canvas.setFillColor(white)
            canvas.setFont(PRIMARY_BOLD, 12)
            canvas.drawRightString(width - 28, height - 24, "Castillo Engineering")

        # Below the band — PMO 360 logo (replaces the text PMO + 360 + ™).
        # Aspect ratio 5.41:1. Width ~138pt matches the original text footprint.
        pmo_img = _get_logo_reader("pmo360")
        if pmo_img is not None:
            logo_w = 140
            logo_h = logo_w / 5.41
            canvas.drawImage(
                pmo_img,
                48, height - 50 - logo_h,
                width=logo_w, height=logo_h,
                mask="auto",
            )
        else:
            canvas.setFillColor(NEAR_BLACK)
            canvas.setFont(PRIMARY_BOLD, 30)
            canvas.drawString(48, height - 80, "PMO")
            canvas.setFillColor(RED)
            canvas.drawString(48 + 76, height - 80, "360")
            canvas.setFont(PRIMARY_BOLD, 12)
            canvas.drawString(48 + 76 + 60, height - 64, "™")
            canvas.setStrokeColor(RED)
            canvas.setLineWidth(1.5)
            canvas.line(48, height - 84, 48 + 138, height - 84)

        # Right: page title + date. Auto-size the title font so longer
        # titles (e.g. "Pre-Meeting Coordination Agenda") still fit.
        title_size = 22 if len(page_title) <= 18 else (18 if len(page_title) <= 30 else 15)
        canvas.setFillColor(RED)
        canvas.setFont(PRIMARY_BOLD, title_size)
        canvas.drawRightString(width - 48, height - 70, page_title)
        canvas.setFillColor(NEAR_BLACK)
        canvas.setFont(PRIMARY_FONT, 10)
        canvas.drawRightString(width - 48, height - 86, doc._meeting_date_str)
    else:
        # Thinner band on continuation pages — Castillo white logo only.
        band_h = 22
        canvas.setFillColor(DARK_RED)
        canvas.rect(0, height - band_h, width, band_h, fill=1, stroke=0)
        castillo_img = _get_logo_reader("castillo_white")
        if castillo_img is not None:
            logo_h = 14
            logo_w = logo_h * 2.82
            canvas.drawImage(
                castillo_img,
                width - logo_w - 12,
                height - band_h + (band_h - logo_h) / 2,
                width=logo_w, height=logo_h,
                mask="auto",
            )
        else:
            canvas.setFillColor(white)
            canvas.setFont(PRIMARY_BOLD, 10)
            canvas.drawRightString(width - 28, height - 15, "Castillo Engineering")

    # Footer on every page
    canvas.setFillColor(NEAR_BLACK)
    canvas.setFont(PRIMARY_FONT, 8)
    canvas.drawString(
        48, 28,
        "Castillo Engineering  |  Project Management Office  |  Confidential",
    )
    canvas.drawRightString(width - 48, 28, doc._meeting_date_str)

    canvas.restoreState()


# ============================================================
# Status cell — colored pill background drawn by the table, plus an
# AcroForm combo-box overlaid on top so the client can change the
# status in Adobe Reader and the PDF saves the new value.
# ============================================================
STATUS_OPTIONS = ("Open", "Pending", "Completed", "Cancelled")


def _status_pill_text(status: str) -> str:
    return (status or "").strip().capitalize() or "Open"


class StatusFormField(Flowable):
    """A zero-height-ink Flowable that, when drawn, overlays an AcroForm
    choice (dropdown) widget at the flowable's current canvas position.

    ReportLab places this inside a table cell; on draw() the canvas is already
    translated to the cell's origin, so we can use (0, 0)..(width, height)
    coordinates directly. The visual pill is drawn by the table's BACKGROUND
    + the text we draw here; the AcroForm sits on top with `borderStyle="N"`
    so it's invisible but clickable, and reveals a dropdown in Reader.
    """

    def __init__(self, field_name: str, value: str,
                 width_pt: float, height_pt: float, text_color=white):
        super().__init__()
        self.field_name = field_name
        self.value = _status_pill_text(value)
        self.width = width_pt
        self.height = height_pt
        self.text_color = text_color

    def wrap(self, available_w, available_h):
        # Fill the cell — ReportLab will give us the cell width as available_w.
        # We respect the requested height set at construction.
        return (self.width, self.height)

    def draw(self):
        canv = self.canv
        # AcroForm widget rects are stored as ABSOLUTE page coordinates — they
        # don't honor the current transform. So we extract the cell's absolute
        # origin from the canvas's accumulated matrix (the table renderer has
        # already translated to the cell's bottom-left) and pass it in.
        a, b, c, d, e, f = canv._currentMatrix
        abs_x, abs_y = e, f

        # The AcroForm widget renders the current value itself, so we don't
        # also drawString here — that previously produced a duplicate label.
        # AcroForm widgets only support the 14 standard PDF fonts — use the
        # Helvetica-Bold fallback regardless of the document's primary font.
        canv.acroForm.choice(
            name=self.field_name,
            value=self.value,
            options=list(STATUS_OPTIONS),
            x=abs_x, y=abs_y,
            width=self.width, height=self.height,
            borderStyle="N",
            borderWidth=0,
            forceBorder=False,
            fillColor=None,
            textColor=self.text_color,
            fontName="Helvetica-Bold",
            fontSize=9,
            tooltip="Click to change status",
        )


# ============================================================
# Discussion-points flowable (recursive with nested sub-bullets)
# ============================================================
def _build_discussion_flowables(meeting, styles) -> list:
    """Walk the discussion-points tree (top-level by parent_id IS NULL, then
    each parent's sub_points relationship/list) and produce a flat list of
    flowables that ReportLab can render in document order."""
    out = []

    # `dp.sub_points` works for both DB-mapped DiscussionPoint (relationship)
    # and Pydantic ParsedDiscussionPoint (list).
    def _children(dp):
        kids = getattr(dp, "sub_points", None) or []
        # Sort by order_index when present (DB objects); Pydantic models have it too only if persisted
        try:
            return sorted(kids, key=lambda k: getattr(k, "order_index", 0))
        except Exception:
            return list(kids)

    top_level = [
        dp for dp in meeting.discussion_points
        if getattr(dp, "parent_id", None) is None
    ]
    top_level.sort(key=lambda d: getattr(d, "order_index", 0))

    # Flatten the tree into Paragraphs with explicit bullet markers + indent.
    # Top-level → solid disc (●), sub → hollow circle (○) further indented.
    from reportlab.lib.styles import ParagraphStyle as _PS
    top_p_style = _PS(
        "dp_top", parent=styles["bullet"],
        leftIndent=18, bulletIndent=4, firstLineIndent=0,
        bulletFontSize=10, bulletFontName=PRIMARY_BOLD,
    )
    sub_p_style = _PS(
        "dp_sub", parent=styles["subbullet"],
        leftIndent=44, bulletIndent=30, firstLineIndent=0,
        # Lowercase 'o' bold is rendered as a small circle-shape by every
        # PDF viewer (no glyph-substitution surprises like with U+25CB).
        bulletFontSize=10, bulletFontName=PRIMARY_BOLD,
    )

    def _flatten(dp, level: int):
        label_md = f"<b>{dp.label}:</b> " if dp.label else ""
        text = label_md + (dp.content or "")
        if level == 0:
            out.append(Paragraph(text, top_p_style, bulletText="•"))   # filled
        else:
            out.append(Paragraph(text, sub_p_style, bulletText="o"))   # hollow-look
        for child in _children(dp):
            _flatten(child, level + 1)

    for dp in top_level:
        _flatten(dp, 0)
    return out


# ============================================================
# Deliverable rows — manual MeetingDeliverable rows, else upcoming
# leaf tasks from the project's latest uploaded Schedule.
# ============================================================
def _gather_deliverable_rows(meeting: Meeting) -> list[list[str]]:
    """Manual MeetingDeliverable rows only. The Review page lets the PM
    explicitly pick from the project's Schedule — we never auto-pull."""
    rows: list[list[str]] = []
    for idx, md in enumerate(meeting.meeting_deliverables, start=1):
        d = md.deliverable
        rows.append([
            str(idx),
            d.project_segment or meeting.project.name,
            d.task,
            d.start_status or "In Progress",
            d.delivery_date.strftime("%m/%d/%Y") if d.delivery_date else "",
        ])
    return rows


# ============================================================
# Public entry point
# ============================================================
def generate_meeting_minutes_pdf(meeting: Meeting, output_path: Optional[Path] = None) -> bytes:
    buf = BytesIO()

    # Build the doc with two page templates: first page leaves room for the
    # title block; later pages start higher.
    doc = BaseDocTemplate(
        buf, pagesize=letter,
        leftMargin=48, rightMargin=48,
        topMargin=110, bottomMargin=54,
    )
    # Stash date string + page title so the page-painter can use it
    doc._meeting_date_str = meeting.meeting_date.strftime("%B %d, %Y")
    doc._page_title = "Meeting Minutes"

    width, height = letter
    frame_first = Frame(
        48, 54, width - 96, height - 110 - 54,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="first_frame",
    )
    frame_later = Frame(
        48, 54, width - 96, height - 50 - 54,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="later_frame",
    )
    doc.addPageTemplates([
        PageTemplate(id="first", frames=[frame_first], onPage=_draw_header_band),
        PageTemplate(id="later", frames=[frame_later], onPage=_draw_header_band),
    ])

    s = _styles()
    story = []

    # ---- Project block (no separate title — title is drawn on the canvas) ----
    story.append(Paragraph(meeting.project.name, s["project"]))
    if meeting.project.client:
        story.append(Paragraph(
            f"<b>Client:</b>&nbsp;&nbsp;&nbsp;&nbsp;{meeting.project.client.name}",
            s["kv"],
        ))
    if meeting.project.scope:
        story.append(Paragraph(
            f"<b>Scope:</b>&nbsp;&nbsp;&nbsp;&nbsp;{meeting.project.scope}",
            s["kv"],
        ))

    # ---- Attendees ----
    story.append(Paragraph("Attendees", s["section"]))
    by_org = {}
    for a in meeting.attendees:
        by_org.setdefault(a.organization or "Other", []).append(a)
    for org, attendees in by_org.items():
        names = ", ".join(f"{a.full_name} ({a.initials})" for a in attendees)
        story.append(Paragraph(f"<b>{org}:</b> {names}", s["attendee_line"]))

    # ---- Deliverable Timelines (manual rows or fallback to upcoming ScheduleItems) ----
    deliv_rows = _gather_deliverable_rows(meeting)
    if deliv_rows:
        story.append(Paragraph("Deliverable Timelines", s["section"]))
        table_data = [["#", "Project", "Task", "Start", "Delivery"]]
        for r in deliv_rows:
            table_data.append([
                r[0],
                r[1],
                Paragraph(r[2], s["table_body"]),
                r[3],
                r[4],
            ])
        deliv_table = Table(
            table_data,
            colWidths=[0.35 * inch, 1.55 * inch, 3.10 * inch, 1.10 * inch, 1.10 * inch],
            repeatRows=1,
        )
        deliv_table.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), DARK_RED),
            ("TEXTCOLOR",     (0, 0), (-1, 0), white),
            ("FONTNAME",      (0, 0), (-1, 0), PRIMARY_BOLD),
            ("FONTSIZE",      (0, 0), (-1, 0), 11),
            ("FONTNAME",      (0, 1), (-1, -1), PRIMARY_FONT),
            ("FONTSIZE",      (0, 1), (-1, -1), 10),
            ("TEXTCOLOR",     (0, 1), (-1, -1), NEAR_BLACK),
            ("VALIGN",        (0, 0), (-1, 0),  "MIDDLE"),
            ("VALIGN",        (0, 1), (-1, -1), "TOP"),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ("LINEBELOW",     (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ]))
        story.append(deliv_table)

    # ---- Agenda ----
    # Render with the same ● bullet style + paragraph indent as Discussion
    # Points below, so visual weight is uniform between the two sections.
    if meeting.agenda_items:
        from reportlab.lib.styles import ParagraphStyle as _PS
        story.append(Paragraph("Agenda", s["section"]))
        agenda_p_style = _PS(
            "agenda_item", parent=s["bullet"],
            leftIndent=18, bulletIndent=4, firstLineIndent=0,
            bulletFontSize=10, bulletFontName=PRIMARY_BOLD,
        )
        for item in sorted(meeting.agenda_items, key=lambda a: a.order_index):
            story.append(Paragraph(
                item.text or "", agenda_p_style, bulletText="•",
            ))

    # ---- Discussion Points (with sub-points) ----
    if meeting.discussion_points:
        story.append(Paragraph("Discussion Points", s["section"]))
        story.extend(_build_discussion_flowables(meeting, s))

    # ---- Action Items ----
    if meeting.raised_actions:
        story.append(Paragraph("Action Items", s["section"]))
        # Header row + data rows
        table_data = [["#", "Action", "Owner", "Due", "Status"]]
        # Status cell footprint — the AcroForm widget needs to know its size
        # at build time. Match the table's column width + a comfortable row
        # height (the colored pill background fills this exact area).
        STATUS_CELL_W = 0.95 * inch
        STATUS_CELL_H = 26
        for idx, a in enumerate(meeting.raised_actions, start=1):
            status = (a.status or "open").lower()
            # No-leading-zero date (Windows uses %#m/%#d, POSIX uses %-m/%-d)
            if a.due_date:
                try:
                    due_str = a.due_date.strftime("%#m/%#d/%Y")
                except ValueError:
                    due_str = a.due_date.strftime("%-m/%-d/%Y")
            else:
                due_str = ""
            table_data.append([
                str(idx),
                Paragraph(a.text or "", s["table_body"]),
                # Owner wrapped in a Paragraph so long owner strings
                # (multiple comma-separated full names) wrap inside the cell
                # instead of overflowing into the Due column.
                Paragraph(a.owner or "", s["table_body"]),
                due_str,
                StatusFormField(
                    field_name=f"action_status_{idx}",
                    value=status,
                    width_pt=STATUS_CELL_W,
                    height_pt=STATUS_CELL_H,
                    text_color=STATUS_TEXT_COLORS.get(status, white),
                ),
            ])

        act_table = Table(
            table_data,
            # Owner column widened to fit full names (initials would have
            # been ambiguous when two people share them).
            colWidths=[0.35 * inch, 3.0 * inch, 1.65 * inch, 0.85 * inch, 0.95 * inch],
            repeatRows=1,
        )
        ts = TableStyle([
            # Header
            ("BACKGROUND", (0, 0), (-1, 0), DARK_RED),
            ("TEXTCOLOR",  (0, 0), (-1, 0), white),
            ("FONTNAME",   (0, 0), (-1, 0), PRIMARY_BOLD),
            ("FONTSIZE",   (0, 0), (-1, 0), 11),
            # Body
            ("FONTNAME",   (0, 1), (-1, -1), PRIMARY_FONT),
            ("FONTSIZE",   (0, 1), (-1, -1), 10),
            ("TEXTCOLOR",  (0, 1), (-1, -1), NEAR_BLACK),
            # Header row stays centered; body cells top-aligned so wrapped
            # Action / Owner text reads naturally from the top of the cell.
            ("VALIGN",     (0, 0), (-1, 0),  "MIDDLE"),
            ("VALIGN",     (0, 1), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ("LINEBELOW",     (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ])
        # Per-row status pill background — color the Status cell
        for row_idx, a in enumerate(meeting.raised_actions, start=1):
            status = (a.status or "open").lower()
            bg = STATUS_COLORS.get(status, STATUS_COLORS["open"])
            ts.add("BACKGROUND", (4, row_idx), (4, row_idx), bg)
        act_table.setStyle(ts)
        story.append(act_table)

    # ---- Closing Remarks ----
    story.append(Paragraph("Closing Remarks", s["section"]))
    story.append(Paragraph(
        meeting.closing_remarks or
        "Thank you to everyone for attending this meeting. "
        "Your time is very much appreciated.",
        s["closing"],
    ))

    # Build with first-page template, then switch to later-page template for
    # continuation pages. ReportLab handles the transition automatically
    # because the second template id ("later") follows the first.
    from reportlab.platypus import NextPageTemplate
    # Insert a NextPageTemplate at the start so that after the first page
    # break we transition to the "later" template.
    story.insert(0, NextPageTemplate("later"))

    doc.build(story)
    data = buf.getvalue()
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(data)
    return data


def add_status_form_fields(pdf_bytes: bytes, action_count: int) -> bytes:
    """Phase 3 stub — see module docstring."""
    return pdf_bytes


# ============================================================
# Pre-meeting agenda PDF — matches the .docx layout 1:1
# ============================================================
# Fixed agenda topics that always appear in the Castillo agenda doc.
# The third tuple entry is the 30-minute time allocation; ``_scale_topics``
# below doubles every numeric "Nm" value when the meeting is 60-minute.
# "Open" passes through unchanged.
# Mirrors PREMEETING_AGENDA_TOPICS in docgen/document_builder.py.
PREMEETING_AGENDA_TOPICS_PDF = [
    ("Welcome and Introductions",          "Project Manager", "2m"),
    ("Review of Previous Action Items",    "Project Manager", "5m"),
    ("Schedule Update and Change Log",     "Project Manager", "5m"),
    ("Status of Active Deliverables",      "Project Manager", "5m"),
    ("Risks and Constraints",              "Project Manager", "5m"),
    ("Required Decisions",                 "Project Manager", "5m"),
    ("Open Discussion",                    "All",             "Open"),
    ("Summary of Actions and Next Steps",  "Project Manager", "5m"),
]


def _scale_topics(topics, duration_minutes: int):
    """Return ``topics`` with each numeric time-allocation column scaled to
    match the requested duration (30 = no change, 60 = doubled). "Open" and
    any non-numeric times pass through verbatim."""
    factor = max(1, int(duration_minutes or 30)) / 30
    if factor == 1:
        return topics
    out = []
    for topic, presenter, time_str in topics:
        new = time_str
        s = (time_str or "").strip()
        if s.lower().endswith("m") and s[:-1].isdigit():
            new = f"{int(s[:-1]) * int(factor)}m"
        out.append((topic, presenter, new))
    return out


def _agenda_subheading_style():
    """Bold black sub-heading used for discipline labels (Civil, Electrical…)
    inside Discussion Points and Previous Week Recap."""
    return ParagraphStyle(
        "agenda_subheading", fontName=PRIMARY_BOLD, fontSize=11,
        textColor=NEAR_BLACK, leading=15, spaceBefore=6, spaceAfter=2,
    )


def _walk_dp_to_paragraphs(dps, styles) -> list:
    """Render a list of (DB or Pydantic) discussion points as flowables — same
    nested-bullet look as meeting minutes, but driven by an explicit list
    instead of pulling from meeting.discussion_points."""
    if not dps:
        return []
    from reportlab.lib.styles import ParagraphStyle as _PS
    top_p_style = _PS(
        "agenda_dp_top", parent=styles["bullet"],
        leftIndent=18, bulletIndent=4, firstLineIndent=0,
        bulletFontSize=10, bulletFontName=PRIMARY_BOLD,
    )
    sub_p_style = _PS(
        "agenda_dp_sub", parent=styles["subbullet"],
        leftIndent=44, bulletIndent=30, firstLineIndent=0,
        bulletFontSize=10, bulletFontName=PRIMARY_BOLD,
    )
    out = []

    def _flatten(dp, level: int):
        label = getattr(dp, "label", "") or ""
        content = getattr(dp, "content", "") or ""
        label_md = f"<b>{label}:</b> " if label else ""
        text = label_md + content
        if level == 0:
            out.append(Paragraph(text, top_p_style, bulletText="•"))
        else:
            out.append(Paragraph(text, sub_p_style, bulletText="o"))
        kids = getattr(dp, "sub_points", None) or []
        for child in kids:
            _flatten(child, level + 1)

    for dp in dps:
        _flatten(dp, 0)
    return out


def _empty_para(style) -> Paragraph:
    """Sentinel placeholder paragraph for an empty bullet section."""
    return Paragraph("&nbsp;", style)


def _make_table_with_header(data, col_widths, repeat_header=True,
                            header_font_size: float = 10,
                            header_padding: float = 7) -> Table:
    """Common Castillo-styled table: red header row, light gray row dividers,
    Jost body. Caller supplies header as row 0 of ``data``. ``header_font_size``
    and ``header_padding`` let dense tables shrink so the header labels fit
    without truncation."""
    tbl = Table(
        data, colWidths=col_widths,
        repeatRows=1 if repeat_header else 0,
    )
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), DARK_RED),
        ("TEXTCOLOR",     (0, 0), (-1, 0), white),
        ("FONTNAME",      (0, 0), (-1, 0), PRIMARY_BOLD),
        ("FONTSIZE",      (0, 0), (-1, 0), header_font_size),
        ("FONTNAME",      (0, 1), (-1, -1), PRIMARY_FONT),
        ("FONTSIZE",      (0, 1), (-1, -1), 9.5),
        ("TEXTCOLOR",     (0, 1), (-1, -1), NEAR_BLACK),
        ("VALIGN",        (0, 0), (-1, 0),  "MIDDLE"),
        ("VALIGN",        (0, 1), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (0, 0),  header_padding),
        ("BOTTOMPADDING", (0, 0), (-1, 0),  header_padding),
        ("TOPPADDING",    (0, 1), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
    ]))
    return tbl


def generate_premeeting_agenda_pdf(
    meeting,
    prior_meeting=None,
    carry_actions=None,
    carry_deliverables=None,
    schedule=None,
    disciplines=None,
    dp_by_discipline=None,
    recap_by_discipline=None,
    risks=None,
    decisions=None,
    schedule_changes=None,
    meeting_duration_minutes: int = 30,
    schedule_version_override: Optional[str] = None,
    output_path: Optional[Path] = None,
) -> bytes:
    """Generate the Castillo pre-meeting coordination agenda as a PDF.

    Mirrors ``generate_premeeting_agenda_docx`` 1:1 — same sections, same
    order, same fixed Agenda table. Uses Jost via the same font registration
    as the meeting-minutes PDF, and the same PMO 360 header band and footer.

    Arguments match the .docx generator so the UI can call both with the
    same payload.
    """
    from docgen.document_builder import AGENDA_DISCUSSION_DISCIPLINES

    active_disciplines = list(disciplines) if disciplines else list(AGENDA_DISCUSSION_DISCIPLINES)
    dp_map = dp_by_discipline or {}

    # Fall back to scanning the prior meeting if the caller didn't supply
    # an explicit recap dict — matches the .docx generator behavior.
    if recap_by_discipline is None:
        recap_by_discipline = {d: [] for d in active_disciplines}
        if prior_meeting:
            for dp in sorted(prior_meeting.discussion_points,
                             key=lambda d: getattr(d, "order_index", 0)):
                if getattr(dp, "parent_id", None) is not None:
                    continue
                disc = (getattr(dp, "discipline", None) or "General").strip().capitalize() or "General"
                if disc not in recap_by_discipline:
                    disc = "General" if "General" in recap_by_discipline else active_disciplines[-1]
                recap_by_discipline[disc].append(dp)

    buf = BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=letter,
        leftMargin=48, rightMargin=48,
        topMargin=110, bottomMargin=54,
    )
    doc._meeting_date_str = meeting.meeting_date.strftime("%B %d, %Y")
    doc._page_title = "Pre-Meeting Coordination Agenda"

    width, height = letter
    frame_first = Frame(
        48, 54, width - 96, height - 110 - 54,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="first_frame",
    )
    frame_later = Frame(
        48, 54, width - 96, height - 50 - 54,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="later_frame",
    )
    doc.addPageTemplates([
        PageTemplate(id="first", frames=[frame_first], onPage=_draw_header_band),
        PageTemplate(id="later", frames=[frame_later], onPage=_draw_header_band),
    ])

    s = _styles()
    sub_h = _agenda_subheading_style()
    story = []

    # ---- Project block ----
    story.append(Paragraph(meeting.project.name, s["project"]))
    if meeting.project.client:
        story.append(Paragraph(
            f"<b>Client:</b>&nbsp;&nbsp;&nbsp;&nbsp;{meeting.project.client.name}",
            s["kv"],
        ))
    if meeting.project.scope:
        story.append(Paragraph(
            f"<b>Scope:</b>&nbsp;&nbsp;&nbsp;&nbsp;{meeting.project.scope}",
            s["kv"],
        ))

    # ---- Attendees (grouped by org) ----
    story.append(Paragraph("Attendees", s["section"]))
    by_org = {}
    for a in meeting.attendees:
        by_org.setdefault(a.organization or "Other", []).append(a)
    if by_org:
        for org, atts in by_org.items():
            names = ", ".join(a.full_name for a in atts)
            story.append(Paragraph(f"<b>{org}:</b> {names}", s["attendee_line"]))
    else:
        story.append(Paragraph(
            "(attendee list will be confirmed before the meeting)",
            s["attendee_line"],
        ))

    # ---- Agenda (fixed 8-row table) ----
    story.append(Paragraph("Agenda", s["section"]))
    story.append(Paragraph(
        f"<b>Meeting duration:</b> {int(meeting_duration_minutes or 30)} min",
        s["kv"],
    ))
    ag_data = [["#", "Topic", "Presenter", "Time Allocation"]]
    scaled_topics = _scale_topics(
        PREMEETING_AGENDA_TOPICS_PDF, meeting_duration_minutes
    )
    for idx, (topic, presenter, time) in enumerate(scaled_topics, start=1):
        ag_data.append([str(idx), topic, presenter, time])
    story.append(_make_table_with_header(
        ag_data,
        col_widths=[0.4 * inch, 4.0 * inch, 1.5 * inch, 1.2 * inch],
    ))

    # ---- Deliverable Timelines ----
    story.append(Paragraph("Deliverable Timelines", s["section"]))
    # Resolution order: explicit per-agenda override → latest uploaded
    # Schedule → portfolio.schedule_version → "—".
    _override = (schedule_version_override or "").strip()
    version_label = (
        _override if _override
        else (schedule.version if schedule
              else (meeting.project.schedule_version or "—"))
    )
    story.append(Paragraph(
        f"<b>Current Schedule Version: {version_label}</b>",
        s["kv"],
    ))
    deliv_data = [["#", "Project", "Task", "Start", "Delivery"]]
    deliv_list = list(carry_deliverables or [])
    if deliv_list:
        for idx, d in enumerate(deliv_list, start=1):
            seg = getattr(d, "project_segment", None) or meeting.project.name
            task = getattr(d, "task", "") or ""
            start = getattr(d, "start_status", None) or "In Progress"
            dd = getattr(d, "delivery_date", None)
            deliv_data.append([
                str(idx),
                Paragraph(seg, s["table_body"]),
                Paragraph(task, s["table_body"]),
                start,
                dd.strftime("%m/%d/%Y") if dd else "",
            ])
    else:
        # Four empty rows for fill-in (matches the template's empty state)
        for idx in range(1, 5):
            deliv_data.append([str(idx), "", "", "In Progress", ""])
    story.append(_make_table_with_header(
        deliv_data,
        col_widths=[0.4 * inch, 1.6 * inch, 3.2 * inch, 1.0 * inch, 1.0 * inch],
    ))

    # ---- Discussion Points ----
    story.append(Paragraph("Discussion Points", s["section"]))
    for discipline in active_disciplines:
        story.append(Paragraph(discipline, sub_h))
        items = dp_map.get(discipline) or []
        flows = _walk_dp_to_paragraphs(items, s)
        if flows:
            story.extend(flows)
        else:
            story.append(_empty_para(s["bullet"]))

    # ---- Previous Week Recap ----
    story.append(Paragraph("Previous Week Recap", s["section"]))
    for discipline in active_disciplines:
        story.append(Paragraph(discipline, sub_h))
        items = (recap_by_discipline or {}).get(discipline) or []
        flows = _walk_dp_to_paragraphs(items, s)
        if flows:
            story.extend(flows)
        else:
            story.append(_empty_para(s["bullet"]))

    # ---- Schedule Change Log ----
    story.append(Paragraph("Schedule Change Log", s["section"]))
    sc_data = [[
        "Project", "Task / Milestone", "Previous Date", "Updated Date",
        "Change Description", "Reason for Change", "Impact on Overall Schedule",
    ]]
    sc_rows = [r for r in (schedule_changes or [])
               if any((r.get(k) or "").strip()
                      for k in ("project", "task", "previous_date",
                                "updated_date", "change_description",
                                "reason_for_change", "impact"))]
    if sc_rows:
        for r in sc_rows:
            sc_data.append([
                Paragraph(r.get("project") or "", s["table_body"]),
                Paragraph(r.get("task") or "", s["table_body"]),
                r.get("previous_date") or "",
                r.get("updated_date") or "",
                Paragraph(r.get("change_description") or "", s["table_body"]),
                Paragraph(r.get("reason_for_change") or "", s["table_body"]),
                Paragraph(r.get("impact") or "", s["table_body"]),
            ])
    else:
        for _ in range(5):
            sc_data.append(["", "", "", "", "", "", ""])
    story.append(_make_table_with_header(
        sc_data,
        # 7 columns. The rightmost header — "Impact on Overall Schedule" —
        # is the longest, so it gets the widest column. "Project" holds short
        # sub-project names (Civil / Electrical) so it gives up width.
        col_widths=[0.65 * inch, 0.95 * inch, 0.75 * inch, 0.75 * inch,
                    1.20 * inch, 1.10 * inch, 1.75 * inch],
        header_font_size=7.5,
        header_padding=6,
    ))

    # ---- Risks and Constraints ----
    story.append(Paragraph("Risks and Constraints", s["section"]))
    risk_data = [["Description", "Impact", "Likelihood", "Mitigation Strategy", "Owner"]]
    risks_filled = [
        r for r in (risks or [])
        if any((r.get(k) or "").strip()
               for k in ("description", "impact", "likelihood", "mitigation", "owner"))
    ]
    if risks_filled:
        for r in risks_filled:
            risk_data.append([
                Paragraph(r.get("description") or "", s["table_body"]),
                Paragraph(r.get("impact") or "", s["table_body"]),
                r.get("likelihood") or "",
                Paragraph(r.get("mitigation") or "", s["table_body"]),
                Paragraph(r.get("owner") or "", s["table_body"]),
            ])
    else:
        for _ in range(5):
            risk_data.append(["", "", "", "", ""])
    story.append(_make_table_with_header(
        risk_data,
        col_widths=[1.85 * inch, 1.05 * inch, 0.75 * inch, 1.85 * inch, 1.65 * inch],
    ))

    # ---- Required Decisions ----
    story.append(Paragraph("Required Decisions", s["section"]))
    dec_data = [["Decision", "Description", "Impact if Not Decided",
                 "Required By", "Decision Owner"]]
    decisions_filled = []
    for d in (decisions or []):
        has_text = any((d.get(k) or "").strip()
                       for k in ("decision", "description", "impact_if_not", "owner"))
        has_date = bool(d.get("required_by"))
        if has_text or has_date:
            decisions_filled.append(d)
    if decisions_filled:
        for d in decisions_filled:
            rb = d.get("required_by")
            rb_str = ""
            if rb:
                try:
                    rb_str = rb.strftime("%m/%d/%Y")
                except AttributeError:
                    rb_str = str(rb)
            dec_data.append([
                Paragraph(d.get("decision") or "", s["table_body"]),
                Paragraph(d.get("description") or "", s["table_body"]),
                Paragraph(d.get("impact_if_not") or "", s["table_body"]),
                rb_str,
                Paragraph(d.get("owner") or "", s["table_body"]),
            ])
    else:
        for _ in range(5):
            dec_data.append(["", "", "", "", ""])
    story.append(_make_table_with_header(
        dec_data,
        col_widths=[1.10 * inch, 2.00 * inch, 1.50 * inch, 0.85 * inch, 1.70 * inch],
    ))

    # ---- Open Action Items (carry-forward, with status pills) ----
    if carry_actions:
        story.append(Paragraph("Open Action Items", s["section"]))
        ai_data = [["#", "Action", "Owner", "Due", "Status"]]
        STATUS_CELL_W = 0.95 * inch
        STATUS_CELL_H = 24
        for idx, a in enumerate(carry_actions, start=1):
            status = (getattr(a, "status", None) or "open").lower()
            due = getattr(a, "due_date", None)
            if due:
                try:
                    due_str = due.strftime("%#m/%#d/%Y")
                except ValueError:
                    due_str = due.strftime("%-m/%-d/%Y")
            else:
                due_str = ""
            ai_data.append([
                str(idx),
                Paragraph(getattr(a, "text", "") or "", s["table_body"]),
                Paragraph(getattr(a, "owner", "") or "", s["table_body"]),
                due_str,
                StatusFormField(
                    field_name=f"agenda_action_status_{idx}",
                    value=status,
                    width_pt=STATUS_CELL_W,
                    height_pt=STATUS_CELL_H,
                    text_color=STATUS_TEXT_COLORS.get(status, white),
                ),
            ])
        act_table = Table(
            ai_data,
            colWidths=[0.35 * inch, 3.0 * inch, 1.65 * inch, 0.85 * inch, 0.95 * inch],
            repeatRows=1,
        )
        ts = TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), DARK_RED),
            ("TEXTCOLOR",  (0, 0), (-1, 0), white),
            ("FONTNAME",   (0, 0), (-1, 0), PRIMARY_BOLD),
            ("FONTSIZE",   (0, 0), (-1, 0), 11),
            ("FONTNAME",   (0, 1), (-1, -1), PRIMARY_FONT),
            ("FONTSIZE",   (0, 1), (-1, -1), 10),
            ("TEXTCOLOR",  (0, 1), (-1, -1), NEAR_BLACK),
            ("VALIGN",     (0, 0), (-1, 0),  "MIDDLE"),
            ("VALIGN",     (0, 1), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ("LINEBELOW",     (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ])
        for row_idx, a in enumerate(carry_actions, start=1):
            status = (getattr(a, "status", None) or "open").lower()
            bg = STATUS_COLORS.get(status, STATUS_COLORS["open"])
            ts.add("BACKGROUND", (4, row_idx), (4, row_idx), bg)
        act_table.setStyle(ts)
        story.append(act_table)

    # ---- Closing Remarks ----
    story.append(Paragraph("Closing Remarks", s["section"]))
    story.append(Paragraph(
        "Thank you to everyone for attending this meeting. "
        "Your time is very much appreciated.",
        s["closing"],
    ))

    from reportlab.platypus import NextPageTemplate
    story.insert(0, NextPageTemplate("later"))

    doc.build(story)
    data = buf.getvalue()
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(data)
    return data

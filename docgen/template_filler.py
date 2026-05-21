"""
Generate meeting minutes by populating the canonical Castillo .docm template
in-place rather than rebuilding from scratch.

The template at templates/_External__Meeting_Minutes_-_Template_-_mm-dd-yy.docm
contains the exact design (Jost font, 14pt red title + date on one row, 18pt
project name with red underline, 14pt red section headings, banded tables, the
template's specific spacing). By cloning the template and rewriting only the
content, we keep every styling detail without trying to mirror it with
python-docx run properties.

The approach:
  1. Open the .docm zip and rewrite its [Content_Types].xml so it loads as a
     .docx (python-docx rejects macro-enabled content type). Drop vbaProject.
  2. Walk the body, locating each section by anchor text from the template's
     sample data (the Nov 7 / Heelstone meeting).
  3. Rewrite paragraph text in place (single-run collapse), or — for the list
     sections and tables — clone the template's first row/paragraph and use
     it as a row template for the new content.
  4. Save as .docx.
"""
from __future__ import annotations

import io
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Iterable

from docx import Document
from docx.oxml.ns import qn

from db.models import Meeting


TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "templates"
    / "_External__Meeting_Minutes_-_Template_-_mm-dd-yy.docm"
)

# Sample-data anchors in the shipped template — these are what we replace.
ANCHOR_DATE = "November 7, 2025"
ANCHOR_PROJECT = "Snapdragon and Two Blues"
ANCHOR_CLIENT = "Heelstone"
ANCHOR_SCOPE = "Electrical Design, Civil Design and Studies"

SECTION_HEADINGS = (
    "Attendees",
    "Deliverable Timelines",
    "Agenda",
    "Discussion Points",
    "Action Items",
    "Closing Remarks",
)


# ============================================================
# Zip-level conversion: .docm → in-memory .docx stream
# ============================================================
def _docm_as_docx_stream(path: Path) -> io.BytesIO:
    """Repack the .docm so python-docx accepts it. Drops the macro container
    and any .rels Relationship that points at it."""
    import re as _re
    out = io.BytesIO()
    macro_ct = b"application/vnd.ms-word.document.macroEnabled.main+xml"
    docx_ct = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    rels_vba_re = _re.compile(
        rb'<Relationship[^>]*vbaProject\.bin[^>]*/>'
    )
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            if name == "word/vbaProject.bin":
                continue  # drop macros
            data = src.read(name)
            if name == "[Content_Types].xml":
                data = data.replace(macro_ct, docx_ct)
                data = data.replace(
                    b'<Override PartName="/word/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/>',
                    b"",
                )
            elif name.endswith(".rels"):
                # Strip the vbaProject relationship if present
                data = rels_vba_re.sub(b"", data)
            dst.writestr(name, data)
    out.seek(0)
    return out


# ============================================================
# Paragraph helpers
# ============================================================
def _paragraph_text(p) -> str:
    """Visible text of a paragraph, joining all runs."""
    return "".join((r.text or "") for r in p.runs)


def _is_heading(p, heading_text: str) -> bool:
    return _paragraph_text(p).strip() == heading_text


def _find_paragraph_by_text(doc, fragment: str):
    for p in doc.paragraphs:
        if fragment in _paragraph_text(p):
            return p
    return None


def _replace_substring_in_runs(p, old: str, new: str) -> bool:
    """Replace `old` with `new` in paragraph p. Collapses into the first run,
    preserving its formatting. The remaining runs' text is cleared but their
    formatting elements stay — that's fine because empty runs render nothing.
    """
    joined = _paragraph_text(p)
    if old not in joined:
        return False
    new_joined = joined.replace(old, new)
    if not p.runs:
        p.add_run(new_joined)
        return True
    p.runs[0].text = new_joined
    for r in p.runs[1:]:
        r.text = ""
    return True


# ============================================================
# Section walking — given a heading, iterate the body elements that follow
# until we hit the next heading.
# ============================================================
def _body_children(doc):
    return list(doc.element.body.iterchildren())


def _section_range(doc, heading_text: str, stop_headings: Iterable[str]):
    """Return (heading_para_element, [next_elements_until_stop])."""
    children = _body_children(doc)
    head_idx = -1
    for i, child in enumerate(children):
        tag = child.tag.split("}")[-1]
        if tag != "p":
            continue
        # Match by visible text against the python-docx paragraph wrapper
        for p in doc.paragraphs:
            if p._element is child and _paragraph_text(p).strip() == heading_text:
                head_idx = i
                break
        if head_idx != -1:
            break
    if head_idx == -1:
        return None, []
    stop_set = set(stop_headings)
    section = []
    for child in children[head_idx + 1:]:
        tag = child.tag.split("}")[-1]
        if tag == "p":
            # If this paragraph is itself a known heading, stop
            for p in doc.paragraphs:
                if p._element is child and _paragraph_text(p).strip() in stop_set:
                    return children[head_idx], section
        section.append(child)
    return children[head_idx], section


# ============================================================
# Attendees rewrite — kill existing org lines, clone one as template,
# insert new lines after the heading.
# ============================================================
def _rewrite_attendees(doc, by_org: dict[str, list[str]]):
    head, section = _section_range(doc, "Attendees",
                                   stop_headings=("Deliverable Timelines", "Agenda"))
    if head is None:
        return
    # Existing org-line paragraphs (skip empty / non-paragraph elements)
    existing_paras = []
    for el in section:
        if el.tag.split("}")[-1] == "p":
            for p in doc.paragraphs:
                if p._element is el:
                    if _paragraph_text(p).strip():
                        existing_paras.append(el)
                    break
    if not existing_paras:
        return
    # Keep the first one as a clone template
    template_el = deepcopy(existing_paras[0])
    parent = head.getparent()
    # Remove existing lines (we'll re-insert)
    for el in existing_paras:
        parent.remove(el)
    # Insert new lines right after the heading
    anchor = head
    for org, names in by_org.items():
        new_p = deepcopy(template_el)
        # Replace the whole paragraph text content with "Org: names"
        new_text = f"{org}: {names}"
        # Walk runs of the new_p and put text into the first run, clear rest
        runs = new_p.findall(qn("w:r"))
        if runs:
            # Get the first w:t in first run, set text
            ts = runs[0].findall(qn("w:t"))
            if ts:
                ts[0].text = new_text
                # Preserve spaces (xml:space="preserve") so trailing/leading spaces render
                ts[0].set(qn("xml:space"), "preserve")
                # Clear additional w:t in first run
                for t in ts[1:]:
                    t.text = ""
            else:
                # No text element in first run — add one
                t = deepcopy(qn("w:t"))  # not directly usable; build via lxml
                from lxml import etree
                t = etree.SubElement(runs[0], qn("w:t"))
                t.text = new_text
            # Clear text in other runs of the paragraph
            for r in runs[1:]:
                for t in r.findall(qn("w:t")):
                    t.text = ""
        anchor.addnext(new_p)
        anchor = new_p


# ============================================================
# Listing rewrite (Agenda, Discussion Points)
# ============================================================
def _rewrite_listing(doc, heading_text: str, stop_headings: Iterable[str],
                     new_lines):
    """Rewrite a bulleted listing section.

    `new_lines` can be:
      - a list[str] (flat bullets), or
      - a list[tuple[int, str]] where the int is an indent level
        (0 = top-level, 1 = sub-bullet, 2 = sub-sub).
    """
    head, section = _section_range(doc, heading_text, stop_headings)
    if head is None:
        return
    existing_paras = []
    for el in section:
        if el.tag.split("}")[-1] == "p":
            for p in doc.paragraphs:
                if p._element is el and _paragraph_text(p).strip():
                    existing_paras.append(el)
                    break
    if not existing_paras:
        return
    template_el = deepcopy(existing_paras[0])
    parent = head.getparent()
    for el in existing_paras:
        parent.remove(el)

    from lxml import etree
    anchor = head
    for entry in new_lines:
        if isinstance(entry, tuple):
            indent, line = entry
        else:
            indent, line = 0, entry
        new_p = deepcopy(template_el)

        # Apply paragraph indent (~0.3" per nesting level) so sub-bullets sit
        # below their parent without losing the template's run formatting.
        if indent > 0:
            pPr = new_p.find(qn("w:pPr"))
            if pPr is None:
                pPr = etree.SubElement(new_p, qn("w:pPr"))
                new_p.insert(0, pPr)
            ind = pPr.find(qn("w:ind"))
            if ind is None:
                ind = etree.SubElement(pPr, qn("w:ind"))
            # 360 twips per indent step is roughly 0.25"
            ind.set(qn("w:left"), str(360 * (indent + 1)))

        runs = new_p.findall(qn("w:r"))
        if runs:
            ts = runs[0].findall(qn("w:t"))
            if ts:
                ts[0].text = line
                ts[0].set(qn("xml:space"), "preserve")
                for t in ts[1:]:
                    t.text = ""
            else:
                t = etree.SubElement(runs[0], qn("w:t"))
                t.text = line
                t.set(qn("xml:space"), "preserve")
            for r in runs[1:]:
                for t in r.findall(qn("w:t")):
                    t.text = ""
        anchor.addnext(new_p)
        anchor = new_p


# ============================================================
# Table rewrite — keep header row, replace data rows
# ============================================================
def _table_after_heading(doc, heading_text: str):
    """Find the first w:tbl element after the given heading paragraph."""
    head, section = _section_range(doc, heading_text,
                                   stop_headings=SECTION_HEADINGS)
    if head is None:
        return None
    for el in section:
        if el.tag.split("}")[-1] == "tbl":
            for t in doc.tables:
                if t._element is el:
                    return t
    return None


def _set_cell_text(cell, text: str):
    """Replace cell text while preserving run-level formatting from the first
    run of the first paragraph. Removes extra paragraphs in the cell."""
    if not cell.paragraphs:
        cell.text = str(text)
        return
    p = cell.paragraphs[0]
    runs = p.runs
    if runs:
        runs[0].text = str(text)
        for r in runs[1:]:
            r.text = ""
    else:
        p.add_run(str(text))
    # Drop any subsequent paragraphs in this cell
    for extra in p._element.getparent().findall(qn("w:p"))[1:]:
        extra.getparent().remove(extra)


def _rewrite_table(doc, heading_text: str, rows_data: list[list[str]]):
    tbl = _table_after_heading(doc, heading_text)
    if tbl is None or len(tbl.rows) < 2:
        return
    # Use the first data row as the clone template for new rows
    template_row_el = deepcopy(tbl.rows[1]._tr)
    # Remove every existing data row (keep header at index 0)
    for row in list(tbl.rows)[1:]:
        tbl._tbl.remove(row._tr)
    # Insert one cloned row per data tuple, set cell texts
    for row_values in rows_data:
        new_row_el = deepcopy(template_row_el)
        tbl._tbl.append(new_row_el)
        new_row = tbl.rows[-1]
        for i, cell in enumerate(new_row.cells):
            if i < len(row_values):
                _set_cell_text(cell, row_values[i])
            else:
                _set_cell_text(cell, "")


# ============================================================
# Deliverable rows — manual MeetingDeliverable rows, or fallback to the
# upcoming leaf tasks from the project's latest uploaded Schedule.
# ============================================================
def _gather_deliverable_rows(meeting: Meeting) -> list[list[str]]:
    """Build the rows that go into the Deliverable Timelines table from the
    meeting's manually-attached MeetingDeliverable rows. The PM picks these
    on the Review page (optionally choosing items from the project's
    Schedule); we don't auto-pull anything here."""
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
def generate_meeting_minutes_from_template(meeting: Meeting) -> bytes:
    """Open the canonical .docm template, populate with this meeting's data,
    return clean .docx bytes."""
    stream = _docm_as_docx_stream(TEMPLATE_PATH)
    doc = Document(stream)

    # --- Date in the title row ---
    new_date = meeting.meeting_date.strftime("%B %d, %Y")
    title_p = _find_paragraph_by_text(doc, ANCHOR_DATE)
    if title_p:
        _replace_substring_in_runs(title_p, ANCHOR_DATE, new_date)

    # --- Project name ---
    proj_name = meeting.project.name
    for p in doc.paragraphs:
        if _paragraph_text(p).strip() == ANCHOR_PROJECT:
            _replace_substring_in_runs(p, ANCHOR_PROJECT, proj_name)
            break

    # --- Client value ---
    if meeting.project.client:
        for p in doc.paragraphs:
            t = _paragraph_text(p)
            if t.strip().startswith("Client:") and ANCHOR_CLIENT in t:
                _replace_substring_in_runs(p, ANCHOR_CLIENT, meeting.project.client.name)
                break

    # --- Scope value ---
    if meeting.project.scope:
        for p in doc.paragraphs:
            t = _paragraph_text(p)
            if t.strip().startswith("Scope:") and ANCHOR_SCOPE in t:
                _replace_substring_in_runs(p, ANCHOR_SCOPE, meeting.project.scope)
                break

    # --- Attendees grouped by org ---
    by_org: dict[str, list] = {}
    for a in meeting.attendees:
        by_org.setdefault(a.organization or "Other", []).append(a)
    org_to_names = {
        org: ", ".join(f"{a.full_name} ({a.initials})" for a in attendees)
        for org, attendees in by_org.items()
    }
    # Always call the rewriters so the template's sample data is replaced
    # (or cleared, if we don't have any new data to substitute in).
    _rewrite_attendees(doc, org_to_names)

    # --- Deliverable Timelines table ---
    deliv_rows = _gather_deliverable_rows(meeting)
    _rewrite_table(doc, "Deliverable Timelines", deliv_rows)

    # --- Agenda ---
    agenda_lines = [a.text for a in sorted(meeting.agenda_items, key=lambda x: x.order_index)]
    _rewrite_listing(doc, "Agenda", ("Discussion Points",), agenda_lines)

    # --- Discussion points (label: content) — flatten tree to (indent, text) ---
    def _format_dp(dp) -> str:
        return f"{dp.label}: {dp.content}" if dp.label else (dp.content or "")

    disc_entries: list = []

    def _walk(dp, level: int):
        disc_entries.append((level, _format_dp(dp)))
        for sub in sorted(getattr(dp, "sub_points", []) or [], key=lambda s: s.order_index):
            _walk(sub, level + 1)

    # Only iterate top-level points (parent_id IS NULL); sub_points come via _walk
    top_level = [dp for dp in meeting.discussion_points if getattr(dp, "parent_id", None) is None]
    for dp in sorted(top_level, key=lambda d: d.order_index):
        _walk(dp, 0)
    _rewrite_listing(doc, "Discussion Points", ("Action Items",), disc_entries)

    # --- Action Items table ---
    act_rows = []
    for idx, a in enumerate(meeting.raised_actions, start=1):
        act_rows.append([
            str(idx),
            a.text,
            a.owner or "",
            a.due_date.strftime("%m/%d/%Y") if a.due_date else "",
            a.status.capitalize(),
        ])
    _rewrite_table(doc, "Action Items", act_rows)

    # --- Closing Remarks ---
    # Keep template's "Thank you to everyone..." text as-is, unless the meeting
    # has explicit closing remarks set.
    if meeting.closing_remarks:
        for p in doc.paragraphs:
            t = _paragraph_text(p).strip()
            if t.startswith("Thank you to everyone"):
                _replace_substring_in_runs(p, t, meeting.closing_remarks)
                break

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()

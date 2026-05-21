"""
Orchestration layer between UI and the data/AI/storage layers.

Functions here are the "verbs" of the app: parse_and_save_meeting,
finalize_meeting, generate_agenda_for_next_meeting, etc.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from db.models import (
    Meeting, MeetingAttendee, AgendaItem, DiscussionPoint, ActionItem,
    Project, ProjectAttendee, Deliverable, MeetingDeliverable,
)
from db.repository import (
    upsert_project_attendee, latest_meeting, open_actions, all_actions,
)
from llm.providers import get_provider, ParsedMeeting
from docgen import (
    generate_meeting_minutes_docx,
    generate_meeting_minutes_pdf,
    generate_action_items_xlsx,
    add_status_form_fields,
)
from storage import get_storage


# ============================================================
# AI parsing → save draft meeting
# ============================================================
def parse_notes_with_ai(
    minutes_text: str,
    agenda_text: str,
    actions_text: str,
    project: Project,
    attendees_roster: Optional[list[dict]] = None,
) -> ParsedMeeting:
    """Call the LLM with three separate sections (minutes, agenda, actions).
    Returns the structured ParsedMeeting (not yet persisted).

    `attendees_roster` (optional) is the list of people already selected on
    the Capture page; passing it lets the model normalize name references
    and emit action-item owners as initials.
    """
    provider = get_provider()
    context = f"{project.client.name if project.client else ''} / {project.name}"
    return provider.parse_meeting_notes(
        minutes_text=minutes_text,
        agenda_text=agenda_text,
        actions_text=actions_text,
        project_context=context,
        attendees_roster=attendees_roster,
    )


def _write_meeting_children(session: Session, meeting: Meeting,
                            project: Project, parsed: ParsedMeeting,
                            deliverables: Optional[list[dict]] = None) -> None:
    """Insert the attendees / agenda / discussion-points / action-items rows
    that belong to `meeting`. Assumes the meeting row already exists and any
    existing children have been cleared.

    `deliverables` (optional) is a list of dicts the Review page builds:
      {project_segment, task, start_status, delivery_date (date | None)}.
    Each is materialized as a fresh Deliverable + MeetingDeliverable so the
    meeting carries its own copy of the chosen rows.
    """
    for a in parsed.attendees:
        upsert_project_attendee(
            session, project_id=project.id,
            full_name=a.full_name or a.initials,
            initials=a.initials, organization=a.organization,
            first_seen_meeting_id=meeting.id,
        )
        session.add(MeetingAttendee(
            meeting_id=meeting.id,
            full_name=a.full_name or a.initials,
            initials=a.initials, organization=a.organization,
        ))

    for idx, d in enumerate(deliverables or []):
        task = (d.get("task") or "").strip()
        if not task:
            continue  # drop blank rows
        deliv = Deliverable(
            project_id=project.id,
            project_segment=(d.get("project_segment") or "").strip() or None,
            task=task,
            start_status=(d.get("start_status") or "In Progress"),
            delivery_date=d.get("delivery_date"),
            source="manual",
            schedule_version_added=project.schedule_version,
        )
        session.add(deliv)
        session.flush()
        session.add(MeetingDeliverable(
            meeting_id=meeting.id,
            deliverable_id=deliv.id,
            order_index=idx,
            carried_from_prior=False,
        ))

    for idx, item in enumerate(parsed.agenda_items):
        session.add(AgendaItem(
            meeting_id=meeting.id, order_index=idx,
            text=item.text, discipline=item.discipline,
        ))

    def _persist_dp(dp, parent_id, idx):
        row = DiscussionPoint(
            meeting_id=meeting.id, parent_id=parent_id,
            order_index=idx, label=dp.label, content=dp.content,
            discipline=dp.discipline, ai_extracted=True,
        )
        session.add(row)
        session.flush()
        for sub_idx, sub in enumerate(dp.sub_points or []):
            _persist_dp(sub, row.id, sub_idx)
    for idx, dp in enumerate(parsed.discussion_points):
        _persist_dp(dp, None, idx)

    for a in parsed.action_items:
        due = None
        if a.due_date:
            try:
                due = date.fromisoformat(a.due_date)
            except ValueError:
                due = None
        session.add(ActionItem(
            project_id=project.id,
            originating_meeting_id=meeting.id,
            text=a.text, owner=a.owner, due_date=due, status=a.status,
        ))


def save_parsed_meeting(
    session: Session,
    project: Project,
    meeting_date: date,
    parsed: ParsedMeeting,
    raw_notes: str = "",
    title: str = "",
    deliverables: Optional[list[dict]] = None,
) -> Meeting:
    """Persist a NEW parsed meeting. Adds attendees to the project roster as
    a side effect. Returns the newly-created Meeting in 'draft' stage."""
    meeting = Meeting(
        project_id=project.id,
        meeting_date=meeting_date,
        title=title or f"Weekly coordination — {meeting_date.isoformat()}",
        raw_notes=raw_notes,
        stage="draft",
        schedule_version_at_meeting=project.schedule_version,
    )
    session.add(meeting)
    session.flush()
    _write_meeting_children(session, meeting, project, parsed,
                            deliverables=deliverables)
    session.flush()
    return meeting


def update_parsed_meeting(
    session: Session,
    meeting: Meeting,
    parsed: ParsedMeeting,
    meeting_date: date,
    raw_notes: str = "",
    title: str = "",
    deliverables: Optional[list[dict]] = None,
) -> Meeting:
    """Update an EXISTING meeting in place. Replaces all child rows (attendees,
    agenda, discussion points, action items raised at this meeting, picked
    deliverables) so a re-save after editing fully reflects the new state."""
    meeting.meeting_date = meeting_date
    if title:
        meeting.title = title
    meeting.raw_notes = raw_notes
    meeting.updated_at = datetime.utcnow()

    # Wipe child rows. Cascade is set on the relationships, but explicit delete
    # via session is safer here because action_items have cross-meeting FKs.
    for child in list(meeting.attendees):
        session.delete(child)
    for child in list(meeting.agenda_items):
        session.delete(child)
    for child in list(meeting.discussion_points):
        session.delete(child)
    for child in list(meeting.raised_actions):
        # Action items closed at OTHER meetings won't show up in raised_actions
        # — those are raised here, so deletion is safe.
        session.delete(child)
    # Drop picked deliverables (MeetingDeliverable rows) and their underlying
    # Deliverable rows — we re-create from the latest `deliverables` arg below.
    for md in list(meeting.meeting_deliverables):
        deliv = md.deliverable
        session.delete(md)
        if deliv is not None:
            session.delete(deliv)
    session.flush()

    _write_meeting_children(session, meeting, meeting.project, parsed,
                            deliverables=deliverables)
    session.flush()
    return meeting


# ============================================================
# Finalize → generate documents → upload to storage
# ============================================================
def safe_filename_slug(s: str) -> str:
    """Filesystem-safe filename slug — spaces → underscores, slashes/colons/
    quotes → hyphens, drop other non-alphanumeric punctuation."""
    import re
    if not s:
        return "project"
    s = s.strip().replace(" ", "_")
    s = re.sub(r"[/\\:*?\"<>|]", "-", s)
    s = re.sub(r"[^\w\-.]", "", s)
    return s or "project"


def meeting_filename(meeting: Meeting, kind: str, ext: str,
                     draft: bool = False) -> str:
    """Build a consistent output filename leading with the project name +
    kind + date.
    Example: ``Snapdragon_and_Two_Blues_Meeting_Minutes_2026-04-22.pdf``."""
    project_name = meeting.project.name if meeting.project else ""
    date_str = meeting.meeting_date.strftime("%Y-%m-%d")
    parts = [
        safe_filename_slug(project_name) if project_name else None,
        ("Draft_" if draft else "") + kind,
        date_str,
    ]
    base = "_".join(p for p in parts if p)
    return f"{base}.{ext}"


def finalize_meeting(session: Session, meeting: Meeting) -> dict:
    """
    Generate all client-ready documents for this meeting and save to storage.
    Returns dict of {kind: storage_path}.
    """
    storage = get_storage()
    project = meeting.project

    # Folder for this project's outputs uses the project name; filenames
    # include the client name for at-a-glance identification (matches the
    # Castillo deliverable convention: "Meeting Minutes - <Client> - <date>").
    proj_slug = safe_filename_slug(project.name or "project")

    # PDF (Phase 3 will add form fields)
    pdf_bytes = generate_meeting_minutes_pdf(meeting)
    pdf_bytes = add_status_form_fields(pdf_bytes, len(meeting.raised_actions))
    pdf_path = storage.save(
        f"{proj_slug}/{meeting_filename(meeting, 'Meeting_Minutes', 'pdf')}",
        pdf_bytes,
    )

    # DOCX
    docx_bytes = generate_meeting_minutes_docx(meeting)
    docx_path = storage.save(
        f"{proj_slug}/{meeting_filename(meeting, 'Meeting_Minutes', 'docx')}",
        docx_bytes,
    )

    # XLSX action log
    actions = all_actions(session, project.id)
    xlsx_bytes = generate_action_items_xlsx(project, actions)
    xlsx_path = storage.save(
        f"{proj_slug}/Action_Items_Log_{safe_filename_slug(project.client.name) if project.client else 'project'}.xlsx",
        xlsx_bytes,
    )

    # Mark meeting as final
    meeting.stage = "final"
    meeting.updated_at = datetime.utcnow()

    return {
        "pdf": pdf_path,
        "docx": docx_path,
        "xlsx": xlsx_path,
    }


# ============================================================
# Pre-meeting agenda generation (Phase 4)
# ============================================================
class _DraftAttendeeView:
    """Detached attendee row passed to the agenda docgen — never persisted."""
    __slots__ = ("full_name", "initials", "organization")

    def __init__(self, full_name, initials, organization=None):
        self.full_name = full_name
        self.initials = initials
        self.organization = organization


class _DraftMeetingView:
    """Detached, Meeting-shaped view passed to the agenda docgen — never persisted."""
    __slots__ = ("project", "meeting_date", "attendees")

    def __init__(self, project, meeting_date, attendees):
        self.project = project
        self.meeting_date = meeting_date
        self.attendees = attendees


def generate_next_agenda(
    session: Session,
    project: Project,
    upcoming_date: date,
    source_meeting_id: Optional[int] = None,
) -> _DraftMeetingView:
    """
    PHASE 4 — build an IN-MEMORY draft of the upcoming meeting (NOT persisted)
    pre-filled with attendees carried from the chosen source meeting (default:
    latest). Returns a detached view shaped for the agenda docgen — fields
    accessed there are ``meeting_date``, ``project``, and ``attendees`` (each
    with ``full_name`` / ``initials`` / ``organization``).

    No Meeting row is created here. The real row is inserted later by the
    Capture / Review flow once the meeting actually happens — this keeps PMs
    free to regenerate the agenda repeatedly without leaving phantom 'draft'
    rows on the project History.
    """
    source = None
    if source_meeting_id:
        source = session.get(Meeting, source_meeting_id)
    if source is None:
        source = latest_meeting(session, project.id)

    attendees = []
    if source:
        for a in source.attendees:
            attendees.append(_DraftAttendeeView(
                full_name=a.full_name,
                initials=a.initials,
                organization=a.organization,
            ))

    return _DraftMeetingView(
        project=project,
        meeting_date=upcoming_date,
        attendees=attendees,
    )

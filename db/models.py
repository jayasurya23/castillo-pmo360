"""
SQLAlchemy ORM models for the Castillo meeting management app.

Schema overview:
  Client → Project → Meeting → (Attendee, Discussion, Deliverable, Action, Agenda)

Action items have dual foreign keys to Meeting (originating + closed_in) so the
rolling action log can track when items were raised vs when they were closed.
"""
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Text, Date, DateTime, ForeignKey, Boolean, JSON
)
from sqlalchemy.orm import declarative_base, relationship, backref

Base = declarative_base()


class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    email_domain = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)

    projects = relationship("Project", back_populates="client", cascade="all, delete-orphan")


class GlobalAttendee(Base):
    """Company-wide default roster — shown on every project's Capture page in
    addition to that project's local roster. Pre-seeded with the Castillo
    Engineering team on first run."""
    __tablename__ = "global_attendees"
    id = Column(Integer, primary_key=True)
    full_name = Column(String(200), nullable=False, unique=True)
    initials = Column(String(10), nullable=False)
    organization = Column(String(150))
    email = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    name = Column(String(300), nullable=False)
    scope = Column(Text)
    schedule_version = Column(String(20), default="V1")
    # Curated list of sub-project names within this portfolio (e.g. for the
    # "Snapdragon and Two Blues" portfolio: ["Snapdragon", "Two Blues"]).
    # Used to drive the Project selectbox on the Notes tab.
    sub_projects_json = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="projects")
    meetings = relationship("Meeting", back_populates="project", cascade="all, delete-orphan")
    project_attendees = relationship("ProjectAttendee", back_populates="project", cascade="all, delete-orphan")
    deliverables = relationship("Deliverable", back_populates="project", cascade="all, delete-orphan")


class ProjectAttendee(Base):
    """
    Persistent attendee roster — saves people seen on prior meetings for a
    project so the PM can one-click add them on future meetings.
    """
    __tablename__ = "project_attendees"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    full_name = Column(String(200), nullable=False)
    initials = Column(String(10), nullable=False)
    organization = Column(String(150))
    email = Column(String(200))
    first_seen_meeting_id = Column(Integer, ForeignKey("meetings.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="project_attendees")


class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    meeting_date = Column(Date, nullable=False)
    title = Column(String(300))
    raw_notes = Column(Text)            # Original pasted/uploaded notes
    closing_remarks = Column(Text)
    stage = Column(String(20), default="draft")    # draft / final / sent
    sent_at = Column(DateTime)
    schedule_version_at_meeting = Column(String(20))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="meetings")
    attendees = relationship("MeetingAttendee", back_populates="meeting", cascade="all, delete-orphan")
    agenda_items = relationship("AgendaItem", back_populates="meeting", cascade="all, delete-orphan")
    discussion_points = relationship("DiscussionPoint", back_populates="meeting", cascade="all, delete-orphan")
    # Action items raised at this meeting:
    raised_actions = relationship(
        "ActionItem",
        foreign_keys="ActionItem.originating_meeting_id",
        back_populates="originating_meeting",
        cascade="all, delete-orphan"
    )
    meeting_deliverables = relationship("MeetingDeliverable", back_populates="meeting", cascade="all, delete-orphan")


class MeetingAttendee(Base):
    """Who actually attended this specific meeting (subset of project roster)."""
    __tablename__ = "meeting_attendees"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False)
    project_attendee_id = Column(Integer, ForeignKey("project_attendees.id"))
    # Denormalized for convenience and to handle ad-hoc attendees not yet in roster:
    full_name = Column(String(200), nullable=False)
    initials = Column(String(10), nullable=False)
    organization = Column(String(150))

    meeting = relationship("Meeting", back_populates="attendees")


class AgendaItem(Base):
    __tablename__ = "agenda_items"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False)
    order_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    discipline = Column(String(50))          # Electrical / Civil / General

    meeting = relationship("Meeting", back_populates="agenda_items")


class DiscussionPoint(Base):
    __tablename__ = "discussion_points"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False)
    # Self-referential parent for sub-points. NULL = top-level point.
    parent_id = Column(Integer, ForeignKey("discussion_points.id"), nullable=True)
    order_index = Column(Integer, nullable=False)
    label = Column(String(200))              # e.g. "IE methodology change"
    content = Column(Text, nullable=False)
    discipline = Column(String(50))          # Electrical / Civil / General
    ai_extracted = Column(Boolean, default=False)

    meeting = relationship("Meeting", back_populates="discussion_points")
    sub_points = relationship(
        "DiscussionPoint",
        cascade="all, delete-orphan",
        backref=backref("parent", remote_side="DiscussionPoint.id"),
        order_by="DiscussionPoint.order_index",
    )


class Deliverable(Base):
    """
    Project-level deliverable definition. A meeting tracks a subset of these
    via MeetingDeliverable for that week's status reporting.
    """
    __tablename__ = "deliverables"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    project_segment = Column(String(100))    # e.g. "Snapdragon" within a parent project
    task = Column(String(300), nullable=False)
    start_status = Column(String(50), default="In Progress")
    delivery_date = Column(Date)
    source = Column(String(30), default="manual")  # manual / schedule / ai
    schedule_version_added = Column(String(20))
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="deliverables")


class MeetingDeliverable(Base):
    """Which deliverables a specific meeting reports on (the tracked subset)."""
    __tablename__ = "meeting_deliverables"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False)
    deliverable_id = Column(Integer, ForeignKey("deliverables.id"), nullable=False)
    order_index = Column(Integer, default=0)
    carried_from_prior = Column(Boolean, default=False)
    risk_flag = Column(Boolean, default=False)

    meeting = relationship("Meeting", back_populates="meeting_deliverables")
    deliverable = relationship("Deliverable")


class ActionItem(Base):
    """
    Rolling action item with dual FK to Meeting:
      - originating_meeting_id: meeting where it was raised
      - closed_in_meeting_id:  meeting where it was marked completed/cancelled
    This lets us reconstruct status history across the project timeline.
    """
    __tablename__ = "action_items"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    originating_meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False)
    closed_in_meeting_id = Column(Integer, ForeignKey("meetings.id"))
    text = Column(Text, nullable=False)
    owner = Column(String(100))              # may be initials or comma list e.g. "CK, KC"
    due_date = Column(Date)
    status = Column(String(20), default="open")  # open / pending / completed / cancelled
    last_status_change = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    originating_meeting = relationship(
        "Meeting",
        foreign_keys=[originating_meeting_id],
        back_populates="raised_actions"
    )
    closed_in_meeting = relationship("Meeting", foreign_keys=[closed_in_meeting_id])


class Schedule(Base):
    """
    A parsed project schedule, uploaded from a proposal PDF or a duration
    Excel workbook. One Project can have multiple Schedule versions over time.
    """
    __tablename__ = "schedules"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    version = Column(String(20), nullable=False)         # e.g. "V1", "V2"
    source_filename = Column(String(300))
    source_format = Column(String(10))                   # "pdf" or "xlsx"
    project_start_date = Column(Date)
    total_duration_days = Column(Integer)
    total_price = Column(Integer)                        # whole dollars
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    project = relationship(
        "Project",
        backref=backref("schedules", cascade="all, delete-orphan"),
    )
    items = relationship(
        "ScheduleItem", back_populates="schedule",
        cascade="all, delete-orphan", order_by="ScheduleItem.order_index"
    )


class ScheduleItem(Base):
    """A single row in a project schedule (discipline header, phase header, or leaf task)."""
    __tablename__ = "schedule_items"
    id = Column(Integer, primary_key=True)
    schedule_id = Column(Integer, ForeignKey("schedules.id"), nullable=False)
    order_index = Column(Integer, default=0)
    indent_level = Column(Integer, default=0)            # 0=discipline, 1=phase, 2=task
    discipline = Column(String(100))                     # "Civil Engineering" etc
    phase = Column(String(100))                          # "30% Design" etc
    task = Column(String(300), nullable=False)
    duration_days = Column(Integer)
    start_date = Column(Date)
    finish_date = Column(Date)
    price = Column(Integer)                              # whole dollars; nullable
    is_milestone = Column(Boolean, default=False)

    schedule = relationship("Schedule", back_populates="items")


class Agenda(Base):
    """
    A saved pre-meeting coordination agenda — the editor state from the
    Next Agenda page, persisted so PMs can come back, edit, and regenerate.

    Distinct from `Meeting`: a Meeting represents something that happened
    (with minutes / discussion / raised actions), while an Agenda is the
    *plan* for an upcoming meeting. Once the meeting actually happens and
    minutes are captured, the agenda's job is done — but it stays on the
    project so PMs can audit what they planned vs. what occurred.

    All editor state lives in JSON columns. We don't normalize discussion
    points / risks / decisions into child tables because nothing else queries
    them individually — this is ephemeral planning data.
    """
    __tablename__ = "agendas"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    upcoming_date = Column(Date, nullable=False)
    # The source meeting that seeded recap / attendees / carry-forward actions.
    source_meeting_id = Column(Integer, ForeignKey("meetings.id"))
    title = Column(String(300))  # optional PM-supplied label
    # Total meeting duration. Drives the time-allocation column in the fixed
    # 8-row Agenda table on the .docx / PDF (30-min default; 60-min doubles
    # each numeric value, "Open" stays "Open").
    meeting_duration_minutes = Column(Integer, default=30)
    # Optional per-agenda override of the schedule version shown on the
    # "Current Schedule Version: …" line in the Deliverable Timelines block.
    # When NULL/empty, the generator falls back to the latest uploaded
    # Schedule, then to portfolio.schedule_version.
    schedule_version_override = Column(String(20))

    # Editor state, all JSON. See render_next_agenda for the exact shapes.
    disciplines_json = Column(JSON)        # list[str]
    dp_text_json = Column(JSON)            # dict[discipline -> textarea text]
    recap_text_json = Column(JSON)         # dict[discipline -> textarea text]
    attendees_json = Column(JSON)          # list[{full_name, initials, organization}]
    open_actions_json = Column(JSON)       # list[{text, owner, due_date, status}]
    risks_json = Column(JSON)              # list[{description, impact, likelihood, mitigation, owner}]
    decisions_json = Column(JSON)          # list[{decision, description, impact_if_not, required_by_iso, owner}]
    schedule_changes_json = Column(JSON)   # list[{project, task, previous_date, updated_date, change_description, reason_for_change, impact}]

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship(
        "Project",
        backref=backref("agendas", cascade="all, delete-orphan"),
    )
    source_meeting = relationship("Meeting", foreign_keys=[source_meeting_id])


class Note(Base):
    """
    Portfolio-scoped planner note. Lets PMs jot down topics, action items,
    follow-up reminders etc. between meetings — distinct from ActionItem
    (which is meeting-raised and shows up on agendas) and Agenda (which is a
    formal pre-meeting deliverable). Notes are private to the team.
    """
    __tablename__ = "notes"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)

    project_area = Column(String(150))           # sub-project / discipline / area
    source = Column(String(200))                 # "Meeting: May 14", "Email", "Phone", "Site visit"
    topic = Column(String(300))                  # short title
    action_needed = Column(Text)                 # longer description / to-do
    note_date = Column(Date, nullable=False)     # what date this note is "about"; defaults to today
    follow_up_date = Column(Date)                # optional "revisit on" date
    priority = Column(String(10), default="Medium")  # Low / Medium / High
    status = Column(String(20), default="open")  # open / closed

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship(
        "Project",
        backref=backref("notes", cascade="all, delete-orphan"),
    )


class GeneratedDocument(Base):
    """Audit trail of every PDF/docx/xlsx the app produced."""
    __tablename__ = "generated_documents"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False)
    kind = Column(String(30), nullable=False)        # minutes_pdf / minutes_docx / actions_xlsx / agenda_pdf
    filename = Column(String(300), nullable=False)
    storage_path = Column(String(500), nullable=False)
    file_size_bytes = Column(Integer)
    is_draft = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

"""Database package — SQLAlchemy models, session, repository helpers."""
from db.session import init_db, session_scope
from db.models import (
    Client, Project, ProjectAttendee, GlobalAttendee, Meeting, MeetingAttendee,
    AgendaItem, DiscussionPoint, Deliverable, MeetingDeliverable,
    ActionItem, GeneratedDocument,
)

__all__ = [
    "init_db", "session_scope",
    "Client", "Project", "ProjectAttendee", "GlobalAttendee", "Meeting", "MeetingAttendee",
    "AgendaItem", "DiscussionPoint", "Deliverable", "MeetingDeliverable",
    "ActionItem", "GeneratedDocument",
]

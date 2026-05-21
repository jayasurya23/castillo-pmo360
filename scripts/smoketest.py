"""
End-to-end smoke test that doesn't require OpenAI API access.

Verifies:
  - DB tables create
  - Seed works
  - We can build a fake ParsedMeeting and save it
  - All three documents generate without errors

Run: `python scripts/smoketest.py`
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date

from db import init_db, session_scope
from db.models import Client, Project, Meeting, ActionItem, Deliverable, MeetingDeliverable
from db.repository import list_clients, list_projects
from app.services import save_parsed_meeting, finalize_meeting
from llm.providers import (
    ParsedMeeting, ParsedAttendee, ParsedAgendaItem,
    ParsedDiscussionPoint, ParsedActionItem,
)


def _fake_parsed():
    return ParsedMeeting(
        attendees=[
            ParsedAttendee(full_name="Arun Ramadass", initials="AR", organization="Castillo Engineering"),
            ParsedAttendee(full_name="Rick Castillo", initials="RC", organization="Castillo Engineering"),
            ParsedAttendee(full_name="Cheyne Matheny", initials="CM", organization="Heelstone"),
        ],
        agenda_items=[
            ParsedAgendaItem(text="Electrical: cable approval strategy and IE comments.", discipline="Electrical"),
            ParsedAgendaItem(text="Civil: IDOT comments/status for Snapdragon and Two Blues.", discipline="Civil"),
        ],
        discussion_points=[
            ParsedDiscussionPoint(
                label="IE methodology change",
                content="HDR requested 0% soil moisture at cable periphery, driving higher thermal loads.",
                discipline="Electrical",
            ),
            ParsedDiscussionPoint(
                label="Civil status",
                content="Morning IDOT discussion was productive; awaiting District 9 feedback.",
                discipline="Civil",
            ),
        ],
        action_items=[
            ParsedActionItem(text="Set up call with HDR IE", owner="CK, KC", due_date="2025-11-10", status="open"),
            ParsedActionItem(text="Resend Heelstone tech specs", owner="KC", due_date="2025-11-10", status="completed"),
        ],
    )


def main():
    print("1. Init DB…")
    init_db()

    # Seed first
    print("2. Seed sample data…")
    from scripts.seed import seed
    seed()

    print("3. Save a fake parsed meeting…")
    with session_scope() as session:
        clients = list_clients(session)
        project = list_projects(session, clients[0].id)[0]

        # Add a deliverable so the deliverable table appears
        deliv = Deliverable(
            project_id=project.id,
            project_segment="Snapdragon",
            task="EE 60% Recreation",
            start_status="In Progress",
            delivery_date=date(2025, 11, 13),
        )
        session.add(deliv)
        session.flush()

        meeting = save_parsed_meeting(
            session=session,
            project=project,
            meeting_date=date(2025, 11, 7),
            parsed=_fake_parsed(),
            raw_notes="(test notes)",
            title="Smoke test meeting",
        )
        # Link the deliverable
        session.add(MeetingDeliverable(
            meeting_id=meeting.id,
            deliverable_id=deliv.id,
            order_index=0,
            carried_from_prior=False,
        ))
        session.flush()
        meeting_id = meeting.id
        print(f"   Created meeting #{meeting_id} with {len(meeting.attendees)} attendees")

    print("4. Generate documents…")
    with session_scope() as session:
        meeting = session.get(Meeting, meeting_id)
        paths = finalize_meeting(session, meeting)
        session.commit()
        for kind, p in paths.items():
            size = Path(p).stat().st_size if Path(p).exists() else 0
            print(f"   {kind}: {p} ({size:,} bytes)")

    print("\n✅ Smoke test passed!")
    print(f"\nOpen the files in data/outputs/ to inspect the branding.")


if __name__ == "__main__":
    main()

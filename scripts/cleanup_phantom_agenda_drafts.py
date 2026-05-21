"""
One-shot cleanup for phantom "Pre-meeting agenda — <date>" draft Meeting rows
left behind by earlier versions of ``generate_next_agenda`` that persisted a
fresh row on every Generate click.

Only deletes rows that are TRULY empty (no attendees, no discussion points, no
raised action items, no meeting deliverables) so legitimate meetings — even
ones that happened to be titled "Pre-meeting agenda — …" — are never touched.
Prints a dry-run summary first and prompts for confirmation.

Run: ``python scripts/cleanup_phantom_agenda_drafts.py``
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import session_scope
from db.models import (
    Meeting, MeetingAttendee, DiscussionPoint, ActionItem, MeetingDeliverable,
)


def _phantom_query(session):
    """Return all Meeting rows matching the phantom title+stage pattern and
    having no children of any of the four tracked kinds."""
    candidates = (
        session.query(Meeting)
        .filter(Meeting.stage == "draft")
        .filter(Meeting.title.like("Pre-meeting agenda%"))
        .order_by(Meeting.project_id, Meeting.meeting_date)
        .all()
    )
    phantoms, kept = [], []
    for m in candidates:
        n_att = session.query(MeetingAttendee).filter_by(meeting_id=m.id).count()
        n_dp = session.query(DiscussionPoint).filter_by(meeting_id=m.id).count()
        n_act = session.query(ActionItem).filter_by(originating_meeting_id=m.id).count()
        n_md = session.query(MeetingDeliverable).filter_by(meeting_id=m.id).count()
        info = {
            "id": m.id,
            "project_id": m.project_id,
            "project_name": m.project.name if m.project else "?",
            "date": m.meeting_date,
            "title": m.title or "",
            "n_attendees": n_att,
            "n_discussion": n_dp,
            "n_actions": n_act,
            "n_deliverables": n_md,
        }
        if n_att == 0 and n_dp == 0 and n_act == 0 and n_md == 0:
            phantoms.append(info)
        else:
            kept.append(info)
    return phantoms, kept


def _print_row(row):
    print(
        f"  #{row['id']:>4}  "
        f"{row['date'].isoformat()}  "
        f"[{row['project_name']}]  "
        f"att={row['n_attendees']} dp={row['n_discussion']} "
        f"act={row['n_actions']} deliv={row['n_deliverables']}  "
        f"— {row['title']}"
    )


def main():
    with session_scope() as session:
        phantoms, kept = _phantom_query(session)

    print(f"Found {len(phantoms)} phantom draft(s) matching the cleanup criteria")
    print("  (title LIKE 'Pre-meeting agenda%' AND stage='draft' AND no children)\n")

    if phantoms:
        print("Will DELETE:")
        for row in phantoms:
            _print_row(row)
        print()

    if kept:
        print(
            f"Skipping {len(kept)} row(s) that match the title+stage pattern "
            "but have children (inspect manually if you believe these are phantoms):"
        )
        for row in kept:
            _print_row(row)
        print()

    if not phantoms:
        print("Nothing to delete. Exiting.")
        return

    try:
        resp = input(f"Delete {len(phantoms)} row(s)? [y/N] ").strip().lower()
    except EOFError:
        resp = ""
    if resp not in ("y", "yes"):
        print("Aborted — no changes made.")
        return

    with session_scope() as session:
        ids = [p["id"] for p in phantoms]
        deleted = 0
        for mid in ids:
            m = session.get(Meeting, mid)
            if m is None:
                continue
            session.delete(m)
            deleted += 1
    print(f"Deleted {deleted} phantom draft row(s).")


if __name__ == "__main__":
    main()

"""
Seed the database with the Heelstone / Snapdragon and Two Blues project so
you have something to click on first run.

Run: `python scripts/seed.py`
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import init_db, session_scope
from db.models import Client, Project, ProjectAttendee


def seed():
    init_db()
    with session_scope() as session:
        # Heelstone
        if not session.query(Client).filter_by(name="Heelstone").first():
            heelstone = Client(name="Heelstone", email_domain="heelstone.com")
            session.add(heelstone)
            session.flush()

            project = Project(
                client_id=heelstone.id,
                name="Snapdragon and Two Blues",
                scope="Electrical Design, Civil Design and Studies",
                schedule_version="V7",
            )
            session.add(project)
            session.flush()

            # Seed the attendee roster from the Nov 7 meeting
            roster = [
                ("Arun Ramadass",     "AR", "Castillo Engineering"),
                ("Rick Castillo",     "RC", "Castillo Engineering"),
                ("Manjil Puri",       "MP", "Castillo Engineering"),
                ("Roashaael Mary John", "RM", "Castillo Engineering"),
                ("Brett Beattie",     "BB", "Castillo Engineering"),
                ("Cheyne Matheny",    "CM", "Heelstone"),
                ("Jacob Cardin",      "JC", "KE Way"),
                ("Kyle Cunningham",   "KC", "KE Way"),
                ("Alexander Rezansoff", "AR", "KE Way"),
                ("Michael Kutz",      "MK", "KE Way"),
                ("Jake Olsen",        "JO", "KE Way"),
                ("Andy Thomforde",    "AT", "KE Way"),
            ]
            for name, init, org in roster:
                session.add(ProjectAttendee(
                    project_id=project.id,
                    full_name=name,
                    initials=init,
                    organization=org,
                ))
            print(f"Created Heelstone / Snapdragon and Two Blues with {len(roster)} roster entries")
        else:
            print("Heelstone already seeded.")


if __name__ == "__main__":
    seed()

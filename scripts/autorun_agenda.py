"""
End-to-end auto-fill smoke test of the Next Agenda flow.

Picks the first project in the DB, picks the latest real meeting as the
source, fabricates plausible engineering-meeting content for each section
(Discussion Points / Recap / Risks / Decisions), pulls real carry-forward
actions + deliverables from the DB, then calls both
``generate_premeeting_agenda_pdf`` and ``generate_premeeting_agenda_docx``.

Writes the outputs to ``data/outputs/AUTORUN_Pre_Meeting_Agenda_<...>``
and confirms no phantom Meeting row was created.
"""
import os
import random
import sys
from datetime import date, timedelta
from pathlib import Path

# Allow `python scripts/autorun_agenda.py` from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.session import session_scope, init_db
from db.models import Project, Meeting, Schedule
from db.repository import open_actions, deliverables_to_carry_forward
from app.services import generate_next_agenda, safe_filename_slug
from docgen import (
    generate_premeeting_agenda_pdf,
    generate_premeeting_agenda_docx,
)
from llm.providers import ParsedDiscussionPoint as P

random.seed(7)

DISCIPLINES = ["Civil", "Electrical", "Structural", "Permitting", "Procurement"]

DP_TOPICS = {
    "Civil": [
        ("Site grading review",
         "Walk through proposed cuts/fills against existing topo"),
        ("Drainage",
         "Update storm pond outflow calc with new tributary area"),
        ("Erosion control",
         "Confirm SWPPP submittal date with client"),
        ("Geotech",
         "Awaiting boring log finalization from contractor"),
    ],
    "Electrical": [
        ("Recloser settings",
         "Need utility confirmation on 138kV settings"),
        ("Ampacity study",
         "Aluminum wire selection assumes 90C engineered soil values"),
        ("Auxiliary loads",
         "Confirm 5 kVA transformer adequacy vs. new SCADA enclosure"),
        ("IFP comments",
         "Owner review comments due back Friday"),
    ],
    "Structural": [
        ("Pile depths", "Final depths pending soil report"),
        ("Module rack",
         "Confirm tilt angle change does not impact load case"),
        ("Inverter pads",
         "Reviewing rebar schedule with vendor"),
    ],
    "Permitting": [
        ("AHJ submittal", "Cover sheet drafted, awaiting stamp"),
        ("Building dept response",
         "Comment log items 4 and 7 still open"),
    ],
    "Procurement": [
        ("Transformer lead time",
         "Vendor quoting 18 weeks - flag in schedule"),
        ("Switchgear",
         "Quote refresh needed for revised one-line"),
    ],
}

RISK_POOL = [
    ("Utility study delay",
     "Pushes interconnection date by 4-6 weeks",
     "High",
     "Weekly coordination call; escalate via project sponsor",
     "Arun Ramadass"),
    ("Geotech results variance",
     "May invalidate current rack design",
     "Medium",
     "Request preliminary borings before 60% submittal",
     "Cheyne Matheny"),
    ("Long-lead transformer",
     "Cannot energize without it",
     "Medium",
     "Place order at 60% to lock vendor",
     "Rick Castillo"),
    ("Permitting cycle",
     "AHJ has been slow to respond historically",
     "Low",
     "Schedule pre-app meeting with planning dept",
     "Arun Ramadass"),
]

DECISION_POOL = [
    ("Inverter brand selection",
     "SunPower vs. Sungrow vs. CPS - owner preference?",
     "Vendor lock-in affects spare parts strategy",
     7,
     "Rick Castillo"),
    ("Module orientation",
     "East-West vs. South tilt for the south parcel",
     "Civil layout cannot lock until selected",
     14,
     "Cheyne Matheny, Arun Ramadass"),
    ("Recloser scheme",
     "Utility-spec vs. owner-supplied",
     "Drawings cannot finalize until utility confirms",
     10,
     "Roashaael Mary John"),
]


def main() -> None:
    init_db()

    with session_scope() as session:
        project = session.query(Project).first()
        if not project:
            print("No project in DB - run scripts/seed.py first.")
            return
        print(f"Project:    {project.name}")
        print(
            f"Client:     "
            f"{project.client.name if project.client else '(none)'}"
        )

        meeting_count_before = (
            session.query(Meeting).filter_by(project_id=project.id).count()
        )

        candidates = (
            session.query(Meeting)
            .filter_by(project_id=project.id)
            .order_by(Meeting.meeting_date.desc())
            .all()
        )
        real_meetings = [
            m for m in candidates
            if not (m.title or "").startswith("Pre-meeting agenda")
        ]
        source = real_meetings[0] if real_meetings else (
            candidates[0] if candidates else None
        )
        if source:
            print(
                f"Source:     id={source.id}  "
                f"{source.meeting_date}  {(source.title or '')!r}"
            )
        else:
            print("Source:     (none)")

        upcoming = date.today() + timedelta(days=random.choice([3, 5, 7, 10]))
        print(f"Upcoming:   {upcoming}")

        disciplines = random.sample(DISCIPLINES, k=3)
        print(f"Sections:   {disciplines}")

        dp_by_d = {}
        for d in disciplines:
            topics = random.sample(DP_TOPICS[d], k=min(3, len(DP_TOPICS[d])))
            dps = []
            for label, content in topics:
                sub_count = random.randint(0, 2)
                subs = []
                for _ in range(sub_count):
                    sub_label, sub_content = random.choice(DP_TOPICS[d])
                    subs.append(P(
                        label=sub_label, content=sub_content, discipline=d,
                    ))
                dps.append(P(
                    label=label, content=content, discipline=d,
                    sub_points=subs,
                ))
            dp_by_d[d] = dps
        dp_total = sum(len(v) for v in dp_by_d.values())
        print(f"DP points:  {dp_total} top-level")

        recap_by_d = {d: [] for d in disciplines}
        if source:
            for dp in sorted(
                source.discussion_points,
                key=lambda d: d.order_index,
            ):
                if dp.parent_id is not None:
                    continue
                disc = (dp.discipline or "General").capitalize()
                target = (
                    disc if disc in recap_by_d
                    else random.choice(disciplines)
                )
                recap_by_d[target].append(P(
                    label=dp.label or "",
                    content=dp.content or "",
                    discipline=target,
                ))
        rec_total = sum(len(v) for v in recap_by_d.values())
        print(f"Recap pts:  {rec_total} (from source meeting)")

        risks = [
            {
                "description": d, "impact": im, "likelihood": lh,
                "mitigation": mit, "owner": own,
            }
            for (d, im, lh, mit, own) in random.sample(RISK_POOL, k=3)
        ]
        print(f"Risks:      {len(risks)}")

        decisions = []
        for (dec, desc, imp_if, days_out, owner) in random.sample(
            DECISION_POOL, k=2,
        ):
            decisions.append({
                "decision": dec, "description": desc,
                "impact_if_not": imp_if,
                "required_by": upcoming + timedelta(days=days_out),
                "owner": owner,
            })
        print(f"Decisions:  {len(decisions)}")

        carry_actions = list(open_actions(session, project.id))
        carry_deliv = list(
            deliverables_to_carry_forward(session, project.id)
        )
        sched = (
            session.query(Schedule)
            .filter_by(project_id=project.id)
            .order_by(Schedule.uploaded_at.desc())
            .first()
        )
        print(f"Actions:    {len(carry_actions)} carry-forward")
        print(f"Deliv:      {len(carry_deliv)} carry-forward")

        draft = generate_next_agenda(
            session, project, upcoming,
            source_meeting_id=source.id if source else None,
        )
        print(f"Attendees:  {len(draft.attendees)} on draft view")

        gen_kwargs = dict(
            meeting=draft,
            prior_meeting=source,
            carry_actions=carry_actions,
            carry_deliverables=carry_deliv,
            schedule=sched,
            disciplines=disciplines,
            dp_by_discipline=dp_by_d,
            recap_by_discipline=recap_by_d,
            risks=risks,
            decisions=decisions,
        )

        pdf_bytes = generate_premeeting_agenda_pdf(**gen_kwargs)
        docx_bytes = generate_premeeting_agenda_docx(**gen_kwargs)

        proj_slug = safe_filename_slug(project.name or "project")
        stem = (
            f"AUTORUN_{proj_slug}_Pre_Meeting_Agenda_{upcoming.isoformat()}"
        )
        os.makedirs("data/outputs", exist_ok=True)
        pdf_path = f"data/outputs/{stem}.pdf"
        docx_path = f"data/outputs/{stem}.docx"
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)

        meeting_count_after = (
            session.query(Meeting).filter_by(project_id=project.id).count()
        )

        print()
        print(f"OK PDF   {len(pdf_bytes):>7} bytes -> {pdf_path}")
        print(f"OK DOCX  {len(docx_bytes):>7} bytes -> {docx_path}")
        print()
        print(
            f"Meeting rows before: {meeting_count_before}  "
            f"after: {meeting_count_after}  "
            f"({'OK no leak' if meeting_count_before == meeting_count_after else 'LEAKED'})"
        )


if __name__ == "__main__":
    main()

"""
LLM provider for parsing meeting notes into structured form.

Uses OpenAI's Chat Completions API with JSON-mode response_format.
Provider interface is abstract so we can swap to Anthropic or others later
without touching the rest of the app.
"""
from abc import ABC, abstractmethod
from typing import Optional
import json

from pydantic import BaseModel, Field
from openai import OpenAI

from config import openai_api_key, openai_model


# ============================================================
# Output schema — what the LLM is asked to return
# ============================================================
class ParsedAttendee(BaseModel):
    full_name: str = Field(..., description="Person's full name when known, otherwise just initials")
    initials: str = Field(..., description="2-3 letter initials, e.g. 'AR'")
    organization: str = Field(default="", description="Company/org if stated, otherwise empty")


class ParsedAgendaItem(BaseModel):
    text: str
    discipline: str = Field(default="General", description="Electrical / Civil / General")


class ParsedDiscussionPoint(BaseModel):
    label: str = Field(..., description="Short bold lead like 'IE methodology change'")
    content: str = Field(..., description="The discussion detail after the label")
    discipline: str = Field(default="General")
    sub_points: list["ParsedDiscussionPoint"] = Field(
        default_factory=list,
        description="Nested sub-points (indented bullets under this point in the notes).",
    )


class ParsedActionItem(BaseModel):
    text: str
    owner: str = Field(default="", description="Initials, possibly comma-separated like 'CK, KC'")
    due_date: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD if stated")
    status: str = Field(default="open", description="open / pending / completed / cancelled")


class ParsedMeeting(BaseModel):
    attendees: list[ParsedAttendee]
    agenda_items: list[ParsedAgendaItem]
    discussion_points: list[ParsedDiscussionPoint]
    action_items: list[ParsedActionItem]


# ============================================================
# Provider interface
# ============================================================
class LLMProvider(ABC):
    @abstractmethod
    def parse_meeting_notes(
        self,
        minutes_text: str,
        agenda_text: str = "",
        actions_text: str = "",
        project_context: str = "",
        attendees_roster: Optional[list[dict]] = None,
    ) -> ParsedMeeting:
        """Extract structured meeting data from three separated note sections.

        `attendees_roster` is an optional list of {"full_name", "initials",
        "organization"} dicts representing people already selected for the
        meeting. When provided, the LLM uses them as a name→initials lookup
        so that:
          - Discussion-point text that references a first name only is mapped
            to the matching attendee
          - `action_items[*].owner` is filled with the matching initials
            (comma-separated when multiple people are mentioned)
        """


# ============================================================
# OpenAI implementation
# ============================================================
SYSTEM_PROMPT = """\
You are a structured-data extractor for an electrical engineering consulting firm \
(Castillo Engineering, solar PV projects). You receive three clearly delimited \
sections of raw notes — a MEETING MINUTES section, an AGENDA section, and an \
ACTION ITEMS section — and return a JSON object matching the provided schema. \
Be faithful to the source: do NOT invent anything that isn't present.

Section scoping (important):
- Extract `attendees` and `discussion_points` ONLY from the MEETING MINUTES \
  section.
- Extract `agenda_items` from the AGENDA section when it has content. If the \
  AGENDA section is empty (or has fewer than 2 distinct lines), ALSO scan \
  the MEETING MINUTES section for agenda-style topic headers — short phrases \
  like "Due Diligence", "Folder Structure", "General Concerns", or any \
  section labels that look like a list of meeting topics. Deduplicate. If \
  neither source has agenda topics, return an empty list.
- Extract `action_items` ONLY from the ACTION ITEMS section. If the other \
  sections describe action-like tasks, ignore them here — the ACTION ITEMS \
  section is authoritative.

Conventions:
- Attendees: use initials when given (e.g. "AR", "CK, KC"); leave full_name empty \
  if only initials appear.
- Discipline tags must be one of: Electrical, Civil, General.
- Agenda items are the topics being DISCUSSED (the bulleted plan), not action \
  items. Preserve the wording from the AGENDA section verbatim.
- Discussion points have a short bold "label" (a few words) followed by the \
  detail. Example: label="IE methodology change", content="HDR requested more \
  stringent soil moisture assumptions..."
- Discussion points may also have NESTED sub-points (indented bullets in the \
  notes). Capture these in the `sub_points` list of the parent point, each \
  with its own label/content/discipline. Sub-points themselves can have \
  further sub-points (use the same `sub_points` field recursively); keep \
  nesting to at most 2 levels deep unless the source clearly shows more.
- Action items: extract owner initials and ISO due dates when present. Default \
  status is "open" unless the source says done/completed/pending/cancelled.
- Preserve the wording in the source. Do not paraphrase aggressively.

ATTENDEE ROSTER MAPPING (important when an ATTENDEES list is provided below):
- The user has already selected attendees for this meeting and listed them \
  before the notes. Treat that list as the authoritative name→initials lookup.
- When discussion-point text mentions a person by first name, partial name, \
  or initials, normalize references in your output to use the full_name + \
  initials from the roster.
- For the `attendees` output field: emit ONLY people who are mentioned in \
  the MEETING MINUTES text but who are NOT already on the roster (matched \
  by initials, exact full name, first name, OR last name). Do NOT re-list \
  people who are already on the roster — the application already has them. \
  If everyone mentioned is already on the roster, return an empty list for \
  `attendees`.
- For `action_items[*].owner`, ALWAYS emit FULL NAMES from the roster (not \
  initials). Multiple people on the roster can share initials, so initials \
  are ambiguous. Example outputs: "Roashaael Mary John", "Andrew Proctor", \
  or "Roashaael Mary John, Dylan Wraga" when two people are named. \
  If a person mentioned in the actions text is NOT on the roster, emit the \
  name exactly as it appears in the notes. If only initials are shown in the \
  notes and they match someone on the roster, expand them to that person's \
  full name. Comma-separate multiple owners.
- If the roster is empty or omitted, behave as before.
"""

USER_PROMPT_TEMPLATE = """\
Project context: {context}

{attendees_block}=== MEETING MINUTES ===
\"\"\"
{minutes}
\"\"\"

=== AGENDA ===
\"\"\"
{agenda}
\"\"\"

=== ACTION ITEMS ===
\"\"\"
{actions}
\"\"\"

Return a JSON object with this exact structure:
{{
  "attendees": [{{"full_name": "string", "initials": "string", "organization": "string"}}],
  "agenda_items": [{{"text": "string", "discipline": "Electrical|Civil|General"}}],
  "discussion_points": [{{"label": "string", "content": "string", "discipline": "Electrical|Civil|General", "sub_points": [/* recursive */]}}],
  "action_items": [{{"text": "string", "owner": "string (full names, comma-separated)", "due_date": "YYYY-MM-DD or null", "status": "open|pending|completed|cancelled"}}]
}}
"""


class OpenAIProvider(LLMProvider):
    def __init__(self, model: Optional[str] = None):
        self.client = OpenAI(api_key=openai_api_key())
        self.model = model or openai_model()

    def parse_meeting_notes(
        self,
        minutes_text: str,
        agenda_text: str = "",
        actions_text: str = "",
        project_context: str = "",
        attendees_roster: Optional[list[dict]] = None,
    ) -> ParsedMeeting:
        if attendees_roster:
            lines = [
                f"- {p.get('full_name','')} ({p.get('initials','')})"
                + (f" — {p['organization']}" if p.get("organization") else "")
                for p in attendees_roster
            ]
            attendees_block = (
                "=== ATTENDEES (use these names+initials as the authoritative roster) ===\n"
                + "\n".join(lines) + "\n\n"
            )
        else:
            attendees_block = ""

        user_msg = USER_PROMPT_TEMPLATE.format(
            context=project_context or "Solar PV engineering project",
            attendees_block=attendees_block,
            minutes=minutes_text or "(none)",
            agenda=agenda_text or "(none)",
            actions=actions_text or "(none)",
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )

        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("OpenAI returned empty response")

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenAI returned invalid JSON: {exc}\n---\n{content}")

        return ParsedMeeting(**data)


# ============================================================
# Factory
# ============================================================
def get_provider() -> LLMProvider:
    """Returns the configured LLM provider. Currently OpenAI; swap by env later."""
    return OpenAIProvider()

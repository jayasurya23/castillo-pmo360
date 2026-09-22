# Implementation Plan

Detailed phase-by-phase breakdown for the Castillo Meeting Management Tool.

## Phase 1 — Local prototype ✅ DONE

**Duration:** 4 days
**Goal:** Working end-to-end on a developer's machine using SQLite + local files.

- [x] SQLAlchemy models for Client, Project, Meeting, Attendee, AgendaItem, DiscussionPoint, Deliverable, ActionItem, GeneratedDocument
- [x] `session_scope()` context manager
- [x] Streamlit app skeleton with 7-page sidebar nav
- [x] OpenAI provider + structured Pydantic schemas
- [x] Word .docx generation with Castillo branding (red bold headings, red underlined sections, table styling)
- [x] Excel .xlsx rolling action items log
- [x] PDF generation via ReportLab (flat, no form fields yet)
- [x] `scripts/seed.py` creates the Heelstone / Snapdragon and Two Blues sample project
- [x] `scripts/smoketest.py` runs the full pipeline without OpenAI

## Phase 2 — AI parsing + roster + status dropdowns 🟡 IN PROGRESS

**Duration:** 5 days
**Goal:** PM can paste notes, get them structured by OpenAI, edit inline, and manage attendee roster.

- [x] OpenAI Chat Completions integration with JSON-mode response
- [x] Pydantic schemas for parsed output (`ParsedMeeting`)
- [x] System prompt tuned for Castillo's engineering context
- [x] Project-level attendee roster (`ProjectAttendee` table) persists across meetings
- [ ] Roster chips on Review screen — click to add to current meeting (Streamlit limitation: needs custom component or st.button workaround)
- [ ] Inline edit of discussion points and action items in the Streamlit UI
- [ ] Status dropdown UX — `st.selectbox` per row works but feels clunky; consider streamlit-aggrid for better table interaction
- [ ] Token usage tracking + cost estimate displayed to PM after each parse

**Risks:**
- Streamlit's table editing UX is limited compared to what was mocked. May need streamlit-aggrid or a custom component.
- OpenAI rate limits — `gpt-4o-mini` is cheap but synchronous parsing for a long meeting can take 5-10s. Show a clear spinner.

## Phase 3 — PDF form fields with pikepdf ⏳

**Duration:** 4 days
**Goal:** Generate PDFs where the Status column on the Action Items table has fillable dropdowns the client can edit in Adobe Reader and send back.

### Implementation approach

1. Generate the flat PDF with ReportLab at known coordinates (specifically: hold the Action Items table's Status column at a fixed x-range so we can calculate rect positions later)
2. Use pikepdf to open the generated PDF
3. For each row in the Action Items table, calculate the rectangle of the Status cell
4. Add an AcroForm choice field at that rect with options `["Open", "Pending", "Completed", "Cancelled"]`
5. Style with the Castillo gold border (`#c7bb2e`) so it visually reads as editable

### Key code locations

- `docgen/pdf_builder.py::add_status_form_fields()` — stub already exists with implementation notes in docstring
- Field naming convention: `action_status_{i}` where `i` is the 0-indexed action row

### Re-import logic

When the client returns the PDF, the app reads it back:
1. pikepdf opens the PDF, iterates AcroForm fields
2. For each `action_status_*` field, parse the index and current value
3. Match back to the `ActionItem.id` (need a mapping table: meeting_id + index → action_id)
4. Call `update_action_status()` to apply the change

Save the original action IDs in a JSON sidecar or in a hidden form field so we don't have to re-derive the mapping.

### Risks
- Adobe Reader vs Foxit vs browser PDF viewers handle AcroForms differently. Test in Adobe Reader DC, Adobe Acrobat Pro, Chrome's built-in viewer, and Edge.
- Form fields don't survive flatten-on-save in some workflows. Test the round-trip carefully.

## Phase 4 — Pre-meeting agenda generator ⏳

**Duration:** 2 days
**Goal:** Auto-build the next meeting's pre-agenda from prior minutes.

### Behavior

When PM hits "Generate next agenda":
1. Find the latest meeting for this project
2. Pull all open + pending action items from that meeting (and earlier still-open ones)
3. Pull discussion points from the last meeting as "previous week recap"
4. Carry attendees forward
5. Leave new discussion points empty (PM fills in during the actual meeting)
6. Bump the schedule version if a new schedule PDF was uploaded since last meeting

### Implementation

- Stub already in `app/services.py::generate_next_agenda()`
- Document generation: similar to meeting minutes but title is "Pre-Meeting Agenda" and the sections are different
- New docgen function: `generate_pre_meeting_agenda_pdf()` and `generate_pre_meeting_agenda_docx()`

## Phase 5 — Multi-user infrastructure ⏳

**Duration:** 1 week
**Goal:** One URL the whole Castillo team can use, with proper auth and shared storage.

### Pieces

1. **PostgreSQL migration** — `config.database_url()` already handles this. Alembic is set up (`migrations/`, baseline revision `7d5911baf6ed`) and reads its URL from `config.database_url()`; new schema changes should be authored as migrations from here on rather than ad hoc `ALTER TABLE` in `_bootstrap_migrations()`.
2. **SharePoint backend** — implement `storage/backend.py::SharePointBackend` methods using `msal` for auth and Graph API for file operations
3. **Azure AD SSO** — wrap Streamlit in `streamlit-authenticator` or a reverse proxy doing OAuth
4. **Concurrent edits** — Postgres row locking + UI banner when another user has the meeting open
5. **Deployment** — pick one: internal VM, Azure App Service, or Streamlit Cloud

### Azure AD setup

See `docs/PRODUCTION_SETUP.md` for the full registration walkthrough. Needed permissions:
- `Sites.ReadWrite.All` (delegated) for SharePoint
- `Mail.Send` for outbound email
- `User.Read` for SSO

## Phase 6 — Polish + email + training ⏳

**Duration:** 4 days
**Goal:** Hand off to the PM team.

- Email composition flow — pre-filled subject and body, recipient picker from attendee roster
- Send via Microsoft Graph with attachments
- Reply tracking — when a recipient opens an action item dropdown and replies, parse the returned PDF and apply changes
- Onboarding doc with screenshots
- 30-min training session for PM team
- Inline action item editing (not just status — text, owner, due date)

## Phase 7 — Schedule module integration ⏳

**Duration:** 1 week
**Goal:** Connect uploaded proposal PDFs to the meetings module so deliverables come from a single source of truth.

- Parser for Castillo proposal PDFs (reference: `templates/Sample_Project_V1_Proposal.pdf`)
- Extract: project name, client, duration, deliverable line items with dates and prices
- Version tracking — each upload creates a new schedule version with a diff against the previous
- "At-risk" flagging — items due within 14 days that haven't appeared on a meeting in 2+ weeks
- Wire the meetings deliverables picker to search the parsed schedule

## Total timeline

**5–6 weeks** for one engineer working alone, with phases 3+4 and 5+6 running in parallel where feasible.

See `Castillo_Meeting_Tool_Plan.pdf` (or the one-page version) for the management-friendly summary.

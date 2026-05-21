# Claude Code Prompts

Copy-paste prompts for Claude Code to continue building each remaining phase. Each one assumes Claude Code is open in the project root and has already read `CLAUDE.md` and `docs/IMPLEMENTATION_PLAN.md`.

---

## Phase 2 (finish): Roster click-to-add + status dropdown polish

```
Finish Phase 2 of the Castillo Meeting Management Tool.

Open app/main.py and look at render_review(). The roster chips currently log
to Streamlit instead of adding the attendee. Wire them up properly:

1. When a roster chip is clicked, append that ProjectAttendee to
   st.session_state.parsed.attendees if not already present.
2. After adding, rerun the page so the new attendee appears in the
   "In this meeting" list below.
3. Skip the add if the person (matched by full_name) is already in the
   current meeting's attendee list.

Also improve the action item editing UX:
- Replace the read-only dataframe with st.data_editor so users can edit
  text, owner, due date, and status inline.
- The status column should be a SelectboxColumn with options ["open",
  "pending", "completed", "cancelled"].
- Save edits back to st.session_state.parsed when the dataframe changes.

Verify the smoketest still passes: python scripts/smoketest.py
```

---

## Phase 3: PDF form fields

```
Implement Phase 3 of the Castillo Meeting Management Tool: AcroForm
dropdowns on the Status column of the Action Items table.

The stub is in docgen/pdf_builder.py::add_status_form_fields(). Currently
returns the PDF unmodified.

Requirements:
1. Open the input PDF with pikepdf
2. Locate the Action Items table — easiest approach: when generating the
   PDF in generate_meeting_minutes_pdf(), record the y-coordinate where
   the action items table starts and the height per row. Stash that
   metadata in the PDF's /Info dictionary or in a sidecar JSON.
3. For each action row i (0-indexed), add an AcroForm /Ch (choice) field
   at the Status cell rect.
4. Field name: f"action_status_{i}"
5. Options: ["Open", "Pending", "Completed", "Cancelled"]
6. Default value: whatever the action's current status is
7. Border color: Castillo gold #c7bb2e (BrandColors.GOLD)
8. Background: white
9. The field should be flagged as editable (not read-only)

Also implement re-import: a new function in app/services.py:
  def import_pdf_status_updates(session, meeting_id, pdf_bytes) -> dict

This reads the AcroForm fields back, parses the index from each field
name, maps it to the action_id (which you'll need to persist when
generating the PDF — extend GeneratedDocument with a JSON sidecar
column), and calls db.repository.update_action_status() for each
changed field.

Test by:
1. Generating a PDF
2. Opening it in Adobe Reader, changing some statuses, saving
3. Calling import_pdf_status_updates() on the saved bytes
4. Verifying the database reflects the changes

Update CLAUDE.md to mark Phase 3 complete when done.
```

---

## Phase 4: Pre-meeting agenda

```
Implement Phase 4: pre-meeting agenda generation.

The stub is in app/services.py::generate_next_agenda(). It creates a
new draft Meeting but doesn't yet do the carry-forward logic or generate
documents.

Requirements:
1. Pull all open + pending action items from the project (not just the
   last meeting — these are rolling)
2. Pull discussion points from the last meeting only (those become the
   "previous week recap")
3. Carry attendees forward from the last meeting
4. Leave the new meeting's discussion points empty
5. Set schedule_version_at_meeting from the project (it may have been
   bumped since the last meeting)

Then create two new docgen functions:
- docgen/pdf_builder.py::generate_pre_meeting_agenda_pdf(meeting)
- docgen/document_builder.py::generate_pre_meeting_agenda_docx(meeting)

Both should match the Castillo branding (red headings with underline,
red-header tables) and have these sections:
1. Title: "Pre-Meeting Agenda" (red bold)
2. Date, project, client (same as meeting minutes header)
3. Attendees (carried from last meeting)
4. Carry-forward action items table (only open + pending, ordered by
   due date)
5. Previous week recap (discussion points from last meeting, grouped by
   discipline if useful)
6. Footer

Wire up render_next_agenda() in app/main.py:
- Date picker for upcoming meeting
- "Generate agenda" button that calls generate_next_agenda() then the
  two docgen functions
- Download buttons for the PDF and Word versions
```

---

## Phase 5: SharePoint backend + Postgres + SSO

```
Implement Phase 5: production multi-user infrastructure.

This phase has three parts:

A) SharePoint backend
The stub is at storage/backend.py::SharePointBackend. All three methods
currently raise NotImplementedError.

Implement using msal for auth and requests for the Graph API:
- save(relative_path, content) -> str: PUT to
  /sites/{site}/drives/{drive}/root:/{root}/{path}:/content
- read(relative_path) -> bytes: GET the file content
- list_folder(relative_path) -> list[str]: list children

Auth uses client credentials flow (no user context needed for
backend file ops):
  msal.ConfidentialClientApplication(
    client_id, authority=f"https://login.microsoftonline.com/{tenant_id}",
    client_credential=client_secret,
  ).acquire_token_for_client(["https://graph.microsoft.com/.default"])

Cache the token in memory with expiry check.

B) Postgres
Already supported via DATABASE_URL. Add Alembic:
  pip install alembic
  alembic init alembic
Configure env.py to use DATABASE_URL from config.
Create the initial migration from current models.

C) Azure AD SSO
Add streamlit-authenticator with OIDC config pointing at Azure AD.
Or wrap with a reverse proxy doing OAuth — document both options in
docs/PRODUCTION_SETUP.md.

Update tests to cover the SharePoint backend with mocked Graph
responses. Update docs/PRODUCTION_SETUP.md if anything changed.
```

---

## Phase 6: Polish + Microsoft Graph email

```
Implement Phase 6: email composition and inline editing polish.

A) Email composition flow
After finalize_meeting() succeeds in app/main.py::render_send():
- Show a compose pane with:
  - To: pre-filled from attendee emails (where org doesn't match
    Castillo's domain)
  - CC: Castillo team emails
  - Subject: "Meeting Minutes - {project name} - {date}"
  - Body: short note pointing to the attached PDF, mentioning that
    Status fields are editable in Adobe Reader
  - Attachments: the 3 generated files
- "Send via Outlook" button that calls Microsoft Graph
  /me/sendMail with the attachments base64-encoded

B) Reply tracking (optional Phase 6.5)
Set up a webhook subscription on the SendFromEmail inbox so that when
the client replies with a modified PDF, the app parses the PDF for
status changes and applies them automatically.

C) Inline action item editing
Already partially done in Phase 2. Make sure:
- Editing text, owner, due_date, or status updates the database
- Last-write-wins with a warning if the row was modified after load

D) Training materials
Create docs/USER_GUIDE.md with screenshots of the four main steps and
the actions/history views.
```

---

## Phase 7: Schedule module

```
Implement Phase 7: parse Castillo proposal PDFs into structured
deliverables, integrated with the meetings module.

A) Proposal PDF parser
Reference file: templates/Sample_Project_V1_Proposal.pdf
This is a Castillo proposal showing schedule structure with
deliverables, dates, and prices.

Create scripts/parse_proposal.py that:
1. Opens a proposal PDF
2. Extracts the schedule section (it's typically a multi-page table)
3. Returns a structured list of {category, subcategory, task, days,
   start, finish, price}
4. Uses OpenAI for the harder parsing (semi-structured tables in PDFs
   are a real pain — let the LLM handle it)

B) UI for schedule upload + version management
New page: "Schedule" in the sidebar (currently a stub).
- Upload PDF
- Parse it
- Show the parsed deliverables in a reviewable table
- "Save as V8" creates a new version, marks the previous version as
  superseded
- Show a diff against the previous version

C) Integrate with meetings
- The meetings deliverables picker should search the current schedule
  version
- "At-risk" suggestions: deliverables due within 14 days that haven't
  appeared on a meeting in 2+ weeks
- Carry-forward logic on Review uses the schedule as source of truth

D) Version control
Each schedule upload creates a Schedule row with version, source_pdf
path, parsed_at timestamp. Project.schedule_version updates to point at
the latest.
```

---

## General prompts

### "Run the smoketest and tell me what's broken"
```
Run python scripts/smoketest.py. If it passes, tell me. If it fails,
diagnose the error and propose a fix. Don't change any code without
asking first.
```

### "Add a new attendee field"
```
I want to track attendee phone numbers in addition to email. Add a
phone column to ProjectAttendee, expose it in the roster chips on the
Review screen, and include it in the generated documents (just email
and phone separated by a bullet). Update the smoketest and any
fixtures. Don't break existing tests.
```

### "Switch from OpenAI to another model"
```
Add an AnthropicProvider class to llm/providers.py that uses the
Anthropic Python SDK with claude-3-5-sonnet-latest. Make get_provider()
read an LLM_PROVIDER env var ("openai" or "anthropic", default "openai").
Both providers must return the same ParsedMeeting schema.
```

# AGENTS.md — Castillo Meeting Management Tool

Read this first. It tells you what this project is, where things live, and what's left to build.

## Project at a glance

A Streamlit app for Castillo Engineering PMs to capture meeting minutes, auto-parse them with OpenAI, edit the structured output, generate Castillo-branded client deliverables (PDF + Word + Excel), and track rolling action items across meetings. Organized by **Client → Project → Meeting**.

The user is Castillo, an electrical engineer at Castillo Engineering who works on solar PV interconnection and related projects. They want this tool to replace manual Word-template editing for every weekly client coordination meeting.

## Tech stack — already decided, don't change without asking

- **Python 3.10+** / Streamlit for the UI
- **OpenAI API** for note parsing (configurable via `OPENAI_MODEL` env var, default `gpt-4o-mini`)
- **SQLAlchemy 2.0** ORM with PostgreSQL in prod, SQLite locally
- **Microsoft Graph** for SharePoint storage + email (Phase 5)
- **ReportLab** for PDF generation, **pikepdf** for AcroForm field overlay (Phase 3)
- **python-docx** for Word, **openpyxl** for Excel
- **Pydantic** for LLM response schemas

## Run modes

The `.env` file has a `LOCAL_DEV_MODE` flag:
- `true` (default): SQLite at `data/castillo.db`, files in `data/outputs/`, no Azure deps
- `false` (prod): PostgreSQL + SharePoint + Microsoft Graph email

Always check `config.is_local_dev()` when adding storage or DB code so both modes keep working.

## Codebase map

```
config.py                  Env loading + BrandColors. Never hardcode brand hexes elsewhere.
app/
  main.py                  Streamlit entrypoint with 7-page sidebar nav
  services.py              Workflow orchestration (parse → save → finalize → agenda)
db/
  models.py                SQLAlchemy models. ActionItem has dual FK to Meeting.
  session.py               session_scope() context manager
  repository.py            Query helpers
llm/
  providers.py             OpenAIProvider + abstract LLMProvider interface
  __init__.py
storage/
  backend.py               LocalFSBackend + SharePointBackend (stubbed)
docgen/
  document_builder.py      Word .docx with Castillo branding
  action_log.py            Excel .xlsx rolling action log
  pdf_builder.py           PDF with form-field placeholders
scripts/
  seed.py                  Sample Heelstone / Snapdragon project + roster
  smoketest.py             End-to-end without needing OpenAI
templates/                 Original Word .docm templates from Gary
docs/
  IMPLEMENTATION_PLAN.md   Phase-by-phase breakdown
  PRODUCTION_SETUP.md      Azure AD app registration + Postgres setup
  CLAUDE_CODE_PROMPTS.md   Ready-to-use prompts for each remaining phase
tests/                     pytest unit tests
data/                      SQLite DB and local file outputs (gitignored)
scripts/sync_projects_from_monday.py   Backfills Project.project_number from Monday's Portfolio board (see below)
```

## Monday.com is the source of truth for project identity

`Project.project_number` (String(50), indexed, **not unique**) is the Castillo job number — the key everything else (the QC tool, a future data warehouse) joins on. It didn't exist until it was added here; Monday's Portfolio board carries it on ~38 of 40 projects, so that board is adopted as canonical rather than inventing a new numbering scheme.

- **Two formats circulate** (`NNN-NNN` and `YYMM-NNN`) — stored opaque, never parsed.
- **Deliberately not unique.** A hard constraint that later rejects a legitimate write is worse than a duplicate the sync can report and a human can fix.
- **`scripts/sync_projects_from_monday.py`** is read-only against Monday (refuses any mutation query) and writes nothing to PMO 360 without `--apply`:
  ```
  python scripts/sync_projects_from_monday.py                      # dry run
  python scripts/sync_projects_from_monday.py --apply              # backfill numbers only
  python scripts/sync_projects_from_monday.py --create-missing --apply
  python scripts/sync_projects_from_monday.py --set "Nesler=264-066" --apply
  python scripts/sync_projects_from_monday.py --reconcile-clients --apply
  ```
  Matching existing projects is **by name**. Exact (normalised) matches apply by default; close-but-not-exact matches are only reported, never written, unless `--include-fuzzy` — assigning the wrong job number silently corrupts every downstream join.
- **Client-name drift:** if a client is renamed/corrected in Monday (e.g. "Priority Power" → "Priority Power Management"), a project already here stays filed under the stale name unless `--reconcile-clients` is passed. That flag re-files affected projects and prunes clients left with zero projects by the run — never one that already had projects, or one a person created deliberately. A plain run still reports the drift, just doesn't act on it.
- Near-duplicate client detection uses similarity ratio **plus** whole-word-prefix containment (ratio alone scores "Priority Power" vs "Priority Power Management" at 0.72 and misses it).
- Env: `MONDAY_API_TOKEN` (required), `MONDAY_PORTFOLIO_BOARD_ID` (optional), `MONDAY_API_VERSION` (optional).

## Castillo brand — apply to all generated documents

These hex codes are in `config.BrandColors`. Import from there; never hardcode:

| Color | Hex | Use |
|---|---|---|
| Red | `#ad1f2b` | Primary brand, section headings, table headers |
| Dark red | `#991f2b` | Hover state |
| Near black | `#1a1a1a` | Body text |
| Dark gray | `#4d4d4f` | Secondary text |
| Light gray | `#bcbec0` | Borders |
| Gold | `#c7bb2e` | PDF form field borders (AcroForm dropdowns) |
| Green | `#278747` | Completed status |
| Blue | `#185fa5` | Civil discipline tag |

Font: **Jost** (Regular/Semibold/Bold). Fallback to Helvetica when Jost isn't available (ReportLab default fonts don't include Jost — use Helvetica there).

## Template style — match exactly

The reference template is at `templates/_External__Meeting_Minutes_-_Template_-_mm-dd-yy.docm`. The PDF/Word output should match this layout:

1. **Title** — "Meeting Minutes" — large, red bold
2. **Date** — small, dark gray
3. **Project name** — bold black with red underline below
4. **Client / Scope** — bold key + value pairs
5. **Section headings** — red bold text with thin red underline (`0.75px solid #ad1f2b`)
6. **Tables** — solid red header row with white text, white/light-gray banded body rows
7. **Sections in order**: Attendees → Deliverable Timelines → **Agenda** → Discussion Points → Action Items → Closing Remarks

Agenda goes **between** Deliverable Timelines and Discussion Points — this is important and was confirmed against the source template.

## Phase status

- ✅ **Phase 1 — Local prototype**: DB, models, basic UI, doc generation working
- 🟡 **Phase 2 — AI parsing + roster + status dropdowns** (in progress): OpenAI provider wired up, roster persistence working, status dropdown UX needs polish
- ⏳ **Phase 3 — PDF form fields**: stub in `docgen/pdf_builder.py::add_status_form_fields`. Implement with pikepdf to overlay AcroForm dropdowns on the Status column of the Action Items table. Field names: `action_status_0`, `action_status_1`, etc. Options: `Open` / `Pending` / `Completed` / `Cancelled`. Yellow border (`#c7bb2e`) signals editable.
- ⏳ **Phase 4 — Pre-meeting agenda**: stub in `app/services.py::generate_next_agenda`. Pull open + pending actions from latest meeting, carry attendees forward, leave discussion blank.
- ⏳ **Phase 5 — Multi-user infra**: PostgreSQL migration (already supported), SharePoint backend in `storage/backend.py::SharePointBackend` (currently raises `NotImplementedError`), Azure AD SSO via streamlit-authenticator or msal.
- ⏳ **Phase 6 — Polish + email**: Microsoft Graph email composition, inline action item editing, training materials.
- ⏳ **Phase 7 — Schedule module**: parse Castillo proposal PDFs (see `templates/Sample_Project_V1_Proposal.pdf` for reference) into structured deliverables, connect to meeting deliverables picker.

See `docs/IMPLEMENTATION_PLAN.md` for the full phase breakdown with durations.

## Key conventions

### Session usage
Always wrap DB work in `session_scope()`:
```python
from db import session_scope
with session_scope() as session:
    project = session.get(Project, project_id)
    # ... work
    # auto-commit on exit
```

### LLM usage
The provider is abstract — never instantiate `OpenAI` directly outside `llm/providers.py`. To add a new LLM:
1. Subclass `LLMProvider`
2. Implement `parse_meeting_notes`
3. Update `get_provider()` to switch based on env var

### Storage
Same pattern — use `get_storage()`, never instantiate backends directly. New backends subclass `StorageBackend`.

### Brand colors
Always `from config import BrandColors` — never inline hex codes in docgen or UI code.

### Action items have dual foreign keys
`ActionItem.originating_meeting_id` (where it was raised) and `ActionItem.closed_in_meeting_id` (where it was marked done). SQLAlchemy relationships are configured with explicit `foreign_keys=` arguments. Don't simplify this — the dual-FK is what enables the rolling action log to track close-out meetings.

### The Nov 7 meeting data is the canonical test fixture
The `scripts/seed.py` creates Heelstone / Snapdragon and Two Blues with the real roster from the user's actual Nov 7 meeting. Use this for end-to-end manual testing. Don't make up other test data unless writing isolated unit tests.

## Things to verify when making changes

1. `python scripts/smoketest.py` passes — runs the full DB + docgen pipeline without OpenAI
2. `streamlit run app/main.py` opens cleanly and the sidebar renders Castillo-branded
3. Generated PDFs/docx open without errors in their respective viewers
4. Brand colors look right — pull up any generated document and spot-check the red `#ad1f2b` is consistent

## Open questions / decisions still to make

- **Adobe Reader version compatibility** for form fields (Phase 3 risk)
- **SharePoint folder structure** — currently flat `<project>/` but might want `<client>/<project>/<year>/<month>/`
- **Email send pathway** — direct via Graph or open Outlook with mailto? Graph preferred for tracking, mailto preferred for PM control. Currently undecided.
- **Concurrent edit handling** — Postgres row locking is configured but the UI doesn't show a "someone else is editing" warning yet
- **Schedule version bumping** — should it auto-increment when uploading a new schedule PDF, or stay manual?

## Working with the user

The user is technically strong (electrical engineer doing his own scripting in C# for Cognex VisionPro work) but isn't a Python web dev by trade. When suggesting changes:
- Show diffs/code rather than just descriptions
- Verify Castillo branding survives any UI change
- Run the smoketest before declaring work done
- For new phases, write the phase doc in `docs/` first so there's a paper trail

Branding note from user preferences: **all artifacts and documents use Jost (Regular, Semibold, Bold)** with the primary palette above. This is non-negotiable.

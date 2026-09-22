# PMO 360

![Smoketest](https://github.com/jayasurya23/castillo-pmo360/actions/workflows/smoketest.yml/badge.svg)

> **Project Management workspace for Castillo Engineering's PMs.**
> Capture meeting minutes (AI-assisted), draft pre-meeting agendas, track
> rolling action items, store planner notes, and generate Castillo-branded
> client deliverables (PDF + Word + Excel) — all organized by
> Client → Portfolio → Meeting.

Built as a Streamlit app with SQLAlchemy + OpenAI + ReportLab. Runs entirely
on a laptop with SQLite (local-dev mode) or against PostgreSQL + SharePoint
in production.

---

## Table of contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [The tabs, at a glance](#the-tabs-at-a-glance)
- [Run modes](#run-modes)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Codebase layout](#codebase-layout)
- [Data model](#data-model)
- [Generated deliverables](#generated-deliverables)
- [Development workflow](#development-workflow)
- [Key conventions](#key-conventions)
- [Troubleshooting](#troubleshooting)
- [Phase status](#phase-status)
- [Deployment](#deployment)
- [License](#license)

---

## What it does

Castillo's PMs run weekly coordination meetings with clients across multiple
engineering disciplines (Civil, Electrical, Structural, Permitting). After
each meeting they have to:

- Capture handwritten or transcribed notes
- Structure them into attendees, agenda, discussion points, action items
- Produce a client-ready PDF + Word doc + Excel action log with consistent
  Castillo branding
- Track which action items are still open across meetings
- Prepare an agenda for the next meeting that pulls forward the open items

PMO 360 replaces ~2 hours of manual Word-template editing per meeting with
a guided workflow:

1. **Capture** — paste/upload notes, select attendees from a saved roster
2. **Parse with OpenAI** — `gpt-4o-mini` extracts structured data from free
   text (attendees, agenda items, discussion points with sub-points, action
   items with owners + due dates)
3. **Review & edit** — confirm everything; inline-edit any field, drag rows,
   add/delete items
4. **Preview** — see the actual PDF rasterized in-page (Chrome blocks
   `data:application/pdf` iframes; we sidestep that with `pypdfium2`)
5. **Generate** — Castillo-branded PDF (Jost font, AcroForm status dropdowns)
   + Word doc (template-based) + Excel rolling action log
6. **Plan the next one** — auto-build the pre-meeting agenda with recap,
   carry-forward actions, risks, decisions, schedule changes

---

## Quick start

Requires **Python 3.10+** (3.12 recommended) and an **OpenAI API key**.

```bash
git clone https://github.com/jayasurya23/castillo-pmo360.git
cd castillo-pmo360

# Virtual env
python -m venv venv
venv\Scripts\activate            # macOS/Linux: source venv/bin/activate

# Dependencies
pip install -r requirements.txt

# Config
copy .env.example .env           # macOS/Linux: cp .env.example .env
# Open .env and set OPENAI_API_KEY (and OPENAI_MODEL if you want something
# other than gpt-4o-mini). All other vars can stay as defaults for local dev.

# Seed sample data (Heelstone / Snapdragon and Two Blues, plus Castillo's
# team roster)
python scripts/seed.py

# Run
streamlit run app/main.py
```

Open <http://localhost:8501>. The sidebar should show the seeded client and
portfolio; everything else is ready to use.

### Smoke-test the install

```bash
python scripts/smoketest.py
```

End-to-end check that doesn't need OpenAI: builds the DB, seeds it, fakes
a parsed meeting, generates all three deliverable docs. Exits zero if
everything's wired correctly.

---

## The tabs, at a glance

The app has a tab strip at the top of every page. Each one is a self-contained
workspace for one job:

| Tab | What it does |
|---|---|
| **📥 Capture** | Paste meeting minutes, agenda, and action-item text. Pick attendees (from global roster + portfolio-specific roster + bulk-add). Parse with OpenAI into structured form. |
| **📝 Review** | Edit the parsed attendees / agenda / discussion / action items inline. Multi-select Owner picker per action row. Status dropdown. Due date picker (defaults to meeting date + 7 days). |
| **👁️ Preview** | Rasterized PDF preview before download. Side-by-side PDF + Word download buttons. |
| **📤 Send** | Finalize → generate PDF/DOCX/XLSX into storage. (Microsoft Graph email integration: pending.) |
| **📅 Next Agenda** | Full editor for the pre-meeting coordination agenda — disciplines, discussion points (per discipline), previous-week recap (auto-pulled from source meeting), open action items, risks, required decisions, schedule change log. Saved per-agenda. 30/60-min duration toggle scales the fixed agenda table's time-allocation column. |
| **✅ Actions** | Portfolio-wide rolling action log with full CRUD (add, edit any field, delete with confirmation). Status filter (Open / Pending / Completed / Cancelled / All). Inline color-coded status dropdowns. |
| **📓 Notes** | Per-portfolio planner notes with sub-project picker (Snapdragon / Two Blues / Common / etc.), priority (Low/Med/High), status (Open/Closed), and optional follow-up date. Sorted with upcoming follow-ups first. |
| **📚 History** | Two sub-tabs: **Meeting Minutes** (all saved meetings with Open / Export / Delete) and **Pre-Meeting Agendas** (all saved agendas, same actions). |
| **📊 Schedule** | Upload a Castillo proposal PDF or a duration .xlsx. Parser extracts disciplines / phases / tasks. Saved as versioned `Schedule` (V1, V2, V3…) on the portfolio. |

---

## Run modes

`.env`'s `LOCAL_DEV_MODE` flag switches storage backends:

### `LOCAL_DEV_MODE=true` (default)

- **DB:** SQLite at `data/castillo.db`
- **Files:** `data/outputs/` on local disk
- **Auth:** none — anyone with access to the machine has full app access
- **OpenAI:** required (only thing that talks to the cloud)

This is the development mode. Zero cloud configuration needed beyond an
OpenAI API key. Good for local prototyping and the smoketest.

### `LOCAL_DEV_MODE=false` (production)

- **DB:** PostgreSQL via `DATABASE_URL`
- **Files:** SharePoint via Microsoft Graph (the backend is currently a
  stub — see [docs/PRODUCTION_SETUP.md](docs/PRODUCTION_SETUP.md) for the
  Azure AD app registration steps and the implementation outline)
- **Auth:** Azure AD SSO (planned)

The app refuses to start in production mode if any of the required Azure /
DB env vars are missing.

---

## Configuration

All configuration is in `.env` (loaded by `python-dotenv` at startup). See
`.env.example` for the full list with comments.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `OPENAI_API_KEY` | ✅ | — | From <https://platform.openai.com/api-keys> |
| `OPENAI_MODEL` |  | `gpt-4o-mini` | Any chat-completion model — `gpt-4o`, `gpt-4-turbo`, etc. |
| `LOCAL_DEV_MODE` |  | `true` | `false` switches to Postgres + SharePoint |
| `SQLITE_PATH` |  | `data/castillo.db` | Local dev only |
| `DATABASE_URL` | ✅ (prod) | — | e.g. `postgresql://user@host:5432/db?sslmode=require` |
| `AZURE_TENANT_ID` | ✅ (prod) | — | Castillo's M365 tenant |
| `AZURE_CLIENT_ID` | ✅ (prod) | — | App registration's client ID |
| `AZURE_CLIENT_SECRET` | ✅ (prod) | — | App secret |
| `SHAREPOINT_SITE_ID` | ✅ (prod) | — | Lookup via Graph Explorer |
| `SHAREPOINT_DRIVE_ID` | ✅ (prod) | — | The "Documents" drive on the site |
| `SHAREPOINT_ROOT_FOLDER` |  | `Castillo Meeting Tool` | Top-level folder under the drive |
| `APP_PORT` |  | `8501` | Streamlit listen port |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Streamlit UI (app/)                    │
│   Capture · Review · Preview · Send · Next Agenda · …       │
└───────────┬───────────────────────┬─────────────────────────┘
            │                       │
            ▼                       ▼
   ┌─────────────────┐    ┌──────────────────────┐
   │  app/services   │    │   llm/providers      │
   │ (orchestration) │───▶│ OpenAI / abstract    │
   └────────┬────────┘    │ ParsedMeeting models │
            │             └──────────────────────┘
            ▼
   ┌──────────────────┐   ┌─────────────────┐   ┌────────────────┐
   │  db/ (SQLAlchemy │   │  docgen/        │   │  storage/      │
   │  models +        │   │  PDF · DOCX ·   │   │  LocalFS or    │
   │  repository)     │   │  XLSX builders  │   │  SharePoint    │
   └──────────────────┘   └─────────────────┘   └────────────────┘
            │                     │                     │
            ▼                     ▼                     ▼
       SQLite/Postgres     ReportLab + python-docx   Disk / MS Graph
```

Three principles that keep this manageable:

1. **The UI never talks to OpenAI or storage directly.** Always through
   `app/services.py` or the abstracted providers (`llm.providers.get_provider`,
   `storage.get_storage`).
2. **The DB layer doesn't know about Streamlit.** `db/repository.py` is
   pure SQLAlchemy + Python; you could swap the UI for a CLI without
   touching it.
3. **Document generation is stateless.** `docgen/*` takes in plain data
   (or detached SQLAlchemy snapshots) and returns bytes. No side effects.

---

## Codebase layout

```
.
├── app/
│   ├── main.py             Streamlit entrypoint + all rendering functions
│   └── services.py         Orchestration layer (parse → save → finalize)
├── db/
│   ├── models.py           SQLAlchemy declarative models
│   ├── repository.py       Query helpers (the only place that writes SQL)
│   └── session.py          session_scope() + init_db + bootstrap migrations
├── docgen/
│   ├── document_builder.py     Word .docx generators (template + from-scratch)
│   ├── pdf_builder.py          Castillo-branded PDF (Jost, AcroForm dropdowns)
│   ├── template_filler.py      .docm → .docx + anchor-replacement for the
│   │                           meeting-minutes template
│   └── action_log.py           Excel rolling action log
├── llm/
│   └── providers.py        LLMProvider abstract + OpenAIProvider + Pydantic
│                           ParsedMeeting schemas
├── storage/
│   └── backend.py          LocalFSBackend + SharePointBackend (stub)
├── schedule_parser/
│   └── parser.py           Proposal PDF + duration XLSX → ScheduleItem list
├── assets/logo/            PMO 360 + Castillo brand logos (PNG)
├── templates/              Original .docm templates from Gary
│                           + Sample_Project_V1_Proposal.pdf for parser tests
├── scripts/
│   ├── seed.py             Heelstone / Snapdragon + Castillo team roster
│   ├── smoketest.py        End-to-end check (no OpenAI)
│   ├── full_coverage_test.py  Builds a complete portfolio + meeting + agenda;
│   │                          produces both PDFs with every section populated
│   └── autorun_agenda.py   Random-data agenda generation for spot checks
├── docs/
│   ├── IMPLEMENTATION_PLAN.md  Phase-by-phase breakdown
│   ├── PRODUCTION_SETUP.md     Azure AD + Postgres walkthrough
│   └── CLAUDE_CODE_PROMPTS.md  Ready-to-use prompts for AI-assisted work
├── tests/                  pytest unit tests
├── data/                   SQLite DB + generated outputs (gitignored)
├── .github/workflows/
│   └── smoketest.yml       CI: runs scripts/smoketest.py on every push
├── .streamlit/
│   └── config.toml         Theme (Jost font + sidebar config)
├── CLAUDE.md               Project context for AI-assisted dev
├── LICENSE                 Proprietary, all rights reserved
└── requirements.txt
```

---

## Data model

Hierarchy: **Client → Portfolio → Meeting → (Attendees, Agenda, Discussion,
Actions, Deliverables)** plus standalone per-portfolio entities (Agenda
drafts, Notes, Schedules).

| Table | Purpose |
|---|---|
| `clients` | Castillo's client companies (E-Light, Heelstone, etc.) |
| `projects` | Portfolios. The "Project" name is historical — see *Naming note* below |
| `global_attendees` | Castillo team roster, auto-seeded on first run |
| `project_attendees` | Portfolio-specific attendee roster (people seen on prior meetings) |
| `meetings` | One row per actual meeting that happened. Has `stage` (draft / final / sent) |
| `meeting_attendees` | Who attended this specific meeting |
| `agenda_items` | Items on the meeting's agenda (separate from the formal pre-meeting agenda) |
| `discussion_points` | Free-form discussion items with optional sub-points (self-referential FK) |
| `action_items` | The rolling action log. **Dual FK to `meetings`:** `originating_meeting_id` (where raised) and `closed_in_meeting_id` (where closed) |
| `deliverables` | Portfolio-level deliverable definitions |
| `meeting_deliverables` | Which deliverables a specific meeting reports on |
| `agendas` | Saved pre-meeting coordination agenda drafts (JSON columns for all editor state) |
| `notes` | Per-portfolio planner notes |
| `schedules` + `schedule_items` | Parsed proposal/duration schedules (V1, V2, …) |
| `generated_documents` | Audit trail of every PDF/DOCX/XLSX generated |

### Naming note: "Portfolio" vs "Project"

Castillo runs **portfolios** containing multiple sub-projects (e.g. the
"Raven, Gonzo and Waxwing" portfolio contains three solar projects). The
DB models the portfolio as the `Project` table for historical reasons —
the UI exclusively says "Portfolio" everywhere a PM looks. The sub-projects
within a portfolio are tracked as:

- `Deliverable.project_segment` — for the Deliverable Timelines table
- `Note.project_area` — for the Notes tab (with a curated dropdown
  stored on `Project.sub_projects_json`)

---

## Generated deliverables

Every meeting produces three deliverables; the agenda flow produces two:

### Meeting Minutes
- **PDF** — `<Portfolio>_Meeting_Minutes_<YYYY-MM-DD>.pdf`. Castillo red banner
  with the white Castillo logo, PMO 360 logo on the first page, Jost font,
  AcroForm status dropdowns on the Action Items table (clients can change
  the status in Acrobat and save).
- **Word** — same content, from the original `.docm` template Gary built;
  anchor-replacement preserves the exact design.
- **Excel** — rolling action log across all meetings for the portfolio.

### Pre-Meeting Agenda
- **PDF** + **Word** — same Castillo branding. Sections in order:
  Attendees → Agenda (fixed 8-row, time-scaled by 30/60-min toggle) →
  Deliverable Timelines (with editable schedule version) → Discussion Points
  (per discipline) → Previous Week Recap (auto-pulled from source meeting) →
  Schedule Change Log → Risks and Constraints → Required Decisions →
  Open Action Items (carry-forward) → Closing Remarks.

All files use the **Jost** font (loaded from Google Fonts in the UI,
embedded as TTF subsets in the PDF, fallback to Helvetica if Jost can't
be found on the host).

---

## Development workflow

### Run the app while you edit

```bash
streamlit run app/main.py
```

Streamlit auto-reloads `app/main.py` on save. For changes to other modules
(`db/`, `docgen/`, `llm/`, `storage/`, `schedule_parser/`, `app/services.py`),
there's a dev-mode `importlib.reload()` block at the top of `main.py` that
sidesteps Python's `sys.modules` cache — meaning new functions you add to
`db/repository.py` (etc.) show up without a server restart.

**Exception:** `db/models.py` is intentionally excluded from auto-reload —
re-declaring SQLAlchemy classes creates duplicate Base metadata and breaks
the existing session. **If you change a model, restart Streamlit.**

### Run the smoketest

```bash
python scripts/smoketest.py
```

Builds DB → seeds → saves a fake meeting → generates PDF/DOCX/XLSX. Exits
zero on success. Runs on every push via GitHub Actions too.

### Generate fully-populated test PDFs

```bash
python scripts/full_coverage_test.py
```

Creates a `Full Coverage Sample` portfolio, builds a meeting with all
sections populated, and produces both PDFs in `data/outputs/`. Useful for
checking layout regressions visually after a change.

### Run unit tests

```bash
pytest
```

### Database migrations

Schema changes go through Alembic (`migrations/`), targeting the same URL
`config.database_url()` would give the app (SQLite locally, Postgres in
prod — `migrations/env.py` calls it directly, so there's nothing to
configure per-environment).

```bash
# after changing db/models.py:
alembic revision --autogenerate -m "describe the change"
# review the generated migrations/versions/<rev>_describe_the_change.py,
# then apply it:
alembic upgrade head

# roll back the most recent migration:
alembic downgrade -1
```

`init_db()` still calls `Base.metadata.create_all()` on startup (so a
brand-new dev DB doesn't need a manual `alembic upgrade` first) and then
stamps `alembic_version` at head if it isn't stamped yet — this covers both
fresh databases and pre-Alembic ones that only ever went through
`create_all()`. `_bootstrap_migrations()` in `db/session.py` is the
pre-Alembic idempotent `ALTER TABLE` shim kept only to carry old databases
forward; don't add new columns there — write an Alembic migration instead.

---

## Key conventions

### Always use `session_scope()` for DB work

```python
from db import session_scope

with session_scope() as session:
    project = session.get(Project, project_id)
    # … work …
    # auto-commits on clean exit, auto-rolls-back on exception
```

Never instantiate a session directly outside of this helper.

### Never instantiate `OpenAI` directly

Use `from llm.providers import get_provider`. To add a new LLM backend,
subclass `LLMProvider` and implement `parse_meeting_notes`.

### Brand colors are in `config.BrandColors`

```python
from config import BrandColors
red = BrandColors.RED  # #ad1f2b
```

Never inline hex codes in docgen or UI code.

### Use `safe_filename_slug()` for output filenames

```python
from app.services import safe_filename_slug
fname = f"{safe_filename_slug(portfolio.name)}_Meeting_Minutes_{date}.pdf"
```

Strips/replaces filesystem-unsafe characters consistently.

### Action items have dual FKs to Meeting

`originating_meeting_id` (where the action was raised) and
`closed_in_meeting_id` (where it was marked completed/cancelled). The
SQLAlchemy relationships use explicit `foreign_keys=` arguments — don't
simplify this; the dual-FK is what enables the rolling action log.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ImportError: cannot import name '...'` after adding a function to `db.repository` | Streamlit's `sys.modules` cache. The dev-mode auto-reload at the top of `main.py` handles most cases, but if you add a new module entirely, restart Streamlit. |
| `OPENAI_API_KEY not set` | Edit `.env`. Smoketest doesn't need a real key but the env var must exist. |
| `NOT NULL constraint failed: schedules.project_id` when deleting a portfolio | Old DB without the cascade fix. Either drop `data/castillo.db` and re-seed, or run the script in `scripts/cleanup_phantom_agenda_drafts.py` to clear orphans. |
| `PermissionError: [Errno 13]` writing to `data/outputs/...` | You've got the PDF open in Acrobat — Windows locks the file. Close it. |
| Chrome won't render the PDF preview iframe | Expected. We rasterize via `pypdfium2` instead. If preview is blank, check `pypdfium2` is installed (`pip show pypdfium2`). |
| Jost font isn't rendering in the PDF | The TTFs must be installed (Windows: `C:\Windows\Fonts\Jost-Regular.ttf`). Falls back to Helvetica if not. |
| Streamlit says "value is not in options" on a selectbox | Session state holds a stale value. Switch the related parent (project/portfolio) once to reset, or clear `data/castillo.db` for a fresh start. |
| `UnicodeEncodeError` on Windows when running scripts | Some emoji/unicode characters in print statements; cp1252 default codec can't encode them. Replace with ASCII or set `PYTHONIOENCODING=utf-8`. |

---

## Phase status

| Phase | Status | What it includes |
|---|---|---|
| 1 — Local prototype | ✅ Done | DB, models, basic UI, doc generation |
| 2 — AI parsing + roster + status dropdowns | ✅ Done | OpenAI provider, persistent roster, inline editing |
| 3 — PDF AcroForm status dropdowns | ✅ Done | Clickable Status column in Acrobat; client can edit + save |
| 4 — Pre-meeting agenda full editor | ✅ Done | All sections editable, save/load, 30/60-min duration, schedule version override |
| Bonus — Notes planner | ✅ Done | Per-portfolio notes with sub-project picker, follow-up dates |
| Bonus — Actions tab CRUD | ✅ Done | Full add/edit/delete with filter + color-coded status |
| Bonus — URL routing | ✅ Done | `?tab=`, `?client=`, `?portfolio=`, `?agenda=`, `?meeting=` — refresh-stable + shareable |
| Bonus — Brand logo integration | ✅ Done | PMO 360 + Castillo logos in sidebar, PDFs, agenda DOCX |
| Bonus — CI smoketest | ✅ Done | GitHub Actions runs `scripts/smoketest.py` on every push |
| 5 — Multi-user infra | ⏳ Pending | PostgreSQL migration, Azure AD SSO, SharePoint backend (`SharePointBackend` is stub) |
| 6 — Polish + email | ⏳ Pending | Microsoft Graph `Mail.Send` integration, training materials |
| 7 — Schedule integration | 🟡 Partial | Parser is done (`schedule_parser/`); not yet wired into the deliverables picker on Review |

See [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) for the
phase-by-phase breakdown with rough durations.

---

## Deployment

For the Azure AD app registration, SharePoint setup, PostgreSQL provisioning,
and Streamlit hosting options (internal VM vs Azure App Service vs Streamlit
Cloud Enterprise), see [docs/PRODUCTION_SETUP.md](docs/PRODUCTION_SETUP.md).

Expected monthly cost in production (5 PMs):

- OpenAI API (~30 meetings × 5K tokens): **~$3–8/mo**
- Azure Database for PostgreSQL Flexible Server (B1ms): **~$15/mo**
- Hosting (Azure App Service B1): **~$55/mo**, or **$0** if on an internal VM
- M365 / SharePoint: **already paid** under existing Castillo licenses

**Total:** ~$75–80/mo (or ~$20/mo on an internal VM).

---

## Working with Claude Code / AI-assisted dev

`CLAUDE.md` at the repo root captures full project context — open this
folder in [Claude Code](https://claude.com/claude-code) and it'll auto-load.
See [docs/CLAUDE_CODE_PROMPTS.md](docs/CLAUDE_CODE_PROMPTS.md) for
ready-to-use prompts for each remaining phase.

---

## License

Proprietary. Copyright © 2026 Castillo Engineering. All rights reserved.
See [LICENSE](LICENSE) for full terms. Use restricted to Castillo Engineering
employees and authorized contractors.

Third-party open-source libraries (Streamlit, SQLAlchemy, ReportLab, OpenAI
SDK, python-docx, openpyxl, pikepdf, pdfplumber, pypdfium2, mammoth, msal,
requests, pydantic) are used under their respective licenses.

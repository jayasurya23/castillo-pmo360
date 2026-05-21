# PMO 360 — Castillo Engineering

Streamlit-based project management workspace for Castillo Engineering's PMs.
Captures meeting minutes (AI-assisted), drafts pre-meeting coordination
agendas, tracks rolling action items, and stores planner notes — all organized
by Client → Portfolio.

## Quick start

```bash
git clone https://github.com/jayasurya23/castillo-pmo360.git
cd castillo-pmo360
python -m venv venv
venv\Scripts\activate            # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — at minimum set OPENAI_API_KEY

streamlit run app/main.py
```

App opens at <http://localhost:8501> (or whichever port Streamlit picks).

## What's in it

| Tab | What it does |
|---|---|
| **📥 Capture** | Paste meeting notes, pick attendees, parse with OpenAI |
| **📝 Review** | Edit the parsed attendees / agenda / discussion / actions inline |
| **👁️ Preview** | Rasterized PDF preview before download |
| **📤 Send** | Generate final PDF + DOCX + XLSX action log |
| **📅 Next Agenda** | Build the pre-meeting coordination agenda — disciplines, recap, risks, decisions, schedule change log; save/load drafts |
| **✅ Actions** | Portfolio-wide rolling action log with full CRUD |
| **📓 Notes** | Per-portfolio planner notes with sub-project picker, follow-ups, priorities |
| **📚 History** | Saved meetings + saved agendas, open/export/delete each |
| **📊 Schedule** | Upload proposal PDF / duration .xlsx; parse into ScheduleItems |

## Architecture

```
app/                  Streamlit UI + workflow orchestration
db/                   SQLAlchemy models + repository + session helpers
docgen/               PDF / DOCX / XLSX generators with Castillo branding
llm/                  OpenAI provider (abstract, swap-friendly)
storage/              LocalFS + SharePoint backends
schedule_parser/      Proposal PDF + duration XLSX parsers
templates/            Original .docm templates from Gary
assets/logo/          PMO 360 + Castillo brand logos (PNG)
scripts/              Seed data, smoke tests, full-coverage fixture
data/                 SQLite DB + generated outputs (gitignored)
docs/                 Implementation plan, production setup
```

## Run modes

`.env`'s `LOCAL_DEV_MODE` flag switches storage backends:

- **`LOCAL_DEV_MODE=true`** (default) — SQLite + local FS. Zero cloud deps.
- **`LOCAL_DEV_MODE=false`** — Postgres + SharePoint via Microsoft Graph. See [docs/PRODUCTION_SETUP.md](docs/PRODUCTION_SETUP.md) for Azure AD app registration steps.

## Phase status

| Phase | Status |
|---|---|
| 1 — Local prototype, models, doc generation | ✅ Done |
| 2 — AI parsing + roster + status dropdowns | ✅ Done |
| 3 — PDF AcroForm status dropdowns | ✅ Done |
| 4 — Pre-meeting agenda with full editor | ✅ Done |
| Bonus — Notes planner | ✅ Done |
| Bonus — URL routing (deep links) | ✅ Done |
| 5 — Postgres + SharePoint + Azure AD SSO | ⏳ Pending |
| 6 — Microsoft Graph email + polish | ⏳ Pending |
| 7 — Schedule module integration | 🟡 Parser done; not yet wired into deliverables |

## Brand assets

The Castillo and PMO 360 logos in `assets/logo/` are Castillo Engineering's
property — included here for the internal tool. Jost font is loaded via
Google Fonts (Streamlit theme) and TTFs (ReportLab PDF generation).

## Working with Claude Code

`CLAUDE.md` at the root captures project context for AI-assisted development.
Open this repo in Claude Code and it'll auto-load.

## License

Proprietary — Castillo Engineering. Internal use only.

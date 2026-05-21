"""
Castillo Meeting Management — Streamlit app entrypoint.

Run: `streamlit run app/main.py`

This is the Phase 1-2 implementation:
  - Capture notes (paste/upload)
  - Parse with OpenAI
  - Review structured output
  - Generate PDF/Word/Excel client deliverables

Phases 3-7 are TODOs documented in docs/IMPLEMENTATION_PLAN.md and CLAUDE.md.
"""
import sys
import importlib
from pathlib import Path
from datetime import date, datetime

# Allow imports from project root when run via `streamlit run app/main.py`
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

from config import APP_TITLE, APP_TAGLINE, TOOL_NAME, BrandColors, is_local_dev, openai_model

# ============================================================
# Dev-mode module auto-reload
# ============================================================
# Streamlit's hot-reload re-executes this script on save but does NOT clear
# Python's sys.modules cache. That means new top-level names added to
# imported modules (e.g. a new function in db/repository.py) won't be visible
# even though Streamlit "reloaded" — `from x import new_name` still raises
# ImportError until the process is restarted.
#
# Fix: explicitly `importlib.reload()` our app-owned modules on every
# script run. SQLAlchemy declarative models (db.models) are intentionally
# excluded — reloading their classes would create duplicate Base metadata
# and break the existing ORM session state. Repository / service / docgen
# modules contain only function definitions and constants, so reloading
# them is safe.
if is_local_dev():
    _AUTORELOAD_MODULES = (
        "db.repository", "db.session",
        "app.services",
        "docgen", "docgen.document_builder", "docgen.pdf_builder",
        "docgen.template_filler", "docgen.action_log",
        "llm", "llm.providers",
        "storage", "storage.backend",
        "schedule_parser", "schedule_parser.parser",
    )
    for _name in _AUTORELOAD_MODULES:
        _mod = sys.modules.get(_name)
        if _mod is not None:
            try:
                importlib.reload(_mod)
            except Exception:
                # If a reload fails (rare — e.g. mid-edit syntax error), keep
                # the cached version rather than blowing up the whole app.
                pass
from db import init_db, session_scope
from db.models import Client, Project, Meeting, ActionItem, Schedule, ScheduleItem, GlobalAttendee, MeetingDeliverable
from db.repository import (
    list_clients, list_projects, get_project_roster,
    open_actions, latest_meeting, upsert_project_attendee,
)
from app.services import (
    parse_notes_with_ai, save_parsed_meeting, finalize_meeting,
    update_parsed_meeting, meeting_filename,
)
from llm.providers import (
    ParsedMeeting, ParsedAttendee, ParsedAgendaItem,
    ParsedDiscussionPoint, ParsedActionItem,
)
from schedule_parser import parse_schedule_file


# ============================================================
# Early helpers — used by the sidebar's New Project / New Person forms,
# so they have to be defined before any UI rendering code runs.
# ============================================================
def _make_initials(full_name: str) -> str:
    parts = [p for p in (full_name or "").strip().split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:2].upper()
    return "".join(p[0] for p in parts[:3]).upper()


def _parse_bulk_attendees(text: str) -> list[dict]:
    """Parse ``Org: Name (Init), Name (Init), …`` lines into a list of
    ``{full_name, initials, organization}`` dicts. Tolerant of extra
    whitespace, missing initials, and stray punctuation."""
    import re
    out: list[dict] = []
    if not text:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip().strip(",;")
        if not line or ":" not in line:
            continue
        org, rest = line.split(":", 1)
        org = org.strip()
        if not org or not rest.strip():
            continue
        # Each name token is "Full Name (II)" — split on commas that are not
        # inside parentheses.
        tokens = [t.strip() for t in re.split(r",(?![^()]*\))", rest) if t.strip()]
        for tok in tokens:
            m = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", tok)
            if m:
                name = m.group(1).strip()
                initials = m.group(2).strip().upper()
            else:
                name, initials = tok, ""
            if not name:
                continue
            initials = initials or _make_initials(name)
            out.append({
                "full_name": name, "initials": initials, "organization": org,
            })
    return out


# ============================================================
# Page setup
# ============================================================
st.set_page_config(
    page_title=f"{APP_TITLE} · {TOOL_NAME}",
    page_icon="🔴",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Castillo brand CSS — kept minimal. The Jost font is loaded via
# .streamlit/config.toml [theme] fontFaces so it's applied through Streamlit's
# native theme system instead of brute-force CSS that breaks icon fonts.
st.markdown(f"""
<style>
.stButton > button[kind="primary"] {{
  background-color: {BrandColors.RED} !important;
  border-color: {BrandColors.RED} !important;
  font-weight: 600;
}}
.stButton > button[kind="primary"]:hover {{
  background-color: {BrandColors.DARK_RED} !important;
}}
section[data-testid="stSidebar"] {{
  background-color: {BrandColors.NEAR_BLACK};
}}
/* Whitelist: text that sits directly on the dark sidebar background should
   be white. We DON'T blanket-color every descendant — the selectbox/text_input
   value display lives on a light input background, so forcing white there
   produces white-on-white.  Input control internals keep their theme colors. */
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
section[data-testid="stSidebar"] [data-testid="stWidgetLabel"],
section[data-testid="stSidebar"] details summary,
section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3,
section[data-testid="stSidebar"] h4,
section[data-testid="stSidebar"] h5,
section[data-testid="stSidebar"] h6 {{
  color: white !important;
}}
/* Form input text inside the sidebar — explicitly dark so it stays readable
   on the light input background regardless of system theme. */
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] textarea,
section[data-testid="stSidebar"] [data-baseweb="select"] *,
section[data-testid="stSidebar"] [role="combobox"],
section[data-testid="stSidebar"] [role="combobox"] * {{
  color: {BrandColors.NEAR_BLACK} !important;
}}
/* Sidebar buttons (form-submit + secondary) — the default theme renders the
   text in a low-contrast gray that's invisible on white. Streamlit puts the
   label inside a <p> / <span> child of the button, so we have to color both
   the button AND its descendants. */
section[data-testid="stSidebar"] .stButton > button,
section[data-testid="stSidebar"] .stFormSubmitButton > button,
section[data-testid="stSidebar"] button[kind="secondary"],
section[data-testid="stSidebar"] button[kind="secondaryFormSubmit"],
section[data-testid="stSidebar"] .stButton > button *,
section[data-testid="stSidebar"] .stFormSubmitButton > button *,
section[data-testid="stSidebar"] button[kind="secondary"] *,
section[data-testid="stSidebar"] button[kind="secondaryFormSubmit"] * {{
  color: {BrandColors.NEAR_BLACK} !important;
}}
section[data-testid="stSidebar"] .stButton > button,
section[data-testid="stSidebar"] .stFormSubmitButton > button,
section[data-testid="stSidebar"] button[kind="secondary"],
section[data-testid="stSidebar"] button[kind="secondaryFormSubmit"] {{
  background-color: white !important;
  border: 1px solid {BrandColors.NEAR_BLACK} !important;
  font-weight: 600 !important;
}}
section[data-testid="stSidebar"] .stButton > button:hover,
section[data-testid="stSidebar"] .stFormSubmitButton > button:hover,
section[data-testid="stSidebar"] button[kind="secondary"]:hover,
section[data-testid="stSidebar"] button[kind="secondaryFormSubmit"]:hover,
section[data-testid="stSidebar"] .stButton > button:hover *,
section[data-testid="stSidebar"] .stFormSubmitButton > button:hover *,
section[data-testid="stSidebar"] button[kind="secondary"]:hover *,
section[data-testid="stSidebar"] button[kind="secondaryFormSubmit"]:hover * {{
  background-color: {BrandColors.RED} !important;
  color: white !important;
  border-color: {BrandColors.RED} !important;
}}
/* Primary form-submit buttons in the sidebar — solid red with white text */
section[data-testid="stSidebar"] .stButton > button[kind="primary"],
section[data-testid="stSidebar"] .stFormSubmitButton > button[kind="primary"],
section[data-testid="stSidebar"] button[kind="primary"],
section[data-testid="stSidebar"] button[kind="primaryFormSubmit"],
section[data-testid="stSidebar"] .stButton > button[kind="primary"] *,
section[data-testid="stSidebar"] .stFormSubmitButton > button[kind="primary"] *,
section[data-testid="stSidebar"] button[kind="primary"] *,
section[data-testid="stSidebar"] button[kind="primaryFormSubmit"] * {{
  background-color: {BrandColors.RED} !important;
  color: white !important;
  border-color: {BrandColors.RED} !important;
}}
.brand-banner {{
  background: {BrandColors.RED};
  color: white;
  padding: 10px 16px;
  border-radius: 6px;
  margin-bottom: 14px;
  font-weight: 600;
}}
/* Status pills — color codes from Castillo secondary palette */
.status-open      {{ background:#1aa6c9; color:#fff;    padding:3px 10px; border-radius:11px; font-size:10px; font-weight:700; text-transform:uppercase; }}
.status-pending   {{ background:#c7bb2e; color:#fff;    padding:3px 10px; border-radius:11px; font-size:10px; font-weight:700; text-transform:uppercase; }}
.status-completed {{ background:#278747; color:#fff;    padding:3px 10px; border-radius:11px; font-size:10px; font-weight:700; text-transform:uppercase; }}
.status-cancelled {{ background:#e6e7e8; color:#1a1a1a; padding:3px 10px; border-radius:11px; font-size:10px; font-weight:700; text-transform:uppercase; }}

/* Status selectboxes — tinted by their current value via a JS observer
   below. Classes get attached to the [data-testid="stSelectbox"] container
   based on the displayed text. Targets the BaseWeb internal control divs. */
[data-testid="stSelectbox"].sb-status-open    [data-baseweb="select"] > div,
[data-testid="stSelectbox"].sb-status-open    div[role="combobox"] {{
  background-color: #1aa6c9 !important; color: white !important; font-weight: 600;
}}
[data-testid="stSelectbox"].sb-status-pending [data-baseweb="select"] > div,
[data-testid="stSelectbox"].sb-status-pending div[role="combobox"] {{
  background-color: #c7bb2e !important; color: white !important; font-weight: 600;
}}
[data-testid="stSelectbox"].sb-status-completed [data-baseweb="select"] > div,
[data-testid="stSelectbox"].sb-status-completed div[role="combobox"] {{
  background-color: #278747 !important; color: white !important; font-weight: 600;
}}
[data-testid="stSelectbox"].sb-status-cancelled [data-baseweb="select"] > div,
[data-testid="stSelectbox"].sb-status-cancelled div[role="combobox"] {{
  background-color: #e6e7e8 !important; color: #1a1a1a !important; font-weight: 600;
}}
/* Recolor the dropdown chevron icon to match the background */
[data-testid="stSelectbox"].sb-status-open svg,
[data-testid="stSelectbox"].sb-status-pending svg,
[data-testid="stSelectbox"].sb-status-completed svg {{
  fill: white !important; color: white !important;
}}
</style>
""", unsafe_allow_html=True)

# Make Tab insert 2 spaces inside textareas instead of moving focus. The script
# runs in a tiny iframe but reaches up to the parent document and installs a
# single delegated keydown listener (flag-guarded so reruns don't duplicate it).
#
# The second block runs an observer that watches every selectbox and applies
# a `sb-status-<value>` class to any whose displayed text is one of the four
# action statuses. The matching CSS up top then tints the dropdown background
# to mirror the PDF's status pills.
import streamlit.components.v1 as _components
_components.html(
    """
    <script>
    const doc = window.parent.document;
    if (!doc.__pmo360TabHandlerInstalled) {
        doc.__pmo360TabHandlerInstalled = true;
        doc.addEventListener('keydown', function(e) {
            if (e.key !== 'Tab') return;
            const ta = e.target;
            if (!ta || ta.tagName !== 'TEXTAREA') return;
            e.preventDefault();
            const start = ta.selectionStart;
            const end   = ta.selectionEnd;
            if (e.shiftKey) {
                // Shift+Tab: remove up to 2 leading spaces from the line(s) in selection.
                const before = ta.value.slice(0, start);
                const sel    = ta.value.slice(start, end);
                const after  = ta.value.slice(end);
                // Walk back to the start of the current line.
                const lineStart = before.lastIndexOf('\\n') + 1;
                const head = ta.value.slice(lineStart, start);
                const dropFromHead = Math.min(2, head.length - head.replace(/^ {1,2}/, '').length);
                if (dropFromHead > 0) {
                    ta.value = ta.value.slice(0, lineStart) + head.slice(dropFromHead) + sel + after;
                    ta.selectionStart = start - dropFromHead;
                    ta.selectionEnd   = end - dropFromHead;
                }
            } else {
                ta.value = ta.value.slice(0, start) + '  ' + ta.value.slice(end);
                ta.selectionStart = ta.selectionEnd = start + 2;
            }
            ta.dispatchEvent(new Event('input',  { bubbles: true }));
            ta.dispatchEvent(new Event('change', { bubbles: true }));
        }, true);
    }

    // Status-selectbox colorizer. Runs once on load, then watches the DOM
    // for mutations (Streamlit re-renders widgets on every rerun).
    const STATUS_VALUES = ['open', 'pending', 'completed', 'cancelled'];
    function paintStatusSelectboxes() {
        const boxes = doc.querySelectorAll('[data-testid="stSelectbox"]');
        boxes.forEach(box => {
            // The selected value sits inside the [data-baseweb="select"] div.
            const valueEl = box.querySelector('div[role="combobox"]')
                          || box.querySelector('[data-baseweb="select"] > div');
            if (!valueEl) return;
            const raw = (valueEl.innerText || valueEl.textContent || '').trim().toLowerCase();
            // Strip any decorative icon characters
            const clean = raw.replace(/[^a-z+ ]/g, '').trim();
            // Remove any prior status class
            STATUS_VALUES.forEach(s => box.classList.remove('sb-status-' + s));
            if (STATUS_VALUES.includes(clean)) {
                box.classList.add('sb-status-' + clean);
            }
        });
    }
    paintStatusSelectboxes();
    if (!doc.__pmo360StatusObserverInstalled) {
        doc.__pmo360StatusObserverInstalled = true;
        const obs = new MutationObserver(() => {
            // Throttle: requestAnimationFrame collapses bursts of mutations
            if (doc.__pmo360StatusPaintQueued) return;
            doc.__pmo360StatusPaintQueued = true;
            requestAnimationFrame(() => {
                doc.__pmo360StatusPaintQueued = false;
                paintStatusSelectboxes();
            });
        });
        obs.observe(doc.body, { childList: true, subtree: true, characterData: true });
    }
    </script>
    """,
    height=0,
)


# ============================================================
# DB init on first run
# ============================================================
@st.cache_resource
def _init():
    init_db()
    return True

_init()


# ============================================================
# URL routing — Stage 1: persist tab + client + portfolio in the URL so
# refresh restores the same view, and so you can bookmark/share a link
# straight to (say) the Actions tab on the Heelstone / Snapdragon view.
# Only NAVIGATIONAL state goes in the URL — editor state (typed text,
# unsaved actions, draft agendas) stays in session_state.
# ============================================================
import re as _re_url

TAB_TO_SLUG = {
    "📥 Capture":      "capture",
    "📝 Review":       "review",
    "👁️ Preview":      "preview",
    "📤 Send":         "send",
    "📅 Next Agenda":  "next-agenda",
    "✅ Actions":      "actions",
    "📓 Notes":        "notes",
    "📚 History":      "history",
    "📊 Schedule":     "schedule",
}
SLUG_TO_TAB = {v: k for k, v in TAB_TO_SLUG.items()}


def _url_slug(s: str) -> str:
    """Lowercase-hyphen slug used for client/portfolio names in the URL.
    Reversible via case-insensitive lookup against the live name list."""
    if not s:
        return ""
    out = s.strip().lower()
    out = _re_url.sub(r"\s+", "-", out)
    out = _re_url.sub(r"[^a-z0-9-]", "", out)
    out = _re_url.sub(r"-+", "-", out).strip("-")
    return out


def _find_name_by_slug(slug: str, names) -> "str | None":
    """Reverse-lookup: return the first name in ``names`` whose slug matches.
    Returns None when no match (caller falls back to first option)."""
    if not slug:
        return None
    target = slug.lower()
    for n in names:
        if _url_slug(n) == target:
            return n
    return None


# Read URL params ONCE per script run and seed session state. Sidebar
# selectboxes below use these as their initial values via `key=`.
_qp = st.query_params
_url_tab = _qp.get("tab", "")
_url_client = _qp.get("client", "")
_url_portfolio = _qp.get("portfolio", "")
_url_agenda = _qp.get("agenda", "")
_url_meeting = _qp.get("meeting", "")

if "nav" not in st.session_state and _url_tab in SLUG_TO_TAB:
    st.session_state.nav = SLUG_TO_TAB[_url_tab]


def _url_meeting_id_for_current_portfolio() -> "int | None":
    """If the URL has a valid ?meeting=<id> for the active portfolio, return
    its id. None otherwise (silently — caller falls back to whatever was
    already in session_state, or starts blank)."""
    raw = st.query_params.get("meeting", "")
    if not raw.isdigit():
        return None
    mid = int(raw)
    pid = globals().get("project_id")  # set by the sidebar block above
    if pid is None:
        return None
    with session_scope() as session:
        m = session.get(Meeting, mid)
        if m is None or m.project_id != pid:
            return None
    return mid


def _url_agenda_id_for_current_portfolio(agenda_choices) -> "int | None":
    """Same validation pattern for ?agenda=<id>. ``agenda_choices`` is the
    pre-fetched list of dicts the Next Agenda page already builds — saves
    a duplicate DB hit."""
    raw = st.query_params.get("agenda", "")
    if not raw.isdigit():
        return None
    aid = int(raw)
    valid_ids = {a["id"] for a in (agenda_choices or [])}
    return aid if aid in valid_ids else None


def _sync_url_params(desired: dict) -> None:
    """Update URL query params to match ``desired``, removing extras. No-op
    when already in sync (avoids the rerun loop that would otherwise come
    from blindly re-writing the same values on every render)."""
    current = dict(st.query_params)
    if current == desired:
        return
    # Drop any keys that should no longer be there
    for k in list(current.keys()):
        if k not in desired:
            del st.query_params[k]
    for k, v in desired.items():
        if current.get(k) != v:
            st.query_params[k] = v


# ============================================================
# Sidebar — project picker + navigation
# ============================================================
with st.sidebar:
    # Platform branding — PMO 360 logo on a white pill (the logo has black
    # text + red accent, so it needs a light background to read on the dark
    # sidebar). Falls back to the original text branding if the file is
    # missing. The tagline + tool-name pill stay below as before.
    import base64 as _b64
    _logo_dir = Path(__file__).resolve().parent.parent / "assets" / "logo"
    _pmo_logo = _logo_dir / "pmo360_logo.png"
    if _pmo_logo.exists():
        _pmo_b64 = _b64.b64encode(_pmo_logo.read_bytes()).decode()
        st.markdown(
            f'<div style="background:white;border-radius:6px;'
            f'padding:8px 12px;margin:4px 0 6px;text-align:center;">'
            f'<img src="data:image/png;base64,{_pmo_b64}" '
            f'style="max-width:100%;height:auto;max-height:46px;" '
            f'alt="PMO 360" />'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="margin:4px 0 2px;">'
            f'<span style="font-size:24px;font-weight:800;letter-spacing:-0.5px;color:white;">PMO </span>'
            f'<span style="font-size:24px;font-weight:800;letter-spacing:-0.5px;color:{BrandColors.RED};">360</span>'
            '<span style="font-size:11px;font-weight:700;color:white;vertical-align:super;">™</span>'
            '</div>',
            unsafe_allow_html=True,
        )
    st.markdown(
        f'<div style="background:{BrandColors.RED};color:white;padding:5px 10px;'
        'border-radius:5px;font-size:11px;font-weight:700;margin:8px 0 10px;'
        'display:flex;align-items:center;gap:6px;">'
        f'📝 <span>{TOOL_NAME}</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    if is_local_dev():
        st.markdown(
            '<div style="background:#c7bb2e;color:#1a1a1a;padding:3px 8px;'
            'border-radius:4px;font-size:10px;font-weight:700;display:inline-block;">'
            'LOCAL DEV MODE</div>',
            unsafe_allow_html=True,
        )
    st.divider()

    # Project picker
    with session_scope() as session:
        clients = list_clients(session)
        client_options = {c.name: c.id for c in clients}

    if client_options:
        _client_names = list(client_options.keys())
        # Seed from URL on first render of this session. After that, the
        # widget owns its own state via `key=`. If the stored name has been
        # deleted, silently fall back to the first available client.
        if "sidebar_client_name" not in st.session_state:
            _url_match = _find_name_by_slug(_url_client, _client_names)
            st.session_state.sidebar_client_name = _url_match or _client_names[0]
        elif st.session_state.sidebar_client_name not in _client_names:
            st.session_state.sidebar_client_name = _client_names[0]
        chosen_client = st.selectbox(
            "Client", _client_names, key="sidebar_client_name",
        )
        client_id = client_options[chosen_client]
    else:
        st.info("No clients yet — add one below.")
        client_id = None
        chosen_client = None

    if client_id:
        with session_scope() as session:
            projects = list_projects(session, client_id)
            proj_options = {p.name: p.id for p in projects}
        if proj_options:
            _portfolio_names = list(proj_options.keys())
            # Same seed-from-URL pattern, but the portfolio choice is scoped
            # to the chosen client — so if the URL portfolio doesn't belong
            # to this client (or was deleted), we drop back to the first.
            if "sidebar_portfolio_name" not in st.session_state:
                _url_match = _find_name_by_slug(_url_portfolio, _portfolio_names)
                st.session_state.sidebar_portfolio_name = _url_match or _portfolio_names[0]
            elif st.session_state.sidebar_portfolio_name not in _portfolio_names:
                st.session_state.sidebar_portfolio_name = _portfolio_names[0]
            chosen_proj = st.selectbox(
                "Portfolio", _portfolio_names, key="sidebar_portfolio_name",
            )
            project_id = proj_options[chosen_proj]
        else:
            st.info("No portfolios under this client — add one below.")
            project_id = None
            chosen_proj = None
    else:
        project_id = None
        chosen_proj = None

    # --- New client form ---
    with st.expander("➕ New client"):
        with st.form("new_client_form", clear_on_submit=True):
            nc_name = st.text_input("Client name", placeholder="e.g. Heelstone")
            nc_domain = st.text_input("Email domain (optional)", placeholder="heelstone.com")
            if st.form_submit_button("Create client"):
                name = nc_name.strip()
                if not name:
                    st.error("Name is required.")
                else:
                    with session_scope() as session:
                        existing = session.query(Client).filter_by(name=name).first()
                        if existing:
                            st.warning(f"Client '{name}' already exists.")
                        else:
                            session.add(Client(
                                name=name,
                                email_domain=nc_domain.strip() or None,
                            ))
                    st.success(f"Added client: {name}")
                    st.rerun()

    # --- New project form ---
    with st.expander("➕ New portfolio"):
        if not client_options:
            st.caption("Add a client first — a portfolio must belong to one.")
        else:
            with st.form("new_project_form", clear_on_submit=True):
                client_names = list(client_options.keys())
                # Default the parent-client dropdown to whatever's selected in the sidebar
                default_idx = client_names.index(chosen_client) if client_id else 0
                np_client_name = st.selectbox(
                    "Under client",
                    client_names,
                    index=default_idx,
                    help="The portfolio will be created under this client.",
                )
                np_client_id = client_options[np_client_name]
                np_name = st.text_input("Portfolio name", placeholder="e.g. Raven, Gonzo and Waxwing")
                np_scope = st.text_area(
                    "Scope (optional)", height=70,
                    placeholder="Electrical Design, Civil Design and Studies",
                )
                np_roster = st.text_area(
                    "Portfolio roster — attendees grouped by company (optional)",
                    height=130,
                    placeholder=(
                        "E Light Electric Services, Inc: Blake Ely (BE), Ricky Dzabic (RD)\n"
                        "Sunshare: Andrew Proctor (AP), Brian McKinney (BM)\n"
                        "Ampacity: Dylan Wraga (DW)"
                    ),
                    help=(
                        "Paste the people who normally attend meetings on this "
                        "portfolio, one organization per line in 'Org: Name (II), …' "
                        "format. They'll appear as portfolio roster chips on every "
                        "meeting's Capture page."
                    ),
                )
                if st.form_submit_button("Create portfolio"):
                    name = np_name.strip()
                    if not name:
                        st.error("Name is required.")
                    else:
                        roster_to_seed = _parse_bulk_attendees(np_roster)
                        with session_scope() as session:
                            existing = (session.query(Project)
                                        .filter_by(client_id=np_client_id, name=name)
                                        .first())
                            if existing:
                                st.warning(
                                    f"Portfolio '{name}' already exists under "
                                    f"{np_client_name}."
                                )
                            else:
                                proj = Project(
                                    client_id=np_client_id,
                                    name=name,
                                    scope=np_scope.strip() or None,
                                )
                                session.add(proj)
                                session.flush()
                                for p in roster_to_seed:
                                    upsert_project_attendee(
                                        session, project_id=proj.id,
                                        full_name=p["full_name"],
                                        initials=p["initials"],
                                        organization=p["organization"],
                                    )
                                roster_msg = (f" + {len(roster_to_seed)} roster entries"
                                              if roster_to_seed else "")
                                st.success(
                                    f"Added portfolio: {np_client_name} / {name}{roster_msg}"
                                )
                        st.rerun()

    # --- Delete portfolio (destructive — typed-name confirmation required) ---
    with st.expander("🗑️ Delete portfolio"):
        if not client_id or not project_id:
            st.caption("Pick a portfolio above to delete it.")
        else:
            with session_scope() as session:
                target = session.get(Project, project_id)
                if target is None:
                    st.caption("Portfolio not found.")
                else:
                    target_name = target.name
                    target_client = target.client.name if target.client else "?"
                    n_meetings = (session.query(Meeting)
                                  .filter_by(project_id=project_id).count())
                    n_schedules = (session.query(Schedule)
                                   .filter_by(project_id=project_id).count())
                    n_actions = (session.query(ActionItem)
                                 .filter_by(project_id=project_id).count())
            st.warning(
                f"Permanently deletes **{target_client} / {target_name}** "
                "and everything beneath it:"
            )
            st.markdown(
                f"- **{n_meetings}** meeting(s) — attendees, agenda, discussion, "
                f"action items\n"
                f"- **{n_schedules}** uploaded schedule(s)\n"
                f"- **{n_actions}** action item(s) in the rolling log\n"
                f"- Portfolio-specific saved roster"
            )
            confirm = st.text_input(
                f"Type the portfolio name to confirm",
                placeholder=target_name,
                key="delete_project_confirm",
            )
            disabled = (confirm != target_name)
            if st.button(
                "🗑️ Permanently delete portfolio",
                type="primary",
                disabled=disabled,
                use_container_width=True,
                key="delete_project_btn",
            ):
                try:
                    with session_scope() as session:
                        # Schedules (+ items via cascade) aren't on the Project's
                        # relationship cascade list — wipe explicitly first.
                        for sched in (session.query(Schedule)
                                      .filter_by(project_id=project_id).all()):
                            session.delete(sched)
                        proj = session.get(Project, project_id)
                        if proj is not None:
                            session.delete(proj)
                    # Clear any session state that points at this project
                    st.session_state.draft_meeting_id = None
                    st.session_state.parsed = None
                    st.session_state.selected_attendees = []
                    for k in ("meeting_minutes_text", "agenda_items_text",
                              "action_items_text", "meeting_title"):
                        st.session_state[k] = ""
                    st.toast(f"Deleted {target_client} / {target_name}", icon="🗑️")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Delete failed: {exc}")
            elif disabled and confirm:
                st.caption(
                    f"Name didn't match — type **{target_name}** exactly to enable the button."
                )

    # Sidebar no longer hosts the workflow navigator — that lives in the top
    # tab strip in the main area now. Sidebar keeps the project picker, the
    # New-client / New-project forms, project deletion, and the platform branding.


# ============================================================
# Session state for wizard flow + navigation
# ============================================================
NAV_ITEMS = [
    "📥 Capture", "📝 Review", "👁️ Preview", "📤 Send",
    "📅 Next Agenda", "✅ Actions", "📓 Notes", "📚 History", "📊 Schedule",
]

if "nav" not in st.session_state:
    st.session_state.nav = NAV_ITEMS[0]
if "parsed" not in st.session_state:
    st.session_state.parsed = None
if "draft_meeting_id" not in st.session_state:
    st.session_state.draft_meeting_id = None
if "meeting_minutes_text" not in st.session_state:
    st.session_state.meeting_minutes_text = ""
if "agenda_items_text" not in st.session_state:
    st.session_state.agenda_items_text = ""
if "action_items_text" not in st.session_state:
    st.session_state.action_items_text = ""
if "meeting_title" not in st.session_state:
    st.session_state.meeting_title = ""
# Attendees selected on the Capture page (list of dicts with full_name/initials/organization)
if "selected_attendees" not in st.session_state:
    st.session_state.selected_attendees = []
# Deliverable Timelines rows picked on the Review page — each entry is a
# dict {project_segment, task, start_status, delivery_date (date or None)}.
# Persisted as MeetingDeliverable rows on save.
if "selected_deliverables" not in st.session_state:
    st.session_state.selected_deliverables = []


# ============================================================
# Top tab strip — replaces the sidebar radio
# ============================================================
def _goto(name: str):
    """Switch the active tab and trigger a clean rerun."""
    st.session_state.nav = name
    st.rerun()


def _render_top_tabs():
    """Render the workflow tabs as a button row at the top of the main area."""
    # Inject a small CSS tweak so the inactive tab buttons feel like tabs,
    # not blocky buttons. Active = solid red (primary); inactive = white pill.
    st.markdown(
        """
        <style>
        div[data-testid="stHorizontalBlock"] .stButton > button {
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            padding: 6px 4px;
            white-space: nowrap;
            min-height: 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    cols = st.columns(len(NAV_ITEMS))
    for i, name in enumerate(NAV_ITEMS):
        is_active = (st.session_state.nav == name)
        with cols[i]:
            if st.button(
                name,
                key=f"tab_{i}",
                use_container_width=True,
                type="primary" if is_active else "secondary",
            ):
                if not is_active:
                    _goto(name)


_render_top_tabs()


# ============================================================
# Helpers
# ============================================================
def _resolve_dp(top_list, path):
    """Walk `path` (list of indices) through discussion-point tree, return the node."""
    node = top_list[path[0]]
    for idx in path[1:]:
        node = node.sub_points[idx]
    return node


def _resolve_dp_parent_and_index(top_list, path):
    """Return (parent_list_containing_target, last_index)."""
    if len(path) == 1:
        return top_list, path[0]
    parent = top_list[path[0]]
    for idx in path[1:-1]:
        parent = parent.sub_points[idx]
    return parent.sub_points, path[-1]


def _move_dp(top_list, path, delta):
    parent_list, i = _resolve_dp_parent_and_index(top_list, path)
    j = i + delta
    if 0 <= j < len(parent_list):
        parent_list[i], parent_list[j] = parent_list[j], parent_list[i]


def _delete_dp(top_list, path):
    parent_list, i = _resolve_dp_parent_and_index(top_list, path)
    if 0 <= i < len(parent_list):
        parent_list.pop(i)


# (card-background, initials-circle, border) — used to tint selected-attendee chips by company.
# Castillo Engineering is locked to the brand red; everyone else cycles through the rest.
COMPANY_PALETTE = [
    ("#fce8ea", "#ad1f2b", "#ad1f2b"),   # Castillo red (reserved for Castillo Engineering)
    ("#fff7e6", "#c7bb2e", "#c7bb2e"),   # gold
    ("#e6f0fa", "#185fa5", "#185fa5"),   # blue
    ("#e7f5ed", "#278747", "#278747"),   # green
    ("#f0e6f5", "#7c4ba2", "#7c4ba2"),   # purple
    ("#e1f4f4", "#1f8a8a", "#1f8a8a"),   # teal
    ("#f5e6dc", "#a85420", "#a85420"),   # burnt orange
]
_CASTILLO_ORG = "Castillo Engineering"


def _color_for_org(org: str, ordered_orgs: list[str]) -> tuple[str, str, str]:
    """Pick a stable palette slot for `org`. Castillo always gets slot 0; the
    remaining orgs cycle through slots 1.. in first-seen order."""
    if org == _CASTILLO_ORG:
        return COMPANY_PALETTE[0]
    if org not in ordered_orgs:
        ordered_orgs.append(org)
    idx = (ordered_orgs.index(org) % (len(COMPANY_PALETTE) - 1)) + 1
    return COMPANY_PALETTE[idx]


def _name_tokens(full_name: str) -> tuple[str, str]:
    """Return (first_name, last_name) lowercased, both empty if blank."""
    parts = [p for p in (full_name or "").strip().split() if p]
    if not parts:
        return ("", "")
    first = parts[0].lower()
    last = parts[-1].lower() if len(parts) > 1 else ""
    return (first, last)


def _attendee_already_in_meeting(parsed_att, selected_list) -> bool:
    """Match a ParsedAttendee against the canonical selected_attendees list.
    A match is found when the parsed attendee shares initials, exact full name,
    first name, OR last name with any selected attendee."""
    p_init = (parsed_att.initials or "").strip().upper()
    p_name = (parsed_att.full_name or "").strip().lower()
    p_first, p_last = _name_tokens(parsed_att.full_name or "")

    if not p_init and not p_name:
        return True  # empty entry — drop it

    for s in selected_list:
        s_init = (s.get("initials") or "").strip().upper()
        s_name = (s.get("full_name") or "").strip().lower()
        s_first, s_last = _name_tokens(s.get("full_name") or "")
        if p_init and s_init and p_init == s_init:
            return True
        if p_name and s_name and p_name == s_name:
            return True
        # Name overlap (first or last) — covers "Roashaael" vs "Roashaael Mary John"
        if p_first and (p_first == s_first or p_first == s_last):
            return True
        if p_last and (p_last == s_last or p_last == s_first):
            return True
    return False


def _agenda_to_text(items) -> str:
    """Serialize agenda items to one bulleted line each."""
    lines = []
    for it in items or []:
        text = (it.text or "").strip()
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


def _text_to_agenda(text: str):
    """Parse one-line-per-item agenda text. Bullet markers and indent are
    stripped (the AgendaItem model is flat, so nesting isn't preserved)."""
    if not text:
        return []
    out = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        body = raw.replace("\t", "  ").lstrip(" ")
        for prefix in ("- ", "* ", "• ", "○ ", "● ", "o "):
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        body = body.strip()
        if body:
            out.append(ParsedAgendaItem(text=body, discipline="General"))
    return out


def _dps_to_text(dps, level: int = 0) -> str:
    """Serialize a discussion-points tree to indented text.
    2 spaces per nesting level, `- ` bullet, `Label: content` body."""
    if not dps:
        return ""
    out = []
    indent = "  " * level
    for dp in dps:
        label = (dp.label or "").strip()
        content = (dp.content or "").strip()
        body = f"{label}: {content}" if label else content
        out.append(f"{indent}- {body}")
        kids = getattr(dp, "sub_points", None) or []
        if kids:
            child_str = _dps_to_text(kids, level + 1)
            if child_str:
                out.append(child_str)
    return "\n".join(filter(None, out))


def _text_to_dps(text: str):
    """Parse indented bullet text into a discussion-points tree.

    Rules:
      - Each non-blank line is one point.
      - Leading whitespace determines nesting: 2 spaces (or 1 tab) = +1 level.
      - Lines may start with `- `, `* `, `• `, `○ `, `● `, or `o ` (stripped).
      - `Label: content` is split on the first colon (within the first 80 chars).
    """
    if not text:
        return []
    items: list[tuple[int, "ParsedDiscussionPoint"]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        normalized = raw.replace("\t", "  ")
        leading = len(normalized) - len(normalized.lstrip(" "))
        level = leading // 2
        body = normalized.lstrip(" ")
        for prefix in ("- ", "* ", "• ", "○ ", "● ", "o "):
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        if ":" in body[:80]:
            label, _, content = body.partition(":")
            label, content = label.strip(), content.strip()
        else:
            label, content = "", body.strip()
        items.append((level, ParsedDiscussionPoint(
            label=label, content=content, discipline="General",
        )))

    roots = []
    parent_stack: list[tuple[int, "ParsedDiscussionPoint"]] = []
    for level, dp in items:
        while parent_stack and parent_stack[-1][0] >= level:
            parent_stack.pop()
        if not parent_stack:
            roots.append(dp)
        else:
            parent_stack[-1][1].sub_points.append(dp)
        parent_stack.append((level, dp))
    return roots


# ============================================================
# Next-Agenda helpers — discussion-points tree + table renderers
# ============================================================
def _text_to_dps_with_discipline(text: str, discipline: str):
    """Same as `_text_to_dps` but stamps every parsed point with the given
    discipline (so the docx generator can bucket them correctly)."""
    tree = _text_to_dps(text)
    def _walk(nodes):
        for n in nodes:
            n.discipline = discipline
            if n.sub_points:
                _walk(n.sub_points)
    _walk(tree)
    return tree


def _count_dps(dps) -> int:
    """Total node count including nested sub-points."""
    if not dps:
        return 0
    total = 0
    for dp in dps:
        total += 1
        total += _count_dps(getattr(dp, "sub_points", None) or [])
    return total


def _render_dp_preview(dps, level: int = 0) -> None:
    """Indented bullet preview of a discussion-points tree."""
    indent = "&nbsp;" * (level * 4)
    marker = "●" if level == 0 else "○"
    for dp in dps:
        label = f"<b>{dp.label}:</b> " if dp.label else ""
        st.markdown(
            f"<div style='font-size:13px;color:#1a1a1a;line-height:1.5;'>"
            f"{indent}{marker} {label}{dp.content}</div>",
            unsafe_allow_html=True,
        )
        kids = getattr(dp, "sub_points", None) or []
        if kids:
            _render_dp_preview(kids, level + 1)


def _dp_snapshot(dp) -> dict:
    """Snapshot a DB DiscussionPoint (+ its sub_points) into a plain dict
    that can be used outside the SQLAlchemy session."""
    return {
        "label": dp.label or "",
        "content": dp.content or "",
        "sub_points": [
            _dp_snapshot(s) for s in (dp.sub_points or [])
        ],
    }


def _recap_seed_to_text(points, level: int = 0) -> str:
    """Serialize a list of `_dp_snapshot` dicts into the indented-bullet text
    format used by the per-discipline textareas."""
    if not points:
        return ""
    lines = []
    indent = "  " * level
    for p in points:
        label = (p.get("label") or "").strip()
        content = (p.get("content") or "").strip()
        body = f"{label}: {content}" if label else content
        lines.append(f"{indent}- {body}")
        kids = p.get("sub_points") or []
        if kids:
            child = _recap_seed_to_text(kids, level + 1)
            if child:
                lines.append(child)
    return "\n".join(filter(None, lines))


def _iso_to_date(s: str | None):
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


class _DraftAction:
    """Duck-typed stand-in for ActionItem so docgen can render carry-forward
    actions edited in the UI without round-tripping through the DB."""
    __slots__ = ("text", "owner", "due_date", "status")
    def __init__(self, text, owner, due_date, status):
        self.text = text
        self.owner = owner
        self.due_date = due_date
        self.status = status


def _na_row_border_css(key: str) -> None:
    """Subtle column separators for the inline table — same look the Review
    page uses for action items."""
    st.markdown(
        f"""
        <style>
        .{key}-row-wrap > div[data-testid="stHorizontalBlock"] {{
            border-bottom: 1px solid #bcbec0;
            padding: 4px 0;
            align-items: start;
        }}
        .{key}-row-wrap > div[data-testid="stHorizontalBlock"]:last-child {{
            border-bottom: none;
        }}
        .{key}-header > div[data-testid="stHorizontalBlock"] {{
            border-bottom: 2px solid #ad1f2b !important;
            padding-bottom: 6px;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _na_table_header(weights: list[float], labels: list[str], key: str) -> None:
    st.markdown(f'<div class="{key}-header">', unsafe_allow_html=True)
    h = st.columns(weights)
    for col, label in zip(h, labels):
        col.markdown(
            "<div style='font-size:11px;font-weight:700;"
            "text-transform:uppercase;letter-spacing:0.5px;color:#1a1a1a;'>"
            f"{label}</div>",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)


def _render_action_table(rows: list[dict], owner_options: list[str],
                         key_prefix: str) -> None:
    """Render rows of {text, owner, due_date (iso), status} as an inline
    editable table. Mutates rows in place."""
    STATUS_CHOICES = ["Open", "Pending", "Completed", "Cancelled"]
    weights = [0.4, 4.5, 3.0, 1.4, 1.4, 0.5]
    labels = ["#", "Action", "Owner(s)", "Due", "Status", ""]
    _na_row_border_css(key_prefix)
    _na_table_header(weights, labels, key_prefix)

    st.markdown(f'<div class="{key_prefix}-row-wrap">', unsafe_allow_html=True)
    delete_idx = None
    for idx, r in enumerate(rows):
        c = st.columns(weights)
        c[0].markdown(
            f"<div style='padding-top:18px;color:#4d4d4f;font-weight:700;'>"
            f"{idx + 1}</div>",
            unsafe_allow_html=True,
        )
        r["text"] = c[1].text_area(
            "Action", value=r.get("text") or "", height=68,
            key=f"{key_prefix}_text_{idx}",
            label_visibility="collapsed",
            placeholder="What needs to happen?",
        )
        existing_parts = [p.strip() for p in (r.get("owner") or "").split(",")
                          if p.strip()]
        option_set = list(dict.fromkeys(list(owner_options) + existing_parts))
        picked = c[2].multiselect(
            "Owners", options=option_set, default=existing_parts,
            key=f"{key_prefix}_own_{idx}",
            label_visibility="collapsed",
            placeholder=("Pick one or more…" if option_set else "Type name"),
        )
        r["owner"] = ", ".join(picked)
        due_val = c[3].date_input(
            "Due", value=_iso_to_date(r.get("due_date")),
            key=f"{key_prefix}_due_{idx}",
            label_visibility="collapsed",
            format="MM/DD/YYYY",
        )
        r["due_date"] = due_val.isoformat() if due_val else None
        try:
            st_idx = STATUS_CHOICES.index(
                (r.get("status") or "Open").strip().capitalize()
            )
        except ValueError:
            st_idx = 0
        r["status"] = c[4].selectbox(
            "Status", STATUS_CHOICES, index=st_idx,
            key=f"{key_prefix}_st_{idx}",
            label_visibility="collapsed",
        )
        if c[5].button("✕", key=f"{key_prefix}_del_{idx}",
                       help="Drop from agenda",
                       use_container_width=True):
            delete_idx = idx
    st.markdown("</div>", unsafe_allow_html=True)
    if delete_idx is not None:
        rows.pop(delete_idx)
        st.rerun()
    if not rows:
        st.caption("No open actions on this portfolio right now.")


def _render_action_preview(rows: list[dict]) -> None:
    rows_html = ""
    for i, r in enumerate(rows, start=1):
        status = (r.get("status") or "open").lower()
        if status not in ("open", "pending", "completed", "cancelled"):
            status = "open"
        due_str = ""
        d = _iso_to_date(r.get("due_date"))
        if d:
            try:
                due_str = d.strftime("%#m/%#d/%Y")
            except ValueError:
                due_str = d.strftime("%-m/%-d/%Y")
        rows_html += (
            "<tr>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;"
            f"text-align:center;'>{i}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>"
            f"{(r.get('text') or '')}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>"
            f"{(r.get('owner') or '')}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;"
            f"white-space:nowrap;'>{due_str}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>"
            f"<span class='status-{status}'>{status.capitalize()}</span></td>"
            "</tr>"
        )
    st.markdown(
        "<table style='width:100%;border-collapse:collapse;font-size:12px;'>"
        "<thead><tr>"
        "<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>#</th>"
        "<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>Action</th>"
        "<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>Owner(s)</th>"
        "<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>Due</th>"
        "<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>Status</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody></table>",
        unsafe_allow_html=True,
    )


def _render_risk_table(rows: list[dict], owner_options: list[str],
                       key_prefix: str) -> None:
    weights = [0.4, 3.0, 2.0, 1.4, 3.0, 1.6, 0.5]
    labels = ["#", "Description", "Impact", "Likelihood",
              "Mitigation Strategy", "Owner", ""]
    _na_row_border_css(key_prefix)
    _na_table_header(weights, labels, key_prefix)
    st.markdown(f'<div class="{key_prefix}-row-wrap">', unsafe_allow_html=True)
    delete_idx = None
    for idx, r in enumerate(rows):
        c = st.columns(weights)
        c[0].markdown(
            f"<div style='padding-top:18px;color:#4d4d4f;font-weight:700;'>"
            f"{idx + 1}</div>",
            unsafe_allow_html=True,
        )
        r["description"] = c[1].text_area(
            "Description", value=r.get("description") or "", height=68,
            key=f"{key_prefix}_desc_{idx}",
            label_visibility="collapsed",
            placeholder="What's the risk or constraint?",
        )
        r["impact"] = c[2].text_area(
            "Impact", value=r.get("impact") or "", height=68,
            key=f"{key_prefix}_imp_{idx}",
            label_visibility="collapsed",
            placeholder="Schedule / cost / scope effect",
        )
        r["likelihood"] = c[3].text_input(
            "Likelihood", value=r.get("likelihood") or "",
            key=f"{key_prefix}_lh_{idx}",
            label_visibility="collapsed",
            placeholder="Low / Med / High",
        )
        r["mitigation"] = c[4].text_area(
            "Mitigation", value=r.get("mitigation") or "", height=68,
            key=f"{key_prefix}_mit_{idx}",
            label_visibility="collapsed",
            placeholder="Plan to reduce/avoid",
        )
        existing_parts = [p.strip()
                          for p in (r.get("owner") or "").split(",")
                          if p.strip()]
        option_set = list(dict.fromkeys(list(owner_options) + existing_parts))
        picked = c[5].multiselect(
            "Owner", options=option_set, default=existing_parts,
            key=f"{key_prefix}_own_{idx}",
            label_visibility="collapsed",
            placeholder=("Pick…" if option_set else "Name"),
        )
        r["owner"] = ", ".join(picked)
        if c[6].button("✕", key=f"{key_prefix}_del_{idx}",
                       help="Drop row", use_container_width=True):
            delete_idx = idx
    st.markdown("</div>", unsafe_allow_html=True)
    if delete_idx is not None:
        rows.pop(delete_idx)
        st.rerun()
    if not rows:
        st.caption("No risks added yet.")


def _render_risk_preview(rows: list[dict]) -> None:
    body = ""
    for i, r in enumerate(rows, start=1):
        body += (
            "<tr>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;text-align:center;'>{i}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('description') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('impact') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('likelihood') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('mitigation') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('owner') or ''}</td>"
            "</tr>"
        )
    head_cells = ("#", "Description", "Impact", "Likelihood",
                  "Mitigation Strategy", "Owner")
    head_html = "".join(
        f"<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>{h}</th>"
        for h in head_cells
    )
    st.markdown(
        "<table style='width:100%;border-collapse:collapse;font-size:12px;'>"
        f"<thead><tr>{head_html}</tr></thead>"
        f"<tbody>{body}</tbody></table>",
        unsafe_allow_html=True,
    )


def _render_decision_table(rows: list[dict], owner_options: list[str],
                           key_prefix: str) -> None:
    weights = [0.4, 2.5, 3.0, 2.5, 1.4, 1.6, 0.5]
    labels = ["#", "Decision", "Description", "Impact if Not Decided",
              "Required By", "Decision Owner", ""]
    _na_row_border_css(key_prefix)
    _na_table_header(weights, labels, key_prefix)
    st.markdown(f'<div class="{key_prefix}-row-wrap">', unsafe_allow_html=True)
    delete_idx = None
    for idx, r in enumerate(rows):
        c = st.columns(weights)
        c[0].markdown(
            f"<div style='padding-top:18px;color:#4d4d4f;font-weight:700;'>"
            f"{idx + 1}</div>",
            unsafe_allow_html=True,
        )
        r["decision"] = c[1].text_area(
            "Decision", value=r.get("decision") or "", height=68,
            key=f"{key_prefix}_d_{idx}",
            label_visibility="collapsed",
            placeholder="Short name of the decision",
        )
        r["description"] = c[2].text_area(
            "Description", value=r.get("description") or "", height=68,
            key=f"{key_prefix}_desc_{idx}",
            label_visibility="collapsed",
            placeholder="What needs to be decided",
        )
        r["impact_if_not"] = c[3].text_area(
            "Impact if not", value=r.get("impact_if_not") or "", height=68,
            key=f"{key_prefix}_imp_{idx}",
            label_visibility="collapsed",
            placeholder="What happens if this slips",
        )
        rb_val = r.get("required_by")
        if isinstance(rb_val, str):
            rb_val = _iso_to_date(rb_val)
        rb_picked = c[4].date_input(
            "Required by", value=rb_val,
            key=f"{key_prefix}_rb_{idx}",
            label_visibility="collapsed",
            format="MM/DD/YYYY",
        )
        r["required_by"] = rb_picked
        existing_parts = [p.strip()
                          for p in (r.get("owner") or "").split(",")
                          if p.strip()]
        option_set = list(dict.fromkeys(list(owner_options) + existing_parts))
        picked = c[5].multiselect(
            "Owner", options=option_set, default=existing_parts,
            key=f"{key_prefix}_own_{idx}",
            label_visibility="collapsed",
            placeholder=("Pick…" if option_set else "Name"),
        )
        r["owner"] = ", ".join(picked)
        if c[6].button("✕", key=f"{key_prefix}_del_{idx}",
                       help="Drop row", use_container_width=True):
            delete_idx = idx
    st.markdown("</div>", unsafe_allow_html=True)
    if delete_idx is not None:
        rows.pop(delete_idx)
        st.rerun()
    if not rows:
        st.caption("No decisions added yet.")


def _render_decision_preview(rows: list[dict]) -> None:
    body = ""
    for i, r in enumerate(rows, start=1):
        rb = r.get("required_by")
        rb_str = ""
        if rb:
            try:
                rb_str = rb.strftime("%#m/%#d/%Y")
            except (AttributeError, ValueError):
                try:
                    rb_str = rb.strftime("%-m/%-d/%Y")
                except Exception:
                    rb_str = str(rb)
        body += (
            "<tr>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;text-align:center;'>{i}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('decision') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('description') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('impact_if_not') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;white-space:nowrap;'>{rb_str}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('owner') or ''}</td>"
            "</tr>"
        )
    head_cells = ("#", "Decision", "Description", "Impact if Not Decided",
                  "Required By", "Decision Owner")
    head_html = "".join(
        f"<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>{h}</th>"
        for h in head_cells
    )
    st.markdown(
        "<table style='width:100%;border-collapse:collapse;font-size:12px;'>"
        f"<thead><tr>{head_html}</tr></thead>"
        f"<tbody>{body}</tbody></table>",
        unsafe_allow_html=True,
    )


def _render_schedule_change_table(rows: list[dict], key_prefix: str) -> None:
    """Inline editable table for Schedule Change Log rows. Date columns use
    date_input widgets (date | None); the generator converts to display
    strings (MM/DD/YYYY) at render time."""
    weights = [0.4, 1.4, 2.0, 1.3, 1.3, 2.5, 2.0, 2.5, 0.5]
    labels = ["#", "Project", "Task / Milestone", "Previous Date",
              "Updated Date", "Change Description", "Reason for Change",
              "Impact on Overall Schedule", ""]
    _na_row_border_css(key_prefix)
    _na_table_header(weights, labels, key_prefix)
    st.markdown(f'<div class="{key_prefix}-row-wrap">', unsafe_allow_html=True)
    delete_idx = None
    for idx, r in enumerate(rows):
        c = st.columns(weights)
        c[0].markdown(
            f"<div style='padding-top:18px;color:#4d4d4f;font-weight:700;'>"
            f"{idx + 1}</div>",
            unsafe_allow_html=True,
        )
        r["project"] = c[1].text_input(
            "Project", value=r.get("project") or "",
            key=f"{key_prefix}_proj_{idx}",
            label_visibility="collapsed",
            placeholder="Sub-project",
        )
        r["task"] = c[2].text_input(
            "Task", value=r.get("task") or "",
            key=f"{key_prefix}_task_{idx}",
            label_visibility="collapsed",
            placeholder="Task or milestone",
        )
        # Date columns — coerce ISO strings back to date for the widget
        prev_val = r.get("previous_date")
        if isinstance(prev_val, str):
            prev_val = _iso_to_date(prev_val)
        prev_picked = c[3].date_input(
            "Previous", value=prev_val,
            key=f"{key_prefix}_prev_{idx}",
            label_visibility="collapsed",
            format="MM/DD/YYYY",
        )
        r["previous_date"] = prev_picked
        upd_val = r.get("updated_date")
        if isinstance(upd_val, str):
            upd_val = _iso_to_date(upd_val)
        upd_picked = c[4].date_input(
            "Updated", value=upd_val,
            key=f"{key_prefix}_upd_{idx}",
            label_visibility="collapsed",
            format="MM/DD/YYYY",
        )
        r["updated_date"] = upd_picked
        r["change_description"] = c[5].text_area(
            "Change", value=r.get("change_description") or "", height=68,
            key=f"{key_prefix}_chg_{idx}",
            label_visibility="collapsed",
            placeholder="What changed",
        )
        r["reason_for_change"] = c[6].text_area(
            "Reason", value=r.get("reason_for_change") or "", height=68,
            key=f"{key_prefix}_rsn_{idx}",
            label_visibility="collapsed",
            placeholder="Why it changed",
        )
        r["impact"] = c[7].text_area(
            "Impact", value=r.get("impact") or "", height=68,
            key=f"{key_prefix}_imp_{idx}",
            label_visibility="collapsed",
            placeholder="Effect on overall schedule",
        )
        if c[8].button("✕", key=f"{key_prefix}_del_{idx}",
                       help="Drop row", use_container_width=True):
            delete_idx = idx
    st.markdown("</div>", unsafe_allow_html=True)
    if delete_idx is not None:
        rows.pop(delete_idx)
        st.rerun()
    if not rows:
        st.caption("No schedule changes recorded yet.")


def _render_schedule_change_preview(rows: list[dict]) -> None:
    body = ""
    for i, r in enumerate(rows, start=1):
        def _fmt_d(v):
            if v is None or v == "":
                return ""
            if isinstance(v, str):
                d = _iso_to_date(v)
            else:
                d = v
            if d is None:
                return str(v)
            try:
                return d.strftime("%#m/%#d/%Y")
            except (ValueError, AttributeError):
                try:
                    return d.strftime("%-m/%-d/%Y")
                except Exception:
                    return str(v)
        body += (
            "<tr>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;text-align:center;'>{i}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('project') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('task') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;white-space:nowrap;'>{_fmt_d(r.get('previous_date'))}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;white-space:nowrap;'>{_fmt_d(r.get('updated_date'))}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('change_description') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('reason_for_change') or ''}</td>"
            f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{r.get('impact') or ''}</td>"
            "</tr>"
        )
    head_cells = ("#", "Project", "Task / Milestone", "Previous Date",
                  "Updated Date", "Change Description", "Reason for Change",
                  "Impact on Overall Schedule")
    head_html = "".join(
        f"<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;font-size:11px;'>{h}</th>"
        for h in head_cells
    )
    st.markdown(
        "<table style='width:100%;border-collapse:collapse;font-size:12px;'>"
        f"<thead><tr>{head_html}</tr></thead>"
        f"<tbody>{body}</tbody></table>",
        unsafe_allow_html=True,
    )


def _na_wipe_editor_keys() -> None:
    """Drop every Next-Agenda editor state key, preserving only the bits
    used to identify the project / which agenda is loaded. Called whenever
    we switch agendas (so the picker can lazily re-seed from the new one)."""
    PRESERVE = {
        "na_loaded_for_project",
        "na_agenda_id",
        # Don't clear the picker widget's own key — Streamlit re-renders it
        # and a missing key resets it.
        "na_agenda_picker",
    }
    for k in list(st.session_state.keys()):
        if k.startswith("na_") and k not in PRESERVE:
            del st.session_state[k]


def _na_load_from_agenda_row(agenda) -> None:
    """Populate session_state from a saved Agenda row. Run inside a session_scope
    so the JSON columns are accessible. Decision dates come back as ISO
    strings — coerce to date objects so the date_input widgets accept them."""
    if agenda is None:
        return
    st.session_state.na_agenda_id = agenda.id
    st.session_state.na_agenda_title = agenda.title or ""
    st.session_state.na_upcoming_date = agenda.upcoming_date
    st.session_state.na_meeting_duration = (
        getattr(agenda, "meeting_duration_minutes", None) or 30
    )
    st.session_state.na_schedule_version = (
        getattr(agenda, "schedule_version_override", None) or ""
    )
    st.session_state.na_source_meeting_id = agenda.source_meeting_id
    st.session_state.na_disciplines = list(agenda.disciplines_json or
                                           ["Civil", "Electrical",
                                            "Structural", "General"])
    # Re-seed per-discipline textareas
    for disc, text in (agenda.dp_text_json or {}).items():
        st.session_state[f"na_dp_text__{disc}"] = text
    for disc, text in (agenda.recap_text_json or {}).items():
        st.session_state[f"na_recap_text__{disc}"] = text
    # Mark recap as already seeded so the source-meeting fallback doesn't
    # overwrite the saved text.
    st.session_state.na_recap_seeded = True

    st.session_state.na_attendees = [
        dict(p) for p in (agenda.attendees_json or [])
    ]
    st.session_state.na_open_actions = [
        dict(p) for p in (agenda.open_actions_json or [])
    ]
    st.session_state.na_risks = [
        dict(p) for p in (agenda.risks_json or [])
    ]
    # Decisions: required_by was stored as ISO string — coerce back
    decisions = []
    for d in (agenda.decisions_json or []):
        d = dict(d)
        rb = d.get("required_by")
        if isinstance(rb, str):
            try:
                d["required_by"] = date.fromisoformat(rb)
            except (TypeError, ValueError):
                d["required_by"] = None
        decisions.append(d)
    st.session_state.na_decisions = decisions
    # Schedule changes: previous_date / updated_date stored as ISO strings
    sched_changes = []
    for r in (getattr(agenda, "schedule_changes_json", None) or []):
        r = dict(r)
        for k in ("previous_date", "updated_date"):
            v = r.get(k)
            if isinstance(v, str):
                try:
                    r[k] = date.fromisoformat(v)
                except (TypeError, ValueError):
                    r[k] = None
        sched_changes.append(r)
    st.session_state.na_schedule_changes = sched_changes


def _na_collect_state_for_save() -> dict:
    """Build the kwargs payload to hand to save_agenda(). Decision dates
    serialize to ISO strings; attendee/risk/action dicts pass through as-is."""
    disciplines = list(st.session_state.get("na_disciplines") or [])
    dp_text = {
        d: st.session_state.get(f"na_dp_text__{d}", "")
        for d in disciplines
    }
    recap_text = {
        d: st.session_state.get(f"na_recap_text__{d}", "")
        for d in disciplines
    }
    decisions_out = []
    for d in (st.session_state.get("na_decisions") or []):
        d = dict(d)
        rb = d.get("required_by")
        if rb and not isinstance(rb, str):
            try:
                d["required_by"] = rb.isoformat()
            except AttributeError:
                d["required_by"] = str(rb)
        decisions_out.append(d)
    # Schedule changes: serialize date objects to ISO for JSON storage
    sched_changes_out = []
    for r in (st.session_state.get("na_schedule_changes") or []):
        r = dict(r)
        for k in ("previous_date", "updated_date"):
            v = r.get(k)
            if v and not isinstance(v, str):
                try:
                    r[k] = v.isoformat()
                except AttributeError:
                    r[k] = str(v)
        sched_changes_out.append(r)
    return {
        "upcoming_date": st.session_state.get("na_upcoming_date") or date.today(),
        "source_meeting_id": st.session_state.get("na_source_meeting_id"),
        "title": (st.session_state.get("na_agenda_title") or "").strip() or None,
        "meeting_duration_minutes": int(
            st.session_state.get("na_meeting_duration") or 30
        ),
        "schedule_version_override": (
            st.session_state.get("na_schedule_version") or ""
        ).strip() or None,
        "disciplines": disciplines,
        "dp_text": dp_text,
        "recap_text": recap_text,
        "attendees": list(st.session_state.get("na_attendees") or []),
        "open_actions": list(st.session_state.get("na_open_actions") or []),
        "risks": list(st.session_state.get("na_risks") or []),
        "decisions": decisions_out,
        "schedule_changes": sched_changes_out,
    }


def _render_attendee_table(rows: list[dict], key_prefix: str) -> None:
    """Render rows of {full_name, initials, organization} as an inline
    editable table. Mutates rows in place. Blank `initials` are auto-filled
    on edit by re-deriving from the full name."""
    weights = [0.4, 3.0, 1.0, 3.0, 0.5]
    labels = ["#", "Full name", "Initials", "Organization", ""]
    _na_row_border_css(key_prefix)
    _na_table_header(weights, labels, key_prefix)
    st.markdown(f'<div class="{key_prefix}-row-wrap">', unsafe_allow_html=True)
    delete_idx = None
    for idx, r in enumerate(rows):
        c = st.columns(weights)
        c[0].markdown(
            f"<div style='padding-top:14px;color:#4d4d4f;font-weight:700;'>"
            f"{idx + 1}</div>",
            unsafe_allow_html=True,
        )
        new_name = c[1].text_input(
            "Full name", value=r.get("full_name") or "",
            key=f"{key_prefix}_name_{idx}",
            label_visibility="collapsed",
            placeholder="First Last",
        )
        # Auto-derive initials when the field is empty and the name has changed
        prev_name = r.get("full_name") or ""
        r["full_name"] = new_name
        new_initials = c[2].text_input(
            "Initials",
            value=(r.get("initials") or "").upper()
                  or (_make_initials(new_name) if new_name and not r.get("initials") else ""),
            key=f"{key_prefix}_init_{idx}",
            label_visibility="collapsed",
            placeholder="ABC",
            max_chars=6,
        )
        # If the name changed and the user hadn't manually set initials, refresh
        if (
            new_name != prev_name
            and (new_initials.upper() == _make_initials(prev_name).upper()
                 or not new_initials.strip())
            and new_name.strip()
        ):
            new_initials = _make_initials(new_name)
        r["initials"] = new_initials.strip().upper()
        r["organization"] = c[3].text_input(
            "Organization",
            value=r.get("organization") or "",
            key=f"{key_prefix}_org_{idx}",
            label_visibility="collapsed",
            placeholder="Castillo Engineering / Client / Vendor / …",
        )
        if c[4].button("✕", key=f"{key_prefix}_del_{idx}",
                       help="Remove from attendees",
                       use_container_width=True):
            delete_idx = idx
    st.markdown("</div>", unsafe_allow_html=True)
    if delete_idx is not None:
        rows.pop(delete_idx)
        st.rerun()
    if not rows:
        st.caption("No attendees yet — click ➕ Add attendee to start.")


def _render_attendee_preview(rows: list[dict]) -> None:
    """Group attendees by organization and render as colored chips, matching
    the Capture-page styling."""
    by_org: dict[str, list[dict]] = {}
    for r in rows:
        name = (r.get("full_name") or "").strip()
        if not name:
            continue
        by_org.setdefault((r.get("organization") or "Other").strip() or "Other",
                          []).append(r)
    if not by_org:
        st.caption("(no attendees with a name yet)")
        return
    org_order: list[str] = []
    html_parts: list[str] = []
    for org, atts in by_org.items():
        bg, badge, border = _color_for_org(org, org_order)
        chips = "".join(
            f"<span style='display:inline-block;background:{bg};"
            f"border:1px solid {border};border-radius:14px;"
            f"padding:3px 10px;margin:3px 4px 3px 0;font-size:12px;"
            f"color:{badge};'>"
            f"{a.get('full_name','')} "
            f"<span style='opacity:0.6;font-size:10px;'>({a.get('initials','')})</span>"
            f"</span>"
            for a in atts
        )
        html_parts.append(
            f"<div style='margin:6px 0;'>"
            f"<div style='font-size:11px;font-weight:700;color:#4d4d4f;"
            f"text-transform:uppercase;letter-spacing:0.5px;margin-bottom:2px;'>"
            f"{org}</div>"
            f"{chips}"
            f"</div>"
        )
    st.markdown("".join(html_parts), unsafe_allow_html=True)


def _split_raw_notes(raw: str) -> tuple[str, str, str]:
    """Reverse of `_save_draft`'s combined raw_notes. Returns (minutes, agenda, actions)."""
    import re as _re
    if not raw:
        return ("", "", "")
    pat_min = _re.search(r"=== MEETING MINUTES ===\s*\n(.*?)(?=\n===\s|\Z)", raw, _re.S)
    pat_ag = _re.search(r"=== AGENDA ===\s*\n(.*?)(?=\n===\s|\Z)", raw, _re.S)
    pat_ac = _re.search(r"=== ACTION ITEMS ===\s*\n(.*?)\Z", raw, _re.S)
    return (
        (pat_min.group(1).strip() if pat_min else ""),
        (pat_ag.group(1).strip() if pat_ag else ""),
        (pat_ac.group(1).strip() if pat_ac else ""),
    )


def _load_meeting_into_session(meeting_id: int) -> bool:
    """Hydrate session state from an existing Meeting row so the user can
    edit it via the same Capture/Review/Preview/Send flow. Returns True on
    success, False if the meeting can't be found."""
    with session_scope() as session:
        m = session.get(Meeting, meeting_id)
        if not m:
            return False

        # Selected attendees
        st.session_state.selected_attendees = [
            {
                "full_name": a.full_name,
                "initials": a.initials,
                "organization": a.organization or "",
            }
            for a in m.attendees
        ]

        # Selected deliverables
        st.session_state.selected_deliverables = [
            {
                "project_segment": md.deliverable.project_segment or "",
                "task": md.deliverable.task,
                "start_status": md.deliverable.start_status or "In Progress",
                "delivery_date": md.deliverable.delivery_date,
            }
            for md in m.meeting_deliverables
        ]

        # Discussion-point tree
        def _build_dp_tree(dp):
            kids = sorted(dp.sub_points, key=lambda x: x.order_index)
            return ParsedDiscussionPoint(
                label=dp.label or "",
                content=dp.content or "",
                discipline=dp.discipline or "General",
                sub_points=[_build_dp_tree(c) for c in kids],
            )

        top_level = sorted(
            [dp for dp in m.discussion_points if dp.parent_id is None],
            key=lambda x: x.order_index,
        )

        parsed = ParsedMeeting(
            attendees=[
                ParsedAttendee(
                    full_name=a.full_name, initials=a.initials,
                    organization=a.organization or "",
                )
                for a in m.attendees
            ],
            agenda_items=[
                ParsedAgendaItem(
                    text=ai.text, discipline=ai.discipline or "General",
                )
                for ai in sorted(m.agenda_items, key=lambda x: x.order_index)
            ],
            discussion_points=[_build_dp_tree(dp) for dp in top_level],
            action_items=[
                ParsedActionItem(
                    text=ai.text or "",
                    owner=ai.owner or "",
                    due_date=ai.due_date.isoformat() if ai.due_date else None,
                    status=ai.status or "open",
                )
                for ai in sorted(m.raised_actions, key=lambda x: x.created_at)
            ],
        )

        st.session_state.parsed = parsed
        st.session_state.draft_meeting_id = m.id
        st.session_state.meeting_title = m.title or ""
        st.session_state.meeting_date = m.meeting_date

        minutes, agenda, actions = _split_raw_notes(m.raw_notes or "")
        st.session_state.meeting_minutes_text = minutes
        st.session_state.agenda_items_text = agenda
        st.session_state.action_items_text = actions
        # Force the discussion-points + agenda textareas to reseed from the loaded meeting
        st.session_state.pop("dp_editor_text", None)
        st.session_state.pop("agenda_editor_text", None)

        return True


def _consume_url_meeting_if_present() -> None:
    """If the URL has ?meeting=<id> for a meeting in the active portfolio AND
    it differs from what's currently loaded, hydrate session state from it.
    Call at the top of meeting-flow pages (Capture/Review/Preview/Send) so a
    bookmarked deep-link like ?tab=preview&meeting=42 works end-to-end."""
    target = _url_meeting_id_for_current_portfolio()
    if target is None:
        return
    if st.session_state.get("draft_meeting_id") == target:
        return
    if _load_meeting_into_session(target):
        st.rerun()


def _reset_session_for_new_meeting():
    """Clear all wizard state so the user starts a fresh meeting."""
    st.session_state.parsed = None
    st.session_state.draft_meeting_id = None
    st.session_state.meeting_title = ""
    st.session_state.meeting_minutes_text = ""
    st.session_state.agenda_items_text = ""
    st.session_state.action_items_text = ""
    st.session_state.selected_attendees = []
    st.session_state.selected_deliverables = []
    for k in ("meeting_date", "dp_editor_text", "agenda_editor_text"):
        if k in st.session_state:
            del st.session_state[k]


# `_make_initials` and `_parse_bulk_attendees` were moved to the top of this
# module so the sidebar (which uses them in the New Project form) can call
# them before they would otherwise be defined.


def _render_steps(active: int):
    labels = ["Capture", "Review", "Preview", "Send"]
    html = '<div style="display:flex;align-items:center;gap:10px;background:#f0eee6;padding:13px 20px;border-radius:6px;margin:10px 0 16px;flex-wrap:wrap;">'
    for i, label in enumerate(labels, start=1):
        if i < active:
            bg, txt, label_color = "#278747", "✓", "#1a1a1a"
        elif i == active:
            bg, txt, label_color = BrandColors.RED, str(i), BrandColors.RED
        else:
            bg, txt, label_color = "#4d4d4f", str(i), "#1a1a1a"
        html += (
            f'<div style="display:flex;align-items:center;gap:8px;font-size:12px;color:{label_color};'
            f'font-weight:{700 if i == active else 500};">'
            f'<span style="width:26px;height:26px;border-radius:50%;background:{bg};color:white;'
            'display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:12px;">'
            f'{txt}</span>{label}</div>'
        )
        if i < len(labels):
            html += '<div style="flex:1;height:2px;background:#bcbec0;min-width:30px;"></div>'
    html += '</div>'
    st.markdown(html, unsafe_allow_html=True)


def _render_method_cards():
    methods = [
        ("📋", "Paste / type notes", "Quickest. Drop in your bullets, transcript, or rough scribbles.", True),
        ("⬆", "Upload file", ".txt, .docx, .md, or a Teams / Zoom transcript file.", False),
    ]
    cols = st.columns(2)
    for i, (icon, title, sub, active) in enumerate(methods):
        bg = "#fffafa" if active else "white"
        border = f"2.5px solid {BrandColors.RED}" if active else "1.5px solid #bcbec0"
        cols[i].markdown(
            f'<div style="background:{bg};border:{border};border-radius:8px;padding:14px 16px;min-height:118px;">'
            f'<div style="width:36px;height:36px;background:{BrandColors.RED};color:white;border-radius:7px;'
            f'display:flex;align-items:center;justify-content:center;font-size:18px;margin-bottom:9px;">{icon}</div>'
            f'<div style="font-size:14px;font-weight:700;color:#1a1a1a;margin-bottom:4px;">{title}</div>'
            f'<div style="font-size:12px;color:#1a1a1a;font-weight:500;line-height:1.45;">{sub}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )


# ============================================================
# Routes
# ============================================================
def render_capture():
    if not project_id:
        st.warning("Pick a client and portfolio in the sidebar to start.")
        return
    _consume_url_meeting_if_present()

    # --- "Currently editing" banner (when a draft is loaded) ---
    if st.session_state.draft_meeting_id:
        edit_a, edit_b = st.columns([4, 1])
        with edit_a:
            st.info(
                f"📂 Editing **meeting #{st.session_state.draft_meeting_id}**. "
                "Saving on Review will update this meeting in place."
            )
        with edit_b:
            if st.button("➕ Start fresh", use_container_width=True, key="cap_reset"):
                _reset_session_for_new_meeting()
                st.rerun()

    # --- Resolve project context for the header subtitle + roster fetch ---
    with session_scope() as session:
        project = session.get(Project, project_id)
        client_name = project.client.name if project and project.client else ""
        project_name = project.name if project else ""
        roster_rows = get_project_roster(session, project_id)
        roster_data = [
            {"id": p.id, "full_name": p.full_name, "initials": p.initials,
             "organization": p.organization or ""}
            for p in roster_rows
        ]
        global_rows = session.query(GlobalAttendee).order_by(GlobalAttendee.full_name).all()
        global_roster = [
            {"id": -g.id, "full_name": g.full_name, "initials": g.initials,
             "organization": g.organization or ""}
            for g in global_rows
        ]
        # Filter out global members already in the project roster so they
        # don't appear twice.
        project_keys = {(p["full_name"], p["initials"]) for p in roster_data}
        global_roster = [g for g in global_roster
                         if (g["full_name"], g["initials"]) not in project_keys]

    today_str = date.today().strftime("%B %d, %Y")

    # --- Panel header ---
    st.markdown(
        f'<div style="background:white;border:1px solid #bcbec0;border-radius:8px;'
        'padding:14px 20px;margin-bottom:4px;">'
        '<div style="font-size:17px;font-weight:700;color:#1a1a1a;margin-bottom:3px;">'
        'New meeting · Capture notes</div>'
        f'<div style="font-size:12px;color:#1a1a1a;font-weight:500;">'
        f'{client_name} · {project_name} · {today_str}</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    # --- Step indicator ---
    _render_steps(active=1)

    # --- Meeting name + date ---
    col_title, col_date = st.columns([3, 1])
    with col_title:
        st.session_state.meeting_title = st.text_input(
            "Meeting name",
            value=st.session_state.meeting_title,
            placeholder=f"e.g. Weekly coordination — {project_name}",
        )
    with col_date:
        meeting_date = st.date_input("Meeting date", value=date.today())

    # --- Attendees (top of page — finish this first so AI parsing has the roster) ---
    st.markdown("---")
    sel_count = len(st.session_state.selected_attendees)
    st.markdown(
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">'
        f'<span style="font-size:15px;font-weight:700;color:#1a1a1a;">👥 Attendees</span>'
        f'<span style="background:{BrandColors.RED};color:white;padding:2px 9px;'
        f'border-radius:10px;font-size:11px;font-weight:700;">{sel_count}</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    selected_keys = {(a["full_name"], a["initials"]) for a in st.session_state.selected_attendees}

    def _render_chip_group(roster_list, key_prefix: str):
        if not roster_list:
            return
        chip_cols = st.columns(4)
        for i, p in enumerate(roster_list):
            already = (p["full_name"], p["initials"]) in selected_keys
            label = f"{'✓' if already else '+'} {p['full_name']} ({p['initials']})"
            with chip_cols[i % 4]:
                if st.button(label, key=f"{key_prefix}_{p['id']}",
                             disabled=already, use_container_width=True):
                    st.session_state.selected_attendees.append({
                        "full_name": p["full_name"],
                        "initials": p["initials"],
                        "organization": p["organization"],
                    })
                    st.rerun()

    # Company team (global roster) — always shown
    if global_roster:
        st.markdown(
            f'<div style="background:#fce8ea;border:2px solid {BrandColors.RED};'
            'border-radius:6px;padding:10px 13px;font-size:12px;color:#1a1a1a;'
            'margin-bottom:8px;font-weight:500;">'
            f'<b>🏢 Castillo Engineering team</b> — {len(global_roster)} people in '
            'the company roster. Always available on every portfolio.'
            '</div>',
            unsafe_allow_html=True,
        )
        _render_chip_group(global_roster, key_prefix="grost")

    # Project-specific roster — people picked up from prior meetings on this project
    if roster_data:
        st.markdown(
            f'<div style="background:#fff7e6;border:2px solid {BrandColors.GOLD};'
            'border-radius:6px;padding:10px 13px;font-size:12px;color:#1a1a1a;'
            'margin:12px 0 8px;font-weight:500;">'
            f'<b>📁 Portfolio roster</b> — {len(roster_data)} people saved from '
            'prior meetings on this portfolio.'
            '</div>',
            unsafe_allow_html=True,
        )
        _render_chip_group(roster_data, key_prefix="prost")
    elif not global_roster:
        st.info("No saved roster yet — add the first attendee below.")

    # Collect every distinct organization we've seen so the New-person and
    # Bulk-import forms can offer them as a dropdown.
    known_orgs = sorted({
        p["organization"] for p in (global_roster + roster_data)
        if p["organization"]
    })
    ORG_NEW_SENTINEL = "+ New organization…"

    with st.expander("➕ New person (not on roster)"):
        with st.form("new_attendee_form", clear_on_submit=True):
            new_name = st.text_input("Full name")
            nc1, nc2 = st.columns([1, 2])
            with nc1:
                new_init = st.text_input("Initials", placeholder="auto from name")
            with nc2:
                if known_orgs:
                    new_org_choice = st.selectbox(
                        "Organization",
                        options=known_orgs + [ORG_NEW_SENTINEL],
                        index=0,
                        help="Pick an existing org or choose 'New organization' to type one.",
                    )
                    new_org_typed = ""
                    if new_org_choice == ORG_NEW_SENTINEL:
                        new_org_typed = st.text_input(
                            "New organization name", placeholder="e.g. E Light Electric Services, Inc",
                        )
                else:
                    new_org_choice = ORG_NEW_SENTINEL
                    new_org_typed = st.text_input(
                        "Organization", placeholder="e.g. Castillo Engineering",
                    )
            save_to_global = st.checkbox(
                "Also add to the company-wide roster",
                value=False,
                help="If checked, this person becomes visible on every portfolio, not just this one.",
            )
            submitted = st.form_submit_button("Add to meeting & save to roster")
        if submitted:
            name = new_name.strip()
            org = (new_org_typed.strip() if new_org_choice == ORG_NEW_SENTINEL
                   else new_org_choice)
            if not name:
                st.error("Name is required.")
            else:
                initials = (new_init.strip().upper() or _make_initials(name))
                with session_scope() as session:
                    upsert_project_attendee(
                        session, project_id=project_id,
                        full_name=name, initials=initials, organization=org,
                    )
                    if save_to_global:
                        existing = session.query(GlobalAttendee).filter_by(full_name=name).first()
                        if not existing:
                            session.add(GlobalAttendee(
                                full_name=name, initials=initials, organization=org,
                            ))
                if not any(
                    a["full_name"] == name and a["initials"] == initials
                    for a in st.session_state.selected_attendees
                ):
                    st.session_state.selected_attendees.append({
                        "full_name": name, "initials": initials, "organization": org,
                    })
                st.rerun()

    with st.expander("📋 Bulk import attendees (paste 'Org: Name (Init), …')"):
        st.caption(
            "Paste one line per organization. Format: `Org Name: Full Name (II), "
            "Full Name (II), …`. Initials are optional — auto-derived from the "
            "name when missing."
        )
        with st.form("bulk_attendee_form", clear_on_submit=True):
            bulk_text = st.text_area(
                "Roster lines",
                height=140,
                placeholder=(
                    "E Light Electric Services, Inc: Blake Ely (BE), Ricky Dzabic (RD), Mark Jordan (MJ)\n"
                    "Sunshare: Andrew Proctor (AP), Brian McKinney (BM), Jason Rossi (JR)\n"
                    "Ampacity: Dylan Wraga (DW)"
                ),
            )
            bulk_save_global = st.checkbox(
                "Also save every imported person to the company-wide roster",
                value=False,
            )
            bulk_submit = st.form_submit_button("Import all")
        if bulk_submit:
            people = _parse_bulk_attendees(bulk_text)
            if not people:
                st.error("No valid lines found. Use `Org: Name (Init), Name (Init)…`.")
            else:
                added = 0
                with session_scope() as session:
                    existing_keys = {(a["full_name"], a["initials"])
                                     for a in st.session_state.selected_attendees}
                    for p in people:
                        upsert_project_attendee(
                            session, project_id=project_id,
                            full_name=p["full_name"], initials=p["initials"],
                            organization=p["organization"],
                        )
                        if bulk_save_global:
                            if not session.query(GlobalAttendee).filter_by(
                                full_name=p["full_name"]
                            ).first():
                                session.add(GlobalAttendee(
                                    full_name=p["full_name"], initials=p["initials"],
                                    organization=p["organization"],
                                ))
                        key = (p["full_name"], p["initials"])
                        if key not in existing_keys:
                            st.session_state.selected_attendees.append(p)
                            existing_keys.add(key)
                            added += 1
                st.toast(f"Imported {len(people)} people · added {added} to this meeting.",
                         icon="📋")
                st.rerun()

    # Selected attendees grid — each chip tinted by company
    if st.session_state.selected_attendees:
        st.caption(f"In this meeting · {sel_count} people")
        # Walk the orgs in first-seen order so each company gets a stable color
        org_order: list[str] = []
        for a in st.session_state.selected_attendees:
            org = a["organization"] or "Other"
            if org != _CASTILLO_ORG and org not in org_order:
                org_order.append(org)
        per_row = 3
        for row_start in range(0, sel_count, per_row):
            row_cols = st.columns(per_row)
            for c, a in enumerate(st.session_state.selected_attendees[row_start:row_start + per_row]):
                with row_cols[c]:
                    info_col, x_col = st.columns([6, 1])
                    org = a["organization"] or "Other"
                    bg, badge, border = _color_for_org(org, org_order)
                    info_col.markdown(
                        f'<div style="background:{bg};border:1px solid {border};'
                        'border-radius:6px;padding:8px 10px;font-size:12px;'
                        f'display:flex;align-items:center;gap:9px;color:#1a1a1a;">'
                        f'<span style="background:{badge};color:white;border-radius:50%;'
                        'width:26px;height:26px;display:inline-flex;align-items:center;'
                        f'justify-content:center;font-weight:700;font-size:10px;flex-shrink:0;">'
                        f'{a["initials"]}</span>'
                        f'<span style="color:#1a1a1a;"><b>{a["full_name"]}</b><br>'
                        f'<span style="font-size:11px;color:#4d4d4f;">{org}</span>'
                        '</span></div>',
                        unsafe_allow_html=True,
                    )
                    if x_col.button("✕", key=f"rm_att_{row_start + c}"):
                        st.session_state.selected_attendees.pop(row_start + c)
                        st.rerun()
    else:
        st.caption("No attendees selected yet.")

    # --- Method picker ---
    st.markdown(
        '<p style="font-size:11px;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.6px;color:#1a1a1a;margin:14px 0 8px;">'
        '① Choose how you want to capture the meeting</p>',
        unsafe_allow_html=True,
    )
    _render_method_cards()

    # --- Two notes boxes ---
    st.markdown(
        '<p style="font-size:11px;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.6px;color:#1a1a1a;margin:18px 0 6px;">'
        '② Your notes</p>',
        unsafe_allow_html=True,
    )
    # Agenda on top (short — usually just bullet points)
    st.markdown("**Agenda**  \n*Topics covered / to discuss*")
    st.session_state.agenda_items_text = st.text_area(
        "Agenda",
        value=st.session_state.agenda_items_text,
        height=140,
        label_visibility="collapsed",
        placeholder=(
            "List agenda topics one per line.\n\n"
            "Example: Due Diligence · Folder Structure · General Concerns"
        ),
    )

    # Meeting minutes + Action items side by side
    notes_col, actions_col = st.columns(2)
    with notes_col:
        st.markdown("**Meeting minutes**  \n*Attendees & discussion points*")
        st.session_state.meeting_minutes_text = st.text_area(
            "Meeting minutes",
            value=st.session_state.meeting_minutes_text,
            height=340,
            label_visibility="collapsed",
            placeholder=(
                "Attendees and discussion notes. Don't worry about formatting "
                "— the AI sorts these into the right buckets.\n\n"
                "Example:\n\n"
                "Attendees: AR, RC from Castillo, CM from Heelstone\n\n"
                "Electrical:\n"
                "- HDR pushing 0% soil moisture, causing thermal failures\n"
                "- We want 52% load factor, IE wants 60%\n"
            ),
        )
    with actions_col:
        st.markdown("**Action items**  \n*Owners, due dates, status*")
        st.session_state.action_items_text = st.text_area(
            "Action items",
            value=st.session_state.action_items_text,
            height=340,
            label_visibility="collapsed",
            placeholder=(
                "List action items one per line. Include owner initials and "
                "a due date when known.\n\n"
                "Example:\n\n"
                "- CK, KC to set up call with HDR IE by 11/10 — open\n"
                "- KC to resend Heelstone tech specs by 11/10 — completed\n"
            ),
        )

    # --- AI assist callout ---
    st.markdown(
        f'<div style="background:#fdeac0;border:1.5px solid {BrandColors.GOLD};'
        'border-radius:7px;padding:12px 14px;margin:14px 0;">'
        f'<div style="font-size:13px;font-weight:700;color:#1a1a1a;margin-bottom:2px;">'
        f'✨ AI assist · OpenAI {openai_model()}</div>'
        '<div style="font-size:11.5px;color:#1a1a1a;font-weight:500;line-height:1.5;">'
        'Extracts attendees, agenda topics, discussion points, and action items '
        'into the structured form. You review everything before saving.</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    # --- Upload zone ---
    st.markdown(
        '<p style="font-size:11px;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.6px;color:#1a1a1a;margin:14px 0 6px;">'
        '③ Or upload a transcript file</p>',
        unsafe_allow_html=True,
    )
    uploaded = st.file_uploader(
        "Drop a file here or click to browse",
        type=["txt", "md", "docx", "vtt"],
        help="Plain text, markdown, Word, or Teams/Zoom transcript (.vtt). Goes into Meeting minutes.",
        label_visibility="collapsed",
    )
    if uploaded:
        try:
            content = uploaded.read().decode("utf-8", errors="ignore")
            st.session_state.meeting_minutes_text = content
            st.success(f"Loaded {uploaded.name} ({len(content)} chars) into Meeting minutes")
        except Exception as exc:
            st.error(f"Could not read file: {exc}")

    # --- Footer ---
    st.markdown("---")
    summary_col, btn_col_skip, btn_col_parse = st.columns([3, 1, 1])
    with summary_col:
        st.markdown(
            f'<div style="font-size:12px;color:#1a1a1a;padding-top:6px;">'
            f'<b>{sel_count}</b> attendees · '
            f'{len(st.session_state.meeting_minutes_text)} chars minutes · '
            f'{len(st.session_state.agenda_items_text)} chars agenda · '
            f'{len(st.session_state.action_items_text)} chars actions'
            '</div>',
            unsafe_allow_html=True,
        )
    with btn_col_skip:
        if st.button("Skip AI, fill manually", use_container_width=True):
            st.session_state.parsed = ParsedMeeting(
                attendees=[], agenda_items=[], discussion_points=[], action_items=[]
            )
            st.session_state.meeting_date = meeting_date
            st.session_state.pop("dp_editor_text", None)
            st.session_state.pop("agenda_editor_text", None)
            st.toast("Empty form created — opening Review.", icon="📝")
            _goto("📝 Review")
    with btn_col_parse:
        if st.button("Parse with AI →", type="primary", use_container_width=True):
            minutes_text = st.session_state.meeting_minutes_text.strip()
            agenda_text = st.session_state.agenda_items_text.strip()
            actions_text = st.session_state.action_items_text.strip()
            if not (minutes_text or agenda_text or actions_text):
                st.error("Please fill in at least one of Meeting minutes, Agenda, or Action items.")
                return
            with st.spinner("Parsing notes with OpenAI…"):
                try:
                    with session_scope() as session:
                        proj = session.get(Project, project_id)
                        parsed = parse_notes_with_ai(
                            minutes_text, agenda_text, actions_text, proj,
                            attendees_roster=st.session_state.selected_attendees,
                        )
                        # Drop AI-detected attendees that already match someone
                        # on the roster (by first/last/initials). Whatever
                        # remains in parsed.attendees is "pending review" —
                        # the Review page surfaces them for Confirm/Skip.
                        parsed.attendees = [
                            a for a in parsed.attendees
                            if not _attendee_already_in_meeting(
                                a, st.session_state.selected_attendees
                            )
                        ]
                        # Apply defaults to AI-parsed actions that are missing
                        # a due date: meeting_date + 7 calendar days.
                        from datetime import timedelta as _td
                        _default_due = (meeting_date + _td(days=7)).isoformat()
                        for _ai in parsed.action_items:
                            if not _ai.due_date:
                                _ai.due_date = _default_due
                            if not _ai.status:
                                _ai.status = "open"
                        st.session_state.parsed = parsed
                        st.session_state.meeting_date = meeting_date
                        st.session_state.pop("dp_editor_text", None)
                        st.session_state.pop("agenda_editor_text", None)
                        st.toast(
                            f"Parsed: {len(parsed.attendees)} attendees · "
                            f"{len(parsed.agenda_items)} agenda · "
                            f"{len(parsed.discussion_points)} discussion · "
                            f"{len(parsed.action_items)} actions",
                            icon="✨",
                        )
                        _goto("📝 Review")
                except Exception as exc:
                    st.error(f"AI parsing failed: {exc}")


def render_review():
    st.markdown(
        f'<div class="brand-banner">📝 Step 2 of 4 — Review & edit</div>',
        unsafe_allow_html=True
    )
    _consume_url_meeting_if_present()

    if not st.session_state.parsed:
        st.warning("No parsed meeting yet. Start at **Capture** and parse some notes.")
        return

    parsed: ParsedMeeting = st.session_state.parsed

    # ---- Attendees ----
    selected = st.session_state.selected_attendees
    st.subheader(f"Attendees ({len(selected)})")

    if selected:
        st.caption("Confirmed on the Capture page — these will appear on the document:")
        per_row = 3
        for row_start in range(0, len(selected), per_row):
            rcols = st.columns(per_row)
            for c, a in enumerate(selected[row_start:row_start + per_row]):
                with rcols[c]:
                    st.markdown(
                        f"<div style='color:#1a1a1a;font-size:12px;'>"
                        f"<b>{a['full_name']}</b> ({a['initials']})<br>"
                        f"<span style='color:#4d4d4f;font-size:11px;'>{a['organization'] or '—'}</span>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
    else:
        st.info("No attendees selected. Go back to Capture and pick at least one.")

    # Pending new people — surfaced when AI detected a name not on the roster
    pending = parsed.attendees
    if pending:
        st.markdown("---")
        st.markdown(
            f'<div style="background:#fdeac0;border:2px solid {BrandColors.GOLD};'
            'border-radius:6px;padding:10px 13px;font-size:12px;color:#1a1a1a;'
            'font-weight:500;margin:6px 0 10px;">'
            f'🤔 <b>AI mentioned {len(pending)} person/people not on the roster.</b> '
            "Confirm to add to this meeting, or skip if it was a misread. "
            "Names that share a first or last name with someone already on the "
            "roster were merged automatically and are not shown here."
            '</div>',
            unsafe_allow_html=True,
        )
        for i, a in enumerate(list(pending)):
            cols_p = st.columns([3, 1, 1, 1])
            with cols_p[0]:
                st.markdown(
                    f"<div style='color:#1a1a1a;font-size:13px;padding-top:6px;'>"
                    f"<b>{a.full_name or a.initials}</b> "
                    f"<span style='color:#4d4d4f;'>({a.initials or '—'}) · "
                    f"{a.organization or 'no org'}</span></div>",
                    unsafe_allow_html=True,
                )
            with cols_p[1]:
                if st.button("✓ Add", key=f"pending_add_{i}", use_container_width=True,
                             type="primary"):
                    initials = (a.initials or "").strip().upper() or _make_initials(a.full_name or "")
                    st.session_state.selected_attendees.append({
                        "full_name": a.full_name or initials,
                        "initials": initials,
                        "organization": a.organization or "",
                    })
                    parsed.attendees = [x for j, x in enumerate(parsed.attendees) if j != i]
                    st.session_state.parsed = parsed
                    st.rerun()
            with cols_p[2]:
                if st.button("➕ Add + save to roster", key=f"pending_save_{i}",
                             use_container_width=True):
                    initials = (a.initials or "").strip().upper() or _make_initials(a.full_name or "")
                    org = (a.organization or "").strip()
                    name = (a.full_name or initials).strip()
                    with session_scope() as session:
                        upsert_project_attendee(
                            session, project_id=project_id,
                            full_name=name, initials=initials, organization=org,
                        )
                    st.session_state.selected_attendees.append({
                        "full_name": name, "initials": initials, "organization": org,
                    })
                    parsed.attendees = [x for j, x in enumerate(parsed.attendees) if j != i]
                    st.session_state.parsed = parsed
                    st.rerun()
            with cols_p[3]:
                if st.button("✕ Skip", key=f"pending_skip_{i}", use_container_width=True):
                    parsed.attendees = [x for j, x in enumerate(parsed.attendees) if j != i]
                    st.session_state.parsed = parsed
                    st.rerun()

    # ---- Agenda (text editor — one line per item) ----
    st.subheader(f"Agenda ({len(parsed.agenda_items)})")
    st.caption(
        "One line per topic. Bullet markers (`- `, `*`, `o`) at the start of "
        "a line are stripped automatically. Reorder by moving lines."
    )

    AGENDA_TEXT_KEY = "agenda_editor_text"
    if AGENDA_TEXT_KEY not in st.session_state:
        st.session_state[AGENDA_TEXT_KEY] = _agenda_to_text(parsed.agenda_items)

    def _sync_agenda():
        if st.session_state.parsed is not None:
            st.session_state.parsed.agenda_items = _text_to_agenda(
                st.session_state[AGENDA_TEXT_KEY]
            )

    st.text_area(
        "Agenda (text)",
        key=AGENDA_TEXT_KEY,
        height=180,
        on_change=_sync_agenda,
        label_visibility="collapsed",
        placeholder=(
            "- Due Diligence\n"
            "- Folder Structure\n"
            "- General Concerns"
        ),
    )

    if parsed.agenda_items:
        with st.expander(f"👁️ Preview ({len(parsed.agenda_items)} agenda items)"):
            for ag in parsed.agenda_items:
                st.markdown(
                    f"<div style='font-size:13px;color:#1a1a1a;line-height:1.5;'>"
                    f"● <b>{ag.text}</b></div>",
                    unsafe_allow_html=True,
                )
    st.session_state.parsed = parsed

    # ---- Deliverable Timelines (PM picks from the project's Schedule) ----
    st.subheader(f"Deliverable Timelines ({len(st.session_state.selected_deliverables)})")
    st.caption(
        "Pick which schedule tasks appear in the generated doc's Deliverable "
        "Timelines table. Manual rows can also be added."
    )

    # Currently-selected rows
    if st.session_state.selected_deliverables:
        for i, d in enumerate(list(st.session_state.selected_deliverables)):
            d_cols = st.columns([0.4, 2.0, 4.0, 1.4, 1.4, 0.5])
            d_cols[0].markdown(
                f"<div style='padding-top:8px;color:#4d4d4f;font-weight:700;'>{i + 1}</div>",
                unsafe_allow_html=True,
            )
            d["project_segment"] = d_cols[1].text_input(
                "Project / Segment", value=d.get("project_segment", ""),
                key=f"dl_seg_{i}", label_visibility="collapsed",
                placeholder="Civil Engineering",
            )
            d["task"] = d_cols[2].text_input(
                "Task", value=d.get("task", ""),
                key=f"dl_task_{i}", label_visibility="collapsed",
                placeholder="Civil Plan Set - 90% Design",
            )
            d["start_status"] = d_cols[3].selectbox(
                "Status",
                options=["Not Started", "In Progress", "On Hold", "Done"],
                index=["Not Started", "In Progress", "On Hold", "Done"].index(
                    d.get("start_status") or "In Progress"
                ) if d.get("start_status") in ("Not Started", "In Progress", "On Hold", "Done")
                else 1,
                key=f"dl_st_{i}", label_visibility="collapsed",
            )
            d["delivery_date"] = d_cols[4].date_input(
                "Delivery", value=d.get("delivery_date"),
                key=f"dl_due_{i}", label_visibility="collapsed",
                format="MM/DD/YYYY",
            )
            if d_cols[5].button("✕", key=f"dl_del_{i}",
                                help="Remove this deliverable",
                                use_container_width=True):
                st.session_state.selected_deliverables.pop(i)
                st.rerun()
    else:
        st.caption("No deliverables picked yet for this meeting.")

    # Picker — load upcoming ScheduleItems from the latest Schedule
    if project_id:
        with session_scope() as _sess:
            _latest_sched = (_sess.query(Schedule)
                             .filter_by(project_id=project_id)
                             .order_by(Schedule.uploaded_at.desc())
                             .first())
            if _latest_sched:
                from datetime import date as _date
                _mdate = st.session_state.get("meeting_date", _date.today())
                _items = sorted(
                    [it for it in _latest_sched.items
                     if it.indent_level >= 2 and it.finish_date
                     and it.finish_date >= _mdate],
                    key=lambda i: i.finish_date,
                )
                _sched_version = _latest_sched.version
                _items_data = [
                    {
                        "id": it.id,
                        "discipline": it.discipline or "",
                        "phase": it.phase or "",
                        "task": it.task,
                        "finish_date": it.finish_date,
                    }
                    for it in _items
                ]
            else:
                _sched_version = None
                _items_data = []

        if _sched_version and _items_data:
            with st.expander(
                f"➕ Pick from Schedule {_sched_version} ({len(_items_data)} upcoming tasks)",
                expanded=False,
            ):
                # Build labels for the multiselect
                def _label(it):
                    finish = it["finish_date"].strftime("%m/%d/%Y") if it["finish_date"] else "—"
                    disc = it["discipline"] or "—"
                    phase = f" · {it['phase']}" if it["phase"] else ""
                    return f"{disc}{phase} · {it['task']}  → {finish}"

                already = {
                    (d.get("project_segment", ""), d.get("task", ""))
                    for d in st.session_state.selected_deliverables
                }
                available = [
                    (_label(it), it) for it in _items_data
                    if (it["discipline"], it["task"]) not in already
                ]
                if not available:
                    st.caption("All upcoming schedule items are already in the list above.")
                else:
                    options_labels = [lbl for lbl, _ in available]
                    label_to_item = {lbl: it for lbl, it in available}
                    picked = st.multiselect(
                        "Schedule tasks to add",
                        options=options_labels,
                        placeholder="Pick one or more…",
                        key="dl_picker",
                    )
                    if st.button("➕ Add selected", key="dl_add_picked",
                                 disabled=not picked, type="primary"):
                        for lbl in picked:
                            it = label_to_item[lbl]
                            st.session_state.selected_deliverables.append({
                                "project_segment": it["discipline"],
                                "task": it["task"],
                                "start_status": "In Progress",
                                "delivery_date": it["finish_date"],
                            })
                        st.toast(f"Added {len(picked)} deliverable(s).", icon="📊")
                        st.rerun()
        elif not _sched_version:
            st.caption(
                "📊 No project schedule uploaded for this portfolio. Upload a proposal "
                "PDF or duration .xlsx on the **📊 Schedule** tab, then pick "
                "deliverables here. You can still add custom rows below."
            )

    if st.button("➕ Add custom deliverable row", key="dl_add_custom"):
        from datetime import date as _date, timedelta as _td
        st.session_state.selected_deliverables.append({
            "project_segment": "",
            "task": "",
            "start_status": "In Progress",
            "delivery_date": (st.session_state.get("meeting_date", _date.today())
                              + _td(days=7)),
        })
        st.rerun()

    # ---- Discussion points (text editor — indent for sub-points) ----
    from llm.providers import ParsedDiscussionPoint as _PDP
    total_dp_count = sum(1 + len(dp.sub_points) for dp in parsed.discussion_points)
    st.subheader(f"Discussion Points ({total_dp_count})")
    st.caption(
        "One line per point. **Indent with 2 spaces** (or a tab) to make a "
        "sub-point. Format `Label: content` if you want a bold lead. "
        "Bullet markers `- `, `*`, `o` at the start of a line are optional — "
        "they'll be stripped."
    )

    DP_TEXT_KEY = "dp_editor_text"
    if DP_TEXT_KEY not in st.session_state:
        st.session_state[DP_TEXT_KEY] = _dps_to_text(parsed.discussion_points)

    def _sync_dps():
        if st.session_state.parsed is not None:
            st.session_state.parsed.discussion_points = _text_to_dps(
                st.session_state[DP_TEXT_KEY]
            )

    st.text_area(
        "Discussion points (text)",
        key=DP_TEXT_KEY,
        height=360,
        on_change=_sync_dps,
        label_visibility="collapsed",
        placeholder=(
            "- Project Schedule Updates: Roashaael led the review of action items…\n"
            "- Technical Questions: Andrew, Ricky, Avinash, Arun, and Jalen discussed…\n"
            "  - Utility Recloser Settings for Waxwing: Roashaael requested utility recloser settings…\n"
            "  - Fuse Confirmation for Gonzo: Roashaael raised the need…\n"
            "- Fence Design and Geotechnical Data Review: Ricky, Janet, Jalen, and Avinash discussed…"
        ),
    )

    # Live preview of the parsed structure so the user can verify nesting
    if total_dp_count:
        with st.expander(f"👁️ Preview ({total_dp_count} points / sub-points)"):
            def _render_preview(dps, level=0):
                indent = "&nbsp;" * (level * 4)
                marker = "●" if level == 0 else "○"
                for dp in dps:
                    label = f"<b>{dp.label}:</b> " if dp.label else ""
                    st.markdown(
                        f"<div style='font-size:13px;color:#1a1a1a;line-height:1.5;'>"
                        f"{indent}{marker} {label}{dp.content}</div>",
                        unsafe_allow_html=True,
                    )
                    if dp.sub_points:
                        _render_preview(dp.sub_points, level + 1)
            _render_preview(parsed.discussion_points)

    # Advanced per-item editor — for fine-grained control of label/discipline.
    # Kept in an expander so the default flow stays text-first.
    DISCIPLINES = ["General", "Electrical", "Civil", "Structural"]
    advanced_open = st.expander(
        "🎛️ Advanced editor — per-item label & discipline tagging",
        expanded=False,
    )

    def _render_dp(dp, path: list[int], depth: int = 0):
        """Render one editable discussion point at `path` (list of indices
        through the tree). `depth` controls left indent and whether sub
        actions are offered."""
        key_prefix = "dp_" + "_".join(str(i) for i in path)
        indent_em = depth * 1.6
        with st.container():
            st.markdown(
                f'<div style="margin-left:{indent_em}em;padding:6px 0;'
                'border-left:3px solid '
                f'{"#ad1f2b" if depth == 0 else "#c7bb2e"};padding-left:10px;">'
                f'<small style="color:#4d4d4f;">'
                f'{"●" if depth == 0 else "○"} '
                f'{("Point" if depth == 0 else "Sub-point") + " " + ".".join(str(i+1) for i in path)}'
                '</small></div>',
                unsafe_allow_html=True,
            )
            c_lbl, c_disc = st.columns([3, 1])
            with c_lbl:
                dp.label = st.text_input(
                    "Label", value=dp.label, key=f"{key_prefix}_label",
                    placeholder="Short bold lead (e.g. 'IE methodology change')",
                )
            with c_disc:
                try:
                    disc_idx = DISCIPLINES.index(dp.discipline or "General")
                except ValueError:
                    disc_idx = 0
                dp.discipline = st.selectbox(
                    "Discipline", DISCIPLINES, index=disc_idx,
                    key=f"{key_prefix}_disc",
                )
            dp.content = st.text_area(
                "Content", value=dp.content, height=70,
                key=f"{key_prefix}_content",
                placeholder="The discussion detail after the label",
            )

            # Action buttons
            bcols = st.columns([1, 1, 1, 1, 4])
            with bcols[0]:
                up = st.button("↑", key=f"{key_prefix}_up",
                               help="Move up", use_container_width=True)
            with bcols[1]:
                down = st.button("↓", key=f"{key_prefix}_down",
                                 help="Move down", use_container_width=True)
            with bcols[2]:
                add_sub = st.button(
                    "+ Sub", key=f"{key_prefix}_addsub",
                    help="Add sub-point under this one",
                    use_container_width=True,
                    disabled=(depth >= 2),  # cap at 3 levels
                )
            with bcols[3]:
                delete = st.button(
                    "✕", key=f"{key_prefix}_del",
                    help="Delete this point (and its sub-points)",
                    use_container_width=True,
                )

        if up:
            _move_dp(parsed.discussion_points, path, -1)
            st.session_state.pop(DP_TEXT_KEY, None)
            st.rerun()
        if down:
            _move_dp(parsed.discussion_points, path, +1)
            st.session_state.pop(DP_TEXT_KEY, None)
            st.rerun()
        if add_sub:
            target = _resolve_dp(parsed.discussion_points, path)
            target.sub_points.append(_PDP(label="", content="", discipline="General"))
            st.session_state.pop(DP_TEXT_KEY, None)
            st.rerun()
        if delete:
            _delete_dp(parsed.discussion_points, path)
            st.session_state.pop(DP_TEXT_KEY, None)
            st.rerun()

        # Render children recursively
        for i, sub in enumerate(dp.sub_points):
            _render_dp(sub, path + [i], depth=depth + 1)

    with advanced_open:
        st.caption(
            "Use this when you need explicit discipline tags or the text editor "
            "isn't enough. Edits sync into the text editor above on the next rerun."
        )
        for i, dp in enumerate(parsed.discussion_points):
            _render_dp(dp, [i], depth=0)
        if st.button("➕ Add new discussion point", key="dp_add_top"):
            parsed.discussion_points.append(
                _PDP(label="", content="", discipline="General")
            )
            st.session_state.pop(DP_TEXT_KEY, None)  # reseed textarea
            st.rerun()

    st.session_state.parsed = parsed

    # ---- Action items (interactive — status dropdown + date picker) ----
    st.subheader(f"Action Items ({len(parsed.action_items)})")

    # Owner-helper text for the table — lists every selected attendee as a
    # reference so the user knows what to type into the Owner column.
    attendees_hint = ""
    if st.session_state.selected_attendees:
        attendees_hint = (
            "Available attendees — type one or more names (comma-separated): "
            + " · ".join(
                f"{a['full_name']} ({a['initials']})"
                for a in st.session_state.selected_attendees
            )
        )

    st.caption(
        "Edit any cell inline. Use the **+** row at the bottom of the table to add "
        "a new action. Long text wraps inside the cell — click into the cell to "
        "see / edit the full value."
    )
    if attendees_hint:
        st.caption(attendees_hint)
    import pandas as pd

    def _parse_iso(s):
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except (TypeError, ValueError):
            return None

    # Custom row-based action items editor — laid out with st.columns so it
    # reads as a table, but each row has its own widgets including a NATIVE
    # multi-select dropdown for the primary owners. Streamlit's data_editor
    # has no multi-select column type, so this is the closest "inline table"
    # approach using only Streamlit primitives.
    attendee_names = [a["full_name"] for a in st.session_state.selected_attendees]
    STATUS_CHOICES = ["Open", "Pending", "Completed", "Cancelled"]

    # Subtle column separators + status pill colors for an at-a-glance read
    st.markdown(
        """
        <style>
        .ai-row-wrap > div[data-testid="stHorizontalBlock"] {
            border-bottom: 1px solid #bcbec0;
            padding: 4px 0;
            align-items: start;
        }
        .ai-row-wrap > div[data-testid="stHorizontalBlock"]:last-child {
            border-bottom: none;
        }
        .ai-header > div[data-testid="stHorizontalBlock"] {
            border-bottom: 2px solid #ad1f2b !important;
            padding-bottom: 6px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    COL_WEIGHTS = [0.4, 4.5, 3.0, 1.4, 1.4, 0.5]
    COL_LABELS = ["#", "Action", "Owner(s)", "Due", "Status", ""]

    # Header row
    st.markdown('<div class="ai-header">', unsafe_allow_html=True)
    h = st.columns(COL_WEIGHTS)
    for col, label in zip(h, COL_LABELS):
        col.markdown(
            f"<div style='font-size:11px;font-weight:700;text-transform:uppercase;"
            f"letter-spacing:0.5px;color:#1a1a1a;'>{label}</div>",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    # Body rows
    st.markdown('<div class="ai-row-wrap">', unsafe_allow_html=True)
    for idx, a in enumerate(parsed.action_items):
        c = st.columns(COL_WEIGHTS)

        # #
        c[0].markdown(
            f"<div style='padding-top:18px;color:#4d4d4f;font-weight:700;'>{idx + 1}</div>",
            unsafe_allow_html=True,
        )
        # Action
        a.text = c[1].text_area(
            "Action", value=a.text or "", height=68,
            key=f"ai_text_{idx}", label_visibility="collapsed",
            placeholder="What needs to happen?",
        )
        # Owner(s) — multi-select from attendees. Any names that already exist
        # on the action but aren't on the current roster get rendered as
        # additional options so they stay editable rather than getting dropped.
        existing_parts = [p.strip() for p in (a.owner or "").split(",") if p.strip()]
        option_set = list(dict.fromkeys(attendee_names + existing_parts))
        picked = c[2].multiselect(
            "Owners",
            options=option_set,
            default=existing_parts,
            key=f"ai_own_{idx}",
            label_visibility="collapsed",
            placeholder=("Pick one or more…" if option_set else "No attendees — add via Capture"),
        )
        # Due date
        due_val = c[3].date_input(
            "Due", value=_parse_iso(a.due_date),
            key=f"ai_due_{idx}", label_visibility="collapsed",
            format="MM/DD/YYYY",
        )
        # Status
        try:
            status_idx = STATUS_CHOICES.index(
                (a.status or "Open").strip().capitalize()
            )
        except ValueError:
            status_idx = 0
        status_val = c[4].selectbox(
            "Status", STATUS_CHOICES, index=status_idx,
            key=f"ai_st_{idx}", label_visibility="collapsed",
        )
        # Delete
        if c[5].button("✕", key=f"ai_del_{idx}",
                       help="Delete this action", use_container_width=True):
            parsed.action_items.pop(idx)
            st.session_state.parsed = parsed
            st.rerun()

        # Push edits back into the model
        a.owner = ", ".join(picked)
        a.due_date = due_val.isoformat() if due_val else None
        a.status = status_val.lower()
    st.markdown("</div>", unsafe_allow_html=True)

    if not parsed.action_items:
        st.caption("No actions yet — click ➕ Add action to create one.")

    # Add-row button. Default due date = meeting_date + 7 calendar days.
    if st.button("➕ Add action", key="ai_add"):
        from datetime import timedelta
        md = st.session_state.get("meeting_date") or date.today()
        default_due = (md + timedelta(days=7)).isoformat()
        parsed.action_items.append(
            ParsedActionItem(text="", owner="", due_date=default_due, status="open")
        )
        st.session_state.parsed = parsed
        st.rerun()

    # Preview — same look as the Discussion / Agenda preview blocks. Renders
    # an HTML table that mirrors the PDF's Action Items section, including
    # the colored status pills.
    if parsed.action_items:
        with st.expander(f"👁️ Preview ({len(parsed.action_items)} action items)"):
            def _due_label(iso: str | None) -> str:
                if not iso:
                    return ""
                try:
                    d = date.fromisoformat(iso)
                    return d.strftime("%-m/%-d/%Y") if hasattr(d, "strftime") and " " not in d.strftime("%-m/%-d/%Y") else d.strftime("%#m/%#d/%Y")
                except Exception:
                    return iso

            rows_html = ""
            for i, a in enumerate(parsed.action_items, start=1):
                status = (a.status or "open").lower()
                if status not in ("open", "pending", "completed", "cancelled"):
                    status = "open"
                # Due — format without leading zeros, cross-platform
                due_str = ""
                if a.due_date:
                    try:
                        d = date.fromisoformat(a.due_date)
                        try:
                            due_str = d.strftime("%#m/%#d/%Y")
                        except ValueError:
                            due_str = d.strftime("%-m/%-d/%Y")
                    except (TypeError, ValueError):
                        due_str = a.due_date
                rows_html += (
                    f"<tr>"
                    f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;text-align:center;'>{i}</td>"
                    f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{(a.text or '')}</td>"
                    f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>{(a.owner or '')}</td>"
                    f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;white-space:nowrap;'>{due_str}</td>"
                    f"<td style='padding:6px 9px;border-bottom:1px solid #bcbec0;'>"
                    f"<span class='status-{status}'>{status.capitalize()}</span></td>"
                    f"</tr>"
                )
            st.markdown(
                "<table style='width:100%;border-collapse:collapse;font-size:12px;'>"
                "<thead><tr>"
                "<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>#</th>"
                "<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>Action</th>"
                "<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>Owner(s)</th>"
                "<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>Due</th>"
                "<th style='background:#8b1f2b;color:white;padding:8px 9px;text-align:left;'>Status</th>"
                "</tr></thead>"
                f"<tbody>{rows_html}</tbody></table>",
                unsafe_allow_html=True,
            )
    st.session_state.parsed = parsed

    st.divider()
    back_col, _, save_col, next_col = st.columns([1, 2, 1, 1])
    with back_col:
        if st.button("← Back to Capture", use_container_width=True, key="rev_back"):
            _goto("📥 Capture")
    with save_col:
        if st.button("Save as draft", use_container_width=True, key="rev_save"):
            _save_draft(parsed)
    with next_col:
        if st.button("Preview document →", type="primary",
                     use_container_width=True, key="rev_next"):
            _save_draft(parsed)
            if st.session_state.draft_meeting_id:
                _goto("👁️ Preview")


def _save_draft(parsed):
    """Persist the current parsed meeting.

    If `st.session_state.draft_meeting_id` is set, the existing meeting is
    UPDATED in place (children replaced). Otherwise a new meeting is created.
    """
    if not project_id:
        st.error("Pick a portfolio first.")
        return
    try:
        # selected_attendees is canonical. Any names left in parsed.attendees
        # are AI-detected people the user hasn't resolved on Review yet — we
        # silently drop them rather than create duplicates. The user gets a
        # Confirm / Skip UI on Review to fold them in if they want.
        selected = st.session_state.selected_attendees
        canonical_attendees = [
            ParsedAttendee(
                full_name=s["full_name"], initials=s["initials"],
                organization=s["organization"],
            )
            for s in selected
        ]
        parsed_to_save = parsed.model_copy(update={"attendees": canonical_attendees})

        with session_scope() as session:
            project = session.get(Project, project_id)
            meeting_date = st.session_state.get("meeting_date", date.today())
            combined_raw = (
                "=== MEETING MINUTES ===\n"
                f"{st.session_state.meeting_minutes_text}\n\n"
                "=== AGENDA ===\n"
                f"{st.session_state.agenda_items_text}\n\n"
                "=== ACTION ITEMS ===\n"
                f"{st.session_state.action_items_text}"
            )
            title = st.session_state.meeting_title.strip()

            picked_deliverables = list(st.session_state.selected_deliverables)
            existing_id = st.session_state.draft_meeting_id
            if existing_id:
                meeting = session.get(Meeting, existing_id)
                if meeting and meeting.project_id == project_id:
                    update_parsed_meeting(
                        session=session, meeting=meeting,
                        parsed=parsed_to_save,
                        meeting_date=meeting_date,
                        raw_notes=combined_raw, title=title,
                        deliverables=picked_deliverables,
                    )
                    st.success(f"Updated meeting #{meeting.id}")
                else:
                    # Stale id (different project, deleted, etc.) — fall back to create
                    meeting = save_parsed_meeting(
                        session=session, project=project,
                        meeting_date=meeting_date, parsed=parsed_to_save,
                        raw_notes=combined_raw, title=title,
                        deliverables=picked_deliverables,
                    )
                    session.flush()
                    st.session_state.draft_meeting_id = meeting.id
                    st.success(f"Saved as draft (meeting #{meeting.id})")
            else:
                meeting = save_parsed_meeting(
                    session=session, project=project,
                    meeting_date=meeting_date, parsed=parsed_to_save,
                    raw_notes=combined_raw, title=title,
                    deliverables=picked_deliverables,
                )
                session.flush()
                st.session_state.draft_meeting_id = meeting.id
                st.success(f"Saved as draft (meeting #{meeting.id})")
    except Exception as exc:
        st.error(f"Save failed: {exc}")


def render_preview():
    st.markdown(
        '<div class="brand-banner">👁️ Step 3 of 4 — Preview the document</div>',
        unsafe_allow_html=True
    )
    _consume_url_meeting_if_present()
    if not st.session_state.draft_meeting_id:
        st.warning("No draft saved yet. Save one from **Review**.")
        return

    with session_scope() as session:
        meeting = session.get(Meeting, st.session_state.draft_meeting_id)
        if not meeting:
            st.error("Draft meeting not found.")
            return

        from docgen.pdf_builder import generate_meeting_minutes_pdf
        from docgen import generate_meeting_minutes_docx
        pdf_bytes = generate_meeting_minutes_pdf(meeting)
        docx_bytes = generate_meeting_minutes_docx(meeting)

    # Rasterize each PDF page to an image and render. Chrome (and others) block
    # `data:application/pdf;base64,...` iframes by default, so we sidestep that
    # entirely by drawing PNGs.
    import pypdfium2 as pdfium
    pdf_doc = pdfium.PdfDocument(pdf_bytes)
    st.markdown(
        '<div style="background:#4d4d4f;padding:14px;border-radius:8px;">',
        unsafe_allow_html=True,
    )
    for i in range(len(pdf_doc)):
        page = pdf_doc[i]
        bitmap = page.render(scale=2)  # 2x for crisp display
        pil_img = bitmap.to_pil()
        st.image(pil_img, use_container_width=True,
                 caption=f"Page {i + 1} of {len(pdf_doc)}")
    pdf_doc.close()
    st.markdown('</div>', unsafe_allow_html=True)

    st.caption(
        "Preview rendered from the actual generated PDF (rasterized to PNGs). "
        "Download the PDF below for the interactive AcroForm status dropdowns."
    )

    col_pdf, col_docx = st.columns(2)
    with col_pdf:
        st.download_button(
            "⬇️ Download draft PDF",
            data=pdf_bytes,
            file_name=meeting_filename(meeting, "Meeting_Minutes", "pdf", draft=True),
            mime="application/pdf",
            use_container_width=True,
        )
    with col_docx:
        st.download_button(
            "⬇️ Download draft Word",
            data=docx_bytes,
            file_name=meeting_filename(meeting, "Meeting_Minutes", "docx", draft=True),
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            use_container_width=True,
        )

    st.divider()
    back_col, _, next_col = st.columns([1, 3, 1])
    with back_col:
        if st.button("← Back to Review", use_container_width=True, key="prev_back"):
            _goto("📝 Review")
    with next_col:
        if st.button("Send →", type="primary", use_container_width=True, key="prev_next"):
            _goto("📤 Send")


def render_send():
    st.markdown(
        f'<div class="brand-banner">📤 Step 4 of 4 — Send to client</div>',
        unsafe_allow_html=True
    )
    _consume_url_meeting_if_present()
    if not st.session_state.draft_meeting_id:
        st.warning("No draft saved yet.")
        return

    if st.button("Generate final docs", type="primary"):
        with session_scope() as session:
            meeting = session.get(Meeting, st.session_state.draft_meeting_id)
            paths = finalize_meeting(session, meeting)
            session.commit()
            st.success("Generated:")
            for kind, path in paths.items():
                st.write(f"• **{kind}**: `{path}`")

            # Offer downloads
            for kind, path in paths.items():
                p = Path(path)
                if p.exists():
                    st.download_button(
                        f"⬇️ Download {kind}",
                        data=p.read_bytes(),
                        file_name=p.name,
                    )

    st.caption("Phase 6 will add email composition and one-click send via Microsoft Graph.")

    st.divider()
    if st.button("← Back to Preview", key="send_back"):
        _goto("👁️ Preview")


def render_next_agenda():
    """Pre-meeting agenda editor. Five editable sections (Discussion Points,
    Previous Week Recap, Open Action Items, Risks & Constraints, Required
    Decisions) build up session state that gets handed to the .docx
    generator. Discussion + Recap reuse the indented-textarea pattern from
    Capture; the action/risk/decision tables reuse the inline-row pattern
    from Review."""
    st.markdown(
        f'<div class="brand-banner">📅 Pre-meeting coordination agenda</div>',
        unsafe_allow_html=True
    )
    if not project_id:
        st.warning("Pick a portfolio in the sidebar first.")
        return

    st.caption(
        "Build out the agenda for the upcoming meeting. Discussion Points "
        "are yours to draft; Previous Week Recap and Open Action Items are "
        "pre-filled from the prior meeting and the portfolio's rolling log — "
        "edit anything before generating."
    )

    from datetime import timedelta
    from db.repository import deliverables_to_carry_forward
    from llm.providers import ParsedDiscussionPoint as _PDP

    # ---------- pull lightweight list of meetings for the picker ----------
    from db.repository import (
        list_agendas as _list_agendas, get_agenda as _get_agenda,
        save_agenda as _save_agenda, delete_agenda as _delete_agenda,
    )
    with session_scope() as session:
        project = session.get(Project, project_id)
        meeting_choices_raw = (
            session.query(Meeting)
            .filter_by(project_id=project_id)
            .order_by(Meeting.meeting_date.desc(), Meeting.id.desc())
            .all()
        )
        meeting_choices = [
            {
                "id": m.id,
                "date": m.meeting_date,
                "title": m.title or "",
                "stage": m.stage or "draft",
            }
            for m in meeting_choices_raw
        ]
        # Saved agendas for this project
        agenda_choices = [
            {
                "id": a.id,
                "upcoming_date": a.upcoming_date,
                "title": a.title or "",
                "updated_at": a.updated_at,
            }
            for a in _list_agendas(session, project_id)
        ]
        carry_deliv = deliverables_to_carry_forward(session, project_id)
        latest_sched = (session.query(Schedule)
                        .filter_by(project_id=project_id)
                        .order_by(Schedule.uploaded_at.desc())
                        .first())
        carry_deliv_d = list(carry_deliv)
        sched_d = latest_sched
        schedule_version_label = (
            sched_d.version if sched_d else (project.schedule_version or "—")
        )

    # ---------- session-state seeding (per-project) ----------
    NA_PROJ_KEY = "na_loaded_for_project"
    if st.session_state.get(NA_PROJ_KEY) != project_id:
        # Hard reset — wipe everything tied to the previous project
        for k in list(st.session_state.keys()):
            if k.startswith("na_"):
                del st.session_state[k]
        st.session_state[NA_PROJ_KEY] = project_id

    # Default the source-meeting picker to the most recent meeting (if any).
    default_source_id = meeting_choices[0]["id"] if meeting_choices else None
    if "na_source_meeting_id" not in st.session_state:
        st.session_state.na_source_meeting_id = default_source_id

    # Agenda picker default: prefer ?agenda=<id> from the URL if it points
    # at an agenda for the active portfolio; otherwise auto-load the most
    # recent one so the editor opens populated. The user can switch to
    # "✏️ New" via the picker for a fresh draft.
    if "na_agenda_id" not in st.session_state:
        target_id = _url_agenda_id_for_current_portfolio(agenda_choices)
        if target_id is None and agenda_choices:
            target_id = agenda_choices[0]["id"]  # most recent, desc-sorted
        if target_id is not None:
            st.session_state.na_agenda_id = target_id
            with session_scope() as _session:
                _autoload_target = _get_agenda(_session, target_id)
                _na_load_from_agenda_row(_autoload_target)
        else:
            st.session_state.na_agenda_id = None

    if "na_disciplines" not in st.session_state:
        st.session_state.na_disciplines = ["Civil", "Electrical", "Structural", "General"]
    if "na_upcoming_date" not in st.session_state:
        st.session_state.na_upcoming_date = date.today() + timedelta(days=7)
    if "na_meeting_duration" not in st.session_state:
        st.session_state.na_meeting_duration = 30
    if "na_risks" not in st.session_state:
        st.session_state.na_risks = []
    if "na_decisions" not in st.session_state:
        st.session_state.na_decisions = []
    if "na_schedule_changes" not in st.session_state:
        st.session_state.na_schedule_changes = []
    if "na_schedule_version" not in st.session_state:
        st.session_state.na_schedule_version = ""
    if "na_agenda_title" not in st.session_state:
        st.session_state.na_agenda_title = ""

    # ---------- saved-agenda picker ----------
    st.markdown(
        "<div style='display:flex;align-items:center;gap:8px;"
        "margin-bottom:4px;'>"
        "<span style='font-size:13px;font-weight:600;color:#1a1a1a;'>"
        "Working on:</span></div>",
        unsafe_allow_html=True,
    )
    picker_col, save_col, del_col = st.columns([5, 1.2, 0.6])

    # Build option ids — None sentinel = "new agenda"
    picker_ids: list = [None] + [a["id"] for a in agenda_choices]

    def _fmt_agenda(aid):
        if aid is None:
            return "✏️ New agenda (unsaved)"
        a = next((x for x in agenda_choices if x["id"] == aid), None)
        if not a:
            return f"Agenda #{aid}"
        date_s = a["upcoming_date"].strftime("%b %d, %Y")
        title = a["title"] or f"Pre-meeting agenda — {date_s}"
        upd = a["updated_at"].strftime("%b %d %H:%M") if a["updated_at"] else ""
        return f"💾 {date_s} · {title}" + (f"  (saved {upd})" if upd else "")

    try:
        picker_idx = picker_ids.index(st.session_state.na_agenda_id)
    except ValueError:
        # The saved agenda was deleted under us — fall back to "New".
        picker_idx = 0
        st.session_state.na_agenda_id = None

    with picker_col:
        selected_aid = st.selectbox(
            "Saved agendas",
            options=picker_ids,
            index=picker_idx,
            format_func=_fmt_agenda,
            key="na_agenda_picker",
            label_visibility="collapsed",
        )

    # If the user just switched to a different agenda (or to "New"), load it.
    if selected_aid != st.session_state.na_agenda_id:
        _na_wipe_editor_keys()
        if selected_aid is None:
            # New agenda — defaults are fine; keep the project reset behavior
            st.session_state.na_agenda_id = None
            st.session_state.na_agenda_title = ""
        else:
            with session_scope() as session:
                a = _get_agenda(session, selected_aid)
                _na_load_from_agenda_row(a)
            st.session_state.na_agenda_id = selected_aid
        st.rerun()

    # Save button — always enabled. Inserts on first click, updates after.
    save_clicked = save_col.button(
        "💾 Save", key="na_save_btn",
        use_container_width=True,
        help=("Update the saved agenda" if st.session_state.na_agenda_id
              else "Save this draft to the portfolio"),
    )

    # Delete button — only meaningful when we have a saved row loaded.
    delete_clicked = False
    if st.session_state.na_agenda_id is not None:
        delete_clicked = del_col.button(
            "🗑️", key="na_delete_btn",
            use_container_width=True,
            help="Delete this saved agenda",
        )
    else:
        del_col.markdown(
            "<div style='padding-top:6px;color:#bcbec0;font-size:18px;"
            "text-align:center;'>—</div>",
            unsafe_allow_html=True,
        )

    # Optional title field. Uses the same key as the state var so loading a
    # saved agenda can prefill it via st.session_state assignment before
    # the rerun.
    st.text_input(
        "Title (optional — defaults to the upcoming date)",
        key="na_agenda_title",
        placeholder="e.g. Weekly coordination prep",
    )

    # ---- Save / Delete button handlers ----
    if save_clicked:
        try:
            payload = _na_collect_state_for_save()
            with session_scope() as session:
                saved = _save_agenda(
                    session,
                    project_id=project_id,
                    agenda_id=st.session_state.na_agenda_id,
                    **payload,
                )
                saved_id = saved.id
            st.session_state.na_agenda_id = saved_id
            st.toast(
                f"💾 Saved — {payload['upcoming_date'].strftime('%b %d, %Y')}",
                icon="✅",
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Save failed: {exc}")

    if delete_clicked and st.session_state.na_agenda_id is not None:
        try:
            with session_scope() as session:
                _delete_agenda(session, st.session_state.na_agenda_id)
            st.session_state.na_agenda_id = None
            _na_wipe_editor_keys()
            # Drop the picker widget's prior selection so it defaults to "New"
            st.session_state.pop("na_agenda_picker", None)
            st.toast("🗑️ Agenda deleted", icon="✅")
            st.rerun()
        except Exception as exc:
            st.error(f"Delete failed: {exc}")

    # ---------- meeting metadata + source picker ----------
    # Compute the auto-derived schedule version so the override input below
    # can show it as a placeholder. Matches the resolution order used by the
    # generators: latest uploaded Schedule → portfolio.schedule_version → "—".
    _auto_sched_version = (
        sched_d.version if sched_d
        else (project.schedule_version or "—")
    )
    meta_l, meta_dur, meta_sv, meta_r = st.columns([1, 0.7, 0.7, 1.4])
    with meta_l:
        upcoming = st.date_input(
            "Upcoming meeting date",
            value=st.session_state.na_upcoming_date,
            key="na_upcoming_date",
        )
    with meta_dur:
        # 30 vs 60 minute meeting. 60-min doubles every numeric time in the
        # fixed Agenda table ("Open" stays "Open"). Saved on the Agenda row
        # so the choice persists across edits.
        dur_choice = st.selectbox(
            "Meeting duration",
            options=[30, 60],
            index=([30, 60].index(st.session_state.na_meeting_duration)
                   if st.session_state.na_meeting_duration in (30, 60) else 0),
            format_func=lambda m: f"{m} min",
            key="na_meeting_duration",
        )
    with meta_sv:
        # Per-agenda override of "Current Schedule Version". Blank = auto
        # (latest uploaded project schedule, then portfolio default).
        st.text_input(
            "Schedule version",
            key="na_schedule_version",
            placeholder=f"Auto: {_auto_sched_version}",
            help=("Override the schedule version that prints on the agenda's "
                  "Deliverable Timelines section. Leave blank to use the "
                  f"latest uploaded value ({_auto_sched_version})."),
            max_chars=20,
        )
    with meta_r:
        if meeting_choices:
            def _fmt_meeting(mid: int) -> str:
                m = next((x for x in meeting_choices if x["id"] == mid), None)
                if not m:
                    return "—"
                stage_pill = {
                    "draft": "🟡 draft",
                    "final": "🟢 final",
                    "sent":  "📤 sent",
                }.get(m["stage"], m["stage"])
                title_part = f" — {m['title']}" if m["title"] else ""
                return f"{m['date'].strftime('%b %d, %Y')} · {stage_pill}{title_part}"
            ids = [m["id"] for m in meeting_choices]
            try:
                default_idx = ids.index(st.session_state.na_source_meeting_id)
            except ValueError:
                default_idx = 0
                st.session_state.na_source_meeting_id = ids[0]
            new_source_id = st.selectbox(
                "Source meeting (drives recap + attendees + carry-forward)",
                options=ids,
                index=default_idx,
                format_func=_fmt_meeting,
                key="na_source_meeting_select",
            )
            if new_source_id != st.session_state.na_source_meeting_id:
                # Picker changed — clear the recap seeding flag and any
                # textareas / open-action / attendee state tied to the prior
                # source so they re-seed from the new source on this render.
                st.session_state.na_source_meeting_id = new_source_id
                st.session_state.pop("na_recap_seeded", None)
                for k in list(st.session_state.keys()):
                    if k.startswith("na_recap_text__"):
                        del st.session_state[k]
                # Also re-seed open-action carry-forward (which currently is
                # project-wide — we keep that behavior, but reset the list so
                # the user's row-level deletions don't survive a source swap).
                st.session_state.pop("na_open_actions", None)
                # And re-seed the attendees from the newly-picked source.
                st.session_state.pop("na_attendees", None)
                # Schedule-change log is manual — reset on source swap.
                st.session_state.pop("na_schedule_changes", None)
                # Schedule-version override resets too (auto-derive again)
                st.session_state.pop("na_schedule_version", None)
                # Drop the stale preview from the prior source.
                for k in ("na_preview_pdf", "na_preview_docx",
                          "na_preview_stem", "na_preview_for_date"):
                    st.session_state.pop(k, None)
                st.rerun()
        else:
            st.info(
                "No prior meetings on this portfolio yet — the recap and "
                "carry-forward sections will be empty."
            )

    source_meeting_id = st.session_state.na_source_meeting_id

    # ---------- pre-load data tied to the chosen source meeting ----------
    with session_scope() as session:
        source = session.get(Meeting, source_meeting_id) if source_meeting_id else None
        open_act = open_actions(session, project_id)

        # Snapshot the source meeting's discussion points (bucketed by
        # discipline, top-level only — sub-points are walked via sub_points).
        prior_recap_seed: dict[str, list[dict]] = {}
        if source:
            for dp in sorted(source.discussion_points,
                             key=lambda d: d.order_index):
                if dp.parent_id is not None:
                    continue
                disc = (dp.discipline or "General").strip().capitalize() or "General"
                prior_recap_seed.setdefault(disc, []).append(_dp_snapshot(dp))

        # Snapshot source-meeting attendees (used as Owner-multiselect options)
        prior_attendees = []
        if source:
            for a in source.attendees:
                prior_attendees.append({
                    "full_name": a.full_name, "initials": a.initials,
                    "organization": a.organization,
                })
        open_act_d = [
            {
                "text": a.text, "owner": a.owner or "",
                "due_date": (a.due_date.isoformat() if a.due_date else None),
                "status": (a.status or "open").capitalize(),
            }
            for a in open_act
        ]

    # Seed (or re-seed after a source swap) the editor state
    if "na_open_actions" not in st.session_state:
        st.session_state.na_open_actions = list(open_act_d)
    if "na_recap_seeded" not in st.session_state:
        for disc, points in prior_recap_seed.items():
            st.session_state[f"na_recap_text__{disc}"] = _recap_seed_to_text(points)
        st.session_state.na_recap_seeded = True
    if "na_attendees" not in st.session_state:
        # Snapshot lives in `prior_attendees` (built above from the chosen
        # source). Copy into editor state so per-row edits don't mutate it.
        st.session_state.na_attendees = [dict(a) for a in prior_attendees]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Attendees", len(st.session_state.na_attendees))
    c2.metric("Open actions carrying over", len(st.session_state.na_open_actions))
    c3.metric("Deliverables carrying over", len(carry_deliv_d))
    c4.metric("Schedule version", schedule_version_label)

    # Keep a handle on the source meeting id for the generate-handler below
    last_id = source_meeting_id

    st.divider()

    # ====================================================================
    # 0. Attendees — seeded from source meeting, editable
    # ====================================================================
    st.markdown(
        "<h3 style='color:#ad1f2b;border-bottom:1px solid #ad1f2b;padding-bottom:4px;"
        "margin-top:8px;'>Attendees</h3>",
        unsafe_allow_html=True,
    )
    if source_meeting_id:
        st.caption(
            "Imported from the source meeting. Edit any row, drop people who "
            "won't attend, or add new ones — what's here is what ships in the "
            "agenda."
        )
    else:
        st.caption(
            "No source meeting on this portfolio yet — add attendees manually."
        )
    _render_attendee_table(st.session_state.na_attendees, key_prefix="na_att")
    bcol1, bcol2, _ = st.columns([1, 1, 4])
    if bcol1.button("➕ Add attendee", key="na_att_add",
                    use_container_width=True):
        st.session_state.na_attendees.append({
            "full_name": "", "initials": "", "organization": _CASTILLO_ORG,
        })
        st.rerun()
    if source_meeting_id and bcol2.button(
        "↻ Reset to source", key="na_att_reset",
        help="Discard edits and reload attendees from the selected source meeting",
        use_container_width=True,
    ):
        st.session_state.na_attendees = [dict(a) for a in prior_attendees]
        st.rerun()
    if st.session_state.na_attendees:
        with st.expander(
            f"👁️ Preview — Attendees by company "
            f"({len(st.session_state.na_attendees)} people)"
        ):
            _render_attendee_preview(st.session_state.na_attendees)

    st.divider()

    # ====================================================================
    # 1. Discussion Points — disciplines manager + per-discipline textarea
    # ====================================================================
    st.markdown(
        "<h3 style='color:#ad1f2b;border-bottom:1px solid #ad1f2b;padding-bottom:4px;"
        "margin-top:8px;'>Discussion Points</h3>",
        unsafe_allow_html=True,
    )
    st.caption(
        "One section per discipline. Each section uses the same indented-"
        "bullet format as Capture's discussion points: `- Label: content`, "
        "two-space indent for sub-points. Rename a section to suit the meeting "
        "or add a custom one (e.g. 'Permitting', 'Procurement')."
    )

    disciplines = st.session_state.na_disciplines

    # Add-discipline row
    add_col1, add_col2 = st.columns([3, 1])
    new_disc = add_col1.text_input(
        "Add a section (e.g. Permitting, Procurement)",
        value="", key="na_new_disc_input",
        label_visibility="collapsed",
        placeholder="Add a new section…",
    )
    if add_col2.button("➕ Add section", key="na_add_disc",
                       use_container_width=True):
        nd = (new_disc or "").strip()
        if nd and nd not in disciplines:
            disciplines.append(nd)
            st.session_state.na_disciplines = disciplines
            st.session_state.pop("na_new_disc_input", None)
            st.rerun()

    # Per-discipline editor
    for d_idx, discipline in enumerate(list(disciplines)):
        with st.container():
            head_l, head_r = st.columns([8, 1])
            new_name = head_l.text_input(
                f"Section name — {discipline}",
                value=discipline,
                key=f"na_disc_name_{d_idx}",
                label_visibility="collapsed",
            )
            # If they renamed, migrate the textarea keys
            if new_name.strip() and new_name.strip() != discipline:
                old = discipline
                new = new_name.strip()
                for prefix in ("na_dp_text__", "na_recap_text__"):
                    if f"{prefix}{old}" in st.session_state:
                        st.session_state[f"{prefix}{new}"] = st.session_state.pop(
                            f"{prefix}{old}"
                        )
                disciplines[d_idx] = new
                st.session_state.na_disciplines = disciplines
                st.rerun()
            if head_r.button("✕", key=f"na_disc_del_{d_idx}",
                             help="Remove this section",
                             use_container_width=True):
                # Drop discipline + its associated textarea state
                disciplines.pop(d_idx)
                st.session_state.na_disciplines = disciplines
                for prefix in ("na_dp_text__", "na_recap_text__"):
                    st.session_state.pop(f"{prefix}{discipline}", None)
                st.rerun()

            text_key = f"na_dp_text__{discipline}"
            if text_key not in st.session_state:
                st.session_state[text_key] = ""
            st.text_area(
                f"Discussion — {discipline}",
                key=text_key, height=160,
                label_visibility="collapsed",
                placeholder=(
                    "- Topic label: short description of what we'll cover.\n"
                    "  - Sub-bullet for a specific question or decision.\n"
                    "- Another topic…"
                ),
            )
            dps = _text_to_dps_with_discipline(
                st.session_state[text_key], discipline,
            )
            if dps:
                with st.expander(f"👁️ Preview — {discipline} "
                                 f"({_count_dps(dps)} points)"):
                    _render_dp_preview(dps)

    st.divider()

    # ====================================================================
    # 2. Previous Week Recap — same shape, seeded from prior meeting
    # ====================================================================
    st.markdown(
        "<h3 style='color:#ad1f2b;border-bottom:1px solid #ad1f2b;padding-bottom:4px;"
        "margin-top:8px;'>Previous Week Recap</h3>",
        unsafe_allow_html=True,
    )
    if source_meeting_id:
        src_label = next(
            (m for m in meeting_choices if m["id"] == source_meeting_id), None
        )
        src_date = src_label["date"].strftime("%B %d, %Y") if src_label else ""
        st.caption(
            f"Seeded from the **{src_date}** meeting's discussion points, "
            f"bucketed by the discipline tag. Edit, trim, or add — final "
            f"text is what ships in the agenda. Switch the source meeting "
            f"above to re-seed from a different one."
        )
    else:
        st.info("No prior meeting selected — recap is blank.")
    for discipline in disciplines:
        rec_key = f"na_recap_text__{discipline}"
        if rec_key not in st.session_state:
            st.session_state[rec_key] = ""
        st.markdown(
            f"<div style='font-weight:600;color:#1a1a1a;margin-top:8px;'>"
            f"{discipline}</div>",
            unsafe_allow_html=True,
        )
        st.text_area(
            f"Recap — {discipline}",
            key=rec_key, height=140,
            label_visibility="collapsed",
            placeholder="(no prior discussion for this discipline)",
        )
        rec_dps = _text_to_dps_with_discipline(
            st.session_state[rec_key], discipline,
        )
        if rec_dps:
            with st.expander(f"👁️ Preview — {discipline} recap "
                             f"({_count_dps(rec_dps)} points)"):
                _render_dp_preview(rec_dps)

    st.divider()

    # ====================================================================
    # 3. Open Action Items — inline editable table
    # ====================================================================
    st.markdown(
        "<h3 style='color:#ad1f2b;border-bottom:1px solid #ad1f2b;padding-bottom:4px;"
        "margin-top:8px;'>Open Action Items</h3>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Pre-filled from this portfolio's open and pending actions. Edit any "
        "row; the **×** drops it from the agenda (but does NOT close the "
        "action — go to the Actions tab to update its real status)."
    )
    # Owner-suggestion list — the (edited!) attendee list + names already on
    # the carry-forward actions
    owner_options = sorted({
        (a.get("full_name") or "").strip()
        for a in st.session_state.na_attendees
        if (a.get("full_name") or "").strip()
    } | {
        p.strip()
        for row in st.session_state.na_open_actions
        for p in (row.get("owner") or "").split(",")
        if p.strip()
    })

    _render_action_table(
        st.session_state.na_open_actions,
        owner_options,
        key_prefix="na_oa",
    )
    if st.button("➕ Add action", key="na_oa_add"):
        md = st.session_state.get("na_upcoming_date") or date.today()
        st.session_state.na_open_actions.append({
            "text": "", "owner": "",
            "due_date": (md + timedelta(days=7)).isoformat(),
            "status": "Open",
        })
        st.rerun()
    if st.session_state.na_open_actions:
        with st.expander(
            f"👁️ Preview — Open Action Items "
            f"({len(st.session_state.na_open_actions)} rows)"
        ):
            _render_action_preview(st.session_state.na_open_actions)

    st.divider()

    # ====================================================================
    # 3b. Schedule Change Log — manual entries, same shape as Risks/Decisions
    # ====================================================================
    st.markdown(
        "<h3 style='color:#ad1f2b;border-bottom:1px solid #ad1f2b;padding-bottom:4px;"
        "margin-top:8px;'>Schedule Change Log</h3>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Manual entry. Capture any milestone date shifts you want to surface "
        "to the client this week. Empty by default — only filled rows appear "
        "in the generated agenda."
    )
    _render_schedule_change_table(
        st.session_state.na_schedule_changes, "na_sc"
    )
    if st.button("➕ Add schedule change", key="na_sc_add"):
        st.session_state.na_schedule_changes.append({
            "project": "", "task": "",
            "previous_date": None, "updated_date": None,
            "change_description": "", "reason_for_change": "", "impact": "",
        })
        st.rerun()
    if st.session_state.na_schedule_changes:
        with st.expander(
            f"👁️ Preview — Schedule changes "
            f"({len(st.session_state.na_schedule_changes)} rows)"
        ):
            _render_schedule_change_preview(st.session_state.na_schedule_changes)

    st.divider()

    # ====================================================================
    # 4. Risks and Constraints
    # ====================================================================
    st.markdown(
        "<h3 style='color:#ad1f2b;border-bottom:1px solid #ad1f2b;padding-bottom:4px;"
        "margin-top:8px;'>Risks and Constraints</h3>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Manual entry. Capture the portfolio-level risks you want to surface "
        "to the client. Likelihood is a free-text field — use whatever scale "
        "your team agrees on (Low/Med/High, %, etc.)."
    )
    _render_risk_table(st.session_state.na_risks, owner_options, "na_risk")
    if st.button("➕ Add risk", key="na_risk_add"):
        st.session_state.na_risks.append({
            "description": "", "impact": "", "likelihood": "",
            "mitigation": "", "owner": "",
        })
        st.rerun()
    if st.session_state.na_risks:
        with st.expander(
            f"👁️ Preview — Risks ({len(st.session_state.na_risks)} rows)"
        ):
            _render_risk_preview(st.session_state.na_risks)

    st.divider()

    # ====================================================================
    # 5. Required Decisions
    # ====================================================================
    st.markdown(
        "<h3 style='color:#ad1f2b;border-bottom:1px solid #ad1f2b;padding-bottom:4px;"
        "margin-top:8px;'>Required Decisions</h3>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Manual entry. Decisions you need the client to make this week, "
        "with the impact-if-not-decided and an owner per decision."
    )
    _render_decision_table(
        st.session_state.na_decisions, owner_options, "na_dec"
    )
    if st.button("➕ Add decision", key="na_dec_add"):
        md = st.session_state.get("na_upcoming_date") or date.today()
        st.session_state.na_decisions.append({
            "decision": "", "description": "",
            "impact_if_not": "",
            "required_by": md + timedelta(days=7),
            "owner": "",
        })
        st.rerun()
    if st.session_state.na_decisions:
        with st.expander(
            f"👁️ Preview — Decisions ({len(st.session_state.na_decisions)} rows)"
        ):
            _render_decision_preview(st.session_state.na_decisions)

    st.divider()

    # ====================================================================
    # Generate / Preview
    # ====================================================================
    # The Generate button populates session_state with the rendered bytes;
    # the preview + download buttons render OUTSIDE the button's conditional
    # so they survive subsequent reruns (editing a row after Generate doesn't
    # make the preview disappear).
    gen_label = (
        "🔄 Regenerate agenda"
        if st.session_state.get("na_preview_pdf")
        else "📄 Generate agenda"
    )
    if st.button(gen_label, type="primary",
                 key="na_generate", use_container_width=True):
        try:
            from app.services import (
                generate_next_agenda, safe_filename_slug,
                _DraftAttendeeView,
            )
            from docgen import (
                generate_premeeting_agenda_docx,
                generate_premeeting_agenda_pdf,
            )

            # Build discipline-bucketed discussion + recap trees from the
            # session-state textareas. Empty disciplines drop out.
            dp_by_d = {}
            recap_by_d = {}
            for discipline in disciplines:
                dp_tree = _text_to_dps_with_discipline(
                    st.session_state.get(f"na_dp_text__{discipline}", ""),
                    discipline,
                )
                if dp_tree:
                    dp_by_d[discipline] = dp_tree
                rec_tree = _text_to_dps_with_discipline(
                    st.session_state.get(f"na_recap_text__{discipline}", ""),
                    discipline,
                )
                if rec_tree:
                    recap_by_d[discipline] = rec_tree

            # Build a lightweight detached carry-action list (no DB) so the
            # docgen function gets the freshly-edited rows, not the originals.
            carry_actions_for_doc = [
                _DraftAction(
                    text=r.get("text", ""),
                    owner=r.get("owner", ""),
                    due_date=_iso_to_date(r.get("due_date")),
                    status=(r.get("status") or "Open").lower(),
                )
                for r in st.session_state.na_open_actions
                if (r.get("text") or "").strip()
            ]

            with session_scope() as session:
                proj = session.get(Project, project_id)
                draft = generate_next_agenda(
                    session, proj, upcoming,
                    source_meeting_id=source_meeting_id,
                )
                # Replace source-meeting attendees with the (edited!)
                # session-state list. Drop blank rows.
                draft.attendees = [
                    _DraftAttendeeView(
                        full_name=(a.get("full_name") or "").strip(),
                        initials=(a.get("initials") or "").strip()
                                 or _make_initials(a.get("full_name") or ""),
                        organization=(a.get("organization") or "").strip()
                                     or "Other",
                    )
                    for a in st.session_state.na_attendees
                    if (a.get("full_name") or "").strip()
                ]
                prior = session.get(Meeting, source_meeting_id) if source_meeting_id else None
                deliv_refetched = deliverables_to_carry_forward(
                    session, project_id
                )
                sched_refetched = (session.query(Schedule)
                                   .filter_by(project_id=project_id)
                                   .order_by(Schedule.uploaded_at.desc())
                                   .first())
                # Convert schedule-change date objects to display strings
                # (the generators take strings for these two columns).
                def _d_to_str(v):
                    if v is None or v == "":
                        return ""
                    if isinstance(v, str):
                        return v
                    try:
                        return v.strftime("%m/%d/%Y")
                    except AttributeError:
                        return str(v)
                sc_for_doc = []
                for r in (st.session_state.get("na_schedule_changes") or []):
                    r2 = dict(r)
                    r2["previous_date"] = _d_to_str(r2.get("previous_date"))
                    r2["updated_date"] = _d_to_str(r2.get("updated_date"))
                    sc_for_doc.append(r2)
                gen_kwargs = dict(
                    meeting=draft,
                    prior_meeting=prior,
                    carry_actions=carry_actions_for_doc,
                    carry_deliverables=deliv_refetched,
                    schedule=sched_refetched,
                    disciplines=list(disciplines),
                    dp_by_discipline=dp_by_d,
                    recap_by_discipline=recap_by_d,
                    risks=list(st.session_state.na_risks),
                    decisions=list(st.session_state.na_decisions),
                    schedule_changes=sc_for_doc,
                    meeting_duration_minutes=int(
                        st.session_state.get("na_meeting_duration") or 30
                    ),
                    schedule_version_override=(
                        st.session_state.get("na_schedule_version") or ""
                    ).strip() or None,
                )
                pdf_bytes = generate_premeeting_agenda_pdf(**gen_kwargs)
                docx_bytes = generate_premeeting_agenda_docx(**gen_kwargs)

                proj_slug = safe_filename_slug(proj.name or "project")
                file_stem = (
                    f"{proj_slug}_Pre_Meeting_Agenda_{upcoming.isoformat()}"
                )

            # Park results in session state so the preview block below
            # renders them on every subsequent rerun until Regenerate.
            st.session_state.na_preview_pdf = pdf_bytes
            st.session_state.na_preview_docx = docx_bytes
            st.session_state.na_preview_stem = file_stem
            st.session_state.na_preview_for_date = upcoming.isoformat()
            st.rerun()
        except Exception as exc:
            st.error(f"Agenda generation failed: {exc}")
            import traceback
            st.code(traceback.format_exc())

    # ---- Preview + downloads ----
    pdf_bytes = st.session_state.get("na_preview_pdf")
    docx_bytes = st.session_state.get("na_preview_docx")
    if pdf_bytes:
        file_stem = st.session_state.get(
            "na_preview_stem", "Pre_Meeting_Agenda"
        )
        gen_date = st.session_state.get("na_preview_for_date") or upcoming.isoformat()
        try:
            gen_date_pretty = date.fromisoformat(gen_date).strftime("%B %d, %Y")
        except (TypeError, ValueError):
            gen_date_pretty = gen_date
        st.success(f"Agenda generated for {gen_date_pretty}.")

        # Download buttons: PDF dominates (3 cols wide, primary), Word is
        # a narrower secondary on the right.
        dl_pdf, dl_docx, _spacer = st.columns([3, 1.2, 0.2])
        with dl_pdf:
            st.download_button(
                "⬇️ Download agenda PDF",
                data=pdf_bytes,
                file_name=f"{file_stem}.pdf",
                mime="application/pdf",
                type="primary",
                use_container_width=True,
            )
        with dl_docx:
            st.download_button(
                "Word (.docx)",
                data=docx_bytes,
                file_name=f"{file_stem}.docx",
                mime=(
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                use_container_width=True,
            )

        # Rasterized preview — same dark-frame look as the Meeting Minutes
        # Preview tab. PDFs in <iframe> tags get blocked by Chrome, so we
        # render each page as a PNG via pypdfium2.
        st.markdown(
            '<div style="background:#4d4d4f;padding:14px;border-radius:8px;'
            'margin-top:14px;">',
            unsafe_allow_html=True,
        )
        try:
            import pypdfium2 as pdfium
            pdf_doc = pdfium.PdfDocument(pdf_bytes)
            for i in range(len(pdf_doc)):
                page = pdf_doc[i]
                bitmap = page.render(scale=2)
                st.image(
                    bitmap.to_pil(),
                    use_container_width=True,
                    caption=f"Page {i + 1} of {len(pdf_doc)}",
                )
            pdf_doc.close()
        except Exception as exc:
            st.error(f"Could not render preview: {exc}")
        st.markdown("</div>", unsafe_allow_html=True)

        st.caption(
            "Preview rendered from the actual generated PDF (rasterized to "
            "PNGs). Download the PDF for the interactive Status dropdowns on "
            "the carry-forward action items, or grab the Word version if you "
            "need to edit the document outside the app."
        )


def render_actions():
    """Project-wide rolling Action Items log — full inline CRUD.
    Filter by status (all / open + pending / completed / cancelled),
    edit any field in place, add new actions, delete with confirmation."""
    st.markdown(
        f'<div class="brand-banner">✅ Action items</div>',
        unsafe_allow_html=True
    )
    if not project_id:
        st.warning("Pick a portfolio first.")
        return

    from db.repository import all_actions
    from datetime import timedelta as _td

    # ---- top bar: status filter + Add + counts ----
    if "act_status_filter" not in st.session_state:
        st.session_state.act_status_filter = "Open + Pending"

    with session_scope() as session:
        proj = session.get(Project, project_id)
        rows = all_actions(session, project_id)
        # Snapshot the rows + a per-meeting label so we can render outside
        # the session without lazy-loading surprises.
        rows_snap = []
        for a in rows:
            origin = a.originating_meeting
            origin_label = (
                origin.meeting_date.strftime("%b %d, %Y") if origin else "—"
            )
            rows_snap.append({
                "id": a.id,
                "text": a.text or "",
                "owner": a.owner or "",
                "due_date": a.due_date,
                "status": (a.status or "open").capitalize(),
                "origin_label": origin_label,
                "updated_at": a.updated_at,
            })
        # Pull all attendee names ever seen on this project so the Owner
        # multiselect has good defaults.
        from db.repository import get_project_roster
        roster = get_project_roster(session, project_id)
        owner_options = sorted({r.full_name for r in roster})
        # Pick a fallback "raised at" meeting for new actions (latest one;
        # the meeting cannot be NULL on ActionItem).
        recent_meeting = latest_meeting(session, project_id)
        recent_meeting_id = recent_meeting.id if recent_meeting else None

    # ---- top toolbar (dropdown + add button, same row, equal height) ----
    f_col, add_col, _spacer = st.columns([2, 1, 3])
    with f_col:
        st.session_state.act_status_filter = st.selectbox(
            "Show",
            ["Open + Pending", "All", "Open", "Pending",
             "Completed", "Cancelled"],
            index=["Open + Pending", "All", "Open", "Pending",
                   "Completed", "Cancelled"].index(
                       st.session_state.act_status_filter),
            key="act_filter_select",
        )
    with add_col:
        # Invisible label matches the selectbox's label height so the
        # button bottom-aligns with the dropdown control on the same row.
        st.markdown(
            "<div style='font-size:14px;line-height:22px;visibility:hidden;'>"
            "&nbsp;</div>",
            unsafe_allow_html=True,
        )
        if st.button("➕ Add action", use_container_width=True,
                     key="act_add", type="primary"):
            if recent_meeting_id is None:
                st.error(
                    "Add at least one meeting to this portfolio before "
                    "creating action items — actions have to be raised "
                    "at a specific meeting."
                )
            else:
                try:
                    with session_scope() as session:
                        new = ActionItem(
                            project_id=project_id,
                            originating_meeting_id=recent_meeting_id,
                            text="",
                            owner="",
                            due_date=date.today() + _td(days=7),
                            status="open",
                        )
                        session.add(new)
                    st.toast("Added new action.", icon="➕")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Add failed: {exc}")

    # Counts row — separate caption line below the toolbar so it doesn't
    # fight the dropdown/button for vertical alignment.
    st.markdown(
        f"<div style='font-size:12px;color:#4d4d4f;margin:6px 0 4px;'>"
        f"<b>{len(rows_snap)}</b> total · "
        f"<b>{sum(1 for r in rows_snap if r['status'].lower() == 'open')}</b> open · "
        f"<b>{sum(1 for r in rows_snap if r['status'].lower() == 'pending')}</b> pending · "
        f"<b>{sum(1 for r in rows_snap if r['status'].lower() == 'completed')}</b> completed · "
        f"<b>{sum(1 for r in rows_snap if r['status'].lower() == 'cancelled')}</b> cancelled"
        f"</div>",
        unsafe_allow_html=True,
    )

    # ---- apply filter ----
    f = st.session_state.act_status_filter
    if f == "Open + Pending":
        rows_view = [r for r in rows_snap if r["status"].lower() in ("open", "pending")]
    elif f == "All":
        rows_view = rows_snap
    else:
        rows_view = [r for r in rows_snap if r["status"].lower() == f.lower()]

    if not rows_view:
        st.info(f"No actions match the **{f}** filter.")
        return

    # ---- editable table ----
    STATUS_CHOICES = ["Open", "Pending", "Completed", "Cancelled"]
    _na_row_border_css("act")
    _na_table_header(
        [0.4, 4.5, 2.6, 1.4, 1.4, 1.2, 0.5],
        ["#", "Action", "Owner(s)", "Due", "Status", "Raised", ""],
        "act",
    )

    st.markdown('<div class="act-row-wrap">', unsafe_allow_html=True)
    pending_updates: list[dict] = []
    delete_id = None
    for idx, r in enumerate(rows_view):
        c = st.columns([0.4, 4.5, 2.6, 1.4, 1.4, 1.2, 0.5])
        c[0].markdown(
            f"<div style='padding-top:18px;color:#4d4d4f;font-weight:700;'>"
            f"{idx + 1}</div>",
            unsafe_allow_html=True,
        )
        new_text = c[1].text_area(
            "Action", value=r["text"], height=68,
            key=f"act_text_{r['id']}",
            label_visibility="collapsed",
            placeholder="What needs to happen?",
        )
        existing_parts = [p.strip() for p in r["owner"].split(",") if p.strip()]
        option_set = list(dict.fromkeys(list(owner_options) + existing_parts))
        new_owners = c[2].multiselect(
            "Owners", options=option_set, default=existing_parts,
            key=f"act_own_{r['id']}",
            label_visibility="collapsed",
            placeholder=("Pick one or more…" if option_set else "Type name"),
        )
        new_due = c[3].date_input(
            "Due", value=r["due_date"],
            key=f"act_due_{r['id']}",
            label_visibility="collapsed",
            format="MM/DD/YYYY",
        )
        try:
            st_idx = STATUS_CHOICES.index(r["status"])
        except ValueError:
            st_idx = 0
        new_status = c[4].selectbox(
            "Status", STATUS_CHOICES, index=st_idx,
            key=f"act_st_{r['id']}",
            label_visibility="collapsed",
        )
        c[5].markdown(
            f"<div style='padding-top:14px;font-size:11px;color:#4d4d4f;'>"
            f"{r['origin_label']}</div>",
            unsafe_allow_html=True,
        )
        if c[6].button("✕", key=f"act_del_{r['id']}",
                       help="Delete this action",
                       use_container_width=True):
            delete_id = r["id"]

        # Detect any change vs. stored value and queue an update
        new_owner_str = ", ".join(new_owners)
        if (new_text != r["text"] or new_owner_str != r["owner"]
                or new_due != r["due_date"]
                or new_status.lower() != r["status"].lower()):
            pending_updates.append({
                "id": r["id"],
                "text": new_text,
                "owner": new_owner_str,
                "due_date": new_due,
                "status": new_status.lower(),
            })
    st.markdown("</div>", unsafe_allow_html=True)

    # ---- delete confirmation ----
    if delete_id is not None:
        st.session_state.act_pending_delete_id = delete_id
        st.rerun()
    if st.session_state.get("act_pending_delete_id"):
        del_id = st.session_state.act_pending_delete_id
        del_row = next((r for r in rows_snap if r["id"] == del_id), None)
        if del_row:
            st.warning(
                f"Delete action **#{del_id}**? "
                f"_{del_row['text'][:80]}_"
            )
            dcc1, dcc2, _ = st.columns([1, 1, 4])
            with dcc1:
                if st.button("🗑️ Yes, delete",
                             key=f"act_del_confirm_{del_id}",
                             type="primary", use_container_width=True):
                    try:
                        with session_scope() as session:
                            a = session.get(ActionItem, del_id)
                            if a is not None:
                                session.delete(a)
                        st.session_state.pop("act_pending_delete_id", None)
                        st.toast(f"Deleted action #{del_id}.", icon="🗑️")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Delete failed: {exc}")
            with dcc2:
                if st.button("Cancel",
                             key=f"act_del_cancel_{del_id}",
                             use_container_width=True):
                    st.session_state.pop("act_pending_delete_id", None)
                    st.rerun()

    # ---- save edits button (appears only when there are changes) ----
    if pending_updates:
        st.divider()
        sc1, sc2 = st.columns([1, 4])
        with sc1:
            if st.button(
                f"💾 Save {len(pending_updates)} change"
                f"{'s' if len(pending_updates) != 1 else ''}",
                type="primary", use_container_width=True, key="act_save_all",
            ):
                try:
                    with session_scope() as session:
                        for u in pending_updates:
                            a = session.get(ActionItem, u["id"])
                            if a is None:
                                continue
                            a.text = u["text"]
                            a.owner = u["owner"]
                            a.due_date = u["due_date"]
                            # If status moved to completed/cancelled and
                            # there's no closed_in_meeting_id yet, stamp it
                            # with the most-recent meeting so the action log
                            # records where the closeout happened.
                            if (u["status"] in ("completed", "cancelled")
                                    and a.closed_in_meeting_id is None):
                                if recent_meeting_id is not None:
                                    a.closed_in_meeting_id = recent_meeting_id
                            # If status reverted to open/pending, clear any
                            # close-out reference.
                            if (u["status"] in ("open", "pending")
                                    and a.closed_in_meeting_id is not None):
                                a.closed_in_meeting_id = None
                            a.status = u["status"]
                    st.toast(
                        f"Saved {len(pending_updates)} change"
                        f"{'s' if len(pending_updates) != 1 else ''}.",
                        icon="✅",
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Save failed: {exc}")
        with sc2:
            st.caption(
                "Unsaved edits highlighted by the Save button. "
                "Changes don't persist until you click."
            )


def render_notes():
    """Portfolio-scoped planner notes. Full inline CRUD — add / edit any
    field / delete with confirmation. Sorted with upcoming follow-ups first.
    Filter by status (Open / Closed / All) and by sub-project."""
    from db.repository import list_notes, set_portfolio_sub_projects
    from db.models import Note, Deliverable
    from datetime import timedelta as _td

    st.markdown(
        '<div class="brand-banner">📓 Notes</div>',
        unsafe_allow_html=True,
    )
    if not project_id:
        st.warning("Pick a portfolio first.")
        return

    # ---- session state defaults ----
    if "notes_status_filter" not in st.session_state:
        st.session_state.notes_status_filter = "Open"
    if "notes_project_filter" not in st.session_state:
        st.session_state.notes_project_filter = "All"

    # ---- pull notes + sub-project list (stored + auto-discovered) ----
    UNSET = "—"
    COMMON = "Common"
    with session_scope() as session:
        proj = session.get(Project, project_id)
        stored_subs = list(proj.sub_projects_json or [])

        notes = list_notes(session, project_id)
        rows_snap = []
        seen_note_subs: set[str] = set()
        for n in notes:
            area = n.project_area or ""
            if area:
                seen_note_subs.add(area)
            rows_snap.append({
                "id": n.id,
                "project_area": area,
                "source": n.source or "",
                "topic": n.topic or "",
                "action_needed": n.action_needed or "",
                "note_date": n.note_date,
                "follow_up_date": n.follow_up_date,
                "priority": n.priority or "Medium",
                "status": (n.status or "open").capitalize(),
                "updated_at": n.updated_at,
            })

        # Auto-discover from deliverables on this portfolio — gives the picker
        # useful options on day one even if PM hasn't curated the list yet.
        deliv_subs: set[str] = set()
        for d in (session.query(Deliverable)
                  .filter_by(project_id=project_id).all()):
            if d.project_segment:
                deliv_subs.add(d.project_segment.strip())

    # Merge into one ordered list — stored first (PM's curation wins),
    # then auto-discovered values not already covered. Casing preserved
    # from first appearance.
    seen_lower: set[str] = set()
    combined_subs: list[str] = []
    for src_list in (stored_subs, sorted(deliv_subs), sorted(seen_note_subs)):
        for s in src_list:
            if s.lower() in seen_lower:
                continue
            seen_lower.add(s.lower())
            combined_subs.append(s)

    # ---- manage-sub-projects expander ----
    with st.expander(
        f"🏷️ Manage sub-projects ({len(stored_subs)} saved · "
        f"{len(combined_subs)} in use including auto-discovered)"
    ):
        st.caption(
            "Curate the dropdown options that appear in the Project column "
            "below. **Common** is always available as a special value for "
            "notes that apply to the whole portfolio. Names found on existing "
            "deliverables or notes show up automatically — save them here to "
            "make them stick."
        )
        # Editable text area, one name per line
        sub_text = "\n".join(stored_subs)
        new_text = st.text_area(
            "Saved sub-projects (one per line)",
            value=sub_text,
            key="notes_subs_text",
            height=120,
            placeholder="Snapdragon\nTwo Blues",
        )
        scol_a, scol_b, scol_c = st.columns([1, 1, 3])
        with scol_a:
            if st.button("💾 Save list", use_container_width=True,
                         key="notes_subs_save"):
                try:
                    new_list = [
                        line.strip() for line in new_text.splitlines()
                        if line.strip()
                    ]
                    with session_scope() as session:
                        set_portfolio_sub_projects(session, project_id, new_list)
                    st.toast("Sub-project list saved.", icon="✅")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Save failed: {exc}")
        with scol_b:
            if deliv_subs - set(stored_subs):
                if st.button(
                    "↻ Pull from deliverables",
                    use_container_width=True,
                    key="notes_subs_pull",
                    help="Append any sub-project names seen on this "
                         "portfolio's deliverables to the saved list.",
                ):
                    try:
                        merged = list(stored_subs) + [
                            d for d in sorted(deliv_subs)
                            if d.lower() not in {s.lower() for s in stored_subs}
                        ]
                        with session_scope() as session:
                            set_portfolio_sub_projects(session, project_id, merged)
                        st.toast(
                            f"Added {len(merged) - len(stored_subs)} from deliverables.",
                            icon="✅",
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Pull failed: {exc}")

    # ---- top toolbar (status + project filter + add) ----
    f_col, p_col, add_col, _sp = st.columns([1.4, 1.8, 1, 2])
    with f_col:
        st.session_state.notes_status_filter = st.selectbox(
            "Show",
            ["Open", "Closed", "All"],
            index=["Open", "Closed", "All"].index(
                st.session_state.notes_status_filter),
            key="notes_filter_select",
        )
    with p_col:
        # Filter dropdown — All / Common / each sub-project / Unspecified
        filter_options = ["All", COMMON] + combined_subs + [UNSET]
        # Dedupe in case "Common" is also in the user's curated list
        seen = set()
        filter_options = [
            x for x in filter_options
            if not (x in seen or seen.add(x))
        ]
        try:
            pf_idx = filter_options.index(st.session_state.notes_project_filter)
        except ValueError:
            pf_idx = 0
            st.session_state.notes_project_filter = "All"
        st.session_state.notes_project_filter = st.selectbox(
            "Project", filter_options,
            index=pf_idx,
            format_func=lambda x: ("(unspecified)" if x == UNSET else x),
            key="notes_project_select",
        )
    with add_col:
        st.markdown(
            "<div style='font-size:14px;line-height:22px;visibility:hidden;'>"
            "&nbsp;</div>",
            unsafe_allow_html=True,
        )
        if st.button("➕ Add note", use_container_width=True,
                     key="notes_add", type="primary"):
            try:
                # Pre-fill project_area with the active filter when it's a
                # specific sub-project, so the new note inherits the context.
                pf = st.session_state.notes_project_filter
                default_area = pf if pf not in ("All", UNSET) else ""
                with session_scope() as session:
                    new = Note(
                        project_id=project_id,
                        project_area=default_area,
                        source="",
                        topic="",
                        action_needed="",
                        note_date=date.today(),
                        follow_up_date=None,
                        priority="Medium",
                        status="open",
                    )
                    session.add(new)
                st.toast("Added new note.", icon="➕")
                st.rerun()
            except Exception as exc:
                st.error(f"Add failed: {exc}")

    # Counts strip — show breakdown by sub-project as well
    by_sub_counts = {}
    for r in rows_snap:
        k = r["project_area"] or UNSET
        by_sub_counts[k] = by_sub_counts.get(k, 0) + 1
    sub_count_str = " · ".join(
        f"<b>{by_sub_counts.get(s, 0)}</b> {s}"
        for s in (combined_subs + [COMMON, UNSET])
        if by_sub_counts.get(s, 0) > 0
    )
    st.markdown(
        f"<div style='font-size:12px;color:#4d4d4f;margin:6px 0 4px;'>"
        f"<b>{len(rows_snap)}</b> total · "
        f"<b>{sum(1 for r in rows_snap if r['status'].lower() == 'open')}</b> open · "
        f"<b>{sum(1 for r in rows_snap if r['status'].lower() == 'closed')}</b> closed"
        + (f" &nbsp;|&nbsp; {sub_count_str}" if sub_count_str else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    # ---- apply filters ----
    f = st.session_state.notes_status_filter
    pf = st.session_state.notes_project_filter
    rows_view = rows_snap
    if f != "All":
        rows_view = [r for r in rows_view if r["status"].lower() == f.lower()]
    if pf == UNSET:
        rows_view = [r for r in rows_view if not r["project_area"]]
    elif pf != "All":
        rows_view = [r for r in rows_view if r["project_area"] == pf]

    if not rows_view:
        info_msg = f"No notes match the **{f}** filter"
        if pf != "All":
            info_msg += f" + **{pf}** project"
        st.info(info_msg + ".")
        return

    # ---- editable table ----
    PRIORITY_CHOICES = ["Low", "Medium", "High"]
    STATUS_CHOICES = ["Open", "Closed"]
    # Dropdown options for the per-row Project selectbox. Curated list +
    # auto-discovered + Common + "—" (unset). Any value already on a row
    # that's not in this list gets appended on the fly per row so it stays
    # editable.
    PROJECT_BASE_CHOICES = [UNSET, COMMON] + combined_subs
    # dedupe (keep first-seen casing)
    _seen_pc = set()
    PROJECT_BASE_CHOICES = [
        x for x in PROJECT_BASE_CHOICES
        if not (x.lower() in _seen_pc or _seen_pc.add(x.lower()))
    ]
    _na_row_border_css("notes")
    weights = [0.4, 1.4, 1.5, 1.7, 3.5, 1.2, 1.2, 1.2, 1.2, 0.5]
    labels = ["#", "Project", "Source", "Topic", "Action Needed",
              "Date", "Follow-up", "Priority", "Status", ""]
    _na_table_header(weights, labels, "notes")

    st.markdown('<div class="notes-row-wrap">', unsafe_allow_html=True)
    pending_updates: list[dict] = []
    delete_id = None
    for idx, r in enumerate(rows_view):
        c = st.columns(weights)
        c[0].markdown(
            f"<div style='padding-top:18px;color:#4d4d4f;font-weight:700;'>"
            f"{idx + 1}</div>",
            unsafe_allow_html=True,
        )
        # Row-level Project selectbox. If the stored value isn't in the
        # base list (e.g. PM typed a sub-project name once and never saved
        # it to the curated list), surface it as an option so it stays
        # selectable and the row can be re-saved with it intact.
        row_options = list(PROJECT_BASE_CHOICES)
        cur_area = r["project_area"]
        if not cur_area:
            cur_display = UNSET
        else:
            if cur_area not in row_options:
                row_options.append(cur_area)
            cur_display = cur_area
        new_area_choice = c[1].selectbox(
            "Project", row_options,
            index=row_options.index(cur_display),
            key=f"notes_area_{r['id']}",
            label_visibility="collapsed",
        )
        new_area = "" if new_area_choice == UNSET else new_area_choice
        new_source = c[2].text_input(
            "Source", value=r["source"],
            key=f"notes_src_{r['id']}",
            label_visibility="collapsed",
            placeholder="e.g. Meeting May 14",
        )
        new_topic = c[3].text_input(
            "Topic", value=r["topic"],
            key=f"notes_topic_{r['id']}",
            label_visibility="collapsed",
            placeholder="Short title",
        )
        new_action = c[4].text_area(
            "Action", value=r["action_needed"], height=68,
            key=f"notes_action_{r['id']}",
            label_visibility="collapsed",
            placeholder="What needs to happen / what to remember",
        )
        new_date = c[5].date_input(
            "Date", value=r["note_date"] or date.today(),
            key=f"notes_date_{r['id']}",
            label_visibility="collapsed",
            format="MM/DD/YYYY",
        )
        new_followup = c[6].date_input(
            "Follow-up", value=r["follow_up_date"],
            key=f"notes_fu_{r['id']}",
            label_visibility="collapsed",
            format="MM/DD/YYYY",
        )
        try:
            p_idx = PRIORITY_CHOICES.index(r["priority"])
        except ValueError:
            p_idx = 1
        new_priority = c[7].selectbox(
            "Priority", PRIORITY_CHOICES, index=p_idx,
            key=f"notes_pri_{r['id']}",
            label_visibility="collapsed",
        )
        try:
            s_idx = STATUS_CHOICES.index(r["status"])
        except ValueError:
            s_idx = 0
        new_status = c[8].selectbox(
            "Status", STATUS_CHOICES, index=s_idx,
            key=f"notes_st_{r['id']}",
            label_visibility="collapsed",
        )
        if c[9].button("✕", key=f"notes_del_{r['id']}",
                       help="Delete this note",
                       use_container_width=True):
            delete_id = r["id"]

        # Detect any change vs stored value and queue an update
        if (new_area != r["project_area"]
                or new_source != r["source"]
                or new_topic != r["topic"]
                or new_action != r["action_needed"]
                or new_date != r["note_date"]
                or new_followup != r["follow_up_date"]
                or new_priority != r["priority"]
                or new_status.lower() != r["status"].lower()):
            pending_updates.append({
                "id": r["id"],
                "project_area": new_area,
                "source": new_source,
                "topic": new_topic,
                "action_needed": new_action,
                "note_date": new_date,
                "follow_up_date": new_followup,
                "priority": new_priority,
                "status": new_status.lower(),
            })
    st.markdown("</div>", unsafe_allow_html=True)

    # ---- delete confirmation ----
    if delete_id is not None:
        st.session_state.notes_pending_delete_id = delete_id
        st.rerun()
    if st.session_state.get("notes_pending_delete_id"):
        del_id = st.session_state.notes_pending_delete_id
        del_row = next((r for r in rows_snap if r["id"] == del_id), None)
        if del_row:
            preview = del_row["topic"] or del_row["action_needed"]
            st.warning(
                f"Delete note **#{del_id}**? "
                f"_{(preview or '(empty)')[:80]}_"
            )
            dcc1, dcc2, _ = st.columns([1, 1, 4])
            with dcc1:
                if st.button("🗑️ Yes, delete",
                             key=f"notes_del_confirm_{del_id}",
                             type="primary", use_container_width=True):
                    try:
                        with session_scope() as session:
                            n = session.get(Note, del_id)
                            if n is not None:
                                session.delete(n)
                        st.session_state.pop("notes_pending_delete_id", None)
                        st.toast(f"Deleted note #{del_id}.", icon="🗑️")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Delete failed: {exc}")
            with dcc2:
                if st.button("Cancel",
                             key=f"notes_del_cancel_{del_id}",
                             use_container_width=True):
                    st.session_state.pop("notes_pending_delete_id", None)
                    st.rerun()

    # ---- save button (only when there are changes) ----
    if pending_updates:
        st.divider()
        sc1, sc2 = st.columns([1, 4])
        with sc1:
            if st.button(
                f"💾 Save {len(pending_updates)} change"
                f"{'s' if len(pending_updates) != 1 else ''}",
                type="primary", use_container_width=True, key="notes_save_all",
            ):
                try:
                    with session_scope() as session:
                        for u in pending_updates:
                            n = session.get(Note, u["id"])
                            if n is None:
                                continue
                            n.project_area = u["project_area"]
                            n.source = u["source"]
                            n.topic = u["topic"]
                            n.action_needed = u["action_needed"]
                            n.note_date = u["note_date"]
                            n.follow_up_date = u["follow_up_date"]
                            n.priority = u["priority"]
                            n.status = u["status"]
                    st.toast(
                        f"Saved {len(pending_updates)} change"
                        f"{'s' if len(pending_updates) != 1 else ''}.",
                        icon="✅",
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Save failed: {exc}")
        with sc2:
            st.caption(
                "Unsaved edits highlighted by the Save button. "
                "Changes don't persist until you click."
            )


def render_history():
    st.markdown(
        '<div class="brand-banner">📚 Portfolio history</div>',
        unsafe_allow_html=True
    )
    if not project_id:
        st.warning("Pick a portfolio first.")
        return

    tab_meetings, tab_agendas = st.tabs([
        "📋 Meeting Minutes", "📅 Pre-Meeting Agendas",
    ])
    with tab_meetings:
        _render_history_meetings()
    with tab_agendas:
        _render_history_agendas()


def _render_history_meetings():
    # Top bar — current edit state + new-meeting reset
    top_c1, top_c2 = st.columns([4, 1])
    with top_c1:
        if st.session_state.draft_meeting_id:
            st.info(
                f"Currently editing **meeting #{st.session_state.draft_meeting_id}** — "
                "saves on Review will update it in place."
            )
        else:
            st.caption(
                "Click **Open** on any meeting below to load it into "
                "Capture / Review / Preview for editing or re-export."
            )
    with top_c2:
        if st.button("➕ Start new meeting", use_container_width=True, key="hist_new"):
            _reset_session_for_new_meeting()
            _goto("📥 Capture")

    with session_scope() as session:
        from db.repository import list_meetings
        meetings = list_meetings(session, project_id)
        # Snapshot what we need so we don't touch the session after closing it
        meeting_rows = [
            {
                "id": m.id,
                "date": m.meeting_date,
                "title": m.title,
                "stage": m.stage,
                "n_attendees": len(m.attendees),
                "n_actions": len(m.raised_actions),
                "n_discussion": len(m.discussion_points),
                "n_agenda": len(m.agenda_items),
                "updated_at": m.updated_at,
            }
            for m in meetings
        ]

    if not meeting_rows:
        st.info("No meetings yet for this portfolio.")
        return

    # Render each meeting as a card with Open / Export actions
    for row in meeting_rows:
        stage_color = {
            "draft": ("#fdeac0", "#5e3f00"),
            "final": ("#c7e9a3", "#1a3a04"),
            "sent":  ("#e6f0fa", "#185fa5"),
        }.get(row["stage"], ("#e6e7e8", "#1a1a1a"))
        with st.container(border=True):
            head_a, head_b, head_open, head_export, head_del = st.columns([3, 2, 1, 1, 1])
            with head_a:
                st.markdown(
                    f'<div style="font-size:14px;font-weight:700;color:#1a1a1a;">'
                    f'{row["date"].strftime("%B %d, %Y")} — '
                    f'{row["title"] or "Meeting"}</div>'
                    f'<div style="font-size:11px;color:#4d4d4f;margin-top:2px;">'
                    f'Meeting #{row["id"]} · updated '
                    f'{row["updated_at"].strftime("%b %d, %Y") if row["updated_at"] else "—"}'
                    '</div>',
                    unsafe_allow_html=True,
                )
            with head_b:
                st.markdown(
                    f'<div><span style="background:{stage_color[0]};color:{stage_color[1]};'
                    'padding:2px 9px;border-radius:10px;font-size:11px;font-weight:700;'
                    f'text-transform:uppercase;letter-spacing:0.4px;">{row["stage"]}</span>'
                    f'<span style="margin-left:10px;font-size:11px;color:#4d4d4f;">'
                    f'{row["n_attendees"]} attendees · {row["n_agenda"]} agenda · '
                    f'{row["n_discussion"]} discussion · {row["n_actions"]} actions'
                    '</span></div>',
                    unsafe_allow_html=True,
                )
            with head_open:
                if st.button("📂 Open", key=f"hist_open_{row['id']}", use_container_width=True):
                    if _load_meeting_into_session(row["id"]):
                        st.toast(f"Loaded meeting #{row['id']} — opening Review.", icon="📂")
                        _goto("📝 Review")
                    else:
                        st.error("Meeting not found.")
            with head_export:
                if st.button("📤 Export", key=f"hist_export_{row['id']}",
                             use_container_width=True):
                    st.session_state[f"_show_export_{row['id']}"] = True
            with head_del:
                if st.button("🗑️ Delete", key=f"hist_del_{row['id']}",
                             use_container_width=True):
                    st.session_state[f"_show_delete_{row['id']}"] = True

            # Inline delete confirmation panel — appears when Delete is clicked
            if st.session_state.get(f"_show_delete_{row['id']}"):
                st.warning(
                    f"Deleting **meeting #{row['id']}** "
                    f"({row['date'].strftime('%B %d, %Y')} — {row['title'] or 'Meeting'}) "
                    "will permanently remove its attendees, agenda, discussion points, "
                    "and any action items raised at this meeting. Action items that were "
                    "*closed* in this meeting will have their close-out reference cleared "
                    "but the items themselves stay."
                )
                del_c1, del_c2 = st.columns([1, 1])
                with del_c1:
                    if st.button("🗑️ Yes, permanently delete",
                                 key=f"hist_delconfirm_{row['id']}",
                                 type="primary", use_container_width=True):
                        try:
                            with session_scope() as session:
                                m = session.get(Meeting, row["id"])
                                if m is not None:
                                    # Clear "closed at this meeting" refs on
                                    # actions raised at OTHER meetings.
                                    for ai in (session.query(ActionItem)
                                               .filter_by(closed_in_meeting_id=row["id"])
                                               .all()):
                                        ai.closed_in_meeting_id = None
                                    # Generated-document audit rows don't
                                    # cascade — drop them explicitly.
                                    from db.models import GeneratedDocument
                                    (session.query(GeneratedDocument)
                                     .filter_by(meeting_id=row["id"]).delete())
                                    session.delete(m)
                            # If this was the currently-loaded draft, clear session
                            if st.session_state.draft_meeting_id == row["id"]:
                                _reset_session_for_new_meeting()
                            st.session_state.pop(f"_show_delete_{row['id']}", None)
                            st.session_state.pop(f"_show_export_{row['id']}", None)
                            st.toast(f"Deleted meeting #{row['id']}.", icon="🗑️")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Delete failed: {exc}")
                with del_c2:
                    if st.button("Cancel", key=f"hist_delcancel_{row['id']}",
                                 use_container_width=True):
                        del st.session_state[f"_show_delete_{row['id']}"]
                        st.rerun()

            # Inline export panel — appears when Export is clicked
            if st.session_state.get(f"_show_export_{row['id']}"):
                from docgen.pdf_builder import generate_meeting_minutes_pdf
                from docgen import generate_meeting_minutes_docx
                with session_scope() as session:
                    m = session.get(Meeting, row["id"])
                    pdf_bytes = generate_meeting_minutes_pdf(m)
                    docx_bytes = generate_meeting_minutes_docx(m)
                    pdf_name = meeting_filename(m, "Meeting_Minutes", "pdf")
                    docx_name = meeting_filename(m, "Meeting_Minutes", "docx")
                exp_pdf, exp_docx, exp_close = st.columns([2, 2, 1])
                with exp_pdf:
                    st.download_button(
                        "⬇️ PDF",
                        data=pdf_bytes,
                        file_name=pdf_name,
                        mime="application/pdf",
                        key=f"hist_dlpdf_{row['id']}",
                        use_container_width=True,
                    )
                with exp_docx:
                    st.download_button(
                        "⬇️ Word",
                        data=docx_bytes,
                        file_name=docx_name,
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        key=f"hist_dldocx_{row['id']}",
                        use_container_width=True,
                    )
                with exp_close:
                    if st.button("✕ Close", key=f"hist_closex_{row['id']}",
                                 use_container_width=True):
                        del st.session_state[f"_show_export_{row['id']}"]
                        st.rerun()


def _build_agenda_artifacts(agenda_id: int) -> tuple[bytes, bytes, str]:
    """Re-render a saved Agenda back into PDF + DOCX bytes for export. Returns
    (pdf_bytes, docx_bytes, file_stem). Each call hits the DB to load the
    Agenda row + its source meeting + project schedule, then assembles the
    same kwargs the Generate handler uses."""
    from app.services import (
        generate_next_agenda, safe_filename_slug, _DraftAttendeeView,
    )
    from db.repository import (
        get_agenda, open_actions, deliverables_to_carry_forward,
    )
    from docgen import (
        generate_premeeting_agenda_pdf, generate_premeeting_agenda_docx,
    )

    with session_scope() as session:
        a = get_agenda(session, agenda_id)
        if a is None:
            raise ValueError(f"Agenda {agenda_id} not found")
        proj = a.project

        disciplines = list(a.disciplines_json or [])
        dp_by_d = {}
        recap_by_d = {}
        for disc in disciplines:
            dp_text = (a.dp_text_json or {}).get(disc, "")
            tree = _text_to_dps_with_discipline(dp_text, disc)
            if tree:
                dp_by_d[disc] = tree
            rec_text = (a.recap_text_json or {}).get(disc, "")
            rec_tree = _text_to_dps_with_discipline(rec_text, disc)
            if rec_tree:
                recap_by_d[disc] = rec_tree

        # Carry-forward actions: prefer the saved snapshot (PM may have
        # edited it inside the agenda editor and re-saved). Fall back to
        # the project's live open actions if the snapshot is empty.
        saved_actions = list(a.open_actions_json or [])
        if saved_actions:
            carry_actions = [
                _DraftAction(
                    text=r.get("text", ""),
                    owner=r.get("owner", ""),
                    due_date=_iso_to_date(r.get("due_date")),
                    status=(r.get("status") or "Open").lower(),
                )
                for r in saved_actions
                if (r.get("text") or "").strip()
            ]
        else:
            carry_actions = list(open_actions(session, proj.id))

        # Decisions: ISO-string dates → date objects
        decisions = []
        for d in (a.decisions_json or []):
            d = dict(d)
            rb = d.get("required_by")
            if isinstance(rb, str):
                d["required_by"] = _iso_to_date(rb)
            decisions.append(d)

        # Build the detached draft meeting view + attendees from the saved list
        draft = generate_next_agenda(
            session, proj, a.upcoming_date,
            source_meeting_id=a.source_meeting_id,
        )
        attendees_saved = list(a.attendees_json or [])
        if attendees_saved:
            draft.attendees = [
                _DraftAttendeeView(
                    full_name=(p.get("full_name") or "").strip(),
                    initials=(p.get("initials") or "").strip()
                              or _make_initials(p.get("full_name") or ""),
                    organization=(p.get("organization") or "").strip() or "Other",
                )
                for p in attendees_saved
                if (p.get("full_name") or "").strip()
            ]

        prior = a.source_meeting
        carry_deliv = list(deliverables_to_carry_forward(session, proj.id))
        sched = (
            session.query(Schedule)
            .filter_by(project_id=proj.id)
            .order_by(Schedule.uploaded_at.desc())
            .first()
        )

        # Schedule changes stored as ISO strings — convert dates to MM/DD/YYYY
        def _d_to_str(v):
            if v is None or v == "":
                return ""
            if isinstance(v, str):
                d = _iso_to_date(v)
                if d is None:
                    return v
                v = d
            try:
                return v.strftime("%m/%d/%Y")
            except AttributeError:
                return str(v)
        sc_for_doc = []
        for r in (getattr(a, "schedule_changes_json", None) or []):
            r2 = dict(r)
            r2["previous_date"] = _d_to_str(r2.get("previous_date"))
            r2["updated_date"] = _d_to_str(r2.get("updated_date"))
            sc_for_doc.append(r2)
        gen_kwargs = dict(
            meeting=draft,
            prior_meeting=prior,
            carry_actions=carry_actions,
            carry_deliverables=carry_deliv,
            schedule=sched,
            disciplines=disciplines,
            dp_by_discipline=dp_by_d,
            recap_by_discipline=recap_by_d,
            risks=list(a.risks_json or []),
            decisions=decisions,
            schedule_changes=sc_for_doc,
            meeting_duration_minutes=(
                getattr(a, "meeting_duration_minutes", None) or 30
            ),
            schedule_version_override=(
                getattr(a, "schedule_version_override", None) or None
            ),
        )
        pdf_bytes = generate_premeeting_agenda_pdf(**gen_kwargs)
        docx_bytes = generate_premeeting_agenda_docx(**gen_kwargs)

        proj_slug = safe_filename_slug(proj.name or "project")
        file_stem = (
            f"{proj_slug}_Pre_Meeting_Agenda_{a.upcoming_date.isoformat()}"
        )
    return pdf_bytes, docx_bytes, file_stem


def _render_history_agendas():
    """Saved pre-meeting agendas — list / open / export / delete."""
    from db.repository import (
        list_agendas as _list_agendas, get_agenda as _get_agenda,
        delete_agenda as _delete_agenda,
    )

    # Top bar
    top_c1, top_c2 = st.columns([4, 1])
    with top_c1:
        if st.session_state.get("na_agenda_id"):
            st.info(
                f"Currently editing agenda **#{st.session_state.na_agenda_id}** — "
                "saves on Next Agenda will update it in place."
            )
        else:
            st.caption(
                "Click **Open** on any agenda below to load it into "
                "Next Agenda for editing or re-export."
            )
    with top_c2:
        if st.button("➕ Start new agenda", use_container_width=True,
                     key="hist_ag_new"):
            _na_wipe_editor_keys()
            st.session_state.na_agenda_id = None
            st.session_state.pop("na_agenda_picker", None)
            _goto("📅 Next Agenda")

    with session_scope() as session:
        agendas = _list_agendas(session, project_id)
        # Snapshot fields we need outside the session
        rows = []
        for a in agendas:
            src_label = ""
            if a.source_meeting is not None:
                src = a.source_meeting
                src_label = (
                    f"{src.meeting_date.strftime('%b %d, %Y')}"
                    + (f" — {src.title}" if src.title else "")
                )
            rows.append({
                "id": a.id,
                "upcoming_date": a.upcoming_date,
                "title": a.title,
                "source_label": src_label,
                "updated_at": a.updated_at,
                "n_disciplines": len(a.disciplines_json or []),
                "n_attendees": len(a.attendees_json or []),
                "n_actions": len(a.open_actions_json or []),
                "n_risks": len(a.risks_json or []),
                "n_decisions": len(a.decisions_json or []),
            })

    if not rows:
        st.info(
            "No saved agendas yet for this portfolio. Build one on the "
            "**Next Agenda** tab and click **💾 Save**."
        )
        return

    for r in rows:
        with st.container(border=True):
            head_a, head_b, head_open, head_export, head_del = st.columns(
                [3, 2, 1, 1, 1]
            )
            with head_a:
                title = r["title"] or (
                    f"Pre-meeting agenda — {r['upcoming_date'].strftime('%b %d, %Y')}"
                )
                st.markdown(
                    f'<div style="font-size:14px;font-weight:700;color:#1a1a1a;">'
                    f'{r["upcoming_date"].strftime("%B %d, %Y")} — {title}</div>'
                    f'<div style="font-size:11px;color:#4d4d4f;margin-top:2px;">'
                    f'Agenda #{r["id"]} · updated '
                    f'{r["updated_at"].strftime("%b %d, %Y") if r["updated_at"] else "—"}'
                    + (f' · source: {r["source_label"]}' if r["source_label"] else " · no source meeting")
                    + '</div>',
                    unsafe_allow_html=True,
                )
            with head_b:
                st.markdown(
                    f'<div><span style="background:#fdeac0;color:#5e3f00;'
                    'padding:2px 9px;border-radius:10px;font-size:11px;font-weight:700;'
                    f'text-transform:uppercase;letter-spacing:0.4px;">draft</span>'
                    f'<span style="margin-left:10px;font-size:11px;color:#4d4d4f;">'
                    f'{r["n_disciplines"]} disciplines · {r["n_attendees"]} attendees · '
                    f'{r["n_actions"]} actions · {r["n_risks"]} risks · '
                    f'{r["n_decisions"]} decisions'
                    '</span></div>',
                    unsafe_allow_html=True,
                )
            with head_open:
                if st.button("📂 Open", key=f"hist_ag_open_{r['id']}",
                             use_container_width=True):
                    with session_scope() as session:
                        a = _get_agenda(session, r["id"])
                        _na_wipe_editor_keys()
                        _na_load_from_agenda_row(a)
                    st.session_state.pop("na_agenda_picker", None)
                    st.toast(f"Loaded agenda #{r['id']} — opening Next Agenda.",
                             icon="📂")
                    _goto("📅 Next Agenda")
            with head_export:
                if st.button("📤 Export", key=f"hist_ag_export_{r['id']}",
                             use_container_width=True):
                    st.session_state[f"_show_agenda_export_{r['id']}"] = True
            with head_del:
                if st.button("🗑️ Delete", key=f"hist_ag_del_{r['id']}",
                             use_container_width=True):
                    st.session_state[f"_show_agenda_delete_{r['id']}"] = True

            # Delete confirmation
            if st.session_state.get(f"_show_agenda_delete_{r['id']}"):
                st.warning(
                    f"Deleting **agenda #{r['id']}** "
                    f"({r['upcoming_date'].strftime('%B %d, %Y')} — {title}) "
                    "will permanently remove this saved draft. The source "
                    "meeting and rolling action items are NOT affected."
                )
                del_c1, del_c2 = st.columns([1, 1])
                with del_c1:
                    if st.button("🗑️ Yes, permanently delete",
                                 key=f"hist_ag_delconfirm_{r['id']}",
                                 type="primary", use_container_width=True):
                        try:
                            with session_scope() as session:
                                _delete_agenda(session, r["id"])
                            if st.session_state.get("na_agenda_id") == r["id"]:
                                st.session_state.na_agenda_id = None
                                _na_wipe_editor_keys()
                            st.session_state.pop(
                                f"_show_agenda_delete_{r['id']}", None
                            )
                            st.session_state.pop(
                                f"_show_agenda_export_{r['id']}", None
                            )
                            st.toast(f"Deleted agenda #{r['id']}.", icon="🗑️")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Delete failed: {exc}")
                with del_c2:
                    if st.button("Cancel", key=f"hist_ag_delcancel_{r['id']}",
                                 use_container_width=True):
                        del st.session_state[f"_show_agenda_delete_{r['id']}"]
                        st.rerun()

            # Export panel — rebuilds PDF + DOCX from the saved row
            if st.session_state.get(f"_show_agenda_export_{r['id']}"):
                try:
                    pdf_bytes, docx_bytes, file_stem = _build_agenda_artifacts(r["id"])
                    exp_pdf, exp_docx, exp_close = st.columns([2, 2, 1])
                    with exp_pdf:
                        st.download_button(
                            "⬇️ PDF",
                            data=pdf_bytes,
                            file_name=f"{file_stem}.pdf",
                            mime="application/pdf",
                            key=f"hist_ag_dlpdf_{r['id']}",
                            type="primary",
                            use_container_width=True,
                        )
                    with exp_docx:
                        st.download_button(
                            "⬇️ Word",
                            data=docx_bytes,
                            file_name=f"{file_stem}.docx",
                            mime=("application/vnd.openxmlformats-officedocument."
                                  "wordprocessingml.document"),
                            key=f"hist_ag_dldocx_{r['id']}",
                            use_container_width=True,
                        )
                    with exp_close:
                        if st.button("✕ Close", key=f"hist_ag_closex_{r['id']}",
                                     use_container_width=True):
                            del st.session_state[f"_show_agenda_export_{r['id']}"]
                            st.rerun()
                except Exception as exc:
                    st.error(f"Export failed: {exc}")
                    import traceback
                    st.code(traceback.format_exc())


def render_schedule():
    st.markdown(
        '<div class="brand-banner">📊 Project schedule — upload and parse</div>',
        unsafe_allow_html=True
    )
    if not project_id:
        st.warning("Pick a portfolio in the sidebar first.")
        return

    st.caption(
        "Upload a Castillo proposal **PDF** or a duration **.xlsx**. The parser "
        "extracts disciplines, phases, tasks, durations, dates, and prices."
    )

    uploaded = st.file_uploader(
        "Schedule file",
        type=["pdf", "xlsx", "xlsm"],
        help="Proposal PDF (page 1 'Project Schedule & Schedule of Value') "
             "or a duration .xlsx with 'Project Info' + 'Tasks' sheets.",
        key="schedule_uploader",
    )

    if uploaded is not None:
        try:
            parsed_sched = parse_schedule_file(uploaded.name, uploaded.read())
        except Exception as exc:
            st.error(f"Could not parse schedule: {exc}")
            return

        # Preview summary
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Format", parsed_sched.source_format.upper())
        c2.metric("Version", parsed_sched.version)
        c3.metric("Items", len(parsed_sched.items))
        if parsed_sched.total_price:
            c4.metric("Total", f"${parsed_sched.total_price:,}")
        else:
            c4.metric("Total", "—")

        col_a, col_b = st.columns(2)
        col_a.write(f"**Project start:** {parsed_sched.project_start_date or '—'}")
        col_b.write(f"**Total duration:** "
                    f"{parsed_sched.total_duration_days or '—'} working days")

        # Preview table
        import pandas as pd
        df = pd.DataFrame([{
            "Lvl": ["D", "P", "T"][min(i.indent_level, 2)],
            "Discipline": i.discipline,
            "Phase": i.phase,
            "Task": i.task,
            "Days": i.duration_days,
            "Start": i.start_date,
            "Finish": i.finish_date,
            "Price": i.price,
        } for i in parsed_sched.items])
        st.dataframe(df, use_container_width=True, hide_index=True, height=380)

        # Save form
        with st.form("save_schedule_form"):
            v_col, btn_col = st.columns([1, 1])
            with v_col:
                version_label = st.text_input(
                    "Version label", value=parsed_sched.version,
                    help="Stored on the Schedule row; used to track changes over time."
                )
            with btn_col:
                st.write("")
                st.write("")
                submit = st.form_submit_button("💾 Save project schedule", type="primary")
            if submit:
                try:
                    with session_scope() as session:
                        existing = (session.query(Schedule)
                                    .filter_by(project_id=project_id, version=version_label.strip())
                                    .first())
                        if existing:
                            st.warning(
                                f"Project schedule {version_label} already exists for this portfolio — "
                                "delete it below and re-upload to replace."
                            )
                        else:
                            sched = Schedule(
                                project_id=project_id,
                                version=version_label.strip() or "V1",
                                source_filename=parsed_sched.source_filename or uploaded.name,
                                source_format=parsed_sched.source_format,
                                project_start_date=parsed_sched.project_start_date,
                                total_duration_days=parsed_sched.total_duration_days,
                                total_price=parsed_sched.total_price,
                            )
                            session.add(sched)
                            session.flush()
                            for it in parsed_sched.items:
                                session.add(ScheduleItem(
                                    schedule_id=sched.id,
                                    order_index=it.order_index,
                                    indent_level=it.indent_level,
                                    discipline=it.discipline or None,
                                    phase=it.phase or None,
                                    task=it.task,
                                    duration_days=it.duration_days,
                                    start_date=it.start_date,
                                    finish_date=it.finish_date,
                                    price=it.price,
                                    is_milestone=it.is_milestone,
                                ))
                            st.success(
                                f"Saved schedule {sched.version} with "
                                f"{len(parsed_sched.items)} items."
                            )
                except Exception as exc:
                    st.error(f"Save failed: {exc}")

    # Existing schedules for this project
    st.markdown("---")
    st.subheader("Saved schedules")
    with session_scope() as session:
        existing = (session.query(Schedule)
                    .filter_by(project_id=project_id)
                    .order_by(Schedule.uploaded_at.desc())
                    .all())
        rows = [{
            "id": s.id,
            "version": s.version,
            "format": (s.source_format or "").upper(),
            "filename": s.source_filename,
            "start": s.project_start_date,
            "days": s.total_duration_days,
            "price": s.total_price,
            "items": len(s.items),
            "uploaded": s.uploaded_at,
        } for s in existing]

    if not rows:
        st.info("No project schedules saved for this portfolio yet.")
        return

    import pandas as pd
    st.dataframe(
        pd.DataFrame(rows).drop(columns=["id"]),
        use_container_width=True, hide_index=True,
    )

    # Delete control
    ids = [r["id"] for r in rows]
    labels = [f"{r['version']} · {r['filename']} ({r['items']} items)" for r in rows]
    del_idx = st.selectbox("Delete a schedule", options=range(len(ids)),
                           format_func=lambda i: labels[i], key="delete_schedule_idx")
    if st.button("🗑️ Delete selected schedule"):
        target_id = ids[del_idx]
        deleted_version = None
        with session_scope() as session:
            sched = session.get(Schedule, target_id)
            if sched:
                deleted_version = sched.version
                session.delete(sched)
        # st.rerun() raises RerunException; if called inside session_scope,
        # the context manager treats it as an error and rolls back. Run it
        # AFTER the with-block so the delete actually commits.
        if deleted_version:
            st.toast(f"Deleted schedule {deleted_version}.", icon="🗑️")
            st.rerun()
        else:
            st.error("Schedule not found.")


# ============================================================
# Route dispatch
# ============================================================
ROUTES = {
    "📥 Capture": render_capture,
    "📝 Review": render_review,
    "👁️ Preview": render_preview,
    "📤 Send": render_send,
    "📅 Next Agenda": render_next_agenda,
    "✅ Actions": render_actions,
    "📓 Notes": render_notes,
    "📚 History": render_history,
    "📊 Schedule": render_schedule,
}
ROUTES[st.session_state.nav]()

# ---- Sync URL to current navigational state ----
# Runs last so the URL reflects what's actually rendered. Defensive against
# missing pieces (e.g. no client yet) — only writes what's available.
_desired_qp = {}
_nav = st.session_state.get("nav")
if _nav in TAB_TO_SLUG:
    _desired_qp["tab"] = TAB_TO_SLUG[_nav]
_cur_client = st.session_state.get("sidebar_client_name")
if _cur_client:
    _desired_qp["client"] = _url_slug(_cur_client)
_cur_portfolio = st.session_state.get("sidebar_portfolio_name")
if _cur_portfolio:
    _desired_qp["portfolio"] = _url_slug(_cur_portfolio)
# Stage 2: entity-level deep links. Only writes when those keys are set —
# session_state defaults are None for both, so they self-clear when the
# user picks "✏️ New agenda" or "➕ Start new meeting".
_cur_agenda_id = st.session_state.get("na_agenda_id")
if _cur_agenda_id is not None:
    _desired_qp["agenda"] = str(_cur_agenda_id)
_cur_meeting_id = st.session_state.get("draft_meeting_id")
if _cur_meeting_id is not None:
    _desired_qp["meeting"] = str(_cur_meeting_id)
_sync_url_params(_desired_qp)

"""
SQLAlchemy session factory. Singleton engine for the app lifetime.
"""
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from config import database_url
from db.models import Base


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        url = database_url()
        # SQLite needs check_same_thread=False for Streamlit
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, echo=False)
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


# Castillo Engineering team — auto-seeded into the global roster on first run.
CASTILLO_TEAM = [
    ("Roashaael Mary John",   "RM", "Castillo Engineering"),
    ("Rick Castillo",         "RC", "Castillo Engineering"),
    ("Christopher Castillo",  "CC", "Castillo Engineering"),
    ("Gary Joseph",           "GJ", "Castillo Engineering"),
    ("Arun Ramadass",         "AR", "Castillo Engineering"),
    ("Manjil Puri",           "MP", "Castillo Engineering"),
    ("Brett Beattie",         "BB", "Castillo Engineering"),
]


def init_db():
    """Create all tables, apply lightweight in-place migrations, and seed the
    company-wide global roster. Safe to call repeatedly."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _bootstrap_migrations(engine)
    _seed_global_roster()


def _bootstrap_migrations(engine):
    """Add columns the model expects but legacy DBs don't have yet.
    SQLite `create_all()` doesn't ALTER existing tables, so this fills the gap.
    Each migration is idempotent — checks the column exists first."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "discussion_points" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("discussion_points")}
        if "parent_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE discussion_points "
                    "ADD COLUMN parent_id INTEGER REFERENCES discussion_points(id)"
                ))
    if "agendas" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("agendas")}
        if "meeting_duration_minutes" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE agendas "
                    "ADD COLUMN meeting_duration_minutes INTEGER DEFAULT 30"
                ))
        if "schedule_changes_json" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE agendas "
                    "ADD COLUMN schedule_changes_json JSON"
                ))
        if "schedule_version_override" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE agendas "
                    "ADD COLUMN schedule_version_override VARCHAR(20)"
                ))
    if "projects" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("projects")}
        if "sub_projects_json" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE projects ADD COLUMN sub_projects_json JSON"
                ))


def _seed_global_roster():
    """Ensure each member of CASTILLO_TEAM exists in `global_attendees`.
    Skips members that are already present (matched on full_name)."""
    from db.models import GlobalAttendee
    SessionLocal = get_session_factory()
    with SessionLocal() as s:
        existing_names = {r[0] for r in s.query(GlobalAttendee.full_name).all()}
        for full_name, initials, org in CASTILLO_TEAM:
            if full_name in existing_names:
                continue
            s.add(GlobalAttendee(
                full_name=full_name,
                initials=initials,
                organization=org,
            ))
        s.commit()


@contextmanager
def session_scope() -> Session:
    """Context-managed session with commit/rollback handling."""
    SessionLocal = get_session_factory()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

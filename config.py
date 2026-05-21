"""
Central configuration. Loads .env, exposes typed accessors.
Brand colors and constants used throughout the app live here.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


# ============================================================
# Castillo brand
# ============================================================
class BrandColors:
    """Castillo Engineering brand palette, lifted from the official
    Branding Style Guide (V2). Used in PDF/docx/Streamlit — never inline
    hex codes elsewhere."""
    # ---- Primary palette ----
    RED         = "#ad1f2b"     # primary brand red
    DARK_RED    = "#991f2b"     # darker accent / hover state
    NEAR_BLACK  = "#333132"     # warm near-black — official guide value
    DARK_GRAY   = "#4d4d4f"     # body text on light backgrounds
    LIGHT_GRAY  = "#bcbec0"     # borders / inactive UI
    NEAR_WHITE  = "#e6e7e8"     # softest gray, almost-white backgrounds
    BROWN       = "#5e4b40"     # tertiary accent (used sparingly)

    # ---- Secondary palette ----
    BRIGHT_RED   = "#e12a3f"    # punchier red, for emphasis on light bg
    BLUE         = "#1aa6c9"    # the brand-spec blue (status: open, info)
    GREEN        = "#278747"    # status: completed, success
    BRIGHT_GREEN = "#4ab751"    # secondary success / positive accent
    GOLD         = "#c7bb2e"    # status: pending, AcroForm field borders

    # ---- Status pill colors (background, text, border) ----
    # These are derived tints — soft fills with the brand color as the
    # border so the pill reads as "branded" but doesn't shout.
    STATUS_OPEN      = ("#fce8ea", "#791f1f", "#ad1f2b")
    STATUS_PENDING   = ("#fdeac0", "#5e3f00", "#c7bb2e")
    STATUS_COMPLETED = ("#c7e9a3", "#1a3a04", "#278747")
    STATUS_CANCELLED = ("#e6e7e8", "#1a1a1a", "#888780")


BRAND_FONT_PRIMARY = "Jost"
BRAND_FONT_FALLBACK = "Helvetica"


# ============================================================
# Mode
# ============================================================
def is_local_dev() -> bool:
    """True if running with SQLite + local files (no SharePoint / Postgres)."""
    return os.getenv("LOCAL_DEV_MODE", "true").lower() == "true"


# ============================================================
# OpenAI
# ============================================================
def openai_api_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Add it to your .env file."
        )
    return key


def openai_model() -> str:
    """Pass-through to OpenAI client — set whatever model string you want."""
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


# ============================================================
# Database
# ============================================================
def database_url() -> str:
    if is_local_dev():
        sqlite_path = os.getenv("SQLITE_PATH", "data/castillo.db")
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{sqlite_path}"
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL not set. Required when LOCAL_DEV_MODE=false."
        )
    return url


# ============================================================
# Storage
# ============================================================
def local_output_dir() -> Path:
    p = Path(os.getenv("LOCAL_OUTPUT_DIR", "data/outputs"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def sharepoint_config() -> dict:
    return {
        "tenant_id": os.getenv("AZURE_TENANT_ID", ""),
        "client_id": os.getenv("AZURE_CLIENT_ID", ""),
        "client_secret": os.getenv("AZURE_CLIENT_SECRET", ""),
        "site_id": os.getenv("SHAREPOINT_SITE_ID", ""),
        "drive_id": os.getenv("SHAREPOINT_DRIVE_ID", ""),
        "root_folder": os.getenv("SHAREPOINT_ROOT_FOLDER", "Castillo Meeting Tool"),
    }


# ============================================================
# App
# ============================================================
APP_TITLE = os.getenv("APP_TITLE", "PMO 360")
APP_TAGLINE = os.getenv("APP_TAGLINE", "Castillo Engineering Project Management Office")
TOOL_NAME = os.getenv("TOOL_NAME", "Meeting Minutes")
APP_PORT = int(os.getenv("APP_PORT", "8501"))
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "America/New_York")
SEND_FROM_EMAIL = os.getenv("SEND_FROM_EMAIL", "pmo@castilloengineering.com")

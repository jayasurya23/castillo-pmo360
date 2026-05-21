"""
Smoke-level unit tests. Run: pytest tests/
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date

from llm.providers import (
    ParsedMeeting, ParsedAttendee, ParsedAgendaItem,
    ParsedDiscussionPoint, ParsedActionItem,
)


def test_parsed_meeting_schema():
    """ParsedMeeting accepts valid data."""
    pm = ParsedMeeting(
        attendees=[ParsedAttendee(full_name="X", initials="X", organization="Y")],
        agenda_items=[ParsedAgendaItem(text="Topic", discipline="Electrical")],
        discussion_points=[ParsedDiscussionPoint(label="L", content="C")],
        action_items=[ParsedActionItem(text="A", owner="O", due_date="2025-11-10", status="open")],
    )
    assert len(pm.attendees) == 1
    assert pm.action_items[0].status == "open"


def test_brand_colors_loaded():
    from config import BrandColors
    assert BrandColors.RED == "#ad1f2b"
    assert BrandColors.NEAR_BLACK == "#1a1a1a"
    assert BrandColors.GOLD == "#c7bb2e"


def test_local_dev_mode_default():
    import os
    os.environ.pop("LOCAL_DEV_MODE", None)
    from config import is_local_dev
    # Reload because config is loaded at import time
    import importlib, config
    importlib.reload(config)
    assert config.is_local_dev() is True

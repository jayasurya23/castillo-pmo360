from docgen.document_builder import (
    generate_meeting_minutes_docx as generate_meeting_minutes_docx_from_scratch,
    generate_premeeting_agenda_docx,
)
from docgen.template_filler import generate_meeting_minutes_from_template
from docgen.action_log import generate_action_items_xlsx
from docgen.pdf_builder import (
    generate_meeting_minutes_pdf,
    generate_premeeting_agenda_pdf,
    add_status_form_fields,
)

# Public alias — template-based generator is the primary path now. The
# from-scratch builder is kept under an alias for reference / fallback.
generate_meeting_minutes_docx = generate_meeting_minutes_from_template

__all__ = [
    "generate_meeting_minutes_docx",
    "generate_meeting_minutes_from_template",
    "generate_meeting_minutes_docx_from_scratch",
    "generate_premeeting_agenda_docx",
    "generate_premeeting_agenda_pdf",
    "generate_meeting_minutes_pdf",
    "generate_action_items_xlsx",
    "add_status_form_fields",
]

"""What the browser extension sends (form fields) and gets back (actions)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FieldOption(BaseModel):
    value: str
    label: str = ""


class FormField(BaseModel):
    id: str
    label: str = ""
    name: str = ""
    placeholder: str = ""
    autocomplete: str = ""
    # text, email, tel, url, number, date, month, textarea, select, combobox (custom dropdown), radio, checkbox, file
    type: str = "text"
    required: bool = False
    options: list[FieldOption] = Field(default_factory=list)
    section: Literal["", "education", "experience"] = ""
    section_index: int = 0
    entry_hint: str = ""  # what the site already filled in this entry ("Master of Science University of Michigan")
    value: str = ""  # what the field holds now (Google fills work entries from the resume, not always rightly)
    maxlength: int | None = None
    context: str = ""  # nearby text: fieldset legend, helper text, the question a radio group belongs to


class Action(BaseModel):
    field_id: str
    # fill: type text | select: choose an option (value = option value) | check: tick a checkbox
    # combobox: pick the option whose text matches `value` in a custom dropdown
    # upload: attach the resume PDF | draft: free text a model may write, shown for review
    # needs_you: the person must answer | skip: leave empty
    # cover: a cover letter written for this posting goes here (an upload gets the PDF, a text box the text)
    # transcript: the person's own transcript PDF (profile/transcript.pdf) is attached
    kind: Literal["fill", "select", "combobox", "check", "upload", "draft", "cover", "transcript", "needs_you", "skip"]
    value: str | None = None
    source: Literal["profile", "answer_sheet", "llm", "none"] = "none"
    category: str | None = None  # why it is sensitive, if it is
    reason: str = ""

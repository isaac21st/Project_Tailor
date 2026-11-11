"""Structured resume. The renderer only ever sees this, never free-form text.

Bullet and line text may use two inline marks: **bold** and *italic*.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Link(BaseModel):
    text: str
    url: str


class ContactItem(BaseModel):
    text: str
    url: str | None = None


class Header(BaseModel):
    name: str
    contact: list[ContactItem]


class Role(BaseModel):
    title: str  # italic line: degree or job title
    dates: str
    bullets: list[str] = Field(default_factory=list)


class Entry(BaseModel):
    title: str  # bold line: school, employer or project
    location: str = ""
    link: Link | None = None  # e.g. the "CASE STUDY" link shown after the title
    roles: list[Role]  # several roles under one employer render as extra italic rows


class LabeledLine(BaseModel):
    label: str  # e.g. "Analytics"
    text: str


class Section(BaseModel):
    heading: str
    entries: list[Entry] = Field(default_factory=list)
    lines: list[LabeledLine] = Field(default_factory=list)


class Resume(BaseModel):
    header: Header
    summary: str | None = None
    sections: list[Section]

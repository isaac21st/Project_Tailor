"""Parse LinkedIn's "Save to PDF" profile export (never scrape LinkedIn).

The plain text of that PDF cannot tell an organization name from the last line of a wrapped bullet, but
the font sizes can: in the export, organizations are 12 pt, titles 11.5 pt, and everything else is 10.5 pt.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

_DATE_LINE = re.compile(
    r"^(?P<start>(?:[A-Za-z]+ )?\d{4})\s*-\s*(?P<end>Present|(?:[A-Za-z]+ )?\d{4})\s*(?:\((?P<span>[^)]*)\))?$"
)
_PAGE_FOOTER = re.compile(r"^Page \d+ of \d+$")
_SECTION_NAMES = {"Contact", "Top Skills", "Summary", "Experience", "Education", "Languages", "Certifications",
                  "Honors-Awards", "Publications", "Projects", "Skills"}


@dataclass
class Position:
    org: str
    title: str
    start: str = ""
    end: str = ""
    span: str = ""
    location: str = ""
    bullets: list[str] = field(default_factory=list)


@dataclass
class EducationEntry:
    school: str
    detail: str  # degree line(s) as printed, e.g. "Master of Science, HCI · (August 2024 - May 2027)"
    lines: list[str] = field(default_factory=list)


@dataclass
class LinkedInProfile:
    name: str = ""
    headline: str = ""
    location: str = ""
    summary: str = ""
    top_skills: list[str] = field(default_factory=list)
    contact: list[str] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    education: list[EducationEntry] = field(default_factory=list)


def _clean(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[-]", " ", s)
    return " ".join(s.split())


def read_lines(path: Path) -> list[tuple[float, str]]:
    """(font size, text) per printed line, top to bottom, page by page."""
    reader = PdfReader(str(path))
    lines: list[tuple[float, str]] = []
    for page in reader.pages:
        frags: list[tuple[float, float, str]] = []  # (y, size, text)

        def visit(text, cm, tm, fd, fs, _frags=frags):
            if not text.strip():
                return
            size = round(fs * abs(tm[3] if tm else 1) * abs(cm[3] if cm else 1) * 2) / 2
            y = round(tm[5] if tm else 0)
            _frags.append((y, size, text))

        page.extract_text(visitor_text=visit)
        # fragments arrive in reading order; merge those that continue the same printed line
        cur_y, cur_size, cur_text = None, None, ""
        for y, size, text in frags:
            if cur_y is not None and (y != cur_y or size != cur_size):
                lines.append((cur_size, _clean(cur_text)))
                cur_text = ""
            cur_y, cur_size, cur_text = y, size, cur_text + text
        if cur_y is not None:
            lines.append((cur_size, _clean(cur_text)))
    return [(s, t) for s, t in lines if t]


def parse_profile(path: str | Path) -> LinkedInProfile:
    lines = [(s, t) for s, t in read_lines(Path(path)) if not _PAGE_FOOTER.match(t)]
    prof = LinkedInProfile()

    # Sections are the largest text after the name (about 15.8 pt).
    section = ""
    pos: Position | None = None
    org = ""
    edu: EducationEntry | None = None
    summary: list[str] = []
    name_seen = False

    header: list[str] = []  # 12 pt lines between the name and "Summary": headline lines, then location
    for size, text in lines:
        if size >= 25 and not name_seen:
            prof.name, name_seen, section = text, True, "header"
            continue
        if size >= 13 and text in _SECTION_NAMES:
            section = text
            continue
        if section == "Contact":
            prof.contact.append(text)
        elif section == "Top Skills":
            prof.top_skills.append(text)
        elif section == "header":
            header.append(text)
        elif section == "Summary":
            summary.append(text)
        elif section == "Experience":
            if size == 12.0:
                org, pos = text, None
            elif size == 11.0 and re.fullmatch(r"\d+ (?:years?|months?)(?: \d+ months?)?", text):
                continue  # "2 years" group total under a multi-role organization
            elif size == 11.5:
                pos = Position(org=org, title=text)
                prof.positions.append(pos)
            elif pos is not None:
                if (m := _DATE_LINE.match(text)) and not pos.start:
                    pos.start, pos.end, pos.span = m["start"], m["end"], m["span"] or ""
                elif text.startswith("-"):
                    pos.bullets.append(text.lstrip("- ").strip())
                elif pos.bullets:
                    pos.bullets[-1] += " " + text  # wrapped bullet line
                elif not pos.location:
                    pos.location = text
        elif section == "Education":
            if size == 12.0:
                # a school group header can be printed glued to its first entry:
                # "State UniversityState University - School of Business"
                if (m := re.match(r"^(.+?)(\1 - .+)$", text)):
                    text = m.group(2)
                edu = EducationEntry(school=text, detail="")
                prof.education.append(edu)
            elif edu is not None:
                edu.lines.append(text)

    if header:
        prof.location = header[-1]
        prof.headline = " ".join(header[:-1])
    prof.summary = " ".join(summary)
    for e in prof.education:
        e.detail = " ".join(e.lines)
    return prof

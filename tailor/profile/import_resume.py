"""Read a resume (PDF, Word or plain text) into Tailor's structured form, so a new person need not write JSON.

Rules only: nothing is worded, added or guessed beyond splitting the person's own lines into their parts, so the result
stays their words. Whatever it is unsure of comes back in `warnings` for them to check; it is a first draft of the
resume every tailored version is checked against, so they read it once before relying on it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..resume.schema import ContactItem, Entry, Header, LabeledLine, Resume, Role, Section

_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
_SEASON = r"(?:Winter|Spring|Summer|Fall|Autumn)\s+\d{4}"
_WHEN = rf"(?:{_MONTH}\s+\d{{4}}|{_SEASON}|\d{{1,2}}/\d{{4}}|\d{{4}})"
_DATES = re.compile(rf"(?:Expected\s+{_WHEN}|{_WHEN}\s*(?:-|–|—|to)\s*(?:{_WHEN}|Present|Current|Now)|"
                    rf"{_MONTH}\s+\d{{1,2}}\s*(?:-|–|—)\s*\d{{1,2}},\s*\d{{4}}|{_MONTH}\s+\d{{4}}|{_SEASON})", re.I)
_BULLET = re.compile(r"^\s*(?:[•·▪◦‣*●○■–-]|\d+\.)\s+")
_PLACE = re.compile(r"(?:^|[,|·]\s*|\s{2,})((?:[A-Z][A-Za-z.'-]+(?: [A-Z][A-Za-z.'-]+)*,\s*[A-Z]{2}(?:,\s*USA?)?)|Remote|Hybrid)\s*$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d?[\s.(-]*\d{3}[\s.)-]*\d{3}[\s.-]*\d{4}")
_LINK = re.compile(r"(?:https?://)?(?:www\.)?[\w-]+\.(?:com|net|org|io|dev|me|co|ai|edu)(?:/[\w./~-]*)?", re.I)
_DEGREE_OR_ROLE = re.compile(r"\b(?:bachelor|master|associate|doctor|ph\.?d|mba|b\.?a|b\.?s|m\.?s|manager|analyst|engineer|director|"
                             r"coordinator|lead|teacher|nurse|consultant|intern|assistant|specialist)\b", re.I)
_HEADINGS = (("EDUCATION", r"education|academic"), ("PROFESSIONAL EXPERIENCE", r"(?:work|professional|employment|relevant)?\s*(?:experience|employment|history)"),
             ("PROJECT EXPERIENCE", r"(?:\w+\s+)?projects?(?:\s+experience)?|leadership|activities|volunteer\w*"),
             ("SKILLS", r"(?:technical\s+|core\s+)?skills?(?:\s*(?:&|and)\s*\w+)*|technologies|tools|interests|certifications?|awards?|languages"))


@dataclass
class Imported:
    resume: Resume
    warnings: list[str] = field(default_factory=list)


def read_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join((p.extract_text(extraction_mode="layout") or p.extract_text() or "") for p in PdfReader(str(path)).pages)
    if suffix in (".docx",):
        import docx
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    return path.read_text(encoding="utf-8", errors="ignore")


def _date_of(line: str) -> re.Match | None:
    """A line's dates: at its end or start, or set apart by a gap. A year inside a sentence ("2015–2024) into charts") is not."""
    for m in _DATES.finditer(line):
        before, after = line[:m.start()], line[m.end():].strip()
        if not after or not before.strip() or before.endswith("  ") or re.match(r"\s*[|·,]", after) or re.search(r"[|·]\s*$", before):
            return m
    return None


def _heading(line: str) -> str | None:
    t = re.sub(r"[:\s]+$", "", line.strip())
    if not t or len(t) > 45 or _date_of(t) or _BULLET.match(line):
        return None
    if not (t.upper() == t or re.sub(r"\b(?:and|of|the)\b", "", t).strip().istitle() or len(t.split()) <= 3):
        return None
    for name, pat in _HEADINGS:
        if re.fullmatch(pat, t.strip(), re.I):
            return name
    # a heading of the person's own ("Consulting & Project Experience", "Research Experience")
    if len(t.split()) <= 6 and "," not in t and not re.search(r"\d", t):
        if re.search(r"\bprojects?(?: experience)?$", t, re.I):
            return "PROJECT EXPERIENCE"
        if re.search(r"\bexperience$", t, re.I):
            return "PROFESSIONAL EXPERIENCE"
    return None


def _header(lines: list[str]) -> Header:
    name = next((re.sub(r"\s{2,}.*", "", x.strip()) for x in lines if x.strip()), "")
    joined = " | ".join(x.strip() for x in lines[1:] if x.strip()) + " | " + " ".join(lines[:1])
    contact: list[ContactItem] = []
    if (m := _PHONE.search(joined)):
        contact.append(ContactItem(text=m[0].strip()))
    if (m := _EMAIL.search(joined)):
        contact.append(ContactItem(text=m[0], url=f"mailto:{m[0]}"))
    rest = _EMAIL.sub(" ", joined)
    for m in _LINK.finditer(rest):
        link = m[0].rstrip("/.")
        contact.append(ContactItem(text=re.sub(r"^https?://(?:www\.)?", "", link), url=link if link.startswith("http") else f"https://{link}"))
    return Header(name=name, contact=contact)


def _split_title(text: str) -> tuple[str, str]:
    """"Acme Corp | Analyst" -> both parts; a line with no separator is one part."""
    parts = [p.strip() for p in re.split(r"\s+[|·•–—-]\s+|\s{3,}", text.strip()) if p.strip()]
    return (parts[0], parts[1]) if len(parts) >= 2 else (text.strip(), "")


def _parse_entries(lines: list[str], warnings: list[str], heading: str) -> list[Entry]:
    # bullets first, joining wrapped lines: a line that is not a bullet and has no date near it continues the bullet above
    items: list[tuple[str, str]] = []
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if not line.strip():
            continue
        if _BULLET.match(line):
            items.append(("b", _BULLET.sub("", line).strip()))
            continue
        following = next((x for x in lines[i + 1:] if x.strip()), "")
        indented = len(line) - len(line.lstrip()) >= 2 and bool(items) and items[-1][0] == "b"
        starts_entry = bool(_date_of(line)) or (line.strip()[:1].isupper() and not indented and
                                                bool(_date_of(following)) and not _BULLET.match(following))
        if items and items[-1][0] == "b" and not starts_entry:
            joiner = "" if items[-1][1].endswith("-") else " "  # "post-" at a line's end, "redesign" on the next
            items[-1] = ("b", items[-1][1] + joiner + line.strip())
        else:
            items.append(("h", line))
    entries: list[Entry] = []
    head: list[str] = []

    def closes_entry(line: str) -> bool:
        """Entries with no bullets run together ("Howard / Master's, 2020 / Spelman / Bachelor's, 2016"): a line naming a
        new date, or a new line once the title, role and date are all there, starts the next one."""
        dated = any(_date_of(h) for h in head)
        return bool(dated and (_date_of(line) or (len(head) >= 2 and not _DEGREE_OR_ROLE.search(line))))

    for kind, text in items + [("end", "")]:
        if kind == "h" and not (head and closes_entry(text)):
            head.append(text)
            continue
        if head:
            dates = ""
            kept = []
            for h in head:
                if not dates and (m := _date_of(h)):
                    dates = re.sub(r"\s+", " ", m[0]).replace("–", "-").replace("—", "-").replace(" to ", " - ").strip()
                    h = (h[:m.start()] + "   " + h[m.end():]).strip(" ,|·-–—")
                kept.append(h)
            kept = [k for k in kept if k.strip()]
            location = ""
            for j, k in enumerate(kept):
                if (m := _PLACE.search(k)):
                    location = m[1]
                    kept[j] = k[:m.start()].strip(" ,|·-–—")
            kept = [k for k in kept if k.strip()]
            if len(kept) == 1 and entries and entries[-1].roles and heading != "EDUCATION" and not _split_title(kept[0])[1] and not location:
                entries[-1].roles.append(Role(title=kept[0].strip(), dates=dates))  # another role under the same employer
            else:
                first = kept[0] if kept else ""
                title, role = (first.strip(), kept[1].strip()) if len(kept) >= 2 else _split_title(first)
                if not role:
                    warnings.append(f"No role or degree line found for '{title[:60]}': check it.")
                if not dates:
                    warnings.append(f"No dates found for '{title[:60]}': check them.")
                entries.append(Entry(title=title, location=location, roles=[Role(title=role, dates=dates)]))
            head = []
        if kind == "h":
            head = [text]
        if kind == "b":
            if entries:
                entries[-1].roles[-1].bullets.append(text)
            else:
                warnings.append(f"A bullet with no job above it was left out: '{text[:60]}'")
    return entries


def _parse_skills(lines: list[str], label: str = "Skills") -> list[LabeledLine]:
    out: list[LabeledLine] = []
    for raw in lines:
        t = _BULLET.sub("", raw).strip()
        if not t:
            continue
        if ":" in t[:40]:
            label, text = t.split(":", 1)
            out.append(LabeledLine(label=label.strip(), text=text.strip()))
        elif out:
            out[-1].text = (out[-1].text + " " + t).strip()
        else:
            out.append(LabeledLine(label=label, text=t))
    return out


def parse_resume_text(text: str) -> Imported:
    lines = [re.sub(r" ", " ", x).rstrip() for x in (text or "").splitlines()]
    warnings: list[str] = []
    spots = [(i, name) for i, x in enumerate(lines) if (name := _heading(x))]
    if not spots:
        raise ValueError("No sections found (Education, Experience, Skills). Is this a resume with readable text?")
    header = _header(lines[: spots[0][0]])
    if not header.name:
        warnings.append("No name found at the top: add it.")
    if not any(c.url and c.url.startswith("mailto:") for c in header.contact):
        warnings.append("No email found: add it.")
    sections: dict[str, Section] = {}
    for n, (i, name) in enumerate(spots):
        body = lines[i + 1: spots[n + 1][0] if n + 1 < len(spots) else len(lines)]
        sec = sections.setdefault(name, Section(heading=name))
        if name == "SKILLS":
            sec.lines += _parse_skills(body, re.sub(r"[:\s]+$", "", lines[i].strip()).capitalize())
        else:
            sec.entries += _parse_entries(body, warnings, name)
    for needed in ("EDUCATION", "PROFESSIONAL EXPERIENCE"):
        if needed not in sections:
            warnings.append(f"No {needed.lower()} section found: check the result.")
    order = ["EDUCATION", "PROFESSIONAL EXPERIENCE", "PROJECT EXPERIENCE", "SKILLS"]
    return Imported(Resume(header=header, sections=[sections[k] for k in order if k in sections]), warnings)


def import_resume(path: Path) -> Imported:
    return parse_resume_text(read_text(Path(path)))

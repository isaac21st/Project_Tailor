"""The facts application forms ask for, derived from the (confirmed) resume.

Nothing here is invented: anything the resume does not say (street address, city, GPA) stays None, and the
form filler turns a required-but-missing value into a "needs you" flag instead of guessing.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from ..resume.schema import Resume

_MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
                "october", "november", "december"]
MONTHS = {**{m: i for i, m in enumerate(_MONTH_NAMES, 1)}, **{m[:3]: i for i, m in enumerate(_MONTH_NAMES, 1)},
          "sept": 9}
_M = r"(?P<{}>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
_DASH = r"\s*[-–—]\s*"
_DEGREES = ["Master of Business Administration", "Master of Science", "Master of Arts", "Bachelor of Science",
            "Bachelor of Arts", "Associate of Arts", "Associate of Science", "Doctor of Philosophy"]
_ACRONYMS = {"IPO", "UX", "AI", "MAP", "I/DD", "HFH", "MI", "CA", "FL", "USA", "US", "LLC", "MBA", "MSI"}
_SMALL = {"of", "and", "the", "at", "for", "in", "on"}


class YearMonth(BaseModel):
    year: int
    month: int | None = None


class Education(BaseModel):
    school: str
    degree: str
    field: str = ""
    location: str = ""
    start: YearMonth | None = None  # resumes leave it out: from profile_overrides.json "education_starts"
    form_field: str = ""  # the field of study as forms list it ("Business Administration"), the person's pick
    end: YearMonth | None = None
    expected: bool = False
    bullets: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    employer: str
    title: str
    location: str = ""
    start: YearMonth | None = None
    end: YearMonth | None = None
    current: bool = False
    kind: Literal["job", "project"] = "job"
    bullets: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str
    linkedin_url: str | None = None
    portfolio_url: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    address: str | None = None
    postal_code: str | None = None
    # entered once on the Desk's My info tab (profile_overrides.json "extras"); never guessed
    preferred_name: str | None = None
    github_url: str | None = None
    phone_type: str | None = None  # Workday: "Mobile", "Home"...
    how_heard: str | None = None  # the usual answer to "How did you hear about us?"
    location_preferences: list[str] = Field(default_factory=list)  # ranked offices ("San Francisco", "New York"...)
    available_start: str | None = None  # earliest start date for a new role, ISO ("2027-05-01")
    # questions many forms ask that only the person can answer, entered once on My info (blank = asked on each form)
    gpa: str | None = None  # of the current degree ("3.8"); blank = not shared
    # another degree's GPA, by words of its school's name ({"State University": "3.4"}: the person's own figure, 2026-10-10)
    education_gpas: dict[str, str] = Field(default_factory=dict)
    high_school_year: str | None = None  # high school graduation year
    languages: str | None = None  # "English (native), Mandarin (conversational)"
    interviewed_at: str | None = None  # companies interviewed with before, comma-separated, or "none"
    relatives_at: str | None = None  # companies where a relative works, or "none"
    government_official: str | None = None  # "No" / "Yes": a government official, or related to one
    conflict_of_interest: str | None = None  # "No" / "Yes"
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def current_job(self) -> Experience | None:
        return next((e for e in self.experience if e.current and e.kind == "job"), None) or next(
            (e for e in self.experience if e.kind == "job"), None)

    def facts_text(self) -> str:
        """Every fact a model may draw on, as plain text (also used to check a draft for invented numbers)."""
        lines = [f"Name: {self.full_name}"]
        for e in self.education:
            lines.append(f"Education: {e.degree}, {e.field}, {e.school}" + (" (expected)" if e.expected else ""))
        for x in self.experience:
            span = f"{_fmt(x.start)} to {'present' if x.current else _fmt(x.end)}"
            lines.append(f"{'Project' if x.kind == 'project' else 'Role'}: {x.title}, {x.employer} ({span})")
            lines += [f"  - {b}" for b in x.bullets]
        if self.skills:
            lines.append("Skills: " + ", ".join(self.skills))
        return "\n".join(lines)


def _fmt(ym: YearMonth | None) -> str:
    if ym is None:
        return "?"
    return f"{_MONTH_NAMES[ym.month - 1].title()} {ym.year}" if ym.month else str(ym.year)


def plain(text: str) -> str:
    """Drop the **bold** / *italic* marks used in resume text."""
    return re.sub(r"\*+", "", text).strip()


def titleize(s: str, overrides: dict[str, str] | None = None) -> str:
    """ALL-CAPS resume headings -> normal case for forms ("ACME RENTAL GROUP" -> "Acme Rental Group")."""
    if overrides and s in overrides:
        return overrides[s]
    letters = [c for c in s if c.isalpha()]
    if not letters or not all(c.isupper() for c in letters):
        return s
    out = []
    for i, word in enumerate(s.split(" ")):
        parts = []
        for piece in word.split("-"):
            core = piece.strip(",.;:")
            if core in _ACRONYMS:
                parts.append(piece)
            elif core.lower() in _SMALL and i > 0:
                parts.append(piece.lower())
            elif not any(c.isalpha() for c in piece):
                parts.append(piece)  # "&", "+"
            else:
                parts.append(piece.capitalize())
        out.append("-".join(parts))
    return " ".join(out)


def _ym(month: str | None, year: str) -> YearMonth:
    return YearMonth(year=int(year), month=MONTHS[month.lower().rstrip(".")] if month else None)


def parse_dates(text: str) -> tuple[YearMonth | None, YearMonth | None, bool, bool]:
    """(start, end, current, expected) from resume date text such as 'June 2025 – September 2025',
    'May 2026 - Present', 'February 1 – 6, 2026' or 'Expected May 2027'."""
    t = text.strip()
    if (m := re.search(r"Expected\s+" + _M.format("m") + r"\s+(?P<y>\d{4})", t, re.I)):
        return None, _ym(m["m"], m["y"]), False, True
    rng = re.search(_M.format("m1") + r"\s+(?P<y1>\d{4})" + _DASH + r"(?:(?P<pres>Present)|" + _M.format("m2") +
                    r"\s+(?P<y2>\d{4}))", t, re.I)
    if rng:
        start = _ym(rng["m1"], rng["y1"])
        if rng["pres"]:
            return start, None, True, False
        return start, _ym(rng["m2"], rng["y2"]), False, False
    if (m := re.search(_M.format("m") + r"\s+\d{1,2}" + _DASH + r"\d{1,2},\s*(?P<y>\d{4})", t, re.I)):
        ym = _ym(m["m"], m["y"])
        return ym, ym, False, False
    if (m := re.search(_M.format("m") + r"\s+(?P<y>\d{4})", t, re.I)):
        return None, _ym(m["m"], m["y"]), False, False
    return None, None, False, False


def _start_of(school: str, starts: dict[str, str] | None) -> YearMonth | None:
    value = (starts or {}).get(school)
    if not value or not (m := re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?", value.strip())):
        return None
    return YearMonth(year=int(m[1]), month=int(m[2]) if m[2] else None)


def _split_degree(line: str) -> tuple[str, str]:
    for d in _DEGREES:
        if line.startswith(d):
            rest = re.sub(r"^(?:\s+in\s+|,\s*|\s+)", "", line[len(d):])
            return d, rest.strip()
    head, _, tail = line.partition(",")
    return head.strip(), tail.strip()


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside parentheses."""
    items, depth, cur = [], 0, ""
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur.strip())
    return items


def profile_from_resume(resume: Resume, overrides: dict[str, str] | None = None,
                        extras: dict | None = None, education_starts: dict[str, str] | None = None,
                        education_order: list[str] | None = None,
                        fields_of_study: dict[str, str] | None = None) -> Profile:
    """Build a Profile. `extras` may carry values the resume lacks (city, state, address...), from the owner;
    `education_starts` maps a resume school to its start month ("2024-08"), which resumes do not show.
    `education_order` lists words of school names in the order forms should list them (the person's choice: their
    master's program before their MBA, say, though the resume lists the MBA first); schools it does not name keep their place
    after the named ones."""
    head = resume.header
    email = next((c.text for c in head.contact if c.url and c.url.startswith("mailto:")), "")
    phone = next((c.text for c in head.contact if not c.url and re.search(r"\d{3}", c.text)), "")
    linkedin = next((c.url for c in head.contact if c.url and "linkedin.com" in c.url), None)
    portfolio = next((c.url for c in head.contact if c.url and c.url.startswith("http")
                      and "linkedin.com" not in c.url), None)
    first, _, last = head.name.partition(" ")

    education: list[Education] = []
    experience: list[Experience] = []
    skills: list[str] = []
    for sec in resume.sections:
        heading = sec.heading.upper()
        if heading.startswith("EDUCATION"):
            for e in sec.entries:
                for r in e.roles:
                    degree, field = _split_degree(r.title)
                    _, end, _, expected = parse_dates(r.dates)
                    start = _start_of(e.title, education_starts)
                    education.append(Education(school=e.title, degree=degree, field=field, location=e.location,
                                               start=start, end=end, expected=expected, bullets=[plain(b) for b in r.bullets]))
        elif "EXPERIENCE" in heading:
            kind = "project" if "PROJECT" in heading else "job"
            for e in sec.entries:
                for r in e.roles:
                    start, end, current, _ = parse_dates(r.dates)
                    experience.append(Experience(
                        employer=titleize(e.title, overrides), title=r.title, location=e.location, start=start,
                        end=end, current=current, kind=kind, bullets=[plain(b) for b in r.bullets]))
        elif heading.startswith("SKILLS"):
            for line in sec.lines:
                if line.label.lower() != "awards":
                    skills += [plain(s) for s in _split_top_level(line.text)]

    for e in education:  # {"State University": "History"}: words of the school name -> the field forms get
        e.form_field = next((v for k, v in (fields_of_study or {}).items() if k.lower() in e.school.lower()), "")
    if education_order:
        def rank(e: Education) -> int:
            return next((i for i, w in enumerate(education_order) if w.lower() in e.school.lower()), len(education_order))
        education.sort(key=rank)  # stable: unnamed schools keep the resume's order
    data = dict(first_name=first, last_name=last, email=email, phone=phone, linkedin_url=linkedin,
                portfolio_url=portfolio, education=education, experience=experience, skills=skills)
    data.update(extras or {})
    return Profile(**data)


# ---- per posting: the person's cities in the order this posting offers them ----

BAY_AREA = ["San Francisco", "South San Francisco", "Mountain View", "San Jose", "Sunnyvale", "Palo Alto", "San Bruno",
            "Menlo Park", "Redwood City", "Cupertino", "Santa Clara", "Oakland", "San Mateo", "Foster City"]
_PLACES = BAY_AREA + ["New York", "Chicago", "Seattle", "Boston", "Austin", "Los Angeles", "Washington", "Atlanta",
                      "Denver", "Dallas", "Houston", "Miami", "Philadelphia", "Remote"]


def rank_locations(preferences: list[str], posting_text: str) -> list[str]:
    """The person's cities first in the order the posting lists its offices, then the rest of theirs. A Bay Area office
    counts as San Francisco (Google's "Mountain View, New York, San Bruno or San Jose": the person picked Mountain
    View, 2026-10-06), and is named as the posting names it."""
    text = posting_text or ""
    found = sorted(((m.start(), place) for place in _PLACES
                    for m in [re.search(rf"\b{re.escape(place)}\b", text)] if m), key=lambda t: t[0])
    mine = {p.lower() for p in preferences}
    out: list[str] = []
    for _, place in found:
        if place.lower() in mine or (place in BAY_AREA and "san francisco" in mine):
            if place not in out:
                out.append(place)
    if "San Francisco" in out:  # San Francisco itself is offered (Databricks, 2026-10-07): it leads its Bay Area neighbours
        bay = [x for x in out if x in BAY_AREA and x != "San Francisco"]
        if bay and out.index("San Francisco") > out.index(bay[0]):
            out.remove("San Francisco")
            out.insert(out.index(bay[0]), "San Francisco")
    return out + [p for p in preferences if p not in out and not (p == "San Francisco" and any(x in BAY_AREA for x in out))]

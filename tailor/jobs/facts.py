"""Facts that regexes read more reliably than a small model: ids, travel, sponsorship stance,
deadlines, years-of-experience floors, salary, MBA requirements.

These feed the fit check (e.g. "needs 6+ years, you have ~3") without any model call, so a
model can never talk itself into a requirement being met.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .schema import JobFacts, YearsReq
from .text import CleanJob

_REQ_ID = [
    re.compile(r"Requisition code:\s*([A-Z0-9-]+)", re.I),
    re.compile(r"Job No\.?\s*([A-Z0-9-]+)", re.I),
    re.compile(r"\b(\d{7}[A-Z]{3})\b"),
]
_TRAVEL_RANGE = re.compile(r"travel[^.\n]{0,25}?\b\d{1,3}\s*(?:-|–|to)\s*(\d{1,3})\s*%", re.I)
_TRAVEL_PLAIN = re.compile(r"travel[^.\n]{0,25}?\b(\d{1,3})\s*%", re.I)
_UP_TO = re.compile(r"\bup to\s+(\d{1,3})\s*%", re.I)
_YEARS = re.compile(r"(\d{1,2})\+?\s+years?\b", re.I)
_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
_WORD_THEN_DIGIT = re.compile(r"\b(?:" + "|".join(_NUMBER_WORDS) + r")\s*\((\d{1,2})\)", re.I)  # "seven (7)"
_WORD_YEARS = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")(\+?\s+years?\b)", re.I)  # "five years"


def _numerals(text: str) -> str:
    """Legal-style postings write "seven (7) years" or "five years": make them "7 years" for _YEARS."""
    text = _WORD_THEN_DIGIT.sub(r"\1", text)
    return _WORD_YEARS.sub(lambda m: _NUMBER_WORDS[m.group(1).lower()] + m.group(2), text)
_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+)")
_MBA = re.compile(r"\bMBA\b|master.s of business administration", re.I)
_FINAL_YEAR_MBA = re.compile(r"final year of (?:their|the) MBA", re.I)
# A deadline is a date right after words that announce one ("apply by", "applications close on", "before end of day
# on"...). A date on its own (a start date, a posting date) is not a deadline.
_DEADLINE_LEAD = (r"(?:posted until|apply by|applications? (?:are |is )?due(?: by| on)?|deadline(?: to apply)?(?: is|:)?|"
                  r"applications? (?:close|closes|will close)(?: on)?|closing date(?: is|:)?|recruiting for this role ends on|"
                  r"accepting applications (?:until|through)|no later than|submit[^.\n]{0,40}? by|before)")
_DATE = (r"(?P<md>[A-Z][a-z]{2,8}\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4})|(?P<num>\d{1,2}/\d{1,2}/\d{4})|"
         r"(?P<iso>\d{4}-\d{2}-\d{2})|(?P<dm>\d{1,2} [A-Z][a-z]{2,8},? \d{4})")
_DEADLINE = re.compile(_DEADLINE_LEAD + r"[^.\n]{0,40}?\b(?:" + _DATE + r")", re.I)
_ONSITE = re.compile(r"minimum of (\d) days per week", re.I)
_SPONSOR_RESTRICTED = re.compile(
    r"(?:do|does) not (?:intend to hire|offer|provide|support)[^.]{0,200}?sponsorship"
    r"|(?:will not|won't|unable to|cannot|can't|not able to|do not|does not) (?:provide |offer )?(?:visa |immigration )?sponsor"
    r"|without (?:requiring |the need for )?(?:visa |immigration )?sponsorship"
    r"|\bno (?:visa |immigration )?sponsorship",
    re.I,
)
_SPONSOR_LIMITED = re.compile(r"(?:limited )?(?:immigration )?sponsorship may be available", re.I)


def _quote(job: CleanJob, pattern: re.Pattern) -> str | None:
    """The sentence containing the match, taken from the one block (bullet/paragraph) that has it."""
    for b in job.blocks:
        if (m := pattern.search(b.text)):
            for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", b.text):
                if m.group(0) in s:
                    return s.strip()
            return b.text
    return None


def _travel(flat: str) -> int | None:
    for pat in (_TRAVEL_RANGE, _TRAVEL_PLAIN):
        m = pat.search(flat)
        if m:
            return int(m.group(1))
    m = _UP_TO.search(flat)
    return int(m.group(1)) if m else None


def _parse_date(m: re.Match) -> date | None:
    if m.group("num"):
        text, fmts = m.group("num"), ["%m/%d/%Y"]
    elif m.group("iso"):
        text, fmts = m.group("iso"), ["%Y-%m-%d"]
    elif m.group("dm"):
        text, fmts = m.group("dm").replace(",", ""), ["%d %B %Y", "%d %b %Y"]
    else:
        text = re.sub(r"(?<=\d)(?:st|nd|rd|th)", "", m.group("md")).replace(".", "")
        text = re.sub(r",? (\d{4})$", r", \1", text)
        fmts = ["%B %d, %Y", "%b %d, %Y", "%b %d, %Y"]
        text = text.replace("Sept ", "Sep ")
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def extract_facts(job: CleanJob) -> JobFacts:
    full = job.full_text()
    flat = re.sub(r"\s+", " ", full)
    facts = JobFacts()

    for pat in _REQ_ID:
        m = pat.search(flat)
        if m:
            facts.req_id = m.group(1)
            break

    facts.travel_max_pct = _travel(flat)

    if _SPONSOR_RESTRICTED.search(flat):
        facts.sponsorship, facts.sponsorship_quote = "restricted", _quote(job, _SPONSOR_RESTRICTED)
    elif _SPONSOR_LIMITED.search(flat):
        facts.sponsorship, facts.sponsorship_quote = "limited", _quote(job, _SPONSOR_LIMITED)
    elif "sponsorship" in flat.lower():
        facts.sponsorship = "mentioned"

    for m in _DEADLINE.finditer(flat):
        if (d := _parse_date(m)):
            facts.deadline = d
            break

    if (m := _ONSITE.search(flat)):
        facts.onsite_days_min = int(m.group(1))

    for b in job.blocks:
        if b.kind == "boilerplate":
            continue
        if (m := _YEARS.search(_numerals(b.text))):
            kind = b.kind if b.kind in ("required", "preferred") else "unknown"
            facts.min_years.append(YearsReq(years=int(m.group(1)), topic=b.text, kind=kind))

    amounts = [int(x.replace(",", "")) for x in _MONEY.findall(flat)]
    amounts = [a for a in amounts if a >= 20_000]
    if amounts:
        facts.salary_min, facts.salary_max = min(amounts), max(amounts)

    facts.mba_required = any(_MBA.search(t) for t in job.of_kind("required"))
    facts.final_year_mba = bool(_FINAL_YEAR_MBA.search(flat))
    return facts


def posting_open(facts: JobFacts, today: date | None = None) -> bool | None:
    """True/False if the posting states a deadline, None if it does not."""
    if facts.deadline is None:
        return None
    return (today or date.today()) <= facts.deadline

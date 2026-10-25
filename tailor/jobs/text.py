"""Turn a saved job page (PDF or text) into clean, section-tagged blocks.

Saved web pages are messy: navigation bars bleed into sentences ("Careersdevelopment"), widgets
interrupt paragraphs, headings get separated from their lists, and PDFs use ligature glyphs
("ﬁnancial"). This module fixes those before any regex or model sees the text.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

_NAV_TOKENS = ("Chat with our", "Recruiting Assistant", "Access Application", "Saved Jobs", "Job Search")
_GLUED_CAREERS = re.compile(r"\bCareers(?=[a-z])")
_COLUMN_GAP = re.compile(r"\s{3,}")
_PRIVATE_USE = re.compile(r"[-]")  # icon-font glyphs (invisible on screen, present in the text)
_NOISE_PIECE = (
    r"(?:Careers|Apply now|Apply for this job|Save job|Save|Share:|Back to search results"
    r"|Caution against fraudulent job offers\. Learn more\.|Same job available in \d+ locations"
    r"|Available in \d+ locations See all Associated with \d+ categories See all"
    r"|Associated with \d+ categories See all Available in \d+ locations See all)"
)
_NOISE_LINE = re.compile(rf"(?:{_NOISE_PIECE}\s*)+", re.I)
# Widgets that interrupt a paragraph as a fixed run of short lines.
_WIDGET_RUNS = [
    ["Recommended", "Find", "relevant", "(0)"],
    ["roles", "Upload your", "resume to see", "open roles relevant", "to your skills and", "experience",
     "Search with", "resume"],
]
_TAIL = re.compile(r"(?:Similar jobs|Explore PwC careers|About Accenture|Discover where this job fits at.*)", re.I)
_BULLET = re.compile(r"^[-•●*]\s+")
_CONNECTOR_END = re.compile(
    r"(?:\b(?:a|an|the|and|or|of|to|in|for|with|by|as|at|on|from|that|into|across|within|including)|[,(&/])$", re.I
)

# Heading text (lowercased, trailing colon removed) -> section kind for the blocks below it.
_HEADINGS: dict[str, str] = {
    "required": "required", "what you must have": "required", "here’s what you need": "required",
    "here's what you need": "required", "minimum qualifications": "required", "basic qualifications": "required",
    "preferred": "preferred", "bonus points if you have": "preferred", "what sets you apart": "preferred",
    "preferred qualifications": "preferred", "nice to have": "preferred",
    "responsibilities": "responsibilities", "work you’ll do": "responsibilities", "work you'll do": "responsibilities",
    "what you’ll do": "responsibilities", "what you'll do": "responsibilities",
    "position summary": "summary", "the opportunity": "summary", "you are": "required", "we are": "summary",
    "the team": "summary", "qualifications": "other", "qualification": "other",
    "a successful candidate would possess these skills": "skills",
    "the successful candidate will possess": "skills",
    # more of the headings real postings use (2026-10-06: Roblox's "You Will:" read as plain text, so its duties
    # weighed no more than its company blurb). Apostrophes are straightened before the lookup.
    **{h: "required" for h in (
        "you have", "you'll have", "you will have", "what you bring", "what you'll bring", "what you will bring",
        "requirements", "minimum requirements", "basic requirements", "required qualifications", "required skills",
        "must have", "must haves", "must-haves", "what we're looking for", "what we are looking for", "who you are",
        "your profile", "what you'll need", "what you need", "what you will need", "skills and experience",
        "skills & experience", "experience and skills", "key qualifications", "education and experience",
        "you might be a good fit if", "you may be a good fit if", "about you", "your background", "qualifications required",
        "what you should have", "the ideal candidate", "ideal candidate", "candidate profile", "job requirements")},
    **{h: "preferred" for h in (
        "preferred skills", "bonus", "bonus points", "nice-to-haves", "nice to haves", "nice-to-have", "pluses",
        "it would be great if", "it's a plus if", "preferred requirements", "desired qualifications", "desired skills",
        "additional qualifications", "what would be nice to have", "even better if", "preferred experience",
        "you'll stand out if", "you will stand out if", "extra credit")},
    **{h: "responsibilities" for h in (
        "you will", "you'll", "in this role", "in this role, you will", "in this role you will", "key responsibilities",
        "your responsibilities", "primary responsibilities", "what you will do", "duties", "job duties",
        "essential functions", "essential duties", "day to day", "day-to-day", "your impact", "what you'll be doing",
        "what you will be doing", "responsibilities include", "how you'll make an impact", "the work", "your role",
        "role responsibilities", "what you'll work on", "what you will work on", "your mission", "the impact you'll have")},
    **{h: "summary" for h in (
        "about the job", "job summary", "overview", "role overview", "about the role", "about the position",
        "job description", "position overview", "about the opportunity", "the role", "role summary")},
    **{h: "boilerplate" for h in (
        "benefits", "what we offer", "perks", "perks & benefits", "perks and benefits", "compensation", "pay",
        "pay transparency", "salary", "equal employment opportunity", "equal opportunity", "eeo statement",
        "our commitment", "diversity, equity & inclusion", "diversity, equity and inclusion", "why join us", "why us",
        "accommodations", "total rewards", "what's in it for you")},
}
# Lines that are boilerplate wherever they appear (EEO, pay, visa notes): kept for fact extraction, not for analysis.
_BOILERPLATE_ANY = re.compile(
    r"equal (?:employment )?opportunit|reasonable accommodation|\be-verify\b|\bbase pay\b|(?:salary|pay|wage) range|"
    r"benefits (?:include|package)|\b401\(k\)|visa categories|regardless of (?:race|age|sex)|"
    r"(?:discriminat|harassment) of any (?:type|kind)|privacy (?:notice|policy)|applicant data", re.I)
# Blocks that are boilerplate for analysis purposes (still kept for fact extraction).
_BOILERPLATE_START = re.compile(
    r"(?:The (?:wage|salary) range|Wage disclosure|Compensation at|You may also be eligible"
    r"|Deloitte is committed to providing|Deloitte refers to one or more|As used in this posting"
    r"|Qualified applicants with criminal histories|For only those qualified applicants"
    r"|Accenture offers a market competitive|Learn more about how we work|Role Location Annual Salary Range"
    r"|PwC offers a wide range of benefits|At Deloitte, we know that great people|Our inclusive culture"
    r"|Deloitte.s purpose is|From entry-level employees|From developing a stand out resume"
    r"|Explore |© \d{4}|Recruiting tips|Benefits$|Our people and culture|Our purpose|Professional development)",
    re.I,
)


@dataclass
class Block:
    kind: str  # required | preferred | responsibilities | summary | skills | other | boilerplate
    text: str


@dataclass
class CleanJob:
    blocks: list[Block]

    def full_text(self) -> str:
        """Everything kept after cleaning (used for fact extraction)."""
        return "\n".join(b.text for b in self.blocks)

    def body_text(self) -> str:
        """Without boilerplate; this is what a model should read."""
        return "\n".join(b.text for b in self.blocks if b.kind != "boilerplate")

    def of_kind(self, kind: str) -> list[str]:
        return [b.text for b in self.blocks if b.kind == kind]


def extract_pdf_text(path: Path) -> str:
    """Layout mode keeps headings next to their lists but fails on some PDFs; fall back to plain."""
    reader = PdfReader(str(path))
    plain = "\n".join(p.extract_text() or "" for p in reader.pages)
    try:
        layout = "\n".join(p.extract_text(extraction_mode="layout") or "" for p in reader.pages)
    except Exception:
        layout = ""
    count = lambda s: len(re.sub(r"\s+", "", s))  # noqa: E731
    return layout if count(layout) >= 0.8 * count(plain) else plain


def load_job_text(path: str | Path) -> str:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        return extract_pdf_text(path)
    if path.suffix.lower() in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"Unsupported job file type: {path.suffix} (use .pdf or .txt)")


def _clean_line(line: str) -> str:
    line = _GLUED_CAREERS.sub("", line.strip())
    for token in _NAV_TOKENS:
        line = line.replace(token, "   ")
    segments = [s for s in _COLUMN_GAP.split(line) if s.strip()]
    return " ".join(s.strip() for s in segments)


def _drop_widget_runs(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(lines):
        for run in _WIDGET_RUNS:
            window = [l for l in lines[i:] if l][: len(run)]
            if window == run:
                # skip exactly those non-empty lines
                seen = 0
                while seen < len(run):
                    if lines[i]:
                        seen += 1
                    i += 1
                break
        else:
            out.append(lines[i])
            i += 1
    return out


def _wrapped_url(prev: str) -> bool:
    """A URL that was cut at a hyphen at the end of a line."""
    last = prev.split()[-1] if prev.split() else ""
    return last.endswith("-") and "://" in last


def _to_blocks(lines: list[str]) -> list[str]:
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in lines:
        if not line:
            continue  # blank lines also come from page breaks mid-sentence, so they never end a block
        if _BULLET.match(line):
            if cur:
                blocks.append(cur)
            cur = [_BULLET.sub("", line)]
            continue
        if cur and (line[0].islower() or line[0] in "(,;" or _CONNECTOR_END.search(cur[-1]) or _wrapped_url(cur[-1])):
            if cur[-1].endswith("-") and (line[0].islower() or _wrapped_url(cur[-1])):
                cur[-1] = cur[-1] + line  # "financial-" + "crime" -> "financial-crime"
            else:
                cur.append(line)
            continue
        if cur:
            blocks.append(cur)
        cur = [line]
    if cur:
        blocks.append(cur)
    return [" ".join(b).strip() for b in blocks]


# A short line that reads as a heading but is not in the list ("What You Bring to the Table", "Here's what you'll do")
_HEADING_LIKE = [
    ("boilerplate", re.compile(r"\b(?:benefits|perks|what we offer|equal (?:employment )?opportunity|compensation|salary|pay range|"
                               r"accommodation)\b", re.I)),
    # not a form's "Preferred First Name" box (Greenhouse, 2026-10-07: the whole application form became "preferred")
    ("preferred", re.compile(r"\b(?:bonus|nice[- ]to[- ]haves?|preferred(?! (?:first |last |full )?name| pronouns?| locations?| "
                             r"offices?| contact| method| start| phone| email| language)|pluses|stand out)\b", re.I)),
    ("responsibilities", re.compile(r"\b(?:you(?:'ll| will) (?:do|work|own|build)|responsibilit|in this role|day[- ]to[- ]day|"
                                    r"your impact|what you(?:'ll| will) be doing)\b", re.I)),
    ("required", re.compile(r"\b(?:you(?:'ll| will)? (?:bring|have|need)|qualifications|requirements|about you|who you are|"
                            r"looking for|must[- ]haves?|you should have|skills (?:and|&) experience)\b", re.I)),
]


def _heading_kind(text: str) -> str | None:
    if len(text) > 60:
        return None
    key = text.strip().replace("\u2019", "'").rstrip(":").strip().lower()
    if key in _HEADINGS:
        return _HEADINGS[key]
    if len(key.split()) <= 8 and not re.search(r"[.!]$", key):
        for kind, rx in _HEADING_LIKE:
            if rx.search(key):
                return kind
    return None


# Where an application form starts on a page that shows the posting first (Greenhouse, Lever, Ashby): everything after
# it is the form (Email, Resume/CV, the self-identification survey), never the posting's sections
_FORM_START = re.compile(r"(?:\*\s*)?indicates a required field|autofill (?:my|with|from) |apply for this (?:job|position|role)|"
                         r"(?:legal )?first name\s*\*?|submit (?:your )?application", re.I)


def clean_job_text(raw: str) -> CleanJob:
    text = unicodedata.normalize("NFKC", raw)  # ligatures (ﬁ -> fi), non-breaking spaces
    text = _PRIVATE_USE.sub(" ", text)
    lines = [_clean_line(l) for l in text.splitlines()]
    lines = [l for l in lines if not _NOISE_LINE.fullmatch(l)]
    lines = _drop_widget_runs(lines)
    for i, l in enumerate(lines):  # cut the "similar jobs" / footer tail
        if _TAIL.fullmatch(l):
            lines = lines[:i]
            break
    blocks: list[Block] = []
    kind = "other"
    posting_chars = 0
    in_form = False
    for t in _to_blocks(lines):
        t = re.sub(r"\s{2,}", " ", t)
        if in_form or (posting_chars > 600 and len(t) < 60 and _FORM_START.fullmatch(t.strip())):
            in_form = True
            blocks.append(Block("boilerplate", t))
            continue
        heading = _heading_kind(t)
        if heading is not None:
            kind = heading
            continue  # headings only set the section; they are not content
        if _BOILERPLATE_START.match(t):
            blocks.append(Block("boilerplate", t))
            kind = "other"
            continue
        if _BOILERPLATE_ANY.search(t) and len(t) < 900:
            blocks.append(Block("boilerplate", t))  # a single EEO / pay / visa line: the section around it goes on
            continue
        blocks.append(Block(kind, t))
        posting_chars += len(t)
    return CleanJob(blocks)

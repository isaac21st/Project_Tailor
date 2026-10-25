"""Truth check for a tailored resume, measured against the confirmed base resume.

Tailoring may re-word, re-order and re-emphasize. It may not add facts. So this fails a tailored resume that:
  * changes the header, or any education entry's content,
  * adds an entry, changes an employer/location/link/role title/date, or drops a professional role,
  * uses a number that is not in the base resume,
  * names a new tool, organization or acronym (a capitalized term mid-sentence) that is not in the base resume,
  * adds a skill that is not in the base resume.
Terms a posting uses for something the person really did (for example "CRM" for HubSpot) can be allowed explicitly,
so each such re-labelling is a visible, deliberate decision instead of a silent one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .schema import Entry, Resume, Section

_NUM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&/+]*")


@dataclass
class Issue:
    level: str  # "error" | "warn"
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}: {self.message}"


def _canon(num: str) -> str:
    return num.replace("$", "").replace(",", "").rstrip("%").rstrip(".")


def _texts(resume: Resume) -> list[tuple[str, str]]:
    """(where, text) for every piece of prose: bullets and skills lines."""
    out: list[tuple[str, str]] = []
    if resume.summary:
        out.append(("summary", resume.summary))
    for s in resume.sections:
        for e in s.entries:
            for r in e.roles:
                for i, b in enumerate(r.bullets, 1):
                    out.append((f"{e.title} / {r.title} / bullet {i}", b))
        for line in s.lines:
            out.append((f"{s.heading} / {line.label}", line.text))
    return out


def _all_text(resume: Resume) -> str:
    parts = [resume.header.name] + [c.text for c in resume.header.contact]
    for s in resume.sections:
        parts.append(s.heading)
        for e in s.entries:
            parts += [e.title, e.location] + ([e.link.text] if e.link else [])
            for r in e.roles:
                parts += [r.title, r.dates] + r.bullets
        for line in s.lines:
            parts += [line.label, line.text]
    return "\n".join(p.replace("*", "") for p in parts)


def _split_items(text: str) -> list[str]:
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


def _is_education(section: Section) -> bool:
    return section.heading.upper().startswith("EDUCATION")


def _is_projects(section: Section) -> bool:
    return "PROJECT" in section.heading.upper()


def _entry_key(e: Entry) -> str:
    return e.title.casefold()


def check_tailored(base: Resume, tailored: Resume, allowed_terms: tuple[str, ...] = (),
                   summary_terms: tuple[str, ...] = ()) -> list[Issue]:
    """`allowed_terms`: skills added from the posting because the resume shows them (keywords.add_keywords).
    `summary_terms`: words only the summary line may use (the posting's role title, "MS", "MBA")."""
    issues: list[Issue] = []
    allowed = {t.casefold() for t in allowed_terms}
    summary_ok = {t.casefold() for t in summary_terms}

    if tailored.header != base.header:
        issues.append(Issue("error", "header", "name or contact line changed"))

    # ---- structure ----
    if [s.heading for s in tailored.sections] != [s.heading for s in base.sections]:
        issues.append(Issue("warn", "sections", "section names or order differ from the base resume"))

    base_edu = sorted(e.model_dump_json() for s in base.sections if _is_education(s) for e in s.entries)
    new_edu = sorted(e.model_dump_json() for s in tailored.sections if _is_education(s) for e in s.entries)
    if base_edu != new_edu:
        issues.append(Issue("error", "education", "education entries must be identical to the base resume"))

    base_entries = {_entry_key(e): (e, s) for s in base.sections if not _is_education(s) for e in s.entries}
    seen: set[str] = set()
    for s in tailored.sections:
        if _is_education(s):
            continue
        for e in s.entries:
            key = _entry_key(e)
            seen.add(key)
            if key not in base_entries:
                issues.append(Issue("error", e.title, "entry does not exist in the base resume"))
                continue
            b, _ = base_entries[key]
            if (e.location, e.link) != (b.location, b.link):
                issues.append(Issue("error", e.title, "location or link changed"))
            if [(r.title, r.dates) for r in e.roles] != [(r.title, r.dates) for r in b.roles]:
                issues.append(Issue("error", e.title, "a role title, date range, or the set of roles changed"))
    for key, (b, sec) in base_entries.items():
        if key not in seen:
            issues.append(Issue("warn" if _is_projects(sec) else "error", b.title,
                                "entry was dropped" + ("" if _is_projects(sec) else " (professional experience stays)")))

    # ---- numbers and new terms ----
    base_text = _all_text(base)
    base_nums = {_canon(n) for n in _NUM.findall(base_text)}
    base_words = {w.casefold() for w in _WORD.findall(base_text)}
    for where, text in _texts(tailored):
        plain = text.replace("*", "")
        for n in _NUM.findall(plain):
            if _canon(n) not in base_nums:
                issues.append(Issue("error", where, f"number '{n}' is not in the base resume"))
        words = list(_WORD.finditer(plain))
        for i, m in enumerate(words):
            w = m.group(0)
            if len(w) < 2:
                continue
            inner_caps = any(c.isupper() for c in w[1:])
            if not (w[0].isupper() or inner_caps):
                continue
            if i == 0 and not inner_caps:
                continue  # a sentence-initial capital (a verb), not a proper noun
            if w.casefold() in base_words or w.casefold() in allowed or (where == "summary" and w.casefold() in summary_ok):
                continue
            if "/" in w and all(x.casefold() in base_words for x in w.split("/") if x):
                continue  # "HTML/CSS" when the resume says "HTML, CSS"
            if any(w.casefold() in a.split() or w.casefold() in re.split(r"[ /-]", a) for a in allowed):
                continue  # a word of an added skill ("Primary" in "Primary research")
            issues.append(Issue("error", where, f"new term '{w}' is not in the base resume"))

    # ---- skills: reorder freely, add nothing ----
    base_skills = {i.casefold() for s in base.sections for line in s.lines for i in _split_items(line.text)}
    for s in tailored.sections:
        for line in s.lines:
            for item in _split_items(line.text):
                if item.replace("*", "").casefold() not in base_skills and item.casefold() not in allowed:
                    issues.append(Issue("error", f"{s.heading} / {line.label}", f"new skill '{item}'"))
    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "error"]

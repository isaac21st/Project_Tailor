"""How well a role fits the person (layer 2), read from the posting and the resume with rules only, no model.

Each requirement is matched to the person's own evidence and labeled direct, transferable, academic or missing. The score
comes from those labels and from the role itself, and is capped by what would stop an application (too many required
things missing, far too senior, a different profession), so matching words alone cannot make it high.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..jobs.facts import extract_facts
from ..jobs.text import clean_job_text
from .auto import CONCEPTS, has_job_description
from .career import _CONSULTING_TITLE, CareerProfile, Track, best_track, has_term
from .keywords import Keyword, extract_keywords
from .schema import Resume

WEIGHTS = {"Industry alignment": 20, "Functional alignment": 25, "Relevant experience": 25, "Technical skills": 10,
           "Education alignment": 10, "Seniority and experience": 10}
_CREDIT = {"direct": 1.0, "transferable": 0.5, "academic": 0.4, "missing": 0.0}
_KIND_WEIGHT = {"required": 3.0, "preferred": 1.5, "skills": 2.0, "responsibilities": 1.0}
_NOISE = re.compile(r"equal opportunity|authori[sz]ed to work|sponsorship|salary|compensation|benefits|background check|"
                    r"accommodation|travel|on-?site|hybrid|relocat|bachelor|master|degree|gpa\b|years? of|\byears\b", re.I)
_ABOUT_THEM = re.compile(r"^#|^(?:we|our|about|at \w+,?)\b|\b(?:our|we)\b (?:platforms?|mission|clients? (?:are|include)|customers? (?:are|include)|believe|build)", re.I)
_SOFT = re.compile(r"communication skills|communicat\w+ clearly|critical thinking|problem[- ]solving skills|team player|attention to detail|"
                   r"detail[- ]oriented|self[- ]starter|work ethic|interpersonal|organi[sz]ational skills|time management|energizing", re.I)
_CREDENTIAL = re.compile(r"\b(?:licen[sc]e[ds]?|certifi(?:ed|cation)s?|PMP|CPA|CFA|CISSP|RN|PE|security clearance)\b")
_STOP = set("""a an and or the of to in on for with at by from as is are be will you your our we their this that these those
ability able experience experienced strong excellent good skills skill knowledge work working including include such across
within using use used related relevant required preferred plus must have has other new high level team teams""".split())
_EARLY = re.compile(r"new grad|campus|entry[- ]level|early career|class of 20|recent graduate|mba (?:graduate|hire|associate|program)|"
                    r"graduating|development program|rotational", re.I)
_VERY_SENIOR = re.compile(r"\b(?:senior manager|director|principal|vice president|vp|head of|partner|chief)\b", re.I)
_SENIOR = re.compile(r"\b(?:senior|sr\.?|lead|staff|manager)\b", re.I)
_DATES = re.compile(r"([A-Z][a-z]+)\s+(\d{4})\s*[-–]\s*(?:([A-Z][a-z]+)\s+(\d{4})|Present)")
_GENERIC_SKILLS = {"analytics", "research", "strategy", "operations", "modeling", "forecasting", "prototyping", "statistics", "consulting"}
_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


@dataclass
class Unit:
    entry: str
    text: str
    kind: str  # work | client_project | academic | skill
    from_context: bool = False
    words: set[str] = field(default_factory=set)
    concepts: set[str] = field(default_factory=set)


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z][a-z+#-]+", text.lower()) if w not in _STOP and len(w) > 2}


def _concepts(text: str) -> set[str]:
    return {c for c, p in CONCEPTS.items() if p.search(text)}


def _plain(text: str) -> str:
    return re.sub(r"\*+", "", text)


def _unit(entry: str, text: str, kind: str, from_context: bool = False) -> Unit:
    return Unit(entry, _plain(text), kind, from_context, _stems(text), _concepts(text))


def _kind_of(entry: str, heading: str, career: CareerProfile | None) -> str:
    for part, kind in (career.entry_kinds.items() if career else ()):
        if part.lower() in entry.lower():
            return kind
    return "academic" if re.search(r"education|project", heading, re.I) else "work"


def _units(base: Resume, career: CareerProfile | None, confirmed: tuple[str, ...]) -> list[Unit]:
    units: list[Unit] = []
    kinds: dict[str, str] = {}
    for section in base.sections:
        for line in section.lines:
            units.append(_unit("Skills", f"{line.label}: {line.text}", "skill"))
        for entry in section.entries:
            kinds[entry.title] = _kind_of(entry.title, section.heading, career)
            for role in entry.roles:
                for text in role.bullets:  # what was done; a job title alone shows nothing
                    units.append(_unit(entry.title, text, kinds[entry.title]))
    if confirmed:
        units.append(_unit("Skills", ", ".join(confirmed), "skill"))
    for c in (career.context if career else ()):
        entry = next((t for t in kinds if c.entry.lower() in t.lower()), None)
        if entry:
            units.append(_unit(entry, c.note, kinds[entry], from_context=True))
    return units


def _requirements(job) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def read(kinds: tuple[str, ...], as_kind: str = "") -> None:
        for kind in kinds:
            for block in job.of_kind(kind):
                for line in re.split(r"\n|(?<=[.;])\s+(?=[A-Z])", block):
                    line = line.strip(" -•\t")
                    if 18 <= len(line) <= 260 and not _ABOUT_THEM.search(line) and not _NOISE.search(line) and (line, as_kind or kind) not in out:
                        out.append((line, as_kind or kind))

    read(("required", "skills", "preferred", "responsibilities"))
    if not out:  # a posting without headings: its lines are all there is to go on
        read(("other", "summary"), "responsibilities")
    return sorted(out, key=lambda x: -_KIND_WEIGHT[x[1]])[:26]


def _assess(text: str, kind: str, units: list[Unit], named: list[Keyword], career: CareerProfile | None) -> dict:
    words, concepts = _stems(text), _concepts(text)
    asked = [k for k in named if k.found_in(text)]
    if not _CREDENTIAL.search(text) and not asked and (not concepts or _SOFT.search(text)):
        return None  # "create an energizing team environment", "strong communication skills": nothing a resume could show or lack
    needs = [n for n in (career.needs if career else ()) if has_term(n.terms, text)]
    barred = [(b.term.lower(), b.entry.lower()) for b in (career.never_claim if career else ()) if b.term.lower() in text.lower()]
    usable = [u for u in units if not any(entry in u.entry.lower() for _, entry in barred)]

    def score(u: Unit) -> float:
        points = 3.0 * sum(k.found_in(u.text) for k in asked) + 1.5 * len(concepts & u.concepts) + 0.5 * min(len(words & u.words), 6)
        return points + (1.5 if any(has_term(n.evidence, u.entry) for n in needs) else 0.0)

    ranked = sorted(usable, key=lambda u: -score(u))
    best = ranked[0] if ranked and score(ranked[0]) > 0 else None
    result = {"text": text[:220], "weight": kind, "label": "missing", "evidence": "", "entry": ""}
    credential = _CREDENTIAL.search(text)
    if best is None or (credential and not any(credential[0].lower() in u.text.lower() for u in usable)):
        return result  # a license or certification is held or it is not; related work does not stand in for it
    tools = [k for k in asked if _is_tool(k)]
    if tools and not any(k.found_in(u.text) for k in tools for u in usable):
        return result  # a named tool that nothing shows: the concepts around it are not the tool
    domain = {"healthcare", "finance"} & concepts
    def strong(u: Unit) -> bool:
        """Direct evidence: a bullet that says the thing asked for, or closely describes it. A skills line proves a tool only."""
        if not domain <= u.concepts:
            return False
        if u.kind == "skill":
            return any(_is_tool(k) and k.found_in(u.text) for k in asked)
        return any(k.found_in(u.text) for k in asked) or (len(concepts & u.concepts) >= 2 and len(words & u.words) >= 3)
    good = [u for u in ranked if strong(u)]
    earned = [u for u in good if u.kind in ("work", "client_project", "skill") and score(u) >= score(good[0]) - 1.0]
    if earned:
        best, label = earned[0], "direct"
    elif good:
        best, label = good[0], "academic"
    else:
        # the same kind of work in another setting: a real bullet that shares the idea and some of the words, never a skills list
        near = [u for u in ranked if u.kind != "skill" and ((concepts & u.concepts and len(words & u.words) >= 2) or any(k.found_in(u.text) for k in asked))]
        if not near:
            return result
        best = near[0]
        label = "academic" if best.kind == "academic" else "transferable"
    result.update(label=label, evidence=best.text[:220], entry=best.entry, from_context=best.from_context)
    return result


def _is_tool(k: Keyword) -> bool:
    """Tableau, SQL, Power BI: a thing one has used or not. "Quantitative analysis" is a skill shown in other words."""
    if k.kind != "hard" or (k.term is None and not k.name.isupper()):  # words read from the posting's lists can be anything
        return False
    return k.name.isupper() or (" " not in k.name and k.name.lower() not in _GENERIC_SKILLS) or bool(re.search(r"\s[A-Z]", k.name))


def _display(entry: str) -> str:
    short = re.split(r"\s+&\s+|\s+\(", entry)[0]
    return re.sub(r"\b(Of|For|The|And)\b", lambda m: m[1].lower(), short.title()) if short.isupper() else short


def _month(name: str, year: str) -> int:
    return int(year) * 12 + _MONTHS.get(name[:3].lower(), 6)


def years_worked(base: Resume, career: CareerProfile | None) -> float:
    today = date.today()
    months = 0
    for section in base.sections:
        for entry in section.entries:
            if _kind_of(entry.title, section.heading, career) != "work":
                continue
            for role in entry.roles:
                if (m := _DATES.search(role.dates)):
                    end = _month(m[3], m[4]) if m[3] else today.year * 12 + today.month
                    months += max(0, end - _month(m[1], m[2]))
    return round(months / 12, 1)


def _years_asked(facts) -> int | None:
    asked = [y.years for y in facts.min_years if 1 <= y.years <= 15 and y.kind != "preferred" and re.search(r"experience|work", y.topic, re.I)]
    return max(asked) if asked else None


def _seniority(facts, title: str, body: str, have: float) -> tuple[int, str]:
    asked = _years_asked(facts)
    if _EARLY.search(title) or re.search(r"\bgraduate\b|\bmba\b", title, re.I):
        return 100 if not asked or asked <= have + 1 else 60, "Written for early-career or graduating candidates."
    if _VERY_SENIOR.search(title):
        return 25, f"The title is well above your {have:g} years of work."
    if asked is not None:
        gap = asked - have
        score = 100 if gap <= 0.5 else 80 if gap <= 1 else 55 if gap <= 2 else 30 if gap <= 3 else 10
        return score, f"Asks for {asked}+ years; your resume shows about {have:g} years of paid work."
    if _SENIOR.search(re.sub(r"(?:product|program|project|account) manager", "", title, flags=re.I)):
        return 60, "The title reads as experienced, with no years stated."
    if _EARLY.search(title + " " + body[:3000]):
        return 100, "Written for early-career or graduating candidates."
    return 80, "No years stated."


def _education(job, base: Resume, title: str = "") -> tuple[int, str]:
    asked = " ".join(job.of_kind("required")) or job.body_text()
    if re.search(r"\bph\.?d\b", title, re.I):
        return 15, "The role is for PhD candidates."
    degrees = " ".join(r.title for s in base.sections if re.search("education", s.heading, re.I) for e in s.entries for r in e.roles)
    if any(re.search(r"\b(?:ph\.?d|doctorate)\b", s, re.I) and not re.search(r"\b(?:bachelor|master|mba|bs|ba|ms)\b", s, re.I)
           for s in re.split(r"(?<=[.;])\s+|\n", asked)) and "doctor" not in degrees.lower():
        return 20, "Asks for a doctorate."
    if re.search(r"\b(?:RN|MD|PharmD|JD|nursing degree|medical degree|law degree)\b", asked):
        return 10, "Asks for a professional clinical or legal degree you do not have."
    if re.search(r"\bMBA\b|master.s of business", asked, re.I) and re.search("business administration|mba", degrees, re.I):
        return 100, "Asks for an MBA, and you are completing one."
    if re.search(r"computer science|engineering degree|degree in (?:engineering|data science|statistics|mathematics)", asked, re.I):
        return (65, "Asks for a technical degree or related field; the MSI is related, not the same.") \
            if re.search(r"or (?:a )?(?:related|equivalent|similar)", asked, re.I) else (40, "Asks for a technical degree you do not have.")
    if re.search(r"bachelor|master|degree|graduate", asked, re.I):
        return 90, "Your degrees meet what it asks."
    return 80, "No degree is stated."


def _industry(track: Track | None, strength: int, career, title: str, body: str) -> tuple[int, str]:
    outside = has_term(career.outside_targets, title) or [t for t in has_term(career.outside_targets, body) if len(re.findall(re.escape(t), body, re.I)) >= 4] if career else []
    if outside:
        return 20, f"Reads as {outside[0]}, outside what you are targeting."
    if track is None:
        return 30, "Not one of your target areas."
    if track.kind != "consulting":
        return 65, f"{track.name}: part of your search, outside the consulting targets."
    base = 100 if strength >= 7 else 80 if strength >= 5 else 60
    return max(40, base - 2 * (track.priority - 1)), f"{track.name}, your priority {track.priority}."


def _function(track: Track | None, career, title: str, vec_top: list[str], units: list[Unit]) -> tuple[int, str]:
    other = has_term(career.not_my_function, title) if career else []
    if other:
        return 25, f"The job itself is {other[0]}, a different profession from how you position yourself."
    if track is not None and has_term(track.role_terms, title):
        return 100, f"The title matches the {track.name} roles you want."
    earned = {c for u in units if u.kind in ("work", "client_project") for c in u.concepts}
    covered = sum(c in earned for c in vec_top) / max(len(vec_top), 1)
    score = round(35 + 45 * covered)
    if not _CONSULTING_TITLE.search(title):  # a media planner at a healthcare firm is still a media planner
        score = min(score, 50)
    return score, f"The work centers on {', '.join(c.replace('_', ' ') for c in vec_top[:3])}."


def assess_fit(base: Resume, job_text: str, title: str = "", company: str = "", career: CareerProfile | None = None,
               confirmed: tuple[str, ...] = ()) -> dict | None:
    """The fit report, or None when the text is not a job description."""
    if not has_job_description(job_text or "", title):
        return None
    job = clean_job_text(job_text)
    body, facts = job.body_text(), extract_facts(job)
    track, strength = best_track(career, title, f"{company} {body[:1500]}", body)
    units = _units(base, career, confirmed)
    kw = extract_keywords(job, title)
    # the vocabulary's own terms, never the employer's name ("Figma" at Figma is not a tool the job asks for)
    named = [k for k in kw.items if k.kind in ("hard", "industry") and (k.term is not None or k.name.isupper())
             and k.name.lower() not in company.lower()]
    rows = [r for text, kind in _requirements(job) if (r := _assess(text, kind, units, named, career))]

    weights = [_KIND_WEIGHT[r["weight"]] for r in rows]
    experience = round(100 * sum(w * _CREDIT[r["label"]] for w, r in zip(weights, rows)) / sum(weights)) if rows else None
    if experience is not None and len(rows) < 4:  # a line or two is not enough to call it strong or weak
        experience = round((experience * len(rows) + 50 * (4 - len(rows))) / 4)
    must = [r for r in rows if r["weight"] in ("required", "skills")]
    missing_share = sum(r["label"] == "missing" for r in must) / len(must) if must else 0.0

    hard = [k for k in named if _is_tool(k)]
    held = [k for k in hard if any(k.found_in(u.text) for u in units)]
    technical = round(100 * len(held) / len(hard)) if len(hard) >= 2 else None  # one tool named says little either way

    from .auto import job_concepts
    vec, _ = job_concepts(job, title)
    top = [c for c, v in sorted(vec.items(), key=lambda kv: -kv[1])[:4] if v > 0]
    have = years_worked(base, career)
    scores = {
        "Industry alignment": _industry(track, strength, career, title, body),
        "Functional alignment": _function(track, career, title, top, units),
        "Relevant experience": (experience, f"{sum(r['label'] != 'missing' for r in rows)} of {len(rows)} requirements have evidence."
                                if rows else "The posting lists no clear requirements, so this could not be judged."),
        "Technical skills": (technical, f"Your resume shows {len(held)} of {len(hard)} named tools and methods.")
                            if technical is not None else (None, "Too few specific tools named to judge."),
        "Education alignment": _education(job, base, title),
        "Seniority and experience": _seniority(facts, title, body, have),
    }
    used = {n: s for n, (s, _) in scores.items() if s is not None}
    overall = round(sum(WEIGHTS[n] * s for n, s in used.items()) / sum(WEIGHTS[n] for n in used))
    caps = []
    if missing_share >= 0.4:
        caps.append((58, "Capped: many required qualifications have no evidence."))
    if used["Seniority and experience"] <= 30:
        caps.append((45, "Capped: the role is well above your experience."))
    if used["Functional alignment"] <= 25:
        caps.append((40, "Capped: a different profession."))
    if used["Education alignment"] <= 20:
        caps.append((45, "Capped: asks for a degree you do not have."))
    if used["Industry alignment"] <= 20:
        caps.append((45, "Capped: outside what you are targeting."))
    if not rows:
        caps.append((55, "Capped: too little in the posting to judge your experience against."))
    cap_notes = []
    for limit, note in caps:
        if overall > limit:
            overall, cap_notes = limit, [note]

    counts = {k: sum(r["label"] == k for r in rows) for k in _CREDIT}
    strongest = sorted((r for r in rows if r["label"] in ("direct", "academic", "transferable")),
                       key=lambda r: (-_CREDIT[r["label"]], -_KIND_WEIGHT[r["weight"]]))[:5]
    gaps = [r for r in rows if r["label"] == "missing"][:6]
    entries: dict[str, dict] = {}
    for r in rows:
        if r["entry"] and r["entry"] != "Skills":
            e = entries.setdefault(r["entry"], {"entry": r["entry"], "requirements": 0, "points": 0.0, "labels": {}})
            e["requirements"] += 1
            e["points"] += _CREDIT[r["label"]]
            e["labels"][r["label"]] = e["labels"].get(r["label"], 0) + 1
    experiences = sorted(entries.values(), key=lambda e: -e["points"])[:4]
    for e in experiences:
        e.pop("points")

    positioning = []
    if track is not None and track.positioning:
        positioning.append(track.positioning)
    if experiences:
        wanted = track.evidence if track is not None else []
        rank = lambda e: next((i for i, w in enumerate(wanted) if w.lower() in e["entry"].lower()), len(wanted))  # noqa: E731
        lead = sorted(experiences, key=rank)[:2]  # stable: within the same rank, what the requirements leaned on most
        positioning.append("Lead with " + " and ".join(_display(e["entry"]) for e in lead) + ".")
    if track is not None and track.language:
        positioning.append("Where true, say it as: " + ", ".join(track.language) + ".")
    required_gaps = [r["text"] for r in gaps if r["weight"] in ("required", "skills")]
    if required_gaps:
        positioning.append(f"Do not paper over this gap; be ready to speak to it: {required_gaps[0][:120]}")

    return {
        "score": overall, "label": "Strong fit" if overall >= 75 else "Good fit" if overall >= 60 else "Stretch" if overall >= 45 else "Low fit",
        "track": {"name": track.name, "priority": track.priority, "consulting": track.kind == "consulting"} if track else None,
        "dimensions": [{"name": n, "score": s, "why": w} for n, (s, w) in scores.items()],
        "counts": counts, "strongest": strongest, "missing": gaps, "experiences": experiences, "positioning": positioning,
        "notes": cap_notes + ([] if rows else ["The posting lists few clear requirements, so this is rough."]),
        "years_worked": have,
    }

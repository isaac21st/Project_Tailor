"""Networking for referrals (the person, 2026-10-13: "find people to network to ask for referral base on alum, work,
and most of all location").

Nothing here reads LinkedIn or any other site about people: Project Tailor stays off LinkedIn (its terms forbid
automated tools) and does not gather people's details. For each posting it works out
  * where the role is (the office cities in the posting) and which of them matters most to the person,
  * the person's own ties (schools, past employers, programs), from the resume plus their own additions,
  * ranked searches the person opens themselves: LinkedIn's own people / alumni search and Google, location first,
  * the recruiter the posting itself names (school job boards list one with an email),
  * message drafts built from the person's real facts (fixed templates: nothing invented, no em dashes),
and keeps the contacts the person chooses to save, with their status and follow-up dates.
The person sends every message themselves.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

# ---- places ------------------------------------------------------------------------------------------------------

# metro areas: a posting's office city and a contact's city count as the same place when they share a metro
METROS: dict[str, list[str]] = {
    "San Francisco Bay Area": ["San Francisco", "South San Francisco", "Mountain View", "San Jose", "Sunnyvale",
                               "Palo Alto", "San Bruno", "Menlo Park", "Redwood City", "Cupertino", "Santa Clara",
                               "Oakland", "San Mateo", "Foster City", "Los Gatos", "Los Gatos", "Fremont", "Berkeley",
                               "Emeryville", "Burlingame", "Milpitas", "Los Altos"],
    "New York City": ["New York", "New York City", "NYC", "Brooklyn", "Manhattan", "Jersey City", "Hoboken"],
    "Chicago": ["Chicago", "Evanston", "Oak Brook", "Deerfield", "Northbrook"],
    "Seattle": ["Seattle", "Bellevue", "Redmond", "Kirkland"],
    "Los Angeles": ["Los Angeles", "Santa Monica", "Culver City", "Pasadena", "Irvine", "Burbank"],
    "Boston": ["Boston", "Cambridge", "Somerville", "Waltham"],
    "Washington, DC": ["Washington, DC", "Washington DC", "Washington D.C.", "Arlington", "McLean", "Reston", "Bethesda"],
    "Austin": ["Austin"],
    "Dallas": ["Dallas", "Irving", "Plano", "Fort Worth"],
    "Houston": ["Houston"],
    "Atlanta": ["Atlanta"],
    "Denver": ["Denver", "Boulder"],
    "Detroit / Ann Arbor": ["Detroit", "Ann Arbor", "Southfield", "Dearborn", "Troy"],
    "Minneapolis": ["Minneapolis", "Eden Prairie", "St. Paul"],
    "Nashville": ["Nashville"],
    "Philadelphia": ["Philadelphia"],
    "Miami": ["Miami"],
    "Salt Lake City": ["Salt Lake City", "Lehi"],
    "Phoenix": ["Phoenix", "Tempe", "Scottsdale"],
    "San Diego": ["San Diego"],
    "Pittsburgh": ["Pittsburgh"],
    "Portland": ["Portland"],
    "Raleigh": ["Raleigh", "Durham"],
}
_CITY_TO_METRO = {c.lower(): m for m, cities in METROS.items() for c in cities}
_CITY_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(c) for c in _CITY_TO_METRO), key=len, reverse=True)) + r")\b",
                      re.I)


def metro_of(place: str) -> str | None:
    """The metro area a place belongs to ("Mountain View, CA" -> "San Francisco Bay Area")."""
    m = _CITY_RE.search(place or "")
    return _CITY_TO_METRO.get(m.group(1).lower()) if m else None


_PLACE_LINE = re.compile(r"\b(?:location|office|based in|hybrid|on-?site|in-person|remote|relocat)", re.I)


def posting_places(text: str) -> list[str]:
    """The office cities a posting names, in its order, one per metro. Only short lines or lines about location count
    (a brand like "Cambridge Consultants" in a paragraph is not an office). "Remote" is kept when offered."""
    seen: dict[str, str] = {}
    remote = False
    for line in (text or "").splitlines():
        short = len(line) <= 140
        about_place = bool(_PLACE_LINE.search(line))
        if not (short or about_place):
            continue
        for m in _CITY_RE.finditer(line):
            city = m.group(1)
            seen.setdefault(_CITY_TO_METRO[city.lower()], "New York" if city.lower() == "nyc" else city)
        if re.search(r"\bremote\b", line, re.I) and (short or about_place):
            remote = True
    return list(seen.values()) + (["Remote"] if remote else [])


def board_places(url: str) -> list[str]:
    """Where a posting is, from its job board's public listing data, for postings whose text never names a city
    (DoorDash on Greenhouse shows it only in the page header). Greenhouse, Lever and SmartRecruiters. [] when unknown."""
    import httpx

    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=([\w-]+)&token=(\d+)|([\w-]+)/jobs/(\d+))", url or "")
    try:
        if m:
            board, job = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}", timeout=8)
            if r.status_code == 200:
                return posting_places((r.json().get("location") or {}).get("name", "").replace(";", "\n"))
        m = re.search(r"jobs\.lever\.co/([\w-]+)/([0-9a-f-]{36})", url or "")
        if m:
            r = httpx.get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}", timeout=8)
            if r.status_code == 200:
                cats = r.json().get("categories") or {}
                return posting_places("\n".join([cats.get("location") or ""] + list(cats.get("allLocations") or [])))
        m = re.search(r"smartrecruiters\.com/([\w-]+)/(\d{6,})", url or "")
        if m:
            r = httpx.get(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}", timeout=8)
            if r.status_code == 200:
                loc = r.json().get("location") or {}
                return posting_places(loc.get("city") or "")
    except (httpx.HTTPError, ValueError):
        return []
    return []


def location_focus(places: list[str], home: str, preferred: list[str]) -> list[str]:
    """The posting's places, the ones that matter most to the person first: their home metro, then their preferred
    cities in their order, then the rest as the posting lists them."""
    def rank(place: str) -> tuple[int, int]:
        metro = metro_of(place)
        if metro and metro == metro_of(home):
            return (0, 0)
        for i, p in enumerate(preferred):
            if metro and metro == metro_of(p):
                return (1, i)
        return (2 if place != "Remote" else 3, 0)
    return sorted(places, key=rank)


# ---- the person's ties ---------------------------------------------------------------------------------------------

@dataclass
class Tie:
    kind: str          # "school" | "program" | "employer"
    label: str         # how a message names it ("Example Business School")
    search: str        # what a search looks for ("Example School of Business")
    weight: float      # how strong a bond it is for a referral ask
    words: list[str] = field(default_factory=list)  # words that show it on someone's headline / profile
    short: str = ""    # how a list names it ("EBS")
    within: str = ""   # a broader tie this one already covers (a business school is part of its university)
    alumni_page: str = ""  # the school's LinkedIn page name ("university-of-michigan"): its alumni tool

    def __post_init__(self) -> None:
        self.short = self.short or self.label


# No built-in ties: each person's come from their own profile (ties_from_profile) and the ones they add or tune on the
# Network page (profile_overrides.json "network_ties", "network_weights", "network_off").
DEFAULT_TIES: list[Tie] = []

_DEGREES = [(r"business administration|\bmba\b", "MBA"), (r"master of science|\bm\.?s\.?\b|\bmsc?\b", "MS"),
            (r"master of arts|\bm\.?a\.?\b", "MA"), (r"master of", "master's"), (r"doctor|ph\.?d", "PhD"),
            (r"juris doctor|\bj\.?d\.?\b", "JD"), (r"bachelor of science|\bb\.?s\.?\b", "BS"),
            (r"bachelor of arts|\bb\.?a\.?\b", "BA"), (r"bachelor", "bachelor's"), (r"associate", "associate's")]


def degree_short(degree: str) -> str:
    """"Master of Business Administration" -> "MBA"; "Bachelor of Arts in History" -> "BA"."""
    return next((short for pat, short in _DEGREES if re.search(pat, degree or "", re.I)), degree or "degree")


def _school_words(school: str) -> list[str]:
    """Words that show a school on someone's profile: its name and the parts of a "University, School of X" name."""
    name = re.sub(r"\s+", " ", school or "").strip()
    parts = [name] + [p.strip() for p in re.split(r",| - | – ", name) if len(p.strip()) > 8]
    return list(dict.fromkeys(p.lower() for p in parts if p))


def ties_from_profile(profile) -> list[Tie]:
    """The ties a person's own profile shows: each school (most recent first, a little stronger) and each employer."""
    out: list[Tie] = []
    edu = list(getattr(profile, "education", []) or [])
    edu.sort(key=lambda e: -((e.end.year * 12 + (e.end.month or 0)) if getattr(e, "end", None) else 0))
    for i, e in enumerate(edu):
        if not e.school or any(t.label.lower() == e.school.lower() for t in out):
            continue
        out.append(Tie("school", e.school, e.school, max(1.5, 2.5 - 0.2 * i), _school_words(e.school),
                       e.school.split(",")[0].strip()))
    for x in getattr(profile, "experience", []) or []:
        if getattr(x, "kind", "job") != "job" or not x.employer:
            continue
        if not any(t.label.lower() == x.employer.lower() for t in out):
            out.append(Tie("employer", x.employer, x.employer, 1.8, [x.employer.lower()]))
    return out


def load_ties(overrides: dict | None = None, profile=None) -> list[Tie]:
    """The person's ties: the ones their profile shows (unless "network_profile_ties" is false), plus any they add in
    profile_overrides.json "network_ties" ([{"kind": "club", "label": ..., "search": ..., "weight": 1.5, "words": [...],
    "short": ..., "within": ...}]); their own weights ("network_weights": {label: weight}) and switched-off ties
    ("network_off": [label]) apply."""
    import copy

    o = overrides or {}
    weights = o.get("network_weights") or {}
    off = set(o.get("network_off") or [])
    ties = []
    base = ties_from_profile(profile) if profile is not None and o.get("network_profile_ties", True) else []
    for t in base:
        t = copy.copy(t)
        if t.label in weights:
            try:
                t.weight = max(0.0, min(3.0, float(weights[t.label])))
            except (TypeError, ValueError):
                pass
        ties.append(t)
    for t in o.get("network_ties", []) or []:
        try:
            label = str(t["label"]).strip()
            if label and not any(x.label.lower() == label.lower() for x in ties):
                ties.append(Tie(str(t.get("kind", "employer")), label, str(t.get("search") or label),
                                float(weights.get(label, t.get("weight", 1.5))), [w.lower() for w in t.get("words", [label])],
                                str(t.get("short") or ""), str(t.get("within") or ""), str(t.get("alumni_page") or "")))
        except (KeyError, TypeError, ValueError):
            continue
    return [t for t in ties if t.label not in off and t.weight > 0]


TIE_KINDS = ("school", "program", "club", "employer")


# ---- searches the person opens -------------------------------------------------------------------------------------

def _linkedin_people(keywords: str) -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote_plus(keywords)


def _google(query: str) -> str:
    return "https://www.google.com/search?q=" + quote_plus(query)


def _company_term(company: str) -> str:
    # "Partners in Performance (Accenture)" -> "Partners in Performance"; quotes keep a multi-word name together
    name = re.sub(r"\s*\(.*?\)\s*$", "", company or "").strip()
    return f'"{name}"' if " " in name else name


@dataclass
class Search:
    rank: int
    group: str       # "Location + alumni", ...
    label: str       # what this search finds, in plain words
    url: str
    why: str         # why it is worth trying


def search_plan(company: str, places: list[str], ties: list[Tie], max_per_group: int = 4) -> list[Search]:
    """Searches for people who could refer the person, strongest first: same city and a shared school, same city and
    a past employer, the school anywhere at the company, past coworkers there, anyone there in that city, and the
    company's campus recruiters. LinkedIn's own search pages for company + school / employer; Google for anything with
    a city (LinkedIn's location filter needs place codes Tailor cannot check)."""
    if not company:
        return []
    co = _company_term(company)
    city_places = [p for p in places if p != "Remote"][:2]
    schools = sorted([t for t in ties if t.kind in ("school", "program", "club")], key=lambda t: -t.weight)
    employers = sorted([t for t in ties if t.kind == "employer"], key=lambda t: -t.weight)
    out: list[Search] = []

    def add(group: str, label: str, url: str, why: str) -> None:
        if sum(1 for s in out if s.group == group) < max_per_group:
            out.append(Search(len(out) + 1, group, label, url, why))

    for city in city_places:
        for t in schools:
            add("Location + alumni", f"{t.short} alumni at {company} in {city}",
                _google(f'site:linkedin.com/in {co} "{t.search}" "{city}"'),
                f"Same school and same office: the warmest ask for a referral to the {city} team.")
    for city in city_places:
        for t in employers:
            add("Location + past coworkers", f"Former {t.short} people at {company} in {city}",
                _google(f'site:linkedin.com/in {co} "{t.search}" "{city}"'),
                f"You both worked at {t.short}, and they sit in the {city} office.")
    for t in schools:  # a school's own alumni tool first: it lists every alum at the company, sortable by city
        if t.alumni_page:
            add("Alumni at the company", f"{t.short} alumni tool: {company}",
                f"https://www.linkedin.com/school/{quote_plus(t.alumni_page)}/people/?keywords=" + quote_plus(company),
                f"LinkedIn's alumni page for {t.short}, filtered to the company: sort by where they live.")
    for t in schools:
        add("Alumni at the company", f"{t.short} alumni at {company}", _linkedin_people(f'{co} "{t.search}"'),
            "Alumni answer alumni: a shared school is the easiest opener.")
    for t in employers:
        add("Past coworkers at the company", f"Former {t.short} people at {company}", _linkedin_people(f"{co} {t.search}"),
            f"Someone who also worked at {t.short} knows what that job taught you.")
    for city in city_places:
        add("Same city", f"People at {company} in {city}", _google(f'site:linkedin.com/in {co} "{city}"'),
            "Same office: they can tell the hiring team about you directly.")
    add("Recruiters", f"University / campus recruiters at {company}",
        _linkedin_people(f"{co} university recruiter"),
        "Campus recruiters own the new-grad and MBA pipelines; a short, specific note is welcome.")
    add("Recruiters", f"Talent acquisition at {company}" + (f" in {city_places[0]}" if city_places else ""),
        _google(f'site:linkedin.com/in {co} ("recruiter" OR "talent acquisition")'
                + (f' "{city_places[0]}"' if city_places else "")),
        "The recruiter for the role's office.")
    return out


# ---- the recruiter a posting names ---------------------------------------------------------------------------------

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def posting_contacts(text: str) -> list[dict]:
    """People a posting names as its contact (12twenty school boards: "Primary Contact / Name / ... / Job Title / ... / Email /
    ..."), and recruiting email addresses it gives. Only what the posting itself says."""
    found: list[dict] = []
    t = text or ""
    for m in re.finditer(r"Primary (?:Job )?Contact\s*(?:\n\s*Contact)?\s*\n\s*(?:Name\s*\n\s*)?([^\n]{3,60})\n"
                         r"(?:\s*Job Title\s*\n)?\s*([^\n]{2,80})?", t):
        name, title = m.group(1).strip(), (m.group(2) or "").strip()
        if name in ("--", "Name") or not re.search(r"[A-Za-z]", name):
            continue
        tail = t[m.end(): m.end() + 300]
        email = _EMAIL.search(tail)
        if title in ("--", "Job Title"):
            title = ""
        found.append({"name": name, "title": title, "email": email.group(0) if email else "",
                      "source": "named on the posting"})
    for e in dict.fromkeys(_EMAIL.findall(t)):
        if re.search(r"recruit|talent|career|hiring|jobs|campus|university", e, re.I) and not any(c["email"] == e for c in found):
            found.append({"name": "", "title": "Recruiting inbox", "email": e, "source": "email in the posting"})
    return found[:5]


# ---- message drafts ------------------------------------------------------------------------------------------------

@dataclass
class Draft:
    relation: str     # "connection" | "alum" | "coworker" | "local" | "recruiter"
    label: str
    note: str         # LinkedIn connection note (300 characters at most)
    message: str      # a longer message or email


def _school_line(person: dict) -> str:
    return person.get("school_line") or "my degree"


def message_drafts(person: dict, company: str, title: str, place: str = "", tie: Tie | None = None,
                   coworker: Tie | None = None, link: str = "", ref: str = "") -> list[Draft]:
    """Short referral asks in the person's voice, from their real facts only. {first_name} is the reader's first name,
    filled in when a saved contact is used. Point first, one real fact, a clear and easy ask; no em dashes."""
    me = person.get("name", "")
    grad = person.get("grad", "May 2027")
    role = f"the {title} role" if title else "an open role"
    where = f" on the {place} team" if place and place != "Remote" else ""
    link_line = f" Here is the posting: {link}" if link else ""
    school = tie.label if tie else person.get("school", "my school")
    past = coworker.label if coworker else person.get("past", "a past employer")
    study = person.get("study", "my degree")        # "my MBA at Example Business School"
    student = person.get("student", "a graduate student")  # "an Example Business School MBA student"
    ref_line = f" (job ID {ref})" if ref else ""
    drafts = [
        Draft("connection", "Your connection (ask for the referral)",
              f"Hi {{first_name}}, hope you're well! I'm applying for {role} at {company}{ref_line}. Would you be "
              f"comfortable referring me? I can send my resume and a short summary to make it easy.",
              f"Hi {{first_name}},\n\nHope you're doing well! I'm finishing {_school_line(person)} in {grad} and applying "
              f"for {role} at {company}{where}{ref_line}.{link_line}\n\nSince you're there, would you be comfortable "
              f"referring me? To make it easy, I can send my resume and a two-line summary you can paste into the referral "
              f"form. If you'd rather not, I'd still love 15 minutes to hear what the team is like.\n\nThank you!\n{me}"),
        Draft("alum", f"Fellow alum ({school})",
              f"Hi {{first_name}}, we share {school}. I'm finishing {study} ({grad}) and applying for {role} at "
              f"{company}. Would you be open to a quick chat about your team?",
              f"Hi {{first_name}},\n\nI'm {me}, finishing {_school_line(person)} in {grad}, and I'm applying for {role} at "
              f"{company}{where}. I saw that you're also from {school}, so I hoped you might share how you found your way to "
              f"{company} and what the team values.{link_line}\n\nIf it seems like a fit after we talk, I'd be grateful for a "
              f"referral, but a 15-minute conversation would already help a lot. Thank you for considering it.\n\n{me}"),
        Draft("coworker", f"Former coworker ({past})",
              f"Hi {{first_name}}, we both worked at {past}. I'm now finishing {study} and applying for {role} at "
              f"{company}. Could I ask you a few questions about it?",
              f"Hi {{first_name}},\n\nWe both spent time at {past}, and I'm reaching out because I'm applying for {role} at "
              f"{company}{where}. Since then I've been finishing {_school_line(person)}, graduating {grad}.{link_line}\n\n"
              f"Would you have 15 minutes to tell me about the team? If you think I'd be a good fit, a referral would mean a "
              f"lot. Thanks either way.\n\n{me}"),
        Draft("local", f"Same city{f' ({place})' if place and place != 'Remote' else ''}",
              f"Hi {{first_name}}, I'm {student}, graduating {grad}, applying for {role} at {company}"
              f"{where}. Would you be open to a short chat about working there?",
              f"Hi {{first_name}},\n\nI'm {me}, finishing {_school_line(person)} in {grad}, and I'm applying for {role} at "
              f"{company}{where}. I'd love to hear what the team is like from someone in that office.{link_line}\n\n"
              f"Would you have 15 minutes in the next couple of weeks? If it goes well and you're comfortable, I'd be "
              f"grateful for a referral. Thank you.\n\n{me}"),
        Draft("recruiter", "Recruiter (after you apply)",
              f"Hi {{first_name}}, I just applied for {role} at {company}. I'm finishing {study} in {grad} "
              f"and would welcome the chance to connect.",
              f"Hi {{first_name}},\n\nI recently applied for {role} at {company}{where} and wanted to introduce myself. I'm "
              f"{me}, finishing {_school_line(person)} in {grad}.{link_line}\n\nIf there's anything else that would help my "
              f"application, or a better person to speak with on the team, I'd appreciate the pointer. Thank you for your "
              f"time.\n\n{me}"),
    ]
    for d in drafts:
        d.note = _fit_note(no_dashes(d.note))
        d.message = no_dashes(d.message)
    return drafts


def no_dashes(text: str) -> str:
    """The person's rule (2026-10-10): no em or en dashes in anything written for them."""
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    return re.sub(r" {2,}", " ", text)


def _fit_note(note: str, limit: int = 300) -> str:
    """LinkedIn connection notes hold 300 characters; a longer one loses its last sentence, never a cut-off word."""
    if len(note) <= limit:
        return note
    parts = re.split(r"(?<=[.?!])\s+", note)
    while len(parts) > 1 and len(" ".join(parts)) > limit:
        parts.pop()
    return " ".join(parts)[:limit]


def fill_name(text: str, name: str) -> str:
    first = (name or "").strip().split(" ")[0] if name else ""
    return text.replace("{first_name}", first or "there")


# ---- a contact's ties and score ------------------------------------------------------------------------------------

def contact_ties(contact: dict, ties: list[Tie], places: list[str], home: str, company: str = "") -> list[str]:
    """What connects the person with this contact, from what the person wrote about them (headline, notes, location)."""
    text = " ".join(str(contact.get(k, "")) for k in ("headline", "title", "notes", "education", "past", "role", "snippet")).lower()
    hit = [t for t in ties if any(w in text for w in t.words)]
    covered = {t.within for t in hit if t.within}
    found = [t.label for t in hit if t.label not in covered]
    where = metro_of(contact.get("location", "") or "")
    if where and any(where == metro_of(p) for p in places):
        found.append(f"same city as the role ({where})")
    elif where and where == metro_of(home):
        found.append(f"near you ({where})")
    if contact.get("connection"):  # 1st-degree, from the person's own LinkedIn export
        found.insert(0, "1st-degree connection")
    own = " ".join(str(contact.get(k, "")) for k in ("headline", "title", "role", "notes")).lower()
    if re.search(r"recruit|talent acquisition|university relations|campus", own):
        found.append("recruiter")
    return list(dict.fromkeys(found))


def contact_score(found: list[str], ties: list[Tie]) -> float:
    """Location first (the person's priority), then the strongest shared school / employer, then a recruiter."""
    weights = {t.label: t.weight for t in ties}
    score = 0.0
    if any(f.startswith("same city as the role") for f in found):
        score += 3.0
    elif any(f.startswith("near you") for f in found):
        score += 1.5
    shared = sorted((weights[f] for f in found if f in weights), reverse=True)
    score += sum(w * (1.0 if i == 0 else 0.4) for i, w in enumerate(shared))
    if "recruiter" in found:
        score += 1.0
    if "1st-degree connection" in found:  # someone who already knows the person: the strongest referral ask
        score += 4.0
    return round(score, 2)


# ---- the person's saved contacts -----------------------------------------------------------------------------------

STATUSES = ("to contact", "messaged", "replied", "referred", "no reply", "not a fit")
FOLLOW_UP_DAYS = 5


class ContactStore:
    """Contacts the person saved (data/network/contacts.json). Only what they typed or pasted; never fetched."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "network" / "contacts.json"
        self.lock = threading.Lock()

    def load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(5)


def due_follow_ups(contacts: list[dict], today: date | None = None) -> list[dict]:
    """Contacts messaged at least FOLLOW_UP_DAYS ago with no reply yet."""
    today = today or date.today()
    out = []
    for c in contacts:
        if c.get("status") != "messaged" or not c.get("contacted_at"):
            continue
        try:
            sent = datetime.fromisoformat(c["contacted_at"]).date()
        except ValueError:
            continue
        if today - sent >= timedelta(days=FOLLOW_UP_DAYS):
            out.append(c)
    return out


# ---- companies to network into ---------------------------------------------------------------------------------------

def company_key(company: str) -> str:
    """One key per employer across its postings ("Partners in Performance (Accenture)" and "DoorDash USA")."""
    name = re.sub(r"\s*\(.*?\)\s*", " ", company or "").strip().lower()
    name = re.sub(r"\b(?:inc|llc|corp|corporation|co|usa|us|group|the)\b\.?", " ", name)
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def relation_of(found: list[str], ties: list[Tie]) -> str:
    """Which kind of message fits a contact: a shared school first, then a past employer, a recruiter, else local."""
    kinds = {t.label: t.kind for t in ties}
    if "1st-degree connection" in found:
        return "connection"
    if any(kinds.get(f) in ("school", "program", "club") for f in found):
        return "alum"
    if any(kinds.get(f) == "employer" for f in found):
        return "coworker"
    if "recruiter" in found:
        return "recruiter"
    return "local"


def follow_up_draft(person: dict, company: str, title: str, name: str = "") -> str:
    """A short, polite nudge a few days after a first message with no reply."""
    role = f"the {title} role" if title else "the role"
    text = (f"Hi {{first_name}}, following up on my note about {role} at {company}. I know things get busy, so no "
            f"pressure at all. If a short chat in the next week or two works, I'd really appreciate it. Thank you!\n\n"
            f"{person.get('name', '')}")
    return fill_name(no_dashes(text), name)


# ---- a profile's top text, pasted by the person ---------------------------------------------------------------------

_NOISE = re.compile(r"^(?:\d+(?:st|nd|rd|th)\b|· ?\d|contact info|connect|message|follow|more|pending|\d+\+? (?:connections|followers)|"
                    r"he/him|she/her|they/them|open to|show all|about|activity|view profile|premium|mutual)", re.I)


def parse_profile_text(text: str) -> dict:
    """Name, headline and city from the top of a profile the person copied (its first lines). Only what the text says;
    the person checks the fields before saving."""
    lines = [re.sub(r"\s+", " ", ln).strip(" ·•|") for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln and not _NOISE.match(ln)][:15]
    out = {"name": "", "headline": "", "location": ""}
    for i, ln in enumerate(lines):
        words = ln.split()
        if not out["name"] and 2 <= len(words) <= 5 and not re.search(r"[\d@|,]", ln) and all(w[:1].isupper() for w in words if w[0].isalpha()):
            out["name"] = ln
            rest = lines[i + 1:]
            if rest:
                out["headline"] = rest[0][:300]
            for r in rest[1:6]:
                if metro_of(r) or re.search(r"\b(?:Area|Metropolitan|United States|[A-Z]{2})$", r):
                    out["location"] = r[:120]
                    break
            break
    return out


# ---- outcomes ------------------------------------------------------------------------------------------------------

def outreach_stats(contacts: list[dict], ties: list[Tie]) -> dict:
    """How outreach is going: counts by status, and for each kind of tie how many were messaged and how many answered
    (replied or referred), so the person sees which ties work."""
    by_status = {s: 0 for s in STATUSES}
    groups: dict[str, dict] = {}
    for c in contacts:
        by_status[c.get("status", "to contact")] = by_status.get(c.get("status", "to contact"), 0) + 1
        rel = c.get("relation") or "local"
        g = groups.setdefault(rel, {"saved": 0, "messaged": 0, "answered": 0, "referred": 0})
        g["saved"] += 1
        if c.get("contacted_at") or c.get("status") in ("messaged", "replied", "referred", "no reply"):
            g["messaged"] += 1
        if c.get("status") in ("replied", "referred"):
            g["answered"] += 1
        if c.get("status") == "referred":
            g["referred"] += 1
    for g in groups.values():
        g["reply_rate"] = round(100 * g["answered"] / g["messaged"]) if g["messaged"] else None
    return {"by_status": by_status, "by_relation": groups, "total": len(contacts)}


# ---- a page of search results, pasted by the person ------------------------------------------------------------------

_RESULT_ANCHOR = re.compile(r"^LinkedIn\s*[·•]\s*(.+?)\s*$")
_FOLLOWERS = re.compile(r"^[\d.,]+[KM]?\+?\s+(?:followers|connections)$", re.I)
_SLUG = re.compile(r"linkedin\.com\s*›\s*in\s*›\s*([A-Za-z0-9\-_%]+)")


def _looks_like_place(text: str) -> bool:
    return bool(metro_of(text) or re.search(r"(?:Area|Metropolitan|United States|, [A-Z]{2})$", text.strip()))


def _profile_link(url: str) -> str:
    """A clean linkedin.com/in/... link from a result's href (Google sometimes wraps it in /url?q=...)."""
    from urllib.parse import parse_qs, unquote, urlparse

    u = urlparse(url or "")
    if u.path == "/url":
        url = (parse_qs(u.query).get("q") or parse_qs(u.query).get("url") or [""])[0]
        u = urlparse(unquote(url))
    m = re.match(r"/in/([^/?#]+)", u.path or "")
    if not m or not (u.netloc or "").endswith("linkedin.com"):
        return ""
    return f"https://www.linkedin.com/in/{m[1]}"


def parse_search_results(text: str, links: list[dict] | None = None, company: str = "") -> list[dict]:
    """People in a page of web search results for LinkedIn profiles that the person selected and pasted (Google's
    "Name - Headline" title, then "LinkedIn · Name", then location / education lines). Only what the pasted text says;
    links come from the pasted page's own anchors. Nothing is fetched."""
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in (text or "").splitlines()]
    anchors = [i for i, ln in enumerate(lines) if _RESULT_ANCHOR.match(ln)]
    by_text = []
    for a in links or []:
        url = _profile_link(str(a.get("url", "")))
        if url:
            by_text.append((str(a.get("text", "")).strip(), url))
    want = company_key(company)
    people: list[dict] = []
    for n, i in enumerate(anchors):
        name = _RESULT_ANCHOR.match(lines[i])[1].strip()
        name = re.sub(r"\s*[,|].*$", "", name)
        if not name or len(name) > 80:
            continue
        # the result's title sits just above: "Name - Headline ..."
        headline = ""
        for j in range(i - 1, max(i - 4, -1), -1):
            if lines[j].lower().startswith(name.lower()):
                headline = re.sub(r"^\s*[-–—|]\s*", "", lines[j][len(name):]).rstrip(" .…")
                headline = re.sub(r"\s*\|\s*LinkedIn$", "", headline)
                break
        # this result's lines run to the next result's title
        end = len(lines)
        if n + 1 < len(anchors):
            nxt = _RESULT_ANCHOR.match(lines[anchors[n + 1]])[1].strip().lower()
            end = next((j for j in range(anchors[n + 1] - 1, i, -1) if lines[j].lower().startswith(nxt)), anchors[n + 1])
        body = [ln for ln in lines[i + 1:end] if ln and not _FOLLOWERS.match(ln)
                and ln not in ("About", "Experience", "Education", "Page Navigation")]
        location, role, education = "", "", ""
        for ln in body:
            parts = [p.strip() for p in re.split(r"\s+·\s+", ln) if p.strip()]
            if not location and parts and _looks_like_place(parts[0]) and len(parts) <= 4:
                location = parts[0]
                if len(parts) >= 2 and not role:
                    role = " · ".join(parts[1:])
            m = re.search(r"Location:\s*([^·.]+?)(?:\s*·|\s*\.\.\.|$)", ln)
            if m and not location:
                location = m[1].strip()
            m = re.search(r"Education:\s*([^·]+?)(?:\s*·|\s*\.\.\.|$)", ln)
            if m and not education:
                education = m[1].strip()
        snippet = " ".join(body)[:600]
        if headline.endswith("...") or len(headline) < 4:
            headline = headline.rstrip(". ")
        link = ""
        for t, url in by_text:
            if t.lower().startswith(name.lower()):
                link = url
                break
        if not link:
            m = _SLUG.search(" ".join(lines[max(i - 3, 0):end]))
            if m:
                link = f"https://www.linkedin.com/in/{m[1]}"
        mentions = bool(want) and want in company_key(" ".join([headline, role, snippet]))
        people.append({"name": name, "headline": headline or role, "role": role, "location": location,
                       "education": education, "snippet": snippet, "link": link, "at_company": mentions})
    # one entry per person (a page can repeat a result)
    seen, out = set(), []
    for p in people:
        k = (p["link"] or p["name"]).lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


# ---- the person's own connections (LinkedIn's "Get a copy of your data" export) ---------------------------------------

# employers known by several names (a connection's "Company" is whatever they typed)
COMPANY_ALIASES = {
    "facebook": "meta", "meta platforms": "meta", "alphabet": "google", "google llc": "google", "youtube": "google",
    "amazon web services": "amazon", "aws": "amazon", "amazon com": "amazon", "pricewaterhousecoopers": "pwc",
    "pwc us": "pwc", "ernst young": "ey", "ey parthenon": "ey", "boston consulting": "bcg",
    "boston consulting bcg": "bcg", "bcg x": "bcg", "bcg platinion": "bcg", "mckinsey company": "mckinsey",
    "bain company": "bain", "monitor deloitte": "deloitte", "deloitte consulting": "deloitte", "doordashusa": "doordash",
    "scaleai": "scale ai", "lek consulting": "l e k consulting", "l e k": "l e k consulting", "mmc": "marsh mclennan",
    "zs": "zs associates", "capital one financial": "capital one", "accenture strategy": "accenture",
    "micron technology": "micron", "lpl financial holdings": "lpl financial", "lplfinancial": "lpl financial",
    "marsh mclennan oliver wyman mercer": "marsh mclennan", "mercer": "marsh mclennan", "oliver wyman": "marsh mclennan",
    "mercer mmc": "marsh mclennan", "clearviewhealthcarepartners": "clearview healthcare partners",
    "motorolasolutions": "motorola solutions", "amat": "applied materials", "bah": "booz allen hamilton",
    "abnormalsecurity": "abnormal security", "thenewyorktimes": "new york times", "cvshealth": "cvs health",
}


def canonical_company(name: str) -> str:
    k = company_key(name)
    return COMPANY_ALIASES.get(k, k)


def same_company(a: str, b: str) -> bool:
    """One employer under two spellings: aliases, or one name starting the other ("Adobe" / "Adobe Systems")."""
    ka, kb = canonical_company(a), canonical_company(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = sorted((ka, kb), key=len)
    return len(short) >= 4 and long_.startswith(short + " ")


def parse_connections_csv(text: str) -> list[dict]:
    """LinkedIn's Connections.csv (a few lines of notes, then First Name, Last Name, URL, Email Address, Company,
    Position, Connected On). Only the person's own export; nothing is fetched."""
    import csv
    import io

    lines = (text or "").lstrip("\ufeff").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.lower().startswith("first name")), None)
    if start is None:
        return []
    out = []
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        name = " ".join(x for x in (row.get("first name", ""), row.get("last name", "")) if x)
        if not name:
            continue
        link = row.get("url", "")
        out.append({"name": name[:120], "link": link if link.startswith("https://www.linkedin.com/") else "",
                    "email": row.get("email address", "")[:200], "company": row.get("company", "")[:120],
                    "position": row.get("position", "")[:200], "connected_on": row.get("connected on", "")[:20]})
    return out


class ConnectionStore:
    """The person's imported connections (data/network/connections.json), replaced on each import."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "network" / "connections.json"
        self.lock = threading.Lock()

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"imported_at": "", "items": []}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def connections_at(company: str, connections: list[dict]) -> list[dict]:
    return [c for c in connections if same_company(c.get("company", ""), company)]


# ---- what a referrer needs -------------------------------------------------------------------------------------------

def job_ref(url: str, text: str = "") -> str:
    """The posting's job ID / requisition number, which most employee referral forms ask for."""
    m = re.search(r"(?:job|req(?:uisition)?)\s*(?:id|#|number|no\.?)\s*[:#]?\s*([A-Z]{0,4}[-_]?\d{4,}[\w-]*)", text or "", re.I)
    if m:
        return m[1]
    u = url or ""
    for pat in (r"_((?:JR|R|REQ)[-_]?\d{4,}(?:-\d+)?)(?:[/?#]|$)", r"gh_jid=(\d{5,})", r"/jobs/(\d{6,})",
                r"jobid=(\d{4,})", r"/job/(BCG\w+?)EXTERNAL", r"jobs\.lever\.co/[^/]+/([0-9a-f-]{36})",
                r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f-]{36})"):
        m = re.search(pat, u, re.I)
        if m:
            return m[1]
    return ""


def referral_blurb(person: dict, title: str, company: str, strengths: list[str] | None = None) -> str:
    """Two lines a referrer can paste into their company's referral form. Only the person's real facts."""
    first = f"{person.get('name', '')} is finishing {_school_line(person)} ({person.get('grad', 'May 2027')}) and is " \
            f"applying for {('the ' + title) if title else 'a role'} at {company}."
    second = f" Strengths: {', '.join(strengths[:3])}." if strengths else ""
    return no_dashes(first + second)

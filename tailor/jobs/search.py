"""Role search: read companies' public job feeds, keep the roles that fit the person's preferences, rank them against the
resume, and remember what was already shown.

Feeds (all public, read-only, no sign-in):
  * Greenhouse  boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true
  * Lever       api.lever.co/v0/postings/<company>?mode=json
  * Ashby       api.ashbyhq.com/posting-api/job-board/<org>
  * Workday     <tenant>.wdN.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs  (the JSON behind each careers site)
  * Sites       careers sites read as a visitor would: Google Careers (early-career level), BCG, and the public
                community new-grad list on GitHub (its Product Management section)

Nothing here applies to anything. Saving a result puts it on the Tailor Desk; the person applies.

Storage: <data_dir>/search/prefs.json (what to look for) and results.json (every role seen: when first and last seen,
dismissed or saved).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from .facts import extract_facts
from .text import clean_job_text

UA = {"User-Agent": "project-tailor-role-search (personal use; one person's job search)"}

# ----------------------------------------------------------------------------------------------- preferences

FOCUS = {  # what a role is about -> title words
    "product": r"product manag|associate product|\bapm\b|product (?:analyst|operations|strategy|owner)",
    "strategy_ops": r"\bstrategy\b|\bstrategist\b|business operations|biz ?ops|\boperations (?:analyst|associate|manager)|chief of staff|"
                    r"strategy (?:&|and) operations|\bs&o\b|business analyst|program manager",
    # people-facing research, not AI research ("Researcher, Alignment")
    "ux_research": r"ux research|user research|user experience research|design research|mixed[- ]methods|"
                   r"(?:ux|user|product|design|market|consumer|qualitative|quantitative ux) researcher|"
                   r"research (?:associate|analyst)",
    "consulting": r"consultant|consulting|advisory|\badvisor\b",
    # programs, not any title that says "2027" or "new grad" (those only raise a matching role's rank: see EARLY)
    "early_programs": r"rotational|leadership (?:development )?program|development program|graduate program|"
                      r"associate program|\bmba\b",
    "analytics": r"business intelligence|data analyst|analytics|insights analyst",
    "product_marketing": r"product marketing",
}
FOCUS_LABELS = {"product": "Product management", "strategy_ops": "Strategy & operations", "ux_research": "UX research",
                "consulting": "Consulting", "early_programs": "New-grad & rotational programs",
                "analytics": "Analytics", "product_marketing": "Product marketing"}

CONSULTING_FIRMS = ["thebrattlegroup", "keystone", "charlesriverassociates", "chartis", "accordion", "capco", "elixirr",
                    "alixpartners", "pariveda", "secretariatadvisorsllc", "cornerstone", "fticonsulting"]
_FIRM_ENTRY = re.compile(r"^(?:research |economic |economics |litigation |business )?(?:analyst|associate|consultant)\b"
                         r"|\b(?:analyst|associate|consultant)\s*(?:[|,(-]|$)", re.I)

DEFAULT_COMPANIES = {
    "greenhouse": ["figma", "stripe", "airbnb", "discord", "duolingo", "databricks", "robinhood", "coinbase", "pinterest",
                   "instacart", "lyft", "reddit", "dropbox", "asana", "roblox", "twitch", "affirm", "chime", "gusto",
                   "brex", "anthropic", "scaleai", "samsara", "datadog", "mongodb", "squarespace", "peloton", "airtable",
                   "calendly", "grammarly", "webflow", "flexport", "toast", "waymo", "verkada", "doordashusa", "gitlab",
                   "okta", "cloudflare", "elastic", "twilio", "etsy", "carta", "thumbtack", "faire", "oscar", "lattice",
                   "vercel", "nuro", "headway"],
    "lever": ["palantir", "spotify", "attentive", "kraken", "outreach", "wealthfront", "zoox"],
    "ashby": ["ramp", "openai", "notion", "linear", "plaid", "vanta", "perplexity", "cursor", "harvey"],
    "sites": ["google", "bcg", "newgrad_pm"],
    "workday": ["https://accenture.wd103.myworkdayjobs.com/AccentureCareers",
                "https://pwc.wd3.myworkdayjobs.com/Global_Experienced_Careers",
                "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
                "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site",
                "https://adobe.wd5.myworkdayjobs.com/external_experienced",
                "https://capitalone.wd12.myworkdayjobs.com/Capital_One"],
}


@dataclass
class Prefs:
    focus: list[str] = field(default_factory=lambda: ["product", "strategy_ops", "ux_research", "consulting",
                                                      "early_programs"])
    include_words: list[str] = field(default_factory=list)  # extra title words to look for
    exclude_words: list[str] = field(default_factory=list)  # titles with these are never shown
    locations: list[str] = field(default_factory=list)  # empty = anywhere in the U.S.
    remote_ok: bool = True
    # graduate, MBA and early-career programs anywhere in the U.S. are kept even outside `locations` (marked so)
    early_anywhere: bool = True
    max_years: int = 3  # the posting's highest years-of-experience bar
    skip_no_sponsorship: bool = False
    companies: dict = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_COMPANIES.items()})
    daily: bool = True
    # once a week, before the daily search: add boards found by discover() that list 2+ matching roles right now
    auto_discover: bool = True
    daily_hour: int = 8
    auto_save: bool = True  # after each search, save and tailor the best NEW roles, ready on the Desk
    auto_min_fit: int = 65
    auto_max: int = 5
    # consulting firms whose entry roles are titled just "Analyst" / "Associate" / "Consultant" (Secretariat's
    # "Associate, Damages & Valuations", Compass Lexecon's "Analyst | Competition Practice"): those count as consulting
    consulting_firms: list[str] = field(default_factory=lambda: list(CONSULTING_FIRMS))

    @classmethod
    def load(cls, path: Path) -> "Prefs":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known).validated()

    def validated(self) -> "Prefs":
        self.focus = [f for f in self.focus if f in FOCUS] or ["product"]
        clean = lambda xs: [x.strip() for x in xs if isinstance(x, str) and x.strip()][:50]  # noqa: E731
        self.include_words, self.exclude_words, self.locations = map(clean, (self.include_words, self.exclude_words,
                                                                             self.locations))
        self.max_years = max(0, min(int(self.max_years), 15))
        self.daily_hour = max(0, min(int(self.daily_hour), 23))
        self.auto_min_fit = max(0, min(int(self.auto_min_fit), 100))
        self.auto_max = max(0, min(int(self.auto_max), 20))
        comp = {}
        for kind in DEFAULT_COMPANIES:  # up to 150 companies per feed (the 50 cap above is for words and places)
            items = [x.strip() for x in self.companies.get(kind, []) if isinstance(x, str) and x.strip()][:150]
            if kind not in self.companies:  # a feed added after the prefs were saved starts with its defaults
                items = list(DEFAULT_COMPANIES[kind])
            if kind == "workday":
                items = [u for u in items if _WORKDAY.match(u)]
            elif kind == "sites":
                items = [u for u in items if u in SITES]
            else:
                items = [re.sub(r"[^a-z0-9_-]", "", u.lower()) for u in items]
            comp[kind] = [x for x in dict.fromkeys(items) if x][:150]
        self.companies = comp
        return self

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------------------------- feeds

_WORKDAY = re.compile(r"^https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)/?$")


def _text(h: str) -> str:
    h = html.unescape(h or "")
    h = re.sub(r"<(?:br|/p|/li|/h\d|/div)[^>]*>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"[ \t]+", " ", html.unescape(h)).strip()


def _job(source: str, company: str, title: str, url: str, location: str, text: str, posted: str = "") -> dict:
    return {"id": hashlib.sha1(f"{source}|{url}".encode()).hexdigest()[:12], "source": source, "company": company,
            "title": re.sub(r"\s+", " ", title or "").strip(), "url": url, "location": (location or "").strip(),
            "text": text or "", "posted": posted or ""}


def greenhouse(client: httpx.Client, board: str) -> list[dict]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
    if r.status_code != 200:
        return []
    return [_job("greenhouse", board, j.get("title", ""), j.get("absolute_url", ""),
                 (j.get("location") or {}).get("name", ""), _text(j.get("content", "")), j.get("updated_at", ""))
            for j in r.json().get("jobs", [])]


def lever(client: httpx.Client, company: str) -> list[dict]:
    r = client.get(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
    if r.status_code != 200 or not isinstance(r.json(), list):
        return []
    out = []
    for j in r.json():
        lists = "\n".join(f"{x.get('text', '')}\n{_text(x.get('content', ''))}" for x in j.get("lists", []))
        text = f"{j.get('descriptionPlain', '')}\n{lists}\n{j.get('additionalPlain', '')}"
        posted = datetime.fromtimestamp(j["createdAt"] / 1000).isoformat() if j.get("createdAt") else ""
        out.append(_job("lever", company, j.get("text", ""), j.get("hostedUrl", ""),
                        (j.get("categories") or {}).get("location", ""), text, posted))
    return out


def ashby(client: httpx.Client, org: str) -> list[dict]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", params={"includeCompensation": "true"})
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "") + (" · Remote" if j.get("isRemote") else "")
        text = j.get("descriptionPlain") or _text(j.get("descriptionHtml", ""))
        comp = (j.get("compensation") or {}).get("compensationTierSummary", "")
        out.append(_job("ashby", org, j.get("title", ""), j.get("jobUrl", ""), loc,
                        f"{text}\n{comp}" if comp else text, j.get("publishedAt", "")))
    return out


WORKDAY_TERMS = ["product manager", "strategy", "operations", "analyst", "consultant", "associate", "research",
                 "new grad", "early career"]
# graduate hiring has its own words; these get a second page of results and their roles are read first
WORKDAY_GRAD_TERMS = ["MBA", "2027", "university graduate", "campus", "leadership development", "rotational",
                      "associate product manager", "associate consultant"]


def workday(client: httpx.Client, url: str, wanted: Callable[[str], bool], limit_details: int = 60) -> list[dict]:
    """Search the site for a few terms, then read details only for titles that already pass `wanted`: the list does
    not say which country a role is in, the detail page does."""
    m = _WORKDAY.match(url)
    if not m:
        return []
    tenant, wd, site = m.groups()
    host = f"{tenant}.{wd}.myworkdayjobs.com"
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    found: dict[str, dict] = {}
    def page(term: str, offset: int) -> list[dict] | None:
        try:
            r = client.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
        except httpx.HTTPError:
            return []
        return r.json().get("jobPostings", []) if r.status_code == 200 else None

    for term in WORKDAY_GRAD_TERMS + WORKDAY_TERMS:
        for offset in ((0, 20) if term in WORKDAY_GRAD_TERMS else (0,)):
            posts = page(term, offset)
            if posts is None:  # the site refuses searches: stop asking
                break
            for p in posts:
                path = p.get("externalPath", "")
                if path and path not in found and wanted(p.get("title", "")):
                    found[path] = p
            if len(posts) < 20:
                break
        if posts is None:
            break
    # early-career titles first: the detail reads are capped
    order = sorted(found.items(), key=lambda kv: not EARLY.search(kv[1].get("title", "")))
    out = []
    for path, p in order[:limit_details]:
        try:
            d = client.get(f"{api}{path}").json().get("jobPostingInfo", {})
        except (httpx.HTTPError, ValueError):
            continue
        country = (d.get("country") or {}).get("descriptor", "")
        loc = ", ".join(x for x in (d.get("location", ""), country) if x)
        out.append(_job("workday", tenant, d.get("title") or p.get("title", ""),
                        d.get("externalUrl") or f"https://{host}/{site}{path}", loc, _text(d.get("jobDescription", "")),
                        d.get("startDate", "")))
    return out


BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
GOOGLE_QUERIES = ["product manager", "associate product manager", "strategy", "business analyst", "consultant",
                  "program manager", "operations", "user experience researcher", "MBA"]


def google_careers(client: httpx.Client, queries: list[str] | None = None, pages: int = 5) -> list[dict]:
    """Google's early-career roles in the U.S. from its careers search pages (the HTML a visitor gets)."""
    out: dict[str, dict] = {}
    base = "https://www.google.com/about/careers/applications/jobs/results/"
    for q in queries or GOOGLE_QUERIES:
        for page in range(1, pages + 1):
            try:
                r = client.get(base, params={"target_level": "EARLY", "location": "United States", "q": q, "page": page},
                               headers=BROWSER_UA)
            except httpx.HTTPError:
                break
            if r.status_code != 200:
                break
            got = 0
            for chunk in re.split(r"<h3\b[^>]*>", r.text)[1:]:
                m = re.search(r"jobs/results/(\d{10,})", chunk)
                if not m:
                    continue
                got += 1
                title = html.unescape(re.sub(r"<.*?>", "", chunk.split("</h3>")[0])).strip()
                lines = [ln.strip() for ln in html.unescape(re.sub(r"<[^>]+>", "\n", chunk)).splitlines() if ln.strip()]
                locs = list(dict.fromkeys(ln for ln in lines if ln.endswith(", USA")))
                if m[1] not in out and locs:
                    text = "\n".join(ln for ln in lines if ln not in ("corporate_fare", "place", "bar_chart", "share",
                                                                      "bookmark_border", "Learn more"))
                    out[m[1]] = _job("google", "google", title, base + m[1], "; ".join(locs), text)
            if got < 20:
                break
    return list(out.values())


def bcg_careers(client: httpx.Client, keywords: tuple[str, ...] = ("consultant", "associate", "product")) -> list[dict]:
    """BCG's U.S. openings: its careers search embeds the results as JSON (phApp.ddo)."""
    out: dict[str, dict] = {}
    for kw in keywords:
        for start in range(0, 200, 10):
            try:
                r = client.get("https://careers.bcg.com/global/en/search-results",
                               params={"keywords": kw, "from": start, "s": 1}, headers=BROWSER_UA)
                m = re.search(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp", r.text, re.S)
                data = json.loads(m[1]).get("eagerLoadRefineSearch", {}) if m else {}
            except (httpx.HTTPError, ValueError):
                break
            jobs = (data.get("data") or {}).get("jobs") or []
            for j in jobs:
                seq = j.get("jobSeqNo", "")
                if seq and seq not in out and "United States" in (j.get("country") or ""):
                    loc = ", ".join(x for x in (j.get("city", ""), "United States") if x)
                    out[seq] = _job("bcg", "bcg", j.get("title", ""), f"https://careers.bcg.com/global/en/job/{seq}", loc,
                                    _text(j.get("descriptionTeaser", "")), j.get("postedDate", ""))
            if len(jobs) < 10 or start + 10 >= (data.get("totalHits") or 0):
                break
    return list(out.values())


NEWGRAD_LIST = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"


def newgrad_pm(client: httpx.Client) -> list[dict]:
    """The Product Management section of the public community new-grad list on GitHub (active roles only)."""
    try:
        r = client.get(NEWGRAD_LIST)
    except httpx.HTTPError:
        return []
    if r.status_code != 200:
        return []
    m = re.search(r"##[^\n]*Product Management New Grad Roles(.*?)(?:<summary>|\n## )", r.text, re.S)
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", m[1] if m else "", re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 4:
            continue
        clean = lambda h: html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>", "; ", h))).strip()  # noqa: E731
        company = re.sub(r"^[^\w]+", "", clean(tds[0])).strip()
        link = (re.findall(r'href="([^"]+)"', tds[3]) or [""])[0]
        link = re.sub(r"[?&]utm_source=Simplify.*$|[?&]ref=Simplify.*$", "", link)
        if company and link:
            title = re.sub(r"\s+", " ", clean(tds[1]).replace("🎓", "")).strip()
            out.append(_job("newgrad", company, title, link, clean(tds[2]), f"{title}\nNew grad role at {company}."))
    return out


SITES: dict[str, Callable[[httpx.Client], list[dict]]] = {"google": google_careers, "bcg": bcg_careers,
                                                         "newgrad_pm": newgrad_pm}
SITE_NAMES = {"google": "Google", "bcg": "BCG"}


# ----------------------------------------------------------------------------------------------- filtering and ranking

TITLE_NEVER = re.compile(r"\b(?:ph\.?d|doctoral|billing|construction (?:delay|quantum)|"  # PhD-only, billing, construction claims
                         r"engineer(?:ing)?|scientist|developer|counsel|attorney|legal|paralegal|recruit(?:er|ing)|"
                         r"sales|account (?:executive|manager)|designer|design|security|interns?(?:hips?)?|co-?op|warehouse|"
                         r"area manager|member of technical staff|ai researcher|ml researcher|research (?:scientist|engineer)|"
                         r"hardware|circuit|asic|verification|"
                         r"technician|nurse|physician|accountant|tax|development representative|technical program|"
                         r"sourcing|procurement|policy|people operations|payroll|facilities|driver|mechanic|"
                         r"customer support|support specialist|talent|people partner|hrbp|human resources|architect|"
                         r"business development|field marketing|\bae\b|customer care|content creator|account strategist|"
                         r"summer (?:associate|analyst|consultant)|actuar(?:y|ial)|clinical|"
                         r"(?:solutions?|implementation|onboarding|support|technical) consultant)\b", re.I)
# campus roles open only to bachelor's students (the person is in master's programs): West Monroe's "Bachelor's degree in
# progress", BGBx's "May 2027 & June 2027 graduates" with a B.S./B.A., unless the posting also names master's or MBA students
UNDERGRAD_ONLY = re.compile(r"bachelor[’']?s degree in progress|currently (?:pursuing|enrolled in) (?:a |an |their )?"
                            r"(?:undergraduate|bachelor)|undergraduate (?:seniors|students) only|rising (?:college )?seniors", re.I)
GRAD_OK = re.compile(r"master|\bmba\b|graduate (?:degree|students?|program)", re.I)


def undergrad_only(text: str) -> bool:
    return bool(UNDERGRAD_ONLY.search(text or "")) and not GRAD_OK.search(text or "")


TITLE_SENIOR = re.compile(r"\b(?:senior|sr\.?|staff|principal|director|head|vp|vice president|lead|l[3-9]|iii|ii|"
                          r"expert|project leader|experienced hire|engagement manager|consulting manager|managing consultant|"
                          r"mid-level|group product|strategic leader|partner)\b|^manager\b|\bmanager,", re.I)
EARLY = re.compile(r"new grad|early career|rotational|\bapm\b|associate product|university|graduate program|2027|"
                   r"\bmba\b|entry[- ]level|campus", re.I)
# a posting written for graduates (its text, not just a word in the title): the location rule relaxes for these
GRAD_HIRE = re.compile(r"\bmba\b|new (?:college )?grad|recent graduates?|university graduate|class of 2027|"
                       r"2027 (?:start|graduates?)|graduating (?:in|by) (?:december|may|spring|june)|campus (?:hire|recruit)|"
                       r"leadership development program|rotational program|early[- ]career program", re.I)


def early_hire(job: dict) -> bool:
    return bool(EARLY.search(job.get("title", "")) or GRAD_HIRE.search((job.get("text") or "")[:6000]))
_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK "
           "OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
US = re.compile(r"united states|u\.s\.|\bSF\b|\bNYC\b|new york|san francisco|seattle|austin|boston|chicago|los angeles|"
                r"mountain view|palo alto|menlo park|san mateo|bellevue|denver|atlanta|washington|detroit|ann arbor|"
                r"miami|san jose|sunnyvale|oakland|brooklyn|pittsburgh|philadelphia|tempe|salt lake|dallas|houston|"
                r"arlington|mclean|charlotte|minneapolis|nashville|raleigh|phoenix|portland|san diego|irvine", re.I)
# State codes in capitals only: "in office" is not Indiana, "or" is not Oregon.
US_STATE = re.compile(r"(?:,|\s|^)(?:" + "|".join(_STATES) + r")\b")
NOT_US = re.compile(r"canada|toronto|vancouver|montreal|london|united kingdom|\buk\b|ireland|dublin|germany|berlin|munich|"
                    r"france|paris|netherlands|amsterdam|spain|madrid|estonia|poland|india|bangalore|bengaluru|hyderabad|"
                    r"singapore|japan|tokyo|australia|sydney|mexico|brazil|argentina|buenos aires|israel|tel aviv|korea|"
                    r"china|emea|apac|latam|philippines|costa rica|colombia|chile|switzerland|zurich|sweden|italy", re.I)


class Filter:
    def __init__(self, prefs: Prefs):
        self.prefs = prefs
        words = [FOCUS[f] for f in prefs.focus] + [re.escape(w) for w in prefs.include_words]
        self.title_in = re.compile("|".join(words), re.I)
        self.title_out = re.compile("|".join(re.escape(w) for w in prefs.exclude_words), re.I) if prefs.exclude_words else None
        # the person's own words win over the built-in exclusions ("designer" brings design roles back)
        self.title_mine = re.compile("|".join(re.escape(w) for w in prefs.include_words), re.I) if prefs.include_words else None

    def title_ok(self, title: str, company: str = "") -> bool:
        return self.check_title(title, company) is None

    def check_title(self, title: str, company: str = "") -> str | None:
        firm = company.lower() in {c.lower() for c in self.prefs.consulting_firms}
        if not self.title_in.search(title) and not (firm and _FIRM_ENTRY.search(title)):
            return "not a role you asked for"
        if self.title_out and self.title_out.search(title):
            return "excluded word"
        if TITLE_NEVER.search(title) and not (self.title_mine and self.title_mine.search(title)):
            return "different kind of role"
        if TITLE_SENIOR.search(title) and not EARLY.search(title):
            return "too senior"
        return None

    def check_location(self, loc: str) -> str | None:
        low = loc.lower()
        if re.search(r"\bUSA?\b", loc):  # capitals only: "us" is also an English word
            loc = loc + " United States"
        remote = "remote" in low
        us = bool(US.search(loc) or US_STATE.search(loc))
        if NOT_US.search(loc) and not us:
            return "outside the U.S."
        if loc and not us and not remote:
            return "outside the U.S."
        if remote and not self.prefs.remote_ok and not self.prefs.locations:
            return "remote"
        if self.prefs.locations and not any(p.lower() in low for p in self.prefs.locations):
            if not (remote and self.prefs.remote_ok):
                return "not in your locations"
        return None

    def check(self, job: dict) -> str | None:
        return self.check_title(job["title"], job.get("company", "")) or self.check_location(job["location"])


def enrich(job: dict) -> dict:
    """Facts a person checks before applying: the years asked for, the deadline, sponsorship wording."""
    facts = extract_facts(clean_job_text(f"{job['title']}\n\n{job['text']}"))
    years = [y.years for y in facts.min_years if 1 <= y.years <= 15]
    job["years"] = max(years) if years else None  # the real bar: "7+ years of strategy and operations" beats "2+ SQL"
    job["deadline"] = facts.deadline.isoformat() if facts.deadline else None
    job["sponsorship"] = facts.sponsorship
    job["clearance"] = bool(re.search(r"security clearance|clearance required|active (?:ts|secret)|u\.?s\.? citizenship "
                                      r"(?:is )?required|must be a u\.?s\.? citizen", job["text"], re.I))
    return job


def fit_score(job: dict, resume_text: str) -> int:
    """0-100: how much of what the posting stresses the resume shows, plus a nudge for early-career roles."""
    from ..resume.auto import _relevance, job_concepts  # the tailoring's own concepts (imported late: auto imports jobs)

    vec, signal = job_concepts(clean_job_text(f"{job['title']}\n\n{job['text']}"), job["title"])
    if signal < 4:
        return 0
    fit = _relevance(vec, resume_text) / max(sum(vec.values()), 1e-6)  # about 0-4
    score = 20 * min(fit, 4) + (15 if EARLY.search(job["title"] + " " + job["text"][:3000]) else 0)
    if job.get("years"):
        score -= 5 * max(0, job["years"] - 1)
    return max(0, min(100, round(score)))


CONCEPT_NAMES = {"ux_research": "User research", "design": "Design", "product": "Product", "data": "Data & analytics",
                 "strategy": "Strategy", "ai": "AI", "engineering": "Technical building", "stakeholder": "Stakeholders",
                 "operations": "Operations", "healthcare": "Healthcare", "finance": "Finance", "education": "Education",
                 "customer": "Customers"}


_SMALL = {"of", "and", "for", "the", "at", "in", "on"}


def _nice(name: str) -> str:
    """"ACME-CLUB" -> "Acme-Club", "UNIVERSITY OF MICHIGAN" -> "University of Michigan"."""
    words = []
    for i, w in enumerate(name.split()):
        parts = [p.upper() if len(p) <= 3 and p.isalpha() and p.lower() not in _SMALL else
                 (p.lower() if p.lower() in _SMALL and i else p.capitalize()) for p in w.split("-")]
        words.append("-".join(parts))
    return " ".join(words)


def resume_entries(resume_text: str) -> list[tuple[str, str]]:
    """(short name, text) for each resume entry, to say where the evidence for a match is."""
    try:
        data = json.loads(resume_text)
    except (ValueError, TypeError):
        return [("your resume", resume_text or "")]
    out = []
    for sec in data.get("sections", []):
        if sec.get("heading", "").upper().startswith("EDUCATION"):
            continue
        for e in sec.get("entries", []):
            name = _nice(re.split(r" \(| & | \| |, ", e.get("title", ""))[0].strip())
            text = " ".join([e.get("title", "")] + [r.get("title", "") + " " + " ".join(r.get("bullets", []))
                                                     for r in e.get("roles", [])])
            out.append((name, text))
        for ln in sec.get("lines", []):
            out.append((f"{ln.get('label', '')} skills", ln.get("text", "")))
    return out


def explain(job: dict, entries: list[tuple[str, str]]) -> dict:
    """Why a role fits: the posting's main themes and the resume entries that show them; and the themes it stresses
    that the resume barely shows. Plain facts from the same concepts the tailoring uses, no model."""
    from ..resume.auto import job_concepts
    from ..resume.rewrite import _concepts  # the strict version: "built a dataset" is not technical work

    strict = _concepts()
    shows = lambda text, c: bool(strict[c].search(text))  # noqa: E731
    vec, _ = job_concepts(clean_job_text(f"{job['title']}\n\n{job['text']}"), job["title"])
    top = [c for c, w in sorted(vec.items(), key=lambda kv: -kv[1]) if w >= 2.0][:4]
    why, gaps = [], []
    for c in top:
        shown = [name for name, text in entries if shows(text, c)]
        if shown:
            why.append(f"{CONCEPT_NAMES.get(c, c)}: {', '.join(shown[:2])}")
        elif vec[c] >= 3.0:
            gaps.append(CONCEPT_NAMES.get(c, c))
    return {"why": why[:3], "gaps": gaps[:2]}


# ----------------------------------------------------------------------------------------------- the store and a run

class SearchStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "search"
        self.prefs_path = self.dir / "prefs.json"
        self.results_path = self.dir / "results.json"
        self.lock = threading.Lock()

    def prefs(self) -> Prefs:
        return Prefs.load(self.prefs_path)

    def load(self) -> dict:
        if not self.results_path.exists():
            return {"runs": [], "results": {}}
        return json.loads(self.results_path.read_text(encoding="utf-8"))

    def save(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.results_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.results_path)


def run_search(prefs: Prefs, store: SearchStore, resume_text: str, progress: Callable[[str, int, int], None] = None,
               client: httpx.Client | None = None) -> dict:
    """Read every feed, filter, rank, and merge into the store. Returns a summary of the run."""
    progress = progress or (lambda *a: None)
    client = client or httpx.Client(timeout=25, headers=UA, follow_redirects=True)
    flt = Filter(prefs)
    tasks: list[tuple[str, str]] = [(kind, name) for kind, names in prefs.companies.items() for name in names]
    started = datetime.now().isoformat(timespec="microseconds")  # also the run's id: two runs never share one
    seen: list[dict] = []
    errors: list[str] = []
    done = 0

    def read(task):
        kind, name = task
        try:
            if kind == "greenhouse":
                return greenhouse(client, name)
            if kind == "lever":
                return lever(client, name)
            if kind == "ashby":
                return ashby(client, name)
            if kind == "sites":
                return SITES[name](client)
            m = _WORKDAY.match(name)
            tenant = m.group(1) if m else ""
            return workday(client, name, lambda t: flt.title_ok(t, tenant))
        except (httpx.HTTPError, ValueError) as e:
            errors.append(f"{kind}:{name} ({type(e).__name__})")
            return []

    progress("reading job feeds", 0, len(tasks))
    with ThreadPoolExecutor(6) as pool:
        for batch in pool.map(read, tasks):
            seen.extend(batch)
            done += 1
            progress("reading job feeds", done, len(tasks))

    why: dict[str, int] = {}
    kept: list[dict] = []
    for job in seen:
        reason = flt.check(job)
        if reason == "not in your locations" and prefs.early_anywhere and early_hire(job):
            reason, job["outside_cities"] = None, True
        if not reason:
            enrich(job)
            job["early"] = early_hire(job)
            if job["years"] and job["years"] > prefs.max_years:
                reason = "asks for more years"
            elif prefs.skip_no_sponsorship and job["sponsorship"] == "restricted":
                reason = "does not sponsor"
            elif undergrad_only(job.get("text") or ""):
                reason = "for bachelor's students only"
        if reason:
            why[reason] = why.get(reason, 0) + 1
        else:
            kept.append(job)
    progress("ranking", 0, len(kept))
    entries = resume_entries(resume_text)
    for i, job in enumerate(kept):
        job["fit"] = fit_score(job, resume_text)
        job.update(explain(job, entries))
        if i % 20 == 0:
            progress("ranking", i, len(kept))

    with store.lock:
        data = store.load()
        results = data["results"]
        new = 0
        for job in kept:
            old = results.get(job["id"])
            rec = {k: v for k, v in job.items() if k != "text"}
            rec["text"] = job["text"][:40000]
            rec["last_seen"] = started
            if old:
                rec["first_seen"] = old.get("first_seen", started)
                rec["dismissed"] = old.get("dismissed", False)
                rec["desk_id"] = old.get("desk_id")
            else:
                rec["first_seen"], rec["dismissed"], rec["desk_id"] = started, False, None
                new += 1
            results[job["id"]] = rec
        # roles no longer listed (or no longer matching) drop out unless they were saved to the Desk; roles the
        # person (or Claude) found by hand stay until dismissed or saved
        for rid in [r for r, v in results.items() if v["last_seen"] != started and not v.get("desk_id")
                    and v.get("source") != "found"]:
            del results[rid]
        summary = {"at": started, "read": len(seen), "kept": len(kept), "new": new, "left_out": why,
                   "feeds": len(tasks), "errors": errors[:20]}
        data["runs"] = ([summary] + data.get("runs", []))[:30]
        store.save(data)
    progress("done", len(kept), len(kept))
    return summary


_VARIANT = re.compile(r"\s*(?:\((?:[^)]*speaking|[^)]*bilingual[^)]*|[^)]*portuguese|[^)]*spanish)\)|"
                      r"[-–,]\s*bilingual\b.*$)", re.I)


def base_title(title: str) -> str:
    """The role without its language variant: "Strategist (Spanish speaking)" is the same opening to decide on."""
    return re.sub(r"\s+", " ", _VARIANT.sub("", title)).strip().lower()


def import_found(store: "SearchStore", rows: list[dict]) -> dict:
    """Roles found by hand (career sites the feeds cannot read, a school's job board): kept with the search results
    until dismissed or saved. Each row: company, title, url, location, and optionally category and note."""
    at = datetime.now().isoformat(timespec="microseconds")
    added = 0
    with store.lock:
        data = store.load()
        for r in rows:
            url, title = str(r.get("url", "")).strip(), re.sub(r"\s+", " ", str(r.get("title", ""))).strip()
            if not url.startswith("https://") or not title:
                continue
            job = _job("found", str(r.get("company", "")).strip()[:120], title[:200], url, str(r.get("location", ""))[:120], "")
            if job["id"] in data["results"]:
                continue
            note = str(r.get("note", "")).strip()[:200]
            due = str(r.get("deadline", "")).strip()
            due = due if re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) else None
            job.update(fit=0, why=[note] if note else [], gaps=[], years=None, deadline=due, sponsorship=None,
                       clearance=False, category=str(r.get("category", ""))[:40], early=True, first_seen=at,
                       last_seen=at, dismissed=False, desk_id=None, checked=False)
            data["results"][job["id"]] = job
            added += 1
        store.save(data)
    return {"added": added}


def rank_found(store: "SearchStore", rid: str, text: str, resume_text: str) -> None:
    """A found role's posting text, once read: the same facts and fit as a searched role."""
    with store.lock:
        data = store.load()
        rec = data["results"].get(rid)
        if not rec:
            return
        rec["checked"] = True
        if len(text.strip()) >= 200:
            job = enrich(dict(rec, text=text))
            job["fit"] = fit_score(job, resume_text)
            note = rec.get("why", [])
            job.update(explain(job, resume_entries(resume_text)))
            job["why"] = note + [w for w in job.get("why", []) if w not in note]
            rec.update({k: job[k] for k in ("years", "deadline", "sponsorship", "clearance", "fit", "why", "gaps")})
            rec["text"] = text[:40000]
        store.save(data)


def grouped(results: list[dict]) -> list[dict]:
    """One row per role: the same title at the same company in several cities is one opening to decide on."""
    rows: dict[tuple, dict] = {}
    for r in sorted(results, key=lambda r: -r.get("fit", 0)):
        key = (r["company"], base_title(r["title"]))
        if key in rows:
            row = rows[key]
            row["also"].append({"id": r["id"], "location": r["location"], "url": r["url"]})
            row["dismissed"] = row["dismissed"] and r.get("dismissed", False)
            row["desk_id"] = row.get("desk_id") or r.get("desk_id")
            row["first_seen"] = min(row["first_seen"], r.get("first_seen", row["first_seen"]))
        else:
            rows[key] = dict({k: v for k, v in r.items() if k != "text"}, also=[])
    return list(rows.values())


def spread(rows: list[dict], per_company: int = 3) -> list[dict]:
    """Best first, but no company fills the top of the list: a company's 4th, 5th... role waits until every other
    company's best have been shown (Palantir posts eight "Deployment Strategist" variants)."""
    count: dict[str, int] = {}
    ranked = []
    for r in sorted(rows, key=lambda r: -r.get("fit", 0)):
        n = count.get(r["company"], 0)
        count[r["company"]] = n + 1
        ranked.append((n // per_company, -r.get("fit", 0), r))
    return [r for *_, r in sorted(ranked, key=lambda t: (t[0], t[1]))]


# Employers worth checking beyond the defaults (tech, fintech, health, consumer; many hire new grads and MBAs). Each
# name is tried on every feed; only boards that answer and have matching roles are suggested.
DISCOVER = """amplitude andurilindustries benchling betterment braze brightwheel chainalysis checkr circle clever coursera
cruise dataminr epicgames everlaw fanatics fivetran flatironhealth gemini ginkgobioworks headspace himshers hopper houzz
hudl intercom justworks kickstarter klaviyo mapbox marqeta medium mixpanel nerdwallet nextdoor niantic noom nuna
opendoor outschool pagerduty patreon postman quizlet remitly rubrik scopely seatgeek sofi strava stitchfix sweetgreen
tanium tripadvisor turo udemy upstart vimeo warbyparker wealthsimple whatnot xometry yext zocdoc zapier retool replit
elevenlabs mistral cohere posthog supabase modal sierra decagon glean writer watershed abridge eventbrite shieldai
rippling benchling snowflake hightouch census dbtlabs figure brex mercury deel vanta clay notion airtable chime""".split()


# Workday careers sites known to answer (checked 2026-10-14) with graduate or MBA hiring in consulting, product or strategy
WORKDAY_CANDIDATES = """guidehouse.wd1/External huron.wd1/huroncareers mmc.wd1/MMC bah.wd1/BAH_Jobs gartner.wd5/EXT
verizon.wd12/verizon-careers mastercard.wd1/CorporateCareers expedia.wd108/search paypal.wd1/jobs ebay.wd5/apply
tmobile.wd1/External autodesk.wd1/Ext workday.wd5/Workday disney.wd5/disneycareer zillow.wd5/Zillow_Group_External
intel.wd1/External hp.wd5/ExternalCareerSite target.wd5/targetcareers nike.wd1/nke cvshealth.wd1/CVS_Health_Careers
humana.wd5/Humana_External_Career_Site mckesson.wd3/External_Careers medtronic.wd1/MedtronicCareers
abbott.wd5/abbottcareers homedepot.wd5/CareerDepot amat.wd1/External micron.wd1/External lplfinancial.wd1/university
motorolasolutions.wd5/Careers adobe.wd5/external_university pfizer.wd1/PfizerCareers amgen.wd1/Careers
boeing.wd1/EXTERNAL_CAREERS 3m.wd1/Search""".split()


def discover_workday(prefs: Prefs, client: httpx.Client | None = None, candidates: list[str] | None = None) -> list[dict]:
    """Workday careers sites not searched yet whose graduate searches ("MBA", "2027", "new grad") list roles that pass
    the preferences' title rules (titles only: no detail reads)."""
    client = client or httpx.Client(timeout=20, headers=UA, follow_redirects=True)
    flt = Filter(prefs)
    have = set(prefs.companies.get("workday", []))

    def probe(c: str):
        host, site = c.split("/")
        tenant = host.split(".")[0]
        url = f"https://{host}.myworkdayjobs.com/{site}"
        if url in have:
            return None
        titles: set[str] = set()
        for term in ("MBA", "2027", "new grad", "university graduate"):
            try:
                r = client.post(f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                                json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term})
            except httpx.HTTPError:
                return None
            if r.status_code != 200:
                return None
            titles |= {p.get("title", "") for p in r.json().get("jobPostings", []) if flt.title_ok(p.get("title", ""), tenant)}
        if not titles:
            return None
        return {"kind": "workday", "name": url, "company": company_name({"company": tenant}), "openings": None,
                "matching": len(titles), "sample": sorted(titles)[:3]}

    with ThreadPoolExecutor(8) as pool:
        found = [r for r in pool.map(probe, candidates or WORKDAY_CANDIDATES) if r]
    return sorted(found, key=lambda r: -r["matching"])


def discover(prefs: Prefs, client: httpx.Client | None = None, candidates: list[str] | None = None) -> list[dict]:
    """Boards (on any feed) not searched yet that have roles matching the preferences right now, most roles first."""
    client = client or httpx.Client(timeout=15, headers=UA, follow_redirects=True)
    have = {(k, n) for k, names in prefs.companies.items() for n in names}
    flt = Filter(prefs)
    tries = [(k, n) for n in dict.fromkeys(candidates or DISCOVER) for k in ("greenhouse", "lever", "ashby")
             if (k, n) not in have]

    def probe(t):
        kind, name = t
        try:
            jobs = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}[kind](client, name)
        except (httpx.HTTPError, ValueError):
            return None
        fits = [j for j in jobs if flt.check(j) is None]
        if not jobs:
            return None
        return {"kind": kind, "name": name, "company": company_name({"company": name}), "openings": len(jobs),
                "matching": len(fits), "sample": [j["title"] for j in fits[:3]]}

    with ThreadPoolExecutor(12) as pool:
        found = [r for r in pool.map(probe, tries) if r and r["matching"]]
    best: dict[str, dict] = {}
    for r in found:  # a name on two feeds (an old board left behind): keep the one with more roles
        if r["name"] not in best or r["matching"] > best[r["name"]]["matching"]:
            best[r["name"]] = r
    return sorted(best.values(), key=lambda r: -r["matching"])


def company_name(rec: dict) -> str:
    """A readable employer name for a result."""
    if rec.get("source") in ("newgrad", "found"):  # the list names the employer in full already
        return rec.get("company", "")
    if rec.get("company") in SITE_NAMES:
        return SITE_NAMES[rec["company"]]
    special = {"thebrattlegroup": "The Brattle Group", "keystone": "Keystone Strategy", "charlesriverassociates":
               "Charles River Associates", "chartis": "Chartis", "accordion": "Accordion", "capco": "Capco",
               "elixirr": "Elixirr", "alixpartners": "AlixPartners", "pariveda": "Pariveda", "precisionaq": "Precision AQ", "avalerehealth": "Avalere Health",
               "secretariatadvisorsllc": "Secretariat",
               "doordashusa": "DoorDash", "scaleai": "Scale AI", "capitalone": "Capital One", "pwc": "PwC",
               "nvidia": "NVIDIA", "openai": "OpenAI", "gitlab": "GitLab", "mongodb": "MongoDB",
               "andurilindustries": "Anduril", "elevenlabs": "ElevenLabs", "shieldai": "Shield AI", "sofi": "SoFi",
               "himshers": "Hims & Hers", "flatironhealth": "Flatiron Health", "ginkgobioworks": "Ginkgo Bioworks",
               "dbtlabs": "dbt Labs", "warbyparker": "Warby Parker", "nerdwallet": "NerdWallet", "posthog": "PostHog",
               "epicgames": "Epic Games", "seatgeek": "SeatGeek", "hubspot": "HubSpot", "doordash": "DoorDash",
               "stitchfix": "Stitch Fix", "tripadvisor": "Tripadvisor", "pagerduty": "PagerDuty", "linkedin": "LinkedIn",
               "mmc": "Marsh McLennan (Oliver Wyman, Mercer)", "clearviewhealthcarepartners": "ClearView Healthcare Partners",
               "bah": "Booz Allen Hamilton", "mckesson": "McKesson", "hp": "HP", "tmobile": "T-Mobile",
               "cvshealth": "CVS Health", "homedepot": "The Home Depot", "lplfinancial": "LPL Financial",
               "motorolasolutions": "Motorola Solutions", "amat": "Applied Materials", "guidehouse": "Guidehouse",
               "abnormalsecurity": "Abnormal Security", "thenewyorktimes": "The New York Times", "ebay": "eBay",
               "paypal": "PayPal", "ideo": "IDEO", "rga": "R/GA", "zscaler": "Zscaler", "thoughtworks": "Thoughtworks"}
    c = rec.get("company", "")
    return special.get(c, c.replace("-", " ").replace("_", " ").title())


def job_key(url: str) -> str:
    """The same opening under different links: a Greenhouse job number (gh_jid=, /jobs/N), a Lever or Ashby posting id,
    else the link without its query string."""
    if not url:
        return ""
    if ((m := re.search(r"gh_jid=(\d+)", url)) or (m := re.search(r"greenhouse\.io/[^/]+/jobs/(\d+)", url))
            or (m := re.search(r"greenhouse\.io/embed/job_app(?:/confirmation)?\?.*\btoken=(\d+)", url))):
        return "gh:" + m[1]
    if (m := re.search(r"myworkdayjobs\.com/.*?/job/[^/]+/([^/?#]+)", url)):
        return "wd:" + m[1].lower()  # ".../job/San-Francisco/Product-Manager_R170556-1[/apply/applyManually]"
    if (m := re.search(r"(?:lever\.co|ashbyhq\.com)/[^/]+/([0-9a-f-]{36})", url)):
        return "id:" + m[1]
    if (m := re.search(r"([\w-]+)\.csod\.com/.*?/requisition/(\d+)", url)):  # Cornerstone: posting and its application
        return f"csod:{m[1].lower()}:{m[2]}"
    u = urlparse(url)
    path = re.sub(r"/(?:apply|application|applyManually)(?:/.*)?$", "", u.path.rstrip("/"))  # the form under a posting
    if u.fragment.startswith("/"):
        # a page that routes after "#" (12twenty school boards: ".../jobPostings#/jobPostings/3500.../application"):
        # the posting is in the fragment; without it every posting there looked like the same job (2026-10-13)
        path += re.sub(r"/(?:apply|application)(?:/.*)?$", "", u.fragment.split("?")[0].rstrip("/"))
    return f"{(u.hostname or '').removeprefix('www.')}{path}".lower()


def posting_url(url: str) -> str:
    """The job description page for an application form's link ("" when it cannot be told or is the same page): the
    description is on the posting page, and a multi-step form never shows it again (Simon-Kucher, 2026-10-10)."""
    u = urlparse(url or "")
    host = u.hostname or ""
    if (m := re.search(r"/ux/ats/careersite/(\d+)/(?:home/)?requisition/(\d+)", u.path)) and host.endswith(".csod.com"):
        c = re.search(r"(?:^|&)c=([\w-]+)", u.query)
        return f"https://{host}/ux/ats/careersite/{m[1]}/home/requisition/{m[2]}" + (f"?c={c[1]}" if c else "")
    path = re.sub(r"/(?:apply|application|applyManually)(?:/.*)?$", "", u.path.rstrip("/"))
    if path != u.path.rstrip("/") and host:
        return f"{u.scheme or 'https'}://{host}{path}"
    return ""


def link_desk(results: dict, postings: list[dict]) -> int:
    """Mark results whose opening is already on the Desk (saved from the extension or by link, not from Find roles),
    so they are not shown or prepared again. Returns how many were newly linked."""
    on_desk = {job_key(p.get("url") or ""): p["id"] for p in postings if p.get("url")}
    on_desk.pop("", None)
    n = 0
    for rec in results.values():
        if not rec.get("desk_id") and (pid := on_desk.get(job_key(rec.get("url", "")))):
            rec["desk_id"] = pid
            n += 1
    return n


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")

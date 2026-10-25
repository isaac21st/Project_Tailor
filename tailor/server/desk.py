"""Tailor Desk: a queue of job postings (PDFs, links, or pages saved from the extension), each read for its facts and
tailored with the same truth-checked path the extension uses.

Where things live: <data_dir>/desk/postings.json (the queue, with each posting's text) and output/tailored/<id>.pdf (the
resumes). The page itself is desk.html, served at /desk on 127.0.0.1 only.

A link is opened by the local headless browser, because most career sites build the posting with JavaScript. Only
http(s) links to public hosts are opened. That is the one time the Desk reaches outside this computer, and only for a
link the person pasted.
"""
from __future__ import annotations

import io
import ipaddress
import json
import re
import secrets
import socket
import tempfile
import threading
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from ..config import Settings, apply_environment
from ..jobs import clean_job_text, extract_facts, load_job_text

PAGE = Path(__file__).with_name("desk.html")
STAGES = ("saved", "applying", "applied", "skipped")
MAX_TEXT = 40000
ALLOW_PRIVATE_HOSTS = False  # tests serve postings from 127.0.0.1

# Same idea as the extension's postingOnlyText(): start at the qualifications heading and go out until the job title is
# inside, then only while the text barely grows, so a list of other jobs beside the posting is left out.
_POSTING_JS = r"""
() => {
  // Workday marks the posting itself: use it, not the cookie banner and menus around it
  const wdTitle = document.querySelector("[data-automation-id='jobPostingHeader']");
  const wdBody = document.querySelector("[data-automation-id='jobPostingDescription']");
  if (wdBody && wdBody.innerText.length > 300) {
    const info = document.querySelector("[data-automation-id='job-posting-details']");
    return { text: [wdTitle ? wdTitle.innerText : "", info ? info.innerText : "", wdBody.innerText].join("\n\n"),
             title: wdTitle ? wdTitle.innerText.trim() : document.title, site: "", pageTitle: document.title };
  }
  const words = /qualifications|requirements|what we require|responsibilit|what you(?:'|’)ll do|what you will do|about the (?:job|role)|the role\b|who you are|what we(?:'re| are) looking for|what we value|your impact|duties/i;  // same list as the extension's POSTING_WORDS
  const head = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6,strong,b")]
    .find((e) => e.textContent.length < 80 && words.test(e.textContent));
  let node = head, title = "";
  if (head) {
    while (node.parentElement && node.parentElement !== document.body) {
      const up = node.parentElement;
      if (node.querySelector("h1,h2") && up.innerText.length > node.innerText.length * 1.1) break;
      node = up;
    }
    const t = node.querySelector("h1,h2");
    title = t ? t.innerText.trim() : "";
  }
  const text = head && node.innerText.length > 800 ? node.innerText : document.body.innerText;
  const site = document.querySelector("meta[property='og:site_name']");
  const h1 = document.querySelector("h1");
  return { text, title: title || (h1 ? h1.innerText.trim() : "") || document.title,
           site: site ? site.content : "", pageTitle: document.title };
}
"""


class AddRequest(BaseModel):
    url: str = ""
    title: str = ""
    text: str = Field(default="", max_length=MAX_TEXT * 2)
    source: str = "link"


class AppliedRequest(BaseModel):
    url: str = Field(default="", max_length=2000)
    title: str = Field(default="", max_length=300)
    page_text: str = Field(default="", max_length=20000)
    host: str = Field(default="", max_length=200)
    answers: list[dict] = Field(default_factory=list, max_length=30)  # what the person sent in free-text boxes
    resume_id: str = Field(default="", pattern=r"^(?:[0-9a-f]{12})?$")  # the tailored resume the form got ("" = default)


class BankDecision(BaseModel):
    original: str = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1, max_length=1000)
    status: str = Field(pattern=r"^(?:approved|rejected|proposed)$")


class AiWordingDecision(BaseModel):
    original: str = Field(min_length=1, max_length=1000)
    term: str = Field(min_length=1, max_length=80)
    status: str = Field(pattern=r"^(?:auto|rejected)$")


class AiWordingSettings(BaseModel):
    enabled: bool


class PatchRequest(BaseModel):
    stage: str | None = None
    title: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


class TailorAsk(BaseModel):
    instruction: str = Field(default="", max_length=300)


# ----------------------------------------------------------------------------------------------- reading postings

def _public_http_url(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise HTTPException(status_code=422, detail="only http(s) links can be read")
    if not ALLOW_PRIVATE_HOSTS:
        try:
            addrs = {ai[4][0] for ai in socket.getaddrinfo(u.hostname, None)}
        except OSError:
            raise HTTPException(status_code=422, detail=f"cannot find {u.hostname}")
        for a in addrs:
            ip = ipaddress.ip_address(a.split("%")[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise HTTPException(status_code=422, detail="that link points inside this computer or network")
    return url.strip()


def fetch_posting(url: str, settings: Settings) -> dict:
    """Open the link in the local headless browser and keep only the posting."""
    from playwright.sync_api import sync_playwright

    apply_environment(settings)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            got: dict = {}
            for _ in range(12):  # career sites build the posting after load: wait for its heading, up to ~12 s
                page.wait_for_timeout(1000)
                got = page.evaluate(_POSTING_JS)
                if len(got.get("text", "")) > 1500 and re.search(r"qualifications|requirements|require|responsibilit",
                                                                 got["text"], re.I):
                    break
            got["final_url"] = page.url
            return got
        finally:
            browser.close()


EXTENSION_DIR = Path(__file__).resolve().parent.parent.parent / "extension"


def greenhouse_form(url: str, settings) -> str | None:
    """Stripe, Roblox, Databricks... show Greenhouse postings on their own sites ("stripe.com/jobs/...?gh_jid=N"),
    where the form sits behind their Apply button. Greenhouse serves the same form directly given the company's
    board name, which the role search knows."""
    from ..jobs.search import SearchStore, job_key

    host = (urlparse(url).hostname or "")
    m = re.search(r"gh_jid=(\d+)", url)
    if not m or host.endswith("greenhouse.io"):
        return None
    key = job_key(url)
    try:
        results = list(SearchStore(settings.data_dir).load()["results"].values())
    except Exception:
        results = []
    boards = [r["company"] for r in results if r.get("source") == "greenhouse" and job_key(r.get("url", "")) == key]
    # not in the search results: the site's own name is usually the board ("careers.roblox.com" -> "roblox")
    parts = [x for x in host.split(".") if x not in ("www", "careers", "jobs", "com", "io", "co", "org", "net")]
    boards += parts[-1:]
    for board in dict.fromkeys(boards):
        form = f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={m[1]}"
        try:  # only a form that is really there: a wrong board name sends you to some board's job list
            r = httpx.get(form, timeout=8, follow_redirects=True)
            if r.status_code == 200 and "/embed/job_app" in str(r.url) and "Job Application for" in r.text:
                return form
        except httpx.HTTPError:
            continue
    return None


def greenhouse_meta(url: str) -> dict:
    """The real job title and employer for a Greenhouse link, from Greenhouse's public API: the printed page starts
    with a team blurb ("About the Team") and the URL only has the board's slug ("doordashusa")."""
    u = urlparse(url or "")
    m = re.match(r"/([\w-]+)/jobs/(\d+)", u.path)
    if not ((u.hostname or "").endswith("greenhouse.io") and m):
        return {}
    board, job = m.groups()
    out = {}
    try:
        r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}", timeout=8)
        if r.status_code == 200:
            out["title"] = (r.json().get("title") or "").strip()
        r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{board}", timeout=8)
        if r.status_code == 200:  # "DoorDash USA" -> "DoorDash"
            out["company"] = re.sub(r",? (?:USA|US|Inc\.?|LLC)$", "", (r.json().get("name") or "").strip())
    except (httpx.HTTPError, ValueError):
        pass
    return {k: v for k, v in out.items() if v}


def spelled_as_posted(name: str, text: str) -> str:
    """The employer's own spelling when the posting uses it ("Openai" -> "OpenAI", "Scaleai" stays a guess)."""
    if not name or not text:
        return name
    m = re.search(r"\b(" + re.escape(name) + r")\b", text, re.I)
    got = m.group(1) if m else ""
    mixed = got and got != got.lower() and (got != got.upper() or len(got) <= 4)  # "OpenAI", "PwC"; never "CHARTIS"
    return got if mixed else name


def company_from(url: str, site: str = "", text: str = "") -> str:
    """A best guess at the employer, for the table only (the person can edit it)."""
    return spelled_as_posted(_company_guess(url, site, text), text)


def _board_name(slug: str) -> str:
    from ..jobs.search import company_name  # one list of proper names (OpenAI, DoorDash, Scale AI...)

    return company_name({"company": slug}) if slug.lower() != "embed" else ""


def _company_guess(url: str, site: str = "", text: str = "") -> str:
    if site and site.lower() not in {"linkedin", "indeed", "greenhouse", "lever", "workday", "glassdoor"}:
        return site.strip()[:80]
    u = urlparse(url or "")
    host, parts = (u.hostname or "").lower(), [p for p in u.path.split("/") if p]
    if host.endswith("greenhouse.io") and parts:
        return _board_name(parts[0])
    if host == "jobs.lever.co" and parts:
        return _board_name(parts[0])
    if host.endswith("myworkdayjobs.com"):
        from ..jobs.search import company_name  # one list of proper names (NVIDIA, PwC, Capital One...)

        return company_name({"company": host.split(".")[0]})
    if host.endswith("ashbyhq.com") and parts:
        return _board_name(parts[0])
    if host.endswith("smartrecruiters.com") and parts:  # jobs.smartrecruiters.com/RolandBerger/744...
        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", parts[0]).replace("-", " ")
    if host.endswith(".csod.com"):  # simon-kucher.csod.com
        return host.split(".")[0].replace("-", " ").title()
    if "google.com" in host and "/careers" in u.path:
        return "Google"
    if host:
        name = host.removeprefix("www.").removeprefix("careers.").removeprefix("jobs.").split(".")[0]
        name = re.sub(r"^lifeat(?=\w{3})", "", name)  # lifeattiktok.com
        if name == "tiktok":
            return "TikTok"
        return name.replace("-", " ").title()
    m = re.search(r"\babout ([A-Z][\w&.\- ]{1,40})\b", text or "")
    return m.group(1).strip() if m else ""


# Printed career pages start with the site's menu, icon-font characters and buttons before the job title.
_ICONS = re.compile(r"[\ue000-\uf8ff]")
_NAV = re.compile(r"\b(?:careers|job search|saved jobs|apply(?: now| for this job)?|share|back to|search results|sign in|"
                  r"save|see all|locations?|job description|caution|recommended|menu)\b", re.I)
_ROLE = re.compile(r"\b(?:manager|consultant|associate|analyst|engineer|designer|researcher|scientist|intern(?:ship)?|lead|"
                   r"director|specialist|strategist|owner|architect|developer|officer|coordinator|product|program)\b", re.I)


def _title_from_text(text: str) -> str:
    lines = [re.sub(r"\s+", " ", _ICONS.sub(" ", ln)).strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln][:40]
    for i, line in enumerate(lines):
        words = line.split()
        letters = sum(c.isalpha() for c in line) / max(len(line), 1)
        if not (2 <= len(words) <= 12 and 6 <= len(line) <= 120 and letters > 0.6):
            continue
        if len(_NAV.findall(line)) >= 1 and not _ROLE.search(line) or re.search(r"https?://|^page \d|\|", line, re.I):
            continue
        # a title split over two lines ("Strategy& Deals Strategy Senior" / "Associate - Summer/Fall 2027")
        if not _ROLE.search(line) and i + 1 < len(lines) and _ROLE.search(lines[i + 1]) and len(lines[i + 1]) < 60:
            return f"{line} {lines[i + 1]}"[:200]
        return line
    return "Untitled posting"


def company_from_file(name: str, text: str) -> str:
    """People name posting files after the employer ("accenture_digital_assets_consultant.pdf"): use that first word
    when the posting says it too, spelled the way the posting spells it (PwC, not Pwc)."""
    first = re.split(r"[_\-\s.]+", Path(name).stem)[0]
    if len(first) < 2:
        return ""
    m = re.search(r"\b(" + re.escape(first) + r")\b", text or "", re.I)
    return m.group(1) if m else ""


def facts_of(text: str) -> dict:
    """What matters before applying: deadline, years asked for, sponsorship, pay, on-site days."""
    f = extract_facts(clean_job_text(text))
    years = sorted({y.years for y in f.min_years if 1 <= y.years <= 15})
    return {
        "deadline": f.deadline.isoformat() if f.deadline else None,
        "min_years": years[-1] if years else None,
        "sponsorship": f.sponsorship,
        "sponsorship_quote": f.sponsorship_quote,
        "salary_min": f.salary_min, "salary_max": f.salary_max,
        "onsite_days_min": f.onsite_days_min,
        "mba_required": f.mba_required,
        "travel_max_pct": f.travel_max_pct,
    }


def flags_of(p: dict, today: date | None = None) -> list[dict]:
    """Short warnings for the table: {level: red|amber|blue, text}."""
    today = today or date.today()
    f, out = p.get("facts") or {}, []
    if f.get("deadline"):
        days = (date.fromisoformat(f["deadline"]) - today).days
        if days < 0:
            out.append({"level": "red", "text": "deadline passed"})
        elif days <= 7:
            out.append({"level": "amber", "text": f"due in {days} day{'s' if days != 1 else ''}"})
    if f.get("min_years") and f["min_years"] >= 3:
        out.append({"level": "amber", "text": f"{f['min_years']}+ years asked"})
    if f.get("sponsorship") == "restricted":
        out.append({"level": "red", "text": "no sponsorship"})
    elif f.get("sponsorship") == "limited":
        out.append({"level": "blue", "text": "limited sponsorship"})
    if f.get("mba_required"):
        out.append({"level": "blue", "text": "MBA required"})
    if f.get("onsite_days_min"):
        out.append({"level": "blue", "text": f"{f['onsite_days_min']} days on-site"})
    if len(p.get("text", "")) < 800:
        out.append({"level": "amber", "text": "little posting text found"})
    return out


# ----------------------------------------------------------------------------------------------- storage

class DeskStore:
    def __init__(self, settings: Settings):
        self.path = settings.data_dir / "desk" / "postings.json"
        self.lock = threading.Lock()

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def find(self, items: list[dict], pid: str) -> dict:
        for it in items:
            if it["id"] == pid:
                return it
        raise HTTPException(status_code=404, detail="no such posting")


def _summary(p: dict) -> dict:
    """A posting for the table: everything except its full text."""
    out = {k: v for k, v in p.items() if k != "text"}
    out["chars"] = len(p.get("text", ""))
    out["flags"] = flags_of(p)
    return out


def _new(text: str, *, url: str = "", title: str = "", company: str = "", source: str, file: str = "") -> dict:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())[:MAX_TEXT]
    return {
        "id": secrets.token_hex(5), "added": datetime.now().isoformat(timespec="seconds"), "source": source,
        "url": url, "file": file, "title": (title or _title_from_text(text)).strip()[:200],
        "company": (company or company_from(url, text=text)).strip()[:120], "text": text, "facts": facts_of(text),
        "stage": "saved", "status": "new", "result": None, "instruction": "", "notes": "",
    }


_STOP = set("the and for with our you your a an of to in at on job jobs careers apply application".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z&+]{2,}", (text or "").lower()) if w not in _STOP}


def match_applied(items: list[dict], url: str, title: str, page_text: str) -> dict | None:
    """Which Desk posting a confirmation page belongs to. The same posting link (or the same job number on the same
    site) decides; otherwise the posting's title must appear on the page, on the same site. No guess beyond that."""
    u = urlparse(url)
    host, path = (u.hostname or "").removeprefix("www."), u.path
    ids = set(re.findall(r"\d{5,}", url))
    page = (title + "\n" + page_text[:6000]).lower()
    best, best_score = None, 0.0
    from ..jobs.search import job_key

    key = job_key(url)
    if re.match(r"(?:gh|wd|id):", key):  # the same Greenhouse job number (or Workday / Lever / Ashby id) on any site:
        for p in items:                   # careers.roblox.com/jobs/8143976 and greenhouse.io/embed/...token=8143976
            if job_key(p.get("url") or "") == key:
                return p
    for p in items:
        pu = urlparse(p.get("url") or "")
        phost = (pu.hostname or "").removeprefix("www.")
        if not phost:
            continue
        same_site = phost == host or phost.split(".")[-2:] == host.split(".")[-2:]
        if not same_site:
            continue
        if ids & set(re.findall(r"\d{5,}", p["url"])) or (pu.path and pu.path.rstrip("/") and path.startswith(pu.path.rstrip("/"))):
            return p
        tw = _words(p.get("title", ""))
        if tw:
            score = len(tw & _words(page)) / len(tw)
            if score > best_score:
                best, best_score = p, score
    return best if best_score >= 0.8 else None


# ---- the application tracker: an Excel sheet rewritten from the Desk whenever something is marked applied ----
TRACKER_NAME = "Application_Tracker.xlsx"


def _quals_and_description(text: str) -> tuple[str, str]:
    from ..resume.rewrite import qualifications

    job = clean_job_text(text or "")
    quals = "\n".join(f"• {q}" for q in qualifications(job, limit=14))
    duties = "\n".join(job.of_kind("responsibilities")) or job.body_text()
    return quals[:4000], re.sub(r"\n{3,}", "\n\n", duties).strip()[:4000]


def _resume_cell(p: dict, res: dict) -> str:
    """Which resume the application got: the one the extension attached (with its ATS keyword match), else the Desk's."""
    if "sent_resume" in p:
        if not p["sent_resume"]:
            return "default resume"
        ats = f" (ATS {p['sent_ats']}%)" if p.get("sent_ats") else ""
        return f"tailored {p['sent_resume']}.pdf{ats}"
    return f"tailored {res['id']}.pdf" if res.get("id") else "default"


def write_tracker(settings: Settings, items: list[dict]) -> Path:
    """Application_Tracker.xlsx: one row per posting marked applied, newest first."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    rows = sorted((p for p in items if p.get("stage") == "applied"),
                  key=lambda p: p.get("applied_at") or p.get("added", ""), reverse=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Applications"
    head = ["Date applied", "Company", "Job title", "Link", "Qualifications", "Description", "Deadline", "Resume"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="4F46E5")
    for p in rows:
        quals, desc = _quals_and_description(p.get("text", ""))
        res = p.get("result") or {}
        ws.append([(p.get("applied_at") or "")[:10], p.get("company", ""), p.get("title", ""), p.get("url", ""), quals, desc,
                   (p.get("facts") or {}).get("deadline") or "", _resume_cell(p, res)])
        if p.get("url"):
            ws.cell(row=ws.max_row, column=4).hyperlink = p["url"]
            ws.cell(row=ws.max_row, column=4).font = Font(color="1D4ED8", underline="single")
    for col, width in zip("ABCDEFGH", (13, 18, 38, 40, 70, 70, 12, 22)):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    path = settings.data_dir / TRACKER_NAME
    tmp = path.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    tmp.replace(path)
    return path


def add_posting_record(store: "DeskStore", text: str, *, url: str = "", title: str = "", company: str = "",
                       source: str = "link") -> dict:
    """Put one posting on the Desk (or return the one already there for this link)."""
    with store.lock:
        items = store.load()
        if url:
            for old in items:
                if old.get("url") == url:
                    return dict(_summary(old), duplicate=True)
        p = _new(text, url=url, title=title, company=company, source=source)
        items.insert(0, p)
        store.save(items)
    print(f"desk: added {source} posting {p['id']} ({len(p['text'])} chars)", flush=True)
    return _summary(p)


def tailor_desk_posting(store: "DeskStore", run_tailor, pid: str, instruction: str = "") -> dict:
    """Tailor the resume to one Desk posting (the same truth-checked path the extension uses) and keep the result."""
    p = store.find(store.load(), pid)
    res = run_tailor(SimpleNamespace(url=p.get("url") or f"desk:{p['file'] or pid}", title=p["title"],
                                     page_text=p["text"], instruction=instruction))
    with store.lock:
        items = store.load()
        p = store.find(items, pid)
        p["result"] = res.model_dump()
        p["instruction"] = instruction
        p["status"] = "tailored" if res.applied else "not tailored"
        store.save(items)
    return _summary(p)


# ----------------------------------------------------------------------------------------------- routes

MY_INFO = ("address", "city", "state", "postal_code", "country", "phone_type", "how_heard", "available_start", "preferred_name",
           "github_url", "gpa", "high_school_year", "languages", "interviewed_at", "relatives_at", "government_official",
           "conflict_of_interest")


class MyInfo(BaseModel):
    address: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=100)
    postal_code: str = Field(default="", max_length=20)
    country: str = Field(default="", max_length=100)
    phone_type: str = Field(default="", max_length=40)
    how_heard: str = Field(default="", max_length=100)
    available_start: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")  # earliest start, from a date box
    preferred_name: str = Field(default="", max_length=60)
    github_url: str = Field(default="", max_length=200)
    gpa: str = Field(default="", pattern=r"^(?:[0-4](?:\.\d{1,2})?(?:/4(?:\.0)?)?)?$")
    high_school_year: str = Field(default="", pattern=r"^(?:(?:19|20)\d{2})?$")
    languages: str = Field(default="", max_length=200)
    interviewed_at: str = Field(default="", max_length=400)
    relatives_at: str = Field(default="", max_length=400)
    government_official: str = Field(default="", pattern=r"^(?:Yes|No)?$")
    conflict_of_interest: str = Field(default="", pattern=r"^(?:Yes|No)?$")


def register_desk(app: FastAPI, settings: Settings, require_token, run_tailor, browser_lock: threading.Lock,
                  token: str, profile=None, ai_fill=None, ai_state: dict | None = None) -> None:
    store = DeskStore(settings)
    auth = [Depends(require_token)]

    @app.get("/desk", response_class=HTMLResponse)
    def desk_page() -> HTMLResponse:
        # The page carries the token so it can call the API from the same origin. Other sites cannot read it: the
        # server answers only 127.0.0.1 (Host header checked) and sends no CORS headers to web pages.
        html = PAGE.read_text(encoding="utf-8").replace("__TAILOR_TOKEN__", token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                           "Referrer-Policy": "no-referrer"})

    # ---- the wordings bank: other wordings of the same facts, used only once the person approves them ----
    bank_path = settings.profile_dir / "phrase_bank.json"

    @app.get("/api/bank", dependencies=auth)
    def get_bank() -> dict:
        bank = json.loads(bank_path.read_text(encoding="utf-8")) if bank_path.exists() else {"bullets": []}
        over_path = settings.profile_dir / "profile_overrides.json"
        over = json.loads(over_path.read_text(encoding="utf-8")) if over_path.exists() else {}
        return {"bullets": [{"entry": b.get("entry", ""), "role": b.get("role", ""), "original": b["original"],
                             "variants": [{"angle": v.get("angle", ""), "text": v["text"], "status": v.get("status", "")}
                                          for v in b.get("variants", [])]} for b in bank.get("bullets", [])],
                "confirmed_skills": over.get("confirmed_skills", [])}

    @app.post("/api/bank/decide", dependencies=auth)
    def decide_wording(req: BankDecision) -> dict:
        """Approve or reject one wording. An approved one must still pass the fact check against its original."""
        from ..resume.rewrite import check_bullet
        from ..resume.rules import load_rules

        bank = json.loads(bank_path.read_text(encoding="utf-8"))
        for b in bank.get("bullets", []):
            if b["original"] != req.original:
                continue
            for v in b.get("variants", []):
                if v["text"] == req.text:
                    if req.status == "approved" and (why := check_bullet(b["original"], v["text"], load_rules(settings.profile_dir).protect)):
                        raise HTTPException(status_code=422, detail=f"this wording fails the fact check: {why}")
                    v["status"] = req.status
                    v["decided"] = datetime.now().isoformat(timespec="seconds")
                    tmp = bank_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(bank, indent=2, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(bank_path)
                    print(f"bank: {req.status} a wording for {b.get('entry', '')[:40]}", flush=True)
                    return {"ok": True, "status": req.status}
        raise HTTPException(status_code=404, detail="no such wording")

    # ---- AI keyword wordings (resume/wordings.py): used automatically when a posting asks for their keyword ----
    @app.get("/api/ai-wordings", dependencies=auth)
    def get_ai_wordings() -> dict:
        from ..resume import wordings
        from ..resume.auto import load_bank
        from ..resume.rules import load_rules

        protect = load_rules(settings.profile_dir).protect
        items = []
        for it in wordings.load_store(settings):
            if not it.get("text"):
                continue
            term = wordings._term(it["term"])
            why = wordings.check_weave(it["source"], it["text"], term, protect)[1] if term else "unknown keyword"
            if not why and not wordings._still_evidenced(term, it["source"]):
                why = "this keyword would overstate what the bullet shows"
            items.append({"original": it["original"], "term": it["term"], "text": it["text"], "source": it["source"],
                          "status": it.get("status", ""), "problem": why or ""})
        base = None
        try:
            from ..resume.schema import Resume

            base = Resume.model_validate_json((settings.profile_dir / "base_resume.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        todo = len(wordings.missing(settings, base, load_bank(settings))) if base else 0
        failed = sum(1 for it in wordings.load_store(settings) if it.get("status") == "failed")
        return {"enabled": wordings.enabled(settings), "items": items, "missing": todo, "failed": failed,
                "running": bool((ai_state or {}).get("running")), "available": ai_fill is not None}

    @app.post("/api/ai-wordings/decide", dependencies=auth)
    def decide_ai_wording(req: AiWordingDecision) -> dict:
        from ..resume import wordings

        if not wordings.set_status(settings, req.original, req.term, req.status):
            raise HTTPException(status_code=404, detail="no such wording")
        print(f"ai wordings: {req.status} the '{req.term}' wording", flush=True)
        return {"ok": True}

    @app.post("/api/ai-wordings/settings", dependencies=auth)
    def ai_wordings_settings(req: AiWordingSettings) -> dict:
        path = settings.profile_dir / "profile_overrides.json"
        over = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        over["ai_wordings"] = req.enabled
        path.write_text(json.dumps(over, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"ai wordings: {'on' if req.enabled else 'off'}", flush=True)
        return {"enabled": req.enabled}

    @app.post("/api/ai-wordings/refresh", dependencies=auth)
    def refresh_ai_wordings() -> dict:
        """Write the missing wordings now, in the background (about half a minute each with the local model)."""
        if ai_fill is None:
            raise HTTPException(status_code=503, detail="no writing model is running")
        return {"started": bool(ai_fill())}

    @app.get("/api/desk/postings", dependencies=auth)
    def list_postings() -> list[dict]:
        return [_summary(p) for p in store.load()]

    @app.get("/api/desk/postings/{pid}", dependencies=auth)
    def get_posting(pid: str) -> dict:
        p = store.find(store.load(), pid)
        return dict(_summary(p), text=p["text"])

    @app.post("/api/desk/postings", dependencies=auth)
    def add_posting(req: AddRequest) -> dict:
        url, text, title, site = req.url.strip(), req.text, req.title, ""
        if url:
            with store.lock:
                for p in store.load():
                    if p.get("url") == url:
                        return dict(_summary(p), duplicate=True)
        if not text:
            if not url:
                raise HTTPException(status_code=422, detail="give a link or the posting text")
            url = _public_http_url(url)
            with browser_lock:
                try:
                    got = fetch_posting(url, settings)
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(status_code=502, detail=f"could not open the link ({type(e).__name__})")
            text, title, site = got.get("text", ""), title or got.get("title", ""), got.get("site", "")
        if len(text.strip()) < 200:
            raise HTTPException(status_code=422, detail="no posting text found there")
        source = req.source if req.source in {"link", "extension", "text"} else "link"
        gh = greenhouse_meta(url)
        return add_posting_record(store, text, url=url, title=req.title or gh.get("title") or title,
                                  company=gh.get("company") or company_from(url, site, text), source=source)

    @app.post("/api/desk/postings/pdf", dependencies=auth)
    def add_pdfs(files: list[UploadFile] = File(...)) -> list[dict]:
        added = []
        for f in files[:20]:
            name = Path(f.filename or "posting.pdf").name
            if not name.lower().endswith((".pdf", ".txt")):
                raise HTTPException(status_code=422, detail=f"{name}: use a PDF (or .txt)")
            data = f.file.read(15 * 1024 * 1024 + 1)
            if len(data) > 15 * 1024 * 1024:
                raise HTTPException(status_code=413, detail=f"{name} is over 15 MB")
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / name
                path.write_bytes(data)
                try:
                    text = load_job_text(path)
                except Exception as e:
                    raise HTTPException(status_code=422, detail=f"{name}: could not read it ({type(e).__name__})")
            if len(text.strip()) < 200:
                raise HTTPException(status_code=422, detail=f"{name}: no text found (a scanned image?)")
            added.append(_new(text, source="pdf", file=name, company=company_from_file(name, text)))
        with store.lock:
            items = store.load()
            items[:0] = added
            store.save(items)
        return [_summary(p) for p in added]

    @app.patch("/api/desk/postings/{pid}", dependencies=auth)
    def patch_posting(pid: str, req: PatchRequest) -> dict:
        with store.lock:
            items = store.load()
            p = store.find(items, pid)
            if req.stage is not None:
                if req.stage not in STAGES:
                    raise HTTPException(status_code=422, detail=f"stage must be one of {', '.join(STAGES)}")
                if req.stage == "applied" and p.get("stage") != "applied":
                    p["applied_at"] = datetime.now().isoformat(timespec="seconds")
                p["stage"] = req.stage
            for k in ("title", "company", "notes"):
                if getattr(req, k) is not None:
                    p[k] = getattr(req, k).strip()
            store.save(items)
            if req.stage is not None:
                _update_tracker(items)
        return _summary(p)

    overrides_path = settings.profile_dir / "profile_overrides.json"

    def _update_tracker(items: list[dict]) -> None:
        try:
            write_tracker(settings, items)
        except Exception as e:  # e.g. the sheet is open in Excel on Windows: the next change writes it again
            print(f"desk: could not update the tracker ({type(e).__name__})", flush=True)

    @app.get("/api/desk/extension.zip", dependencies=auth)
    def extension_zip() -> Response:
        """The Tailor Assist extension as a zip, to load in Chrome on this or another computer (Load unpacked). Only the
        extension's own files: the access token is never in them (it is pasted into the Options page)."""
        folder = EXTENSION_DIR
        if not (folder / "manifest.json").exists():
            raise HTTPException(status_code=404, detail="the extension folder is missing")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(folder.rglob("*")):
                if f.is_file() and not f.name.startswith(".") and "__pycache__" not in f.parts:
                    z.write(f, Path("TailorAssist") / f.relative_to(folder))
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="TailorAssist_extension.zip"'})

    @app.get("/api/desk/tracker", dependencies=auth)
    def tracker() -> FileResponse:
        path = write_tracker(settings, store.load())
        return FileResponse(path, filename=TRACKER_NAME,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/desk/me", dependencies=auth)
    def get_me() -> dict:
        """What forms get filled with: from the resume (read-only here) and what the person entered on My info."""
        extras = (json.loads(overrides_path.read_text(encoding="utf-8")).get("extras", {})
                  if overrides_path.exists() else {})
        base = {}
        if profile is not None:
            base = {"name": profile.full_name, "email": profile.email, "phone": profile.phone,
                    "linkedin": profile.linkedin_url or "", "portfolio": profile.portfolio_url or ""}
        return {"from_resume": base, "mine": {k: extras.get(k) or "" for k in MY_INFO}}

    @app.put("/api/desk/me", dependencies=auth)
    def put_me(body: MyInfo) -> dict:
        data = json.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path.exists() else {}
        extras = data.get("extras", {})
        for k in MY_INFO:
            v = getattr(body, k).strip()
            if v:
                extras[k] = v
            else:
                extras.pop(k, None)
            if profile is not None:
                setattr(profile, k, v or None)  # the next Fill uses it: no restart
        data["extras"] = extras
        overrides_path.parent.mkdir(parents=True, exist_ok=True)
        overrides_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return get_me()

    # One-click Apply: the Desk opens the posting with a one-time code in the link; the extension fills the form only if
    # the server confirms that code (used once, within two minutes). A web page cannot make it fill by itself.
    open_codes: dict[str, tuple[str, float]] = {}

    def apply_url(p: dict) -> str:
        url = p.get("url") or ""
        u = urlparse(url)
        if u.hostname == "jobs.lever.co" and not u.path.rstrip("/").endswith("/apply"):
            url = url.split("?")[0].rstrip("/") + "/apply"  # Lever's form is on its own page
        if (form := greenhouse_form(url, settings)):
            url = form
        return url.split("#")[0]

    @app.post("/api/desk/postings/{pid}/open", dependencies=auth)
    def open_posting(pid: str) -> dict:
        p = store.find(store.load(), pid)
        if not p.get("url"):
            raise HTTPException(status_code=422, detail="this posting has no link (it came from a PDF)")
        now = time.time()
        for k in [k for k, (_, t) in open_codes.items() if now - t > 120]:
            del open_codes[k]
        code = secrets.token_urlsafe(12)
        open_codes[code] = (pid, now)
        with store.lock:
            items = store.load()
            q = store.find(items, pid)
            if q.get("stage") == "saved":
                q["stage"] = "applying"
                store.save(items)
        return {"url": f"{apply_url(p)}#tailor-fill={code}"}

    # ---- the apply queue (2026-10-13, the person: "get project tailor to apply automatically for me"): postings are
    # opened one after another; each is filled step by step up to Submit, the person submits it, and the extension asks
    # for the next one. Nothing here submits anything.
    queue_path = settings.data_dir / "desk" / "apply_queue.json"

    def _load_queue() -> list[str]:
        try:
            return [str(x) for x in json.loads(queue_path.read_text(encoding="utf-8"))]
        except (OSError, ValueError):
            return []

    def _save_queue(ids: list[str]) -> None:
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(json.dumps(ids), encoding="utf-8")

    def _queueable(p: dict) -> bool:
        url = p.get("url") or ""
        return bool(url) and p.get("stage") not in ("applied", "skipped") and "12twenty.com" not in url

    @app.post("/api/desk/queue", dependencies=auth)
    def set_queue(body: dict) -> dict:
        items = {p["id"]: p for p in store.load()}
        ids = [str(i) for i in (body.get("ids") or []) if str(i) in items and _queueable(items[str(i)])][:100]
        _save_queue(ids)
        return {"ids": ids, "left": len(ids)}

    @app.get("/api/desk/queue", dependencies=auth)
    def get_queue() -> dict:
        ids = _load_queue()
        return {"ids": ids, "left": len(ids)}

    @app.post("/api/desk/queue/next", dependencies=auth)
    def queue_next() -> dict:
        """The next posting to apply to, opened by the extension after the person submits the one before."""
        ids = _load_queue()
        items = {p["id"]: p for p in store.load()}
        while ids:
            pid = ids.pop(0)
            p = items.get(pid)
            if not p or not _queueable(p):
                continue
            _save_queue(ids)
            opened = open_posting(pid)
            return {"done": False, "id": pid, "url": opened["url"], "title": p.get("title", ""),
                    "company": p.get("company", ""), "left": len(ids)}
        _save_queue([])
        return {"done": True, "left": 0}

    @app.post("/api/desk/autofill/{code}", dependencies=auth)
    def autofill_ok(code: str) -> dict:
        hit = open_codes.pop(code, None)  # once only
        if not (hit and time.time() - hit[1] <= 120):
            return {"ok": False}
        p = store.find(store.load(), hit[0])
        # the posting goes along: a form-only page (Greenhouse's direct form) has no job description to tailor to
        return {"ok": True, "posting": {"title": p.get("title", ""), "url": p.get("url", ""),
                                        "text": (p.get("text") or "")[:40000]}}

    @app.post("/api/desk/applied", dependencies=auth)
    def mark_applied(req: AppliedRequest) -> dict:
        """The extension saw a "thank you for applying" page: mark the matching posting applied (never un-applies)."""
        from ..fill.answers import add_to_library

        with store.lock:
            items = store.load()
            p = match_applied(items, req.url, req.title, req.page_text)
            kept = add_to_library(settings.profile_dir / "answer_library.json", req.answers,
                                  company=(p or {}).get("company", ""), title=(p or {}).get("title", "")) if req.answers else 0
            if kept:
                print(f"desk: kept {kept} answer(s) in the answer library", flush=True)
            if not p:
                return {"matched": None, "kept_answers": kept}
            already = p.get("stage") == "applied"
            p["stage"] = "applied"
            p.setdefault("applied_at", datetime.now().isoformat(timespec="seconds"))
            if req.resume_id or "sent_resume" not in p:
                # the version the form actually got, and its keyword match (the tracker's Resume column)
                p["sent_resume"] = req.resume_id
                rep_path = settings.output_dir / "tailored" / f"{req.resume_id}.json"
                try:
                    p["sent_ats"] = json.loads(rep_path.read_text(encoding="utf-8")).get("ats_after") if req.resume_id else None
                except (OSError, ValueError):
                    p["sent_ats"] = None
            store.save(items)
            _update_tracker(items)
        print(f"desk: marked applied {p['id']} ({'already' if already else 'new'})", flush=True)
        return {"matched": p["id"], "title": p["title"], "company": p.get("company", ""), "already": already,
                "kept_answers": kept}

    @app.delete("/api/desk/postings/{pid}", dependencies=auth)
    def delete_posting(pid: str) -> dict:
        with store.lock:
            items = store.load()
            store.find(items, pid)
            store.save([p for p in items if p["id"] != pid])
        return {"deleted": pid}

    from .search_routes import register_search  # the role search saves into this Desk

    register_search(app, settings, auth, store, run_tailor, browser_lock)

    from .network_routes import register_network  # people to ask for referrals, per posting

    register_network(app, settings, auth, store, profile, token)

    @app.post("/api/desk/postings/{pid}/tailor", dependencies=auth)
    def tailor_posting(pid: str, ask: TailorAsk) -> dict:
        return tailor_desk_posting(store, run_tailor, pid, ask.instruction)

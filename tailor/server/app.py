"""Local API for the browser extension. Listens on 127.0.0.1 only.

Three protections, because a web page can otherwise try to reach a local server:
  * a secret token (X-Tailor-Token) that only the extension knows,
  * a Host-header check (blocks DNS-rebinding, where a hostile site points its own name at 127.0.0.1),
  * CORS limited to browser-extension origins.
The extension can read your profile and ask the model for drafts. It cannot submit anything: this server has no
endpoint that does, and the extension contains no code that clicks Submit.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, model_validator

from ..config import Settings
from ..fill.answers import NeedsApplicant, SensitiveQuestion, draft_answer, load_library, similar_answers
from ..fill.forms import Action, FieldOption, FormField
from ..fill.mapper import _TOGETHER as TOGETHER, entries_to_add, match_option, norm, plan_fields, plan_fields_with_notes
from ..fill.profile import Profile
from ..fill.sensitive import classify_text
from ..llm import LLMClient
from ..llm.clients import StubClient
from ..resume import wordings
from ..resume.auto import has_job_description, load_bank, tailor_resume
from ..resume.rules import load_rules
from ..resume.schema import Resume
from .desk import register_desk

_ALLOWED_HOSTS = re.compile(r"^(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$")
_EXTENSION_ORIGIN = r"^(?:chrome|edge)-extension://[a-p]{32}$"


class _FromPage(BaseModel):
    """What the extension sends from a web page. A page with no title sends null and a chat note can run long: those
    become "" and a cut note, never a refusal (a refused request looked like a "Write it" button that did nothing)."""

    @model_validator(mode="before")
    @classmethod
    def _tidy(cls, data):
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for name, field in cls.model_fields.items():
            value = out.get(name)
            if value is None and name in out and field.annotation is str:
                out[name] = ""
            elif isinstance(value, str):
                limit = next((m.max_length for m in field.metadata if getattr(m, "max_length", None)), None)
                if limit and len(value) > limit:
                    out[name] = value[:limit]
        if isinstance(out.get("max_chars"), int) and not 20 <= out["max_chars"] <= 10000:
            out["max_chars"] = None  # a box that small or that large: no length hint
        return out


class FillRequest(_FromPage):
    url: str = ""
    title: str = ""
    page_text: str = ""
    resume_id: str = ""  # the resume tailored to this posting: the form's answers then follow it
    fields: list[FormField] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list, max_length=20)  # what this extension version can do


class FillResponse(BaseModel):
    actions: list[Action]
    summary: dict[str, int]
    needs_you: list[str]
    notes: list[str] = Field(default_factory=list)  # things to do that no field shows (a work entry to add)
    add_entries: dict[str, int] = Field(default_factory=dict)  # {"experience": 3}: entries the page lacks (Workday Add)


class ChooseRequest(BaseModel):
    value: str = Field(max_length=2000)
    key: str = Field(default="", max_length=60)  # the profile key it came from ("edu_end_month"), if any
    options: list[str] = Field(default_factory=list, max_length=1000)


class CoverRequest(_FromPage):
    url: str = ""
    title: str = ""
    company: str = ""
    page_text: str = ""
    instruction: str = Field(default="", max_length=2000)
    resume_id: str = Field(default="", pattern=r"^(?:[0-9a-f]{12})?$")  # the letter tells the tailored resume's story
    reuse: bool = False  # the fill's automatic letter: the one already written for this posting, if any


class AnswerRequest(_FromPage):
    question: str
    page_text: str = ""
    max_chars: int | None = Field(default=None, ge=20, le=10000)
    instruction: str = Field(default="", max_length=2000)  # what the person typed in the chat box
    current: str = Field(default="", max_length=6000)  # the text already in the answer box, to revise
    hint: str = Field(default="", max_length=600)  # the form's note under the question ("Please share 3-4 sentences")


class AnswerResponse(BaseModel):
    text: str
    warnings: list[str]


def _with_text(req, text: str):
    """The request with the posting text swapped in: the extension's (a pydantic model) or the Desk's (a namespace)."""
    if hasattr(req, "model_copy"):
        return req.model_copy(update={"page_text": text})
    from types import SimpleNamespace

    return SimpleNamespace(**{**vars(req), "page_text": text})


def shows_posting(page_text: str, posting: str) -> bool:
    """Whether the page shows this posting's description (most of its long lines), not just the application form."""
    norm = lambda t: re.sub(r"\s+", " ", t).strip().lower()
    page = norm(page_text or "")
    lines = [norm(x) for x in (posting or "").splitlines() if len(x.strip()) > 80][:10]
    return bool(lines) and sum(x[:80] in page for x in lines) >= len(lines) / 2


# A page that asks for answers written without AI: drafts are never offered there
NO_AI_ANSWERS = re.compile(
    r"AI[- ]generated (?:responses|answers|content|applications)[^.]{0,80}(?:not be considered|disqualif|not (?:be )?accept|rejected)|"
    r"(?:do not|don['’]t|please don['’]t|please do not|refrain from|avoid) (?:using |use )?(?:any )?(?:AI|ChatGPT|generative AI|AI tools?|"
    r"AI assistants?|large language models?)[^.]{0,80}(?:write|answer|respond|complete|draft)|"
    r"AI partnership guidelines|in your own words,? (?:without|not) (?:the )?(?:use of |help of )?(?:AI|ChatGPT)|"
    r"(?:without|no) (?:the )?(?:assistance|help|use) of (?:AI|ChatGPT|generative AI)", re.I)


class TailorRequest(_FromPage):
    url: str = ""
    title: str = ""
    page_text: str = ""
    instruction: str = Field(default="", max_length=2000)  # from the chat box: "lean on the AI/ML qualification"


class TailorResponse(BaseModel):
    applied: bool
    id: str = ""
    summary: str = ""
    report: dict = Field(default_factory=dict)


class SkillConfirm(BaseModel):
    name: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9 /&+#.\-]*$")
    remove: bool = False


class RememberRequest(BaseModel):
    question: str
    answer: str


def load_or_create_token(settings: Settings) -> str:
    path = settings.data_dir / "server_token.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    settings.ensure_dirs()
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


def create_app(settings: Settings, client: LLMClient, profile: Profile, token: str,
               resume_pdf: Path | None = None, base_resume: Resume | None = None) -> FastAPI:
    app = FastAPI(title="Tailor Assist", docs_url=None, redoc_url=None, openapi_url=None)
    sheet_path = settings.profile_dir / "answer_sheet.json"
    resume_pdf = resume_pdf or settings.output_dir / "current_resume.pdf"

    standing_path = settings.profile_dir / "standing_answers.json"

    def load_standing() -> dict:
        return json.loads(standing_path.read_text(encoding="utf-8")) if standing_path.exists() else {}

    def load_sheet() -> dict:
        return json.loads(sheet_path.read_text(encoding="utf-8")) if sheet_path.exists() else {}

    app.add_middleware(CORSMiddleware, allow_origin_regex=_EXTENSION_ORIGIN, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type", "X-Tailor-Token"],
                       # the resume's file name ("First_Last_Resume.pdf"): hidden from the extension unless exposed
                       expose_headers=["Content-Disposition"])

    @app.middleware("http")
    async def only_local_hosts(request: Request, call_next):
        if not _ALLOWED_HOSTS.match(request.headers.get("host", "")):
            response = JSONResponse({"detail": "unexpected Host header"}, status_code=421)
        else:
            response = await call_next(request)
        # one line per request, to see what the extension reaches (never the token or any body)
        origin = request.headers.get("origin", "-")
        print(f"request: {request.method} {request.url.path} -> {response.status_code} | origin {origin[:60]}", flush=True)
        return response

    def require_token(x_tailor_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_tailor_token.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="missing or wrong token")

    @app.post("/api/extension-loaded", dependencies=[Depends(require_token)])
    def extension_loaded(body: dict) -> dict:
        """The extension says which version Chrome loaded (on install, reload, update and browser start), so an old
        copy still running is visible in the server log."""
        print(f"extension loaded: v{str(body.get('version', '?'))[:20]} ({str(body.get('why', ''))[:20]})", flush=True)
        return {"ok": True}

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "model": settings.model.kind}

    def found_role(url: str) -> dict | None:
        """The role search's record for this application (it keeps U.S. roles only), matched by job link: the form's
        page often does not say where the job is. None when the search never found it."""
        from ..jobs.search import SearchStore, job_key

        key = job_key(url)
        if not key:
            return None
        try:
            results = SearchStore(settings.data_dir).load()["results"].values()
        except Exception:
            results = []
        hit = next((r for r in results if job_key(r.get("url", "")) == key), None)
        if hit:
            return hit
        # a role saved on the Desk (by the search or the person) counts too: its posting says where it is
        from .desk import DeskStore
        try:
            for p in DeskStore(settings).load():
                if job_key(p.get("url") or "") == key:
                    text = p.get("text") or ""
                    place = us_places(text)
                    return {"location": place} if place else None
        except Exception:
            return None
        return None

    def saved_posting(url: str) -> str:
        """The full posting saved for this job (Desk first, then the role search), matched by job link. An embedded
        application form (Greenhouse job_app on a company site) shows only the questions, not the job description."""
        from ..jobs.search import SearchStore, job_key
        from .desk import DeskStore

        key = job_key(url)
        if not key:
            return ""
        texts = []
        try:
            texts += [p.get("text") or "" for p in DeskStore(settings).load() if job_key(p.get("url") or "") == key]
        except Exception:
            pass
        try:
            texts += [r.get("text") or "" for r in SearchStore(settings.data_dir).load()["results"].values()
                      if job_key(r.get("url", "")) == key]
        except Exception:
            pass
        best = max(texts, key=len, default="")
        if not best and (m := re.match(r"https://jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url or "")):
            best = ashby_posting(m[1], m[2])
        if not best and (m := re.search(r"smartrecruiters\.com/oneclick-ui/company/([^/?#]+)/publication/([0-9a-f-]{36})",
                                        url or "")):
            best = smartrecruiters_posting(m[1], m[2])
        return best

    def smartrecruiters_posting(company: str, uuid: str) -> str:
        """SmartRecruiters' one-click application shows only the form (Roland Berger, 2026-10-10: 395 characters, so no
        tailoring); its publication id is the posting's uuid in the company's public postings feed."""
        import html

        import httpx

        base = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
        try:
            for offset in range(0, 1000, 100):
                page = httpx.get(base, params={"limit": 100, "offset": offset}, timeout=15).json()
                hit = next((p for p in page.get("content", []) if p.get("uuid") == uuid), None)
                if hit or len(page.get("content", [])) < 100:
                    break
            if not hit:
                return ""
            job = httpx.get(f"{base}/{hit['id']}", timeout=15).json()
        except Exception:
            return ""
        sections = (job.get("jobAd") or {}).get("sections") or {}
        parts = [html.unescape(re.sub(r"<[^>]+>", " ", (sections.get(k) or {}).get("text") or ""))
                 for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")]
        place = (job.get("location") or {}).get("city") or ""
        text = "\n".join([job.get("name", ""), f"{company} - {place}"] + [re.sub(r"[ \t]+", " ", p).strip() for p in parts])
        print(f"tailor: read the SmartRecruiters posting for this form ({len(text)} chars)", flush=True)
        return text

    def fetch_posting_for_form(url: str, title: str = "") -> str:
        """An application step with no job description (Cornerstone's 9-step form, 2026-10-10): open the posting page
        its link points to, and save the description to the Desk, so the later steps, the cover letter and the tracker
        have it. "" when there is no posting page to read."""
        from ..jobs.search import posting_url
        from .desk import DeskStore, add_posting_record, company_from, fetch_posting

        src = posting_url(url)
        if not src:
            return ""
        try:
            with tailor_lock:
                got = fetch_posting(src, settings)
        except Exception as e:
            print(f"tailor: could not read the posting page ({type(e).__name__})", flush=True)
            return ""
        text = got.get("text") or ""
        if not has_job_description(text, title):
            return ""
        try:
            name = re.sub(r"^(?:apply for|job application for)\s+|\s*\|.*$", "", title or got.get("title") or "", flags=re.I)
            add_posting_record(DeskStore(settings), text, url=src, title=name.strip(),
                               company=company_from(src, got.get("site", ""), text), source="auto")
        except Exception:
            pass
        print(f"tailor: read the posting page for this form ({len(text)} chars) and saved it to the Desk", flush=True)
        return text

    def desk_company(url: str) -> str:
        """The company of the Desk posting for this job link ("" if none): a form page title may not name it."""
        from ..jobs.search import job_key
        from .desk import DeskStore

        key = job_key(url)
        try:
            return next((p.get("company") or "" for p in DeskStore(settings).load()
                         if key and job_key(p.get("url") or "") == key and p.get("company")), "")
        except Exception:
            return ""

    def ashby_posting(board: str, job_id: str) -> str:
        """An Ashby application tab shows only the form: the description comes from Ashby's public job board feed."""
        import httpx

        try:
            jobs = httpx.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}", timeout=15).json().get("jobs", [])
        except Exception:
            return ""
        job = next((j for j in jobs if j.get("id") == job_id), None)
        if not job:
            return ""
        place = ", ".join([job.get("location") or ""] + [x.get("location", "") for x in job.get("secondaryLocations") or []])
        return f"{job.get('title', '')}\n{board} - {place.strip(', ')}\n{job.get('descriptionPlain') or ''}"

    def us_places(page_text: str) -> str:
        """U.S. places the application page names for the role ("... 2027 Start - Mountain View, New York, San Bruno or
        San Jose" on Google), when it names no other country: then "the country of employment" is the U.S."""
        from ..jobs.search import NOT_US, US, US_STATE

        head = (page_text or "")[:3000]
        from ..fill.profile import BAY_AREA

        names = "|".join(re.escape(c) for c in BAY_AREA + ["New York", "Chicago", "Seattle", "Boston", "Austin",
                                                           "Los Angeles", "Atlanta", "Denver", "Remote (US)"])
        cities = re.findall(rf"\b({names}|Washington, D\.?C\.?)\b", head)
        if not cities or NOT_US.search(head) and not (US.search(head) or US_STATE.search(head)):
            return ""
        return ", ".join(dict.fromkeys(cities))

    def profile_for(resume_id: str, page_text: str) -> Profile:
        """The profile the form is filled from: built from the resume tailored to this posting when there is one (its
        bullets in the role descriptions, its degree first), with the person's cities ranked by the posting's offices."""
        from ..fill.profile import profile_from_resume, rank_locations

        base = profile
        posting = page_text
        if re.fullmatch(r"[0-9a-f]{12}", resume_id or "") and (tailored_dir / f"{resume_id}.resume.json").exists():
            try:
                over_path = settings.profile_dir / "profile_overrides.json"
                over = json.loads(over_path.read_text(encoding="utf-8")) if over_path.exists() else {}
                tailored_resume = Resume.model_validate_json(
                    (tailored_dir / f"{resume_id}.resume.json").read_text(encoding="utf-8"))
                built = profile_from_resume(tailored_resume, overrides=over.get("name_overrides"),
                                            extras=over.get("extras"), education_starts=over.get("education_starts"),
                                            education_order=over.get("education_order"),
                                            fields_of_study=over.get("fields_of_study"))
                # what the resume does not hold (My info, set after the server started) stays as it is now
                base = built.model_copy(update={k: getattr(profile, k) for k in (
                    "address", "postal_code", "city", "state", "country", "phone_type", "how_heard", "preferred_name",
                    "github_url", "available_start", "location_preferences", "gpa", "education_gpas", "high_school_year", "languages",
                    "interviewed_at", "relatives_at", "government_official", "conflict_of_interest")})
                if (tailored_dir / f"{resume_id}.posting.txt").exists():
                    posting = (tailored_dir / f"{resume_id}.posting.txt").read_text(encoding="utf-8") + "\n" + page_text
            except Exception as e:  # an old or broken file: the usual profile
                print(f"fill: tailored profile unavailable ({type(e).__name__})", flush=True)
        return base.model_copy(update={"location_preferences": rank_locations(base.location_preferences, posting)})

    def cover_rule(actions: list[Action], fields: dict[str, FormField]) -> list[Action]:
        """Cover letters: written and attached on every form that takes one (profile_overrides "cover_letters":
        "always", the default), only where the form requires one ("required"), or never ("never": the person
        writes them). One letter per form: a text box for the letter is left alone when there is an upload for it."""
        try:
            over = json.loads((settings.profile_dir / "profile_overrides.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            over = {}
        mode = str(over.get("cover_letters", "always")).lower()
        has_upload = any(a.kind == "cover" and fields.get(a.field_id) and fields[a.field_id].type == "file" for a in actions)
        out = []
        for a in actions:
            f = fields.get(a.field_id)
            if a.kind == "cover" and f is not None:
                if mode == "never" or (mode == "required" and not f.required):
                    a = a.model_copy(update={"kind": "needs_you" if f.required else "skip",
                                             "reason": "cover letter: you write this one (your setting)"})
                elif f.type != "file" and has_upload:
                    a = a.model_copy(update={"kind": "skip", "reason": "the letter goes to the upload"})
            out.append(a)
        return out

    @app.get("/api/transcript", dependencies=[Depends(require_token)])
    def transcript(which: str = "") -> FileResponse:
        """The person's own transcript (profile/transcript.pdf), for upload boxes that ask for one; which=undergraduate:
        the undergraduate one (profile/undergrad_transcript.pdf)."""
        under = which == "undergraduate"
        path = settings.profile_dir / ("undergrad_transcript.pdf" if under else "transcript.pdf")
        if not path.exists():
            raise HTTPException(status_code=404, detail="no transcript is saved")
        print(f"{'undergraduate ' if under else ''}transcript sent to the browser", flush=True)
        return FileResponse(path, media_type="application/pdf",
                            filename=f"{profile.first_name}_{profile.last_name}_{'Undergraduate_' if under else ''}Transcript.pdf")

    @app.get("/api/entries", dependencies=[Depends(require_token)])
    def entries(resume_id: str = "") -> dict:
        """The schools and jobs a form's entries are made from, in order, with the names that show one is already on
        the page (a site whose Add opens an editor fills them one by one; see modalEntries in content.js)."""
        from ..fill.mapper import _school_title

        p = profile_for(resume_id if re.fullmatch(r"[0-9a-f]{12}", resume_id or "") else "", "")
        edu = [{"label": f"{e.degree}, {e.school}", "keys": list(dict.fromkeys([e.school, _school_title(e.school)]))}
               for e in p.education]
        jobs = [{"label": f"{x.title}, {x.employer}", "keys": [x.employer]} for x in p.experience if x.kind == "job" and x.employer]
        return {"education": edu, "experience": jobs}

    @app.post("/api/fill-plan", response_model=FillResponse, dependencies=[Depends(require_token)])
    def fill_plan(req: FillRequest) -> FillResponse:
        role = found_role(req.url)
        page_place = us_places(req.page_text) if role is None else ""
        plan_profile = profile_for(req.resume_id, req.page_text)
        actions, notes = plan_fields_with_notes(req.fields, plan_profile, load_sheet(), url=req.url, standing=load_standing(),
                                                us_role=role is not None or bool(page_place),
                                                role_location=(role or {}).get("location", "") or page_place)
        summary: dict[str, int] = {}
        for a in actions:
            summary[a.kind] = summary.get(a.kind, 0) + 1
        if NO_AI_ANSWERS.search(req.page_text or ""):
            # the employer asks for answers in the applicant's own words (Anthropic's AI guidelines, Solace: "AI-generated
            # responses ... will likely not be considered", 2026-10-07): nothing is drafted, the person writes them
            fields = {f.id: f for f in req.fields}
            actions = [a.model_copy(update={"kind": "needs_you" if a.kind == "draft" or (fields.get(a.field_id) and
                                                                                       fields[a.field_id].required) else "skip",
                                            "value": None,
                                            "reason": "this employer asks for answers in your own words, without AI"})
                       if a.kind in ("draft", "cover") else a for a in actions]
        actions = cover_rule(actions, {f.id: f for f in req.fields})
        uploads = [a for a in actions if a.kind == "upload"]
        if len(uploads) == 1 and not any(a.kind == "cover" for a in actions) and TOGETHER.search(norm(req.page_text or "")):
            # the page, not the box, says it (Simon-Kucher's step 7, 2026-10-10: "Please upload your Resume and Cover
            # Letter together in one document" above a box labelled only "Upload Document")
            actions = [a.model_copy(update={"kind": "cover", "value": "with_resume", "source": "llm",
                                            "reason": "your resume and cover letter in one PDF, as the form asks"})
                       if a is uploads[0] else a for a in actions]
        if any(a.kind == "cover" and a.value == "with_resume" for a in actions):
            combined_pages.add(re.sub(r"[?#].*$", "", req.url))  # its cover letter is served with the resume in front
        if not ((settings.profile_dir / "undergrad_transcript.pdf").exists() and "transcript_which" in req.features):
            # no undergraduate transcript saved, or an older extension that would attach the graduate one there
            fields_by_id = {f.id: f for f in req.fields}
            actions = [a.model_copy(update={"kind": "needs_you" if fields_by_id[a.field_id].required else "skip", "value": None,
                                            "reason": "undergraduate transcript: attach it yourself"})
                       if a.kind == "transcript" and a.value == "undergraduate" and a.field_id in fields_by_id else a
                       for a in actions]
        if not (settings.profile_dir / "transcript.pdf").exists():  # no transcript saved: those boxes are the person's
            fields_by_id = {f.id: f for f in req.fields}
            actions = [a.model_copy(update={"kind": "needs_you" if fields_by_id[a.field_id].required else "skip",
                                            "reason": "a transcript: none is saved (profile/transcript.pdf)"})
                       if a.kind == "transcript" and a.field_id in fields_by_id else a for a in actions]
        labels = {f.id: (f.label or f.name or f.id) for f in req.fields}
        add = entries_to_add(req.fields, plan_profile)
        entries = {s: len({f.section_index for f in req.fields if f.section == s}) for s in ("education", "experience")}
        # no personal data: how many entries the page has and how many the plan asks the extension to add
        print(f"fill-plan: {req.url[:70]} | {len(req.fields)} fields | entries {entries} | add {add}", flush=True)
        return FillResponse(actions=actions, summary=summary, notes=notes, add_entries=add,
                            needs_you=[labels[a.field_id] for a in actions if a.kind == "needs_you"])

    @app.post("/api/choose", dependencies=[Depends(require_token)])
    def choose(req: ChooseRequest) -> dict:
        """Which option of a custom dropdown means this answer (the list's wording, not the answer's: "5" -> "May",
        "No" -> "I have never worked here"). Only ever one of the options given, or none."""
        opts = [FieldOption(value=o, label=o) for o in req.options if o.strip()]
        value = req.value
        if req.key == "city" and "," not in value and profile is not None and profile.state:
            value = f"{value}, {profile.state}"  # a city alone does not say which Springfield
        pick = match_option(value, opts, req.key)
        if pick is None and req.key == "edu_field" and profile is not None:
            for e in profile.education:  # "Information Technology" not listed: try the resume's "Information, ..."
                if e.form_field and norm(e.form_field) == norm(value):
                    for alt in (e.field.split(",")[0], e.field):
                        if (pick := match_option(alt, opts, req.key)):
                            break
                if pick:
                    break
        if pick is None and req.key.startswith("location_pref_") and profile is not None:
            n = int(req.key.rsplit("_", 1)[1]) - 1  # the n-th of the person's cities that this list has
            have = [o for c in profile.location_preferences if (o := match_option(c, opts, "city_list"))]
            have = list(dict.fromkeys(o.label for o in have))
            pick = next((o for o in opts if o.label == (have[n] if n < len(have) else None)), None)
        return {"choice": pick.label if pick else None}

    @app.post("/api/answer", response_model=AnswerResponse, dependencies=[Depends(require_token)])
    def answer(req: AnswerRequest) -> AnswerResponse:
        # A question the profile answers ("How did you hear about this job?" -> LinkedIn) gets that fact, not a pitch
        # written by the model (found 2026-10-02: the chat box put three sentences into Oscar's "How did you hear").
        fact = plan_fields([FormField(id="q", label=req.question, type="text", maxlength=req.max_chars)], profile, {},
                           standing=load_standing())[0]
        if fact.kind == "fill" and fact.source == "profile" and fact.value:
            return AnswerResponse(text=fact.value, warnings=["a fact from your profile, not a draft"])
        try:
            examples = similar_answers(load_library(settings.profile_dir / "answer_library.json"), req.question)
            draft = draft_answer(client, profile, req.question, req.page_text, req.max_chars,
                                 instruction=req.instruction, current=req.current, hint=req.hint, examples=examples)
        except (SensitiveQuestion, NeedsApplicant) as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:  # model not running, out of memory, timeout...
            raise HTTPException(status_code=503, detail=f"model unavailable: {type(e).__name__}")
        if draft.text.strip() == "[stub reply]":  # the placeholder model: never put that in someone's application
            raise HTTPException(status_code=503, detail="the writing model is not running. Free about 6 GB of RAM and "
                                                        "start the server with --model ollama")
        print(f"answer: {req.question[:60]!r} | note: {req.hint[:60]!r} | {len(draft.text.split())} words, "
              f"{len(draft.warnings)} warning(s)", flush=True)  # the question, never the answer
        return AnswerResponse(text=draft.text, warnings=draft.warnings)

    tailor_lock = threading.Lock()  # one browser render at a time
    tailored: dict[str, TailorResponse] = {}
    tailored_dir = settings.output_dir / "tailored"
    ai_state = {"running": False}
    ai_lock = threading.Lock()

    def start_ai_fill(terms: set[str] | None = None, delay: float = 0.0) -> bool:
        """Write the missing AI keyword wordings in the background, one model call at a time (resume/wordings.py).
        Tailoring never waits for them: a wording made now is used from the next tailoring on."""
        if base_resume is None or isinstance(client, StubClient) or not wordings.enabled(settings):
            return False
        with ai_lock:
            if ai_state["running"]:
                return False
            ai_state["running"] = True

        def run() -> None:
            try:
                if delay:
                    time.sleep(delay)
                bank = load_bank(settings)
                n = wordings.fill(settings, base_resume, client, bank, protect=load_rules(settings.profile_dir).protect, terms=terms,
                                  model_name=getattr(client, "model", ""), log=lambda m: print(m, flush=True))
                if n:
                    print(f"ai wordings: {n} new", flush=True)
            except Exception as e:  # never take the server down for this
                print(f"ai wordings: stopped ({type(e).__name__})", flush=True)
            finally:
                ai_state["running"] = False

        threading.Thread(target=run, daemon=True, name="ai-wordings").start()
        return True

    if base_resume is not None and not isinstance(client, StubClient):
        try:
            if wordings.enabled(settings) and wordings.missing(settings, base_resume, load_bank(settings)):
                start_ai_fill(delay=120.0)  # after start-up settles; usually there is nothing to do
        except Exception:
            pass

    @app.post("/api/tailor", response_model=TailorResponse, dependencies=[Depends(require_token)])
    def tailor(req: TailorRequest) -> TailorResponse:
        """Tailor the base resume to the posting on this page: rules plus the approved phrase bank, steered by the chat
        instruction, truth-checked. Falls back to the default."""
        return run_tailor(req)

    # The same posting asked again (a tab Chrome reloaded, a second Fill, the Desk and the extension): its tailored resume
    # is reused for a day instead of tailoring again, unless the base resume or the confirmed skills changed or the
    # person gave an instruction (DoorDash, 2026-10-09: a reloaded tab re-tailored and got a new resume id).
    reuse_path = settings.output_dir / "tailored" / "by_posting.json"
    base_sig = hashlib.sha1(base_resume.model_dump_json().encode()).hexdigest()[:12] if base_resume is not None else ""

    def reuse_key(req: TailorRequest) -> str:
        from ..jobs.search import job_key

        overrides = settings.profile_dir / "profile_overrides.json"
        stamp = int(overrides.stat().st_mtime) if overrides.exists() else 0
        url = re.sub(r"#.*$", "", req.url or "") if "#/" not in (req.url or "") else req.url
        return f"{job_key(url) or url}|{base_sig}|{stamp}"

    def reused(req: TailorRequest) -> TailorResponse | None:
        if req.instruction.strip() or not req.url:
            return None
        try:
            hit = json.loads(reuse_path.read_text(encoding="utf-8")).get(reuse_key(req))
        except (OSError, ValueError):
            return None
        if not hit or time.time() - hit.get("at", 0) > 24 * 3600:
            return None
        rid = hit["id"]
        pdf, rep_path = tailored_dir / f"{rid}.pdf", tailored_dir / f"{rid}.json"
        if not pdf.exists() or not rep_path.exists():
            return None
        out = TailorResponse(applied=True, id=rid, summary=hit.get("summary", ""), report=json.loads(rep_path.read_text(encoding="utf-8")))
        tailored[rid] = out
        print(f"tailor: {req.url[:90]} | reused {rid} (same posting, tailored {int((time.time() - hit['at']) / 60)} min ago)", flush=True)
        return out

    def remember_tailored(req: TailorRequest, out: TailorResponse) -> None:
        if req.instruction.strip() or not req.url or not out.applied:
            return
        try:
            data = json.loads(reuse_path.read_text(encoding="utf-8")) if reuse_path.exists() else {}
        except ValueError:
            data = {}
        data[reuse_key(req)] = {"id": out.id, "at": time.time(), "summary": out.summary}
        data = dict(sorted(data.items(), key=lambda kv: -kv[1]["at"])[:500])
        reuse_path.parent.mkdir(parents=True, exist_ok=True)
        reuse_path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def run_tailor(req: TailorRequest) -> TailorResponse:
        """The one tailoring path, shared by the extension and the Tailor Desk."""
        if base_resume is None:
            return TailorResponse(applied=False, summary="Not tailored: no base resume is loaded.")
        again = reused(req)
        if again is not None:
            return again
        saved = saved_posting(req.url)
        if len(saved) > 500 and not shows_posting(req.page_text, saved):  # the page is only the form: tailor to the job's saved posting instead
            req = _with_text(req, saved)
            print(f"tailor: the page shows only the form; using the saved posting ({len(saved)} chars)", flush=True)
        elif not saved and not has_job_description(req.page_text, req.title):
            found = fetch_posting_for_form(req.url, req.title)  # read the posting page once and keep it on the Desk
            if found:
                req = _with_text(req, found)
        with tailor_lock:
            out = tailored_dir / "pending.pdf"
            try:
                result = tailor_resume(base_resume, req.page_text, title=req.title, settings=settings, out_pdf=out,
                                       instruction=req.instruction)
            except Exception as e:  # a missing browser, a render problem: never block the fill
                return TailorResponse(applied=False, summary=f"Not tailored: {type(e).__name__}")
            rep = result.report
            # one line per request (no personal data): which page asked, how much posting text came, what happened
            print(f"tailor: {req.url[:90]} | {len(req.page_text)} chars | {rep.job_id} | "
                  f"{'applied' if rep.applied else 'not applied'} | {', '.join(rep.top_concepts(3))}", flush=True)
            if result.resume is None or not rep.applied:
                return TailorResponse(applied=False, summary=rep.summary(), report=rep.as_dict())
            if not rep.fits:
                return TailorResponse(applied=False, summary="Not tailored: it did not fit on one page.", report=rep.as_dict())
            final = tailored_dir / f"{rep.job_id}.pdf"
            final.parent.mkdir(parents=True, exist_ok=True)
            out.replace(final)
            (tailored_dir / f"{rep.job_id}.json").write_text(json.dumps(rep.as_dict(), indent=2), encoding="utf-8")
            # what the form answers follow for this posting: the tailored resume itself, and the posting (its offices)
            (tailored_dir / f"{rep.job_id}.resume.json").write_text(result.resume.model_dump_json(), encoding="utf-8")
            (tailored_dir / f"{rep.job_id}.posting.txt").write_text(req.page_text[:6000], encoding="utf-8")
            response = TailorResponse(applied=True, id=rep.job_id, summary=rep.summary(), report=rep.as_dict())
            tailored[rep.job_id] = response
            remember_tailored(req, response)
        try:  # keywords this posting asks for that have no AI wording yet: write them for next time
            if wordings.enabled(settings) and not isinstance(client, StubClient):
                from ..jobs import clean_job_text
                from ..resume.keywords import extract_keywords

                asked = {k.term.name for k in extract_keywords(clean_job_text(req.page_text), req.title).items if k.term}
                if wordings.missing(settings, base_resume, load_bank(settings), asked):
                    start_ai_fill(asked)
        except Exception:
            pass
        return response

    @app.post("/api/skills/confirm", dependencies=[Depends(require_token)])
    def confirm_skill(req: SkillConfirm) -> dict:
        """The person says they really have a skill a posting asked for ("I have this" in the panel): from now on it
        counts as evidence, and tailoring adds it where a posting asks for it (profile_overrides "confirmed_skills")."""
        path = settings.profile_dir / "profile_overrides.json"
        over = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        have = [x for x in over.get("confirmed_skills", []) if x.casefold() != req.name.strip().casefold()]
        if not req.remove:
            have.append(req.name.strip())
        over["confirmed_skills"] = have
        path.write_text(json.dumps(over, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"skills: {'removed' if req.remove else 'confirmed'} {req.name.strip()!r}", flush=True)
        return {"confirmed_skills": have}

    @app.get("/api/resume", dependencies=[Depends(require_token)])
    def resume(id: str = "") -> FileResponse:
        path = resume_pdf
        if id:
            # an id this server made: in this run, or saved earlier by the Tailor Desk
            if not re.fullmatch(r"[0-9a-f]{12}", id) or (id not in tailored and not (tailored_dir / f"{id}.pdf").exists()):
                raise HTTPException(status_code=404, detail="unknown tailored resume")
            path = tailored_dir / f"{id}.pdf"
        if not path.exists():
            raise HTTPException(status_code=404, detail="no resume PDF has been generated yet")
        print(f"resume sent to the browser: {'tailored ' + id if id else 'default'} ({path.name})", flush=True)
        return FileResponse(path, media_type="application/pdf",
                            filename=f"{profile.first_name}_{profile.last_name}_Resume.pdf")

    letters_dir = settings.output_dir / "cover_letters"
    combined_pages: set[str] = set()  # pages whose upload box wants the resume and the letter as one PDF
    combined: dict[str, str] = {}  # letter id -> the resume id to put in front of it ("" for the default resume)

    def letter_reply(req: CoverRequest, out: dict) -> dict:
        if re.sub(r"[?#].*$", "", req.url) in combined_pages:
            combined[out["id"]] = req.resume_id
            out["name"] = f"{profile.first_name}_{profile.last_name}_Resume_and_Cover_Letter.pdf"
        return out


    @app.post("/api/cover-letter", dependencies=[Depends(require_token)])
    def cover_letter(req: CoverRequest) -> dict:
        """A cover letter for the posting on this page: written from the applicant's facts, checked like any draft,
        as text and as a PDF to attach (GET /api/cover-letter?id=...). The applicant reads it before submitting."""
        from ..fill.cover import letter_html, render_letter_pdf, write_cover_letter

        # "Job Application for Associate, MBA - Economics/Finance at The Brattle Group" (Greenhouse page titles)
        full = re.sub(r"^(?:job application for|application for)\s+", "", req.title.strip(), flags=re.I)
        found = re.search(r"\s+(?:at|@)\s+(.+?)\s*(?:[|–]|$)", full)
        company = req.company or (found[1].strip() if found else "") or desk_company(req.url)
        title = (full[:found.start()] if found else full.split(" | ")[0]).strip()
        name = f"{profile.first_name}_{profile.last_name}_Cover_Letter.pdf"
        # one letter per posting and resume: a second Fill (or the next step of the same form) reuses it
        # (v2: the letter follows what the posting asks a letter to say; letters written before are not reused)
        key = hashlib.sha1(f"v2|{re.sub(r'[?#].*$', '', req.url)}|{req.resume_id}|{title}|{company}".encode()).hexdigest()[:16]
        index_path = letters_dir / "index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = {}
        hit = index.get(key)
        if req.reuse and not req.instruction.strip() and hit and (letters_dir / f"{hit['id']}.pdf").exists() \
                and time.time() - hit.get("at", 0) < 14 * 86400:
            print(f"cover letter: reused {hit['id']} for {company or '?'}", flush=True)
            return letter_reply(req, {"id": hit["id"], "text": hit["text"], "warnings": hit.get("warnings", []), "name": name,
                                      "reused": True})
        writer = profile_for(req.resume_id, req.page_text) if req.resume_id else profile
        try:
            draft = write_cover_letter(client, writer, company, title, req.page_text, req.instruction)
        except Exception as e:  # model not running, out of memory, timeout...
            raise HTTPException(status_code=503, detail=f"model unavailable: {type(e).__name__}")
        if draft.text.strip() == "[stub reply]":
            raise HTTPException(status_code=503, detail="the writing model is not running")
        if not draft.text.strip():
            raise HTTPException(status_code=503, detail="the letter did not pass the fact check; write it yourself")
        letter_id = secrets.token_hex(6)
        from ..config import apply_environment

        apply_environment(settings)  # the PDF browser's folder (a fresh server has not set it yet)
        with tailor_lock:  # one browser render at a time
            render_letter_pdf(letter_html(profile, draft.text, company), letters_dir / f"{letter_id}.pdf", settings.tmp_dir)
        index[key] = {"id": letter_id, "text": draft.text, "warnings": draft.warnings, "at": time.time()}
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"cover letter: {company or '?'} | {title[:50]} | {len(draft.text.split())} words"
              f"{' | from the tailored resume' if req.resume_id else ''}", flush=True)
        return letter_reply(req, {"id": letter_id, "text": draft.text, "warnings": draft.warnings, "name": name})

    @app.get("/api/cover-letter", dependencies=[Depends(require_token)])
    def cover_letter_pdf(id: str) -> FileResponse:
        path = letters_dir / f"{id}.pdf"
        if not re.fullmatch(r"[0-9a-f]{12}", id) or not path.exists():
            raise HTTPException(status_code=404, detail="unknown cover letter")
        if id in combined:  # the resume first, then the letter, in one PDF
            from pypdf import PdfReader, PdfWriter

            rid = combined[id]
            front = tailored_dir / f"{rid}.pdf" if re.fullmatch(r"[0-9a-f]{12}", rid or "") else resume_pdf
            if front.exists():
                w = PdfWriter()
                for f in (front, path):
                    for page in PdfReader(f).pages:
                        w.add_page(page)
                buf = io.BytesIO()
                w.write(buf)
                print(f"cover letter: sent with the {'tailored' if rid else 'default'} resume in front, as one PDF", flush=True)
                return Response(buf.getvalue(), media_type="application/pdf", headers={"Content-Disposition":
                                f'attachment; filename="{profile.first_name}_{profile.last_name}_Resume_and_Cover_Letter.pdf"'})
        return FileResponse(path, media_type="application/pdf",
                            filename=f"{profile.first_name}_{profile.last_name}_Cover_Letter.pdf")

    @app.post("/api/answer-sheet", dependencies=[Depends(require_token)])
    def remember(req: RememberRequest) -> dict:
        if (cat := classify_text(req.question)):
            raise HTTPException(status_code=422, detail=f"{cat} answers are never saved or filled for you")
        sheet = load_sheet()
        sheet[norm(req.question)] = req.answer
        sheet_path.parent.mkdir(parents=True, exist_ok=True)
        sheet_path.write_text(json.dumps(sheet, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"saved": len(sheet)}

    register_desk(app, settings, require_token, run_tailor, tailor_lock, token, profile,
                  ai_fill=None if isinstance(client, StubClient) else start_ai_fill, ai_state=ai_state)
    return app

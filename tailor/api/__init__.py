"""The tailoring API: Project Tailor's engine for anyone's resume (first version, for local use while it is proven).

    python scripts/run_api.py               # http://127.0.0.1:8770/docs

Stateless on purpose: the resume and posting arrive with each request, the work happens in a throwaway folder, and
nothing about the person is kept or logged. The same truth check as the desktop tool applies: a tailored resume only
chooses, orders and trims what the resume already says, or the request fails with the reason.

Keys: TAILOR_API_KEYS holds the SHA-256 of each key, comma-separated (scripts/run_api.py --new-key makes one).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from ..config import Settings
from ..fill.forms import Action, FormField
from ..fill.mapper import plan_fields_with_notes
from ..fill.profile import Profile
from ..jobs import clean_job_text
from ..resume.auto import MIN_JOB_CHARS, check_result, role_descriptions, tailor_resume, wanted_skills
from ..resume.keywords import extract_keywords, resume_text, score
from ..resume.schema import Resume
from ..resume.validate import errors

VERSION = "0.1"
MAX_PER_MINUTE = 30


class Job(BaseModel):
    text: str = Field(min_length=1, max_length=60000)
    title: str = Field(default="", max_length=300)


class TailorIn(BaseModel):
    resume: Resume
    job: Job
    rules: dict = Field(default_factory=dict)  # tailoring_rules.json's shape: protect, approved_rewrites, ...
    confirmed_skills: list[str] = Field(default_factory=list, max_length=200)
    include_pdf: bool = True


class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=60000)


class ScoreIn(BaseModel):
    resume: Resume
    job: Job


class FillPlanIn(BaseModel):
    profile: Profile
    fields: list[FormField] = Field(max_length=500)
    standing_answers: dict = Field(default_factory=dict)
    url: str = Field(default="", max_length=2000)
    us_role: bool = False


class FillPlanOut(BaseModel):
    actions: list[Action]
    notes: list[str] = Field(default_factory=list)


def _keys_from_env() -> set[str]:
    return {k.strip() for k in os.environ.get("TAILOR_API_KEYS", "").split(",") if k.strip()}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> tuple[str, str]:
    key = "tlr_" + secrets.token_urlsafe(32)
    return key, hash_key(key)


def create_api(keys: set[str] | None = None) -> FastAPI:
    """`keys`: SHA-256 hashes of the keys allowed in (default: TAILOR_API_KEYS). No keys: nobody gets in."""
    allowed = _keys_from_env() if keys is None else set(keys)
    app = FastAPI(title="Project Tailor API", version=VERSION)
    calls: dict[str, deque] = defaultdict(deque)
    limit_lock = threading.Lock()
    render_lock = threading.Lock()  # one browser render at a time

    def auth(authorization: str = Header(default="")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        hashed = hash_key(token)
        if not token or not any(secrets.compare_digest(hashed, k) for k in allowed):
            raise HTTPException(status_code=401, detail="missing or wrong API key")
        with limit_lock:
            q = calls[hashed]
            now = time.time()
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= MAX_PER_MINUTE:
                raise HTTPException(status_code=429, detail="too many requests: try again in a minute")
            q.append(now)
        return hashed

    def workspace(tmp: str, rules: dict | None = None, confirmed: list[str] | None = None) -> Settings:
        settings = Settings(data_dir=Path(tmp))
        settings.ensure_dirs()
        if rules:
            (settings.profile_dir / "tailoring_rules.json").write_text(json.dumps(rules), encoding="utf-8")
        if confirmed:
            (settings.profile_dir / "profile_overrides.json").write_text(json.dumps({"confirmed_skills": confirmed}), encoding="utf-8")
        return settings

    @app.get("/v1/health")
    def health() -> dict:
        return {"ok": True, "version": VERSION}

    @app.post("/v1/parse-resume", dependencies=[Depends(auth)])
    def parse_resume(req: ParseIn) -> dict:
        """A resume's plain text as the structured resume the other calls take, in the person's own words, with what to check."""
        from ..profile.import_resume import parse_resume_text

        try:
            got = parse_resume_text(req.text)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"resume": got.resume.model_dump(exclude_none=True), "warnings": got.warnings}

    @app.post("/v1/score", dependencies=[Depends(auth)])
    def score_resume(req: ScoreIn) -> dict:
        """How well a resume's words match a posting: guidance for the writer, not what any employer's system computes."""
        kw = extract_keywords(clean_job_text(req.job.text), req.job.title)
        s = score(resume_text(req.resume), kw)
        return {"percent": s.percent, "missing": s.missing_hard}

    @app.post("/v1/tailor", dependencies=[Depends(auth)])
    def tailor(req: TailorIn) -> dict:
        """A one-page resume tailored to the posting, with what changed and why. Every result passed the truth check."""
        if len(req.job.text) < MIN_JOB_CHARS:
            raise HTTPException(status_code=422, detail=f"the posting is too short to tailor to (at least {MIN_JOB_CHARS} characters)")
        with tempfile.TemporaryDirectory() as tmp:
            settings = workspace(tmp, req.rules, req.confirmed_skills)
            out_pdf = Path(tmp) / "resume.pdf"
            with render_lock:
                result = tailor_resume(req.resume, req.job.text, title=req.job.title, settings=settings,
                                       out_pdf=out_pdf if req.include_pdf else None, work_dir=settings.tmp_dir)
            if result.resume is None:
                raise HTTPException(status_code=422, detail=result.report.reason or "could not be tailored truthfully")
            problems = [str(i) for i in errors(check_result(req.resume, result))]
            if problems:  # the engine checked it already; a result failing here is never handed out
                raise HTTPException(status_code=500, detail="the tailored resume failed the truth check")
            rep = result.report
            out = {"resume": result.resume.model_dump(),
                   "report": {"keyword_match_before": rep.ats_before, "keyword_match_after": rep.ats_after,
                              "keywords_added": rep.keywords_added, "missing_keywords": rep.missing_keywords,
                              "changes": rep.rewrites + rep.trimmed, "fits_one_page": rep.fits, "warnings": rep.warnings},
                   "role_descriptions": [{"employer": e, "title": t, "bullets": b} for (e, t), b in
                                         role_descriptions(req.resume, req.job.text, title=req.job.title, settings=settings).items()],
                   "wanted_skills": wanted_skills(req.resume, req.job.text, title=req.job.title, settings=settings)}
            if req.include_pdf and out_pdf.exists():
                out["pdf_base64"] = base64.b64encode(out_pdf.read_bytes()).decode()
            return out

    @app.post("/v1/fill-plan", response_model=FillPlanOut, dependencies=[Depends(auth)])
    def fill_plan(req: FillPlanIn) -> FillPlanOut:
        """What to do with each field of an application form. Sensitive questions come back as needs_you unless the
        person's own standing answers cover them. This plans; it never touches a form or submits anything."""
        actions, notes = plan_fields_with_notes(req.fields, req.profile, None, url=req.url, standing=req.standing_answers,
                                                us_role=req.us_role)
        return FillPlanOut(actions=actions, notes=notes)

    return app

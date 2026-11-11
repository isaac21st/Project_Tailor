"""Job schemas.

JobFacts        - deterministic (regex) facts; a model is never asked for these.
JobAnalysis     - what the model produces; every keyword must be found verbatim in the posting.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["required", "preferred", "unknown"]


class YearsReq(BaseModel):
    years: int
    topic: str
    kind: Kind = "unknown"


class JobFacts(BaseModel):
    req_id: str | None = None
    travel_max_pct: int | None = None
    # "limited": "limited sponsorship may be available"; "restricted": employer says it does not
    # intend to hire people who need sponsorship; "mentioned": word appears, stance unclear.
    sponsorship: Literal["not_stated", "limited", "restricted", "mentioned"] = "not_stated"
    sponsorship_quote: str | None = None
    deadline: date | None = None
    onsite_days_min: int | None = None
    min_years: list[YearsReq] = Field(default_factory=list)
    salary_min: int | None = None
    salary_max: int | None = None
    mba_required: bool = False
    final_year_mba: bool = False


class Requirement(BaseModel):
    text: str
    kind: Literal["required", "preferred"]


class JobAnalysis(BaseModel):
    company: str
    title: str
    lens: Literal["consulting", "product", "ux", "project"]
    overlays: list[Literal["healthcare", "ai"]] = Field(default_factory=list)
    domain: str = ""
    summary: str = ""
    requirements: list[Requirement] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)  # ATS terms, copied verbatim from the posting
    notes: list[str] = Field(default_factory=list)  # inconsistencies or things the reader should know

"""The person's permanent career context (layer 1), read from <data_dir>/profile/career_profile.json.

Education, jobs and bullets stay in base_resume.json, the one source of truth. This file holds what a resume does not
say: how the person wants to be positioned, which kinds of roles they are after, and which experience answers which need.
Nothing here can put a new claim on a resume: `context` is read by the fit check only. No file means none of this runs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

FILE_NAME = "career_profile.json"
_CONSULTING_TITLE = re.compile(r"\bconsult|\badvisor|\banalyst\b|\bassociate\b|\btransformation\b|\bstrategy\b", re.I)


class Track(BaseModel):
    id: str
    name: str
    priority: int = 1
    kind: str = "consulting"  # consulting | adjacent (still searched for, but not one of the consulting targets)
    firms: list[str] = Field(default_factory=list)  # examples: a tie-break only, never a reason to score higher
    role_terms: list[str] = Field(default_factory=list)
    industry_terms: list[str] = Field(default_factory=list)
    emphasize: list[str] = Field(default_factory=list)  # concepts (auto.CONCEPTS) to weigh more, if the posting asks for them
    evidence: list[str] = Field(default_factory=list)  # entries that usually carry this kind of role, best first
    language: list[str] = Field(default_factory=list)
    positioning: str = ""


class Need(BaseModel):
    need: str
    terms: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class Context(BaseModel):
    entry: str
    note: str


class NeverClaim(BaseModel):
    term: str
    entry: str


class CareerProfile(BaseModel):
    positioning: str = ""
    not_only: list[str] = Field(default_factory=list)
    recruiting_note: str = ""
    tracks: list[Track] = Field(default_factory=list)
    needs: list[Need] = Field(default_factory=list)
    entry_kinds: dict[str, str] = Field(default_factory=dict)  # part of an entry's name -> work | client_project | academic
    context: list[Context] = Field(default_factory=list)
    never_claim: list[NeverClaim] = Field(default_factory=list)
    outside_targets: list[str] = Field(default_factory=list)
    not_my_function: list[str] = Field(default_factory=list)


def path_for(settings) -> Path:
    return Path(settings.profile_dir) / FILE_NAME


def load_career(settings) -> CareerProfile | None:
    """None when there is no profile, or it cannot be read: tailoring then works exactly as it does without one."""
    try:
        return CareerProfile.model_validate(json.loads(path_for(settings).read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"career profile not used: {type(e).__name__}", flush=True)
        return None


def has_term(terms: list[str], text: str) -> list[str]:
    """The phrases that appear in the text as whole words."""
    return [t for t in terms if t and re.search(rf"(?<![\w]){re.escape(t)}(?![\w])", text, re.I)]


def best_track(career: CareerProfile | None, title: str, company: str, body: str) -> tuple[Track | None, int]:
    """The target this role belongs to, and how strong the signal is. What the role is decides; a firm's name only
    breaks ties, so a famous or a listed firm is never a reason on its own."""
    if career is None:
        return None, 0
    scored = []
    consulting_title = bool(_CONSULTING_TITLE.search(title))
    for t in career.tracks:
        in_body = len(has_term(t.industry_terms + t.role_terms, body))
        s = 5 * bool(has_term(t.role_terms, title) or has_term(t.industry_terms, title)) + min(in_body, 4)
        if s and t.kind == "consulting" and consulting_title:
            s += 2
        if s and has_term(t.firms, company):
            s += 1
        scored.append((s, -t.priority, t))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    if not scored or scored[0][0] < 4:
        return None, scored[0][0] if scored else 0
    return scored[0][2], scored[0][0]


def emphasized(vec: dict[str, float], track: Track | None, factor: float = 1.25) -> dict[str, float]:
    """Weigh what this kind of role cares about more, among what the posting itself asks for. A concept the posting
    never mentions stays at zero, so the track can reorder the person's evidence but cannot change what a job is about."""
    if track is None:
        return vec
    return {c: v * factor if c in track.emphasize else v for c, v in vec.items()}

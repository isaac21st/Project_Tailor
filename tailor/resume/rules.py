"""The person's own tailoring rules, kept with their profile (<data_dir>/profile/tailoring_rules.json), never in the code.

Everything here is about one person's resume, so a new user starts with none of it and the tailoring works from the
resume and the posting alone. The file (all keys optional):

{
  "protect": ["words of a bullet that must stay on every resume"],
  "approved_rewrites": [["old text in a bullet", "new text, reviewed by the person"]],
  "reframes": [{"when": "regex on the posting", "old": "text in a bullet", "new": "the posting's own word for it"}],
  "shortenings": [{"id": "skills_short", "label": "shortened the Strategy skills line", "where": "skills",
                   "find": "long phrase, ", "replace": "", "cost": 1.5,
                   "costlier_when": "regex on the posting", "per_mention": 4, "max_mentions": 2}],
  "education_first": {"degree": "Master of Science", "label": "MS in Information", "reason": "your order"}
}

A shortening is one of the moves the one-page fitter may make (cheapest first): replace `find` with `replace` in the
bullets ("where": "bullets") or in the skills lines ("where": "skills"). `costlier_when` makes it a last resort when
the posting keeps mentioning the words it would cut.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

RULES_FILE = "tailoring_rules.json"


@dataclass
class Shortening:
    id: str
    label: str
    find: str
    replace: str = ""
    where: str = "bullets"  # "bullets" | "skills"
    cost: float = 1.5
    costlier_when: str = ""
    per_mention: float = 0.0
    max_mentions: int = 0

    def cost_for(self, job_text: str) -> float:
        if not self.costlier_when:
            return self.cost
        n = min(len(re.findall(self.costlier_when, job_text or "", re.I)), self.max_mentions or 0)
        return self.cost + self.per_mention * n


@dataclass
class TailoringRules:
    protect: tuple[str, ...] = ()
    approved_rewrites: list[tuple[str, str]] = field(default_factory=list)
    reframes: list[tuple[re.Pattern, str, str]] = field(default_factory=list)
    shortenings: list[Shortening] = field(default_factory=list)
    education_first_degree: str = ""  # words of the degree title listed first ("" keeps the resume's order)
    education_first_label: str = ""
    education_first_reason: str = ""

    def shortening(self, sid: str) -> Shortening | None:
        return next((s for s in self.shortenings if s.id == sid), None)


NO_RULES = TailoringRules()
_cache: dict[str, tuple[float, TailoringRules]] = {}
_lock = threading.Lock()


def parse_rules(raw: dict) -> TailoringRules:
    """Rules from the file's JSON; a malformed item is skipped, never fatal."""
    r = TailoringRules()
    r.protect = tuple(str(x) for x in raw.get("protect", []) if str(x).strip())
    r.approved_rewrites = [(str(a), str(b)) for a, b in (x for x in raw.get("approved_rewrites", [])
                                                         if isinstance(x, (list, tuple)) and len(x) == 2) if a]
    for x in raw.get("reframes", []):
        try:
            r.reframes.append((re.compile(x["when"], re.I), str(x["old"]), str(x["new"])))
        except (KeyError, TypeError, re.error):
            continue
    for x in raw.get("shortenings", []):
        try:
            s = Shortening(id=str(x["id"]), label=str(x.get("label") or x["id"]), find=str(x["find"]),
                           replace=str(x.get("replace", "")), where=x.get("where", "bullets"),
                           cost=float(x.get("cost", 1.5)), costlier_when=str(x.get("costlier_when", "")),
                           per_mention=float(x.get("per_mention", 0)), max_mentions=int(x.get("max_mentions", 0)))
        except (KeyError, TypeError, ValueError):
            continue
        if s.find and s.where in ("bullets", "skills"):
            r.shortenings.append(s)
    first = raw.get("education_first") or {}
    if isinstance(first, dict) and first.get("degree"):
        r.education_first_degree = str(first["degree"])
        r.education_first_label = str(first.get("label") or first["degree"])
        r.education_first_reason = str(first.get("reason") or "your order")
    return r


def load_rules(profile_dir: Path | str | None) -> TailoringRules:
    """The person's rules (re-read when the file changes); none when there is no file."""
    if profile_dir is None:
        return NO_RULES
    path = Path(profile_dir) / RULES_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return NO_RULES
    key = str(path)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            rules = parse_rules(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            rules = NO_RULES
        _cache[key] = (mtime, rules)
        return rules

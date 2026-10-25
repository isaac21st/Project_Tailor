"""Tailor the base resume to one job posting with rules (no model): choose, order and trim what is already true.

What it may do
  * pick which projects to show and in what order, order the skills lines, put the MS or the MBA first;
  * apply a short list of reviewed re-wordings, and re-word one bullet when the posting uses its own term for
    something the person really did ("primary research" for expert interviews);
  * drop the least relevant bullets and skills, cheapest loss first, until the page fits, then put back whatever
    still fits.
What it may not do: add a claim, number, tool or entry. The result must pass check_tailored, or nothing is returned.

How relevance works: the posting is read for a small set of concepts (user research, design, product, data, strategy,
AI, engineering, ...), weighted by where they appear (required > responsibilities > preferred > everything else). Every
entry, bullet and skills line is read for the same concepts. Relevance is the overlap. Nothing here is a black box: the
report says what the posting emphasised, what was kept, what was cut, and what the posting wants that the resume barely
shows.
"""
from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings, apply_environment, load_settings
from ..jobs import clean_job_text, extract_facts
from ..jobs.text import CleanJob
from .keywords import VOCAB, Keywords, _title_hit, extract_keywords, headline_title, past, resume_text, score
from .render import MARGINS, PAGE_HEIGHT_IN, PX_PER_IN, _FONT_FACES, _MEASURE_JS, build_html, render_resume
from .rewrite import check_bullet, qualifications, rewrite_resume
from .rules import NO_RULES, TailoringRules, load_rules
from .schema import Entry, LabeledLine, Resume, Section
from .validate import check_tailored, errors
from . import wordings

MAX_PROJECTS = 3
MIN_PROJECT_BULLETS = 2
MIN_JOB_CHARS = 500
MIN_JOB_SIGNAL = 8.0
# Bullets the person wants on every resume, re-wordings they reviewed, posting-driven re-framings, shortenings and the
# degree they list first: all theirs, so they live in <profile>/tailoring_rules.json (rules.py), never in this code.
DEFAULT_PROTECT: tuple[str, ...] = ()
_RULES: contextvars.ContextVar[TailoringRules] = contextvars.ContextVar("tailoring_rules", default=NO_RULES)


def active_rules() -> TailoringRules:
    """The rules of the tailoring run in progress (none outside one)."""
    return _RULES.get()


def _p(*alts: str) -> re.Pattern:
    return re.compile("|".join(alts), re.I)


CONCEPTS: dict[str, re.Pattern] = {
    "ux_research": _p(r"user research", r"ux research", r"usability", r"user interview", r"interviews? with", r"expert interview",
                      r"virtual interview", r"personas?", r"human[- ]centered", r"user[- ]centered", r"\bhci\b", r"field research",
                      r"ethnograph", r"user needs", r"user experience research", r"\bresearchers?\b"),
    "design": _p(r"product design", r"ux design", r"ui design", r"interaction design", r"prototyp", r"wireframe", r"\bfigma\b",
                 r"design system", r"visual design", r"design thinking", r"\bdesigners?\b", r"\bdesigning\b", r"user experience"),
    "product": _p(r"product manag", r"product[- ]strateg", r"roadmap", r"product requirement", r"user stor", r"go-to-market",
                  r"product owner", r"\bokrs?\b", r"product (?:development|launch|lifecycle)", r"\bfeatures?\b", r"\bonboarding\b"),
    "data": _p(r"data analy", r"analytics", r"\bsql\b", r"\bpython\b", r"statistic", r"dashboard", r"visuali[sz]", r"data scien",
               r"machine learning", r"data engineer", r"business intelligence", r"\bmetrics?\b", r"a/b test", r"quantitative",
               r"\bdata(?:sets?)?\b", r"\bjupyter\b", r"\bpandas\b"),
    "strategy": _p(r"strateg", r"consult", r"\bengagements?\b", r"market (?:research|sizing|entry|analysis)", r"competitive",
                   r"business case", r"operating[- ]model", r"due diligence", r"transformation", r"recommendation"),
    "ai": _p(r"\bai\b", r"artificial intelligence", r"machine learning", r"generative", r"\bllms?\b", r"\bprompt", r"copilot",
             r"agentic", r"\bgemini\b", r"\bclaude\b"),
    "engineering": _p(r"software", r"engineer", r"\bhtml\b", r"\bcss\b", r"javascript", r"front[- ]?end", r"\bcoding\b", r"\bapis?\b",
                      r"\bcloud\b", r"architecture", r"developers?\b", r"web technolog", r"\bbuil(?:d|t|ding)\b"),
    "stakeholder": _p(r"stakeholder", r"executive", r"leadership", r"senior management", r"cross-functional", r"presentation",
                      r"\bpresent(?:ed|ing)?\b", r"\bclient"),
    "operations": _p(r"\boperations?\b", r"\bprocess", r"workflow", r"supply chain", r"efficien", r"implementation",
                     r"project manag", r"bottleneck"),
    "healthcare": _p(r"\bhealth", r"clinical", r"patient", r"\bcare\b"),
    "finance": _p(r"financ", r"invest", r"fintech", r"banking", r"pricing", r"revenue"),
    "education": _p(r"education", r"\bstudents?\b", r"\blearning\b"),
    "customer": _p(r"customer", r"\bsales\b", r"retail", r"consumer", r"\bservice"),
}
_KIND_WEIGHT = {"required": 3.0, "preferred": 2.0, "responsibilities": 2.0, "skills": 2.0, "summary": 1.0, "other": 1.0,
                "boilerplate": 0.0}

_TECH_DEGREE = re.compile(r"computer science|engineering|information (?:systems|science)|data science|statistics|mathematics|"
                          r"\bstem\b|technical (?:field|degree)|human[- ]computer", re.I)
_BIZ_DEGREE = re.compile(r"degree[^.\n]{0,140}\bbusiness\b|\bbusiness\b[^.\n]{0,60}degree|\bmba\b|business administration|"
                         r"management consulting|major[^.\n]{0,100}\b(?:finance|economics|accounting)\b", re.I)

# Leading verbs a bullet may trade for the posting's own verb, because they mean the same thing for that bullet (the
# guide: "lead with action verbs from the JD"). Never a bigger verb: check_bullet refuses led/managed/owned additions.
VERB_GROUPS: list[set[str]] = [
    {"built", "developed", "created"}, {"analyzed", "evaluated", "assessed"}, {"led", "drove", "spearheaded"},
    {"identified", "uncovered", "surfaced"}, {"synthesized", "consolidated"}, {"presented", "communicated"},
    {"conducted", "performed"}, {"generated", "drove"}, {"co-developed", "co-created"}, {"turned", "translated", "transformed"},
]
_ADVERB = re.compile(r"^(Independently|Jointly|Collaboratively)\s+", re.I)


def mirror_verb(bullet: str, verbs_past: list[str], protect: tuple[str, ...] = ()) -> str | None:
    """The bullet led by the posting's verb instead of a same-meaning one ("Built" -> "Developed" when the posting says
    "Develop"), or None. The first verb the posting uses most wins; the result must pass check_bullet."""
    m = re.match(r"^(\**)((?:Independently|Jointly|Collaboratively)\s+)?([A-Za-z-]+)", bullet)
    if not m:
        return None
    word = m.group(3).lower()
    if word in verbs_past:
        return None
    for v in verbs_past:
        if any(word in g and v in g for g in VERB_GROUPS) and v != word:
            new_word = v if m.group(2) else v.capitalize()
            cand = bullet[:m.start(3)] + new_word + bullet[m.end(3):]
            return cand if check_bullet(bullet, cand, protect) is None else None
    return None


# ----------------------------------------------------------------------------------------------- phrase bank
# profile/phrase_bank.json: for a bullet, other wordings of the same facts, each leaning toward a kind of qualification.
# Only variants the person approved are used, and each must still pass check_bullet against the original.

def load_proposals(settings: Settings, protect: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """original bullet -> its wordings that wait for the person's approval (never used, only pointed out)."""
    path = settings.profile_dir / "phrase_bank.json"
    try:
        bullets = json.loads(path.read_text(encoding="utf-8")).get("bullets", [])
    except (OSError, ValueError):
        return {}
    out: dict[str, list[str]] = {}
    for b in bullets:
        waiting = [v["text"] for v in b.get("variants", []) if v.get("status") == "proposed"
                   and check_bullet(b["original"], v["text"], protect) is None]
        if waiting:
            out[b["original"]] = waiting + [v["text"] for v in b.get("variants", []) if v.get("status") == "approved"]
    return out


def better_proposals(final: Resume, kw: Keywords, settings: Settings, protect: tuple[str, ...] = ()) -> int:
    """How many bullets on the page have a proposed wording that matches the posting's keywords clearly better."""
    n = 0
    shown = [b for s in final.sections for e in s.entries for r in e.roles for b in r.bullets]
    for original, texts in load_proposals(settings, protect).items():
        proposed = texts  # proposed first, then the approved ones: the bullet on the page is one of these or the original
        here = next((b for b in shown if b == original or b in proposed or b[:40] in (original[:40], *[t[:40] for t in proposed])), None)
        if here is None:
            continue
        if any(_kw_hits(kw, t) > _kw_hits(kw, here) + 0.9 for t in proposed if t != here):
            n += 1
    return n


def load_bank(settings: Settings, protect: tuple[str, ...] | None = None) -> dict[str, list[tuple[str, str]]]:
    """original bullet -> [(angle, approved text), ...] (`protect`: the person's protected words, from their rules)"""
    if protect is None:
        protect = load_rules(settings.profile_dir).protect
    path = settings.profile_dir / "phrase_bank.json"
    if not path.exists():
        return {}
    bank: dict[str, list[tuple[str, str]]] = {}
    for b in json.loads(path.read_text(encoding="utf-8")).get("bullets", []):
        ok = [(v.get("angle", ""), v["text"]) for v in b.get("variants", [])
              if v.get("status") == "approved" and check_bullet(b["original"], v["text"], protect) is None]
        if ok:
            bank[b["original"]] = ok
    return bank


_BANK_STOP = set("""about across also and are been being both but can for from have into more most must other over such
than that the their them these they this through using what when where which while will with within work your you
ability experience including related relevant strong""".split())


def _words(text: str) -> set[str]:
    return {w.lower()[:5] for w in re.findall(r"[A-Za-z][A-Za-z-]{2,}", text) if w.lower() not in _BANK_STOP}


def _kw_hits(kw: Keywords | None, text: str) -> float:
    """How much of the posting's keyword weight a wording shows (hard skills most), and whether it leads with one of
    the posting's verbs: what an ATS scores."""
    if kw is None:
        return 0.0
    plain = text.replace("*", "")
    got = sum(k.weight for k in kw.items if k.kind in ("hard", "industry", "soft") and k.found_in(plain))
    first = re.sub(r"^(?:Independently|Jointly)\s+", "", plain).split(" ", 1)[0].lower()
    return got + (1.5 if first in {past(v) for v in kw.verbs()} else 0.0)


def _pick(options: list[tuple[str, str]], vec: dict[str, float], want: set[str], ask_vec: dict[str, float],
          ask: set[str], kw: Keywords | None = None) -> tuple[str, str]:
    """The wording that best matches the person's request (from the chat), then the posting (its keywords, its kinds of
    work and its own words). A variant whose angle the request names ("lean on AI") counts most."""
    def score(angle: str, text: str) -> float:
        w = _words(text)
        posting = _relevance(vec, text) + 0.6 * len(w & want) + 1.2 * _kw_hits(kw, text)
        request = _relevance(ask_vec, text) + len(w & ask) + (5.0 if angle and _words(angle) & ask else 0.0)
        return 10.0 * request + posting
    best = options[0]
    for opt in options[1:]:
        if score(*opt) > score(*best) + 0.25:  # the original wins ties
            best = opt
    return best


# ----------------------------------------------------------------------------------------------- results

@dataclass
class TailorReport:
    applied: bool
    reason: str = ""
    concepts: dict[str, float] = field(default_factory=dict)
    education_first: str = ""
    education_reason: str = ""
    projects: list[tuple[str, float]] = field(default_factory=list)
    dropped_projects: list[tuple[str, float]] = field(default_factory=list)
    skills_order: list[str] = field(default_factory=list)
    trimmed: list[str] = field(default_factory=list)
    rewrites: list[str] = field(default_factory=list)
    hard: bool = False  # the model re-worded bullets in the posting's language
    refused: list[str] = field(default_factory=list)  # re-wordings the bullet check turned down
    gaps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ats_before: int = 0  # Jobscan-style keyword match of the base resume (the guide's step 3)
    ats_after: int = 0  # ... and of the tailored one
    keywords_added: list[str] = field(default_factory=list)  # posting terms added to skills lines (evidence-backed)
    missing_keywords: list[str] = field(default_factory=list)  # hard skills the posting wants that the resume lacks
    summary_line: str = ""
    better_proposals: int = 0  # bullets with a proposed (not yet approved) wording that fits this posting better
    ai_keywords: list[str] = field(default_factory=list)  # keywords AI wordings put into bullets on the page
    in_bullets: list[str] = field(default_factory=list)  # the posting's skills and industry terms said in bullets, in context
    allowed_terms: list[str] = field(default_factory=list)  # what the truth check let in: the added skills
    summary_terms: list[str] = field(default_factory=list)  # ... and the words only the summary line may use
    pages: int = 0
    spare_px: float = 0.0
    fits: bool = False
    job_id: str = ""

    def top_concepts(self, n: int = 5) -> list[str]:
        return [c for c, v in sorted(self.concepts.items(), key=lambda kv: -kv[1])[:n] if v > 0]

    def summary(self) -> str:
        if not self.applied:
            return f"Not tailored: {self.reason}"
        bits = [f"{self.education_first} first ({self.education_reason})",
                "projects: " + ", ".join(_short(t) for t, _ in self.projects),
                "skills led by " + (self.skills_order[0] if self.skills_order else "-")]
        n = sum(1 for r in self.rewrites if "' -> '" in r or "wording: '" in r or "verb: '" in r)
        if n:
            bits.append(f"{n} bullet{'s' if n != 1 else ''} worded for this posting")
        if self.ai_keywords:
            bits.append("AI wording put in bullets: " + ", ".join(k.lower() if not k[1:2].isupper() else k
                                                                  for k in self.ai_keywords))
        if self.keywords_added:
            bits.append("skills added from the posting: " + ", ".join(self.keywords_added))
        if self.missing_keywords:
            bits.append("missing (add only if true): " + ", ".join(self.missing_keywords[:6]))
        if self.better_proposals:
            bits.append(f"{self.better_proposals} proposed wording{'s' if self.better_proposals != 1 else ''} would fit "
                        "this posting better: approve them on the Desk (Wordings)")
        if self.trimmed:
            bits.append("trimmed: " + "; ".join(self.trimmed))
        ats = f"ATS match {self.ats_before}% -> {self.ats_after}%. " if self.ats_after else ""
        return ats + "Tailored to this posting (" + ", ".join(self.top_concepts(3)) + "): " + " | ".join(bits)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied, "reason": self.reason, "job_id": self.job_id, "summary": self.summary(),
            "emphasis": self.top_concepts(6), "education_first": self.education_first,
            "education_reason": self.education_reason, "projects": [t for t, _ in self.projects],
            "dropped_projects": [t for t, _ in self.dropped_projects], "skills_order": self.skills_order,
            "trimmed": self.trimmed, "rewrites": self.rewrites, "hard": self.hard, "refused": self.refused, "gaps": self.gaps, "warnings": self.warnings,
            "ats_before": self.ats_before, "ats_after": self.ats_after, "keywords_added": self.keywords_added,
            "missing_keywords": self.missing_keywords, "summary_line": self.summary_line,
            "better_proposals": self.better_proposals, "ai_keywords": self.ai_keywords, "in_bullets": self.in_bullets,
            "pages": self.pages, "fits": self.fits,
        }


@dataclass
class TailorResult:
    resume: Resume | None
    report: TailorReport
    pdf_path: Path | None = None


class TailorError(RuntimeError):
    pass


def _short(title: str) -> str:
    return re.split(r" \(| & | \| ", title)[0].strip().title()


# ----------------------------------------------------------------------------------------------- relevance

def _counts(text: str) -> dict[str, int]:
    return {c: len(p.findall(text)) for c, p in CONCEPTS.items()}


def job_concepts(job: CleanJob, title: str = "") -> tuple[dict[str, float], float]:
    """(concept -> weight, raw signal). Weight grows with the square root of the weighted mentions."""
    raw = {c: 0.0 for c in CONCEPTS}
    for block in job.blocks:
        w = _KIND_WEIGHT.get(block.kind, 1.0)
        if not w:
            continue
        for c, n in _counts(block.text).items():
            raw[c] += w * min(n, 4)
    for c, n in _counts(title).items():
        raw[c] += 4.0 * min(n, 2)
    return {c: math.sqrt(v) for c, v in raw.items()}, sum(raw.values())


def _evidence(text: str) -> dict[str, float]:
    return {c: math.sqrt(n) for c, n in _counts(text).items() if n}


def _relevance(vec: dict[str, float], text: str) -> float:
    return sum(vec.get(c, 0.0) * w for c, w in _evidence(text).items())


def _split_skills(text: str) -> list[str]:
    """Skills on a line, split on commas outside parentheses ("Claude Code (product prototyping), Gemini (...)")."""
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


def _named_in(item: str, posting_lower: str) -> int:
    """1 when the posting names this skill ("SQL", "Figma"): the word before any parenthesis, as a whole word."""
    core = re.sub(r"\(.*?\)", "", item).strip().lower()
    return int(bool(core) and re.search(rf"(?<![a-z]){re.escape(core)}(?![a-z])", posting_lower) is not None)


def _proper(item: str) -> bool:
    """A tool, acronym or name keeps its capitals ("SQL", "HubSpot", "Excel", "Claude Code"); a plain phrase does not."""
    first = item.split()[0] if item.split() else ""
    return (any(c.isupper() for c in first[1:]) or first in _TOOLS or first.rstrip(",") in {"Excel", "Python", "Figma",
            "Framer", "Qualtrics", "Jupyter", "Pandas", "Matplotlib", "Claude", "Gemini", "Generative", "Microsoft"})


def _cap(item: str) -> str:
    return item[:1].upper() + item[1:]


def _entry_text(e: Entry) -> str:
    return " ".join([e.title] + [r.title for r in e.roles] + [b for r in e.roles for b in r.bullets])


# ----------------------------------------------------------------------------------------------- sections

def _edu(r: Resume) -> Section | None:
    return next((s for s in r.sections if s.heading.upper().startswith("EDUCATION")), None)


def _pro(r: Resume) -> Section | None:
    return next((s for s in r.sections if s.heading.upper().startswith("PROFESSIONAL")), None)


def _proj(r: Resume) -> Section | None:
    return next((s for s in r.sections if "PROJECT" in s.heading.upper()), None)


def _skills(r: Resume) -> Section | None:
    return next((s for s in r.sections if s.lines), None)


# ----------------------------------------------------------------------------------------------- preparing

def _rewritten(text: str) -> str:
    for old, new in active_rules().approved_rewrites:
        text = text.replace(old, new)
    return text


def _reframed(text: str, reframes: list[tuple[str, str]]) -> str:
    for old, new in reframes:
        text = text.replace(old, new)
    return text


def prepare(base: Resume, job_text: str, *, rewrites: bool = True, bank: dict | None = None,
            vec: dict[str, float] | None = None, instruction: str = "", kw: Keywords | None = None,
            protect: tuple[str, ...] = DEFAULT_PROTECT, ai: dict | None = None,
            woven: list[str] | None = None) -> tuple[Resume, list[str]]:
    """A copy of the base with the reviewed re-wordings (and any posting-driven re-framing) applied. With a phrase bank,
    each bullet takes whichever approved wording best matches the posting and the person's instruction. `ai`: the AI
    keyword wordings (wordings.usable); one competes only when the posting asks for its keyword, and each keyword is
    woven into one bullet at most. The keywords used are appended to `woven`."""
    r = copy.deepcopy(base)
    done: list[str] = []
    if not rewrites:
        return r, done
    reframes = [(old, new) for pat, old, new in active_rules().reframes if pat.search(job_text or "")]
    asked = {k.term.name for k in (kw.items if kw else []) if k.term is not None and k.kind in ("hard", "industry", "soft")}
    used_ai: set[str] = set()
    want = set()
    if bank or ai:
        job = clean_job_text(job_text or "")
        want = _words(" ".join(qualifications(job, limit=20)))
    ask_vec = _evidence(instruction) if instruction else {}
    ask = _words(instruction) if instruction else set()
    verbs_past = [past(k.verb) for k in sorted((k for k in (kw.items if kw else []) if k.kind == "verb"), key=lambda k: -k.weight)]
    for s in r.sections:
        if s.heading.upper().startswith("EDUCATION"):
            continue
        for e in s.entries:
            for role in e.roles:
                for i, b in enumerate(role.bullets):
                    chosen_variant = False
                    ai_opts = [(a, t) for a, t in (ai or {}).get(b, []) if a[4:] in asked and a[4:] not in used_ai]
                    if (bank and b in bank) or ai_opts:
                        # the wordings are compared as they would be shown: the original with the reviewed re-wordings,
                        # and each with the posting's own terms (an AI "market research" wording must not beat the
                        # original's "grounded in primary research" re-frame unseen)
                        opts = [("", b)] + (bank or {}).get(b, [])
                        shown_as = {t: _reframed(_rewritten(t) if not a else t, reframes) for a, t in opts + ai_opts}
                        # an AI wording must add keyword weight over the person's own wordings, not only tie with them
                        own_best = max(_kw_hits(kw, shown_as[t]) for _, t in opts)
                        opts += [(a, t) for a, t in ai_opts if _kw_hits(kw, shown_as[t]) > own_best + 0.5]
                        angle, chosen = _pick([(a, shown_as[t]) for a, t in opts], vec or {}, want, ask_vec, ask, kw)
                        if angle:
                            raw = next(t for a, t in opts if a == angle and shown_as[t] == chosen)
                            done.append(f"{_short(e.title)}: used the '{angle}' wording: '{raw}'")
                            b, chosen_variant = raw, True
                            if angle.startswith("AI: "):
                                used_ai.add(angle[4:])
                                if woven is not None:
                                    woven.append(angle[4:])
                    if not chosen_variant:
                        b = _rewritten(b)
                    for old, new in reframes:  # the posting's own term, on whichever wording was chosen
                        if old in b:
                            b = b.replace(old, new)
                            done.append(f"{_short(e.title)}: '{old}' -> '{new}' (the posting's own term)")
                    if verbs_past and not any(m in b for m in protect):
                        mirrored = mirror_verb(b, verbs_past, protect)
                        if mirrored:
                            done.append(f"{_short(e.title)}: led with the posting's verb: '{mirrored}'")
                            b = mirrored
                    role.bullets[i] = b
    return r, done


# ----------------------------------------------------------------------------------------------- keywords (the guide)

MAX_ADDED_SKILLS = 6


def add_keywords(prepared: Resume, base: Resume, kw: Keywords, order: list[str] | None = None,
                 confirmed: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """The guide's skills step: every posting keyword the person really has, in the posting's words. A term is added to
    a skills line only when the resume does not say it yet AND either the base resume shows it (the term's evidence:
    expert interviews for "Primary research", Matplotlib for "Data visualization") or the person confirmed it
    ("I have this" in the panel; profile_overrides "confirmed_skills"). `order`: the skills lines best first, so a term
    joins a line that will stay on the page. Returns [(line label, term)]."""
    sk = _skills(prepared)
    if not sk:
        return []
    base_txt = resume_text(base)
    now = resume_text(prepared)
    mine = {c.casefold() for c in confirmed}
    by_label = {ln.label.lower(): ln for ln in sk.lines}
    keep_lines = [lab for lab in (order or []) if lab.strip().lower() != "awards"][:3]
    added: list[tuple[str, str]] = []
    per_line: dict[str, int] = {}
    for k in sorted(kw.items, key=lambda k: -k.weight):
        if len(added) >= MAX_ADDED_SKILLS or k.kind != "hard":
            continue
        t = k.term
        if k.found_in(now):
            continue
        on_resume = t is not None and t.ev() is not None and t.ev().search(base_txt)
        if not on_resume and k.name.casefold() in mine:
            # the person's own word for a skill the resume does not show (A/B testing from the M-Bus project,
            # 2026-10-09): it goes on an "Additional" skills line, made when the first one is needed
            ln = by_label.get("additional")
            if ln is None:
                ln = LabeledLine(label="Additional", text="")
                sk.lines.append(ln)
                by_label["additional"] = ln
            ln.text = ", ".join([x for x in _split_skills(ln.text) if x] + [k.name])
            per_line[ln.label] = per_line.get(ln.label, 0) + 1
            added.append((ln.label, k.name))
            now = resume_text(prepared)
            continue
        if not on_resume:
            continue
        tool = t is None or not t.line and (k.name.isupper() or any(c.isupper() for c in k.name[1:]) or k.name in _TOOLS)
        want = t.line if t is not None and t.line else ("Technology" if tool else "Consulting & Strategy")
        # the term's own line, unless it is likely to be trimmed: then the best kept line of the same kind
        fallback = [want] + (["Technology", "Analytics", "AI Tools"] if tool else ["Consulting & Strategy", "Analytics", "UX Research"])
        ln = next((by_label[l.lower()] for l in fallback if l.lower() in by_label and (not keep_lines or by_label[l.lower()].label in keep_lines)), None) \
            or next((by_label[l.lower()] for l in keep_lines if l.lower() in by_label), None) or by_label.get(want.lower()) or sk.lines[0]
        if per_line.get(ln.label, 0) >= 3 or ln.label.strip().lower() == "awards":
            continue
        items = _split_skills(ln.text)
        pos = per_line.get(ln.label, 0)  # after the terms already added here: the most important posting term leads
        ln.text = ", ".join(items[:pos] + [k.name] + items[pos:])
        per_line[ln.label] = per_line.get(ln.label, 0) + 1
        added.append((ln.label, k.name))
        now = resume_text(prepared)
    return added


def _degree_phrase(base: Resume) -> tuple[str, str]:
    """("an MS in Information and an MBA at the University of Michigan", "May 2027") from the base resume's education:
    the degrees in progress (or the latest one), as a recruiter would shorten them."""
    edu = _edu(base)
    if not edu:
        return "", ""
    current, grad, school = [], "", ""
    for e in edu.entries:
        for role in e.roles:
            if "expected" not in role.dates.lower():
                continue
            t = role.title
            short = "MBA" if "business administration" in t.lower() else \
                re.sub(r"^Master of Science in ([^,]+).*$", r"MS in \1", t) if t.startswith("Master of Science") else \
                re.sub(r"^Master of Arts in ([^,]+).*$", r"MA in \1", t) if t.startswith("Master of Arts") else \
                re.sub(r"^Bachelor of (?:Arts|Science) in ([^,]+).*$", r"bachelor's in \1", t)
            current.append(("an " if short[:1] in "AEIMO" else "a ") + short)
            grad = grad or re.sub(r"(?i)^expected\s+", "", role.dates).strip()
            school = school or re.split(r",| School of| College of", e.title)[0].strip()
    if not current:
        return "", ""
    joined = " and ".join(current) if len(current) <= 2 else ", ".join(current[:-1]) + " and " + current[-1]
    return f"{joined} at the {school}" if not school.lower().startswith("the ") else f"{joined} at {school}", grad


SUMMARY_MAX_CHARS = 118  # one line at 10.5pt: a second line costs as much as a project bullet (measured 2026-10-06)


def build_summary(base: Resume, kw: Keywords, text_now: str) -> tuple[str | None, list[str]]:
    """The guide's summary, in one line: the posting's role title and its 2-3 highest-priority keywords, all of which
    the resume already shows ("Associate Product Manager candidate with hands-on experience in product strategy, data
    analysis and user research."). The degrees are in Education, right below. Returns (summary, the capitalized words
    it may use that the base resume does not have)."""
    title = headline_title(kw.title) if kw.title else ""
    if not title or not _edu(base):
        return None, []
    picks = []
    for k in sorted(kw.items, key=lambda k: -k.weight):
        # "HTML/CSS" only when the resume shows each part (an HTML tool is not CSS: Palantir, 2026-10-09)
        parts_shown = all(re.search(rf"\b{re.escape(x)}\b", text_now, re.I) for x in k.name.split("/")) if "/" in k.name else True
        if k.kind == "hard" and k.term is not None and k.found_in(text_now) and parts_shown and k.name not in picks:
            picks.append(k.name)
        if len(picks) == 3:
            break
    if len(picks) < 2:
        # a posting that names few skills (Amazon's MBA program): the role family's usual ones, if the resume shows them
        for fam, terms in _FAMILY_TERMS:
            if fam.search(title):
                for name in terms:
                    k = next((t for t in VOCAB if t.name == name), None)
                    shown = k is not None and (k.on_resume().search(text_now) or (k.ev() is not None and k.ev().search(text_now)))
                    if shown and name not in picks:
                        picks.append(name)
                    if len(picks) == 3:
                        break
                break
    if len(picks) < 2:
        return None, []
    names = [p if (p.isupper() or any(c.isupper() for c in p[1:]) or p in _TOOLS) else p[0].lower() + p[1:] for p in picks]
    text = ""
    for n, lead in ((3, "with hands-on experience in"), (2, "with hands-on experience in"), (3, "experienced in"), (2, "experienced in")):
        part = names[:n]
        skills = part[0] if len(part) == 1 else ", ".join(part[:-1]) + " and " + part[-1]
        text = f"{title} candidate {lead} {skills}."
        if len(text) <= SUMMARY_MAX_CHARS:
            break
    if len(text) > SUMMARY_MAX_CHARS:
        return None, []
    extra = re.findall(r"\b[A-Z][A-Za-z&/-]*\b", title) + [w for n in picks for w in re.findall(r"[A-Za-z]+", n)]
    return text, extra


_TOOLS = {"Excel", "PowerPoint", "Python", "SQL", "Figma", "Tableau", "JavaScript", "CRM", "HTML/CSS", "Jira", "Qualtrics"}
_FAMILY_TERMS = [
    (re.compile(r"product manag|\bapm\b|product owner", re.I), ["Product strategy", "User research", "Data analysis"]),
    (re.compile(r"design", re.I), ["Prototyping", "User research", "Usability testing"]),
    (re.compile(r"research", re.I), ["User research", "Qualitative research", "Usability testing"]),
    (re.compile(r"consult|analyst|associate|strateg|advis", re.I), ["Market research", "Data analysis", "Strategic recommendations"]),
]


# ----------------------------------------------------------------------------------------------- selection

@dataclass
class Selection:
    projects: list[str]  # titles to show, best first
    dropped: list[tuple[str, float]]
    project_scores: dict[str, float]
    skills_order: list[str]
    edu_first: str
    edu_reason: str
    vec: dict[str, float] = field(default_factory=dict)  # what the posting is about: orders bullets within an entry
    job_text: str = ""  # the posting's words: skills it names lead their line


def _choose(prepared: Resume, vec: dict[str, float], job: CleanJob) -> Selection:
    proj = _proj(prepared)
    scored = []
    for i, e in enumerate(proj.entries if proj else []):
        scored.append((_relevance(vec, _entry_text(e)) - i * 0.001, e.title))
    scored.sort(key=lambda x: -x[0])
    keep = [t for _, t in scored[:MAX_PROJECTS]]
    dropped = [(t, round(s, 2)) for s, t in scored[MAX_PROJECTS:]]
    scores = {t: round(s, 2) for s, t in scored}

    sk = _skills(prepared)
    lines = []
    for i, ln in enumerate(sk.lines if sk else []):
        score = _relevance(vec, f"{ln.label} {ln.text}") - i * 0.001
        if ln.label.strip().lower() == "awards":
            score = -1.0  # never leads
        lines.append((score, ln.label))
    lines.sort(key=lambda x: -x[0])

    # the degree the person always lists first, if they set one (rules.py); otherwise the resume's own order
    rules = active_rules()
    first, why = rules.education_first_label, rules.education_first_reason
    return Selection(keep, dropped, scores, [label for _, label in lines], first, why, vec=vec, job_text=job.body_text())


# ----------------------------------------------------------------------------------------------- steps and building

@dataclass
class Step:
    cost: float
    kind: str  # skill | bullet | shorten | project
    where: str  # skills label, entry title, or shorten key
    text: str = ""  # the bullet text for kind == "bullet"
    label: str = ""


def _redundancy(line_text: str, kept_text: str) -> float:
    """Share of a skills line's items that the kept bullets already show (so cutting the line loses little)."""
    items = [i.strip() for i in re.split(r",(?![^()]*\))", line_text) if i.strip()]
    if not items:
        return 0.0
    low = kept_text.lower()
    covered = 0
    for item in items:
        words = [w for w in re.findall(r"[a-z]+", item.lower()) if len(w) >= 5]
        if words and any(w in low for w in words):
            covered += 1
    return covered / len(items)


def _make_steps(prepared: Resume, sel: Selection, vec: dict[str, float], job_text: str, protect: tuple[str, ...],
                added: list[tuple[str, str]] = (), kw: Keywords | None = None, kw_weight: float = 0.8) -> list[Step]:
    steps: list[Step] = []
    if prepared.summary:
        # the summary mirrors the role title, which an ATS scores heavily: it goes after any project bullet that carries
        # fewer of the posting's keywords, before a professional bullet or a whole project. What it carries counts: the
        # title (when nothing else on the page says it) and its keywords.
        carries = _kw_hits(kw, prepared.summary)
        title = next((k for k in (kw.items if kw else []) if k.kind == "title"), None)
        if title is not None:
            rest = resume_text(prepared.model_copy(update={"summary": None}))
            carries += title.weight * (1.0 - _title_hit(title.name, rest))
        steps.append(Step(19.0 + kw_weight * carries / 3.0, "summary", "summary", label="dropped the summary line"))
    sk = _skills(prepared)
    kept_entries = [e for e in (_proj(prepared).entries if _proj(prepared) else []) if e.title in sel.projects]
    kept_entries += list(_pro(prepared).entries if _pro(prepared) else [])
    kept_text = " ".join(_entry_text(e) for e in kept_entries)
    for ln in (sk.lines if sk else []):
        score = max(_relevance(vec, f"{ln.label} {ln.text}"), 0.0)
        cost = 0.5 if ln.label.strip().lower() == "awards" else 2.0 + score - 3.0 * _redundancy(ln.text, kept_text)
        cost += 3.0 * sum(1 for lab, _ in added if lab == ln.label)  # it carries the posting's own keywords
        cost += kw_weight * _kw_hits(kw, f"{ln.label} {ln.text}")  # ... or names them already ("SQL", "UX Research")
        steps.append(Step(max(cost, 0.8), "skill", ln.label, label=f"dropped the '{ln.label}' skills line"))
    for sh in active_rules().shortenings:  # the person's own shortenings (rules.py)
        steps.append(Step(sh.cost_for(job_text), "shorten", sh.id, label=sh.label))
    proj = _proj(prepared)
    for e in (proj.entries if proj else []):
        if e.title not in sel.projects:
            continue
        for role in e.roles:
            for b in role.bullets:
                if any(m in b for m in protect):
                    continue
                steps.append(Step(2.0 + _relevance(vec, b) + kw_weight * _kw_hits(kw, b), "bullet", e.title, b,
                                  f"cut a bullet from {_short(e.title)}"))
        steps.append(Step(40.0 + sel.project_scores.get(e.title, 0.0) + kw_weight * _kw_hits(kw, _entry_text(e)), "project",
                          e.title, label=f"dropped {_short(e.title)}"))
    pro = _pro(prepared)
    for e in (pro.entries if pro else []):
        for role in e.roles:
            for b in role.bullets:
                if any(m in b for m in protect):
                    continue
                steps.append(Step(20.0 + _relevance(vec, b) + kw_weight * _kw_hits(kw, b), "bullet", e.title, b,
                                  f"cut a bullet from {_short(e.title)}"))
    steps.sort(key=lambda s: s.cost)
    return steps


def _find_entry(r: Resume, title: str) -> Entry | None:
    for s in r.sections:
        for e in s.entries:
            if e.title == title:
                return e
    return None


def _build(prepared: Resume, sel: Selection, steps: list[Step]) -> Resume:
    r = copy.deepcopy(prepared)
    if any(s.kind == "summary" for s in steps):
        r.summary = None
    proj = _proj(r)
    if proj:
        by = {e.title: e for e in proj.entries}
        gone = {s.where for s in steps if s.kind == "project"}
        proj.entries = [by[t] for t in sel.projects if t in by and t not in gone]
    edu = _edu(r)
    want = active_rules().education_first_degree
    if edu and want:
        first = [e for e in edu.entries if any(want in role.title for role in e.roles)]
        last = [e for e in edu.entries if any("Bachelor" in role.title for role in e.roles)]
        rest = [e for e in edu.entries if e not in first and e not in last]
        if first:
            edu.entries = first + rest + last  # the bachelor's degree always goes last
    sk = _skills(r)
    if sk:
        by = {ln.label: ln for ln in sk.lines}
        dropped: list[str] = []
        for s in steps:  # steps are in cost order; always keep at least three skills lines
            if s.kind == "skill" and len(sel.skills_order) - len(dropped) > 3:
                dropped.append(s.where)
        sk.lines = [by[l] for l in sel.skills_order if l in by and l not in dropped]
        # within a line, the skills the posting names come first ("SQL, Python" for a data-heavy posting)
        low = sel.job_text.lower()
        for ln in sk.lines:
            items = _split_skills(ln.text)
            if len(items) > 1:
                sentence = any(i[:1].islower() for i in items[1:])  # "Market research, industry trend ..." style
                ranked = sorted(enumerate(items), key=lambda t: (-_named_in(t[1], low), -_relevance(sel.vec, t[1]), t[0]))
                items = [x for _, x in ranked]
                if sentence:  # keep the line's own style: first item capitalized, plain phrases after it in lower case
                    items = [_cap(items[0])] + [x if _proper(x) else x[:1].lower() + x[1:] for x in items[1:]]
                ln.text = ", ".join(items)
    # within each job and project, the bullets that match the posting most come first (same words, new order)
    for sec in r.sections:
        if sec.heading.upper().startswith(("EDUCATION", "SKILLS")):
            continue
        for e in sec.entries:
            for role in e.roles:
                if len(role.bullets) > 1:
                    order = sorted(enumerate(role.bullets), key=lambda t: (-_relevance(sel.vec, t[1]), t[0]))
                    role.bullets = [b for _, b in order]
        for s in steps:
            sh = active_rules().shortening(s.where) if s.kind == "shorten" else None
            if sh and sh.where == "skills":
                for ln in sk.lines:
                    ln.text = ln.text.replace(sh.find, sh.replace)
    for s in steps:
        sh = active_rules().shortening(s.where) if s.kind == "shorten" else None
        if sh and sh.where == "bullets":
            for sec in r.sections:
                for e in sec.entries:
                    for role in e.roles:
                        role.bullets = [b.replace(sh.find, sh.replace) for b in role.bullets]
        elif s.kind == "bullet":
            e = _find_entry(r, s.where)
            if not e:
                continue
            is_project = bool(proj and e in proj.entries)
            for role in e.roles:
                floor = MIN_PROJECT_BULLETS if is_project else 1
                if s.text in role.bullets and len(role.bullets) > floor:
                    role.bullets.remove(s.text)
    return r


# ----------------------------------------------------------------------------------------------- measuring

class FitMeter:
    """How much room is left on the page, measured many times with one browser (a PDF per candidate would be slow)."""

    def __init__(self, settings: Settings, work_dir: Path):
        self.settings = settings
        self.work_dir = Path(work_dir)

    def __enter__(self) -> "FitMeter":
        from playwright.sync_api import sync_playwright

        apply_environment(self.settings)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._page = self._browser.new_page(viewport={"width": 816, "height": 1056})
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def spare_px(self, resume: Resume) -> float:
        path = self.work_dir / "measure.html"
        path.write_text(build_html(resume), encoding="utf-8")
        self._page.goto(path.as_uri())
        ok = self._page.evaluate("faces => Promise.all(faces.map(f => document.fonts.load(f).then(r => r.length > 0)))",
                                 list(_FONT_FACES))
        if not all(ok):
            raise RuntimeError("Bundled Tinos font failed to load; refusing to measure with a fallback font")
        m = self._page.evaluate(_MEASURE_JS)
        return (PAGE_HEIGHT_IN - MARGINS["top"] - MARGINS["bottom"]) * PX_PER_IN - m["height"]


def _fit(prepared: Resume, sel: Selection, steps: list[Step], meter: FitMeter) -> tuple[Resume, list[Step]]:
    """Apply steps cheapest-first until the page fits, then put back the most valuable ones that still fit."""
    applied: list[Step] = []
    cur = _build(prepared, sel, applied)
    if meter.spare_px(cur) >= 0:
        return cur, applied
    for st in steps:
        trial = applied + [st]
        nxt = _build(prepared, sel, trial)
        if nxt is not None and _same_content(nxt, cur):
            continue  # this step changed nothing (for example the last bullet of a role): skip it
        applied, cur = trial, nxt
        if meter.spare_px(cur) >= 0:
            break
    else:
        return cur, applied
    for st in sorted(applied, key=lambda s: -s.cost):  # restore the costliest losses first
        without = [s for s in applied if s is not st]
        cand = _build(prepared, sel, without)
        if meter.spare_px(cand) >= 0:
            applied, cur = without, cand
    return cur, applied


def _same_content(a: Resume, b: Resume) -> bool:
    return a.model_dump_json() == b.model_dump_json()


# ----------------------------------------------------------------------------------------------- the entry point

def _gaps(final: Resume, vec: dict[str, float]) -> list[str]:
    text = " ".join(_entry_text(e) for s in final.sections for e in s.entries) + " " + \
        " ".join(f"{ln.label} {ln.text}" for s in final.sections for ln in s.lines)
    ev = _evidence(text)
    out = []
    capability = {"ux_research", "design", "product", "data", "strategy", "ai", "engineering", "stakeholder", "operations"}
    for c, w in sorted(((c, w) for c, w in vec.items() if c in capability), key=lambda kv: -kv[1])[:5]:
        if w >= 3.0 and ev.get(c, 0.0) < 1.5:
            out.append(f"The posting stresses '{c.replace('_', ' ')}' but the kept resume shows little of it; nothing was added.")
    return out


def _orphans(pdf: Path) -> list[str]:
    from pypdf import PdfReader

    out = []
    for line in (PdfReader(str(pdf)).pages[0].extract_text() or "").splitlines():
        t = re.sub(r"\s+", " ", line).strip()
        if t and len(t.split()) == 1 and not t.isupper() and len(t) > 3:
            out.append(t)
    return out


def has_job_description(text: str, title: str = "") -> bool:
    """Whether the text holds a job description to tailor to (the same test tailor_resume uses), not only a form."""
    job = clean_job_text(text or "")
    return len(job.body_text()) >= MIN_JOB_CHARS and job_concepts(job, title)[1] >= MIN_JOB_SIGNAL


def tailor_resume(base: Resume, job_text: str, *, title: str = "", settings: Settings | None = None,
                  out_pdf: Path | None = None, work_dir: Path | None = None,
                  protect: tuple[str, ...] = DEFAULT_PROTECT, client=None, instruction: str = "",
                  bank: dict | None = None, summary: bool | None = None, _baseline: bool = True,
                  ai_wordings: bool = True) -> TailorResult:
    """With a model `client`, also re-word the shown bullets in the posting's language (rewrite.py), steered by the
    person's `instruction` from the chat box. Without one, rules only. `ai_wordings`: the AI keyword wordings
    (wordings.py) may compete with the person's own; when one is used, the version without them is made too, and the
    better keyword match wins (a longer bullet can push a keyword off the page)."""
    rules = load_rules((settings or load_settings()).profile_dir)
    token = _RULES.set(rules)
    try:
        return _tailor_with_rules(base, job_text, rules, title=title, settings=settings, out_pdf=out_pdf,
                                  work_dir=work_dir, protect=protect or rules.protect, client=client,
                                  instruction=instruction, bank=bank, summary=summary, _baseline=_baseline,
                                  ai_wordings=ai_wordings)
    finally:
        _RULES.reset(token)


def _tailor_with_rules(base: Resume, job_text: str, rules: TailoringRules, *, title: str, settings, out_pdf, work_dir,
                       protect, client, instruction, bank, summary, _baseline, ai_wordings) -> TailorResult:
    kw_args = dict(title=title, settings=settings, work_dir=work_dir, protect=protect, client=client,
                   instruction=instruction, bank=bank, summary=summary, _baseline=_baseline)
    result = _tailor_once(base, job_text, ai_wordings=ai_wordings, **kw_args)
    if ai_wordings and result.report.applied and result.report.ai_keywords:
        plain = _tailor_once(base, job_text, ai_wordings=False, **kw_args)
        if plain.report.applied and plain.report.ats_after > result.report.ats_after:
            plain.report.warnings.append(f"AI wordings for {', '.join(k.lower() for k in result.report.ai_keywords)} "
                                         f"were left out: without them the page keeps more of the posting's keywords "
                                         f"({plain.report.ats_after}% vs {result.report.ats_after}%)")
            result = plain
    if out_pdf is not None and result.resume is not None:
        settings = settings or load_settings()
        rep = render_resume(result.resume, Path(out_pdf), settings, work_dir=Path(work_dir or settings.tmp_dir))
        result.report.pages, result.report.spare_px, result.report.fits = rep.pages, rep.spare_px, rep.fits
        result.pdf_path = Path(out_pdf)
    return result


def _tailor_once(base: Resume, job_text: str, *, title: str = "", settings: Settings | None = None,
                 work_dir: Path | None = None, protect: tuple[str, ...] = DEFAULT_PROTECT, client=None,
                 instruction: str = "", bank: dict | None = None, summary: bool | None = None, _baseline: bool = True,
                 ai_wordings: bool = True) -> TailorResult:
    settings = settings or load_settings()
    settings.ensure_dirs()
    work_dir = Path(work_dir or settings.tmp_dir)
    job = clean_job_text(job_text or "")
    if bank is None:
        bank = load_bank(settings, protect)
    key = (job_text or "")[:20000] + (f"\n#hard\n{instruction.strip()}" if client is not None or instruction.strip() else "")
    job_id = hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:12]
    report = TailorReport(applied=False, job_id=job_id)

    body = job.body_text()
    vec, signal = job_concepts(job, title)
    report.concepts = {c: round(v, 2) for c, v in vec.items()}
    if len(body) < MIN_JOB_CHARS or signal < MIN_JOB_SIGNAL:
        report.reason = "no job description found on this page"
        return TailorResult(None, report)

    kw = extract_keywords(job, title)
    # "before" is what would have been sent without tailoring: the one-page default resume (the full base resume is
    # longer than a page and is never uploaded as it is)
    report.ats_before = score(resume_text(_default_for_scoring(base, settings, work_dir) if _baseline else base), kw).percent
    use_summary = _summary_wanted(settings) if summary is None else summary
    confirmed = confirmed_skills(settings)

    final = steps = sel = None
    rewrites_done: list[str] = []
    added: list[tuple[str, str]] = []
    summary_terms: list[str] = []
    woven: list[str] = []
    ai = wordings.usable(settings, protect) if ai_wordings else {}
    for use_rewrites in (True, False):
        woven = []
        prepared, rewrites_done = prepare(base, job_text, rewrites=use_rewrites, bank=bank, vec=vec, instruction=instruction,
                                          kw=kw, protect=protect, ai=ai, woven=woven)
        first_pick = _choose(prepared, vec, job)
        added = add_keywords(prepared, base, kw, first_pick.skills_order, confirmed) if use_rewrites else []
        if use_summary:
            prepared.summary, summary_terms = build_summary(base, kw, resume_text(prepared))
        sel = _choose(prepared, vec, job)
        report.hard, report.refused = False, []
        if client is not None and use_rewrites:  # the fallback pass (use_rewrites=False) is rules only
            pro = _pro(prepared)
            shown = set(sel.projects) | {e.title for e in (pro.entries if pro else [])}
            rw = rewrite_resume(prepared, job, client, title=title, instruction=instruction, only=shown, protect=protect)
            prepared, report.hard, report.refused = rw.resume, bool(rw.changed), rw.kept
            rewrites_done = rewrites_done + rw.changed
            if rw.error:
                report.warnings.append(f"the model could not re-word some entries ({rw.error}); they keep their wording")
        with FitMeter(settings, work_dir) as meter:
            # what the page loses to fit is chosen by cost; when that costs keyword match below the base resume's, the
            # fit is tried again with keywords weighing more (Anthropic, 2026-10-06: 60% -> 49% after a skills line went)
            best = None
            for kw_weight in (0.8, 2.5, 6.0):
                candidate_steps = _make_steps(prepared, sel, vec, job_text, protect, added, kw, kw_weight)
                cand, cand_steps = _fit(prepared, sel, candidate_steps, meter)
                pct = score(resume_text(cand), kw).percent
                if best is None or pct > best[0]:
                    best = (pct, cand, cand_steps)
                # the headline (the role's title) went to make room: the title is the heaviest keyword, so the fit is
                # tried with keywords weighing more before settling (Amazon, 2026-10-07: it was cut for two bullets)
                lost_headline = bool(prepared.summary) and any(st.kind == "summary" for st in cand_steps)
                if pct >= report.ats_before and not lost_headline:
                    break
            _, final, steps = best
            report.spare_px = meter.spare_px(final)
        issues = check_tailored(base, final, allowed_terms=tuple(t for _, t in added) + tuple(woven),
                                summary_terms=tuple(summary_terms))
        if not errors(issues):
            break
        report.warnings += [str(i) for i in errors(issues)]
        final = None
    if final is None:
        report.reason = "the tailored version failed the truth check, so the default resume is used"
        return TailorResult(None, report)

    report.applied = True
    report.education_first, report.education_reason = sel.edu_first, sel.edu_reason
    report.projects = [(t, sel.project_scores.get(t, 0.0)) for t in sel.projects if _find_entry(final, t)]
    report.dropped_projects = list(sel.dropped) + [(s.where, sel.project_scores.get(s.where, 0.0)) for s in steps if s.kind == "project"]
    sk = _skills(final)
    report.skills_order = [ln.label for ln in (sk.lines if sk else [])]
    report.trimmed = [s.label for s in steps]
    shown = {b for s in final.sections for e in s.entries for r in e.roles for b in r.bullets}
    report.rewrites = [n for n in rewrites_done if "wording: '" not in n or n.split("wording: '", 1)[1][:-1] in shown]
    report.gaps = _gaps(final, vec)
    final_text = resume_text(final)
    after = score(final_text, kw)
    report.ats_after = after.percent
    report.keywords_added = [t for _, t in added if re.search(re.escape(t), final_text, re.I)]
    report.missing_keywords = after.missing_hard
    report.summary_line = final.summary or ""
    report.better_proposals = better_proposals(final, kw, settings, protect)
    report.allowed_terms = [t for _, t in added] + woven
    report.ai_keywords = [t for t in woven if any(wordings.term_shown(t, b) for b in shown)]
    report.summary_terms = list(summary_terms) if final.summary else []
    facts = extract_facts(job)
    for y in facts.min_years:
        if 3 <= y.years <= 15 and re.search(r"experience", y.topic, re.I) and not re.search(r"more than \d+ years", y.topic):
            report.warnings.append(f"The posting asks for {y.years}+ years of experience ({y.topic[:90]}...).")
            break
    report.fits = report.spare_px >= 0
    bullets = "\n".join(b.replace("*", "") for s in final.sections if not s.heading.upper().startswith("EDUCATION")
                        for e in s.entries for r in e.roles for b in r.bullets)
    report.in_bullets = [k.name for k in kw.items if k.kind in ("hard", "industry") and k.found_in(bullets)]
    return TailorResult(final, report, None)


def check_result(base: Resume, result: TailorResult) -> list:
    """The truth check a tailored result passed: the base resume, plus exactly the skills it added from the posting
    (each backed by evidence or the person's "I have this") and the summary line's own words."""
    return check_tailored(base, result.resume, allowed_terms=tuple(result.report.allowed_terms),
                          summary_terms=tuple(result.report.summary_terms))


def confirmed_skills(settings: Settings) -> tuple[str, ...]:
    """Skills the person confirmed they have ("I have this" on a missing keyword): profile_overrides "confirmed_skills"."""
    path = settings.profile_dir / "profile_overrides.json"
    try:
        return tuple(json.loads(path.read_text(encoding="utf-8")).get("confirmed_skills", []))
    except (OSError, ValueError):
        return ()


_DEFAULT_CACHE: dict[str, Resume] = {}


def _default_for_scoring(base: Resume, settings: Settings, work_dir: Path) -> Resume:
    """The untailored one-page default resume (made against GENERIC_POSTING, no summary), cached per base resume, in
    memory and in tmp/: the baseline every tailored resume's keyword match is compared with."""
    key = hashlib.sha1(base.model_dump_json().encode()).hexdigest()[:12]
    if key in _DEFAULT_CACHE:
        return _DEFAULT_CACHE[key]
    path = Path(work_dir) / f"default_resume.{key}.json"
    try:
        made = Resume.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = tailor_resume(base, GENERIC_POSTING, settings=settings, work_dir=work_dir, summary=False, _baseline=False,
                               ai_wordings=False)
        made = result.resume or base
        try:
            path.write_text(made.model_dump_json(), encoding="utf-8")
        except OSError:
            pass
    _DEFAULT_CACHE[key] = made
    return made


def _summary_wanted(settings: Settings) -> bool:
    """profile_overrides.json "resume_summary": false turns the summary line off (on by default, per the guide)."""
    path = settings.profile_dir / "profile_overrides.json"
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("resume_summary", True))
    except (OSError, ValueError):
        return True


# What the extension uploads on a page that has no posting on it: an all-round one-page version, made by the same
# rules against this neutral posting (every kind of work weighted about equally).
GENERIC_POSTING = """General application

Responsibilities:
Work with stakeholders and cross-functional teams on product strategy and consulting engagements.
Use data analytics and research to develop clear recommendations for leadership and clients.
Conduct user research, build prototypes and design better experiences.
Build tools with AI and software to support decisions and improve operations and processes.
Present findings and recommendations to executives.

Minimum qualifications:
Strong analytical, communication and problem-solving skills.
Experience with data analysis, strategy, product management and design.
Ability to manage projects, stakeholders and client relationships.

Preferred qualifications:
Experience with AI, software and user research. A graduate degree in business or information.
"""


def default_resume(base: Resume, settings: Settings | None = None, out_pdf: Path | None = None,
                   work_dir: Path | None = None) -> TailorResult:
    """The all-round one-page resume used when a page has no job posting."""
    return tailor_resume(base, GENERIC_POSTING, title="", settings=settings, out_pdf=out_pdf, work_dir=work_dir,
                         ai_wordings=False)

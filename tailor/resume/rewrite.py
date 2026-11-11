"""Hard tailoring: the local model re-words bullets in the posting's own language, one entry at a time.

It may only say the same thing in the posting's words. Every re-worded bullet is checked against the bullet it
replaces, and kept only if it
  * uses no number that bullet did not already have (a number never moves to another bullet),
  * names no new proper noun or acronym, and no tool or method the bullet did not already name (AI, SQL, A/B tests...),
  * does not raise the person's role ("led", "managed"... only if the bullet already said so),
  * keeps every protected phrase (Dr. Example Person), and
  * is not much longer than before (the page must still fit).
A bullet that fails keeps its old wording, and the reason is reported. The whole resume still goes through
check_tailored afterwards, like every other tailored resume.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field

from ..jobs.text import CleanJob
from .schema import Entry, Resume

_NUM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&/+]*")

# Tools and methods: saying one of these is a claim, so it must already be in the bullet.
_CLAIMS = {
    "AI": r"\bai\b|artificial intelligence|gen ?ai|\bllms?\b|large language model",
    "machine learning": r"machine learning|\bml\b|deep learning|neural",
    "a model": r"\bmodel(?:s|ed|ing|led|ling)?\b",
    "programming": r"\bprogram(?:med|ming)\b|\bcod(?:e|ed|ing)\b|software develop|\bpython\b|\bjava(?:script)?\b|\bsql\b|\bhtml\b|\bapi\b",
    "a named tool": r"\btableau\b|power ?bi|\bexcel\b|\bfigma\b|\bhubspot\b|\bsalesforce\b|\bjira\b",
    "an agile method": r"\bagile\b|\bscrum\b|\bsprints?\b|\bkanban\b",
    "experiments": r"a/b|\bexperiment(?:s|ed|ation)?\b",
    "statistics": r"\bstatistic(?:s|al)\b|\bregression\b",
    "a launch": r"\blaunch(?:ed|es|ing)?\b|\bshipp?(?:ed|ing)\b|\bdeploy(?:ed|ing)?\b",
    "money": r"\brevenue\b|\bp&l\b|\bbudget\b|\bpricing\b",
    # posting phrases that are claims in themselves
    "presentations": r"\bpresent(?:ed|ing|s|ation|ations)?\b|\bpitch(?:ed|ing)?\b|\bspeak(?:ing)?\b",
    "entrepreneurial work": r"\bentrepreneur",
    "outreach": r"\boutreach\b",
    "teaching": r"\bteach(?:ing)?\b|\btaught\b|teaching assistant",
    "an internship": r"\bintern(?:ship)?s?\b",
}
_CLAIMS_RE = {k: re.compile(v, re.I) for k, v in _CLAIMS.items()}
_LEAD = re.compile(r"\b(?:led|lead(?:s|ing)?|manag(?:e|ed|es|ing)|direct(?:ed|ing)|head(?:ed|ing)|spearhead(?:ed|ing)|"
                   r"own(?:ed|ing)|overs(?:aw|ee|eeing)|supervis(?:e|ed|ing)|mentor(?:ed|ing))\b", re.I)
# "cross-functional" is fair only for work that already involved other people or functions
_CROSS = re.compile(r"cross[- ]functional", re.I)
_PEOPLE = re.compile(r"\bteam|stakeholder|partner|client|clinician|physician|engineer|designer|leadership|executive|"
                     r"across|with (?:the )?\w+s\b|collaborat", re.I)
_STOP = set("""a an and are as at be by for from in into is it its of on or that the their this to was were with
across over under per via using while through within""".split())
# naming an AI tool the person used makes "AI" (and "AI-assisted") a fair description of that bullet
_AI_TOOL = re.compile(r"\bclaude\b|\bgemini\b|\bchatgpt\b|\bgpt\b|\bcopilot\b|\bllms?\b", re.I)

def _concepts() -> dict[str, re.Pattern]:
    """The tailoring's own kinds of work (auto.CONCEPTS). "built" alone is not engineering here: a dataset can be built."""
    from .auto import CONCEPTS  # auto imports this module

    out = dict(CONCEPTS)
    # nor is qualitative "coding findings from 80 studies"
    out["engineering"] = re.compile(CONCEPTS["engineering"].pattern.replace(r"|\bbuil(?:d|t|ding)\b", "").replace(
        r"\bcoding\b", r"\bcoding\b(?!\s+(?:findings|data|interviews|responses|transcripts|themes))").replace(
        "architecture", r"(?<!information )architecture"), re.I)  # UX "information architecture" is not software
    return out


MIN_KEPT = 0.7  # at least 70% of the bullet's own meaningful words must still be there
MAX_GROWTH = 1.25  # a re-worded bullet may be at most 25% (+25 characters) longer; trimming then fits the page

SYSTEM = (
    "You tailor resume bullets to a job posting. For each bullet, pick the ONE posting qualification its facts best "
    "support, then rewrite the bullet so a recruiter scanning for that qualification sees the posting's own key phrase. "
    "The bullet must still say everything it said: keep every number, name, tool and result, and the same subject. "
    "Only re-phrase and weave in the posting's wording; never add a fact, tool, method, audience or activity the bullet "
    "does not state (for example, do not add presentations, teaching or AI unless the bullet already has them). Do not "
    "make the role bigger (no led, managed or owned unless already there). Keep **bold** marks around the same words. "
    "Keep about the same length. Past tense, no first person. Reply with JSON only."
)


@dataclass
class RewriteResult:
    resume: Resume
    changed: list[str] = field(default_factory=list)  # "Entry: 'old' -> 'new'"
    kept: list[str] = field(default_factory=list)  # re-wordings refused, with the reason
    error: str = ""


def _plain(s: str) -> str:
    return s.replace("*", "")


def _canon(n: str) -> str:
    return n.replace("$", "").replace(",", "").rstrip("%").rstrip(".")


def _proper_terms(text: str) -> set[str]:
    """Capitalized words that are not just the first word, and words with inner capitals (AI, HubSpot)."""
    out = set()
    for i, m in enumerate(_WORD.finditer(text)):
        w = m.group(0)
        inner = any(c.isupper() for c in w[1:])
        if len(w) >= 2 and (inner or (w[0].isupper() and i > 0)):
            out.add(w.casefold())
    return out


def check_bullet(old: str, new: str, protect: tuple[str, ...] = (), exempt: re.Pattern | None = None) -> str | None:
    """Why the re-worded bullet may not replace the old one, or None if it may. `exempt`: a keyword the bullet's own
    evidence already supports (wordings.check_weave); it is set aside when looking for new names and claims only."""
    o, n = _plain(old), _plain(new)
    nc = exempt.sub(" ", n) if exempt is not None else n  # what the claim checks read
    if not n.strip():
        return "empty"
    for p in protect:
        if p in o and p not in n:
            return f"drops '{p}'"
    nums_old, nums_new = {_canon(x) for x in _NUM.findall(o)}, {_canon(x) for x in _NUM.findall(n)}
    if nums_new - nums_old:
        return f"adds the number {sorted(nums_new - nums_old)[0]}"
    if nums_old - nums_new:
        return f"drops the number {sorted(nums_old - nums_new)[0]}"
    words_old = {w.casefold() for w in _WORD.findall(o)}
    words_new = {w.casefold() for w in _WORD.findall(n)}
    ai_ok = bool(_AI_TOOL.search(o))
    if ai_ok:
        words_old |= {"ai"}
    new_terms = sorted(t for t in _proper_terms(nc) if t not in words_old)
    if new_terms:
        return f"adds the name or acronym '{new_terms[0]}'"
    lost = sorted(t for t in _proper_terms(o) if t not in words_new)
    if lost:
        return f"drops '{lost[0]}'"
    content = [w for w in words_old if len(w) > 3 and w not in _STOP]
    stems_new = {w[:5] for w in words_new}
    if content and sum(w[:5] in stems_new for w in content) / len(content) < MIN_KEPT:
        return "says something different (too little of the bullet is left)"
    for what, rx in _CLAIMS_RE.items():
        if rx.search(nc) and not rx.search(o) and not (what == "AI" and ai_ok):
            return f"claims {what}"
    if _CROSS.search(nc) and not _CROSS.search(o) and not _PEOPLE.search(o):
        return "claims cross-functional work"
    for concept, rx in _concepts().items():  # a new kind of work: "technical skills in data analysis and design"
        if rx.search(nc) and not rx.search(o) and not (concept == "ai" and ai_ok) and not (concept == "stakeholder" and _CROSS.search(nc) and _PEOPLE.search(o)):
            return f"claims {concept.replace('_', ' ')} work the bullet does not show"
    if _LEAD.search(nc) and not _LEAD.search(o):
        return "makes the role bigger (led/managed)"
    if len(n) > len(o) * MAX_GROWTH + 25:
        return "too long for the page"
    return None


# text other job extensions inject into the page ("2 out of 12 keywords matched")
_NOISE = re.compile(r"keywords? matched|improve my match|strengthen your resume|skip navigation", re.I)


def qualifications(job: CleanJob, limit: int = 12) -> list[str]:
    """What the posting asks for, one line each: requirements first, then preferences and duties."""
    out: list[str] = []
    for kind in ("required", "preferred", "responsibilities", "skills"):
        for block in job.of_kind(kind):
            for line in re.split(r"\n+", block):
                line = line.strip(" -•\t")
                if len(line) > 30 and not _NOISE.search(line) and line not in out:
                    out.append(line)
    if not out:
        out = [ln.strip() for ln in job.body_text().splitlines() if len(ln.strip()) > 40 and not _NOISE.search(ln)]
    return out[:limit]


def _prompt(entry: Entry, bullets: list[str], quals: list[str], title: str, instruction: str) -> str:
    qs = "\n".join(f"Q{i + 1}. {q}" for i, q in enumerate(quals))
    listed = "\n".join(f"{i + 1}. {b}" for i, b in enumerate(bullets))
    ask = f"\nThe person also asks: {instruction.strip()}\n(The rules above still win.)\n" if instruction.strip() else ""
    return (
        f"Job: {title or 'this role'}\nPosting qualifications:\n{qs}\n\n"
        f"Resume entry: {_plain(entry.title)} ({', '.join(r.title for r in entry.roles)})\n"
        f"Bullets:\n{listed}\n{ask}\n"
        f"Rewrite bullet 1 from bullet 1's facts, bullet 2 from bullet 2's facts, and so on, never mixing them. "
        f'Reply as {{"bullets": [{{"q": "Q3", "text": "..."}}, ...]}} with exactly {len(bullets)} items in order.'
    )


def rewrite_resume(resume: Resume, job: CleanJob, client, *, title: str = "", instruction: str = "",
                   only: set[str] | None = None, protect: tuple[str, ...] = ()) -> RewriteResult:
    """Re-word the bullets of the entries named in `only` (all entries if None). Never raises for a model problem."""
    out = copy.deepcopy(resume)
    res = RewriteResult(out)
    quals = qualifications(job)
    for s in out.sections:
        if s.heading.upper().startswith("EDUCATION"):
            continue
        for e in s.entries:
            if only is not None and e.title not in only:
                continue
            for role in e.roles:
                if not role.bullets:
                    continue
                try:
                    raw = client.complete(_prompt(e, role.bullets, quals, title, instruction), system=SYSTEM,
                                          json_mode=True, max_tokens=1200, temperature=0.4)
                    new = [b.get("text", "") if isinstance(b, dict) else b for b in json.loads(raw)["bullets"]]
                except Exception as ex:  # model down, bad JSON: keep this entry as it was
                    res.error = res.error or f"{type(ex).__name__}"
                    continue
                if not isinstance(new, list) or len(new) != len(role.bullets):
                    res.kept.append(f"{e.title}: the model returned {len(new) if isinstance(new, list) else 'no'} "
                                    f"bullets for {len(role.bullets)}; kept the old wording")
                    continue
                for i, (old, cand) in enumerate(zip(role.bullets, new)):
                    cand = re.sub(r"\s+", " ", str(cand)).strip().lstrip("-• ").strip()
                    if _plain(cand) == _plain(old):
                        continue  # same words (maybe without the bold marks): keep the original
                    why = check_bullet(old, cand, protect)
                    if why:
                        res.kept.append(f"{e.title}: kept '{_plain(old)[:70]}...' ({why})")
                    else:
                        role.bullets[i] = cand
                        res.changed.append(f"{e.title}: '{_plain(old)}' -> '{_plain(cand)}'")
    return res

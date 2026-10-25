"""AI keyword wordings: the model re-words one bullet so it shows one posting keyword the bullet already proves.

They are made ahead of time, for every (bullet, keyword) pair where the keyword's evidence is in the bullet
(keywords.VOCAB): "Data cleaning" for "a data-cleanup request", "Quantitative analysis" for "analyzed 1,240 records".
The model never sees a posting, so it cannot copy one, and tailoring stays instant. Each wording must pass check_weave,
which is stricter than check_bullet: every word must come from the bullet itself, the keyword, a verb that means the
same (auto.VERB_GROUPS) or a short list of neutral connecting words, and every keyword the bullet already showed must
stay. At tailoring time a wording competes with the bullet's other wordings (auto.prepare) and wins only when the
posting asks for its keyword. Stored in profile/ai_wordings.json; the person can reject any of them on the Desk
(Wordings tab), and profile_overrides "ai_wordings": false turns them off.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..config import Settings
from .keywords import VOCAB, Term
from .rewrite import _STOP, check_bullet
from .schema import Resume

SYSTEM = (
    "You edit one resume bullet so it contains a keyword from a job posting, the way a careful resume writer would. "
    "Keep every fact, number, name, tool and result exactly, and the same work, subject and scope. Add nothing the "
    "bullet does not already say: no new activity, audience, team, tool, outcome, purpose or bigger role. Do not remove "
    "words that are skills (for example 'data analytics' or 'usability testing'). Weave the keyword in by re-phrasing "
    "words that mean the same thing, or by naming what the work already was ('analyzed 1,240 records' is quantitative "
    "analysis). Best is replacing a few words with the keyword; otherwise add a short phrase (through ..., using ...) "
    "where it reads naturally. The sentence must read well. Keep **bold** marks on the same words. Past tense, no first "
    "person, no period at the end, at most 25 characters longer. Reply with JSON only."
)
# generic examples (not the person's): a copied word from them fails check_weave, because the bullet does not have it
EXAMPLES = (
    'Bullet: Interviewed 15 store managers about scheduling problems and summarized themes for the operations lead\n'
    'Keyword: user research\n'
    'Good: Conducted user research with 15 store managers on scheduling problems, summarizing themes for the operations lead\n\n'
    'Bullet: Merged 4 regional sales spreadsheets into one standardized file for the finance team\n'
    'Keyword: data cleaning\n'
    'Good: Merged and cleaned 4 regional sales spreadsheets into one standardized file for the finance team, handling the data cleaning\n'
)

# words that connect or name the kind of work without claiming anything new
NEUTRAL = set("""conduct conducted conducting perform performed performing apply applied applying use used using through
including combining combined drawing based involving spanning across within while which whose into toward towards then
this these those that from with before after during also both each their such form forms part handling handled
handle via work methods""".split())
WEAK_START = {"utilized", "utilised", "leveraged", "performed", "employed", "engaged", "used"}
MAX_DROPPED = 1  # of the bullet's own words, at most one may go (replaced by the keyword): more changes what it says
# keywords never woven into a bullet, because saying them there would overstate what the evidence shows: presenting to
# leaders is not "stakeholder management", "$2 million in revenue" is not "revenue growth", and a University of
# Michigan study is not work in "the education sector" (they can still be scored, and added to skills lines by the rules)
NOT_WOVEN = {"Stakeholder management", "Revenue growth", "Education", "Consumer",
             "Executive presentations"}  # client leaders are not executives; it came out as "Used executive presentations to"


CHECK_VERSION = 4  # stored "failed" pairs made under an older check are tried again
LINE_CHARS = 116  # characters of bullet text on one printed line (measured on a tailored resume, 2026-10-07)
_SUFFIXES = ("ational", "ations", "ation", "ings", "ing", "ied", "ies", "ed", "es", "ers", "er", "ly", "s", "e")


def _stem(w: str) -> str:
    """A light stem, so "code" matches "coding" and "analysis" matches "analyzed"."""
    w = w.lower()
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    return w[:5]


def _lines(text: str) -> int:
    return max(1, math.ceil(len(text) / LINE_CHARS))


def _tokens(text: str) -> list[str]:
    return [p for w in re.findall(r"[A-Za-z][A-Za-z'’-]*", text) for p in re.split(r"[-’']", w) if p]


# a skill the bullet names in two words ("data analytics workstream") is the person's own name for it: it stays as it is,
# never renamed to the posting's ("quantitative analysis workstream")
_SKILL_PHRASE = re.compile(r"\b([a-z]+)[ -](analytics|analysis|research|strategy|design|testing|modell?ing|science|engineering|"
                           r"management|visuali[sz]ation|architecture)\b")


def _skill_phrases(text: str) -> list[str]:
    return [f"{a} {b}" for a, b in _SKILL_PHRASE.findall(text.lower())
            if a not in _STOP and len(a) > 2 and not a.endswith("ed")]  # "owned strategy" is a verb and its object


def _keywords_in(text: str) -> list[Term]:
    return [t for t in VOCAB if t.on_resume().search(text)]


def _synonyms(words: set[str]) -> set[str]:
    from .auto import VERB_GROUPS  # auto imports this module

    out: set[str] = set()
    for g in VERB_GROUPS:
        if g & words:
            out |= {_stem(v) for v in g}
    return out


def _rebold(old: str, new: str) -> tuple[str, str | None]:
    """The new text with exactly the old bullet's bold spans (the model drops some and bolds the keyword)."""
    spans = re.findall(r"\*\*(.+?)\*\*", old)
    plain = new.replace("*", "")
    for s in spans:
        if s not in plain:
            return plain, f"drops '{s}'"
        plain = plain.replace(s, f"**{s}**", 1)
    return plain, None


def check_weave(old: str, new: str, term: Term, protect: tuple[str, ...] = ()) -> tuple[str, str | None]:
    """(the cleaned-up wording, why it may not replace `old` or None). It must show `term`, keep every keyword `old`
    showed, pass check_bullet once the keyword itself is set aside, and use no word that `old`, the keyword, a
    same-meaning verb or a neutral connector does not supply."""
    new = re.sub(r"\s+", " ", str(new)).strip().lstrip("-• ").strip()
    if not old.rstrip().endswith("."):
        new = new.rstrip(".")
    new, why = _rebold(old, new)
    if why:
        return new, why
    if new[:1].islower():
        new = new[0].upper() + new[1:]
    o, n = old.replace("*", ""), new.replace("*", "")
    found = term.on_resume().search(n)
    if not found:
        return new, f"does not say '{term.name.lower()}'"
    if term.on_resume().search(o):
        return new, "the bullet already says it"
    # the keyword mid-sentence in the bullet's own case ("Data Cleaning" -> "data cleaning"; CRM, KPIs stay)
    span = found.group(0)
    if found.start() > 0 and span != span.lower() and not span.isupper() and not any(c.isupper() for c in term.name[1:]):
        new = new.replace(span, span.lower(), 1)
        n = new.replace("*", "")
    lost = [t.name for t in _keywords_in(o) if not t.on_resume().search(n)]
    if lost:
        return new, f"drops the keyword '{lost[0].lower()}'"
    flat = n.lower().replace("-", " ")
    if (renamed := [ph for ph in _skill_phrases(o) if ph not in flat]):
        return new, f"renames '{renamed[0]}'"
    why = check_bullet(old, new, protect, exempt=term.on_resume())
    if why:
        return new, why
    # the closed vocabulary: nothing the bullet did not already say
    old_words = {w.lower() for w in _tokens(o)}
    allowed = {_stem(w) for w in old_words} | _synonyms(old_words) | {_stem(w) for w in NEUTRAL}
    allowed |= {_stem(w) for w in _tokens(term.name)}  # the keyword's own words only ("higher education clients" would add clients)
    for w in _tokens(n):
        lw = w.lower()
        if len(lw) < 4 or lw in _STOP or _stem(lw) in allowed:
            continue
        return new, f"adds the word '{w}'"
    # ... and nothing it said is lost: "Owned strategy and execution end-to-end" is not "Owned project management"
    new_words = {w.lower() for w in _tokens(n)}
    kept = {_stem(w) for w in new_words} | _synonyms(new_words)
    dropped = [w for w in dict.fromkeys(_tokens(o)) if len(w) >= 4 and w.lower() not in _STOP and _stem(w) not in kept]
    if len(dropped) > MAX_DROPPED:
        return new, f"leaves out '{dropped[0]}' and '{dropped[1]}'"
    if _lines(n) > _lines(o) and len(n) > len(o) + 30:  # a longer wording may not add more than a short line
        return new, "too long for the page"
    return new, None


def _prompt(source: str, term: Term) -> str:
    name = term.name if any(c.isupper() for c in term.name[1:]) else term.name.lower()
    return (f"{EXAMPLES}\nNow this one.\nBullet: {source}\nKeyword: {name}\n"
            'Write 3 different versions. Reply as {"options": ["...", "...", "..."]}')


def _rank(old: str, new: str) -> float:
    """Among passing wordings: keep the bullet's own first verb if possible, avoid weak openers, then the shortest."""
    first_old = old.replace("*", "").split(" ", 1)[0].lower()
    first_new = new.replace("*", "").split(" ", 1)[0].lower()
    return (20.0 if first_new == first_old else 0.0) - (30.0 if first_new in WEAK_START else 0.0) - 0.15 * len(new)


def write_one(client, source: str, term: Term, protect: tuple[str, ...] = (), tries: int = 2) -> tuple[str | None, list[str]]:
    """The best passing wording of `source` that shows `term`, or None, with why the others were refused."""
    refused: list[str] = []
    best: str | None = None
    for attempt in range(tries):
        try:
            raw = client.complete(_prompt(source, term), system=SYSTEM, json_mode=True, max_tokens=600,
                                  temperature=0.5 + 0.2 * attempt)
            opts = json.loads(raw).get("options", [])
        except Exception as e:  # model down, bad JSON
            refused.append(f"model: {type(e).__name__}")
            break
        for cand in opts if isinstance(opts, list) else []:
            text, why = check_weave(source, cand if isinstance(cand, str) else str(cand), term, protect)
            if why:
                refused.append(f"{why}: {text[:90]}")
            elif best is None or _rank(source, text) > _rank(source, best):
                best = text
        if best:
            break
    return best, refused


# ----------------------------------------------------------------------------------------------- the store

@dataclass
class Target:
    original: str  # the base resume's bullet (the key the bank and prepare use)
    source: str  # the wording the model re-words: the bullet as tailoring shows it, or an approved bank variant
    term: Term


def store_path(settings: Settings) -> Path:
    return settings.profile_dir / "ai_wordings.json"


def enabled(settings: Settings) -> bool:
    try:
        over = json.loads((settings.profile_dir / "profile_overrides.json").read_text(encoding="utf-8"))
        return over.get("ai_wordings", True) is not False
    except (OSError, ValueError):
        return True


def load_store(settings: Settings) -> list[dict]:
    try:
        return json.loads(store_path(settings).read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError):
        return []


_SAVE = threading.Lock()


def save_store(settings: Settings, items: list[dict]) -> None:
    with _SAVE:
        p = store_path(settings)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(p)


def _term(name: str) -> Term | None:
    return next((t for t in VOCAB if t.name == name), None)


def _still_evidenced(term: Term | None, source: str) -> bool:
    """The keyword is still one this bullet proves (the evidence rules or NOT_WOVEN may have changed since)."""
    ev = term.ev() if term is not None else None
    return ev is not None and term.name not in NOT_WOVEN and ev.search(source.replace("*", "")) is not None


def term_shown(name: str, bullet: str) -> bool:
    t = _term(name)
    return t is not None and t.on_resume().search(bullet.replace("*", "")) is not None


def targets(base: Resume, bank: dict[str, list[tuple[str, str]]],
            rewrites: list[tuple[str, str]] | None = None) -> list[Target]:
    """Every (bullet, keyword) pair worth a wording: the keyword's evidence is in the bullet (or one of its approved
    wordings) and the bullet does not say the keyword yet. `rewrites`: the person's reviewed re-wordings (rules.py)."""
    from .auto import active_rules  # auto imports this module

    rewrites = active_rules().approved_rewrites if rewrites is None else rewrites

    out: list[Target] = []
    for s in base.sections:
        if s.heading.upper().startswith("EDUCATION"):
            continue
        for e in s.entries:
            for r in e.roles:
                for b in r.bullets:
                    shown = b
                    for old, new in rewrites:
                        shown = shown.replace(old, new)
                    wordings = [shown] + [t for _, t in bank.get(b, [])]
                    for term in VOCAB:
                        if term.name in NOT_WOVEN:
                            continue
                        ev = term.ev()
                        if ev is None or term.on_resume().search(shown.replace("*", "")):
                            continue
                        source = next((w for w in wordings if ev.search(w.replace("*", ""))), None)
                        if source and not term.on_resume().search(source.replace("*", "")):
                            out.append(Target(b, source, term))
    return out


def usable(settings: Settings, protect: tuple[str, ...] = ()) -> dict[str, list[tuple[str, str]]]:
    """original bullet -> [("AI: <keyword>", wording), ...]: the wordings tailoring may use (re-checked on load)."""
    if not enabled(settings):
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    for it in load_store(settings):
        term = _term(it.get("term", ""))
        if it.get("status") != "auto" or not it.get("text") or not _still_evidenced(term, it.get("source", "")):
            continue
        text, why = check_weave(it["source"], it["text"], term, protect)
        if why is None:
            out.setdefault(it["original"], []).append((f"AI: {term.name}", text))
    return out


def missing(settings: Settings, base: Resume, bank: dict, terms: set[str] | None = None) -> list[Target]:
    """Pairs with no wording yet (or whose source wording changed), optionally only for these keyword names."""
    def done(it: dict) -> bool:
        if it.get("status") == "failed":
            return it.get("check", 1) >= CHECK_VERSION
        if it.get("status") == "auto" and it.get("check", 1) < CHECK_VERSION:  # made under an older check: re-judged
            term = _term(it["term"])
            return term is not None and check_weave(it["source"], it["text"], term)[1] is None
        return True  # "auto" under this check, or the person's own decision ("rejected")
    have = {(it["original"], it["term"], it.get("source", "")) for it in load_store(settings) if done(it)}
    from .rules import load_rules

    rewrites = load_rules(settings.profile_dir).approved_rewrites
    return [t for t in targets(base, bank, rewrites) if (t.original, t.term.name, t.source) not in have
            and (terms is None or t.term.name in terms)]


def fill(settings: Settings, base: Resume, client, bank: dict, *, protect: tuple[str, ...] = (),
         terms: set[str] | None = None, budget_s: float | None = None, log=print, model_name: str = "",
         stop: threading.Event | None = None) -> int:
    """Write the missing wordings (one model call or two per pair), saving after each. Returns how many were made."""
    todo = missing(settings, base, bank, terms)
    made, t0 = 0, time.time()
    for i, t in enumerate(todo, 1):
        if (budget_s is not None and time.time() - t0 > budget_s) or (stop is not None and stop.is_set()):
            break
        text, refused = write_one(client, t.source, t.term, protect)
        if refused and refused[0].startswith("model:") and text is None:
            log(f"ai wordings: the model is not answering ({refused[0]}); stopped")
            break
        items = [it for it in load_store(settings)
                 if not (it["original"] == t.original and it["term"] == t.term.name)]
        items.append({"original": t.original, "term": t.term.name, "source": t.source, "text": text or "",
                      "status": "auto" if text else "failed", "refused": refused[:6], "model": model_name,
                      "check": CHECK_VERSION,
                      "made": date.today().isoformat()})
        save_store(settings, items)
        made += bool(text)
        log(f"ai wordings {i}/{len(todo)}: {t.term.name} | {'ok' if text else 'none passed'} | {t.original[:50]}")
    return made


def set_status(settings: Settings, original: str, term: str, status: str) -> bool:
    """The person's decision on the Desk: "rejected" (never used) or "auto" (used when a posting asks for it)."""
    items = load_store(settings)
    hit = False
    for it in items:
        if it["original"] == original and it["term"] == term and it.get("text"):
            it["status"], hit = status, True
    if hit:
        save_store(settings, items)
    return hit

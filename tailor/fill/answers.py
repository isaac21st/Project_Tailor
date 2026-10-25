"""Draft free-text answers with the model, without letting it invent facts.

The model sees only the applicant's confirmed facts and an excerpt of the job. Its draft is then checked: any number
it uses must already appear in those facts (or the job text or the question), otherwise it is asked to rewrite, and if
it still slips, the offending sentence is removed. The result is always a draft for the person to review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..jobs.text import clean_job_text
from ..llm import LLMClient
from .profile import Profile
from .sensitive import classify_text

SYSTEM = (
    "You write short answers for a job application on behalf of the applicant, in their own voice: first person, "
    "plain and warm, the way a thoughtful person talks to a recruiter. Contractions are fine. Start with the point, "
    "not by restating the question. Use one concrete example from APPLICANT FACTS rather than a list of achievements, "
    "and say plainly why it matters for this job. Use ONLY facts that appear in APPLICANT FACTS; never invent numbers, "
    "employers, tools, titles, dates or achievements. Avoid stock phrases such as: passionate, excited, thrilled, "
    "leverage, align(s) with, resonate(s), drawn to, dynamic, fast-paced, cutting-edge, innovative, synergy, "
    "mission-driven, I am confident, I believe I would be a great fit, make an impact, unique blend, "
    "I have always. Do not mention being an AI. No placeholders, no markdown, plain text only. "
    # the shape the person approved (2026-10-10, Palantir "a skill that has nothing to do with your resume")
    "Reason in three steps: first sentence names the answer itself (the skill, the reason, the point); then one real "
    "example from APPLICANT FACTS or the applicant's note, saying where it was and what they did; then how it carries "
    "into this role, tied to something this job actually involves, naming the company. When the note names something "
    "(a job, a skill) without saying what they did there, mention it plainly and do not invent what they did. "
    "Never use em dashes or en dashes: use commas, periods or 'and' instead."
)


def no_dashes(text: str) -> str:
    """The person's rule (2026-10-10): no em dashes in anything written for them. A dash pair around an aside becomes
    commas; a lone dash becomes a comma; number ranges keep a plain hyphen."""
    text = re.sub(r"(\d)\s*[–—]\s*(\d)", r"\1-\2", text)
    text = re.sub(r"\s*[—–]\s*([^—–.!?\n]{1,80}?)\s*[—–]\s*", r", \1, ", text)
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    text = re.sub(r",\s*([.!?,;:])", r"\1", text)
    return re.sub(r" {2,}", " ", text)
# Stock phrases that make an answer read as generated. A draft using one is sent back once to be rewritten.
_CLICHES = re.compile(
    r"\bpassion(?:ate)?\b|\bexcited\b|\bthrilled\b|\bleverag|\balign(?:s|ed)? (?:with|perfectly)|\bresonat|"
    r"\bdrawn to\b|\bdynamic\b|fast-paced|cutting-edge|\binnovative\b|\bsynerg|mission-driven|\bi am confident\b|"
    r"great fit|perfect fit|make an impact|unique blend|\bi have always\b|\bdeeply\b|\btestament\b|\bdelve", re.I)
DEFAULT_SENTENCES = 4
DEFAULT_WORDS = 75

_WORDS_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_NUMBER = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_YEARS_AFTER = re.compile(r"\s*[- ]?(?:year|yr|month)s?\b", re.I)


# Questions about the applicant beyond the resume: only they know the answer, so nothing is drafted without their note
BEYOND_RESUME = re.compile(
    r"nothing to do with (?:your|the) (?:resume|cv|job|work|career)|(?:not|isn['’]?t) (?:on|in) your (?:resume|cv)|"
    r"outside (?:of )?(?:work|your (?:resume|cv|job))|beyond your (?:resume|cv)|fun fact|\bhobb(?:y|ies)\b|"
    r"(?:in|with) your (?:free|spare) time|for fun\b|something (?:about you )?(?:we|that) (?:wouldn['’]?t|won['’]?t|can['’]?t|"
    r"don['’]?t) (?:know|find|learn|see)", re.I)
BEYOND_RESUME_ASK = ("only you know this (it is about you beyond your resume): type what to say in the Tailor chat, "
                     "then click Write it")


class NeedsApplicant(ValueError):
    """The question needs something from the applicant before a draft can be written."""


class SensitiveQuestion(ValueError):
    """The question is one only the person may answer."""


@dataclass
class Draft:
    text: str
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def _canon(token: str) -> str:
    return token.replace("$", "").replace(",", "").rstrip("%").rstrip(".")


def unsupported_numbers(text: str, allowed_source: str) -> list[str]:
    """Numbers in `text` that do not appear in `allowed_source`. Bare single digits are ignored unless they
    describe a length of time ("7 years"), which is exactly the kind of claim that must not be invented."""
    allowed = {_canon(m) for m in _NUMBER.findall(allowed_source)}
    bad: list[str] = []
    for m in _NUMBER.finditer(text):
        tok = _canon(m.group(0))
        if not tok or tok in allowed:
            continue
        bare_digit = len(tok) == 1 and not m.group(0).endswith("%") and not m.group(0).startswith("$")
        if bare_digit and not _YEARS_AFTER.match(text, m.end()):
            continue
        bad.append(m.group(0))
    return bad


def length_hint(*texts: str) -> tuple[int | None, int | None, int | None]:
    """(max sentences, max words, max characters) the form asks for: "Please share 3-4 sentences", "in 100 words or
    less", "500 characters max", "one paragraph"."""
    t = " ".join(x for x in texts if x).lower()
    sentences = words = chars = None
    if m := re.search(r"(\d|one|two|three|four|five|six)\s*(?:-|–|to|or)?\s*(\d|one|two|three|four|five|six)?\s+sentences?", t):
        top = m.group(2) or m.group(1)
        sentences = int(top) if top.isdigit() else _WORDS_NUM[top]
    if m := re.search(r"(\d{2,4})\s*words", t):
        words = int(m.group(1))
    if m := re.search(r"(\d{2,5})\s*char", t):
        chars = int(m.group(1))
    if sentences is None and re.search(r"\bone (?:short )?paragraph|\bbrief(?:ly)?\b", t):
        sentences = 4
    return sentences, words, chars


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", text.strip())


def _drop_sentences_with(text: str, bad: list[str]) -> str:
    return " ".join(s for s in _sentences(text) if not any(b in s for b in bad)).strip()


def _cut_to(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.endswith((".", "!", "?")) and len(cut) - 1)
    return cut[: end + 1].strip() if end and end > limit * 0.4 else cut.rsplit(" ", 1)[0].rstrip(",;:") + "."


def _clean_output(text: str) -> str:
    text = re.sub(r"^```\w*\n?|```$", "", text.strip())
    text = re.sub(r"^(?:answer|response)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"[*_#`]+", "", text)
    return no_dashes(text.strip().strip('"').strip())


# ---- the answer library: what the person actually sent, reused as a starting point for similar questions ----
_Q_STOP = set("a an the and or of to for in on at is are you your do does did what why how tell us about this that "
              "with we our would like please share describe".split())


def _qwords(q: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (q or "").lower()) if w not in _Q_STOP}


def load_library(path) -> list[dict]:
    import json
    from pathlib import Path

    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def add_to_library(path, answers: list[dict], company: str = "", title: str = "") -> int:
    """Keep what the person sent (never a sensitive question), newest first, one copy of each text."""
    import json
    from datetime import date
    from pathlib import Path

    lib = load_library(path)
    have = {e["text"].strip() for e in lib}
    added = 0
    for a in answers:
        q, t = (a.get("question") or "").strip(), (a.get("text") or "").strip()
        if not q or len(t) < 40 or t in have or classify_text(q):
            continue
        lib.insert(0, {"question": q[:300], "text": t[:6000], "company": company, "title": title,
                       "date": date.today().isoformat()})
        have.add(t)
        added += 1
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(lib[:300], indent=2, ensure_ascii=False), encoding="utf-8")
    return added


def similar_answers(library: list[dict], question: str, k: int = 2) -> list[dict]:
    """Past answers to questions that share most of this one's words ("Why X?" finds other "Why ...?" answers)."""
    qw = _qwords(question)
    if not qw:
        return []
    scored = []
    for e in library:
        ew = _qwords(e.get("question", ""))
        if ew:
            score = len(qw & ew) / len(qw | ew)
            if score >= 0.3:
                scored.append((score, e))
    return [e for _, e in sorted(scored, key=lambda t: -t[0])[:k]]


def job_excerpt(page_text: str | None, limit: int = 1800) -> str:
    if not page_text:
        return ""
    try:
        body = clean_job_text(page_text).body_text()
    except Exception:
        body = page_text
    return " ".join((body or page_text).split())[:limit]


def build_prompt(profile: Profile, question: str, excerpt: str = "", max_chars: int | None = None,
                 instruction: str = "", current: str = "", hint: str = "", examples: list[dict] | None = None,
                 as_points: bool = True) -> str:
    """`as_points`: the instruction is the person's chat note, shown one point per line; False for a writing guide
    (the cover letter's), which stays one block."""
    n_sent, n_words, n_chars = length_hint(question, hint, instruction)
    n_sent = n_sent or (None if n_words else DEFAULT_SENTENCES)
    parts = ([f"{n_sent} sentences or fewer"] if n_sent else []) + [f"at most {n_words or DEFAULT_WORDS} words"]
    cap = min(c for c in (max_chars, n_chars) if c) if (max_chars or n_chars) else None
    if cap:
        parts.append(f"at most {cap} characters")
    limit = "LIMIT: " + ", ".join(parts) + ".\n"
    note = f"NOTE UNDER THE QUESTION: {hint}\n" if hint and hint not in question else ""
    past = ""
    if examples:
        past = ("THE APPLICANT'S OWN PAST ANSWERS TO SIMILAR QUESTIONS (reuse their wording and stories where they fit; "
                "change any other company's name to this one):\n" +
                "\n".join(f"- ({e.get('company') or 'another company'}) {e['text']}" for e in examples) + "\n\n")
    job = f"JOB (excerpt):\n{excerpt}\n\n" if excerpt else ""
    draft = f"CURRENT DRAFT (keep what works, change what the applicant asks):\n{current}\n\n" if current else ""
    points = [p.strip() for p in re.split(r"[,.;\n]", instruction) if p.strip()]
    # one point per line: a terse note ("Sales from acme rentals, clinic behavior tech") read as one run-on
    # phrase made the model write "At Acme I applied Clinic Behavior Tech" (2026-10-10)
    ask = ("THE APPLICANT'S NOTE (each line is a separate point and a fact about them; cover every point, keep separate "
           "jobs separate, and add no story, detail or example that is not in the note or APPLICANT FACTS):\n"
           + "\n".join(f"- {p}" for p in points) + "\n") if instruction else ""
    if instruction and not as_points:
        ask = f"THE APPLICANT ASKS: {instruction}\n"
    go = "Rewrite the answer now." if (current or instruction) else "Write the answer now."
    return (f"APPLICANT FACTS:\n{profile.facts_text()}\n\n{past}{job}QUESTION: {question}\n{note}{draft}{ask}{limit}"
            f"{go} /no_think")


def draft_answer(client: LLMClient, profile: Profile, question: str, page_text: str | None = None,
                 max_chars: int | None = None, instruction: str = "", current: str = "", hint: str = "",
                 examples: list[dict] | None = None) -> Draft:
    """`instruction` is what the applicant typed in the chat ("3-4 sentences, lead with my research work"); `current` is
    the text already in the box, so the model revises it instead of starting over. Numbers the applicant typed count
    as theirs; numbers only in the old draft do not (a previous slip must not survive a rewrite)."""
    if (category := classify_text(question)):
        raise SensitiveQuestion(f"'{question}' is a {category} question; only the applicant answers it")

    if BEYOND_RESUME.search(f"{question} {hint}") and not instruction.strip() and not current.strip():
        raise NeedsApplicant(BEYOND_RESUME_ASK)
    excerpt = job_excerpt(page_text)
    # numbers the applicant wrote in their own past answers are theirs too
    allowed = f"{profile.facts_text()}\n{excerpt}\n{question}\n{instruction}\n" + " ".join(e["text"] for e in examples or [])
    prompt = build_prompt(profile, question, excerpt, max_chars, instruction, current, hint, examples)
    n_sent, n_words, n_chars = length_hint(question, hint, instruction)
    n_sent = n_sent or (None if n_words else DEFAULT_SENTENCES)
    n_words = n_words or DEFAULT_WORDS
    cap = min(c for c in (max_chars, n_chars) if c) if (max_chars or n_chars) else None
    warnings: list[str] = []

    text = _clean_output(client.complete(prompt, system=SYSTEM, max_tokens=700, temperature=0.3))
    for _ in range(2):  # at most two rewrites
        problems = []
        if bad := unsupported_numbers(text, allowed):
            problems.append(f"You used numbers that are not in APPLICANT FACTS: {', '.join(bad)}. Remove them")
        if cap and len(text) > cap:
            problems.append(f"The answer is {len(text)} characters; keep it under {cap}")
        if n_sent and len(_sentences(text)) > n_sent:
            problems.append(f"It has {len(_sentences(text))} sentences; use {n_sent} or fewer")
        if len(text.split()) > n_words * 1.15:
            problems.append(f"It has {len(text.split())} words; use at most {n_words}")
        if cliches := sorted({m.group(0).lower() for m in _CLICHES.finditer(text)}):
            problems.append(f"It sounds generated ({', '.join(cliches)}); say it plainly in the applicant's own words")
        if left_out := missing_points(text, instruction):
            problems.append("The applicant asked you to cover these and the answer leaves them out: "
                            f"{', '.join(left_out)}. Use every point they gave")
        if not problems:
            break
        text = _clean_output(client.complete(
            f"{prompt}\n\nYOUR PREVIOUS ANSWER:\n{text}\n\nFix this and write the answer again: "
            + "; ".join(problems) + ". /no_think", system=SYSTEM, max_tokens=700, temperature=0.2))

    if bad := unsupported_numbers(text, allowed):
        warnings.append(f"removed a sentence using numbers not in your profile: {', '.join(bad)}")
        text = _drop_sentences_with(text, bad)
    if n_sent and len(_sentences(text)) > n_sent:
        warnings.append(f"kept the first {n_sent} sentences")
        text = " ".join(_sentences(text)[:n_sent])
    if cap and len(text) > cap:
        warnings.append(f"shortened to fit {cap} characters")
        text = _cut_to(text, cap)
    if cliches := sorted({m.group(0).lower() for m in _CLICHES.finditer(text)}):
        warnings.append(f"still has stock phrasing to reword: {', '.join(cliches)}")
    if left_out := missing_points(text, instruction):
        warnings.append(f"leaves out what you asked for: {', '.join(left_out)}")
    return Draft(text=text, warnings=warnings)


_POINT_STOP = {"talk", "about", "apply", "applying", "applied", "setting", "settings", "skill", "skills", "using", "make",
               "write", "answer", "mention", "focus", "keep", "short", "shorter", "longer", "sentence", "sentences",
               "words", "word", "more", "less", "from", "with", "that", "this", "into", "also", "like", "lead", "please",
               "include", "tone", "job", "jobs", "role", "work", "experience", "know", "knowledge", "body", "my", "your"}


def _stem(w: str) -> str:
    w = w.lower()
    for suf in ("ology", "ical", "ing", "ers", "er", "ed", "es", "s", "y"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)][:6]
    return w[:6]


def missing_points(text: str, instruction: str) -> list[str]:
    """The things the applicant's note names ("Sales from acme rentals, clinic behavior tech") that the
    answer never mentions: each comma- or "and"-separated point counts as covered when most of its words are used."""
    if not instruction.strip():
        return []
    said = {_stem(w) for w in re.findall(r"[A-Za-z][A-Za-z'-]+", text)}
    out = []
    for point in re.split(r"[,.;\n]|\band\b|\bplus\b", instruction):
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", point) if w.lower() not in _POINT_STOP]
        # most of its words, not one shared word ("customer behavior" does not cover "registered behavior tech")
        if words and sum(_stem(w) in said for w in words) * 2 < len(words):
            out.append(point.strip())
    return out

"""Cover letters: written by the local model from the applicant's facts and the posting, checked like any drafted
answer (no number that is not in the applicant's facts, no stock phrases), and made into a one-page PDF to attach.
The applicant reads it before submitting; it is a draft, like every written answer.
"""
from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path

from ..llm import LLMClient
from .answers import Draft, no_dashes
from .profile import Profile

WORDS = 300  # four short paragraphs: well inside one page

# University of Michigan Career Center, "Cover Letter Resources" (the person's chosen guide, 2026-10-06):
# 3-4 paragraphs; intro and interest (no generic opening, a referral if there is one); experiences tied to the job
# description with specific examples and results, in the posting's language, not the resume repeated; a close that
# sums up interest, thanks them, and may name the mission. Enthusiastic, confident but not imposing; no cliches; not
# every sentence starting with "I". One page. AI helps draft and tailor; the person personalizes and reviews it.
GUIDE = ("Follow the University of Michigan Career Center's cover letter guide: "
         "Paragraph 1: who I am (my degree programs) and why this role and this company specifically; no generic "
         "opening. Mention a referral ONLY if my note below names one; never invent a referral, a professor, a "
         "conversation or any contact with the company. "
         "Paragraphs 2 and 3: connect my experiences to what the job description asks for, with specific examples and "
         "the results or numbers in my facts, using the posting's own words; explain what makes me qualified instead of "
         "repeating resume lines. "
         "Paragraph 4: sum up my interest, thank them, and if it fits name the company's mission or values. "
         "Tone: enthusiastic, positive, confident but not imposing. Do not start most sentences with 'I'. No cliches "
         "('outstanding candidate', 'passionate', 'perfect fit', 'To Whom It May Concern'). "
         "Separate the four paragraphs with a blank line. "
         "In paragraphs 2 and 3, reason from each experience to this job: name the skill, show it with one real "
         "example from my facts (where, what I did, the result), then say what it means for this role's actual work. "
         "Never use em dashes or en dashes; use commas, periods or 'and'.")
_GENERIC = re.compile(r"to whom it may concern|outstanding candidate|perfect fit|i am writing to (?:express|apply)|"
                      r"passionate about|dream (?:job|company)|aligns? (?:well )?with|resonates?|your company", re.I)
_CONTACT = re.compile(r"referr|recommended (?:me|by)|(?:spoke|talked|met|connected) with|introduced (?:me|by)|"
                      r"former professor|networking event|info(?:rmation(?:al)?)? (?:session|interview)", re.I)


def _degrees(profile: Profile) -> str:
    """The degrees in plain words, so the letter states them exactly ("MBA (School of Business); MS (...)")."""
    out = []
    for e in profile.education:
        # "University of X, School of Business" -> "School of Business": the school within the university, when named
        school = e.school.split(",", 1)[1].strip() if "," in e.school else e.school
        status = f"expected {e.end.year}" if e.expected and e.end else (f"{e.end.year}" if e.end else "")
        out.append(f"{e.degree} ({school}{', ' + status if status else ''})")
    return "; ".join(out)


def _posting_words(profile: Profile, page_text: str, title: str) -> str:
    """The posting's highest-priority keywords (the tailoring guide's step 1) that the applicant's own experience shows,
    so the letter speaks the posting's language without claiming anything new."""
    try:
        from ..jobs import clean_job_text
        from ..resume.keywords import extract_keywords

        kw = extract_keywords(clean_job_text(page_text or ""), title)
    except Exception:
        return ""
    mine = " ".join([e.title + " " + " ".join(e.bullets) for e in profile.experience] + list(profile.skills))
    picks = [k.name.lower() if not any(c.isupper() for c in k.name[1:]) else k.name
             for k in kw.items if k.kind in ("hard", "industry") and k.found_in(mine)]
    return ", ".join(dict.fromkeys(picks[:6]))


# Words that make the applicant's role bigger than the facts: said about the applicant, they must be in the facts
_INFLATE = re.compile(r"\bsenior\b|\bexecutives?\b|c-suite|\bvice presidents?\b|\bdirectors?\b|\bled\b|\bmanaged\b|"
                      r"\bspearhead\w*|\boversaw\b|\bheaded\b|\bteam of \d+|\blaunch(?:ed)?\b|\bmentored\b|"
                      r"\bhired\b|\bp&l\b|\bbudgets?\b|\bmillions?\b|\byears of experience\b", re.I)
_ME = re.compile(r"\b(?:I|I['’]m|I['’]ve|I['’]d|my|me|myself)\b")


def _sentences(text: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x.strip()]


# "my background in data science, fintech, and economics": each thing named must be in the facts
_CLAIM_LIST = re.compile(r"\b(?:background|expertise|experience|skills?|foundation|proficien\w*|fluen\w*|training|knowledge|"
                         r"coursework|grounding) (?:in|with)\s+([^.;:!?]+)", re.I)
_LIST_STOP = {"and", "or", "the", "a", "an", "of", "in", "with", "both", "my", "strong", "solid", "deep", "hands-on", "areas", "such", "as",
              "like", "including", "skills", "work", "tools", "methods", "practical"}


def _stem5(w: str) -> str:
    w = w.lower()
    for suf in ("ics", "ical", "ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[: -len(suf)]
            break
    return w[:5]


def _listed_unsupported(sent: str, fact_stems: set[str]) -> list[str]:
    bad = []
    for m in _CLAIM_LIST.finditer(sent):
        for item in re.split(r",\s*|\s+and\s+|\s+or\s+|\s*&\s*", m.group(1))[:6]:
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z+#/-]*", item) if w.lower() not in _LIST_STOP]
            if not words or len(words) > 3:  # a clause ("tools I have used to ..."), not a named thing
                continue
            if not all(_stem5(w) in fact_stems for w in words if len(w) >= 3):
                bad.append(re.sub(r"^(?:and|or)\s+", "", item.strip()))
    return bad


def unsupported_claims(text: str, facts: str) -> list[str]:
    """What the letter says about the applicant that the applicant's facts do not show: a tool or method (keywords.VOCAB
    hard skills: "my SQL work") or a bigger role ("senior leaders", "led", "managed"). Sentences about the company are
    not checked ("your team uses SQL")."""
    from ..resume.keywords import VOCAB

    bad: list[str] = []
    fact_stems = {_stem5(w) for w in re.findall(r"[A-Za-z][A-Za-z+#/-]*", facts)}
    for sent in _sentences(text):
        if not _ME.search(sent):
            continue
        bad += _listed_unsupported(sent, fact_stems)
        for m in _INFLATE.finditer(sent):
            if not re.search(rf"\b{re.escape(m.group(0))}", facts, re.I):
                bad.append(m.group(0).lower())
        for t in VOCAB:
            if t.kind != "hard" or not t.rx().search(sent):
                continue
            shown = t.on_resume().search(facts) or (t.ev() is not None and t.ev().search(facts))
            if not shown:
                bad.append(t.name.lower() if not any(c.isupper() for c in t.name[1:]) else t.name)
    return list(dict.fromkeys(bad))


def _repeats(text: str, overlap: float = 0.6) -> set[str]:
    """Sentences that mostly repeat an earlier one: most of their content words (stemmed) were already said together."""
    seen: list[set[str]] = []
    again = set()
    for sent in _sentences(text):
        words = {_stem5(w) for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", sent)} - {"have", "with", "that", "this", "from", "allow"}
        if len(words) >= 5 and any(len(words & old) / len(words) >= overlap for old in seen):
            again.add(sent)
        else:
            seen.append(words)
    return again


def _drop_sentences(text: str, keep) -> str:
    paras = []
    for para in text.split("\n\n"):
        kept = [x for x in _sentences(para) if keep(x)]
        if kept:
            paras.append(" ".join(kept))
    return "\n\n".join(paras)


# what the posting itself asks a letter to say (Brattle: "Cover letter that describes your interest in economic and
# financial consulting and states your location preference")
_LETTER_ASK = re.compile(r"cover letter[^.\n]{0,20}?\b(?:that|which|describing|explaining|stating|outlining|addressing|"
                         r"including|detailing|indicating|should|must)\b([^.\n]{10,260})", re.I)


def letter_asks(page_text: str) -> str:
    m = _LETTER_ASK.search(page_text or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def _i_starts(text: str) -> float:
    sents = [x for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]
    return sum(1 for x in sents if re.match(r"I\b", x.strip())) / max(1, len(sents))


# stock phrases said plainly, so the sentence (often the one saying why this company) can stay
_PLAIN = [
    (r"\baligns? (?:well |closely |perfectly )?with\b", lambda m: "fits" if m.group(0).lower().startswith("aligns") else "fit"),
    (r"\baligned (?:well |closely )?with\b", "suited to"),
    (r"\bresonates? with me\b", "matters to me"), (r"\bresonates with\b", "fits"), (r"\bresonate with\b", "fit"),
    (r"\bresonates?\b", "matters"),
    (r"\bpassionate about\b", "interested in"), (r"\bpassion for\b", "interest in"),
    (r"\b(?:I am|I’m|I'm) (?:excited|thrilled) (?:about|by) (how|what|why|the way)\b", r"I'm interested in \1"),
    (r"\b(?:I am|I’m|I'm) (?:excited|thrilled) (?:about|by)\b", "I look forward to"),
    (r"\b(?:I am|I’m|I'm) (?:excited|thrilled)\b", "I would be glad"), (r"\b(?:excited|thrilled) (?:about|by)\b", "looking forward to"),
    (r"\b(?:excited|thrilled)\b", "glad"),
    (r"\bleveraging\b", "using"), (r"\bleveraged\b", "used"), (r"\bleverage\b", "use"),
    (r"\bdrawn to\b", "interested in"),
]


def _plainer(text: str, company: str = "") -> str:
    for pat, rep in _PLAIN:
        text = re.sub(pat, rep, text, flags=re.I)
    if company:
        text = re.sub(r"\byour company\b", company, text, flags=re.I)
    return no_dashes(re.sub(r"  +", " ", text))


def write_cover_letter(client: LLMClient, profile: Profile, company: str, title: str, page_text: str,
                       instruction: str = "") -> Draft:
    """The letter's body, four paragraphs, by the Career Center's guide. One model call, and a second only for what
    makes a letter untrue (a number, a claim or a referral not in the applicant's facts); smaller problems (a stock
    sentence) are fixed by leaving that sentence out. With the local model a call takes about a minute (it once
    took six minutes of rewrites for word counts and stock phrases, 2026-10-07)."""
    from .answers import SYSTEM, _CLICHES, _clean_output, build_prompt, job_excerpt, unsupported_numbers

    role = f"the {title} role" if title else "this role"
    at = f" at {company}" if company else ""
    question = f"Write the body of my cover letter for {role}{at}. No greeting, no sign-off, no headings."
    hint = f"about {WORDS} words, 4 paragraphs"
    name = f"Call the company '{company}' by name, never 'your company'. " if company else ""
    words = _posting_words(profile, page_text, title)
    use = f"The posting's own keywords my experience shows (use 3-5 of them naturally, never as a list): {words}. " if words else ""
    asks = letter_asks(page_text)
    must = f"The posting asks for: \"{asks}\" Do exactly that. " if asks else ""
    if asks and re.search(r"location|office|city", asks, re.I) and profile.location_preferences:
        must += (f"My office preferences, in order: {', '.join(profile.location_preferences[:3])} (offices I would "
                 f"like to work in; not where I live). ")
    note = (f"{GUIDE} {name}{use}{must}My degrees, stated exactly: {_degrees(profile)}. "
            f"MY NOTE: {instruction or '(none)'}").strip()
    excerpt = job_excerpt(page_text)
    facts = profile.facts_text() + "\n" + instruction
    allowed = f"{facts}\n{excerpt}\n{question}"
    prompt = build_prompt(profile, question, excerpt, None, note, "", hint, as_points=False)

    def untrue(text: str) -> list[str]:
        out = [f"numbers not in my facts: {', '.join(bad)}" for bad in [unsupported_numbers(text, allowed)] if bad]
        if claims := unsupported_claims(text, facts):
            out.append("claims my facts do not show: " + ", ".join(claims))
        if _CONTACT.search(text) and not _CONTACT.search(instruction):
            out.append("a referral or contact I never gave")
        return out

    text = _clean_output(client.complete(prompt, system=SYSTEM, max_tokens=900, temperature=0.4))
    if problems := untrue(text):
        text = _clean_output(client.complete(
            f"{prompt}\n\nYOUR PREVIOUS LETTER:\n{text}\n\nWrite it again without: " + "; ".join(problems) +
            ". Say only what APPLICANT FACTS show. /no_think", system=SYSTEM, max_tokens=900, temperature=0.2))
    warnings: list[str] = []
    if "\n\n" not in text and text.count("\n") >= 2:
        text = re.sub(r"\n+", "\n\n", text)  # paragraphs on single lines
    if bad := unsupported_numbers(text, allowed):
        text = _drop_sentences(text, lambda x: not unsupported_numbers(x, allowed))
        warnings.append(f"removed a sentence using numbers not in your resume: {', '.join(bad)}")
    if _CONTACT.search(text) and not _CONTACT.search(instruction):
        # a referral or contact the person never gave is not written, whatever the model does: those sentences go
        text = _drop_sentences(text, lambda x: not _CONTACT.search(x))
        warnings.append("removed a referral or contact you did not give")
    if claims := unsupported_claims(text, facts):
        # still claiming something the facts do not show: those sentences go (a shorter letter beats a false one)
        text = _drop_sentences(text, lambda x: not unsupported_claims(x, facts))
        warnings.append("removed sentences claiming what your resume does not show: " + ", ".join(claims))
    if (again := _repeats(text)):
        # qwen3:8b sometimes says one thing twice ("My background in Python, Excel..." / "My work with Python, Excel...")
        text = _drop_sentences(text, lambda x: x not in again)
        warnings.append("removed a sentence that repeated an earlier one")
    text = _plainer(text, company)
    home = (profile.city or "").lower()
    lives = re.compile(r"\b(?:I am|I’m|I'm) (?:currently )?(?:based|located|living) in\b|\bI live in\b", re.I)
    if any(lives.search(x) and (not home or home not in x.lower()) for x in _sentences(text)):
        # where the person lives is a fact: only their own city from My info (office preferences are not it)
        text = _drop_sentences(text, lambda x: not (lives.search(x) and (not home or home not in x.lower())))
        warnings.append("removed a sentence about where you live")
    if (stock := _GENERIC.search(text) or _CLICHES.search(text)):
        text = _drop_sentences(text, lambda x: not (_GENERIC.search(x) or _CLICHES.search(x)))
        warnings.append(f"removed a stock sentence ('{stock.group(0)}')")
    if len(text.split()) < 120:
        warnings.append("the letter came out short; add a paragraph of your own")
    return Draft(text=text.strip(), warnings=warnings)


def letter_html(profile: Profile, body: str, company: str = "") -> str:
    contact = " · ".join(x for x in (profile.email, profile.phone, profile.linkedin_url or "") if x)
    paras = "".join(f"<p>{html.escape(p.strip())}</p>" for p in re.split(r"\n\s*\n|\n", body) if p.strip())
    greeting = (("Dear Hiring Team at " + html.escape(company) + "," if company.lower().startswith("the ") else
                 f"Dear {html.escape(company)} Hiring Team,") if company else "Dear Hiring Team,")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: Letter; margin: 0.75in; }}  /* Career Center: 0.5-1 in margins, one page */
body {{ font: 11pt/1.45 Tinos, 'Times New Roman', serif; color: #111; margin: 0; }}
h1 {{ font-size: 15pt; margin: 0 0 2px; }} .contact {{ font-size: 9.5pt; color: #444; margin-bottom: 22px; }}
p {{ margin: 0 0 11px; }} .date {{ margin-bottom: 16px; }}
</style></head><body>
<h1>{html.escape(profile.full_name)}</h1><div class="contact">{html.escape(contact)}</div>
<div class="date">{date.today().strftime("%B %-d, %Y")}</div>
<p>{greeting}</p>{paras}<p>Sincerely,<br>{html.escape(profile.full_name)}</p>
</body></html>"""


def render_letter_pdf(page_html: str, out_pdf: Path, work_dir: Path) -> Path:
    from playwright.sync_api import sync_playwright  # late: the server sets the browser path first

    work_dir.mkdir(parents=True, exist_ok=True)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    src = work_dir / "cover_letter.html"
    src.write_text(page_html, encoding="utf-8")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(src.as_uri())
            page.pdf(path=str(out_pdf), prefer_css_page_size=True)
        finally:
            browser.close()
    return out_pdf

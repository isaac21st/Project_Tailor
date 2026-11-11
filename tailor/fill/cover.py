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

WORDS = 240  # three short paragraphs, then a plain close: a page a person will actually read

# University of Michigan Career Center, "Cover Letter Resources" (the person's chosen guide, 2026-10-06):
# 3-4 paragraphs; intro and interest (no generic opening, a referral if there is one); experiences tied to the job
# description with specific examples and results, in the posting's language, not the resume repeated; a close that
# sums up interest, thanks them, and may name the mission. Enthusiastic, confident but not imposing; no cliches; not
# every sentence starting with "I". One page. AI helps draft and tailor; the person personalizes and reviews it.
GUIDE = ("Write like one real person writing to one recruiter, not like a template. Keep the Career Center's shape: why this "
         "job, then what I've done that bears on it, in three short paragraphs. "
         "Paragraph 1, two or three sentences: open with one specific thing about this job from the posting (a problem the team "
         "owns, a kind of work, who it serves) and why it interests me. Do not open with my name, my degrees, the company's "
         "mission statement or praise. Mention a referral ONLY if my note below names one; never invent a referral, a "
         "professor, a conversation or any contact with the company. "
         "Paragraphs 2 and 3: one story each, the two experiences in APPLICANT FACTS, naming the place. Say what the "
         "situation was, what I did and what happened, retold in fresh words as if explaining it to someone across a table. "
         "Do not copy resume wording. Add nothing to a story: no tool, result, date, or claim that anyone still uses "
         "something unless that same story says so. Then say in one plain sentence what is the same about this job, in "
         "your own words and different each time. Only link to work the story really shows: never say it prepared me for a "
         "tool or task when the link is a stretch. "
         "Write no closing paragraph and no thanks; those are added after. "
         "Use contractions. Mix short sentences with longer ones. Don't begin a paragraph with 'At' or 'In my role as', and "
         "don't start most sentences with 'I'. Never use these: honed, sharpened, this experience, directly, translates, "
         "mirrors, demonstrates, my ability to, my capacity to, critical skill, key skill, essential, meaningful, "
         "actionable, insights, shape the future, commitment to, dedication to, I believe, look forward, drive impact. "
         "Separate paragraphs with a blank line. Never use em dashes or en dashes; use commas, periods or 'and'.")
# What a machine-written letter sounds like (the person, 2026-11-10: "less robotic or gptish, more human")
_ROBOT = re.compile(r"\bhon(?:ed|ing)\b|\bsharpen(?:ed|ing)\b|\bthis (?:experience|role|work) (?:taught|showed|gave|helped|allowed|required)|"
                    r"\b(?:this|that) experience\b|\bdirectly (?:ties|tied|translates?|supports?|applies|applicable|impacts?|relevant)|"
                    r"\btranslates? (?:directly|well|into)|\b(?:critical|key|essential|crucial|vital) (?:skill|part|asset|component)|"
                    r"\bskills? (?:that )?(?:are|is|will be) (?:essential|critical|key)|\bmirrors?\b|\bdemonstrat(?:es|ing|ed) my\b|"
                    r"\bmy (?:ability|capacity) to\b|\bmeaningful(?:ly)?\b|\bactionable\b|\bshap(?:e|ing) the future\b|"
                    r"\b(?:commitment|dedication) to\b|\bI believe\b|\blook forward to\b|\bdrive (?:meaningful )?impact\b|"
                    r"\bfits? (?:my|with my)\b|\bfoster(?:s|ing)?\b|\bin my role as\b|\bgraduate student pursuing\b|"
                    r"\bdual master|\bthank you for considering\b|\bcontribut(?:e|ing) to [A-Z]\S* mission\b|\bthrive\b|\bbring a\b|"
                    r"\bspeaks? to\b|\binsights?\b|\bdata-driven\b|\bdrive (?:growth|outcomes)\b|\bimprove outcomes\b|\bstrategic thinking\b", re.I)
# More than a story can show (2026-11-10, the first plain-language letters: "they're using it now", "the last few years")
_EMBELLISH = re.compile(r"\bstill (?:use|using)\b|\busing it (?:now|today)\b|\bto this day\b|\b(?:last|past) (?:few|several|\d+) years\b|"
                        r"\byears? of\b|\btop (?:third|half|quarter)\b|\bdecisions faster\b|\breal results\b|\bexactly the kind\b", re.I)
_NAME_OPENING = re.compile(r"^(?:I am|I['’]m|My name is)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s*,")
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


def _stiff(text: str) -> list[str]:
    """Sentences that read as generated: stock bridges, and the "I'm <name>, a student pursuing..." opening."""
    return [x for x in _sentences(text) if _ROBOT.search(x) or _NAME_OPENING.match(x)]


def _close(company: str) -> str:
    """Written here, not by the model: its closings all said the same empty thing about the company's mission."""
    team = f"what the team at {company} is working on" if company else "what the team is working on"
    return f"I'd like to hear more about {team}, and I'm glad to talk whenever it's useful. Thanks for reading."


def school_sentence(profile: Profile) -> str:
    """Where the person studies, said once and exactly, so the model never recites or garbles the degrees."""
    now = [e for e in profile.education if e.expected]
    if not now:
        return ""
    names = []
    for e in now:
        area = re.search(r"School of ([A-Z]\w+)$", e.school)
        names.append("an MBA" if "business administration" in e.degree.lower() else
                     f"a {e.degree}" + (f" in {area[1]}" if area and area[1] != "Business" else ""))
    place = re.split(r",| School of", now[0].school)[0].strip()
    place = f"the {place}" if place.startswith("University") else place
    return f"I'm finishing {' and '.join(names)} at {place}."


def pick_stories(profile: Profile, page_text: str, title: str, prefer: tuple[str, ...] = (), n: int = 2) -> list:
    """The experiences closest to this posting, one per place: chosen here, because left to itself the model skipped the
    hospital project on a healthcare role. Stories the person's target for this kind of role names count for more."""
    from ..jobs import clean_job_text
    from ..resume.auto import _relevance, job_concepts

    vec = job_concepts(clean_job_text(page_text or ""), title)[0]
    best: dict[str, tuple[float, object]] = {}
    for x in profile.experience:
        if not x.bullets:
            continue
        lift = 1.3 if any(p.lower() in x.employer.lower() for p in prefer) else 1.0
        score = _relevance(vec, " ".join([x.title, *x.bullets])) * lift
        if x.employer not in best or score > best[x.employer][0]:
            best[x.employer] = (score, x)
    return [x for _, x in sorted(best.values(), key=lambda kv: -kv[0])[:n]]


def write_cover_letter(client: LLMClient, profile: Profile, company: str, title: str, page_text: str,
                       instruction: str = "", prefer: tuple[str, ...] = ()) -> Draft:
    """The letter's body, four paragraphs, by the Career Center's guide. One model call, and a second only for what
    makes a letter untrue (a number, a claim or a referral not in the applicant's facts); smaller problems (a stock
    sentence) are fixed by leaving that sentence out. With the local model a call takes about a minute (it once
    took six minutes of rewrites for word counts and stock phrases, 2026-10-07)."""
    from .answers import SYSTEM, _CLICHES, _clean_output, _drop_misattributed, build_prompt, job_excerpt, misattributed, unsupported_numbers

    role = f"the {title} role" if title else "this role"
    at = f" at {company}" if company else ""
    question = f"Write the body of my cover letter for {role}{at}. No greeting, no sign-off, no headings."
    hint = f"about {WORDS} words, 3 paragraphs"
    name = f"Call the company '{company}' by name, never 'your company'. " if company else ""
    words = _posting_words(profile, page_text, title)
    use = f"The posting's own keywords my experience shows (use 3-5 of them naturally, never as a list): {words}. " if words else ""
    asks = letter_asks(page_text)
    must = f"The posting asks for: \"{asks}\" Do exactly that. " if asks else ""
    if asks and re.search(r"location|office|city", asks, re.I) and profile.location_preferences:
        must += (f"My office preferences, in order: {', '.join(profile.location_preferences[:3])} (offices I would "
                 f"like to work in; not where I live). ")
    school = school_sentence(profile)
    say_school = f"Use this sentence, word for word, in the first paragraph: \"{school}\" " if school else ""
    note = f"{GUIDE} {name}{use}{must}{say_school}MY NOTE: {instruction or '(none)'}".strip()
    excerpt = job_excerpt(page_text)
    # the model sees only the two stories: what it is not shown it cannot attach to the wrong job
    stories = pick_stories(profile, page_text, title, prefer) or profile.experience
    told = profile.model_copy(update={"experience": stories, "skills": []})
    facts = told.facts_text() + "\n" + instruction
    allowed = f"{facts}\n{excerpt}\n{question}"
    prompt = build_prompt(told, question, excerpt, None, note, "", hint, as_points=False)

    def untrue(text: str) -> list[str]:
        out = [f"numbers not in my facts: {', '.join(bad)}" for bad in [unsupported_numbers(text, allowed)] if bad]
        if claims := unsupported_claims(text, facts):
            out.append("claims my facts do not show: " + ", ".join(claims))
        if moved := misattributed(text, profile, instruction, excerpt):
            out.append("one place given another's results: " + "; ".join(f"{n} is from {home}, not {name}" for n, name, home in moved))
        if _CONTACT.search(text) and not _CONTACT.search(instruction):
            out.append("a referral or contact I never gave")
        if over := _EMBELLISH.search(text):
            out.append(f"more than the stories say ('{over.group(0)}')")
        return out

    text = _clean_output(client.complete(prompt, system=SYSTEM, max_tokens=900, temperature=0.6))
    if problems := untrue(text):
        text = _clean_output(client.complete(
            f"{prompt}\n\nYOUR PREVIOUS LETTER:\n{text}\n\nWrite it again without: " + "; ".join(problems) +
            ". Say only what APPLICANT FACTS show. /no_think", system=SYSTEM, max_tokens=900, temperature=0.2))
    if stiff := _stiff(text):  # one more try in plain words; the checks below still apply to what comes back
        text = _clean_output(client.complete(
            f"{prompt}\n\nYOUR PREVIOUS LETTER:\n{text}\n\nThese sentences sound machine-written:\n" +
            "\n".join(f"- {x}" for x in stiff) + "\nRewrite the letter with the same facts, in plain spoken words, the way a "
            "person talks. Only what APPLICANT FACTS show. /no_think", system=SYSTEM, max_tokens=900, temperature=0.6))
    warnings: list[str] = []
    if "\n\n" not in text and text.count("\n") >= 2:
        text = re.sub(r"\n+", "\n\n", text)  # paragraphs on single lines
    if bad := unsupported_numbers(text, allowed):
        text = _drop_sentences(text, lambda x: not unsupported_numbers(x, allowed))
        warnings.append(f"removed a sentence using numbers not in your resume: {', '.join(bad)}")
    if moved := misattributed(text, profile, instruction, excerpt):
        # paragraph by paragraph, so the letter keeps its paragraphs
        text = "\n\n".join(_drop_misattributed(par, profile, instruction, excerpt) for par in text.split("\n\n")).strip()
        warnings.append("removed a sentence giving one place another's results: " +
                        "; ".join(f"{n} is from {home}, not {name}" for n, name, home in moved))
    if _CONTACT.search(text) and not _CONTACT.search(instruction):
        # a referral or contact the person never gave is not written, whatever the model does: those sentences go
        text = _drop_sentences(text, lambda x: not _CONTACT.search(x))
        warnings.append("removed a referral or contact you did not give")
    if claims := unsupported_claims(text, facts):
        # still claiming something the facts do not show: those sentences go (a shorter letter beats a false one)
        text = _drop_sentences(text, lambda x: not unsupported_claims(x, facts))
        warnings.append("removed sentences claiming what your resume does not show: " + ", ".join(claims))
    if over := _EMBELLISH.search(text):
        text = _drop_sentences(text, lambda x: not _EMBELLISH.search(x))
        warnings.append(f"removed a sentence saying more than your resume does ('{over.group(0)}')")
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
    if (left := _stiff(text)) and len(text.split()) - sum(len(x.split()) for x in left) >= 130:
        text = _drop_sentences(text, lambda x: x not in left)  # a shorter letter beats one that reads as generated
        warnings.append("removed a sentence that read as machine-written")
    if len(text.split()) < 120:
        warnings.append("the letter came out short; add a paragraph of your own")
    return Draft(text=f"{text.strip()}\n\n{_close(company)}", warnings=warnings)


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
<div class="date">{date.today():%B} {date.today().day}, {date.today().year}</div>
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

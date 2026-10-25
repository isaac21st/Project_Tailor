"""Questions the assistant must never answer. It flags them for the person instead.

The person decided: work authorization, sponsorship, demographics/EEO, salary, legal attestations, background
questions, government IDs, credentials and bot checks are theirs to answer. This runs before every other rule, so
even a value that exists in the profile is not filled into one of these fields.
"""
from __future__ import annotations

import re

from .forms import FormField

_PATTERNS: dict[str, list[str]] = {
    "work_authorization": [
        r"authori[sz]ed to work", r"legally (?:authori[sz]ed|eligible|permitted)", r"work authori[sz]ation",
        r"right to work", r"eligible to work", r"employment eligibility", r"work eligibility", r"work permit", r"citizen",
    ],
    "sponsorship": [r"sponsor", r"\bvisa\b", r"\bh-?1b\b", r"immigration"],
    "demographics": [
        r"\bgender\b", r"\bsex\b", r"\brace\b", r"ethnic", r"hispanic", r"latino", r"veteran", r"disabilit",
        r"sexual orientation", r"\bpronouns?\b", r"transgender", r"self[- ]identif", r"\bmarital\b", r"protected class",
        r"served in the (?:u\.?s\.? )?(?:military|armed forces)", r"military (?:service|status)", r"\bactive duty\b",
    ],
    "compensation": [
        r"\b(?:salary|compensation|pay|wage)s?\b.*\b(?:expect|desired|requirement|range|minimum|target)",
        r"\b(?:desired|expected|target|current)\b.*\b(?:salary|compensation|pay)\b", r"salary expectation",
    ],
    "background": [r"convict", r"felony", r"criminal", r"background (?:check|screen|investigation)", r"drug test"],
    "legal_attestation": [
        r"\bi (?:certify|attest|acknowledge|agree|consent|authori[sz]e|understand|confirm|declare)\b",
        r"terms (?:and|&) conditions", r"terms of (?:use|service)", r"privacy (?:notice|policy|statement)",
        r"e-?signature", r"electronic signature", r"\bsignature\b", r"\bconsent\b", r"\bat least 1[68] years\b",
        r"\b(?:over|above) (?:the age of )?1[68]\b", r"\b1[68] years (?:of age )?or older\b", r"non[- ]?compete",
        r"acknowledg(?:e)?ment", r"receive (?:communications|messages|texts|updates)|\bsms\b|whatsapp|text messages",
    ],
    "government_id": [
        r"social security", r"\bssn\b", r"passport", r"driver'?s licen[sc]e", r"date of birth", r"\bdob\b",
        r"birth ?date", r"national id", r"tax id",
    ],
    "credentials": [r"\bpassword\b", r"create (?:an )?account", r"security (?:question|answer)", r"one[- ]time (?:code|pass)"],
    "bot_check": [r"captcha", r"i'?m not a robot", r"verify you are human"],
}
_COMPILED = {cat: [re.compile(p, re.I) for p in pats] for cat, pats in _PATTERNS.items()}
# a name box whose helper text says "as on your passport or driver's license" is still just a name
_NAME_FIELD = re.compile(r"\b(?:first|middle|last|given|family|full|legal|preferred)[ _-]?name\b|\bsurname\b|\bsuffix\b", re.I)


_NEEDS_SPONSOR = re.compile(r"\b(?:require|need)s?\b[^?.]{0,60}\bsponsor|\bsponsor\w*\b[^?.]{0,30}\b(?:required|needed)\b")


# The government's self-identification forms ask "Please check one of the boxes below:": what they are about is only in
# their answers ("Yes, I have a disability...", "I am not a protected veteran")
_SELF_ID_OPTION = re.compile(r"i have a disability|have not had (?:one|a disability)|protected veteran|not a veteran|"
                             r"hispanic or latino|two or more races|decline to self[- ]identify", re.I)


def classify(field: FormField) -> str | None:
    """The sensitive category of a field, or None if it is an ordinary question."""
    if field.type == "password":
        return "credentials"
    if any(_SELF_ID_OPTION.search(o.label or o.value or "") for o in (field.options or [])):
        return "demographics"
    own = re.sub(r"\s+", " ", " ".join([field.label, field.name, field.placeholder]).lower())
    text = re.sub(r"\s+", " ", " ".join([field.label, field.context, field.name, field.placeholder]).lower())
    # "Will you require sponsorship to retain your work authorization?" asks about sponsorship (answer: No), even though
    # it says "work authorization"; "authorized to work without sponsorship?" stays a work-authorization question.
    if _NEEDS_SPONSOR.search(own or text) or (not own.strip() and _NEEDS_SPONSOR.search(text)):
        return "sponsorship"
    for category, patterns in _COMPILED.items():
        if any(p.search(text) for p in patterns):
            if category == "government_id" and _NAME_FIELD.search(own) and not any(p.search(own) for p in patterns):
                continue  # only the helper text mentions an ID document
            return category
    return None


def classify_text(question: str) -> str | None:
    """Same check for a bare question string (used before any model is asked to draft an answer)."""
    return classify(FormField(id="q", label=question))

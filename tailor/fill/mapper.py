"""Decide, field by field, what to do with an application form.

Order of decisions (first match wins):
  1. sensitive question             -> needs_you   (never filled, even if the profile has a value), unless the person
                                       saved an answer scoped to THIS application (answer sheet "only_for"); a few
                                       kinds (consent, government ID, bot checks) are never filled at all. Facts about
                                       the person that never change between applications (self-ID answers, and work
                                       authorization when the question itself names the U.S.) can be written once in the
                                       "standing answers" file; nothing else is ever assumed
  2. resume upload                  -> upload
  3. a value the profile knows      -> fill / select / check
  4. an answer the person saved     -> fill / select
  5. a long free-text question      -> draft       (a model may write it; the person reviews it)
  6. anything else                  -> needs_you if required, otherwise skip (left empty)

Nothing is guessed: a required field the profile cannot answer is flagged, not invented.
"""
from __future__ import annotations

import re

from .forms import Action, FieldOption, FormField
from .profile import MONTHS, Education, Experience, Profile, YearMonth
from .answers import BEYOND_RESUME, BEYOND_RESUME_ASK
from .sensitive import classify

_AUTOCOMPLETE = {
    "given-name": "first_name", "family-name": "last_name", "name": "full_name", "email": "email", "tel": "phone",
    "tel-national": "phone", "url": "website", "address-level2": "city", "address-level1": "state",
    "country-name": "country", "country": "country", "postal-code": "postal_code", "street-address": "address",
    "address-line1": "address", "organization": "current_company", "organization-title": "current_title",
}

# (key, pattern) for fields outside education/experience sections. Specific rules come first.
_GENERAL = [
    # someone who referred the person: never filled from the profile (Cicero, 2026-10-13: "Who referred you to this
    # position? Enter their first and last name here." got the person's own last name)
    ("referrer", r"who referred you|referred by|\breferr(?:er|al)'?s? (?:name|first|last|email)|name of (?:your |the )?referr|"
                 r"employee (?:who )?referr"),
    ("available_start", r"when (?:can|could|would|will) you (?:start|begin|be available)|available to (?:start|begin)|"
                        r"earliest (?:possible )?(?:start|availab)|availability (?:to start|date)|desired start date|"
                        r"^start date$|when will you be available to (?:start|work)"),
    # ranked office choices (Stripe): the person's order, from My info
    ("location_pref_1", r"\bfirst (?:location|office) (?:preference|choice)|(?:location|office) (?:preference|choice) (?:#\s*)?(?:1|one)\b|"
                        r"preferred (?:office )?location|preferred office"),
    ("location_pref_2", r"\bsecond (?:location|office) (?:preference|choice)|(?:location|office) (?:preference|choice) (?:#\s*)?(?:2|two)\b"),
    ("location_pref_3", r"\bthird (?:location|office) (?:preference|choice)|(?:location|office) (?:preference|choice) (?:#\s*)?(?:3|three)\b"),
    ("preferred_name", r"\b(?:preferred|nick) ?name\b"),
    # entered once on My info
    ("gpa", r"\bgpa\b|grade point average|cumulative (?:grade|average)"),
    ("hs_year", r"high school[^?]{0,40}(?:graduat|year|class)|(?:graduat|year)[^?]{0,30}high school"),
    ("languages", r"^languages?(?: spoken)?$|languages? (?:do )?you (?:speak|know)|what languages|(?:spoken )?language proficienc|"
                  r"languages? (?:other than english|spoken|you are fluent)|fluent in"),
    # Lever: "Which university are you currently attending or did you last attend?"
    ("current_school", r"(?:which|what) (?:university|school|college)\b|(?:university|school|college) (?:are you|do you|did you)"
                       r"[^?]{0,40}attend|currently attending|last attend|^(?:college|school) or university$|"
                       r"^university or (?:college|school)$"),  # Cicero (JazzHR, 2026-10-13): "College or University*"
    ("first_name", r"\b(?:first|given|fore) ?name\b"),
    ("last_name", r"\b(?:last|family|sur) ?name\b"),
    ("full_name", r"^(?:your |full |legal |candidate )?name$|\b(?:full|legal) name\b"),
    ("email", r"e ?mail"),
    ("phone_country_code", r"(?:country )?phone code|phone country code|country code|dialing code"),  # not the number
    ("phone_extension", r"\bextension\b|^ext\.?$"),  # Workday's "Phone Extension": not the phone number
    ("phone_type", r"phone (?:device )?type|type of phone|device type"),  # before "phone": Workday's "Phone Device Type"
    ("phone", r"\b(?:phone|mobile|cell|telephone)\b"),
    ("linkedin", r"linked ?in"),
    ("github", r"git ?hub"),
    ("how_heard", r"how did you (?:first |originally )?(?:hear|find|learn)|how you (?:first )?(?:heard|found|learned)|"
                  r"hear about (?:us|this)|heard about (?:us|this)|source of (?:application|referral)|"
                  r"how (?:did|do) you know about"),
    ("website", r"\b(?:portfolio|personal (?:web ?site|url)|web ?site|other (?:web ?site|url))\b|^url$"),
    ("current_company", r"\b(?:current|most recent|latest)(?: or (?:previous|past|most recent|last))? (?:company|employer|organi[sz]ation)\b"),
    ("current_title", r"\b(?:current|most recent|latest)(?: or (?:previous|past|most recent|last))? (?:job )?(?:title|position|role)\b"),
    ("grad_date", r"graduation date|expected graduation|when (?:do|will) you graduate|date of graduation|"
                  r"(?:are|will) you (?:be )?graduating|graduating (?:month|date|year|term)|graduation (?:month|year|term)"),
    ("city", r"^city$|\bcity\b|current location|^location$|\blocation \(city\)"),
    ("state", r"^state\b|\bprovince\b|\bstate/|(?:^| )state(?: province| region)?$"),  # "Legal Address - State"
    ("country", r"^country\b|(?:^| )country$|country of residence|country (?:where|in which) you (?:currently )?(?:reside|live)|"
                r"(?:which|what) country are you (?:applying|based|located)"),
    ("postal_code", r"\b(?:zip|postal)\b"),
    ("address", r"\b(?:street )?address(?: line 1)?\b"),
]
_EDUCATION = [
    ("edu_current", r"still (?:a )?student|currently (?:attending|enrolled)|i (?:currently )?attend"),
    ("gpa", r"\bgpa\b|grade point|overall result"),
    ("edu_field", r"discipline|field of study|\bmajor\b|area of study|concentration|\bsubject\b"),
    ("edu_status", r"degree status|status of (?:your )?degree|completion status|graduation status"),
    ("edu_degree", r"\bdegree\b|level of education|education level"),
    ("edu_start_month", r"(?:start|from|began|begin).*\bmonth\b"),
    ("edu_start_year", r"(?:start|from|began|begin).*\byear\b"),
    ("edu_start_date", r"^from$|\bstart"),
    ("edu_end_month", r"(?:graduat|end|to|completion).*\bmonth\b"),
    ("edu_end_year", r"(?:graduat|end|to|completion).*\byear\b|^year$"),
    ("edu_end_date", r"(?:graduat|end|to|completion).*\bdate\b|graduat|^to$"),
    ("edu_school", r"school|university|college|institution"),
]
_EXPERIENCE = [
    ("job_current", r"currently (?:work|employ)|i currently|\bpresent\b|current (?:job|position|role|employer)"),
    ("job_description", r"description|responsibilit|dut(?:y|ies)|summary|accomplishment"),
    ("job_start_month", r"(?:start|from).*\bmonth\b"),
    ("job_start_year", r"(?:start|from).*\byear\b"),
    ("job_end_month", r"(?:end|to).*\bmonth\b"),
    ("job_end_year", r"(?:end|to).*\byear\b"),
    ("job_start_date", r"^from\b|\bstart"),
    ("job_end_date", r"^to$|\bend\b"),
    ("job_title", r"^(?:job )?title$|\bposition\b|\brole\b|job title"),
    ("job_employer", r"company|employer|organi[sz]ation"),
    ("job_city", r"^city$|\bcity\b"),
    ("job_state", r"^state\b|\bprovince\b|\bstate/"),
    ("job_country", r"^country\b"),
    ("job_location", r"location"),
]
_RULES = {
    "": [(k, re.compile(p)) for k, p in _GENERAL],
    "education": [(k, re.compile(p)) for k, p in _EDUCATION] + [(k, re.compile(p)) for k, p in _GENERAL],
    "experience": [(k, re.compile(p)) for k, p in _EXPERIENCE] + [(k, re.compile(p)) for k, p in _GENERAL],
}
_FILE_RESUME = re.compile(r"\b(?:resume|cv|curriculum vitae)\b")
_QUESTION_START = re.compile(
    r"^(?:why|describe|tell|what|how|explain|share|give|please (?:describe|tell|explain|share)|in your own words)\b")
_DEGREE_WORDS = {
    "master of business administration": ["mba", "master of business administration", "masters", "master s", "graduate"],
    "master of science": ["master of science", "ms", "m s", "masters", "master s", "graduate"],
    "master of arts": ["master of arts", "ma", "m a", "masters", "master s", "graduate"],
    "bachelor of arts": ["bachelor of arts", "ba", "b a", "bachelors", "bachelor s", "undergraduate"],
    "bachelor of science": ["bachelor of science", "bs", "b s", "bachelors", "bachelor s", "undergraduate"],
}


def norm(s: str) -> str:
    s = s.lower().replace("*", " ")
    s = re.sub(r"\((?:required|optional)\)", " ", s)
    s = re.sub(r"[^a-z0-9/+& ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _IS_QUESTION(label: str) -> bool:
    text = (label or "").replace("*", "").strip()
    if re.fullmatch(r"still (?:a )?student\??", text, re.I):  # Ashby's per-school checkbox, not a question
        return False
    return "?" in text or len(text) > 60


def _address_part(field: FormField) -> str | None:
    """One box of an address group the page labels only "Address" (JazzHR, 2026-10-13: City, State/Province and Postal
    boxes under one "Address" label all got the street): the box's own placeholder or name says which part it is."""
    own = f"{norm(field.placeholder)} {norm(field.name)}"
    for key in ("city", "state", "postal_code", "country"):
        if dict(_GENERAL)[key] and re.search(dict(_GENERAL)[key], norm(field.placeholder) or own):
            return key
    return None


def resolve_key(field: FormField) -> str | None:
    if field.autocomplete in _AUTOCOMPLETE and field.section == "":
        return _AUTOCOMPLETE[field.autocomplete]
    label = norm(field.label) or norm(field.name) or norm(field.placeholder)
    section = field.section
    if section and _IS_QUESTION(field.label):
        # an entry's fields are short labels ("Company", "End date month"); a full question is never one of them,
        # whatever section the page seemed to put it in (Robinhood: "Do you have relationships with employees...")
        section = ""
    for key, pat in _RULES[section]:
        if pat.search(label):
            if key == "address" and (part := _address_part(field)):
                return part
            return key
    # unlabeled inputs: fall back to the element's name/placeholder
    if not norm(field.label):
        alt = f"{norm(field.name)} {norm(field.placeholder)}".strip()
        for key, pat in _RULES[section]:
            if pat.search(alt):
                return key
    return None


def _pick(seq: list, index: int):
    return seq[index] if 0 <= index < len(seq) else None


def _date_text(ym: YearMonth | None, field: FormField) -> str | None:
    if ym is None:
        return None
    if field.type == "month" and ym.month:
        return f"{ym.year}-{ym.month:02d}"
    if field.type == "date" and ym.month:
        return f"{ym.year}-{ym.month:02d}-01"
    if "mm/yyyy" in field.placeholder.lower() and ym.month:
        return f"{ym.month:02d}/{ym.year}"
    if ym.month:
        return f"{list(MONTHS)[ym.month - 1].title()} {ym.year}"
    return str(ym.year)


_MONTH_NAMES_TITLE = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                      "November", "December"]


def _start_text(iso: str | None, field: FormField) -> str | None:
    """The earliest start date, in the form the field wants: 2027-05-01 for a date box, "May 1, 2027" for text."""
    if not iso or not (m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso)):
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    if field.type == "date":
        return iso
    if field.type == "month":
        return f"{y}-{mo:02d}"
    if "mm/dd/yyyy" in field.placeholder.lower():
        return f"{mo:02d}/{d:02d}/{y}"
    return f"{_MONTH_NAMES_TITLE[mo - 1]} {d}, {y}"


_FULL_MONTHS = "|".join(m.lower() for m in _MONTH_NAMES_TITLE)
_STARTS_ON = re.compile(rf"(?:start|begin|commenc)\w*[^?]{{0,60}}?\b({_FULL_MONTHS})\s+(\d{{4}})|"
                        rf"\b({_FULL_MONTHS})\s+(\d{{4}})[^?]{{0,60}}?(?:start|begin)", re.I)


def _start_fact(field: FormField, profile: Profile) -> str | None:
    """"The program begins July 2027. Does this start date work for you?" / "Are you able to start in September
    2026?": Yes when the person can start by then (their earliest start date, My info), else No."""
    if not profile.available_start or "?" not in field.label:
        return None
    m = _STARTS_ON.search(field.label)
    if not m or not re.search(r"\b(?:work for you|able to|can you|could you|available|okay|ok)\b", field.label, re.I):
        return None
    month, year = (m[1], m[2]) if m[1] else (m[3], m[4])
    asked = (int(year), MONTHS[month.lower()])
    y, mo = (int(x) for x in profile.available_start.split("-")[:2])
    return "Yes" if (y, mo) <= asked else "No"


def _school_title(name: str) -> str:
    """"University of Michigan, Harbor School of Business" -> "University of Michigan"; a campus after the
    comma stays ("University of California, Davis")."""
    name = re.sub(r",[^,]*\bSchool of .*$", "", name)
    return re.sub(r"\s+School of .*$", "", name).strip()


_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def _place(location: str) -> tuple[str | None, str | None]:
    """"San Jose, CA" -> ("San Jose", "California"): job entries (Google, Workday) ask city and state apart."""
    city, _, state = (location or "").rpartition(",")
    state = state.strip()
    if not city.strip():
        return (state or None), None
    return city.strip(), _US_STATES.get(state.upper(), state or None)


def _edu_for(field: FormField, profile: Profile) -> Education | None:
    """The resume education line for this form entry: the one whose degree or school the site already shows there
    (Google fills entries from the resume, not in the resume's order); otherwise by position."""
    hint = (field.entry_hint or "").lower()
    if hint:
        def score(e: Education) -> int:
            s = 0
            deg = e.degree.lower()
            for word in ("master of business", "master of science", "bachelor of arts", "bachelor of science", "doctor"):
                if word in deg and word in hint:
                    s += 3
            if _school_title(e.school).lower() and _school_title(e.school).lower() in hint:
                s += 1
            return s
        best = max(profile.education, key=score, default=None)
        if best is not None and score(best) > 0:
            return best
    return _pick(profile.education, field.section_index)


def value_for(key: str, profile: Profile, field: FormField, job: Experience | None = None) -> str | None:
    """The profile's value for a key, or None if the profile does not have it. `job`: the resume job this work entry
    was matched to (see match_work_entries); without one, the n-th job for the n-th entry."""
    edu: Education | None = _edu_for(field, profile)
    exp: Experience | None = job or _pick([e for e in profile.experience if e.kind == "job"], field.section_index)
    cur = profile.current_job()
    digits = re.sub(r"\D", "", profile.phone)
    if key == "phone" and field.maxlength and field.maxlength <= 10:
        return digits[-10:]
    if key == "city" and field.type == "combobox" and profile.city and profile.state:
        return f"{profile.city}, {profile.state}"  # a places search: "Springfield" alone finds many Springfields
    simple = {
        "first_name": profile.first_name, "preferred_name": profile.preferred_name or profile.first_name,
        "last_name": profile.last_name, "phone_type": profile.phone_type, "how_heard": profile.how_heard,
        "full_name": profile.full_name, "email": profile.email, "phone": profile.phone,
        "linkedin": profile.linkedin_url, "website": profile.portfolio_url, "github": profile.github_url, "city": profile.city,
        "state": profile.state, "country": profile.country, "postal_code": profile.postal_code,
        "address": profile.address,
        # the GPA is the current degree's: never written into another school's entry
        "gpa": _gpa_for(field, profile, edu if field.section == "education" else None),
        "hs_year": profile.high_school_year,
        "languages": None if re.search(r"programming|coding|software", f"{field.label} {field.context}", re.I) else profile.languages,
        "available_start": _start_text(profile.available_start, field),
        **{f"location_pref_{i + 1}": p for i, p in enumerate(profile.location_preferences[:3])},
        "current_company": cur.employer if cur else None, "current_title": cur.title if cur else None,
        # a school search box wants the institution ("University of Michigan"), not the school within it
        "edu_school": (edu.school if field.type != "combobox" else _school_title(edu.school)) if edu else None,
        "edu_degree": edu.degree if edu else None,
        "edu_status": ("Now attending" if edu.expected else "Graduated") if edu else None,
        "edu_current": ("true" if edu.expected else "false") if edu else None,  # Ashby's "Still Student?"
        "current_school": profile.education[0].school if profile.education else None,
        "grad_date": _date_text(profile.education[0].end, field) if profile.education else None,
        "edu_field": (edu.form_field or edu.field) if edu else None,  # the person's pick for forms, else the resume's
        "edu_end_year": str(edu.end.year) if edu and edu.end else None,
        "edu_end_month": str(edu.end.month) if edu and edu.end and edu.end.month else None,
        "edu_end_date": _date_text(edu.end if edu else None, field),
        "edu_start_year": str(edu.start.year) if edu and edu.start else None,
        "edu_start_month": str(edu.start.month) if edu and edu.start and edu.start.month else None,
        "edu_start_date": _date_text(edu.start if edu else None, field),
        "job_title": exp.title if exp else None, "job_employer": exp.employer if exp else None,
        "job_location": exp.location if exp else None,
        "job_city": _place(exp.location)[0] if exp else None,
        "job_state": _place(exp.location)[1] if exp else None,
        "job_country": "United States" if exp and _place(exp.location)[1] else None,
        "job_description": " ".join(exp.bullets) if exp and exp.bullets else None,
        "job_start_year": str(exp.start.year) if exp and exp.start else None,
        "job_start_month": str(exp.start.month) if exp and exp.start and exp.start.month else None,
        "job_end_year": str(exp.end.year) if exp and exp.end else None,
        "job_end_month": str(exp.end.month) if exp and exp.end and exp.end.month else None,
        "job_start_date": _date_text(exp.start if exp else None, field),
        "job_end_date": _date_text(exp.end if exp else None, field),
        "job_current": ("true" if exp and exp.current else "false") if exp else None,
    }
    return simple.get(key)


def _tokens(s: str) -> set[str]:
    return set(norm(s).split())


def _gpa_for(field: FormField, profile: Profile, edu: Education | None) -> str | None:
    """The GPA a box asks for: an education entry's own (the person's per-school figures, else the current degree's
    GPA in the current degree's entry only); a question naming the undergraduate degree gets the bachelor's; any other
    GPA question gets the current (graduate) degree's."""
    def own(e: Education | None) -> str | None:
        return next((v for k, v in profile.education_gpas.items() if e and k.lower() in e.school.lower()), None)

    if edu is not None:
        return own(edu) or (profile.gpa if not profile.education or edu is profile.education[0] else None)
    if re.search(r"undergrad|bachelor", f"{field.label} {field.context}", re.I):
        under = next((e for e in profile.education if re.search(r"bachelor|\bb\.?[as]\b", e.degree, re.I)), None)
        return own(under)
    return profile.gpa


def _gpa_band(gpa: float, options: list[FieldOption]) -> FieldOption | None:
    """The option whose GPA range holds this GPA, for lists of ranges instead of numbers."""
    hits = []
    for o in options:
        lab = (o.label or "").lower().replace("out of 4.0", "").replace("/4.0", "")
        nums = [float(x) for x in re.findall(r"\d\.\d+|\b\d\b", lab)]
        if not nums:
            continue
        if re.search(r"or (?:above|higher|more|greater)|and (?:above|up)|\+|at least|minimum", lab):
            lo, hi = nums[0], 99.0
        elif re.search(r"below|under|less than|lower than", lab):
            lo, hi = -1.0, nums[0] - 1e-9
        elif len(nums) >= 2:
            lo, hi = min(nums[:2]), max(nums[:2])  # "3.89-3.80" lists the range high to low (Cicero, 2026-10-13)
            hi += 0.0099 if len(str(hi).split(".")[-1]) >= 2 else 0.099
        else:
            continue
        if lo <= gpa <= hi:
            hits.append(o)
    return hits[0] if len(hits) == 1 else None


def match_option(value: str, options: list[FieldOption], key: str = "") -> FieldOption | None:
    """Choose the option that means `value`: exact, then synonyms (degrees, months), then containment."""
    if key in ("edu_school", "current_school") and "\n" in value:  # "school\ncity": the city picks the campus
        return match_school(*value.split("\n", 1), options)
    v = norm(value)
    labelled = [(o, norm(o.label), norm(o.value)) for o in options]
    if key == "gpa" and re.fullmatch(r"\d(?:\.\d+)?", value.strip()) and (band := _gpa_band(float(value), options)):
        return band  # "3.6 or above (out of 4.0)" / "3.0 - 3.59" / "Below 3.0" (Databricks, 2026-10-10)
    if key.endswith("_month") and v.isdigit():
        # Month names in the labels decide first: option values are often 0-based indexes, not month numbers.
        n = int(v)
        name = list(MONTHS)[n - 1]  # the first 12 entries are the full month names, in order
        for o, lab, val in labelled:
            first = lab.split()[0] if lab else ""
            if first.isalpha() and first[:3] == name[:3]:
                return o
        for o, lab, val in labelled:
            if lab.lstrip("0") == str(n) or val.lstrip("0") == str(n):
                return o
        return None
    for o, lab, val in labelled:
        if v in (lab, val):
            return o
    if key in ("city", "job_city", "location"):
        return _match_place(value, labelled)
    city = next((c for c in _PLACE_ALIASES if v.startswith(c)), None)
    if city:
        hits = [o for o, lab, _ in labelled if any(re.search(rf"\b{re.escape(a)}\b", lab) for a in _PLACE_ALIASES[city])]
        if len(hits) == 1:
            return hits[0]
    if key in ("edu_school", "current_school"):
        # "University of California, Davis" = "University of California - Davis" = "UC Davis"
        short = lambda x: re.sub(r"^university of california\b", "uc", norm(x)).strip()  # noqa: E731
        same = [o for o, lab, _ in labelled if short(lab) == short(value)]
        if len(same) == 1:
            return same[0]
    if key in ("grad_date", "edu_end_date", "available_start") and (when := _month_year(v)):
        # a range that holds the date ("December 2026 - August 2027" for May 2027; DoorDash, 2026-10-07)
        inside = [o for o, lab, _ in labelled if (r := _range(lab)) and r[0] <= when <= r[1]]
        if len(inside) == 1:
            return inside[0]
        if key == "available_start":
            # the earliest start offered on or after the person's ("January / June / August 2027" for May 1: June)
            dated = sorted(((d, o) for o, lab, _ in labelled if (d := _month_year(lab)) and d >= when), key=lambda x: x[0])
            if dated and not any(_month_year(lab) == when for _, lab, _ in labelled):
                return dated[0][1]
    if key == "available_start" and (m := re.match(r"([a-z]+) \d{1,2} (\d{4})$", v)) and m[1] in MONTHS:
        # "May 1, 2027" in a list of terms: May to August is the summer start ("Summer 2027", "After graduation...")
        n, year = MONTHS[m[1]], m[2]
        same = [o for o, lab, _ in labelled if lab == f"{m[1]} {year}"]  # "May 2027"
        if len(same) == 1:
            return same[0]
        for o, lab, _ in labelled:  # "April - June 2027" (Keystone)
            r = re.fullmatch(r"([a-z]+) (?:to )?([a-z]+) (\d{4})", lab)
            if r and r[3] == year and r[1] in MONTHS and r[2] in MONTHS and MONTHS[r[1]] <= n <= MONTHS[r[2]]:
                return o
        term = "spring" if n <= 4 else "summer" if n <= 8 else "fall"
        hits = [o for o, lab, _ in labelled if term in lab and year in lab]
        if len(hits) == 1:
            return hits[0]
    if key in ("grad_date", "edu_end_date") and (m := re.match(r"([a-z]+) (\d{4})$", v)) and m[1] in MONTHS:
        # "May 2027" in a list of terms ("Fall 2026", "Spring 2027"): the term that month ends
        n = MONTHS[m[1]]
        term = "spring" if n <= 5 else "summer" if n <= 8 else "fall"
        hits = [o for o, lab, _ in labelled if lab == f"{term} {m[2]}"]
        if len(hits) == 1:
            return hits[0]
    if key == "edu_degree" or v in _DEGREE_WORDS:  # also a "highest degree" question answered with a degree name
        for word in _DEGREE_WORDS.get(v, []):
            for o, lab, val in labelled:
                if word in (lab, val) or (len(word) > 3 and word in lab):
                    return o
    if re.match(r"i am not |i m not |not a ", v) and any(re.match(r"no\b", lab) for _, lab, _ in labelled):
        v = "no"  # "I am not a protected veteran" answers "No, I am not a veteran..." / a plain Yes/No list
    if "veteran" in v and re.match(r"i am not ", v):
        # "I am not a protected veteran" (the person is not a veteran at all, 2026-09-30): "I am not a veteran", never
        # "I am a veteran but not a protected veteran"
        hits = [o for o, lab, _ in labelled if re.search(r"\bnot an? (?:protected )?veteran\b", lab)
                and not re.search(r"\bi am an? (?:\w+ )?veteran\b|\bidentify as\b", lab)]
        plain = [o for o in hits if re.search(r"\bnot an? veteran\b", norm(o.label))]
        if len(plain) == 1 or len(hits) == 1:
            return (plain or hits)[0]
        # a military-status list (Robinhood, 2026-10-07): "I have never served in the military"
        never = [o for o, lab, _ in labelled if re.search(r"never served|have not served|no (?:prior )?military (?:service|experience)|"
                                                          r"not (?:a )?(?:current or former )?(?:member of the )?military", lab)]
        if len(never) == 1:
            return never[0]
    if v in ("yes", "no"):
        # a list that spells the answer out ("No, I am not a veteran", "I have never worked at Robinhood"): only when
        # exactly one option says it, and never an "I don't wish to answer"
        starts = [o for o, lab, _ in labelled if re.match(rf"{v}\b", lab)]
        if len(starts) == 1:
            return starts[0]
        if v == "yes":  # "I am authorized to work for any employer..." (not "...requires sponsorship")
            said = [o for o, lab, _ in labelled if _SAYS_AUTHORIZED.search(lab) and not _NEEDS_HELP.search(lab)]
            if len(said) == 1 and not starts:
                return said[0]
        pat = _SAYS_NO if v == "no" else _SAYS_YES
        hits = [o for o, lab, _ in labelled if pat.search(lab) and not _DECLINES.search(lab)]
        return hits[0] if len(hits) == 1 and not starts else None
    for word in _SAME_AS.get(v, []):
        for o, lab, val in labelled:
            if word in (lab, val):
                return o
    if key == "edu_field" and "," in value:  # "Information, User Experience Research and Design": the major comes first
        head = match_option(value.split(",")[0], options, key)
        if head is not None:
            return head
    # containment: the most specific option ("Business Analytics" over "Business" for "Data and Business Analytics")
    inside = []
    for o, lab, val in labelled:
        if lab and len(lab) > 2 and (v in lab or re.search(rf"\b{re.escape(lab)}\b", v)):
            if len(v) <= 4 and not re.search(rf"\b{re.escape(v)}\b", lab):
                continue  # a short answer must be a whole word of the option, not part of one
            inside.append((o, lab))
    if inside:
        exact_words = [x for x in inside if x[1] in v]  # options the answer contains, longest first
        if key in ("edu_school", "current_school", "city", "job_city") and not exact_words and len(inside) > 1:
            return None  # "University of Michigan" in Ann Arbor, Dearborn and Flint: which campus is not a guess
        pool = exact_words or inside
        return max(pool, key=lambda x: len(x[1]))[0] if exact_words else min(pool, key=lambda x: len(x[1]))[0]
    return None


def _month_year(text: str) -> tuple[int, int] | None:
    """(year, month) of "may 2027" or "may 1 2027" (normalized text), else None."""
    m = re.fullmatch(r"([a-z]+)(?: \d{1,2})? (\d{4})", text.strip())
    return (int(m[2]), MONTHS[m[1]]) if m and m[1] in MONTHS else None


def _range(lab: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """The span of "december 2026 august 2027" / "december 2026 to august 2027" (normalized text)."""
    m = re.fullmatch(r"([a-z]+) (\d{4}) (?:to |through |until )?([a-z]+) (\d{4})", lab.strip())
    if not m or m[1] not in MONTHS or m[3] not in MONTHS:
        return None
    return (int(m[2]), MONTHS[m[1]]), (int(m[4]), MONTHS[m[3]])


def _match_place(value: str, labelled: list) -> FieldOption | None:
    """"Springfield, Illinois" -> "Springfield, IL, USA" (not "Springfield, MA, USA"): same city, same state."""
    city, _, state = (x.strip() for x in value.partition(","))
    if not state:  # no state to tell them apart: only a list with one such place
        same = [o for o, lab, _ in labelled if lab == norm(city) or lab.startswith(norm(city) + " ")]
        return same[0] if len({norm(o.label) for o in same}) == 1 else None
    names = {norm(state)}
    names |= {norm(code) for code, name in _US_STATES.items() if norm(name) == norm(state) or norm(code) == norm(state)}
    names |= {norm(name) for code, name in _US_STATES.items() if norm(code) == norm(state)}
    hits = [o for o, lab, _ in labelled
            if lab.startswith(norm(city) + " ") and any(re.search(rf"\b{re.escape(n)}\b", lab[len(norm(city)):]) for n in names if n)]
    return hits[0] if hits and all(norm(h.label) == norm(hits[0].label) for h in hits) else None


_SAYS_NO = re.compile(r"^no\b|\b(?:never|don t|do not|haven t|have not|am not|not an?)\b")
_DECLINES = re.compile(r"wish to (?:answer|disclose|say)|prefer not|decline|rather not|self.describe")
_SAYS_YES = re.compile(r"^yes\b")
_SAYS_AUTHORIZED = re.compile(r"^i am (?:legally )?(?:authori[sz]ed|eligible) to work\b")
_NEEDS_HELP = re.compile(r"sponsor|renewal|requires?|unknown|not authori|visa")
# the same answer in the words different lists use
_SAME_AS = {"male": ["man", "male", "cisgender man"], "female": ["woman", "female", "cisgender woman"],
            "man": ["male"], "woman": ["female"],
            "united states": ["us", "usa", "u s", "u s a", "united states of america"],
            "united kingdom": ["uk", "u k", "great britain"]}


_FACTUAL_QUESTION = re.compile(
    r"hear about|find out about|how did you (?:find|learn|hear)|referr|source|previously (?:work|appl)|"
    r"(?:ever|currently) (?:work|employ)|relocat|notice period|start date|available to start|relative|"
    r"family member|employee of|(?:current|previous|recent|last) (?:or \w+ )?(?:employer|job title|title)|"
    # facts about the person that no model can know
    r"pronounc|nickname|preferred name|what (?:should|would you like) (?:we|us to) call you|legal name|"
    r"date of birth|visa|citizenship|clearance")


_CATCH_ALL = re.compile(r"anything else|anything you.d like|additional (?:info|information|comments?)|"
                        r"^comments?$|^notes?$|other information|is there anything|message to")


# an upload box that wants the resume and the cover letter as one file (the text is normalized: lower case, no punctuation)
_TOGETHER = re.compile(r"(?:resume|cv) (?:and|&) (?:a )?cover letter[^.]{0,40}?(?:together|in one|in a single|combined|"
                       r"one (?:document|file|pdf))|(?:one|single|combined) (?:document|file|pdf)[^.]{0,40}(?:resume|cv)"
                       r"[^.]{0,15}cover letter")
_COVER = re.compile(r"cover\s*letter|cover_letter|coverletter|letter of (?:interest|motivation)|motivation(?:al)? letter", re.I)


def _wants_draft(field: FormField) -> bool:
    """Open-ended questions a model may draft. Factual ones (source, availability, history) are never drafted,
    and neither are optional catch-all boxes ("Anything else?"): leaving those empty is the right answer."""
    label = norm(field.label or field.context)
    if _FACTUAL_QUESTION.search(label) or field.type not in ("textarea", "text"):
        return False
    if _CATCH_ALL.search(label) and not field.required:
        return False
    if _QUESTION_START.match(label) or label.endswith("?") or "cover letter" in label:
        return True
    return field.type == "textarea" and field.required


# Sensitive kinds the person may answer for ONE application by writing a scoped entry in the answer sheet. Consent /
# attestation checkboxes, government IDs and bot checks are never filled, and neither is an <input type=password>.
_SCOPED_OK = {"work_authorization", "sponsorship", "demographics", "compensation", "background", "credentials"}


def _sheet_answer(field: FormField, sheet: dict | None, url: str, *, scoped_only: bool) -> str | None:
    """An answer the person saved for this question. An entry is a plain string (any site) or
    {"value": ..., "only_for": "<text that must appear in the page URL>"}; a scoped entry is used only on that
    application. Sensitive questions are answered only from scoped entries."""
    if not sheet:
        return None
    by_question = {norm(k): v for k, v in sheet.items()}
    for name in (norm(field.label), norm(field.context)):
        entry = by_question.get(name) if name else None
        if entry is None:
            continue
        if isinstance(entry, dict):
            only_for = str(entry.get("only_for", "")).strip().lower()
            if entry.get("value") is None or (scoped_only and not only_for) or (only_for and only_for not in (url or "").lower()):
                continue
            return str(entry["value"])
        if not scoped_only:
            return str(entry)
    return None


# Standing answers: things the person wrote down once because they are true on every application. Only the matching
# kind of question is answered, and only when the person actually wrote that answer. Order matters: the Hispanic/Latino
# question must win over the race/ethnicity one.
_STANDING = [
    ("hispanic_latino", re.compile(r"hispanic|latino|latinx")),
    ("race", re.compile(r"\brace\b|racial|ethnic")),
    ("disability", re.compile(r"disabilit")),
    ("veteran", re.compile(r"veteran|served in the (?:u ?s )?(?:military|armed forces)|military (?:service|status)|active duty")),
    ("gender", re.compile(r"\bgender\b")),
    ("transgender", re.compile(r"transgender|trans (?:woman|man|person)")),
    ("pronouns", re.compile(r"\bpronouns?\b")),  # not "pronounce" (Lever: "How do you pronounce your name?")
]
# Case-sensitive on purpose ("us" is also a pronoun). H-1B only ever appears in U.S. sponsorship questions.
_MENTIONS_US = re.compile(r"\bU\.?S\.?A?\b|United States|\bH-?1B\b")


# a question about another country's work rights is never answered with the U.S. answers
_OTHER_COUNTRY = re.compile(r"\b(?:Canada|Canadian|United Kingdom|U\.?K\.?|Britain|Ireland|India|Germany|France|"
                            r"Netherlands|Spain|Italy|Poland|Israel|Australia|New Zealand|Singapore|Japan|Korea|China|"
                            r"Hong Kong|Taiwan|Mexico|Brazil|Europe(?:an)?|EU|EEA|Switzerland|Sweden|UAE)\b")


_BACKGROUND_OK = re.compile(r"(?:willing|consent|agree|able|authori[sz]e)\w*\b[^?]{0,50}background (?:check|screen|investigation)|"
                            r"background (?:check|screen)\w*[^?]{0,50}\b(?:willing|consent|agree)", re.I)
_WORK_THERE = re.compile(r"relocat|work (?:on a daily basis |daily )?(?:in|at|from) the (?:work )?location|in[- ]person|"
                         r"work (?:in|at|from|out of) [^?]{0,40}\b(?:hubs?|offices?|locations?|campus)\b|"
                         r"on[- ]?site|in (?:the|our|one of our|their|your|a) (?:designated |local )?offices?|commut", re.I)


# how forms shorten the person's cities ("NYC office", "SF Hub")
_PLACE_ALIASES = {"san francisco": ["sf", "san francisco", "bay area"], "new york": ["nyc", "ny", "new york"],
                  "los angeles": ["la", "los angeles"], "washington": ["dc", "washington dc", "washington d c"],
                  "chicago": ["chicago", "chi"], "seattle": ["seattle"], "boston": ["boston"]}


def _expand_places(text: str) -> str:
    """The text with city abbreviations spelled out ("NYC or SF Hub" -> "... new york ... san francisco ...")."""
    t = norm(text)
    for city, aliases in _PLACE_ALIASES.items():
        for a in aliases:
            if re.search(rf"\b{re.escape(a)}\b", t):
                t += " " + city
    return t


def _location_fact(field: FormField, standing: dict | None, role_location: str) -> str | None:
    """"Can you work daily at this location, or relocate?" / "Open to relocation?" / "In an office 25% of the time?":
    Yes when the role is in one of the person's cities or remote (their rule, 2026-10-03); otherwise they answer."""
    cities = (standing or {}).get("relocation_ok_cities") or []
    if not cities or "?" not in field.label or not _WORK_THERE.search(field.label):
        return None
    if not role_location and not any(c.lower() in _expand_places(field.label) for c in cities):
        return None
    # how much in person: up to the person's limit (80%, i.e. 4 days a week, 2026-10-03); more comes to them
    limit = int((standing or {}).get("in_person_max_percent") or 100)
    q = field.label.lower()
    pct = [int(x) for x in re.findall(r"(\d{1,3})\s*%", q)]
    days = [int(x) for x in re.findall(r"\b([1-7])\s*(?:days?|x)\b(?:\s*(?:a|per|each)\s*week)?", q)]
    if any(x > limit for x in pct) or any(d * 20 > limit for d in days) or (
            limit < 100 and re.search(r"\bfully (?:on[- ]?site|in[- ]person|in (?:the )?office)\b|\bfull[- ]time (?:on[- ]?site|in office)", q)):
        return None
    # the question names the places itself ("Are you able to work from Figma's NYC or SF Hub?")
    named = _expand_places(field.label)
    if any(c.lower() in named for c in cities):
        return "Yes"
    loc = role_location.lower()
    from .profile import BAY_AREA
    bay = "san francisco" in {c.lower() for c in cities} and any(b.lower() in loc for b in BAY_AREA)
    if any(c.lower() in loc for c in cities) or bay or re.search(r"\bremote\b", loc):
        return "Yes"
    return None


def _standing_answer(field: FormField, category: str, standing: dict | None, us_role: bool = False) -> str | None:
    if not standing:
        return None
    raw = f"{field.label} {field.context}"
    # The person's rule (2026-10-02): on a role based in the U.S., "the country where the job is located" means the
    # U.S., so the U.S. answers apply. Only when the role is known to be in the U.S. and no other country is named.
    us = _MENTIONS_US.search(raw) or (us_role and standing.get("us_answers_for_us_roles") is True
                                      and not _OTHER_COUNTRY.search(raw))
    if category == "demographics":
        if field.type in ("text", "textarea") and not field.options and re.search(r"disabilit|condition|impairment", norm(raw)):
            return None  # "describe your disability": never written out from a standing answer; the person decides
        # the question's own words first: an EEO section's text around "Pronouns" also says gender and veteran (Stripe)
        hits = [key for key, pat in _STANDING if pat.search(norm(field.label))] or \
            [key for key, pat in _STANDING if pat.search(norm(raw))]
        if not hits:  # "Please check one of the boxes below:": the answers say which form this is
            opts = " ".join(norm(o.label or o.value or "") for o in field.options or [])
            hits = [key for key, pat in _STANDING if pat.search(opts)]
            if "hispanic_latino" in hits and "race" in hits:
                hits = ["race"]  # "White (Not Hispanic or Latino)": the race list
        if "hispanic_latino" in hits:  # "Hispanic or Latino ethnicity" is the Hispanic question, not the race one
            hits = [k for k in hits if k != "race"]
        if len(hits) != 1:  # nothing matched, or a combined question: the person answers
            return None
        value = standing.get(hits[0])
        return str(value) if value else None
    if category == "background" and _BACKGROUND_OK.search(raw):
        value = standing.get("background_check_ok")  # willing to / consent to a check: the person's standing answer
        return str(value) if value else None
    # "What is your current immigration / citizenship status?" with a list (U.S. Citizen, Permanent Resident, H-1B...):
    # the person is a U.S. citizen (standing answer), so that option, not a yes/no
    citizen = next((o for o in field.options or [] if re.search(r"\bcitizen\b", norm(o.label or o.value))
                    and not re.search(r"\bnon[- ]?citizen|not a (?:u ?s )?citizen", norm(o.label or o.value))), None)
    if category in ("sponsorship", "work_authorization") and citizen is not None and \
            re.search(r"status|citizenship|best describes|which of the following", raw, re.I) and \
            str(standing.get("us_citizen", "")).lower() == "yes" and (us or re.search(r"\bU\.?S\.?\b", citizen.label or "")):
        return citizen.label or citizen.value
    if category == "sponsorship" and us:
        value = standing.get("sponsorship_us")
        return str(value) if value else None
    if category == "work_authorization" and us:
        value = standing.get("us_citizen" if re.search(r"citizen", raw, re.I) else "work_authorization_us")
        return str(value) if value else None
    return None


_HAS_BACHELORS = re.compile(r"(?:have you|did you) (?:graduated?|complete[d]?|earn(?:ed)?|obtain(?:ed)?)[^?]{0,30}"
                            r"bachelor|(?:do you have|do you hold|hold) an? bachelor", re.I)


_WORKED_HERE = re.compile(r"(?:have you|did you) (?:ever |previously )?(?:worked|been employed)[^?]{0,20}? (?:at|for|by) "
                          r"(?:an? )?([^?,]{2,80})|(?:former|previous|past) employee of ([^?,]{2,60})", re.I)


_NEVER_WORKED = re.compile(r"\b(?:have not|haven t|never|not)\s+(?:been\s+)?(?:worked|employed)\s+(?:for|at|by|with)\s+"
                           r"(?:an?\s+)?([a-z0-9&' ]{2,60}?)(?:\s+in the past|\s+before|\s+previously|$)")


def _checklist_pick(field: FormField, profile: Profile, standing: dict | None = None) -> bool | None:
    """Check-all-that-apply lists the profile answers: "office locations you would work in" (the person's cities) and
    "programming experience with any of the following" (tools named on the resume). None: not such a list."""
    q, opt = norm(field.context), norm(field.label)
    if not opt:
        return None
    # "What work options are you open to?" (Hybrid / Remote / Fully Onsite): the person's rule (2026-10-03), in person
    # up to their limit (80%) and remote: hybrid and remote yes, fully on site only with no limit
    if re.search(r"work (?:options?|arrangements?|models?|environments?|styles?|settings?)|workplace types?|"
                 r"open to (?:working )?(?:remote|hybrid|on ?site|in office)", q) and standing:
        limit = int(standing.get("in_person_max_percent") or 100)
        if re.search(r"\bremote\b", opt) or re.search(r"\bhybrid\b|flexible", opt):
            return True
        if re.search(r"on ?site|in office|in person|office based", opt):
            return limit >= 100
        return None
    # "What interview modes are you open to?" (Virtual / Hybrid / In person / Phone): any of them
    if re.search(r"interview (?:modes?|formats?|types?|settings?)|interview(?:ing)? (?:options|preferences)", q) and \
            re.search(r"virtual|video|phone|remote|hybrid|in person|on ?site|onsite", opt):
        return True
    if re.search(r"office|location", q) and re.search(r"select all|all that apply|open to|would (?:be )?(?:open|willing)", q):
        return any(norm(c) in opt or opt in norm(c) for c in profile.location_preferences) if profile.location_preferences else None
    # "What languages do you speak? (check all that apply)" (Lever lists 40): the languages from My info
    if re.search(r"languages?", q) and not re.search(r"programming|coding|software", q) and \
            re.search(r"select all|all that apply|speak|fluent|proficien", q) and profile.languages:
        mine = {norm(re.sub(r"\(.*?\)", "", x)) for x in profile.languages.split(",") if x.strip()}
        return norm(re.sub(r"\(.*?\)", "", opt)) in mine
    if re.search(r"programming|software|tools?|languages?\b.*(?:experience|proficien)|experience with any of", q) and \
            re.search(r"select all|all that apply|any of the following", q):
        skills = {norm(x) for s in profile.skills for x in re.split(r"[,;/()]", s) if x.strip()}
        return opt in skills or any(re.fullmatch(rf"{re.escape(opt)}(?: \w+)?", k) for k in skills)
    return None


def _never_worked_here(field: FormField, profile: Profile) -> bool | None:
    """Adobe: "Have you ever worked at Adobe in the following capacity:" with a box "I have not worked for Adobe in the
    past." By the person's rule, that box is ticked when Adobe is not an employer on the resume. None: not that box."""
    m = _NEVER_WORKED.search(norm(field.label))
    if not m:
        return None
    company = m[1].strip()
    employers = [norm(e.employer) for e in profile.experience if e.employer]
    return not any(company.split()[0] in emp or emp.split()[0] in company for emp in employers if emp)


_INTERVIEWED = re.compile(r"(?:previously|ever|before|in the past)[^?]{0,40}\binterview|\binterviewed (?:with|at|for)\b", re.I)
_RELATIVE = re.compile(r"\brelatives?\b|family members?|related to (?:anyone|someone|any)|immediate family|household members?|"
                       r"\bspouse\b[^?]{0,40}(?:employ|work)", re.I)
_GOV_OFFICIAL = re.compile(r"government official|public official|politically exposed|foreign official|"
                           r"government[- ]owned|state[- ]owned enterprise", re.I)
_CONFLICT = re.compile(r"conflicts? of interest", re.I)


def _names_in(listing: str, text: str) -> bool | None:
    """Whether a company of the person's list ("Google, Deloitte" or "none") is named in the question; None when blank."""
    listing = (listing or "").strip()
    if not listing:
        return None
    if listing.lower() in ("none", "no", "n/a"):
        return False
    t = norm(text)
    return any(norm(c) and re.search(rf"\b{re.escape(norm(c))}\b", t) for c in listing.split(","))


def _fill_once_fact(field: FormField, profile: Profile, page_text: str = "") -> str | None:
    """Yes/No questions the person answered once on My info: interviewed here before, a relative works here, a
    government official, a conflict of interest. Blank on My info: None, the form asks the person."""
    q = f"{field.label} {field.context}"
    if _INTERVIEWED.search(q):
        hit = _names_in(profile.interviewed_at or "", f"{q} {page_text[:300]}")
        return None if hit is None else ("Yes" if hit else "No")
    if _RELATIVE.search(q):
        hit = _names_in(profile.relatives_at or "", f"{q} {page_text[:300]}")
        return None if hit is None else ("Yes" if hit else "No")
    if _GOV_OFFICIAL.search(q):
        return profile.government_official or None
    if _CONFLICT.search(q):
        return profile.conflict_of_interest or None
    return None


def _resume_fact(field: FormField, profile: Profile) -> str | None:
    """Yes/No questions the resume answers: a completed bachelor's degree ("Yes" only). And, by the person's own rule
    (2026-09-30), "Have you worked at X?" is "No" when X is not an employer on the resume; when it is, they answer."""
    q = f"{field.label} {field.context}"
    if re.search(r"attended (?:a )?(?:university|college)|university degree program", q, re.I) and profile.education:
        return "Yes"
    if re.search(r"completed (?:at least (?:1|one) |an? |any )?internships?|prior internship|relevant full[- ]time (?:work )?experience",
                 q, re.I) and any(e.kind == "job" for e in profile.experience):
        return "Yes"  # the resume's jobs (2026-10-07, DoorDash: "at least 1 internship or relevant full-time experience?")
    if (m := _WORKED_HERE.search(field.label or "")):
        company = norm(m.group(1) or m.group(2))
        employers = [norm(e.employer) for e in profile.experience if e.employer]
        first = company.split()[0] if company.split() else ""
        if not company or any(first and first in emp or emp.split()[0] in company for emp in employers if emp):
            return None
        return "No"
    if re.search(r"\b(?:18|eighteen)\b[^?]{0,25}(?:years|older|age|\+)|\b18\+|at least 18|of legal (?:working )?age", q, re.I):
        done = [e for e in profile.education if not e.expected]
        return "Yes" if done else None  # a finished degree years ago: an adult
    if _HAS_BACHELORS.search(q):
        done = [e for e in profile.education if re.search(r"bachelor|\bb\.?a\b|\bb\.?s\b", e.degree, re.I) and not e.expected]
        return "Yes" if done else None
    if _ENROLLED.search(q) and profile.education:  # Simon-Kucher, 2026-10-10: "Are you currently enrolled in school?"
        return "Yes" if any(e.expected for e in profile.education) else "No"
    if _HIGHEST_DEGREE.search(q) and profile.education:
        return _highest_degree(profile)
    return None


_ENROLLED = re.compile(r"(?:are you|currently) (?:currently )?enrolled\b|are you (?:currently )?(?:a )?(?:current )?student\b",
                       re.I)
_HIGHEST_DEGREE = re.compile(r"highest (?:level of )?(?:degree|education|academic)", re.I)


def _highest_degree(profile: Profile) -> str:
    """The highest degree the person holds, or will by their start date (degrees in progress count: they finish before
    it). An MBA ranks above another master's, since forms list it on its own ("MA/MS/MPH" | "MBA") as the higher
    professional degree."""
    def rank(e) -> int:
        d = e.degree.lower()
        if re.search(r"ph\.?d|doctor|\bj\.?d\b|pharm|ed\.?d", d):
            return 5
        if re.search(r"business administration|\bmba\b", d):
            return 4
        if re.search(r"master|\bm\.?s\b|\bm\.?a\b", d):
            return 3
        if re.search(r"bachelor|\bb\.?a\b|\bb\.?s\b", d):
            return 2
        return 1
    return max(profile.education, key=rank).degree


_SELF_ID_FORM = re.compile(r"self[- ]identif|disabilit|veteran|cc-?305|vevraa", re.I)


def _self_id_form_field(field: FormField, profile: Profile) -> Action | None:
    """The plain boxes of a self-identification form (Workday's "Self Identify" step, the CC-305 disability form): the
    person's name, today's date, and no employee ID (they are not an employee). None: not one of these."""
    if not _SELF_ID_FORM.search(f"{field.context or ''} {field.name or ''}") or field.options:  # Lever: name="eeo[disabilitySignature]"
        return None
    lab = norm(field.label)
    if re.fullmatch(r"(?:your |full |legal )?name", lab) and profile.full_name:
        return Action(field_id=field.id, kind="fill", value=profile.full_name, source="profile", reason="full_name")
    m = re.fullmatch(r"(?:today s )?date(?: signed)?(?: (month|day|year))?", lab)
    if not m and re.search(r"\bdate\b", field.context or "", re.I):  # Workday: "Month" / "Day" / "Year" under "Date"
        m = re.fullmatch(r"(month|day|year)", lab)
    if m:
        from datetime import date
        today = date.today()
        part = m.group(1)
        value = (f"{today.month:02d}" if part == "month" else f"{today.day:02d}" if part == "day" else str(today.year)
                 if part == "year" else today.isoformat() if field.type == "date" else today.strftime("%m/%d/%Y"))
        return Action(field_id=field.id, kind="fill", value=value, source="profile", reason="today's date on the form")
    if re.fullmatch(r"employee (?:id|number)", lab):
        return Action(field_id=field.id, kind="skip", reason="not an employee: left blank")
    return None


def _same_answer(label: str, saved: str) -> bool:
    """Whether one box of a check-one list IS the saved answer ("Yes, I have a disability..." for "Yes"; "I am not a
    protected veteran" for itself), never a different option of the same question."""
    lab, ans = norm(label), norm(saved)
    if not lab or not ans:
        return False
    if lab == ans or lab.startswith(ans + " "):
        return True
    return ans in ("yes", "no") and lab.split()[0] == ans


def plan_field(field: FormField, profile: Profile, answer_sheet: dict | None = None, url: str = "",
               standing: dict | None = None, us_role: bool = False, job: Experience | None = None,
               role_location: str = "") -> Action:
    if (own := _self_id_form_field(field, profile)) is not None:
        return own
    if (category := classify(field)):
        saved = None
        if category in _SCOPED_OK and field.type != "password":
            saved = _sheet_answer(field, answer_sheet, url, scoped_only=True)
            if saved is None:
                saved = _standing_answer(field, category, standing, us_role)
        if saved is None:
            return Action(field_id=field.id, kind="needs_you", category=category,
                          reason=f"sensitive ({category}): only you answer this")
        if field.type == "checkbox" and not field.options:
            # one box of a check-one list (Workday's disability and veteran boxes): tick only the box that IS the
            # answer. Ticking "Yes" into every box would also say "No" and "I identify as a protected veteran".
            if _same_answer(field.label, saved):
                return Action(field_id=field.id, kind="check", value="true", source="answer_sheet", category=category,
                              reason="your standing answer")
            return Action(field_id=field.id, kind="skip", category=category, reason="not your answer: left unticked")
        return _apply_value(field, resolve_key(field) or "", saved, "answer_sheet")

    if field.type == "checkbox" and (pick := _checklist_pick(field, profile, standing)) is not None:
        return Action(field_id=field.id, kind="check" if pick else "skip", value="true" if pick else None,
                      source="profile", reason="checklist from your profile" if pick else "not in your profile")
    if field.type == "checkbox" and (never := _never_worked_here(field, profile)) is not None:
        if never:
            return Action(field_id=field.id, kind="check", value="true", source="profile",
                          reason="never worked there (your rule: not an employer on your resume)")
        return Action(field_id=field.id, kind="needs_you", reason="you worked there: tick the boxes that apply")
    label = norm(field.label)
    if field.type == "file":
        named = f"{label} {norm(field.name)}"  # some sites label the input just "Attach"; its name says "resume"
        if _TOGETHER.search(f"{named} {norm(field.context)}"):  # Simon-Kucher (2026-10-10): one PDF with both
            return Action(field_id=field.id, kind="cover", value="with_resume", source="llm",
                          reason="your resume and cover letter in one PDF, as the form asks")
        if _FILE_RESUME.search(named) and "cover" not in named:
            return Action(field_id=field.id, kind="upload", source="profile", reason="resume PDF")
        if _COVER.search(f"{label} {norm(field.name)} {field.name}"):
            return Action(field_id=field.id, kind="cover", source="llm", reason="cover letter written for this posting")
        # Greenhouse labels a question's upload box only "Attach": the question is in its context
        if re.search(r"transcript|academic record|course list|grade report", f"{named} {norm(field.context)}"):
            if re.search(r"undergrad|bachelor|high school", f"{named} {norm(field.context)}"):
                # profile/undergrad_transcript.pdf (2026-10-10), never the graduate one (the server checks it is saved)
                return Action(field_id=field.id, kind="transcript", value="undergraduate", source="profile",
                              reason="your undergraduate transcript")
            return Action(field_id=field.id, kind="transcript", source="profile", reason="your transcript")
        return Action(field_id=field.id, kind="needs_you" if field.required else "skip",
                      reason="file other than the resume")

    key = resolve_key(field)
    value = value_for(key, profile, field, job) if key else None
    source = "profile"
    if value is None and answer_sheet:
        value = _sheet_answer(field, answer_sheet, url, scoped_only=False)
        source = "answer_sheet"
    if value is None and field.type in ("radio", "select", "combobox"):  # a saved answer wins over these rules
        value, source = (_start_fact(field, profile) or _location_fact(field, standing, role_location)
                         or _resume_fact(field, profile) or _fill_once_fact(field, profile)), "profile"

    # a school list (also one whose options load only when it opens: DoorDash, 2026-10-07, got "University of
    # Michigan - Flint" without the city): the city picks the campus
    if value is not None and source == "profile" and key in ("edu_school", "current_school") and (
            field.options or field.type in ("combobox", "select")):
        edu = profile.education[0] if key == "current_school" else _pick(profile.education, field.section_index)
        if edu and edu.location:
            value = f"{value}\n{edu.location}"  # the city picks the campus in a school list
    if value is not None:
        return _apply_value(field, key or "", value, source)
    if field.type in ("textarea", "text") and len((field.value or "").strip()) >= 20 and (
            _wants_draft(field) or _COVER.search(f"{label} {norm(field.name)} {field.name}")):
        # already answered (a draft the person may have edited, or their own words): a second Fill never overwrites it
        return Action(field_id=field.id, kind="skip", reason="already answered")
    if field.type == "textarea" and _COVER.search(f"{label} {norm(field.name)} {field.name}") and not (field.label or "").strip().endswith("?"):
        return Action(field_id=field.id, kind="cover", source="llm", reason="cover letter written for this posting")
    if _wants_draft(field):
        if BEYOND_RESUME.search(f"{field.label} {field.context}"):
            # nothing in the profile answers it, so a draft would be made up (Palantir, 2026-10-10: a road trip that
            # never happened): the person says what to write
            return Action(field_id=field.id, kind="needs_you", reason=BEYOND_RESUME_ASK)
        return Action(field_id=field.id, kind="draft", source="llm", reason="free-text question: draft for review")
    if field.required:
        why = f"not in your profile ({key})" if key else "unrecognized required question"
        return Action(field_id=field.id, kind="needs_you", reason=why)
    if field.type == "radio":
        # a Yes/No or pick-one question is never passed over in silence: sites often mark "required" only visually
        # (Google Careers puts the * in the text), so an unflagged skip could leave a required answer blank
        return Action(field_id=field.id, kind="needs_you", reason="a question for you (no saved answer)")
    return Action(field_id=field.id, kind="skip", reason="no matching profile value")


def _school_core(name: str) -> str:
    """"University of Michigan, Harbor School of Business" -> "university of michigan"."""
    core = norm(re.split(r",| - | – |\(", name)[0])
    return re.sub(r"\s+school of .*$", "", core).strip()


def match_school(school: str, location: str, options: list[FieldOption]) -> FieldOption | None:
    """A school list names campuses ("University of Michigan - Ann Arbor / Dearborn / Flint"): same institution, and
    the campus in the school's own city. More than one campus and no city to tell them apart: no guess."""
    core = _school_core(school)
    if not core:
        return None
    same = [o for o in options if _school_core(o.label) == core]
    if len(same) == 1:
        return same[0]
    city = norm(location.split(",")[0]) if location else ""
    here = [o for o in same if city and city in norm(o.label)]
    return here[0] if len(here) == 1 else None


def _apply_value(field: FormField, key: str, value: str, source: str) -> Action:
    if field.type == "combobox" and key.endswith("_month") and value.isdigit() and 1 <= int(value) <= 12:
        value = _MONTH_NAMES_TITLE[int(value) - 1]  # a month list shows names ("May"), not numbers
    if field.type == "combobox":  # a custom dropdown: the extension opens it and picks the option matching this text
        return Action(field_id=field.id, kind="combobox", value=value, source=source, reason=key or "answer")
    if field.type in ("select", "radio"):
        opt = match_option(value, field.options, key)
        if key in ("edu_school", "current_school") and "\n" in value:  # "school\nlocation" (see plan_field)
            opt = match_school(*value.split("\n", 1), field.options)
            value = value.split("\n", 1)[0]
        if opt is None:
            return Action(field_id=field.id, kind="needs_you" if field.required else "skip",
                          reason=f"no option matches '{value}'")
        return Action(field_id=field.id, kind="select", value=opt.value, source=source, reason=f"{key or 'answer'}")
    if field.type == "checkbox":
        return Action(field_id=field.id, kind="check", value=value if value in ("true", "false") else "true",
                      source=source, reason=key)
    if field.maxlength and len(value) > field.maxlength and key != "job_description":
        return Action(field_id=field.id, kind="needs_you", reason=f"value longer than the field allows ({field.maxlength})")
    if field.maxlength and len(value) > field.maxlength:
        value = value[: field.maxlength].rsplit(" ", 1)[0]
    return Action(field_id=field.id, kind="fill", value=value, source=source, reason=key)


def _title_key(s: str) -> str:
    return norm(s.replace("&", " and "))


def _entry_score(title: str, employer: str, x: Experience) -> int:
    """How surely a work entry the site filled in ("Research Associate" at "Efrei") is this resume line."""
    t, e, xt, xe = _title_key(title), norm(employer), _title_key(x.title), norm(x.employer)
    score = 0
    if t and t == xt:
        score += 10
    elif t and len(t) > 3 and (t in xt or xt in t):
        score += 6  # "Analytics Lead" for "Analytics Lead & Strategy"
    if e and xe and (e == xe or e.split()[0] == xe.split()[0]):
        score += 4
    return score


def match_work_entries(fields: list[FormField], profile: Profile) -> tuple[dict[int, object], list[str]]:
    """Which resume line each work entry on the page is, by what the site already put in it (Google fills the entries
    from the resume itself: in its own order, with projects among the jobs and employers misread). Returns
    {entry index: Experience | "project:<title>" | "unknown:<title>"} and notes for the person (jobs with no entry).
    An empty entry gets the next job that has none. Without anything filled in, the n-th job for the n-th entry."""
    entries: dict[int, dict[str, str]] = {}
    for f in fields:
        if f.section != "experience":
            continue
        e = entries.setdefault(f.section_index, {"title": "", "employer": ""})
        key = resolve_key(f)
        if key == "job_title" and f.value.strip():
            e["title"] = f.value.strip()
        elif key == "job_employer" and f.value.strip():
            e["employer"] = f.value.strip()
    if not entries or not any(e["title"] or e["employer"] for e in entries.values()):
        return {}, []
    lines = list(profile.experience)
    # Google sometimes puts the job title in the Employer box and leaves Job title empty ("Brand Strategy"
    # as the employer, 2026-10-06): then the employer box is read as the title
    pairs = sorted(((max(_entry_score(e["title"], e["employer"], x),
                         _entry_score(e["employer"], "", x) if not e["title"] else 0), i, n)
                    for i, e in entries.items() for n, x in enumerate(lines)), reverse=True)
    out: dict[int, object] = {}
    used: set[int] = set()
    for score, i, n in pairs:
        if score < 6 or i in out or n in used:  # a title has to match; an employer alone is too weak
            continue
        x = lines[n]
        out[i] = x if x.kind == "job" else f"project:{x.title}"
        used.add(n)
    jobs_left = [x for n, x in enumerate(lines) if x.kind == "job" and n not in used]
    for i, e in sorted(entries.items()):
        if i in out:
            continue
        if e["title"] or e["employer"]:
            out[i] = f"unknown:{e['title'] or e['employer']}"
        elif jobs_left:
            out[i] = jobs_left.pop(0)
    notes = [f"Add a work entry for {x.title} at {x.employer} (the site's Add button), then click Fill this page again."
             for x in jobs_left]
    return out, notes


def plan_fields(fields: list[FormField], profile: Profile, answer_sheet: dict | None = None,
                url: str = "", standing: dict | None = None, us_role: bool = False) -> list[Action]:
    return plan_fields_with_notes(fields, profile, answer_sheet, url, standing, us_role)[0]


def entries_to_add(fields: list[FormField], profile: Profile) -> dict[str, int]:
    """How many work and education entries the page lacks for the resume ({"experience": 3}): Workday starts with one
    of each, and the extension can press its Add button that many times."""
    out: dict[str, int] = {}
    work = {f.section_index for f in fields if f.section == "experience"}
    if work:
        matched, _ = match_work_entries(fields, profile)
        jobs = [x for x in profile.experience if x.kind == "job"]
        if matched:
            covered = {id(m) for m in matched.values() if isinstance(m, Experience)}
            missing = sum(1 for j in jobs if id(j) not in covered)
        else:
            missing = len(jobs) - len(work)
        if missing > 0:
            out["experience"] = missing
    schools = {f.section_index for f in fields if f.section == "education"}
    if schools and len(profile.education) > len(schools):
        out["education"] = len(profile.education) - len(schools)
    return out


def plan_fields_with_notes(fields: list[FormField], profile: Profile, answer_sheet: dict | None = None,
                           url: str = "", standing: dict | None = None,
                           us_role: bool = False, role_location: str = "") -> tuple[list[Action], list[str]]:
    matched, notes = match_work_entries(fields, profile)
    # schools the page has no entry for (Google dropped the MBA): say so, the person adds one
    edu_first = {}
    for f in fields:
        if f.section == "education" and f.value.strip():
            edu_first.setdefault(f.section_index, f)
    n_schools = len({f.section_index for f in fields if f.section == "education"})
    if edu_first and n_schools < len(profile.education):
        # only when the page has fewer school entries than the resume: two "University of Michigan" entries cannot be
        # told apart by their school name alone, and that once listed the MBA as missing after it was added
        shown = {id(e) for e in (_edu_for(f, profile) for f in edu_first.values()) if e is not None}
        notes += [f"Add an education entry for {e.degree} at {_school_title(e.school)} (the site's Add button), then "
                  "click Fill this page again." for e in profile.education if id(e) not in shown]
    add = entries_to_add(fields, profile)
    if not matched and add.get("experience"):  # empty entries (Workday): fewer than the jobs
        jobs = [x for x in profile.experience if x.kind == "job"]
        n = len({f.section_index for f in fields if f.section == "experience"})
        notes += [f"Add a work entry for {x.title} at {x.employer} (the site's Add button), then click Fill this page "
                  "again." for x in jobs[n:]]
    if not edu_first and add.get("education"):
        n = len({f.section_index for f in fields if f.section == "education"})
        notes += [f"Add an education entry for {e.degree} at {_school_title(e.school)} (the site's Add button), then "
                  "click Fill this page again." for e in profile.education[n:]]
    flagged: set[int] = set()
    actions = []
    for f in fields:
        m = matched.get(f.section_index) if f.section == "experience" else None
        if isinstance(m, str):
            # a project or a line not on the resume: nothing is filled in it; the person removes or checks it, once
            kind, _, what = m.partition(":")
            first = f.section_index not in flagged and resolve_key(f) in ("job_title", "job_employer")
            if first:
                flagged.add(f.section_index)
                why = (f"'{what}' is a project on your resume, not a job: remove this entry" if kind == "project"
                       else f"'{what}' is not a job on your resume: check it or remove this entry")
                actions.append(Action(field_id=f.id, kind="needs_you", reason=why))
            else:
                actions.append(Action(field_id=f.id, kind="skip", reason="entry to remove or check"))
            continue
        actions.append(plan_field(f, profile, answer_sheet, url, standing, us_role, job=m, role_location=role_location))
    # a check-all-that-apply question answered by one box ("I have not worked for Adobe"): the other boxes stay empty
    answered = {f.name or f.context for f, a in zip(fields, actions)
                if f.type == "checkbox" and a.kind == "check" and "never worked" in a.reason}
    for i, (f, a) in enumerate(zip(fields, actions)):
        if f.type == "checkbox" and a.kind == "needs_you" and (f.name or f.context) in answered:
            actions[i] = Action(field_id=f.id, kind="skip", reason="answered by another box")
    _single_location_choice(fields, actions, profile)
    _keep_what_is_there(fields, actions, url)
    return actions, notes


# a dropdown's "nothing chosen yet" entries
_PLACEHOLDER = re.compile(r"^(?:|select|select one|select\.\.\.|choose|choose one|please select|please choose|-+|--.*|mm|dd|yyyy|month|"
                          r"day|year|none selected|not selected|\(select\))$", re.I)


def has_answer(field: FormField) -> bool:
    """Whether the box already holds an answer (the person's, or one the site saved), not a placeholder."""
    v = (field.value or "").strip()
    if field.type == "checkbox":
        return v == "true"
    if not v or v == (field.placeholder or "").strip():
        return False
    if field.type in ("select", "combobox") and field.options:
        label = next((o.label for o in field.options if o.value == v), None)
        if label is None and field.type == "select":
            return False  # the site's own "Select" entry (left out of the options)
        if label is not None and _PLACEHOLDER.match(norm(label)):
            return False
    return not _PLACEHOLDER.match(norm(v))


def _keep_what_is_there(fields: list[FormField], actions: list[Action], url: str = "") -> None:
    """A box that already holds an answer is never written over (L.E.K.'s Oleeo, 2026-10-13: every save reloads the
    step, and the fill on arrival put back Tailor's values over the person's corrections). Google Careers' work and
    school entries are the exception: Google fills them from the resume itself, not always rightly, and Tailor fixes
    them."""
    google = "google.com/about/careers" in (url or "")
    for i, (f, a) in enumerate(zip(fields, actions)):
        if a.kind not in ("fill", "select", "combobox", "check") or not has_answer(f):
            continue
        if google and f.section in ("experience", "education"):
            continue
        actions[i] = Action(field_id=f.id, kind="skip", reason="already answered: kept as it is")


_ONE_LOCATION = re.compile(r"single location|most interested in|location (?:you(?:'| a)re|you are) most|preferred (?:office|location)|"
                           r"which (?:office|location) (?:would you|do you) (?:prefer|like)", re.I)


def _single_location_choice(fields: list[FormField], actions: list[Action], profile: Profile) -> None:
    """"Choose the single location you're most interested in" drawn as checkboxes (Databricks, 2026-10-07): tick only
    the person's best-ranked city among the boxes (their order; the city itself before a Bay Area neighbour)."""
    from .profile import BAY_AREA

    prefs = [norm(c) for c in profile.location_preferences]
    groups: dict[str, list[int]] = {}
    for i, f in enumerate(fields):
        if f.type == "checkbox" and _ONE_LOCATION.search(f.context or ""):
            groups.setdefault(f.context, []).append(i)
    for idxs in groups.values():
        def rank(i: int) -> float:
            opt = norm(fields[i].label)
            for r, c in enumerate(prefs):
                if c and c in opt:
                    return r
                if c == "san francisco" and any(norm(b) in opt for b in BAY_AREA):
                    return r + 0.5  # Mountain View for San Francisco, after San Francisco itself
            return 99.0
        best = min(idxs, key=rank)
        if rank(best) >= 99:
            continue  # none of the person's cities: they choose
        for i in idxs:
            pick = i == best
            actions[i] = Action(field_id=fields[i].id, kind="check" if pick else "skip", value="true" if pick else None,
                                source="profile", reason="your top location" if pick else "one location only")

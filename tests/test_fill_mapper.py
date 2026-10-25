"""Form-field decisions, using labels as they appear on Greenhouse, Lever and Workday forms."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.fill.forms import FieldOption, FormField  # noqa: E402
from tailor.fill.mapper import match_option, plan_field, plan_fields_with_notes  # noqa: E402
from tailor.fill.profile import Education, Experience, Profile, YearMonth  # noqa: E402
from tailor.fill.sensitive import classify, classify_text  # noqa: E402

PROFILE = Profile(
    first_name="Jane", last_name="Doe", email="jane@example.com", phone="(555) 010-0000",
    linkedin_url="https://www.linkedin.com/in/janedoe", portfolio_url="https://janedoe.example/",
    education=[
        Education(school="Example University", degree="Master of Business Administration",
                  field="Data and Business Analytics", start=YearMonth(year=2025, month=8),
                  end=YearMonth(year=2027, month=5), expected=True),
        Education(school="Other College", degree="Bachelor of Arts", field="Economics", end=YearMonth(year=2021, month=6)),
    ],
    experience=[
        Experience(employer="Acme Corp", title="Analyst", location="Chicago, IL", start=YearMonth(year=2026, month=5),
                   current=True, bullets=["Built a dashboard", "Cut reporting time by 40%"]),
        Experience(employer="Globex", title="Intern", location="Remote", start=YearMonth(year=2025, month=6),
                   end=YearMonth(year=2025, month=9)),
    ],
    skills=["SQL", "Python"],
)


def f(label, type="text", **kw):
    return FormField(id=kw.pop("id", "f1"), label=label, type=type, **kw)


def opts(*labels):
    return [FieldOption(value=str(i), label=l) for i, l in enumerate(labels)]


class SensitiveTests(unittest.TestCase):
    def test_sensitive_questions_are_flagged(self):
        cases = {
            "Are you legally authorized to work in the United States?": "work_authorization",
            "Will you now or in the future require sponsorship for employment visa status?": "sponsorship",
            "Gender": "demographics", "Are you Hispanic/Latino?": "demographics", "Veteran Status": "demographics",
            "Disability Status": "demographics", "What are your salary expectations?": "compensation",
            "Have you ever been convicted of a felony?": "background",
            "I acknowledge that I have read the privacy policy": "legal_attestation",
            "Electronic signature": "legal_attestation", "Date of birth": "government_id",
            "Social Security Number": "government_id", "Are you at least 18 years of age?": "legal_attestation",
        }
        for label, want in cases.items():
            self.assertEqual(classify_text(label), want, label)

    def test_legal_name_is_not_a_government_id(self):
        # Google Careers: "Your legal name is what appears on official documents (such as your driver's license or passport)"
        ctx = "Legal name. Your legal name is what appears on official documents (such as your driver's license or passport)."
        for label in ("First name*", "Middle name", "Last name*"):
            self.assertIsNone(classify(f(label, context=ctx)), label)
        # but a field that itself asks for the document is still flagged, whatever the helper text says
        self.assertEqual(classify(f("Passport number", context=ctx)), "government_id")
        self.assertEqual(classify(f("Name as shown on passport")), "government_id")
        self.assertEqual(classify(f("Number", context="Driver's license")), "government_id")

    def test_legal_name_fills_on_google_careers(self):
        ctx = "Legal name. Your legal name is what appears on official documents (such as your driver's license or passport)."
        a = plan_field(f("First name", autocomplete="given-name", context=ctx), PROFILE)
        self.assertEqual((a.kind, a.value), ("fill", "Jane"))

    def test_an_unanswered_radio_question_is_flagged_even_when_not_marked_required(self):
        # Google Careers: the * is in the text only, so the page never says these are required
        yn = opts("Yes", "No")
        for q in ("Personalize your Google Careers profile with job recommendations and related content?",
                  "Would you like to join our talent community?"):
            self.assertEqual(plan_field(f(q, type="radio", options=yn), PROFILE).kind, "needs_you", q)
        self.assertEqual(plan_field(f("Middle name"), PROFILE).kind, "skip")  # an optional text box still may be skipped

    def test_pronounce_is_not_pronouns_and_personal_facts_are_never_drafted(self):
        # Lever (Palantir): this box once got the standing pronouns answer, because "pronounce" contains "pronoun"
        q = f("Name Pronunciation | How do you pronounce your name?")
        self.assertIsNone(classify(q))
        self.assertNotIn(plan_field(q, PROFILE).kind, ("fill", "draft"))
        self.assertEqual(classify_text("What are your pronouns?"), "demographics")
        self.assertNotEqual(plan_field(f("What nickname do you go by?"), PROFILE).kind, "draft")
        self.assertEqual(plan_field(f("Why do you want to work at Palantir?", type="textarea"), PROFILE).kind, "draft")

    def test_school_list_picks_the_institution_and_campus(self):
        from tailor.fill.mapper import match_school
        campuses = opts("Click Here", "Michigan State University", "University of Michigan - Ann Arbor",
                        "University of Michigan - Dearborn", "University of Michigan - Flint",
                        "University of California - Davis", "Other - School Not Listed")
        pick = lambda school, loc: (match_school(school, loc, campuses) or FieldOption(value="", label="")).label  # noqa: E731
        self.assertEqual(pick("University of Michigan, Harbor School of Business", "Ann Arbor, MI"),
                         "University of Michigan - Ann Arbor")
        self.assertEqual(pick("University of Michigan School of Information", "Ann Arbor, MI"), "University of Michigan - Ann Arbor")
        self.assertEqual(pick("University of California, Davis", "Davis, CA"), "University of California - Davis")
        self.assertEqual(pick("University of Michigan", ""), "")  # three campuses, no city: no guess
        self.assertEqual(pick("Michigan State University", "East Lansing, MI"), "Michigan State University")
        self.assertEqual(pick("Wayne State University", "Detroit, MI"), "")
        # Lever's standalone question, with the profile's school and city
        p = PROFILE.model_copy(update={"education": [Education(school="University of Michigan, Harbor School of Business",
                                                               degree="Master of Business Administration", location="Ann Arbor, MI")]})
        a = plan_field(f("Which university are you currently attending or did you last attend?", type="select",
                         options=campuses, required=True), p)
        self.assertEqual((a.kind, a.value), ("select", "2"))

    def test_questions_the_resume_answers_and_consents_it_never_touches(self):
        # DoorDash Greenhouse form, 2026-09-30
        yn = opts("Yes", "No")
        q = f("Have you graduated with your Bachelor’s Degree? *", type="radio", options=yn, required=True)
        self.assertEqual(plan_field(q, PROFILE).value, "0")  # "Yes": Other College, BA, June 2021
        studying = PROFILE.model_copy(update={"education": [e.model_copy(update={"expected": True}) for e in PROFILE.education]})
        self.assertEqual(plan_field(q, studying).kind, "needs_you")  # expected, not done: never "Yes", never guessed "No"
        for label in ("Would you like to receive communications via SMS and/or WhatsApp to the number provided?",
                      "Applicant Privacy Acknowledgement *"):
            self.assertEqual(classify(f(label, type="radio", options=yn)), "legal_attestation", label)

    def test_transgender_question_uses_its_own_standing_answer_not_gender(self):
        standing = {"gender": "Male", "transgender": "No"}
        yn = opts("Yes", "No", "I don't wish to answer")
        a = plan_field(f("Do you identify as transgender?*", type="radio", options=yn, required=True), PROFILE, standing=standing)
        self.assertEqual((a.kind, a.value), ("select", "1"))
        g = plan_field(f("Gender", type="select", options=opts("Female", "Male")), PROFILE, standing=standing)
        self.assertEqual(g.value, "1")

    def test_worked_here_before_is_no_unless_the_company_is_on_the_resume(self):
        yn = opts("Yes", "No")
        for q in ("Have you worked at DoorDash?*", "Have you ever worked for Figma before, as an employee or a contractor?",
                  "Have you previously been employed by Stripe?"):
            self.assertEqual(plan_field(f(q, type="radio", options=yn, required=True), PROFILE).value, "1", q)
        # a company on the resume (Acme Corp): the person answers
        self.assertEqual(plan_field(f("Have you worked at Acme before?", type="radio", options=yn, required=True), PROFILE).kind,
                         "needs_you")

    def test_google_higher_education_entries(self):
        # Google Careers, 2026-09-30: entries prefilled from the resume, MS first, then the BA
        from tailor.fill.mapper import _school_title
        self.assertEqual(_school_title("University of Michigan, Harbor School of Business"), "University of Michigan")
        self.assertEqual(_school_title("University of Michigan School of Information"), "University of Michigan")
        self.assertEqual(_school_title("University of California, Davis"), "University of California, Davis")
        p = PROFILE.model_copy(update={"education": [
            Education(school="Example University", degree="Master of Business Administration", end=YearMonth(year=2027, month=5), expected=True),
            Education(school="Example University School of Information", degree="Master of Science", field="Information", expected=True),
            Education(school="Other College, Davis", degree="Bachelor of Arts", field="History", end=YearMonth(year=2021, month=6))]})
        ms = dict(section="education", section_index=0, entry_hint="Example University Master of Science Information")
        ba = dict(section="education", section_index=1, entry_hint="Other College, Bachelor of Arts History")
        self.assertEqual(plan_field(f("Degree", type="combobox", **ms), p).value, "Master of Science")  # not the MBA
        self.assertEqual(plan_field(f("Degree Status", type="combobox", **ms), p).value, "Now attending")
        self.assertEqual(plan_field(f("Degree Status", type="combobox", **ba), p).value, "Graduated")
        self.assertEqual(plan_field(f("School name", type="combobox", **ba), p).value, "Other College, Davis")
        yn = opts("Yes", "No")
        self.assertEqual(plan_field(f("Attended university degree program?", type="radio", options=yn, required=True), p).value, "0")

    def test_password_field_and_captcha(self):
        self.assertEqual(classify(f("Create a value", type="password")), "credentials")
        self.assertEqual(classify_text("Please complete the reCAPTCHA"), "bot_check")

    def test_ordinary_questions_are_not_flagged(self):
        for label in ["First Name", "Email", "How did you hear about this job?", "Location (City)",
                      "Why do you want to work here?", "Certifications", "Website", "Trace of experience"]:
            self.assertIsNone(classify_text(label), label)


class GreenhouseLeverTests(unittest.TestCase):
    def plan(self, field, sheet=None):
        return plan_field(field, PROFILE, sheet)

    def test_basic_contact_fields(self):
        cases = {"First Name *": "Jane", "Last Name": "Doe", "Email": "jane@example.com", "Phone": "(555) 010-0000",
                 "Full name": "Jane Doe", "LinkedIn Profile": "https://www.linkedin.com/in/janedoe",
                 "LinkedIn URL": "https://www.linkedin.com/in/janedoe", "Website": "https://janedoe.example/",
                 "Portfolio URL": "https://janedoe.example/", "Current company": "Acme Corp"}
        for label, want in cases.items():
            a = self.plan(f(label, required=True))
            self.assertEqual((a.kind, a.value), ("fill", want), label)

    def test_autocomplete_attribute_beats_a_vague_label(self):
        a = self.plan(f("Name", autocomplete="given-name"))
        self.assertEqual(a.value, "Jane")

    def test_phone_with_short_field_gets_digits_only(self):
        self.assertEqual(self.plan(f("Phone", maxlength=10)).value, "5550100000")

    def test_resume_upload_and_the_cover_letter_upload(self):
        self.assertEqual(self.plan(f("Resume/CV", type="file", required=True)).kind, "upload")
        self.assertEqual(self.plan(f("Cover Letter", type="file")).kind, "cover")  # written for the posting (app.cover_rule)
        self.assertEqual(self.plan(f("Writing sample", type="file")).kind, "skip")

    def test_resume_input_labelled_only_attach_is_found_by_its_name(self):
        # Greenhouse's new boards label the file input "Attach"; the field name is "resume" / "cover_letter"
        self.assertEqual(self.plan(f("Attach", type="file", name="resume", required=True)).kind, "upload")
        self.assertEqual(self.plan(f("Attach", type="file", name="cover_letter")).kind, "cover")

    def test_missing_profile_value_is_flagged_only_if_required(self):
        self.assertEqual(self.plan(f("Location (City)", required=True)).kind, "needs_you")
        self.assertEqual(self.plan(f("GitHub URL")).kind, "skip")
        self.assertEqual(self.plan(f("GitHub URL", required=True)).kind, "needs_you")

    def test_sensitive_beats_the_profile(self):
        a = self.plan(f("Are you legally authorized to work in the US?", type="radio", required=True,
                        options=opts("Yes", "No")))
        self.assertEqual((a.kind, a.category), ("needs_you", "work_authorization"))

    def test_open_questions_are_drafted_but_factual_ones_are_not(self):
        self.assertEqual(self.plan(f("Why do you want to work here?", type="textarea")).kind, "draft")
        self.assertEqual(self.plan(f("Cover Letter", type="textarea")).kind, "cover")  # the whole letter, not a short answer
        self.assertEqual(self.plan(f("Did you attach a cover letter?", type="textarea")).kind, "draft")
        self.assertEqual(self.plan(f("Additional information", type="textarea")).kind, "skip")
        # seen on a real company careers form: an optional catch-all box ending in "?" must stay empty
        for label in ("Anything Else?", "Is there anything else you'd like us to know?", "Comments"):
            for kind in ("text", "textarea"):
                self.assertEqual(self.plan(f(label, type=kind)).kind, "skip", label)
        self.assertEqual(self.plan(f("How did you hear about this job?", required=True)).kind, "needs_you")
        self.assertEqual(self.plan(f("Are you willing to relocate?", type="radio", required=True,
                                     options=opts("Yes", "No"))).kind, "needs_you")

    def test_saved_answer_fills_a_factual_question(self):
        field = f("How did you hear about this job?", type="select", required=True,
                  options=opts("Job board", "LinkedIn", "Referral"))
        a = self.plan(field, {"how did you hear about this job": "LinkedIn"})
        self.assertEqual((a.kind, a.value, a.source), ("select", "1", "answer_sheet"))


class ScopedAnswerAndDropdownTests(unittest.TestCase):
    """Answers the person saved for ONE application, and custom dropdowns (react-select style) as on Greenhouse."""
    URL = "https://job-boards.greenhouse.io/figma/jobs/5711468004?gh_jid=5711468004"
    AUTH = "Are you authorized to work in the country for which you applied?"
    SHEET = {AUTH: {"value": "Yes", "only_for": "greenhouse.io/figma/"},
             "Portfolio & Password": {"value": "jordanrivera.example (no password)", "only_for": "greenhouse.io/figma/"},
             "Have you ever worked for Figma before, as an employee or a contractor/consultant?": "No"}

    def plan(self, field, sheet=None, url=URL, profile=PROFILE):
        return plan_field(field, profile, sheet, url)

    def test_scoped_answer_fills_a_sensitive_dropdown_only_on_that_application(self):
        field = f(self.AUTH, type="combobox", required=True)
        a = self.plan(field, self.SHEET)
        self.assertEqual((a.kind, a.value, a.source), ("combobox", "Yes", "answer_sheet"))
        other = self.plan(field, self.SHEET, url="https://job-boards.greenhouse.io/othercorp/jobs/1")
        self.assertEqual((other.kind, other.category), ("needs_you", "work_authorization"))
        self.assertEqual(self.plan(field, self.SHEET, url="").kind, "needs_you")

    def test_plain_or_unscoped_entries_never_answer_a_sensitive_question(self):
        field = f(self.AUTH, type="combobox", required=True)
        self.assertEqual(self.plan(field, {self.AUTH: "Yes"}).kind, "needs_you")
        self.assertEqual(self.plan(field, {self.AUTH: {"value": "Yes"}}).kind, "needs_you")
        self.assertEqual(self.plan(field, {self.AUTH: {"value": "Yes", "only_for": ""}}).kind, "needs_you")

    def test_some_kinds_are_never_filled_even_with_a_scoped_answer(self):
        scope = {"only_for": "greenhouse.io/figma/"}
        cases = [("I acknowledge that I have read the privacy policy", "checkbox"), ("Social Security Number", "text"),
                 ("Date of birth", "text"), ("Please complete the reCAPTCHA", "text"), ("Electronic signature", "text")]
        for label, kind in cases:
            a = self.plan(f(label, type=kind, required=True), {label: {"value": "Yes", **scope}})
            self.assertEqual(a.kind, "needs_you", label)
        pw = self.plan(f("Create a value", type="password", required=True), {"Create a value": {"value": "x", **scope}})
        self.assertEqual(pw.kind, "needs_you")

    def test_portfolio_and_password_label_uses_the_scoped_answer(self):
        a = self.plan(f("Portfolio & Password", required=True), self.SHEET)
        self.assertEqual((a.kind, a.value), ("fill", "jordanrivera.example (no password)"))
        self.assertEqual(self.plan(f("Portfolio & Password", required=True)).kind, "needs_you")

    def test_dropdown_answered_from_the_profile_or_a_plain_saved_answer(self):
        with_country = PROFILE.model_copy(update={"country": "United States"})
        a = self.plan(f("Country", type="combobox", required=True), profile=with_country)
        self.assertEqual((a.kind, a.value, a.source), ("combobox", "United States", "profile"))
        self.assertEqual(self.plan(f("Country", type="combobox", required=True)).kind, "needs_you")
        q = "Have you ever worked for Figma before, as an employee or a contractor/consultant?"
        b = self.plan(f(q, type="combobox", required=True), self.SHEET)
        self.assertEqual((b.kind, b.value, b.source), ("combobox", "No", "answer_sheet"))

    def test_demographic_dropdowns_stay_with_the_person_without_a_scoped_answer(self):
        for label in ("Gender", "Are you Hispanic/Latino?", "Veteran Status", "Pronouns"):
            a = self.plan(f(label, type="combobox"), {label: "Decline"})
            self.assertEqual(a.kind, "needs_you", label)


class StandingAnswerTests(unittest.TestCase):
    """Facts the person wrote down once (self-ID answers, U.S. work authorization) and a city dropdown."""
    STANDING = {"gender": "Male", "hispanic_latino": "No", "race": "Asian", "veteran": "I am not a protected veteran",
                "work_authorization_us": "Yes"}

    def plan(self, field, standing=STANDING, sheet=None, url="https://boards.example.com/acme/jobs/1", profile=PROFILE):
        return plan_field(field, profile, sheet, url, standing)

    def test_self_id_questions_use_the_standing_answers(self):
        cases = {"Gender": "Male", "Are you Hispanic/Latino?": "No", "Please identify your race": "Asian",
                 "Race/Ethnicity": "Asian", "Veteran Status": "I am not a protected veteran"}
        for label, want in cases.items():
            a = self.plan(f(label, type="combobox"))
            self.assertEqual((a.kind, a.value, a.source), ("combobox", want, "answer_sheet"), label)

    def test_a_radio_group_gets_the_matching_option(self):
        a = self.plan(f("Gender", type="radio", options=opts("Female", "Male", "Decline to self-identify")))
        self.assertEqual((a.kind, a.value), ("select", "1"))

    def test_questions_without_a_written_answer_stay_with_the_person(self):
        for label in ("Pronouns", "Disability Status", "Are you transgender?", "Sexual orientation"):
            self.assertEqual(self.plan(f(label, type="combobox")).kind, "needs_you", label)
        self.assertEqual(self.plan(f("Gender", type="combobox"), standing={}).kind, "needs_you")
        self.assertEqual(self.plan(f("Gender", type="combobox"), standing=None).kind, "needs_you")

    def test_work_authorization_is_answered_only_when_the_question_names_the_us(self):
        yes = self.plan(f("Are you legally authorized to work in the United States?", type="combobox", required=True))
        self.assertEqual((yes.kind, yes.value), ("combobox", "Yes"))
        self.assertEqual(self.plan(f("Authorized to work in the U.S.?", type="combobox")).value, "Yes")
        for label in ("Are you authorized to work in the country for which you applied?",
                      "Tell us: are you authorized to work here?"):
            self.assertEqual(self.plan(f(label, type="combobox")).kind, "needs_you", label)

    def test_nothing_else_sensitive_is_ever_read_from_the_standing_answers(self):
        standing = {**self.STANDING, "sponsorship": "No", "salary": "100", "date_of_birth": "x", "consent": "Yes"}
        for label, kind in [("Will you now or in the future require sponsorship?", "combobox"),
                            ("What are your salary expectations?", "text"), ("Date of birth", "text"),
                            ("I acknowledge that I have read the privacy policy", "checkbox"),
                            ("Please complete the reCAPTCHA", "text")]:
            self.assertEqual(self.plan(f(label, type=kind, required=True), standing=standing).kind, "needs_you", label)
        pw = self.plan(f("Gender", type="password"), standing=self.STANDING)
        self.assertEqual(pw.kind, "needs_you")

    def test_pronouns_disability_sponsorship_and_citizenship(self):
        standing = {**self.STANDING, "pronouns": "he/him", "disability": "Yes", "sponsorship_us": "No",
                    "us_citizen": "Yes"}
        cases = [("Pronouns", "he/him"), ("Disability Status", "Yes"),
                 ("Will you now or in the future require sponsorship for employment visa status in the United States?", "No"),
                 ("Will you require H-1B sponsorship?", "No"),
                 ("Are you a U.S. citizen?", "Yes"),
                 ("Are you legally authorized to work in the United States?", "Yes")]
        for label, want in cases:
            a = self.plan(f(label, type="combobox", required=True), standing=standing)
            self.assertEqual((a.kind, a.value), ("combobox", want), label)

    def test_sponsorship_and_citizenship_need_the_us_to_be_named(self):
        standing = {**self.STANDING, "sponsorship_us": "No", "us_citizen": "Yes"}
        for label in ("Will you now or in the future require sponsorship?", "Are you a citizen of the country where this job is?"):
            self.assertEqual(self.plan(f(label, type="combobox"), standing=standing).kind, "needs_you", label)

    def test_a_combined_self_id_question_stays_with_the_person(self):
        standing = {**self.STANDING, "disability": "Yes"}
        a = self.plan(f("Are you a protected veteran or do you have a disability?", type="combobox"), standing=standing)
        self.assertEqual(a.kind, "needs_you")
        b = self.plan(f("Hispanic or Latino ethnicity?", type="combobox"), standing=standing)
        self.assertEqual((b.kind, b.value), ("combobox", "No"))

    def test_a_scoped_answer_beats_a_standing_one(self):
        url = "https://job-boards.greenhouse.io/acme/jobs/1"
        sheet = {"Gender": {"value": "Female", "only_for": "greenhouse.io/acme/"}}
        self.assertEqual(self.plan(f("Gender", type="combobox"), sheet=sheet, url=url).value, "Female")
        self.assertEqual(self.plan(f("Gender", type="combobox"), sheet=sheet, url="https://x.example/").value, "Male")

    def test_city_dropdown_searches_city_and_state_but_a_plain_city_field_gets_the_city(self):
        home = PROFILE.model_copy(update={"city": "Los Gatos", "state": "California"})
        combo = self.plan(f("Location (City)", type="combobox", required=True), profile=home)
        self.assertEqual((combo.kind, combo.value), ("combobox", "Los Gatos, California"))
        plain = self.plan(f("City", required=True), profile=home)
        self.assertEqual((plain.kind, plain.value), ("fill", "Los Gatos"))


class WorkdayTests(unittest.TestCase):
    def plan(self, field):
        return plan_field(field, PROFILE)

    def test_education_fields_use_the_right_entry(self):
        a = self.plan(f("School or University", section="education", section_index=1))
        self.assertEqual(a.value, "Other College")
        deg = f("Degree", type="select", section="education", section_index=0,
                options=opts("Bachelor's Degree", "Master's Degree", "MBA", "PhD"))
        self.assertEqual(self.plan(deg).value, "2")  # MBA
        self.assertEqual(self.plan(f("Field of Study", section="education")).value, "Data and Business Analytics")
        self.assertEqual(self.plan(f("Overall Result (GPA)", section="education")).kind, "skip")
        self.assertEqual(self.plan(f("Overall Result (GPA)", section="education", required=True)).kind, "needs_you")

    def test_work_history_fields(self):
        cur = dict(section="experience", section_index=0)
        self.assertEqual(self.plan(f("Job Title", **cur)).value, "Analyst")
        self.assertEqual(self.plan(f("Company", **cur)).value, "Acme Corp")
        self.assertEqual(self.plan(f("Location", **cur)).value, "Chicago, IL")
        self.assertEqual(self.plan(f("From", type="month", **cur)).value, "2026-05")
        self.assertEqual(self.plan(f("Role Description", type="textarea", **cur)).value,
                         "Built a dashboard Cut reporting time by 40%")
        cb = self.plan(f("I currently work here", type="checkbox", **cur))
        self.assertEqual((cb.kind, cb.value), ("check", "true"))
        past = dict(section="experience", section_index=1)
        self.assertEqual(self.plan(f("To", type="month", **past)).value, "2025-09")
        self.assertEqual(self.plan(f("I currently work here", type="checkbox", **past)).value, "false")

    def test_split_month_and_year_selects(self):
        month = f("From - Month", type="select", section="experience", section_index=1,
                  options=opts("January", "February", "March", "April", "May", "June", "July"))
        self.assertEqual(self.plan(month).value, "5")  # June
        year = f("Graduation year", section="education", section_index=0)
        self.assertEqual(self.plan(year).value, "2027")


class GoogleEntryTests(unittest.TestCase):
    """Google Careers (2026-09-30): each job asks city, state and country apart; schools ask a start month."""

    def plan(self, field):
        return plan_field(field, PROFILE)

    def test_job_city_state_country(self):
        cur = dict(section="experience", section_index=0)
        self.assertEqual(self.plan(f("City", **cur)).value, "Chicago")
        self.assertEqual(self.plan(f("State / province", **cur)).value, "Illinois")
        self.assertEqual(self.plan(f("Country / Region", **cur)).value, "United States")
        remote = dict(section="experience", section_index=1)
        self.assertEqual(self.plan(f("City", **remote)).value, "Remote")
        self.assertEqual(self.plan(f("State / province", **remote)).kind, "skip")

    def test_school_start_month(self):
        edu = dict(section="education", section_index=0)
        self.assertEqual(self.plan(f("Start date", type="month", **edu)).value, "2025-08")
        self.assertEqual(self.plan(f("Start year", **edu)).value, "2025")
        self.assertEqual(self.plan(f("End date", type="month", **edu)).value, "2027-05")


class WrongSectionTests(unittest.TestCase):
    """Robinhood (2026-10-02): questions under "Equal Employment Opportunity" were read as a work entry and got the
    employer and job title as answers."""

    def test_a_question_never_gets_an_entry_value(self):
        for label in ("Do you have: a) any Personal/Familial Relationships (current Robinhood employees)?*",
                      "Robinhood adheres to applicable laws and regulations in relation to government officials.",
                      "The APM Program will begin July 2027. Does this start date work for you?"):
            for section in ("experience", "education"):
                a = plan_field(f(label, required=True, section=section), PROFILE)
                self.assertEqual(a.kind, "needs_you", (label, section, a.value))

    def test_entry_fields_still_map(self):
        self.assertEqual(plan_field(f("Company", section="experience"), PROFILE).value, "Acme Corp")
        self.assertEqual(plan_field(f("End date year", section="education"), PROFILE).value, "2027")


class ListWordingTests(unittest.TestCase):
    def test_answers_in_the_lists_own_words(self):
        rh = opts("I currently work at Robinhood as a full-time employee or intern",
                  "I have previously worked at Robinhood in a contractor role", "I have never worked at Robinhood")
        self.assertEqual(match_option("No", rh).label, "I have never worked at Robinhood")
        self.assertEqual(match_option("Male", opts("Woman", "Man", "Non-binary")).label, "Man")
        self.assertEqual(match_option("Yes", opts("Yes, I can start then", "No")).label, "Yes, I can start then")
        self.assertIsNone(match_option("No", opts("Not applicable", "Prefer not to say")))  # two fit: no guess
        self.assertIsNone(match_option("no", opts("Norway", "Nothing")))  # not part of a word
        vet = opts("Yes, I am a veteran, active member or reservist", "No, I am not a veteran, active member or reservist",
                   "I prefer to self-describe", "I don't wish to answer")
        self.assertEqual(match_option("I am not a protected veteran", vet).label, vet[1].label)
        self.assertEqual(match_option("United States", opts("UK", "US", "UAE", "Other")).label, "US")
        terms = opts("Earlier than Fall 2026", "Fall 2026", "Spring 2027", "Later than Summer 2027")
        self.assertEqual(match_option("May 2027", terms, "grad_date").label, "Spring 2027")
        mix = opts("I am authorized to work for any employer in the country outlined in this role (ie: citizen, permanent resident, etc.)",
                   "My current work authorization requires a renewal or sponsorship now or in the future (ie: H1-B, OPT, TN, etc.)",
                   "My status to work in the country in this role's location is unknown")
        self.assertEqual(match_option("Yes", mix), mix[0])
        places = opts("Los Gatos, IL, USA", "Los Gatos, CA, USA", "Los Gatos, WY, USA")
        self.assertEqual(match_option("Los Gatos, California", places, "city").label, "Los Gatos, CA, USA")
        self.assertIsNone(match_option("Los Gatos, California", places[:1], "city"))
        majors = opts("Business", "Business Analytics", "Design", "Information Science", "Information Technology")
        self.assertEqual(match_option("Data and Business Analytics", majors, "edu_field").label, "Business Analytics")
        self.assertEqual(match_option("Information, User Experience Research and Design", majors, "edu_field").label,
                         "Information Science")


class CompanySiteQuestionTests(unittest.TestCase):
    """Stripe, Roblox, Databricks (2026-10-02)."""

    def plan(self, label, type="text"):
        return plan_field(f(label, type=type, required=True), PROFILE)

    def test_factual_questions_come_from_the_resume(self):
        self.assertEqual(self.plan("Who is your current or previous employer?*").value, "Acme Corp")
        a = self.plan("What is your current or previous job title?*")
        self.assertEqual((a.kind, a.value), ("fill", "Analyst"))  # never a model draft
        from tailor.fill.mapper import resolve_key
        self.assertEqual(resolve_key(f("Please select the country where you currently reside.")), "country")
        self.assertEqual(self.plan("What is your graduation date?*").value, "May 2027")
        self.assertEqual(self.plan("At the time of application, are you 18+ years of age?*", "combobox").value, "Yes")
        self.assertEqual(self.plan("Why do you want to work for this employer?", "textarea").kind, "draft")

    def test_a_negative_statement_answers_a_yes_no_list(self):
        self.assertEqual(match_option("I am not a protected veteran", opts("Yes", "No")).label, "No")
        self.assertIsNone(match_option("I am not a protected veteran", opts("I identify as a veteran", "Decline")))


class USRoleTests(unittest.TestCase):
    """The person's rule (2026-10-02): on a U.S. role, "the country where the job is located" gets the U.S. answers."""
    STANDING = {"work_authorization_us": "Yes", "sponsorship_us": "No", "us_answers_for_us_roles": True}

    def plan(self, label, us_role, standing=None):
        return plan_field(f(label, type="combobox", required=True), PROFILE, {}, "", standing or self.STANDING, us_role)

    def test_country_unnamed_questions(self):
        auth = "Are you legally authorized to work in the country where the job is located?*"
        spons = "Will you now or in the future require company sponsorship to retain or extend your work authorization?*"
        self.assertEqual(self.plan(auth, True).value, "Yes")
        self.assertEqual(self.plan(spons, True).value, "No")
        self.assertEqual(self.plan(auth, False).kind, "needs_you")  # not known to be a U.S. role
        no_rule = {k: v for k, v in self.STANDING.items() if k != "us_answers_for_us_roles"}
        self.assertEqual(self.plan(auth, True, no_rule).kind, "needs_you")  # only with the person's rule
        self.assertEqual(self.plan("Are you authorized to work in Canada?", True).kind, "needs_you")

    def test_location_preferences_in_order(self):
        p = PROFILE.model_copy(update={"location_preferences": ["San Francisco", "New York", "Chicago"]})
        for label, want in (("What is your first location preference?*", "San Francisco"),
                            ("Second location preference*", "New York"), ("Third location preference*", "Chicago")):
            self.assertEqual(plan_field(f(label, type="combobox", required=True), p).value, want)
        self.assertEqual(plan_field(f("Second location preference*", required=True), PROFILE).kind, "needs_you")


class WorkEntryMatchTests(unittest.TestCase):
    """Google (2026-10-02) fills work entries from the resume: its own order, projects among jobs, "Efrei" for the
    employer. Entries are matched by the title already in them, not by position."""

    def setUp(self):
        from tailor.fill.profile import Experience as X
        self.p = PROFILE.model_copy(update={"experience": [
            X(employer="University of Michigan", title="Research Associate", location="Ann Arbor, MI",
              start=YearMonth(year=2026, month=5), current=True),
            X(employer="Summit Club", title="Brand Strategy Associate", location="Miami, FL", start=YearMonth(year=2025, month=6),
              end=YearMonth(year=2025, month=9)),
            X(employer="Acme Rental Group", title="Senior Sales Associate", location="San Jose, CA",
              start=YearMonth(year=2023, month=6), end=YearMonth(year=2024, month=5)),
            X(employer="Acme Rental Group", title="Sales Associate", location="San Jose, CA",
              start=YearMonth(year=2022, month=6), end=YearMonth(year=2023, month=6)),
            X(employer="Operations Center", title="Analytics Lead & Strategy", kind="project")]})

    def entries(self, rows):
        fields = []
        for i, (emp, title) in enumerate(rows):
            for lab, typ, val in (("Employer name", "combobox", emp), ("Job title", "text", title),
                                  ("Start Date Month", "combobox", ""), ("End Date Year", "number", ""),
                                  ("State / province", "combobox", "")):
                fields.append(f(lab, type=typ, id=f"{i}{lab}", section="experience", section_index=i, value=val))
        acts, notes = plan_fields_with_notes(fields, self.p, {}, url="https://www.google.com/about/careers/applications/apply/1")
        return {(x.section_index, x.label): a for x, a in zip(fields, acts)}, notes

    def test_google_prefill(self):
        got, notes = self.entries([("Efrei", "Research Associate"), ("", "Analytics Lead"), ("", "Sales Associate"),
                                   ("Acme Rental Group", "Senior Sales Associate")])
        self.assertEqual(got[(0, "Employer name")].value, "University of Michigan")  # "Efrei" corrected
        self.assertEqual(got[(0, "Start Date Month")].value, "May")
        self.assertEqual(got[(1, "Employer name")].kind, "needs_you")  # a project: remove it
        self.assertIn("project", got[(1, "Employer name")].reason)
        self.assertEqual(got[(1, "Job title")].kind, "skip")
        self.assertEqual(got[(2, "Employer name")].value, "Acme Rental Group")  # "Sales Associate", not "Senior..."
        self.assertEqual(got[(2, "End Date Year")].value, "2023")
        self.assertEqual(got[(3, "End Date Year")].value, "2024")
        self.assertEqual(got[(3, "State / province")].value, "California")
        self.assertEqual(notes, ["Add a work entry for Brand Strategy Associate at Summit Club (the site's Add button), "
                                 "then click Fill this page again."])

    def test_title_in_the_employer_box(self):
        got, notes = self.entries([("Brand Strategy Associate", ""), ("Acme Rental Group", "Senior Sales Associate")])
        self.assertEqual(got[(0, "Employer name")].value, "Summit Club")
        self.assertEqual(got[(0, "Job title")].value, "Brand Strategy Associate")

    def test_empty_entries_get_the_jobs_not_yet_on_the_page(self):
        got, notes = self.entries([("Acme Rental Group", "Senior Sales Associate"), ("", ""), ("", "")])
        self.assertEqual(got[(1, "Job title")].value, "Research Associate")
        self.assertEqual(got[(2, "Job title")].value, "Brand Strategy Associate")
        self.assertEqual(len(notes), 1)  # Sales Associate still needs an entry

    def test_nothing_filled_in_keeps_the_resume_order(self):
        got, notes = self.entries([("", ""), ("", "")])
        self.assertEqual((got[(0, "Job title")].value, got[(1, "Job title")].value),
                         ("Research Associate", "Brand Strategy Associate"))
        self.assertEqual([n.split(" (")[0] for n in notes], ["Add a work entry for Senior Sales Associate at Acme Rental Group",
                                                             "Add a work entry for Sales Associate at Acme Rental Group"])

    def test_how_many_entries_workday_must_add(self):
        from tailor.fill.mapper import entries_to_add
        fields = [f("Job Title", id="j", section="experience", section_index=0),
                  f("School or University", id="s", section="education", section_index=0)]
        self.assertEqual(entries_to_add(fields, self.p), {"experience": 3, "education": 1})


class WorkdayPhoneTests(unittest.TestCase):
    def test_extension_and_code_are_not_the_number(self):
        from tailor.fill.mapper import resolve_key
        self.assertEqual(plan_field(f("Phone Extension"), PROFILE).kind, "skip")
        self.assertEqual(resolve_key(f("Country Phone Code")), "phone_country_code")
        self.assertEqual(plan_field(f("Phone Number", required=True), PROFILE).value, "(555) 010-0000")


class AdobeEducationListTests(unittest.TestCase):
    """Adobe's Workday (2026-10-03): Degree says "Masters"/"Bachelors"; the person picked these fields of study."""
    DEGREES = ["Select One", "GED", "High School", "Associates", "Bachelors", "Masters", "Doctorate", "JD"]

    def test_degree_words(self):
        o = opts(*self.DEGREES)
        self.assertEqual(match_option("Master of Science", o, "edu_degree").label, "Masters")
        self.assertEqual(match_option("Master of Business Administration", o, "edu_degree").label, "Masters")
        self.assertEqual(match_option("Bachelor of Arts", o, "edu_degree").label, "Bachelors")

    def test_field_of_study_pick(self):
        p = PROFILE.model_copy(update={"education": [e.model_copy(update={"form_field": f}) for e, f in
                                                     zip(PROFILE.education, ["Business Administration", ""])]})
        self.assertEqual(plan_field(f("Field of Study", section="education", section_index=0), p).value, "Business Administration")
        self.assertEqual(plan_field(f("Field of Study", section="education", section_index=1), p).value, "Economics")
        o = opts("Business Administration/Management", "Business Analytics", "Information Technology")
        self.assertEqual(match_option("Business Administration", o, "edu_field").label, "Business Administration/Management")


class AdobeQuestionTests(unittest.TestCase):
    """Adobe's Application Questions and disclosures (2026-10-03)."""

    def test_never_worked_here_box(self):
        q = "Have you ever worked at Adobe in the following capacity:"
        boxes = [f(lab, type="checkbox", id=lab, name="workedAtAdobe", context=q, required=True)
                 for lab in ("Employee", "Intern", "Other", "I have not worked for Adobe in the past.")]
        acts, _ = plan_fields_with_notes(boxes, PROFILE, {})
        self.assertEqual([a.kind for a in acts], ["skip", "skip", "skip", "check"])
        mine = f("I have not worked for Acme Corp in the past.", type="checkbox", context=q)
        self.assertEqual(plan_field(mine, PROFILE).kind, "needs_you")  # Acme is on the resume: the person answers

    def test_veteran_wording(self):
        o = opts("I am not a veteran", "I identify as one or more of the classifications of protected veteran",
                 "I am a veteran but not a protected veteran", "I do not wish to self-identify")
        self.assertEqual(match_option("I am not a protected veteran", o).label, "I am not a veteran")

    def test_background_and_location(self):
        st = {"background_check_ok": "Yes", "relocation_ok_cities": ["San Francisco", "New York", "Chicago"]}
        bg = f("Are you willing to submit a background check during the hiring process?", type="combobox", required=True)
        self.assertEqual(plan_field(bg, PROFILE, {}, "", st).value, "Yes")
        crime = f("Have you ever been convicted of a crime? (background check)", type="combobox", required=True)
        self.assertEqual(plan_field(crime, PROFILE, {}, "", st).kind, "needs_you")
        q = f("Are you able to work on a daily basis in the work location listed for this position? If not, are you "
              "willing to relocate at your own expense?", type="combobox", required=True)
        self.assertEqual(plan_field(q, PROFILE, {}, "", st, role_location="San Francisco, CA").value, "Yes")
        self.assertEqual(plan_field(q, PROFILE, {}, "", st, role_location="Austin, TX").kind, "needs_you")
        self.assertEqual(plan_field(q, PROFILE, {}, "", st).kind, "needs_you")  # where the role is: not known
        office = f("Are you open to working in-person in one of our offices 25% of the time?", type="combobox", required=True)
        self.assertEqual(plan_field(office, PROFILE, {}, "", st, role_location="New York, NY").value, "Yes")
        st["in_person_max_percent"] = 80  # the person's limit (2026-10-03)
        ask = lambda q: plan_field(f(q, type="combobox", required=True), PROFILE, {}, "", st, role_location="Chicago, IL")
        self.assertEqual(ask("Are you open to working in-person in one of our offices 80% of the time?").value, "Yes")
        self.assertEqual(ask("Are you able to work in the office 4 days a week?").value, "Yes")
        self.assertEqual(ask("Are you able to work in the office 5 days a week?").kind, "needs_you")
        self.assertEqual(ask("This role is fully on-site. Are you able to work in person?").kind, "needs_you")

    def test_workday_links_are_one_job(self):
        from tailor.jobs.search import job_key
        a = "https://adobe.wd5.myworkdayjobs.com/external_experienced/job/San-Francisco/Product-Manager_R170556-1"
        b = "https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/San-Francisco/Product-Manager_R170556-1/apply/applyManually"
        self.assertEqual(job_key(a), job_key(b))


class ConsultingFormTests(unittest.TestCase):
    """Keystone, Charles River Associates, Brattle (2026-10-03)."""

    def setUp(self):
        self.p = PROFILE.model_copy(update={"location_preferences": ["San Francisco", "New York", "Chicago"],
                                            "skills": ["SQL", "Python", "Excel", "Jupyter"], "available_start": "2027-05-01"})

    def test_office_and_tool_checklists(self):
        q = "Please let us know which office locations you would be open to working in (select all that apply)"
        got = {lab: plan_field(f(lab, type="checkbox", context=q), self.p).kind
               for lab in ("Boston", "New York City", "San Francisco", "Seattle", "Chicago")}
        self.assertEqual(got, {"Boston": "skip", "New York City": "check", "San Francisco": "check", "Seattle": "skip",
                               "Chicago": "check"})
        q2 = "Please indicate if you have programming experience with any of the following (please select all that apply)"
        got2 = {lab: plan_field(f(lab, type="checkbox", context=q2), self.p).kind for lab in ("Python", "SQL", "R", "Stata")}
        self.assertEqual(got2, {"Python": "check", "SQL": "check", "R": "skip", "Stata": "skip"})

    def test_start_term_country_office_days(self):
        terms = opts("Spring 2027", "Summer 2027", "Fall 2027", "Other")
        self.assertEqual(match_option("May 1, 2027", terms, "available_start").label, "Summer 2027")
        keystone = opts("As Soon as possible", "May 2026", "January - March 2027", "April - June 2027", "After July 2027")
        self.assertEqual(match_option("May 1, 2027", keystone, "available_start").label, "April - June 2027")
        self.assertEqual(match_option("May 1, 2027", opts("April 2027", "May 2027"), "available_start").label, "May 2027")
        from tailor.fill.mapper import resolve_key
        self.assertEqual(resolve_key(f("What country are you applying from? *")), "country")
        self.assertEqual(resolve_key(f("Please indicate your preferred office location*")), "location_pref_1")
        self.assertEqual(resolve_key(f("Location preference #1*")), "location_pref_1")
        st = {"relocation_ok_cities": ["San Francisco", "New York", "Chicago"], "in_person_max_percent": 80}
        q = f("CRA asks colleagues to work in their designated office 3-4 days per week on average. Are you able to?",
              type="combobox", required=True)
        self.assertEqual(plan_field(q, self.p, {}, "", st, role_location="Boston, MA; Chicago, IL").value, "Yes")


class SchoolListTests(unittest.TestCase):
    def test_uc_campus_names(self):
        for names in (("University of California - Davis", "Davis City College"),
                      ("UC Davis", "Westmont College")):
            self.assertEqual(match_option("University of California, Davis", opts(*names), "edu_school").label,
                             names[0])


class StartDateTests(unittest.TestCase):
    """The person's answer (2026-10-02): earliest start May 1, 2027."""

    def setUp(self):
        self.p = PROFILE.model_copy(update={"available_start": "2027-05-01"})

    def test_when_can_you_start(self):
        self.assertEqual(plan_field(f("When can you start a new role?", required=True), self.p).value, "May 1, 2027")
        self.assertEqual(plan_field(f("Earliest start date", type="date"), self.p).value, "2027-05-01")
        self.assertEqual(plan_field(f("When can you start a new role?", required=True), PROFILE).kind, "needs_you")

    def test_does_this_start_date_work(self):
        ask = lambda q: plan_field(f(q, type="combobox", required=True), self.p).value
        self.assertEqual(ask("The APM Program will begin July 2027. Does this start date work for you?*"), "Yes")
        self.assertEqual(ask("Are you able to start full time employment in September 2026?"), "No")
        self.assertEqual(ask("Are you able to start full time employment in May 2027?"), "Yes")


class OptionMatchingTests(unittest.TestCase):
    def test_exact_synonym_and_no_match(self):
        o = opts("United States", "Canada")
        self.assertEqual(match_option("Canada", o).value, "1")
        self.assertIsNone(match_option("Narnia", o))
        deg = opts("Bachelor's", "Master's", "Doctorate")
        self.assertEqual(match_option("Master of Science", deg, "edu_degree").value, "1")


if __name__ == "__main__":
    unittest.main()


class SelfIdentifyTests(unittest.TestCase):
    """The person's answers (2026-10-06): no sponsorship, not a veteran, has a disability. Filled from standing answers,
    and on a check-one list only the box that IS the answer is ticked."""
    STANDING = {"disability": "Yes", "veteran": "I am not a protected veteran", "sponsorship_us": "No",
                "work_authorization_us": "Yes", "us_citizen": "Yes", "us_answers_for_us_roles": True}

    def plan(self, field):
        return plan_field(field, PROFILE, {}, url="https://acme.wd5.myworkdayjobs.com/x", standing=self.STANDING, us_role=True)

    def test_the_government_form_is_known_by_its_answers(self):
        dis = opts("Yes, I have a disability (or previously had a disability)",
                   "No, I do not have a disability and have not had one in the past", "I do not want to answer")
        a = self.plan(f("Please check one of the boxes below:", "select", options=dis))
        self.assertEqual((a.kind, a.value), ("select", "0"))

    def test_check_one_boxes_tick_only_the_answer(self):
        ctx = "Voluntary Self-Identification of Disability | Please check one of the boxes below:"
        yes = self.plan(f("Yes, I have a disability, or have had one in the past", "checkbox", context=ctx))
        no = self.plan(f("No, I do not have a disability and have not had one in the past", "checkbox", context=ctx))
        skip = self.plan(f("I do not want to answer", "checkbox", context=ctx))
        self.assertEqual((yes.kind, no.kind, skip.kind), ("check", "skip", "skip"))
        vet_no = self.plan(f("I am not a protected veteran", "checkbox", context="Veteran Status"))
        vet_yes = self.plan(f("I identify as one or more of the classifications of protected veteran", "checkbox",
                              context="Veteran Status"))
        self.assertEqual((vet_no.kind, vet_yes.kind), ("check", "skip"))

    def test_name_date_and_employee_id_on_the_form(self):
        ctx = "Voluntary Self-Identification of Disability"
        self.assertEqual(self.plan(f("Name", context=ctx)).value, "Jane Doe")
        self.assertRegex(self.plan(f("Date", context=ctx)).value, r"^\d\d/\d\d/20\d\d$")
        self.assertEqual(self.plan(f("Date Year", context=ctx)).value[:2], "20")
        self.assertEqual(self.plan(f("Employee ID", context=ctx)).kind, "skip")

    def test_military_service_citizenship_status_and_sponsorship(self):
        self.assertEqual(self.plan(f("Have you served in the U.S. military?", "select", options=opts("Yes", "No"))).value, "1")
        st = self.plan(f("What is your current immigration status?", "select",
                         options=opts("U.S. Citizen", "Permanent Resident", "H-1B", "F-1 OPT", "Other")))
        self.assertEqual(st.value, "0")
        sp = self.plan(f("Will you now or in the future require sponsorship for employment visa status (e.g., H-1B)?", "radio",
                         options=opts("Yes", "No")))
        self.assertEqual(sp.value, "1")

    def test_military_status_dropdown(self):  # Robinhood: a dropdown whose options load only when opened
        a = self.plan(f("What is your military status?", "combobox", required=True))
        self.assertEqual((a.kind, a.value), ("combobox", "I am not a protected veteran"))

    def test_military_status_list_means_never_served(self):
        mil = opts("I am on active duty", "I am part of the national guard or on reserve", "I have never served in the military",
                   "I identify as a protected veteran", "I identify as a non-protected veteran",
                   "I identify in multiple military status categories", "I don't wish to answer")
        a = self.plan(f("What is your military status?", "select", options=mil, required=True))
        self.assertEqual(a.value, "2")

    def test_the_label_decides_inside_an_eeo_section(self):  # Stripe, 2026-10-07
        st = dict(self.STANDING, pronouns="he/him")
        a = plan_field(f("Pronouns", "select", options=opts("She/her", "He/him", "They/them"),
                         context="Voluntary self-identification: gender, race, veteran and disability status"),
                       PROFILE, {}, standing=st, us_role=True)
        self.assertEqual(a.value, "1")

    def test_pronouns_in_a_text_box(self):  # Stripe, 2026-10-07: a plain text box
        st = dict(self.STANDING, pronouns="he/him")
        a = plan_field(f("Pronouns"), PROFILE, {}, standing=st, us_role=True)
        self.assertEqual((a.kind, a.value), ("fill", "he/him"))

    def test_a_disability_is_never_described_in_words(self):
        a = self.plan(f("Please describe your disability (optional)", "textarea"))
        self.assertNotEqual(a.kind, "fill")


class WorkOptionTests(unittest.TestCase):
    """Guidepoint (2026-10-07): "What work options are you open to?" and "What interview modes are you open to?"."""
    STANDING = {"in_person_max_percent": 80}

    def pick(self, label, context):
        return plan_field(f(label, "checkbox", context=context), PROFILE, {}, standing=self.STANDING)

    def test_work_options_follow_the_in_person_limit(self):
        ctx = "What work options are you open to? (check all that apply)"
        self.assertEqual(self.pick("Hybrid", ctx).kind, "check")
        self.assertEqual(self.pick("Remote", ctx).kind, "check")
        self.assertEqual(self.pick("Fully Onsite", ctx).kind, "skip")  # more than 80% in person

    def test_interview_modes_are_all_fine(self):
        ctx = "What interview modes are you open to? (check all that apply)"
        self.assertEqual(self.pick("Virtual", ctx).kind, "check")
        self.assertEqual(self.pick("Hybrid", ctx).kind, "check")


class AddressPartTests(unittest.TestCase):
    def test_legal_address_parts(self):  # Precision AQ (2026-10-07): "Legal Address - State" is the state, not the street
        from tailor.fill.mapper import resolve_key
        self.assertEqual(resolve_key(f("Legal Address - State*")), "state")
        self.assertEqual(resolve_key(f("Legal Address - Country*")), "country")
        self.assertEqual(resolve_key(f("Legal Address - Street Address 1*")), "address")
        self.assertEqual(resolve_key(f("Legal Address - Zip*")), "postal_code")
        self.assertEqual(resolve_key(f("State")), "state")


class SingleLocationTests(unittest.TestCase):
    def test_only_the_top_city_is_ticked(self):  # Databricks, 2026-10-07
        prof = PROFILE.model_copy(update={"location_preferences": ["San Francisco", "New York", "Chicago"]})
        ctx = "Please choose the single location that you're the most interested in, and we will discuss more with you"
        fields = [f(x, "checkbox", id=f"c{i}", context=ctx) for i, x in
                  enumerate(["Mountain View, CA", "San Francisco, CA", "Bellevue, WA", "Vancouver, Canada"])]
        acts, _ = plan_fields_with_notes(fields, prof)
        self.assertEqual([a.kind for a in acts], ["skip", "check", "skip", "skip"])
        fields = [f(x, "checkbox", id=f"d{i}", context=ctx) for i, x in enumerate(["Mountain View, CA", "Bellevue, WA"])]
        acts, _ = plan_fields_with_notes(fields, prof)
        self.assertEqual([a.kind for a in acts], ["check", "skip"])  # the Bay Area for San Francisco


class GraduationWordingTests(unittest.TestCase):
    def test_what_month_year_are_you_graduating(self):  # Flexport, 2026-10-07
        from tailor.fill.mapper import resolve_key
        self.assertEqual(resolve_key(f("What month/year are you graduating?", "combobox")), "grad_date")
        a = plan_field(f("What month/year are you graduating?", "select", options=opts("December 2026", "May 2027", "June 2027")), PROFILE, {})
        self.assertEqual(a.value, "1")


class LeverWordingTests(unittest.TestCase):  # Palantir on Lever, 2026-10-07
    def test_how_you_heard(self):
        from tailor.fill.mapper import resolve_key
        self.assertEqual(resolve_key(f("Please tell us how you heard about this opportunity.", "select")), "how_heard")

    def test_disability_form_name_and_date_by_their_box_names(self):
        n = plan_field(f("Name", name="eeo[disabilitySignature]"), PROFILE, {})
        d = plan_field(f("Date", name="eeo[disabilitySignatureDate]"), PROFILE, {})
        self.assertEqual((n.kind, n.value), ("fill", "Jane Doe"))
        self.assertEqual(d.kind, "fill")


class FillOnceTests(unittest.TestCase):
    """Answers entered once on My info (2026-10-07): blank means the form asks the person."""

    def setUp(self):
        from tailor.fill.profile import Education, YearMonth

        self.base = Profile(first_name="Jane", last_name="Doe", email="j@example.com", phone="1",
                            education=[Education(school="School of Information", degree="MS", field="Information",
                                                 end=YearMonth(year=2027, month=5), expected=True),
                                       Education(school="UCD", degree="BA", field="History", end=YearMonth(year=2021, month=6))])
        self.filled = self.base.model_copy(update={
            "gpa": "3.8", "high_school_year": "2017", "languages": "English (native), Mandarin (conversational)",
            "interviewed_at": "Google, Deloitte", "relatives_at": "none", "government_official": "No",
            "conflict_of_interest": "No"})

    def plan(self, field, profile):
        return plan_field(field, profile)

    def yn(self, label):
        return f(label, type="radio", options=opts("Yes", "No"), required=True)

    def said(self, label, profile=None):
        fld = self.yn(label)
        a = self.plan(fld, profile or self.filled)
        return next((o.label for o in fld.options if o.value == a.value), a.kind)

    def test_blank_means_the_person_is_asked(self):
        for fld in (f("GPA", required=True), f("High school graduation year", required=True),
                    self.yn("Have you previously interviewed with Google?"),
                    self.yn("Do you have any relatives employed by Robinhood?"),
                    self.yn("Are you a government official or related to one?")):
            self.assertEqual(self.plan(fld, self.base).kind, "needs_you", fld.label)

    def test_saved_answers_fill_the_form(self):
        self.assertEqual(self.plan(f("GPA"), self.filled).value, "3.8")
        self.assertEqual(self.plan(f("What year did you graduate from high school?"), self.filled).value, "2017")
        self.assertIn("Mandarin", self.plan(f("Languages"), self.filled).value)
        self.assertEqual(self.said("Have you previously interviewed with Google?"), "Yes")
        self.assertEqual(self.said("Have you ever interviewed with Stripe before?"), "No")
        self.assertEqual(self.said("Do you have any relatives or family members who work at Robinhood?"), "No")
        self.assertEqual(self.said("Are you, or is a family member, a government official?"), "No")
        self.assertEqual(self.said("Do you have any potential conflicts of interest?"), "No")

    def test_gpa_is_the_current_degrees_only_and_programming_languages_are_not_spoken_ones(self):
        second = f("GPA", section="education", section_index=1)
        self.assertNotEqual(self.plan(second, self.filled).value, "3.8")
        self.assertNotIn("Mandarin", self.plan(f("Programming languages", required=True), self.filled).value or "")

    def test_spoken_languages_tick_their_boxes(self):
        q = "What languages do you speak? (check all that apply)"
        tick = lambda opt: self.plan(f(opt, type="checkbox", context=q), self.filled).kind
        self.assertEqual((tick("Mandarin"), tick("English"), tick("Spanish")), ("check", "check", "skip"))

    def test_a_non_compete_stays_the_persons(self):
        a = self.plan(self.yn("Are you subject to a non-compete agreement?"), self.filled)
        self.assertEqual(a.kind, "needs_you")


class TranscriptTests(unittest.TestCase):
    def test_a_transcript_box_gets_the_saved_transcript_but_not_an_undergraduate_one(self):
        p = Profile(first_name="J", last_name="D", email="j@example.com", phone="1")
        self.assertEqual(plan_field(f("Please share your university transcript(s) (Unofficial copy accepted)", type="file"), p).kind,
                         "transcript")
        self.assertEqual(plan_field(f("Attach", type="file", name="transcript"), p).kind, "transcript")
        self.assertEqual(plan_field(f("Attach", type="file", name="question_123",
                                      context="Please share your university transcript(s) (Unofficial copy accepted)"), p).kind,
                         "transcript")
        under = plan_field(f("Undergraduate transcript", type="file", required=True), p)
        self.assertEqual((under.kind, under.value), ("transcript", "undergraduate"))  # the server checks one is saved

    def test_each_school_gets_its_own_gpa_and_a_plain_gpa_question_the_graduate_one(self):
        p = Profile(first_name="J", last_name="D", email="j@example.com", phone="1", gpa="3.84",
                    education_gpas={"Davis": "3.3"}, education=[
                        Education(school="University of Michigan School of Information", degree="Master of Science"),
                        Education(school="University of Michigan, Harbor", degree="Master of Business Administration"),
                        Education(school="University of California, Davis", degree="Bachelor of Arts")])
        gpa = lambda label, section="", i=0: plan_field(f(label, section=section, section_index=i), p).value
        self.assertEqual(gpa("What is your GPA?"), "3.84")
        self.assertEqual(gpa("What was your undergraduate GPA?"), "3.3")
        self.assertEqual(gpa("GPA", "education", 0), "3.84")
        self.assertIsNone(gpa("GPA", "education", 1))  # Harbor: none given
        self.assertEqual(gpa("GPA", "education", 2), "3.3")

    def test_a_gpa_asked_as_ranges_picks_the_range(self):
        # Databricks (2026-10-10): "What is your GPA" with "3.6 or above (out of 4.0)" ... ("no option matches '3.84'")
        s = opts("3.6 or above (out of 4.0)", "3.3 - 3.59 (out of 4.0)", "3.0 - 3.29 (out of 4.0)", "Below 3.0 (out of 4.0)")
        self.assertEqual(match_option("3.84", s, "gpa").label, "3.6 or above (out of 4.0)")
        self.assertEqual(match_option("3.45", s, "gpa").label, "3.3 - 3.59 (out of 4.0)")
        self.assertEqual(match_option("3.59", s, "gpa").label, "3.3 - 3.59 (out of 4.0)")
        self.assertEqual(match_option("2.9", s, "gpa").label, "Below 3.0 (out of 4.0)")
        # Cicero (JazzHR, 2026-10-13) lists ranges high to low
        c = opts("4.00-3.90", "3.89-3.80", "3.79-3.70", "Below 3.00")
        self.assertEqual(match_option("3.84", c, "gpa").label, "3.89-3.80")
        self.assertEqual(match_option("3.9", c, "gpa").label, "4.00-3.90")


class PlaceAbbreviationTests(unittest.TestCase):
    """Figma (2026-10-07): "Are you able to work from Figma's NYC or SF Hub?" and "From where do you intend to work?"
    with the options "NYC office", "SF office", "Other"."""

    def test_a_city_answer_picks_the_office_that_names_it(self):
        from tailor.fill.mapper import match_option
        o = opts("NYC office", "SF office", "Other")
        self.assertEqual(match_option("San Francisco, CA", o).label, "SF office")
        self.assertEqual(match_option("New York", o).label, "NYC office")
        self.assertIsNone(match_option("Austin, TX", o))

    def test_the_question_naming_the_persons_cities_is_yes(self):
        from tailor.fill.mapper import _location_fact
        standing = {"relocation_ok_cities": ["San Francisco", "New York", "Chicago"]}
        q = f("Are you able to work from Figma’s NYC or SF Hub?", type="radio", options=opts("Yes", "No"), required=True)
        self.assertEqual(_location_fact(q, standing, ""), "Yes")
        q2 = f("Are you able to work from our Austin office?", type="radio", options=opts("Yes", "No"), required=True)
        self.assertIsNone(_location_fact(q2, standing, ""))


class DateChoiceTests(unittest.TestCase):
    """DoorDash (2026-10-07): graduation "December 2026 - August 2027"; start "January / June / August 2027"."""

    def test_a_range_holding_the_date_and_the_earliest_start_after_it(self):
        from tailor.fill.mapper import match_option
        g = opts("Before September 2026", "December 2026 - August 2027", "December 2027 or later")
        self.assertEqual(match_option("May 2027", g, "grad_date").label, "December 2026 - August 2027")
        s = opts("January 2027", "June 2027", "August 2027")
        self.assertEqual(match_option("May 1, 2027", s, "available_start").label, "June 2027")
        self.assertEqual(match_option("June 1, 2027", s, "available_start").label, "June 2027")

    def test_an_internship_or_full_time_experience_question_is_yes_from_the_resume(self):
        from tailor.fill.profile import Experience
        p = Profile(first_name="J", last_name="D", email="j@example.com", phone="1",
                    experience=[Experience(title="Analyst", employer="Acme", kind="job", bullets=[])])
        q = f("Have you previously completed at least 1 internship or have relevant full-time experience?", type="radio",
              options=opts("yes", "no"), required=True)
        a = plan_field(q, p)
        self.assertEqual(next(o.label for o in q.options if o.value == a.value), "yes")

    def test_enrolled_in_school_and_highest_degree_come_from_the_education(self):
        # Simon-Kucher (2026-10-10): both were left to the person as "unrecognized required question"
        p = Profile(first_name="J", last_name="D", email="j@example.com", phone="1", education=[
            Education(school="University of Michigan School of Information", degree="Master of Science", expected=True),
            Education(school="University of Lakeview Harbor", degree="Master of Business Administration", expected=True),
            Education(school="UC Davis", degree="Bachelor of Arts")])
        q = f("Are you currently enrolled in school?", type="radio", options=opts("Yes", "No"), required=True)
        self.assertEqual(next(o.label for o in q.options if o.value == plan_field(q, p).value), "Yes")
        q = f("Please indicate the highest degree level you will have attained by your intended start date:", type="radio",
              options=opts("Bachelors", "MA/MS/MPH", "PhD, or other advanced degree (JD, PharmD, EdD, etc.)", "MBA",
                           "None of the above"), required=True)
        self.assertEqual(next(o.label for o in q.options if o.value == plan_field(q, p).value), "MBA")


class SecondFillTests(unittest.TestCase):
    def test_an_answer_already_in_the_box_is_never_redrafted(self):
        p = Profile(first_name="J", last_name="D", email="j@example.com", phone="1")
        q = f("Why do you want to join Figma?", type="textarea", value="I want to build tools designers love, because ...")
        self.assertEqual(plan_field(q, p).kind, "skip")
        self.assertEqual(plan_field(f("Why do you want to join Figma?", type="textarea"), p).kind, "draft")


class CampusTests(unittest.TestCase):
    """DoorDash (2026-10-07): the School list loads only when opened, and "University of Michigan" became Flint."""

    def test_the_city_picks_the_campus_and_no_city_is_no_guess(self):
        from tailor.fill.mapper import match_option
        o = opts("University of Michigan - Flint", "University of Michigan - Ann Arbor", "University of Michigan - Dearborn")
        self.assertEqual(match_option("University of Michigan\nAnn Arbor, MI", o, "edu_school").label, "University of Michigan - Ann Arbor")
        self.assertIsNone(match_option("University of Michigan", o, "edu_school"))

    def test_a_school_box_whose_list_is_not_loaded_still_gets_the_city(self):
        from tailor.fill.profile import Education, YearMonth
        p = Profile(first_name="J", last_name="D", email="j@example.com", phone="1",
                    education=[Education(school="University of Michigan School of Information", degree="MS", field="Information",
                                         location="Ann Arbor, MI", end=YearMonth(year=2027, month=5), expected=True)])
        a = plan_field(f("School", type="combobox", required=True, section="education", section_index=0), p)
        self.assertIn("Ann Arbor", a.value)


class CampusCityLineTests(unittest.TestCase):
    def test_the_city_line_never_hides_a_school_with_a_comma_in_its_name(self):
        from tailor.fill.mapper import match_option
        o = opts("University of California - Davis", "University of California - Berkeley")
        self.assertEqual(match_option("University of California, Davis\nDavis, CA", o, "edu_school").label,
                         "University of California - Davis")


class JazzHRTests(unittest.TestCase):
    """Cicero/MGT on JazzHR (2026-10-13)."""

    def test_a_referral_question_never_gets_the_persons_own_name(self):
        q = f("Who referred you to this position? Enter their first and last name here.", type="text")
        self.assertNotEqual(plan_field(q, PROFILE).kind, "fill")

    def test_address_boxes_under_one_label_use_their_placeholders(self):
        from tailor.fill.mapper import resolve_key

        self.assertEqual(resolve_key(f("Address", placeholder="City", type="text")), "city")
        self.assertEqual(resolve_key(f("Address", placeholder="State/Province", type="text")), "state")
        self.assertEqual(resolve_key(f("Address", placeholder="Postal", type="text")), "postal_code")
        self.assertEqual(resolve_key(f("Address", placeholder="Address", type="text")), "address")


class KeepWhatIsThereTests(unittest.TestCase):
    """L.E.K. on Oleeo (2026-10-13): each save reloads the step and the fill on arrival wrote over the person's edits."""

    def test_a_box_with_an_answer_is_kept_and_an_empty_one_filled(self):
        typed = f("First Name", type="text", value="Jordan T.")
        empty = f("Last Name", type="text", id="f2")
        acts = {a.field_id: a for a in plan_fields_with_notes([typed, empty], PROFILE)[0]}
        self.assertEqual(acts["f1"].kind, "skip")
        self.assertEqual(acts["f2"].kind, "fill")

    def test_a_dropdown_left_on_select_still_counts_as_empty(self):
        from tailor.fill.mapper import has_answer

        self.assertFalse(has_answer(f("Gender", type="select", value="", options=opts("Male", "Female"))))
        self.assertFalse(has_answer(f("Gender", type="select", value="x", options=[FieldOption(value="x", label="Select")] + opts("Male"))))
        self.assertTrue(has_answer(f("Gender", type="select", value="0", options=opts("Male", "Female"))))
        self.assertFalse(has_answer(f("Month", type="select", value="mm", options=opts("January"))))

    def test_google_careers_entries_are_still_corrected(self):
        e = f("Employer name", type="text", value="Wrong Co", section="experience")
        acts = plan_fields_with_notes([e], PROFILE, url="https://www.google.com/about/careers/applications/apply/1")[0]
        self.assertNotEqual(acts[0].reason, "already answered: kept as it is")

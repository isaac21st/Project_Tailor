"""Automatic resume tailoring: rules only, truth-checked, one page. Uses the real base resume when it is present."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.config import load_settings  # noqa: E402
from tailor.resume.auto import SUMMARY_MAX_CHARS, check_result, job_concepts, tailor_resume  # noqa: E402
from tailor.resume.schema import Resume, Section  # noqa: E402
from tailor.resume.validate import check_tailored, errors  # noqa: E402
from tailor.jobs import clean_job_text  # noqa: E402

SETTINGS = load_settings()
BASE_PATH = SETTINGS.profile_dir / "base_resume.json"

UX_JOB = """
User Experience Researcher

Responsibilities:
Conduct user research: interviews with end users, usability testing and field research to uncover user needs.
Create personas, wireframes and prototypes in Figma and run design reviews with product designers.
Facilitate workshops with client stakeholders and present research findings to the design and product team.

Minimum qualifications:
Experience in user experience research, usability testing and interaction design.
Portfolio showing prototypes, wireframes and user interviews.
Ability to communicate findings to stakeholders and cross-functional teams.

Preferred qualifications:
Experience with design systems and human-centered design.
"""

STRATEGY_JOB = """
Strategy Consultant

Responsibilities:
Develop strategic recommendations for client leaders based on primary research and market analysis.
Build operating models and business cases; present recommendations to senior management and executive stakeholders.
Lead client engagements and consulting workstreams with a hypothesis-driven approach.

Minimum qualifications:
An MBA or business degree. Strong consulting or strategy experience, analytical and quantitative skills.
Ability to present to executives and manage stakeholders in client engagements.

Preferred qualifications:
Experience in healthcare, operations or finance strategy.
"""


def _entry_titles(resume, needle):
    return [e.title for s in resume.sections for e in s.entries if needle in s.heading.upper()]


@unittest.skipUnless(BASE_PATH.exists(), "real base resume not present")
class AutoTailorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Resume.model_validate_json(BASE_PATH.read_text(encoding="utf-8"))
        cls.ux = tailor_resume(cls.base, UX_JOB, title="User Experience Researcher", settings=SETTINGS)
        cls.strategy = tailor_resume(cls.base, STRATEGY_JOB, title="Strategy Consultant", settings=SETTINGS)

    def test_both_results_pass_the_truth_check_and_fit_one_page(self):
        for result in (self.ux, self.strategy):
            self.assertTrue(result.report.applied, result.report.reason)
            self.assertEqual(errors(check_result(self.base, result)), [])
            self.assertTrue(result.report.fits)
            # what it added is only the skills it reported, and the plain check finds nothing else
            extra = {i.message for i in errors(check_tailored(self.base, result.resume))}
            for m in extra:
                self.assertTrue(any(t.lower() in m.lower() for t in result.report.allowed_terms + result.report.summary_terms), m)

    def test_a_ux_posting_leads_with_the_ms_and_the_ux_project(self):
        self.assertTrue(self.ux.report.education_first.startswith("MS"))
        self.assertIn("MOVEMENT FOR STUDENTS", _entry_titles(self.ux.resume, "PROJECT")[0])
        edu = _entry_titles(self.ux.resume, "EDUCATION")
        self.assertEqual(edu[0], "University of Michigan School of Information")
        self.assertIn("Davis", edu[-1])

    def test_a_strategy_posting_leads_with_the_consulting_project_and_keeps_the_persons_school_order(self):
        self.assertEqual(self.strategy.report.education_first, "MS in Information")  # the person's order, always
        projects = _entry_titles(self.strategy.resume, "PROJECT")
        self.assertIn("LAKESIDE", projects[0])
        self.assertNotIn("MOVEMENT FOR STUDENTS", " ".join(projects))
        edu = _entry_titles(self.strategy.resume, "EDUCATION")
        self.assertEqual(edu[0], "University of Michigan School of Information")  # then Harbor (2026-10-06)
        self.assertIn("Harbor", edu[1])

    def test_the_posting_term_is_used_only_when_the_posting_uses_it(self):
        def hf_text(result):
            return " ".join(b for s in result.resume.sections for e in s.entries if "LAKESIDE" in e.title
                            for r in e.roles for b in r.bullets)
        self.assertIn("grounded in primary research", hf_text(self.strategy))
        self.assertNotIn("primary research", hf_text(self.ux))

    def test_the_protected_bullet_is_never_dropped(self):
        for result in (self.ux, self.strategy):
            text = " ".join(b for s in result.resume.sections for e in s.entries for r in e.roles for b in r.bullets)
            if any("LAKESIDE" in t for t in _entry_titles(result.resume, "PROJECT")):
                self.assertIn("Dr. Example Person", text)

    def test_professional_entries_and_education_are_untouched(self):
        for result in (self.ux, self.strategy):
            self.assertEqual(len(_entry_titles(result.resume, "PROFESSIONAL")), len(_entry_titles(self.base, "PROFESSIONAL")))
            self.assertEqual(sorted(_entry_titles(result.resume, "EDUCATION")), sorted(_entry_titles(self.base, "EDUCATION")))
            # never a summary paragraph (the person's rule, 2026-09-26): at most one headline line, mirroring the role
            # (the tailoring guide, 2026-10-06), and only words the resume backs up
            if result.resume.summary:
                self.assertLessEqual(len(result.resume.summary), SUMMARY_MAX_CHARS)
                self.assertTrue(result.resume.summary.startswith(("User Experience Researcher candidate", "Strategy Consultant candidate")))

    def test_the_report_says_what_it_did(self):
        r = self.ux.report
        self.assertIn("Tailored to this posting", r.summary())
        self.assertTrue(r.summary().startswith(f"ATS match {r.ats_before}% -> {r.ats_after}%"))
        self.assertGreaterEqual(r.ats_after, r.ats_before)
        self.assertTrue(r.projects and r.skills_order and r.concepts)
        self.assertEqual(r.job_id, tailor_resume(self.base, UX_JOB, settings=SETTINGS).report.job_id)

    def test_the_all_round_default_is_one_page_and_truthful(self):
        from tailor.resume.auto import default_resume

        result = default_resume(self.base, SETTINGS)
        self.assertTrue(result.report.applied, result.report.reason)
        self.assertTrue(result.report.fits)
        self.assertEqual(errors(check_tailored(self.base, result.resume)), [])
        self.assertGreaterEqual(len(_entry_titles(result.resume, "PROJECT")), 2)

    def test_a_page_with_no_posting_is_left_alone(self):
        result = tailor_resume(self.base, "Apply now. First name. Last name. Email. Submit.", settings=SETTINGS)
        self.assertFalse(result.report.applied)
        self.assertIsNone(result.resume)
        self.assertIn("no job description", result.report.reason)


class ConceptTests(unittest.TestCase):
    def test_required_sections_weigh_more_than_boilerplate_and_the_title_counts(self):
        job = clean_job_text(UX_JOB)
        vec, signal = job_concepts(job, "User Experience Researcher")
        self.assertGreater(signal, 8)
        self.assertGreater(vec["ux_research"], vec["finance"])
        self.assertGreater(vec["design"], vec["healthcare"])

    def test_a_strategy_posting_emphasises_strategy(self):
        vec, _ = job_concepts(clean_job_text(STRATEGY_JOB), "Strategy Consultant")
        top = sorted(vec, key=vec.get, reverse=True)[:3]
        self.assertIn("strategy", top)


if __name__ == "__main__":
    unittest.main()


class TailoredOrderTests(unittest.TestCase):
    """2026-10-06: tailoring has to show: the bullets that match the posting first, and the skills it names first."""

    def test_skills_named_by_the_posting_lead(self):
        from tailor.resume.auto import _named_in, _split_skills

        items = _split_skills("Excel, SQL, Python, Jupyter (notebooks, plots), Pandas")
        self.assertEqual(items[3], "Jupyter (notebooks, plots)")
        posting = "you will write sql and python daily".lower()
        ranked = sorted(enumerate(items), key=lambda t: (-_named_in(t[1], posting), t[0]))
        self.assertEqual([x for _, x in ranked][:2], ["SQL", "Python"])



class FormOnlyPageTests(unittest.TestCase):
    def test_form_page_does_not_show_the_posting(self):
        from tailor.server.app import shows_posting
        posting = "\n".join(["Collaborate with cross-functional partners in design, data science, product marketing and engineering.",
                             "Conduct user research, gather feedback, and analyze data to identify creator community needs.",
                             "Support market research and competitive analysis to inform product decisions every quarter."])
        form = "Apply for this job\nFirst Name*\nResume/CV*\nAttach\nEducation\nSchool*\nPreferred First Name"
        self.assertFalse(shows_posting(form, posting))
        self.assertTrue(shows_posting(form + "\n" + posting, posting))


class KeywordTests(unittest.TestCase):
    """The tailoring guide (2026-10-06): extract the posting's keywords, add only what the resume backs up, score it."""

    def test_extraction_kinds_and_title(self):
        from tailor.resume.keywords import extract_keywords

        kw = extract_keywords(clean_job_text(UX_JOB), "User Experience Researcher")
        names = {k.name for k in kw.items}
        self.assertTrue({"User research", "Usability testing", "Personas", "Prototyping", "Figma"} <= names, names)
        self.assertIn("conduct", kw.verbs())
        self.assertEqual(kw.title, "User Experience Researcher")

    def test_titles_are_cleaned_the_way_a_resume_mirrors_them(self):
        from tailor.resume.keywords import clean_title, headline_title

        self.assertEqual(clean_title("[2027] Associate Product Manager, Early Career"), "Associate Product Manager")
        self.assertEqual(clean_title("Apply for Consultant - Americas | Application"), "Consultant")  # Simon-Kucher page title
        self.assertEqual(clean_title("Application Developer"), "Application Developer")
        self.assertEqual(headline_title(clean_title("Consultant - Automotive")), "Automotive Consultant")
        self.assertEqual(headline_title(clean_title("Consultant - Life Sciences Advisory, Health Segment - Campus 2027")),
                         "Life Sciences Advisory Consultant")
        self.assertEqual(clean_title("Associate Product Manager (College Grad 2027) @ Solace"), "Associate Product Manager")

    def test_boilerplate_and_visa_words_are_not_keywords(self):
        from tailor.resume.keywords import extract_keywords

        job = clean_job_text("Analyst\n\nRequirements:\nSQL and Excel. Students on CPT or OPT are welcome.\n"
                             "We will NEVER ask for bank details.\nWe are an equal opportunity employer. Salary range $80,000.")
        names = {k.name for k in extract_keywords(job, "Analyst").items}
        self.assertTrue({"SQL", "Excel"} <= names)
        self.assertFalse({"CPT", "OPT", "NEVER"} & names)

    def test_a_skill_is_added_only_with_evidence(self):
        from tailor.resume.auto import add_keywords
        from tailor.resume.keywords import extract_keywords

        base = Resume.model_validate_json(BASE_PATH.read_text(encoding="utf-8")) if BASE_PATH.exists() else None
        if base is None:
            self.skipTest("real base resume not present")
        import copy
        job = clean_job_text("Consultant\n\nRequirements:\nQualitative research and quantitative analysis. Tableau and "
                             "PowerPoint. Financial modeling.")
        kw = extract_keywords(job, "Consultant")
        r = copy.deepcopy(base)
        added = {t for _, t in add_keywords(r, base, kw)}
        self.assertIn("Qualitative research", added)  # interviews synthesized into findings
        self.assertFalse({"Tableau", "PowerPoint", "Financial modeling"} & added)  # nothing on the resume shows them
        r2 = copy.deepcopy(base)
        self.assertIn("PowerPoint", {t for _, t in add_keywords(r2, base, kw, confirmed=("PowerPoint",))})  # "I have this"


class TitleCleanupTests(unittest.TestCase):
    def test_program_names_levels_and_full_time_leave_the_role(self):
        from tailor.resume.keywords import clean_title

        self.assertEqual(clean_title("2027 Amazon Leadership Accelerator (ALA) - Product Manager Full Time"), "Product Manager")
        self.assertEqual(clean_title("Product Management II - Windows Cloud"), "Product Manager, Windows Cloud")
        self.assertEqual(clean_title("Rotational Product Manager"), "Rotational Product Manager")
        self.assertEqual(clean_title("Product Design, Entry-Level (2027 start)"), "Product Designer")
        self.assertEqual(clean_title("Consultant - Life Sciences Advisory, Health Segment - Campus 2027"), "Consultant, Life Sciences Advisory")


class AdditionalSkillsTests(unittest.TestCase):
    def test_a_confirmed_skill_the_resume_does_not_show_goes_on_an_additional_line(self):
        from tailor.resume.auto import add_keywords
        from tailor.resume.keywords import extract_keywords
        from tailor.resume.schema import Header, LabeledLine
        base = Resume(header=Header(name="A", contact=[]), sections=[
            Section(heading="SKILLS", lines=[LabeledLine(label="Analytics", text="Excel, SQL")])])
        kw = extract_keywords(clean_job_text("Analyst\n\nRequirements:\nExperience running A/B testing and experiments.\n" * 3), "Analyst")
        prepared = base.model_copy(deep=True)
        self.assertEqual(add_keywords(prepared, base, kw), [])  # not shown, not confirmed: never added
        added = add_keywords(prepared, base, kw, confirmed=("A/B testing",))
        self.assertEqual(added, [("Additional", "A/B testing")])
        self.assertEqual(prepared.sections[0].lines[-1].text, "A/B testing")

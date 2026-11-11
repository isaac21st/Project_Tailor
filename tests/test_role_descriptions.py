"""A form's "Role Description" boxes, tailored to the posting (the person, 2026-10-30: "tailor the experience to the job
itself, that is what Simplify cannot do"): each job's own bullets, the best-fitting first, nothing the resume lacks."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tailor.config import Settings  # noqa: E402
from tailor.fill.forms import FormField  # noqa: E402
from tailor.fill.mapper import plan_fields_with_notes  # noqa: E402
from tailor.fill.profile import Experience, Profile  # noqa: E402
from tailor.resume.auto import role_descriptions  # noqa: E402
from tailor.resume.schema import Resume  # noqa: E402

BASE = Resume.model_validate_json((ROOT / "examples" / "profile" / "base_resume.json").read_text(encoding="utf-8"))
OWN = {b.replace("**", "") for s in BASE.sections for e in s.entries for r in e.roles for b in r.bullets}

DATA_JOB = """Data Analyst, Operations
About the role
You will build dashboards in Tableau and SQL, analyze data, track metrics and KPIs, and report insights to leaders.
Responsibilities
- Build and maintain dashboards and reporting for operations metrics
- Analyze data to find trends and turn analytics into recommendations
- Partner with teams to define KPIs and measure performance
Qualifications
- Experience with Tableau, SQL, Excel and data visualization
- Strong quantitative and analytical skills
""" * 2

CONSULTING_JOB = """Project Consultant
About the role
You will interview stakeholders, map current processes, and recommend workflow changes that client leadership adopts.
Responsibilities
- Lead stakeholder interviews and workshops to understand client processes
- Map workflows and identify process improvements and change management needs
- Present recommendations to client leadership and drive adoption
- Coordinate project teams and manage implementation timelines
Qualifications
- Experience with process improvement, stakeholder management and client service
- Strong communication and presentation skills
""" * 2


class RoleDescriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = Settings(data_dir=Path(cls.tmp.name) / "data")
        cls.settings.ensure_dirs()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def first(self, job: str) -> str:
        return role_descriptions(BASE, job, settings=self.settings)[("BRIGHTPATH CLINICS", "Operations Analyst")][0]

    def test_the_best_fitting_bullet_comes_first(self):
        self.assertIn("dashboard", self.first(DATA_JOB))
        self.assertIn("Interviewed 25 clinicians", self.first(CONSULTING_JOB))

    def test_each_job_keeps_only_its_own_true_bullets(self):
        d = role_descriptions(BASE, CONSULTING_JOB, settings=self.settings)
        acme = d[("ACME RENTAL GROUP", "Sales Associate")]
        self.assertTrue(all("clinic" not in b.lower() for b in acme))  # nothing moved from another job
        for bullets in d.values():
            self.assertLessEqual(len(bullets), 4)
            for b in bullets:
                self.assertIn(b, OWN)  # no approved re-wordings in this data folder: the resume's own words
                self.assertNotIn("**", b)

    def test_no_posting_keeps_the_resumes_order(self):
        d = role_descriptions(BASE, "", settings=self.settings)
        self.assertTrue(d[("BRIGHTPATH CLINICS", "Operations Analyst")][0].startswith("Built a weekly Tableau dashboard"))


class WantedSkillsTests(unittest.TestCase):
    """Workday's "Type to Add Skills" (the person, 2026-10-30): the skills the posting wants most that the person has."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = Settings(data_dir=Path(cls.tmp.name) / "data")
        cls.settings.ensure_dirs()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_only_skills_the_resume_shows_heaviest_first(self):
        from tailor.resume.auto import wanted_skills
        from tailor.resume.keywords import extract_keywords, resume_text
        from tailor.jobs import clean_job_text
        got = wanted_skills(BASE, DATA_JOB, settings=self.settings)
        self.assertIn("Dashboards", got)  # asked for, and the resume shows it
        asked = {k.name: k for k in extract_keywords(clean_job_text(DATA_JOB), "").items}
        for skill in got:
            self.assertTrue(asked[skill].found_in(resume_text(BASE)), skill)  # nothing the person does not show
        weights = [asked[s].weight for s in got]
        self.assertEqual(weights, sorted(weights, reverse=True))  # what the posting wants most first
        self.assertEqual(wanted_skills(BASE, "", settings=self.settings), [])

    def test_the_skills_box_gets_them_one_per_line(self):
        from tailor.fill.mapper import plan_fields_with_notes
        p = FormTests.PROFILE.model_copy(update={"wanted_skills": ["Tableau", "SQL"]})
        box = FormField(id="k", label="Type to Add Skills", type="combobox")
        a = plan_fields_with_notes([box], p)[0][0]
        self.assertEqual((a.kind, a.value), ("multi", "Tableau\nSQL"))
        self.assertEqual(plan_fields_with_notes([box], FormTests.PROFILE)[0][0].kind, "skip")


class FormTests(unittest.TestCase):
    PROFILE = Profile(first_name="Jordan", last_name="Rivera", email="j@example.com", phone="555",
                      experience=[Experience(employer="Brightpath Clinics", title="Operations Analyst",
                                             bullets=["First bullet.", "Second bullet."],
                                             description=["Second bullet.", "First bullet."])])

    def plan(self, **kw):
        f = FormField(id="d", label="Role Description", type="textarea", section="experience", **kw)
        actions, _ = plan_fields_with_notes([FormField(id="t", label="Job Title", section="experience",
                                                       value="Operations Analyst"), f], self.PROFILE)
        return next(a for a in actions if a.field_id == "d")

    def test_the_tailored_order_fills_the_box_as_a_list(self):
        self.assertEqual(self.plan().value, "• Second bullet.\n• First bullet.")

    def test_a_short_box_keeps_whole_bullets(self):
        self.assertEqual(self.plan(maxlength=20).value, "• Second bullet.")


if __name__ == "__main__":
    unittest.main()

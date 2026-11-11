"""Tailoring for people unlike the first user (2026-11-04): a nurse, an engineer and a teacher, from plain resume text."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tailor.config import Settings, apply_environment, load_settings  # noqa: E402
from tailor.jobs import clean_job_text  # noqa: E402
from tailor.profile.import_resume import parse_resume_text  # noqa: E402
from tailor.resume.auto import check_result, role_descriptions, tailor_resume, wanted_skills  # noqa: E402
from tailor.resume.keywords import extract_keywords, resume_text  # noqa: E402
from tailor.resume.validate import errors  # noqa: E402

FX = ROOT / "tests" / "fixtures"
JOBS = {
    "nurse": ("Nurse Manager", "Nurse Manager, Cardiac Services. Lead nursing staff on a cardiac unit.\nResponsibilities:\nStaff scheduling, patient safety, "
              "quality improvement, budget management, discharge planning and mentoring new graduate nurses.\nQualifications:\nBSN required, RN license, "
              "charge nurse experience, Epic, ACLS and BLS certification, and strong communication skills.\n"),
    "engineer": ("Staff Software Engineer", "Staff Software Engineer, Platform. Build reliable cloud infrastructure.\nResponsibilities:\nKubernetes, CI/CD pipelines, AWS, "
                 "service migrations, design reviews and cost optimization.\nQualifications:\nGo, Python, Terraform, Docker, PostgreSQL and distributed systems.\n"),
    "teacher": ("Instructional Designer", "Instructional Designer at an education company.\nResponsibilities:\nCreate curriculum, analyze student data, partner with teachers, "
                "learning materials, curriculum design and data analysis.\nQualifications:\nTeaching experience, Excel, Salesforce and strong writing.\n"),
}


class AnyFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        apply_environment(load_settings())
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = Settings(data_dir=Path(cls.tmp.name))
        cls.settings.ensure_dirs()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def resume(self, who):
        return parse_resume_text((FX / f"{who}.txt").read_text(encoding="utf-8")).resume

    def test_a_postings_own_tools_and_phrases_are_seen(self):
        names = {k.name for k in extract_keywords(clean_job_text(JOBS["engineer"][1]), "Staff Software Engineer").items}
        for tool in ("Kubernetes", "Terraform", "Docker", "PostgreSQL"):
            self.assertIn(tool, names)
        self.assertIn("service migrations", names)
        self.assertNotIn("CI", names)  # half of CI/CD
        nurse = {k.name for k in extract_keywords(clean_job_text(JOBS["nurse"][1]), "Nurse Manager").items}
        self.assertTrue({"patient safety", "discharge planning"} <= nurse, nurse)

    def test_the_skills_box_gets_what_the_person_really_has(self):
        got = wanted_skills(self.resume("engineer"), JOBS["engineer"][1], title=JOBS["engineer"][0], settings=self.settings)
        self.assertTrue({"Kubernetes", "Terraform", "Docker"} <= set(got), got)
        got = wanted_skills(self.resume("nurse"), JOBS["nurse"][1], title=JOBS["nurse"][0], settings=self.settings)
        self.assertIn("discharge planning", got)
        self.assertNotIn("budget management", got)  # asked for, not on this resume

    def test_each_person_gets_a_truthful_one_page_resume(self):
        for who, (title, job) in JOBS.items():
            with self.subTest(who=who):
                base = self.resume(who)
                result = tailor_resume(base, job * 3, title=title, settings=self.settings, out_pdf=Path(self.tmp.name) / f"{who}.pdf", work_dir=self.settings.tmp_dir)
                self.assertIsNotNone(result.resume, result.report.reason)
                self.assertEqual(errors(check_result(base, result)), [])
                self.assertTrue(result.report.fits)
                said = resume_text(base).lower()
                for k in result.report.keywords_added:  # a skill is added only with evidence on the resume
                    self.assertTrue(k.lower().split()[0][:4] in said, k)

    def test_role_descriptions_keep_each_job_to_its_own_bullets(self):
        base = self.resume("engineer")
        d = role_descriptions(base, JOBS["engineer"][1] * 3, title=JOBS["engineer"][0], settings=self.settings)
        self.assertIn("Kubernetes", d[("Northwind Software", "Senior Software Engineer")][0])
        self.assertTrue(all("Kubernetes" not in b for b in d[("Contoso Labs", "Software Engineer")]))


if __name__ == "__main__":
    unittest.main()

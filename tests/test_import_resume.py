"""Reading a resume into Tailor's form: a new person's first step. Checked by reading back a resume Tailor rendered."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tailor.config import Settings, apply_environment, load_settings  # noqa: E402
from tailor.fill.profile import profile_from_resume  # noqa: E402
from tailor.profile.import_resume import import_resume, parse_resume_text  # noqa: E402
from tailor.resume.render import render_resume  # noqa: E402
from tailor.resume.schema import Resume  # noqa: E402

EXAMPLE = Resume.model_validate_json((ROOT / "examples" / "profile" / "base_resume.json").read_text(encoding="utf-8"))
PLAIN = """Sam Lee
sam.lee@example.com | (555) 010-2030 | linkedin.com/in/samlee | samlee.dev

EXPERIENCE
Northwind Traders | Operations Analyst            Jan 2022 - Present
San Francisco, CA
- Built a weekly dashboard that cut reporting time by 40%
  across three regional teams
- Led a 5-person team
Senior Analyst                                    Jan 2024 - Present
- Ran pricing reviews
Contoso                                           June 2019 - Dec 2021
Sales Associate
- Grew add-on sales 12%

EDUCATION
State University                                  Sep 2015 - May 2019
Bachelor of Arts in History

Skills
Analytics: SQL, Tableau
Languages: English, Spanish
"""


class PlainTextTests(unittest.TestCase):
    def setUp(self):
        self.got = parse_resume_text(PLAIN)
        self.r = self.got.resume
        self.sec = {s.heading: s for s in self.r.sections}

    def test_header(self):
        self.assertEqual(self.r.header.name, "Sam Lee")
        urls = [c.url or "" for c in self.r.header.contact]
        self.assertIn("mailto:sam.lee@example.com", urls)
        self.assertTrue(any("linkedin.com/in/samlee" in u for u in urls))
        self.assertTrue(any("010-2030" in c.text for c in self.r.header.contact))

    def test_jobs_roles_and_wrapped_bullets(self):
        jobs = self.sec["PROFESSIONAL EXPERIENCE"].entries
        self.assertEqual([e.title for e in jobs], ["Northwind Traders", "Contoso"])
        self.assertEqual(jobs[0].location, "San Francisco, CA")
        self.assertEqual([r.title for r in jobs[0].roles], ["Operations Analyst", "Senior Analyst"])
        self.assertEqual(jobs[0].roles[0].bullets[0], "Built a weekly dashboard that cut reporting time by 40% across three regional teams")
        self.assertEqual(jobs[1].roles[0].title, "Sales Associate")
        self.assertEqual(jobs[1].roles[0].dates, "June 2019 - Dec 2021")

    def test_education_and_skills(self):
        self.assertEqual(self.sec["EDUCATION"].entries[0].roles[0].title, "Bachelor of Arts in History")
        self.assertEqual(self.sec["SKILLS"].lines[0].label, "Analytics")

    def test_the_words_are_the_persons_own(self):
        words = " ".join(PLAIN.split())
        for e in self.sec["PROFESSIONAL EXPERIENCE"].entries:
            for role in e.roles:
                for b in role.bullets:
                    self.assertIn(b, words)

    def test_it_says_what_it_is_unsure_of(self):
        got = parse_resume_text("Sam Lee\n\nEXPERIENCE\nContoso\n- Sold things\n")
        joined = " ".join(got.warnings)
        self.assertIn("No email", joined)
        self.assertIn("No dates", joined)
        with self.assertRaises(ValueError):
            parse_resume_text("just a paragraph about me")

    def test_headings_of_the_persons_own(self):
        text = PLAIN.replace("Skills\n", "CONSULTING & PROJECT EXPERIENCE\nCity Study                     Feb 2026 - Apr 2026\nData Lead\n- Analyzed ridership\n\nSkills\n")
        sec = {s.heading: s for s in parse_resume_text(text).resume.sections}
        self.assertEqual([e.title for e in sec["PROJECT EXPERIENCE"].entries], ["City Study"])
        self.assertEqual(len(sec["PROFESSIONAL EXPERIENCE"].entries), 2)

    def test_other_dates_and_a_year_inside_a_sentence(self):
        text = """Sam Lee
sam@example.com

PROJECTS
City Study                                  Winter 2026
Data Lead
- Organized three public spreadsheets (property tax; 2015–2024) into
  charts, and post-
  launch checks
Datathon                                    February 1 – 6, 2026
Analyst
- Won second place
"""
        sec = {s.heading: s for s in parse_resume_text(text).resume.sections}
        got = sec["PROJECT EXPERIENCE"].entries
        self.assertEqual([(e.title, e.roles[0].dates) for e in got], [("City Study", "Winter 2026"), ("Datathon", "February 1 - 6, 2026")])
        self.assertEqual(got[0].roles[0].bullets, ["Organized three public spreadsheets (property tax; 2015–2024) into charts, and post-launch checks"])

    def test_other_professions(self):
        fx = ROOT / "tests" / "fixtures"
        teacher = {s.heading: s for s in parse_resume_text((fx / "teacher.txt").read_text()).resume.sections}
        self.assertEqual([e.title for e in teacher["EDUCATION"].entries], ["Howard University", "Spelman College"])
        self.assertEqual([e.roles[0].dates for e in teacher["EDUCATION"].entries], ["May 2020", "May 2016"])
        nurse = parse_resume_text((fx / "nurse.txt").read_text()).resume
        p = profile_from_resume(nurse)
        self.assertEqual((p.first_name, p.last_name), ("Maria", "Santos"))
        skills = next(s for s in nurse.sections if s.heading == "SKILLS")
        self.assertEqual(skills.lines[0].label, "Certifications")
        eng = {s.heading: s for s in parse_resume_text((fx / "engineer.txt").read_text()).resume.sections}
        self.assertEqual([len(r.bullets) for e in eng["PROFESSIONAL EXPERIENCE"].entries for r in e.roles], [3, 2])

    def test_it_makes_a_usable_profile(self):
        p = profile_from_resume(self.r)
        self.assertEqual((p.first_name, p.last_name, p.email), ("Sam", "Lee", "sam.lee@example.com"))
        self.assertEqual(p.experience[0].start.year, 2022)
        self.assertEqual(p.education[0].end.year, 2019)


class RoundTripTests(unittest.TestCase):
    def test_a_resume_tailor_rendered_reads_back(self):
        apply_environment(load_settings())
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            settings.ensure_dirs()
            pdf = Path(tmp) / "r.pdf"
            render_resume(EXAMPLE, pdf, settings)
            got = import_resume(pdf).resume
        want = {s.heading: s for s in EXAMPLE.sections}
        have = {s.heading: s for s in got.sections}
        self.assertEqual(got.header.name, EXAMPLE.header.name)
        self.assertEqual(sum(len(r.bullets) for e in have["PROFESSIONAL EXPERIENCE"].entries for r in e.roles),
                         sum(len(r.bullets) for e in want["PROFESSIONAL EXPERIENCE"].entries for r in e.roles))
        self.assertEqual([e.title for e in have["PROFESSIONAL EXPERIENCE"].entries], [e.title for e in want["PROFESSIONAL EXPERIENCE"].entries])
        self.assertEqual(len(have["EDUCATION"].entries), len(want["EDUCATION"].entries))


if __name__ == "__main__":
    unittest.main()

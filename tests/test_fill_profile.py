"""Profile derivation tests: dates, names, degrees, and (if present) the real resume."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.config import load_settings  # noqa: E402
from tailor.fill.profile import parse_dates, profile_from_resume, titleize  # noqa: E402
from tailor.resume.schema import Resume  # noqa: E402

REAL = load_settings().data_dir / "profile" / "base_resume.json"


def ym(t):
    return None if t is None else (t.year, t.month)


class DateTests(unittest.TestCase):
    def check(self, text, start, end, current=False, expected=False):
        s, e, c, x = parse_dates(text)
        self.assertEqual((ym(s), ym(e), c, x), (start, end, current, expected), text)

    def test_formats_seen_on_the_resume(self):
        self.check("May 2026 - Present", (2026, 5), None, current=True)
        self.check("June 2025 – September 2025", (2025, 6), (2025, 9))
        self.check("June 2022 - June 2023", (2022, 6), (2023, 6))
        self.check("February 1 – 6, 2026", (2026, 2), (2026, 2))
        self.check("Expected May 2027", None, (2027, 5), expected=True)
        self.check("June 2021", None, (2021, 6))

    def test_unparseable_text_gives_nothing(self):
        self.check("sometime", None, None)


class NameTests(unittest.TestCase):
    def test_all_caps_becomes_normal_case(self):
        self.assertEqual(titleize("ACME RENTAL GROUP"), "Acme Rental Group")
        self.assertEqual(titleize("UNIVERSITY OF MICHIGAN, SCHOOL OF INFORMATION"),
                         "University of Michigan, School of Information")
        self.assertEqual(titleize("LAKESIDE HEALTH & COMMUNITY HEALTH ALLIANCE"),
                         "Lakeside Health & Community Health Alliance")
        self.assertEqual(titleize("CAMPUS DATA CHALLENGE"), "Campus Data Challenge")

    def test_mixed_case_is_left_alone_and_overrides_win(self):
        self.assertEqual(titleize("Example University"), "Example University")
        self.assertEqual(titleize("SUMMIT-CLUB", {"SUMMIT-CLUB": "Summit Club"}), "Summit Club")


@unittest.skipUnless(REAL.exists(), "real resume data not present")
class RealResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = profile_from_resume(Resume.model_validate_json(REAL.read_text(encoding="utf-8")),
                                    overrides={"SUMMIT-CLUB": "Summit Club",
                                               "CAMPUS SHUTTLE APP (PROTOTYPE)":
                                               "Campus Shuttle App (Prototype)"})

    def test_identity(self):
        p = self.p
        self.assertEqual((p.first_name, p.last_name), ("Jordan", "Rivera"))
        self.assertEqual(p.email, "jordan@example.com")
        self.assertEqual(p.phone, "(555) 010-2000")
        self.assertIn("linkedin.com/in/jordanrivera", p.linkedin_url)
        self.assertEqual(p.portfolio_url, "https://jordanrivera.example/")
        self.assertIsNone(p.city)  # never guessed

    def test_education_is_split_into_degree_and_field(self):
        rows = [(e.degree, e.field, ym(e.end), e.expected) for e in self.p.education]
        self.assertEqual(rows, [
            ("Master of Business Administration", "Data and Business Analytics", (2027, 5), True),
            ("Master of Science", "Information, User Experience Research and Design", (2027, 5), True),
            ("Bachelor of Arts", "Cultural History", (2021, 6), False),
        ])

    def test_experience_dates_and_current_job(self):
        jobs = [(x.employer, x.title, ym(x.start), ym(x.end), x.current) for x in self.p.experience if x.kind == "job"]
        self.assertEqual(jobs[0], ("University of Michigan, School of Information", "Research Associate",
                                   (2026, 5), None, True))
        self.assertEqual(jobs[1][0], "Summit Club")
        self.assertEqual(self.p.current_job().title, "Research Associate")

    def test_projects_are_kept_but_marked(self):
        projects = [x.employer for x in self.p.experience if x.kind == "project"]
        for name in ("Lakeside Health & Community Health Alliance", "Campus Data Challenge",
                     "Campus Shuttle App (Prototype)", "Center for Global Operations"):
            self.assertIn(name, projects)

    def test_skills_split_outside_parentheses_and_awards_excluded(self):
        self.assertIn("Claude Code (product prototyping)", self.p.skills)
        self.assertIn("Pandas", self.p.skills)
        self.assertFalse(any("A-Pop" in s for s in self.p.skills))

    def test_facts_text_carries_confirmed_details(self):
        text = self.p.facts_text()
        for needle in ("Claude Code", "Tisa Johnson-Hooper", "88.4%", "60 competing teams"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(REAL.exists(), "real resume data not present")
class EducationOrderTests(unittest.TestCase):
    """The person's choice (2026-10-03): School of Information first, then Harbor, then UCD, though the resume puts the
    MBA first."""

    def test_order_and_start_months(self):
        resume = Resume.model_validate_json(REAL.read_text(encoding="utf-8"))
        p = profile_from_resume(resume, education_order=["School of Information", "Harbor School of Business", "Davis"],
                                education_starts={"University of California, Davis": "2018-09"})
        self.assertEqual([e.degree for e in p.education], ["Master of Science", "Master of Business Administration",
                                                           "Bachelor of Arts"])
        self.assertEqual((p.education[2].start.year, p.education[2].start.month), (2018, 9))
        same = profile_from_resume(resume)
        self.assertEqual(same.education[0].degree, "Master of Business Administration")  # no order given: the resume's



class RankLocationTests(unittest.TestCase):
    """2026-10-06: the person's cities in the order the posting offers them; Bay Area offices count as San Francisco."""

    def test_posting_order(self):
        from tailor.fill.profile import rank_locations

        mine = ["San Francisco", "New York", "Chicago"]
        self.assertEqual(rank_locations(mine, "APM, 2027 Start - Mountain View, New York, San Bruno or San Jose")[:2],
                         ["Mountain View", "New York"])
        self.assertEqual(rank_locations(mine, "Bellevue, Washington; Mountain View, California; San Francisco, California")[0],
                         "San Francisco")  # San Francisco itself is offered: it leads (Databricks, 2026-10-07)
        self.assertEqual(rank_locations(mine, "Consultant, Market Access | New York, NY, USA"),
                         ["New York", "San Francisco", "Chicago"])
        self.assertEqual(rank_locations(mine, "Business Analyst | Remote (US)"), mine)

"""LinkedIn PDF parser tests: a synthetic export, and (if present) the real one."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.config import apply_environment, load_settings  # noqa: E402
from tailor.profile.linkedin import parse_profile  # noqa: E402

SETTINGS = load_settings()
REAL = SETTINGS.data_dir / "profile" / "linkedin" / "linkedin_profile.pdf"

# Same font-size scheme as LinkedIn's export: org 12, title 11.5, everything else 10.5, footer 9.
HTML = """
<html><body style="width:3.2in;font-family:Arial;font-size:10.5pt;margin:0">
<div style="font-size:13pt">Contact</div><div>jane@example.com</div>
<div style="font-size:13pt">Top Skills</div><div>Analytics</div><div>Research</div>
<div style="font-size:26pt">Jane Doe</div>
<div style="font-size:12pt">Product Analyst at Acme Corp</div><div style="font-size:12pt">Chicago, IL</div>
<div style="font-size:16pt">Summary</div><div style="font-size:12pt">I turn data into decisions.</div>
<div style="font-size:16pt">Experience</div>
<div style="font-size:12pt">Acme Corp</div><div style="font-size:11pt">2 years</div>
<div style="font-size:11.5pt">Senior Analyst</div><div>June 2023 - Present (1 year)</div><div>Chicago, IL</div>
<div>-Built a dashboard that cut weekly reporting time by 40% and was adopted by every regional team</div>
<div>-Trained six new hires</div>
<div style="font-size:11.5pt">Analyst</div><div>June 2021 - June 2023 (2 years)</div><div>Chicago, IL</div>
<div>-Cleaned survey data</div>
<div style="font-size:12pt">Globex</div>
<div style="font-size:11.5pt">Intern</div><div>June 2020 - August 2020 (3 months)</div><div>Remote</div>
<div style="font-size:16pt">Education</div>
<div style="font-size:12pt">Example University</div><div>Bachelor of Arts - BA, Economics · (2016 - 2020)</div>
<div style="font-size:9pt">Page 1 of 1</div>
</body></html>
"""


def make_pdf(path: Path) -> None:
    from playwright.sync_api import sync_playwright

    apply_environment(SETTINGS)
    src = path.with_suffix(".html")
    src.write_text(HTML, encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.goto(src.as_uri())
        page.pdf(path=str(path), width="3.2in", height="11in", print_background=True)
        b.close()


class SyntheticExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        pdf = Path(cls.tmp.name) / "profile.pdf"
        make_pdf(pdf)
        cls.prof = parse_profile(pdf)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_header_and_sidebar(self):
        p = self.prof
        self.assertEqual(p.name, "Jane Doe")
        self.assertEqual(p.headline, "Product Analyst at Acme Corp")
        self.assertEqual(p.location, "Chicago, IL")
        self.assertEqual(p.top_skills, ["Analytics", "Research"])
        self.assertIn("jane@example.com", p.contact)

    def test_two_roles_under_one_employer(self):
        rows = [(x.org, x.title, x.start, x.end) for x in self.prof.positions]
        self.assertEqual(rows, [
            ("Acme Corp", "Senior Analyst", "June 2023", "Present"),
            ("Acme Corp", "Analyst", "June 2021", "June 2023"),
            ("Globex", "Intern", "June 2020", "August 2020"),
        ])

    def test_wrapped_bullet_is_one_bullet_and_group_total_is_ignored(self):
        first = self.prof.positions[0]
        self.assertEqual(len(first.bullets), 2)
        self.assertEqual(first.bullets[0],
                         "Built a dashboard that cut weekly reporting time by 40% and was adopted by every regional team")
        self.assertEqual(first.location, "Chicago, IL")

    def test_education_and_footer(self):
        self.assertEqual([e.school for e in self.prof.education], ["Example University"])
        self.assertIn("Economics", self.prof.education[0].detail)
        self.assertFalse(any("Page 1 of 1" in b for p in self.prof.positions for b in p.bullets))


@unittest.skipUnless(REAL.exists(), "real LinkedIn export not present")
class RealExportTests(unittest.TestCase):
    def test_real_profile_structure(self):
        p = parse_profile(REAL)
        self.assertEqual(p.name, "Jordan Rivera")
        self.assertEqual(len(p.positions), 10)
        self.assertEqual([len(x.bullets) for x in p.positions], [3, 0, 0, 3, 3, 3, 2, 4, 1, 3])
        acme = [x for x in p.positions if x.org == "Acme Rental Group"]
        self.assertEqual([x.title for x in acme], ["Senior Sales", "Rental Sales Agent"])
        self.assertEqual([e.school for e in p.education][0], "University of Michigan - Harbor School of Business")
        self.assertEqual(len(p.education), 4)


if __name__ == "__main__":
    unittest.main()

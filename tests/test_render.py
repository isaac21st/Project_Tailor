"""Renderer tests. Run from the project root:  python -m unittest discover -s tests -v

Uses a made-up person, so no personal data lives in the code folder.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfReader  # noqa: E402

from tailor.resume.render import inline, pdf_text, render_resume  # noqa: E402
from tailor.resume.schema import (  # noqa: E402
    ContactItem, Entry, Header, LabeledLine, Link, Resume, Role, Section,
)

LINE_PX = 12.07 * 96 / 72  # the template's fixed line height in CSS pixels

SHORT = "Cut onboarding time by 40% through a redesigned intake form"
LONG = SHORT + " and a new review checklist that helped the whole team hand off work faster and with fewer mistakes"


def make_resume(first_bullet: str = SHORT, extra_bullets: int = 0) -> Resume:
    bullets = [first_bullet, "Ran weekly stakeholder reviews across three teams"] + [
        f"Extra bullet number {i} with a few words" for i in range(extra_bullets)
    ]
    return Resume(
        header=Header(
            name="Jane Doe",
            contact=[ContactItem(text="jane@example.com", url="mailto:jane@example.com"),
                     ContactItem(text="(555) 010-0000")],
        ),
        sections=[
            Section(heading="EDUCATION", entries=[
                Entry(title="Example University", location="Springfield, IL",
                      roles=[Role(title="BA, Economics", dates="May 2020",
                                  bullets=["**Honors:** Dean's list"])]),
            ]),
            Section(heading="EXPERIENCE", entries=[
                Entry(title="ACME CORP", location="Chicago, IL",
                      link=Link(text="CASE STUDY", url="https://example.com/acme"),
                      roles=[Role(title="Analyst", dates="2021 - 2023", bullets=bullets)]),
                Entry(title="GLOBEX", location="Remote",
                      roles=[Role(title="Intern", dates="Summer 2020",
                                  bullets=["Built a reporting dashboard used by 12 managers"])]),
            ]),
            Section(heading="SKILLS", lines=[LabeledLine(label="Tools", text="Excel, SQL, Python")]),
        ],
    )


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, resume: Resume, name: str):
        return render_resume(resume, self.dir / f"{name}.pdf", work_dir=self.dir / name)

    def test_fits_one_page_and_sits_on_line_grid(self):
        r = self.render(make_resume(), "basic")
        self.assertEqual(r.pages, 1)
        self.assertTrue(r.fits)
        self.assertLess(r.line_grid_error, 0.02)

    def test_text_is_extracted_in_reading_order(self):
        r = self.render(make_resume(), "order")
        text = re.sub(r"\s+", " ", pdf_text(r.pdf_path))
        marks = ["Jane Doe", "EDUCATION", "Example University", "Dean", "EXPERIENCE", "ACME CORP",
                 SHORT, "Ran weekly stakeholder reviews", "GLOBEX", "reporting dashboard", "SKILLS", "Excel, SQL, Python"]
        pos = [text.find(m) for m in marks]
        self.assertNotIn(-1, pos, f"missing text: {[m for m, p in zip(marks, pos) if p < 0]}")
        self.assertEqual(pos, sorted(pos), "text is out of reading order")

    def test_no_ligature_glyphs(self):
        text = pdf_text(self.render(make_resume("Certified file and office workflow"), "lig").pdf_path)
        self.assertNotRegex(text, "[ﬀ-ﬆ]")

    def test_longer_wording_only_adds_whole_lines(self):
        a = self.render(make_resume(SHORT), "short")
        b = self.render(make_resume(LONG), "long")
        self.assertLess(a.line_grid_error, 0.02)
        self.assertLess(b.line_grid_error, 0.02)
        added_lines = sum(b.entry_lines) - sum(a.entry_lines)
        self.assertGreaterEqual(added_lines, 1)
        self.assertAlmostEqual(b.content_height_px - a.content_height_px, added_lines * LINE_PX, delta=0.5)

    def test_overflow_is_reported(self):
        r = self.render(make_resume(extra_bullets=60), "overflow")
        self.assertGreater(r.pages, 1)
        self.assertFalse(r.fits)

    def test_font_is_embedded(self):
        r = self.render(make_resume(), "font")
        fonts = PdfReader(str(r.pdf_path)).pages[0]["/Resources"]["/Font"]
        names = [str(f.get_object()["/BaseFont"]) for f in fonts.values()]
        self.assertTrue(names and all("Tinos" in n for n in names), names)


class InlineTests(unittest.TestCase):
    def test_marks_and_escaping(self):
        self.assertEqual(str(inline("**Bold:** a & <b> *it*")), "<strong>Bold:</strong> a &amp; &lt;b&gt; <em>it</em>")


if __name__ == "__main__":
    unittest.main()

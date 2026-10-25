"""The truth check: re-wording passes; adding facts fails."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.resume.schema import ContactItem, Entry, Header, LabeledLine, Link, Resume, Role, Section  # noqa: E402
from tailor.resume.validate import check_tailored, errors  # noqa: E402

BASE = Resume(
    header=Header(name="Jane Doe", contact=[ContactItem(text="jane@example.com", url="mailto:jane@example.com")]),
    sections=[
        Section(heading="EDUCATION", entries=[
            Entry(title="Example University", location="Springfield, IL",
                  roles=[Role(title="MBA", dates="Expected May 2027", bullets=["**Leadership:** Marketing Club"])]),
            Entry(title="Other College", location="Ames, IA", roles=[Role(title="BA Economics", dates="June 2021")]),
        ]),
        Section(heading="PROFESSIONAL EXPERIENCE", entries=[
            Entry(title="ACME CORP", location="Chicago, IL", link=Link(text="CASE STUDY", url="https://x.example/a"),
                  roles=[Role(title="Analyst", dates="2021 - 2023", bullets=[
                      "Cut reporting time by 40% with a new dashboard built in Tableau",
                      "Led a 5-person team across 3 offices"])]),
            Entry(title="GLOBEX", location="Remote", roles=[Role(title="Intern", dates="Summer 2020", bullets=["Built reports"])]),
        ]),
        Section(heading="CONSULTING & LEADERSHIP PROJECT EXPERIENCE", entries=[
            Entry(title="HACKATHON", location="Detroit, MI", roles=[Role(title="Lead", dates="May 2022", bullets=["Won 2nd place"])]),
        ]),
        Section(heading="SKILLS", lines=[LabeledLine(label="Tools", text="Excel, SQL, Tableau (dashboards)")]),
    ],
)


def variant(edit) -> Resume:
    r = copy.deepcopy(BASE)
    edit(r)
    return r


def bullets(r, entry_idx=0, sec=1):
    return r.sections[sec].entries[entry_idx].roles[0].bullets


class ValidateTests(unittest.TestCase):
    def test_identical_resume_passes(self):
        self.assertEqual(check_tailored(BASE, copy.deepcopy(BASE)), [])

    def test_rewording_and_reordering_pass(self):
        def edit(r):
            b = bullets(r)
            b[0], b[1] = ("Directed a 5-person team spanning 3 offices", "Reduced reporting time by 40% through a Tableau dashboard")
            r.sections[3].lines[0].text = "SQL, Excel, Tableau (dashboards)"
        self.assertEqual(errors(check_tailored(BASE, variant(edit))), [])

    def test_invented_number_fails(self):
        def edit(r): bullets(r)[0] = "Cut reporting time by 60% with a new dashboard built in Tableau"
        self.assertTrue(any("'60%'" in str(i) for i in errors(check_tailored(BASE, variant(edit)))))

    def test_new_tool_or_org_fails_unless_allowed(self):
        def edit(r): bullets(r)[1] = "Led a 5-person team across 3 offices using Copilot"
        self.assertTrue(any("'Copilot'" in str(i) for i in errors(check_tailored(BASE, variant(edit)))))
        self.assertEqual(errors(check_tailored(BASE, variant(edit), allowed_terms=("Copilot",))), [])

    def test_sentence_initial_verbs_are_not_mistaken_for_new_terms(self):
        def edit(r): bullets(r)[1] = "Directed a 5-person team across 3 offices"
        self.assertEqual(errors(check_tailored(BASE, variant(edit))), [])

    def test_education_must_be_identical_but_order_may_change(self):
        def swap(r): r.sections[0].entries.reverse()
        self.assertEqual(errors(check_tailored(BASE, variant(swap))), [])
        def change(r): r.sections[0].entries[0].roles[0].title = "MBA (Honors)"
        self.assertTrue(any(i.where == "education" for i in errors(check_tailored(BASE, variant(change)))))

    def test_dates_titles_and_links_are_locked(self):
        def edit(r): r.sections[1].entries[0].roles[0].dates = "2020 - 2023"
        self.assertTrue(errors(check_tailored(BASE, variant(edit))))
        def edit2(r): r.sections[1].entries[0].roles[0].title = "Senior Analyst"
        self.assertTrue(errors(check_tailored(BASE, variant(edit2))))
        def edit3(r): r.sections[1].entries[0].link = None
        self.assertTrue(errors(check_tailored(BASE, variant(edit3))))

    def test_new_entry_fails_and_dropping_a_professional_entry_fails(self):
        def add(r): r.sections[2].entries.append(Entry(title="NEW GIG", roles=[Role(title="Lead", dates="2024")]))
        self.assertTrue(any("does not exist" in str(i) for i in errors(check_tailored(BASE, variant(add)))))
        def drop_pro(r): r.sections[1].entries.pop()
        self.assertTrue(errors(check_tailored(BASE, variant(drop_pro))))
        def drop_project(r): r.sections[2].entries.pop()
        issues = check_tailored(BASE, variant(drop_project))
        self.assertEqual(errors(issues), [])  # allowed, but reported
        self.assertTrue(any(i.level == "warn" for i in issues))

    def test_new_skill_fails(self):
        def edit(r): r.sections[3].lines[0].text = "Excel, SQL, Tableau (dashboards), Power BI"
        self.assertTrue(any("Power BI" in str(i) for i in errors(check_tailored(BASE, variant(edit)))))

    def test_header_is_locked(self):
        def edit(r): r.header.name = "Jane Q. Doe"
        self.assertTrue(any(i.where == "header" for i in errors(check_tailored(BASE, variant(edit)))))


if __name__ == "__main__":
    unittest.main()

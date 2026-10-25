"""Job-description tests. Run from the project root:  python -m unittest discover -s tests -v

Two kinds: synthetic cases for the cleaner (no private files needed), and a check of the five
sample postings against hand-written gold answers (skipped if the sample folder is missing).
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.config import load_settings  # noqa: E402
from tailor.jobs import (  # noqa: E402
    clean_job_text, extract_facts, load_job_text, missing_keywords, posting_open,
)

JOBS_DIR = load_settings().data_dir / "jobs"


def clean(raw: str):
    return clean_job_text(raw)


class CleanerTests(unittest.TestCase):
    def test_ligatures_are_normalized(self):
        self.assertIn("financial modeling", clean("Responsibilities\n- Performing ﬁnancial modeling\n").full_text())

    def test_icon_font_characters_are_removed(self):
        text = clean("Required:\n- Minimum of 3 years of\n experience in consulting\n").full_text()
        self.assertNotRegex(text, "[-]")
        self.assertIn("Minimum of 3 years of experience in consulting", text)

    def test_nav_bar_glued_to_words_is_removed(self):
        raw = "Required:\nMinimum of 3 years of experience in engagement management and business\n" \
              "Careersdevelopment                 Job Search      Saved Jobs      Access Application\n"
        self.assertIn("and business development", clean(raw).full_text())

    def test_page_break_does_not_split_a_sentence(self):
        raw = "As PwC is an equal opportunity employer, we consider all applicants without regard to race;\n\n" \
              "sex (including pregnancy); age; disability.\n"
        self.assertIn("race; sex (including pregnancy)", clean(raw).full_text())

    def test_url_wrapped_at_a_hyphen_is_rejoined(self):
        raw = "Details at https://pwc.to/H-1B-Lottery-\nPolicy. Thank you.\n"
        self.assertIn("https://pwc.to/H-1B-Lottery-Policy.", clean(raw).full_text())

    def test_widget_run_inside_a_sentence_is_removed(self):
        raw = "You will guide clients through the\nRecommended\nFind\nrelevant\n(0)\n\ncomplexities of mergers.\n"
        self.assertIn("through the complexities of mergers.", clean(raw).full_text())

    def test_required_and_preferred_sections_are_tagged(self):
        raw = "Here’s What You Need:\nA Bachelor’s degree\n\nBonus Points If You Have:\nAn MBA\n"
        job = clean(raw)
        self.assertEqual(job.of_kind("required"), ["A Bachelor’s degree"])
        self.assertEqual(job.of_kind("preferred"), ["An MBA"])

    def test_similar_jobs_tail_is_cut(self):
        job = clean("Required:\n- 3 years of experience\nSimilar jobs\nSalesforce Developer\n")
        self.assertNotIn("Salesforce", job.full_text())


class FactTests(unittest.TestCase):
    def facts(self, raw: str):
        return extract_facts(clean(raw))

    def test_years_floor_is_read_with_its_section(self):
        f = self.facts("Required:\n6+ years of lean product management\nPreferred:\n2 years of SQL\n")
        self.assertEqual([(y.years, y.kind) for y in f.min_years], [(6, "required"), (2, "preferred")])

    def test_travel_takes_the_top_of_a_range(self):
        self.assertEqual(self.facts("Ability to travel 0-20%, on average").travel_max_pct, 20)

    def test_sponsorship_stances(self):
        self.assertEqual(self.facts("Limited immigration sponsorship may be available.").sponsorship, "limited")
        self.assertEqual(
            self.facts("We do not intend to hire candidates who will need sponsorship through the lottery.").sponsorship,
            "restricted")
        self.assertEqual(self.facts("Great benefits.").sponsorship, "not_stated")

    def test_mba_is_required_only_in_required_section(self):
        self.assertTrue(self.facts("What You Must Have:\nCurrently pursuing an MBA\n").mba_required)
        self.assertFalse(self.facts("Preferred:\nMBA or related advanced degree\n").mba_required)

    def test_deadline_and_posting_open(self):
        f = self.facts("We anticipate this job posting will be posted until 07/26/2026.")
        self.assertEqual(f.deadline, date(2026, 7, 26))
        self.assertFalse(posting_open(f, today=date(2026, 9, 19)))
        self.assertIsNone(posting_open(self.facts("No deadline here."), today=date(2026, 9, 19)))


class KeywordCheckTests(unittest.TestCase):
    def test_only_verbatim_phrases_pass(self):
        text = "We use JIRA and financial-crime tools – plus risk & control frameworks."
        self.assertEqual(missing_keywords(["jira", "Financial‐Crime", "Tableau"], text), ["Tableau"])


@unittest.skipUnless((JOBS_DIR / "gold").is_dir(), "sample postings not present")
class SamplePostingTests(unittest.TestCase):
    def cases(self):
        for g in sorted((JOBS_DIR / "gold").glob("*.json")):
            pdf = JOBS_DIR / "samples" / f"{g.stem}.pdf"
            if pdf.exists():
                yield g.stem, json.loads(g.read_text(encoding="utf-8")), clean_job_text(load_job_text(pdf))

    def test_facts_match_gold(self):
        n = 0
        for name, gold, job in self.cases():
            n += 1
            f = extract_facts(job)
            got = {**f.model_dump(mode="json"), "min_years": [[y.years, y.kind] for y in f.min_years]}
            for key, want in gold["facts"].items():
                self.assertEqual(got[key], want, f"{name}: {key}")
        self.assertGreater(n, 0)

    def test_gold_keywords_are_verbatim_in_the_posting(self):
        for name, gold, job in self.cases():
            self.assertEqual(missing_keywords(gold["analysis"]["keywords"], job.body_text()), [], name)

    def test_no_layout_junk_survives(self):
        for name, _, job in self.cases():
            text = job.body_text()
            for junk in ("Job Search", "Saved Jobs", "Chat with our", "Recruiting Assistant", "Access Application"):
                self.assertNotIn(junk, text, f"{name}: {junk}")
            self.assertNotRegex(text, "[-ﬀ-ﬆ]", name)

    def test_company_comes_from_the_text_not_the_file_name(self):
        misnamed = [(n, g, j) for n, g, j in self.cases() if "__file_named_" in n]
        self.assertTrue(misnamed)
        for name, gold, job in misnamed:
            self.assertIn(gold["analysis"]["company"].casefold(), job.full_text().casefold(), name)
            self.assertIn(gold["facts"]["req_id"], job.full_text(), name)


if __name__ == "__main__":
    unittest.main()


class DeadlineTests(unittest.TestCase):
    def deadline(self, text):
        from tailor.jobs import clean_job_text, extract_facts
        return extract_facts(clean_job_text("Associate Product Manager\n\n" + text)).deadline

    def test_common_phrasings(self):
        from datetime import date
        cases = {
            # Google Careers, APM 2027 (real wording)
            "You must complete your application *before* end of day on October 6, 2026 to be considered.": date(2026, 10, 6),
            "Please apply by October 03, 2026.": date(2026, 10, 3),
            "Applications are due by Nov. 15, 2026.": date(2026, 11, 15),
            "Application deadline: 12/01/2026": date(2026, 12, 1),
            "Applications close on September 30th, 2026.": date(2026, 9, 30),
            "Deadline to apply is 2026-10-20.": date(2026, 10, 20),
            "We are accepting applications until 5 January 2027.": date(2027, 1, 5),
            "This role will be posted until 10/31/2026": date(2026, 10, 31),
            "Recruiting for this role ends on October 12, 2026.": date(2026, 10, 12),
        }
        for text, want in cases.items():
            self.assertEqual(self.deadline(text), want, text)

    def test_other_dates_are_not_deadlines(self):
        for text in ("Start date: June 1, 2027.", "Posted on September 22, 2026.",
                     "Cohort starts between April and August 2027.", "Founded in 2015, we serve 3 million users."):
            self.assertIsNone(self.deadline(text), text)


class YearsWordingTests(unittest.TestCase):
    def test_years_written_as_words_or_with_brackets(self):
        from tailor.jobs import clean_job_text, extract_facts
        years = lambda t: [y.years for y in extract_facts(clean_job_text("Role\n\n" + t)).min_years]  # noqa: E731
        # Stripe GTM Strategy & Operations Analyst (2026-09-29): "plus seven (7) years of related experience"
        self.assertEqual(years("Minimum qualifications:\nBachelor's degree plus seven (7) years of related experience."), [7])
        self.assertEqual(years("Requirements:\nFive years of product management experience."), [5])
        self.assertEqual(years("Requirements:\n3+ years of SQL."), [3])


class FormOnPostingPageTests(unittest.TestCase):
    """Greenhouse shows the posting and then the application form on one page (Brattle, 2026-10-07): the form's
    "Preferred First Name" box made everything after it "preferred qualifications", the self-ID survey became a skill."""

    PAGE = ("Associate, MBA - Economics/Finance\nNew York\nApply\n\n"
            "Associates work as part of a team on challenging projects. They apply finance and economic concepts, "
            "build financial models in Excel and run statistical analysis for clients in litigation and regulation. "
            "Associates frame research approaches and manage projects while building individual expertise. " * 3 +
            "\n\nApply for this job\n\n* indicates a required field\n\nFirst Name*\n\nLast Name*\n\nPreferred First Name\n\n"
            "Email*\n\nResume/CV*\n\nVoluntary Self-Identification\n\nFor government reporting purposes, we ask candidates "
            "to respond to the below self-identification survey.\n\nSubmit application\n")

    def test_the_form_is_not_part_of_the_posting(self):
        from tailor.jobs import clean_job_text
        from tailor.resume.keywords import extract_keywords

        job = clean_job_text(self.PAGE)
        self.assertEqual([b.kind for b in job.blocks if b.kind not in ("other", "boilerplate")], [])
        self.assertNotIn("survey", job.body_text().lower())
        names = [k.name for k in extract_keywords(job, "Job Application for Associate, MBA - Economics/Finance at The Brattle Group").items]
        self.assertIn("Financial modeling", names)
        self.assertNotIn("Survey design", names)
        self.assertIn("Associate", names)  # the title, without "Job Application for"

    def test_preferred_name_is_not_a_heading_but_preferred_qualifications_is(self):
        from tailor.jobs.text import _heading_kind

        self.assertIsNone(_heading_kind("Preferred First Name"))
        self.assertIsNone(_heading_kind("Preferred pronouns"))
        self.assertEqual(_heading_kind("Preferred qualifications"), "preferred")
        self.assertEqual(_heading_kind("Bonus points if you have"), "preferred")

"""Hard tailoring: a re-worded bullet replaces the old one only if it says the same thing in the posting's words.

The refused cases below are real qwen3:8b outputs from the Google APM posting (2026-09-27).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.jobs import clean_job_text  # noqa: E402
from tailor.resume.rewrite import check_bullet, qualifications, rewrite_resume  # noqa: E402
from tailor.resume.schema import Entry, Header, Resume, Role, Section  # noqa: E402

B1 = ("Led the data analytics workstream on a 5-person CAMPUS Tech Consulting team, reframing a data-cleanup request "
      "into a product-strategy engagement by analyzing 1,240 member records across 18 program years")
B2 = ("Independently built the HTML Member Explorer, a browser-based decision-support tool with 9 interactive views "
      "(regions, programs, sectors, contact coverage) the Center can re-run on updated data")
B3 = ("Delivered an evidence-based recommendation on venue strategy, surfacing Denver alongside Boston and "
      "reframing planning from historical assumption to ongoing, data-informed decisions")


class CheckBulletTests(unittest.TestCase):
    def test_real_fabrications_are_refused(self):
        # a different bullet's facts, written as a new claim
        self.assertIsNotNone(check_bullet(B1, "Led entrepreneurial efforts as Analytics Lead at OPERATIONS CENTER FOR "
                                              "GLOBAL OPERATIONS, building cross-functional relationships with stakeholders "
                                              "to drive product strategy initiatives."))
        # presentations the resume never mentions, replacing the tool the bullet was about
        self.assertIsNotNone(check_bullet(B2, "Delivered technical presentations to stakeholders, showcasing findings from "
                                              "data analysis and providing evidence-based recommendations to inform "
                                              "venue strategy decisions."))
        # bullet 1's facts moved into bullet 3
        self.assertIsNotNone(check_bullet(B3, "Applied product development methodologies to reframe a data-cleanup request "
                                              "into a product-strategy engagement, analyzing 1,240 member records across "
                                              "18 program years."))

    def test_a_kind_of_work_the_bullet_does_not_show_is_refused(self):
        self.assertIn("product", check_bullet("Built a dashboard for 3 clinics to track weekly intake and staffing gaps",
                                              "Built a dashboard for 3 clinics to track weekly intake and staffing gaps, "
                                              "driving product development"))

    def test_research_words_are_not_technical_work(self):
        from tailor.resume.rewrite import _concepts
        eng = _concepts()["engineering"]
        self.assertIsNone(eng.search("coding findings from 80 studies into a standardized dataset"))
        self.assertIsNone(eng.search("synthesized findings into personas and information architecture"))
        self.assertIsNotNone(eng.search("built an HTML scheduling tool"))
        self.assertIsNotNone(eng.search("coding in Python for the data pipeline"))

    def test_ungrammatical_padding_that_claims_skills_is_refused(self):
        old = ("Prototype was well received by university faculty and classmates, who called out specific gaps "
               "(missing feedback options) validated in testing")
        self.assertIsNotNone(check_bullet(old, "Utilized technical skills in data analysis and design by prototype was well "
                                               "received by university faculty and classmates, who called out "
                                               "specific gaps (missing feedback options) validated in testing."))

    def test_ai_is_fair_only_when_the_bullet_names_an_ai_tool(self):
        old = "Independently built an HTML scheduling tool with Claude Code that turned shift requests into rosters"
        self.assertIsNone(check_bullet(old, "Independently built an AI-assisted HTML scheduling tool with Claude Code that "
                                            "turned shift requests into rosters"))
        self.assertIsNotNone(check_bullet("Independently built an HTML scheduling tool that turned shift requests into rosters",
                                         "Independently built an AI-assisted HTML scheduling tool that turned room "
                                         "requirements into layouts"))

    def test_a_faithful_rewording_in_the_postings_language_is_kept(self):
        new = ("Led the data analytics workstream on a 5-person CAMPUS Tech Consulting team, driving product development by "
               "reframing a data-cleanup request into a product-strategy engagement analyzing 1,240 member records across "
               "18 program years")
        self.assertIsNone(check_bullet(B1, new))

    def test_each_rule(self):
        old = "Built a dashboard with **Tableau** for 3 clinics"
        self.assertIn("adds the number", check_bullet(old, "Built a dashboard with **Tableau** for 4 clinics"))
        self.assertIn("drops the number", check_bullet(old, "Built a dashboard with **Tableau** for clinics"))
        self.assertIn("drops", check_bullet(old, "Built a dashboard for 3 clinics"))
        self.assertIn("name or acronym", check_bullet(old, "Built a dashboard with **Tableau** and SQL for 3 clinics"))
        self.assertIn("claims", check_bullet(old, "Built a machine learning dashboard with **Tableau** for 3 clinics"))
        self.assertIn("claims presentations", check_bullet(old, "Built and presented a dashboard with **Tableau** for 3 clinics"))
        self.assertIn("bigger", check_bullet(old, "Led building a dashboard with **Tableau** for 3 clinics"))
        self.assertIn("different", check_bullet("Interviewed parents about school routines and pickup timing",
                                                "Drove product development to improve outcomes for families"))
        self.assertIn("cross-functional", check_bullet("Built a dashboard for 3 clinics",
                                                       "Built a cross-functional dashboard for 3 clinics"))
        self.assertIsNone(check_bullet("Built a dashboard with 3 clinicians",
                                       "Built a dashboard in cross-functional work with 3 clinicians"))
        self.assertIn("Dr. Example", check_bullet("Advised Dr. Example Person on 3 clinics", "Advised on 3 clinics",
                                               protect=("Dr. Example Person",)))
        self.assertIn("too long", check_bullet("Built a dashboard for 3 clinics", "Built a dashboard for 3 clinics " + "x " * 40))


POSTING = """Associate Product Manager

Minimum qualifications:
Experience leading entrepreneurial efforts or outreach within organizations while building cross-functional relationships.
2 out of 12 keywords matched, add more to make you stand out.

Preferred qualifications:
Experience with methodologies aimed to drive product development and delivery.
""" + "Work with engineers and designers to ship products that affect millions of people every day.\n" * 20


class FakeModel:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def one_entry_resume(bullets):
    return Resume(header=Header(name="Jane Doe", contact=[]), sections=[
        Section(heading="PROFESSIONAL EXPERIENCE", entries=[Entry(title="ACME", roles=[Role(title="Analyst", dates="2025",
                                                                                          bullets=bullets)])])])


class RewriteResumeTests(unittest.TestCase):
    def test_keeps_good_rewordings_and_refuses_bad_ones_with_reasons(self):
        good = "Built dashboard features for 3 clinics to track weekly intake and staffing gaps, driving product development"
        model = FakeModel([json.dumps({"bullets": [{"q": "Q2", "text": good},
                                                   {"q": "Q1", "text": "Presented to 40 executives"}]})])
        res = rewrite_resume(one_entry_resume(["Built dashboard features for 3 clinics to track weekly intake and staffing gaps",
                                               "Cleaned survey data from 2 schools"]),
                             clean_job_text(POSTING), model, instruction="lean on product development")
        bullets = res.resume.sections[0].entries[0].roles[0].bullets
        self.assertEqual(bullets, [good, "Cleaned survey data from 2 schools"])
        self.assertEqual(len(res.changed), 1)
        self.assertEqual(len(res.kept), 1)
        self.assertIn("lean on product development", model.prompts[0])
        self.assertNotIn("keywords matched", model.prompts[0])  # another extension's text is not a qualification

    def test_a_model_failure_changes_nothing(self):
        r = one_entry_resume(["Built a dashboard for 3 clinics"])
        res = rewrite_resume(r, clean_job_text(POSTING), FakeModel(["not json"]))
        self.assertEqual(res.resume, r)
        self.assertTrue(res.error)

    def test_wrong_number_of_bullets_changes_nothing(self):
        r = one_entry_resume(["Built a dashboard for 3 clinics", "Cleaned survey data from 2 schools"])
        res = rewrite_resume(r, clean_job_text(POSTING), FakeModel([json.dumps({"bullets": [{"text": "x"}]})]))
        self.assertEqual(res.resume, r)

    def test_qualifications_skip_injected_text(self):
        qs = qualifications(clean_job_text(POSTING))
        self.assertTrue(any("cross-functional" in q for q in qs))
        self.assertFalse(any("keywords matched" in q for q in qs))


if __name__ == "__main__":
    unittest.main()


class PhraseBankTests(unittest.TestCase):
    ORIGINAL = "Worked with 3 branch teams on scheduling data to fix operational bottlenecks, lifting satisfaction to 85"
    BANK = {ORIGINAL: [
        ("cross-functional", "Coordinated cross-functional fixes with 3 branch teams for operational bottlenecks found in "
                             "scheduling data, lifting satisfaction to 85"),
        ("data", "Analyzed scheduling data to pinpoint operational bottlenecks and fixed them with 3 branch teams, lifting "
                 "satisfaction to 85"),
    ]}

    def prepared(self, posting, instruction=""):
        from tailor.jobs import clean_job_text as cj
        from tailor.resume.auto import job_concepts, prepare
        vec, _ = job_concepts(cj(posting))
        r, notes = prepare(one_entry_resume([self.ORIGINAL]), posting, bank=self.BANK, vec=vec, instruction=instruction)
        return r.sections[0].entries[0].roles[0].bullets[0], notes

    def test_the_posting_picks_the_wording(self):
        data_job = "Minimum qualifications:\nExperience with data analysis, SQL and dashboards to analyze metrics.\n"
        people_job = "Minimum qualifications:\nBuilding cross-functional relationships with teams and stakeholders.\n"
        self.assertTrue(self.prepared(data_job)[0].startswith("Analyzed scheduling data"))
        for _, text in self.BANK[self.ORIGINAL]:  # the fixture itself must be honest
            self.assertIsNone(check_bullet(self.ORIGINAL, text), text)
        self.assertTrue(self.prepared(people_job)[0].startswith("Coordinated cross-functional"))

    def test_the_original_wins_when_nothing_fits_better(self):
        bullet, notes = self.prepared("Minimum qualifications:\nFluent Spanish and a valid driver's license for field visits.\n")
        self.assertEqual(bullet, self.ORIGINAL)
        self.assertEqual(notes, [])

    def test_the_chat_instruction_steers_the_choice(self):
        people_job = "Minimum qualifications:\nBuilding cross-functional relationships with teams and stakeholders.\n"
        bullet, notes = self.prepared(people_job, instruction="lean on my data analysis")
        self.assertTrue(bullet.startswith("Analyzed scheduling data"))
        self.assertIn("'data' wording", notes[0])

    def test_only_approved_variants_that_pass_the_check_are_loaded(self):
        import tempfile
        from tailor.config import Settings
        from tailor.resume.auto import load_bank
        with tempfile.TemporaryDirectory() as d:
            st = Settings(data_dir=Path(d))
            st.ensure_dirs()
            (st.profile_dir / "phrase_bank.json").write_text(json.dumps({"bullets": [{"original": self.ORIGINAL, "variants": [
                {"angle": "ok", "text": self.BANK[self.ORIGINAL][1][1], "status": "approved"},
                {"angle": "pending", "text": self.BANK[self.ORIGINAL][0][1], "status": "pending"},
                {"angle": "edited badly", "text": "Built AI models in 3 branches, lifting satisfaction to 95", "status": "approved"},
            ]}]}), encoding="utf-8")
            self.assertEqual([a for a, _ in load_bank(st)[self.ORIGINAL]], ["ok"])

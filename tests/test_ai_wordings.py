"""AI keyword wordings: the strict check, which pairs get one, and how tailoring uses them (no model needed)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.config import Settings  # noqa: E402
from tailor.llm.clients import StubClient  # noqa: E402
from tailor.resume import wordings  # noqa: E402
from tailor.resume.auto import prepare  # noqa: E402
from tailor.resume.keywords import extract_keywords  # noqa: E402
from tailor.resume.schema import Entry, Header, Resume, Role, Section  # noqa: E402
from tailor.resume.wordings import _term, check_weave  # noqa: E402
from tailor.jobs import clean_job_text  # noqa: E402

TEAM = ("Led the data analytics workstream on a 5-person consulting team, reframing a data-cleanup request into a "
        "product-strategy engagement by analyzing 1,240 member records across 18 program years")
REVIEW = ("Reviewed and analyzed 500 research articles, coding findings from 80 studies into a standardized dataset that "
          "surfaced methodological and reporting patterns")
FOUND = ("Surfaced the key finding that housing rent growth outpaced transit spending growth by 88.4% over the "
         "decade; visualizations anchored the team's final recommendation")


def _resume() -> Resume:
    return Resume(header=Header(name="A Person", contact=[]), sections=[
        Section(heading="EDUCATION", entries=[Entry(title="A School", roles=[Role(title="MS", dates="2027")])]),
        Section(heading="PROJECTS", entries=[
            Entry(title="Alumni study", roles=[Role(title="Analyst", dates="2025", bullets=[TEAM, REVIEW])]),
            Entry(title="Datathon", roles=[Role(title="Data Scientist", dates="2025", bullets=[FOUND])]),
        ]),
    ])


class CheckWeaveTests(unittest.TestCase):
    def ok(self, old, new, term):
        text, why = check_weave(old, new, _term(term))
        self.assertIsNone(why, f"{why}: {text}")
        return text

    def refused(self, old, new, term, because):
        text, why = check_weave(old, new, _term(term))
        self.assertIsNotNone(why, text)
        self.assertIn(because, why)

    def test_a_faithful_wording_passes_and_is_cleaned_up(self):
        text = self.ok(TEAM, TEAM.replace("data-cleanup", "**Data Cleaning**") + ".", "Data cleaning")
        self.assertIn("data cleaning request", text)  # the model's bold dropped, mid-sentence lower case, no period
        self.assertFalse(text.endswith("."))
        self.ok(REVIEW, REVIEW.replace("Reviewed and analyzed", "Applied quantitative analysis to"), "Quantitative analysis")

    def test_a_new_word_or_audience_is_refused(self):
        self.refused(FOUND, FOUND.replace("Surfaced the key finding", "Conducted quantitative analysis to reveal"),
                     "Quantitative analysis", "adds the word 'reveal'")
        self.refused(TEAM, "Partnered with senior product managers on " + TEAM[0].lower() + TEAM[1:].replace("data-cleanup", "data cleaning"),
                     "Data cleaning", "adds the word")

    def test_a_keyword_the_bullet_had_must_stay(self):
        self.refused(FOUND, FOUND.replace("Surfaced", "Surfaced through quantitative analysis").replace("visualizations", "charts"),
                     "Quantitative analysis", "drops the keyword 'data visualization'")

    def test_the_persons_own_name_for_a_skill_is_never_renamed(self):
        self.refused(TEAM, TEAM.replace("data analytics workstream", "quantitative analysis workstream"),
                     "Quantitative analysis", "renames 'data analytics'")

    def test_numbers_role_and_length_are_still_checked(self):
        self.refused(REVIEW, REVIEW.replace("Reviewed and analyzed 500", "Applied quantitative analysis to 600"),
                     "Quantitative analysis", "number")
        self.refused(REVIEW, REVIEW.replace("Reviewed", "Led quantitative analysis and reviewed"), "Quantitative analysis",
                     "bigger")
        self.refused(REVIEW, REVIEW + " through quantitative analysis, using quantitative analysis across the studies and articles "
                     "with the dataset", "Quantitative analysis", "too long")

    def test_it_must_actually_say_the_keyword(self):
        self.refused(REVIEW, REVIEW, "Quantitative analysis", "does not say")

    def test_same_meaning_verbs_are_allowed(self):
        self.ok(FOUND, FOUND.replace("Surfaced the key finding", "Identified through quantitative analysis the key finding"),
                "Quantitative analysis")


class TargetTests(unittest.TestCase):
    def test_pairs_need_evidence_and_skip_overstated_keywords(self):
        pairs = {(t.original[:20], t.term.name) for t in wordings.targets(_resume(), {})}
        self.assertIn((TEAM[:20], "Data cleaning"), pairs)  # "data-cleanup request"
        self.assertIn((REVIEW[:20], "Quantitative analysis"), pairs)  # "analyzed 500 ..."
        self.assertIn((REVIEW[:20], "Data cleaning"), pairs)  # "a standardized dataset"
        self.assertNotIn((FOUND[:20], "Data visualization"), pairs)  # it already says visualizations
        self.assertFalse(any(name in wordings.NOT_WOVEN for _, name in pairs))
        self.assertFalse(any(t.original.startswith("A School") for t in wordings.targets(_resume(), {})))


class StoreAndPrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.settings.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, items):
        wordings.save_store(self.settings, items)

    def test_fill_saves_passing_wordings_and_remembers_failures(self):
        good = TEAM.replace("data-cleanup", "data cleaning")
        client = StubClient({"Keyword: data cleaning": json.dumps({"options": ["Partnered with executives on it", good]}),
                             "Keyword:": json.dumps({"options": ["nothing useful"]})})
        n = wordings.fill(self.settings, _resume(), client, {}, terms={"Data cleaning", "Quantitative analysis"}, log=lambda m: None)
        self.assertGreaterEqual(n, 1)
        items = wordings.load_store(self.settings)
        made = [it for it in items if it["term"] == "Data cleaning"]
        self.assertEqual(made[0]["text"], good)
        self.assertTrue(any(it["status"] == "failed" for it in items))  # remembered: not asked again
        self.assertEqual(wordings.missing(self.settings, _resume(), {}, {"Data cleaning", "Quantitative analysis"}), [])
        self.assertIn(TEAM, wordings.usable(self.settings))

    def test_prepare_uses_a_wording_only_when_the_posting_asks_for_its_keyword(self):
        woven_text = TEAM.replace("data-cleanup", "data cleaning")
        self._store([{"original": TEAM, "term": "Data cleaning", "source": TEAM, "text": woven_text, "status": "auto"}])
        ai = wordings.usable(self.settings)
        asks = "Analyst\n\nRequirements:\nExperience with data cleaning and data preparation in Excel.\nStrong analytical skills."
        other = "Analyst\n\nRequirements:\nExperience with user research and usability testing.\nStrong analytical skills."
        for text, expect in ((asks, True), (other, False)):
            kw = extract_keywords(clean_job_text(text), "Analyst")
            woven: list[str] = []
            r, _ = prepare(_resume(), text, kw=kw, ai=ai, woven=woven)
            bullets = r.sections[1].entries[0].roles[0].bullets
            self.assertEqual(any("data cleaning request" in b for b in bullets), expect)
            self.assertEqual(woven == ["Data cleaning"], expect)

    def test_a_rejected_wording_or_switching_off_stops_its_use(self):
        woven_text = TEAM.replace("data-cleanup", "data cleaning")
        self._store([{"original": TEAM, "term": "Data cleaning", "source": TEAM, "text": woven_text, "status": "auto"}])
        self.assertTrue(wordings.set_status(self.settings, TEAM, "Data cleaning", "rejected"))
        self.assertEqual(wordings.usable(self.settings), {})
        wordings.set_status(self.settings, TEAM, "Data cleaning", "auto")
        (self.settings.profile_dir / "profile_overrides.json").write_text('{"ai_wordings": false}')
        self.assertEqual(wordings.usable(self.settings), {})

    def test_a_stored_wording_that_no_longer_passes_is_not_used(self):
        self._store([{"original": TEAM, "term": "Data cleaning", "source": TEAM,
                      "text": TEAM.replace("1,240", "2,000").replace("data-cleanup", "data cleaning"), "status": "auto"}])
        self.assertEqual(wordings.usable(self.settings), {})


if __name__ == "__main__":
    unittest.main()

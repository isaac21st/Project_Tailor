"""Answer drafting: the guard matters more than the prose, so that is what these tests pin down."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.fill.answers import SensitiveQuestion, Draft, draft_answer, unsupported_numbers  # noqa: E402
from tailor.fill.profile import Experience, Profile, YearMonth  # noqa: E402
from tailor.llm import StubClient  # noqa: E402

PROFILE = Profile(
    first_name="Jane", last_name="Doe", email="j@example.com", phone="555",
    experience=[Experience(employer="Acme Corp", title="Analyst", start=YearMonth(year=2024, month=1), current=True,
                           bullets=["Cut reporting time by 40% with a new dashboard",
                                    "Led a 5-person team across 3 offices"])],
)


class Scripted:
    """A model that returns a fixed sequence of replies (a stand-in for Qwen)."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class NumberGuardTests(unittest.TestCase):
    def test_supported_numbers_pass_and_invented_ones_are_caught(self):
        facts = PROFILE.facts_text()
        self.assertEqual(unsupported_numbers("I cut reporting time by 40% and led a 5-person team.", facts), [])
        self.assertEqual(unsupported_numbers("I grew revenue 300% over 7 years.", facts), ["300%", "7"])

    def test_commas_dollar_signs_and_percent_signs_are_normalized(self):
        self.assertEqual(unsupported_numbers("Saved $1,200.", "We saved 1200 in fees"), [])

    def test_bare_small_numbers_are_allowed_but_years_are_not(self):
        self.assertEqual(unsupported_numbers("I own 2 side projects.", "nothing"), [])
        self.assertEqual(unsupported_numbers("I have 8 years of experience.", "nothing"), ["8"])


class DraftTests(unittest.TestCase):
    def test_clean_answer_is_returned_untouched(self):
        client = Scripted("I cut reporting time by 40% at Acme Corp, and I want to bring that rigor here.")
        d = draft_answer(client, PROFILE, "Why do you want to work here?")
        self.assertEqual(d.warnings, [])
        self.assertIn("40%", d.text)
        self.assertIn("APPLICANT FACTS", client.prompts[0])
        self.assertIn("Cut reporting time by 40%", client.prompts[0])

    def test_the_chat_instruction_and_current_text_reach_the_prompt(self):
        client = Scripted("I cut reporting time by 40% at Acme Corp.")
        draft_answer(client, PROFILE, "Why do you want to work here?", instruction="make it shorter, lead with my dashboard work",
                     current="A long first draft about many things.")
        prompt = client.prompts[0]
        self.assertIn("- make it shorter\n- lead with my dashboard work", prompt)
        self.assertIn("CURRENT DRAFT", prompt)
        self.assertIn("A long first draft about many things.", prompt)
        self.assertIn("Rewrite the answer now", prompt)
        plain = Scripted("Fine.")
        draft_answer(plain, PROFILE, "Why do you want to work here?")
        self.assertIn("Write the answer now", plain.prompts[0])
        self.assertNotIn("THE APPLICANT", plain.prompts[0])

    def test_a_number_the_applicant_typed_is_theirs_but_one_only_in_the_old_draft_is_not(self):
        typed = Scripted("I led a team of 12 people at Acme Corp.")
        d = draft_answer(typed, PROFILE, "Why do you want to work here?", instruction="mention that I led 12 people")
        self.assertIn("12", d.text)
        self.assertEqual(d.warnings, [])
        stale = Scripted("I boosted revenue by 300%.")  # the model just repeats a slip from the previous draft
        d2 = draft_answer(stale, PROFILE, "Why do you want to work here?", current="I boosted revenue by 300%.")
        self.assertNotIn("300%", d2.text)
        self.assertTrue(d2.warnings)

    def test_invented_number_triggers_a_rewrite(self):
        client = Scripted("I boosted revenue by 300%.", "I cut reporting time by 40% at Acme Corp.")
        d = draft_answer(client, PROFILE, "Describe an accomplishment.")
        self.assertEqual(d.text, "I cut reporting time by 40% at Acme Corp.")
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("300%", client.prompts[1])

    def test_a_model_that_keeps_inventing_loses_the_bad_sentence(self):
        client = Scripted("I boosted revenue by 300%. I enjoy analytical work.")
        d = draft_answer(client, PROFILE, "Tell us about yourself.")
        self.assertEqual(d.text, "I enjoy analytical work.")
        self.assertTrue(any("300%" in w for w in d.warnings))

    def test_length_limit_is_enforced(self):
        long = "I like analysis. " * 30
        d = draft_answer(Scripted(long), PROFILE, "Why this role?", max_chars=100)
        self.assertLessEqual(len(d.text), 100)
        self.assertTrue(any("shortened" in w or "first 4 sentences" in w for w in d.warnings), d.warnings)

    def test_the_forms_own_length_note_is_read(self):
        from tailor.fill.answers import length_hint
        self.assertEqual(length_hint("Why Figma?", "Please share 3-4 sentences on why you want to join Figma"), (4, None, None))
        self.assertEqual(length_hint("Tell us about a project (in 150 words or less)"), (None, 150, None))
        self.assertEqual(length_hint("Why us?", "Maximum 500 characters"), (None, None, 500))
        self.assertEqual(length_hint("Describe yourself in two sentences."), (2, None, None))
        self.assertEqual(length_hint("Why this role?"), (None, None, None))

    def test_long_and_generic_drafts_are_sent_back_then_cut(self):
        # Figma, 2026-09-29: the first real draft was 5+ sentences of stock phrases
        generic = ("I am passionate about creating tools that empower teams. My background aligns with Figma's mission. "
                   "I am excited to bring my unique blend of skills. I have experience driving improvements. "
                   "I believe I would be a great fit. I am confident I can make an impact.")
        plain = ("At Acme I cut reporting time by 40% with a new dashboard, and the part I liked most was watching "
                 "people actually use it. Figma is where that kind of tool-building is the whole job.")
        model = Scripted(generic, plain)
        d = draft_answer(model, PROFILE, "Why do you want to join Figma?", hint="Please share 3-4 sentences")
        self.assertEqual(d.text, plain)
        self.assertIn("sounds generated", model.prompts[1])
        self.assertIn("4 sentences or fewer", model.prompts[0])
        self.assertIn("NOTE UNDER THE QUESTION: Please share 3-4 sentences", model.prompts[0])
        stubborn = draft_answer(Scripted(generic), PROFILE, "Why do you want to join Figma?", hint="Please share 3-4 sentences")
        self.assertLessEqual(len(stubborn.text.split(". ")), 4)
        self.assertTrue(any("stock phrasing" in w for w in stubborn.warnings), stubborn.warnings)

    def test_past_answers_to_similar_questions_are_reused(self):
        from tailor.fill.answers import similar_answers
        lib = [{"question": "Why do you want to join Figma?", "text": "I cut reporting time by 55% for a team once.", "company": "Figma"},
               {"question": "Describe a time you failed", "text": "A long story about failure and learning from it.", "company": "X"}]
        hits = similar_answers(lib, "Why do you want to join Stripe?")
        self.assertEqual([h["company"] for h in hits], ["Figma"])
        model = Scripted("I cut reporting time by 55% for a team, and I want to do that at Stripe.")
        d = draft_answer(model, PROFILE, "Why do you want to join Stripe?", examples=hits)
        self.assertIn("OWN PAST ANSWERS", model.prompts[0])
        self.assertIn("55%", d.text)  # a number from the applicant's own past answer is theirs, not invented

    def test_default_length_is_short(self):
        model = Scripted("Short answer.")
        draft_answer(model, PROFILE, "Why this role?")
        self.assertIn("4 sentences or fewer, at most 75 words", model.prompts[0])

    def test_sensitive_questions_never_reach_the_model(self):
        client = StubClient("should never be used")
        for q in ["Are you legally authorized to work in the US?", "What are your salary expectations?"]:
            with self.assertRaises(SensitiveQuestion):
                draft_answer(client, PROFILE, q)
        self.assertEqual(client.calls, [])

    def test_markdown_and_labels_are_stripped(self):
        d = draft_answer(Scripted("Answer: **I enjoy** _analysis_."), PROFILE, "Why this role?")
        self.assertEqual(d.text, "I enjoy analysis.")

    def test_job_excerpt_reaches_the_prompt(self):
        client = Scripted("I want the role.")
        draft_answer(client, PROFILE, "Why this role?", page_text="Required:\n- Experience with SQL dashboards\n")
        self.assertIn("SQL dashboards", client.prompts[0])


if __name__ == "__main__":
    unittest.main()


class BeyondResumeTests(unittest.TestCase):
    """Palantir (2026-10-10): "What's a skill ... that has nothing to do with your resume?" got a made-up road trip, and
    with the applicant's note ("Sales from acme rental, brightpath registered behavior tech...") the Brightpath part was dropped."""
    Q = "What's a skill or body of knowledge you have that has nothing to do with your resume? (150 words max)"
    NOTE = "Sales from acme rental, brightpath registered behavior tech. talk about applying psychology skills to job setting"

    def test_without_a_note_nothing_is_drafted(self):
        from tailor.fill.answers import NeedsApplicant

        with self.assertRaises(NeedsApplicant):
            draft_answer(Scripted("A few years ago I planned a road trip with friends."), PROFILE, self.Q)

    def test_the_form_plan_leaves_it_to_the_person(self):
        from tailor.fill.forms import FormField
        from tailor.fill.mapper import plan_field

        a = plan_field(FormField(id="q", label=self.Q, type="textarea", required=True), PROFILE)
        self.assertEqual(a.kind, "needs_you")
        self.assertIn("Tailor chat", a.reason)

    def test_every_point_of_the_note_is_covered_or_sent_back(self):
        left_out = "I used psychology in sales at Acme Rental Group to read customer behavior."
        whole = left_out + " As a registered behavior technician at Brightpath, I applied behavior principles daily."
        client = Scripted(left_out, whole)
        d = draft_answer(client, PROFILE, self.Q, instruction=self.NOTE)
        self.assertIn("Brightpath", d.text)
        self.assertIn("leaves them out: brightpath registered behavior tech", client.prompts[1])
        self.assertIn("cover every point", client.prompts[0])
        stuck = draft_answer(Scripted(left_out), PROFILE, self.Q, instruction=self.NOTE)
        self.assertTrue(any("leaves out what you asked for" in w for w in stuck.warnings))


class NoDashTests(unittest.TestCase):
    """The person's rule (2026-10-10): no em dashes in anything written for them."""

    def test_dashes_become_commas_and_ranges_keep_a_hyphen(self):
        from tailor.fill.answers import no_dashes

        self.assertEqual(no_dashes("guidance—like step-by-step checklists—based on it."),
                         "guidance, like step-by-step checklists, based on it.")
        self.assertEqual(no_dashes("people—it’s about systems."), "people, it’s about systems.")
        self.assertEqual(no_dashes("From 2015–2021."), "From 2015-2021.")

    def test_a_drafted_answer_has_no_em_dash(self):
        d = draft_answer(Scripted("At Acme Corp I cut reporting time by 40%—a big help to the team."), PROFILE,
                         "Why do you want to work here?")
        self.assertNotIn("—", d.text)
        self.assertIn("40%, a big help", d.text)

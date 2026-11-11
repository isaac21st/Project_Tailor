from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.fill.cover import _repeats  # noqa: E402


class RepeatTests(unittest.TestCase):
    def test_a_sentence_said_twice_in_other_words_is_found(self):
        # Simon-Kucher letter (2026-10-10): the model repeated its first paragraph's last sentence in the second
        text = ("My background in Python, Excel, and PowerPoint has allowed me to present complex findings clearly, whether "
                "crafting data visualizations for a datathon or building tools to support strategic decisions.\n\n"
                "At Acme Rental Group, I used customer insights to boost revenue by $2 million. My work with Python, Excel, "
                "and PowerPoint has enabled me to present complex findings clearly, whether crafting data visualizations "
                "for a datathon or building tools to support strategic decisions.")
        self.assertEqual(len(_repeats(text)), 1)
        self.assertIn("My work with Python", next(iter(_repeats(text))))

    def test_different_sentences_about_the_same_tools_are_kept(self):
        text = ("I used Python and SQL to organize public datasets for a team recommendation. "
                "At Center, I analyzed 1,240 member records in Python to inform event-location decisions.")
        self.assertEqual(_repeats(text), set())


class ScriptedModel:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _person():
    from tailor.fill.profile import Education, Experience, Profile

    return Profile(first_name="Jordan", last_name="Rivera", email="j@example.com", phone="555",
                   education=[Education(school="Example University, Harbor School of Business", degree="Master of Business Administration", expected=True),
                              Education(school="Example University School of Information", degree="Master of Science", expected=True)],
                   skills=["Python", "SQL"],
                   experience=[Experience(employer="City Hospital", title="Consultant", kind="project",
                                          bullets=["Mapped clinical staffing and patient workflow for a hospital service line"]),
                               Experience(employer="Corner Bakery", title="Clerk", bullets=["Sold bread"]),
                               Experience(employer="Retail Co", title="Analyst", bullets=["Analyzed store data to find where customers dropped off"])])


POSTING = ("Healthcare Consultant\n\nResponsibilities\n- Work with hospital clients on clinical operations and patient workflow\n"
           "- Analyze data and present to stakeholders\n\nQualifications\n- Experience in healthcare operations\n") * 4
PLAIN = ("Hospitals losing nurses to scheduling problems is the part of this job I'd want first. I'm finishing an MBA and a Master of Science in Information at Example University. "
         "It's the work I keep coming back to.\n\nCity Hospital asked us to map staffing and patient flow for one service line, so we walked through who did what and wrote it down. "
         "Your clients are asking the same question about their own clinics, only at a larger size and with more people involved in every single decision.\n\n"
         "Store data at Retail Co showed where customers dropped off, and I went looking for the reason behind it before suggesting anything to the team that ran the floor. "
         "Finding the point where things break down is most of what a workflow review comes to, and that's the habit I would start from on your projects.")


class HumanLetterTests(unittest.TestCase):
    def test_stock_bridges_and_the_name_opening_are_caught(self):
        from tailor.fill.cover import _stiff

        for s in ("This experience honed my ability to analyze data.", "I'm Jordan Rivera, a graduate student pursuing an MBA.",
                  "That is a critical skill for this role.", "It mirrors what your analysts do."):
            self.assertEqual(_stiff(s), [s])
        self.assertEqual(_stiff("Store data showed where customers dropped off."), [])

    def test_the_two_closest_stories_are_chosen_and_the_targets_count(self):
        from tailor.fill.cover import pick_stories

        self.assertEqual(pick_stories(_person(), POSTING, "Healthcare Consultant")[0].employer, "City Hospital")
        self.assertEqual(len(pick_stories(_person(), POSTING, "Healthcare Consultant")), 2)

    def test_the_model_sees_only_the_two_stories(self):
        from tailor.fill.cover import write_cover_letter

        m = ScriptedModel(PLAIN)
        write_cover_letter(m, _person(), "Acme", "Healthcare Consultant", POSTING)
        self.assertNotIn("Corner Bakery", m.prompts[0])
        self.assertNotIn("Skills: Python", m.prompts[0])

    def test_a_tool_from_the_skills_list_cannot_be_given_to_a_story(self):
        from tailor.fill.cover import write_cover_letter

        bad = PLAIN.replace("Store data at Retail Co showed", "I used Python and SQL at Retail Co, and it showed")
        d = write_cover_letter(ScriptedModel(bad), _person(), "Acme", "Healthcare Consultant", POSTING)
        self.assertNotIn("Python", d.text)

    def test_an_embellishment_is_taken_out(self):
        from tailor.fill.cover import write_cover_letter

        bad = PLAIN.replace("It's the work I keep coming back to.", "They're using it now.")
        d = write_cover_letter(ScriptedModel(bad), _person(), "Acme", "Healthcare Consultant", POSTING)
        self.assertNotIn("using it now", d.text)

    def test_a_stiff_draft_gets_one_rewrite_and_the_close_is_plain(self):
        from tailor.fill.cover import write_cover_letter

        stiff = "This experience honed my ability to listen.\n\n" + PLAIN
        m = ScriptedModel(stiff, PLAIN)
        d = write_cover_letter(m, _person(), "Acme", "Healthcare Consultant", POSTING)
        self.assertEqual(len(m.prompts), 2)
        self.assertIn("machine-written", m.prompts[1])
        self.assertNotIn("honed", d.text)
        self.assertTrue(d.text.endswith("Thanks for reading."))
        self.assertIn("the team at Acme", d.text)

    def test_school_sentence(self):
        from tailor.fill.cover import school_sentence

        self.assertEqual(school_sentence(_person()), "I'm finishing an MBA and a Master of Science in Information at Example University.")


if __name__ == "__main__":
    unittest.main()

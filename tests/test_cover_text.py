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


if __name__ == "__main__":
    unittest.main()

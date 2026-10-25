"""The person's own tailoring rules live in their profile folder (tailoring_rules.json), never in the code (2026-10-16)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.resume.rules import NO_RULES, load_rules, parse_rules  # noqa: E402


class RulesTests(unittest.TestCase):
    def test_a_new_person_has_no_rules(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIs(load_rules(d), NO_RULES)
        self.assertIs(load_rules(None), NO_RULES)
        self.assertEqual((NO_RULES.protect, NO_RULES.approved_rewrites, NO_RULES.education_first_degree), ((), [], ""))

    def test_the_file_is_read_and_bad_items_are_skipped(self):
        raw = {"protect": ["Dr. Example"], "approved_rewrites": [["old words", "new words"], ["only one"]],
               "reframes": [{"when": r"\bdashboards?\b", "old": "a tool", "new": "a dashboard"}, {"when": "(", "old": "x", "new": "y"}],
               "shortenings": [{"id": "s1", "label": "shortened a line", "where": "skills", "find": "long phrase, ",
                                "costlier_when": "long phrase", "per_mention": 4, "max_mentions": 2},
                               {"id": "bad", "where": "nowhere", "find": "x"}],
               "education_first": {"degree": "Master of Science", "label": "MS first", "reason": "my order"}}
        r = parse_rules(raw)
        self.assertEqual(r.protect, ("Dr. Example",))
        self.assertEqual(r.approved_rewrites, [("old words", "new words")])
        self.assertEqual(len(r.reframes), 1)                      # the broken regex is skipped
        self.assertEqual([s.id for s in r.shortenings], ["s1"])   # an unknown place is skipped
        self.assertEqual(r.shortening("s1").cost_for("long phrase and long phrase and long phrase"), 1.5 + 8)
        self.assertEqual((r.education_first_degree, r.education_first_label), ("Master of Science", "MS first"))

    def test_changes_to_the_file_are_picked_up(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tailoring_rules.json"
            p.write_text(json.dumps({"protect": ["A"]}), encoding="utf-8")
            self.assertEqual(load_rules(d).protect, ("A",))
            import os
            import time
            p.write_text(json.dumps({"protect": ["B"]}), encoding="utf-8")
            os.utime(p, (time.time() + 5, time.time() + 5))
            self.assertEqual(load_rules(d).protect, ("B",))


if __name__ == "__main__":
    unittest.main()

"""Portfolio text extraction tests (synthetic HTML only; no network)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.profile.portfolio import extract_lines  # noqa: E402


def lines(body: str, head: str = "") -> list[str]:
    return extract_lines(f"<html><head><title>T</title>{head}</head><body>{body}</body></html>")[1]


class ExtractLinesTests(unittest.TestCase):
    def test_css_and_scripts_are_dropped(self):
        out = lines("<p>Real text</p><script>var a=1</script>", head="<style>body{color:red}</style>")
        self.assertEqual(out, ["Real text"])

    def test_typewriter_letters_are_rejoined_and_keep_repeats(self):
        h1 = "".join(f"<span>{c}</span>" for c in "Curious") + "<span> </span>" + "".join(f"<span>{c}</span>" for c in "Mind.")
        self.assertEqual(lines(f"<h1>{h1}</h1>"), ["Curious Mind."])

    def test_repeated_desktop_and_mobile_blocks_are_deduplicated(self):
        self.assertEqual(lines("<p>Same block of text</p><p>Other</p><p>Same block of text</p>"),
                         ["Same block of text", "Other"])

    def test_short_repeated_lines_are_kept(self):
        # "5" appearing twice is two different stats, not a duplicated layout block
        self.assertEqual(lines("<p>5</p><p>New investors</p><p>5</p><p>Years</p>"),
                         ["5", "New investors", "5", "Years"])

    def test_navigation_words_are_dropped(self):
        self.assertEqual(lines("<a>Work</a><a>About</a><p>Case study</p>"), ["Case study"])


if __name__ == "__main__":
    unittest.main()

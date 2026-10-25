"""Static guards on the extension source: permissions stay narrow and nothing can submit a form."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"


class ManifestTests(unittest.TestCase):
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))

    def test_manifest_v3_and_expected_permissions_only(self):
        m = self.manifest
        self.assertEqual(m["manifest_version"], 3)
        self.assertEqual(sorted(m["permissions"]), ["activeTab", "alarms", "notifications", "offscreen", "scripting", "storage"])

    def test_the_only_host_it_can_call_is_the_local_server(self):
        self.assertEqual(self.manifest["host_permissions"], ["http://127.0.0.1:8765/*"])

    def test_content_scripts_run_only_on_application_sites_over_https(self):
        matches = [p for cs in self.manifest["content_scripts"] for p in cs["matches"]]
        self.assertTrue(matches)
        for p in matches:
            self.assertTrue(p.startswith("https://"), p)
        self.assertNotIn("<all_urls>", matches)

    def test_every_referenced_file_exists(self):
        m = self.manifest
        files = [m["background"]["service_worker"], m["action"]["default_popup"], m["options_page"]]
        files += list(m["icons"].values())
        for cs in m["content_scripts"]:
            files += cs["js"] + cs.get("css", [])
        for f in files:
            self.assertTrue((EXT / f).exists(), f)


class NoSubmitTests(unittest.TestCase):
    content = (EXT / "content.js").read_text(encoding="utf-8")
    code = re.sub(r"//.*", "", content)  # comments may mention the words; code may not

    def _body(self, name: str) -> str:
        m = re.search(r"(?:async )?function " + name + r"\(.*?\n  }\n", self.code, re.S)
        self.assertIsNotNone(m, name)
        return m.group(0)

    def test_no_way_to_submit_a_form(self):
        for forbidden in (".submit(", "requestSubmit", 'type="submit"', "type='submit'", "[type=submit]",
                          'new Event("click"', "new Event('click'",
                          "querySelector('button", 'querySelector("button', "form.submit", ".action ="):
            self.assertNotIn(forbidden, self.code, forbidden)

    def test_exactly_fourteen_clicks_exist_and_each_is_guarded(self):
        clicks = [m.start() for m in re.finditer(r"\.click\(\)", self.code)]
        self.assertEqual(len(clicks), 14)
        # JazzHR: its own "Attach resume" link (href="#") inside the resume question, only for its hidden resume box
        rv = self._body("revealUpload")
        self.assertEqual(rv.count(".click()"), 1)
        for part in ('el.id !== "resumator-resume-value"', '#resumator-resume #resumator-choose-upload', 'choose.tagName === "A"',
                     '=== "#"', "isVisible(choose)"):
            self.assertIn(part, rv)
        for pos in clicks[:2]:  # radios and checkboxes, on inputs checked by assertSafe
            before = self.code[max(0, pos - 400):pos]
            self.assertIn("assertSafe(", before)
            self.assertRegex(before[-200:], r"radio|checkbox|checked")
        combo = self._body("chooseCombobox")
        self.assertIn("pick.click()", combo)  # an option inside the dropdown's own list
        self.assertRegex(combo, r'pick\.getAttribute\("role"\) !== "option"')
        self.assertIn("comboOptions(input)", combo)
        self.assertEqual(combo.count(".click()"), 1)
        # Workday: a button that opens a list, checked right before it is pressed, then an option in that list
        # Lever's location box: a plain text suggestion in the box's own list, checked right before it is pressed
        sg = self._body("pickSuggestion")
        self.assertEqual(sg.count(".click()"), 1)
        self.assertLess(sg.index("assertSuggestion(pick, input)"), sg.index("pick.click()"))
        self.assertIn("assertSafe(input)", sg)
        guard = self._body("assertSuggestion")
        for part in ("box.contains(el)", "(DIV|LI)", 'closest("a, button, [role=\'button\']")', "lab.control !== input", 'querySelector("input, select, textarea, button, a")',
                     "submit|apply|next|continue|send"):
            self.assertIn(part, guard)
        lb = self._body("chooseListbox")
        self.assertEqual(lb.count(".click()"), 2)
        self.assertLess(lb.index("assertListboxButton(btn)"), lb.index("btn.click()"))
        self.assertRegex(lb, r'pick\.getAttribute\("role"\) !== "option"')
        guard = self._body("assertListboxButton")
        for word in ("submit", "next", "apply", "save", "continue", "sign"):
            self.assertIn(word, self.code[self.code.index("const ACTION_WORDS"):self.code.index("const ACTION_WORDS") + 200])
        self.assertIn("isListboxButton(el)", guard)
        defn = self.code[self.code.index("const isListboxButton"):self.code.index("const isListboxButton") + 260]
        for part in ('aria-haspopup', '"listbox"', '!== "submit"', '"BUTTON"'):
            self.assertIn(part, defn)

    def test_mouse_events_exist_only_inside_chooseCombobox_and_go_to_the_dropdowns_own_box(self):
        combo = self._body("chooseCombobox")
        outside = self.code.replace(combo, "")
        self.assertNotRegex(outside, r"MouseEvent|PointerEvent")
        self.assertEqual(len(re.findall(r"\.dispatchEvent\(new Ev\(", combo)), 1)
        self.assertIn("control.dispatchEvent(new Ev(", combo)
        self.assertIn("assertSafe(input)", combo)
        self.assertIn("comboControl(input)", combo)

    def test_the_dropdown_box_is_never_inside_something_a_press_could_activate(self):
        body = self._body("comboControl")
        self.assertIn("closest(\"button, a, label, summary, [role='button']\")", body)
        self.assertIn("? null : node", body)  # refuse, do not press

    def test_assertSafe_rejects_buttons_and_submit_inputs(self):
        self.assertRegex(self.code, r'SKIP_TYPES = new Set\(\["hidden", "submit", "button", "reset", "image", "password"\]\)')
        self.assertIn("refusing to touch", self.code)

    def test_passwords_are_never_read_or_written(self):
        self.assertIn('"password"', self.code)  # in SKIP_TYPES, so collectFields skips them


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SyntaxTests(unittest.TestCase):
    def test_every_script_parses(self):
        for js in sorted(EXT.glob("*.js")):
            r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{js.name}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()


class SectionHeadingTests(unittest.TestCase):
    def test_only_section_titles_count(self):
        import subprocess
        js = (EXT_CODE := (Path(__file__).resolve().parent.parent / "extension" / "content.js").read_text())
        start = js.index("const SECTION_EDUCATION")
        defs = js[start:js.index("\n\n", start)]
        script = defs + """
const t = (s) => SECTION_EDUCATION.test(s) ? "education" : SECTION_WORK.test(s) ? "experience" : "";
console.log(JSON.stringify(["higher education", "work experience", "employment history", "education",
  "equal employment opportunity information", "voluntary self-identification", "your experience with ai"].map(t)));"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), ["education", "experience", "experience", "education", "", "", ""])


class NextStepTests(unittest.TestCase):
    """The person's request (2026-10-02): go on to the next step by itself, stop at Review. Never Submit."""
    code = re.sub(r"//.*", "", (EXT / "content.js").read_text(encoding="utf-8"))

    def body(self, name):
        m = re.search(r"(?:async )?function " + name + r"\(.*?\n  }\n", self.code, re.S)
        self.assertIsNotNone(m, name)
        return m.group(0)

    def test_only_the_steps_next_button_after_checks(self):
        go = self.body("maybeGoNext")
        self.assertEqual(go.count(".click()"), 1)
        self.assertLess(go.index("assertNextButton(btn)"), go.index("btn.click()"))
        # 2026-10-13: a required question stops the step; optional ones and drafts go on the review list for the end
        for check in ("onReviewStep()", "result.requiredNeeds", "addToReview(result.review)", "pageErrors()", "autoNextCount >= 8",
                      "steps >= 10", "cancelNext", "box.checked", "nextButton()"):
            self.assertIn(check, go)
        self.assertLess(go.index("result.requiredNeeds"), go.index("btn.click()"))
        self.assertIn("12twenty", re.search(r"const NO_AUTO_NEXT = /(.*?)/;", self.code).group(1))  # Harbor Recruit: never stepped
        self.assertIn("NO_AUTO_NEXT.test(location.hostname)", self.body("onReviewStep"))
        self.assertLess(go.index("onReviewStep()"), go.index("btn.click()"))
        guard = self.body("assertNextButton")
        for part in ('"pageFooterNextButton"', "isStepControl(btn)", "NEVER_PRESS.test(text)", "FINAL_TEXT.test(text)",
                     "NEXT_TEXT.test(text)", "onReviewStep()", 'closest("#tailor-panel-host")'):
            self.assertIn(part, guard)
        never = re.search(r"const NEVER_PRESS = /(.*?)/i", self.code).group(1)
        for word in ("submit", "apply", "send", "finish", "confirm", "sign", "draft"):
            self.assertIn(word, never)
        nxt = re.search(r"const NEXT_TEXT = /(.*?)/i", self.code).group(1)
        self.assertTrue(nxt.startswith("^(?:") and nxt.endswith(")$"))  # the whole text, never a part of it
        for word in ("submit", "apply", "send", "finish", "confirm"):
            self.assertNotIn(word, nxt)
        final = re.search(r"const FINAL_TEXT = /(.*?)/i", self.code).group(1)
        for word in ("submit", "apply", "send", "finish"):
            self.assertIn(word, final)
        rv = self.body("onReviewStep")
        self.assertLess(rv.index("SENDS_ON_NEXT.test("), rv.index("isWorkday()"))  # a warning wins on every site
        sends = re.search(r"const SENDS_ON_NEXT = /(.*?)/i;", self.code).group(1)
        for phrase in ("submit", "final", "application will be"):
            self.assertIn(phrase, sends)
        nb = self.body("nextButton")
        self.assertIn("new Set(found.map", nb)  # two different step buttons: none is pressed

    def test_add_presses_only_the_work_or_education_add_button(self):
        add = self.body("addEntries")
        self.assertEqual(add.count(".click()"), 1)
        self.assertLess(add.index("assertAddButton(btn, heading)"), add.index("btn.click()"))
        self.assertIn("Math.min(n, 6)", add)
        guard = self.body("assertAddButton")
        for part in ("isWorkday()", '"add-button"', 'btn.tagName !== "BUTTON"', "/^add(?: another(?: job| degree)?)?$/i",
                     "(?:work experience|education)", "groupHeading(btn) !== heading", "isGreenhouse()",
                     '"add-another-button"', "btn.closest(ghContainer(heading))", "isAddButtonFor(btn, heading)"):
            self.assertIn(part, guard)
        self.assertIn("ASHBY_ADD[heading].test(clean(btn.textContent))", guard)  # Ashby: "+ Add Education" in its own section only
        any_site = self.body("isAddButtonFor")  # any other site: "Add..." words, never a submit/file/link, in its section
        for part in ("ADD_WORDS.test(t)", "!NOT_AN_ENTRY.test(t)", "!btn.disabled", "#tailor-panel-host", "sectionOfButton(btn) ==="):
            self.assertIn(part, any_site)
        for word in ("submit", "apply", "delete", "save", "file", "link"):
            self.assertRegex(self.code, r"NOT_AN_ENTRY = /[^\n]*" + word)
        self.assertIn("if (!grew) break", add)  # a press that adds nothing is never repeated

    def test_choice_buttons_press_only_this_questions_answer(self):
        ch = self.body("chooseButton")
        self.assertEqual(ch.count(".click()"), 1)
        self.assertLess(ch.index("assertChoiceButton(btn, entry.els)"), ch.index("btn.click()"))
        guard = self.body("choiceButtons")
        for part in ("aria-pressed", "btns.length < 2 || btns.length > 6", "ACTION_WORDS.test(t)"):
            self.assertIn(part, guard)

    def test_workday_search_picks_only_a_result_option(self):
        wd = self.body("chooseWorkdayPrompt")
        self.assertEqual(wd.count(".click()"), 1)
        guard = wd.index('pick.getAttribute("data-automation-id") !== "menuItem"')
        self.assertLess(guard, wd.index("pick.click()"))
        self.assertIn("assertSafe(input)", wd)
        self.assertIn('[data-automation-id=\'promptLeafNode\']', self.code)

    def test_an_entry_editor_gets_only_its_own_save_and_never_a_form_submit(self):
        flow = self.body("modalEntries")
        self.assertEqual(flow.count(".click()"), 2)  # the section's Add, the editor's Save
        self.assertLess(flow.index("assertAddButton(btn, heading)"), flow.index("btn.click()"))
        self.assertLess(flow.index("assertEditorSave(save, dlg, section)"), flow.index("save.click()"))
        self.assertIn("Math.min(mine.length, 6)", flow)
        guard = self.body("assertEditorSave")
        for part in ("dlg.contains(btn)", "EDITOR_SAVE.test(t)", "/submit|apply|send|sign|finish/i", "anchor.test(t)).length !== 1",
                     "input[type=file], input[type=email], input[type=password]", "sendsApplication(btn, dlg)"):
            self.assertIn(part, guard)
        self.assertIn("sendsApplication(btn)", self.body("assertAddButton"))
        sends = self.body("sendsApplication")
        self.assertIn('"submit"', sends)
        self.assertIn("input[type=email], input[type=file], input[type=tel]", sends)

    def test_remove_presses_only_a_project_entrys_own_remove_button(self):
        rm = self.body("removeEntries")
        self.assertEqual(rm.count(".click()"), 1)
        self.assertLess(rm.index("assertRemoveButton(btn, entry)"), rm.index("btn.click()"))
        guard = self.body("assertRemoveButton")
        for part in ("isGoogleCareers()", "/^remove this job$/i", "entry.contains(btn)", "Employer name"):
            self.assertIn(part, guard)
        self.assertIn("is a project on your resume", self.code)


class SchoolBoardReaderTests(unittest.TestCase):
    """school_board.js (2026-10-14) only reads a school's 12twenty job list: no clicks, no form, one message."""

    def test_it_only_reads(self):
        js = (EXT / "school_board.js").read_text(encoding="utf-8")
        self.assertNotIn(".click(", js)
        self.assertNotIn("submit", js.lower())
        self.assertNotIn("dispatchEvent", js)
        self.assertEqual(re.findall(r'type: "([a-z_]+)"', js), ["import_found"])
        manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
        board = [c for c in manifest["content_scripts"] if "school_board.js" in c["js"]]
        self.assertEqual(board[0]["matches"], ["https://*.12twenty.com/*"])


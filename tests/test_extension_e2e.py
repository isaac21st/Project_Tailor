"""End to end: the real extension in a real Chromium, the real local server, and a fake application form.

The form has a Submit button, a sensitive question, an EEO dropdown and a hidden trap field. The test checks what
the extension fills, what it flags, and that it never submits.
"""
from __future__ import annotations

import http.server
import json
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402
from ext_helpers import start_or_clean_up  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tailor.config import Settings, apply_environment, load_settings  # noqa: E402
from tailor.fill.profile import Profile  # noqa: E402
from tailor.llm import StubClient  # noqa: E402
from tailor.server.app import create_app  # noqa: E402

EXT = Path(__file__).resolve().parent.parent / "extension"
TOKEN = "e2e-token-abc"
PROFILE = Profile(first_name="Jane", last_name="Doe", email="jane@example.com", phone="(555) 010-0000",
                  linkedin_url="https://www.linkedin.com/in/janedoe", portfolio_url="https://janedoe.example/")
DRAFT = "I enjoy analytical work."

FORM = """<!doctype html><html><head><title>Apply - Acme</title></head><body>
<h1>Data Analyst</h1><p>We are hiring an analyst. Required: SQL and dashboards.</p>
<form id="app" action="/submitted" method="post" novalidate><h2>Application</h2>
<div><label for="first_name">First Name <span>*</span></label><input id="first_name" name="first_name" required></div>
<div><label for="last_name">Last Name *</label><input id="last_name" name="last_name" required></div>
<div><label for="email">Email *</label><input id="email" type="email" name="email" required></div>
<div><label for="phone">Phone</label><input id="phone" type="tel" name="phone"></div>
<div><label for="resume">Resume/CV *</label><input id="resume" type="file" name="resume" style="display:none"><button type="button">Attach</button></div>
<div><label for="linkedin">LinkedIn Profile</label><input id="linkedin" name="linkedin"></div>
<div><label for="website">Website</label><input id="website" name="website"></div>
<div><label for="city">Location (City) *</label><input id="city" name="city" required></div>
<div><label for="hear">How did you hear about this job? *</label>
 <select id="hear" name="hear" required><option value="">Select...</option><option value="1">LinkedIn</option><option value="2">Job board</option></select></div>
<fieldset><legend>Are you legally authorized to work in the United States? *</legend>
 <label><input type="radio" name="auth" value="yes"> Yes</label><label><input type="radio" name="auth" value="no"> No</label></fieldset>
<fieldset><legend>Have you worked at Acme before? *</legend>
 <label><input type="radio" name="prior" value="yes"> Yes</label><label><input type="radio" name="prior" value="no" checked> No</label></fieldset>
<fieldset><legend>Will you now or in the future require visa sponsorship? *</legend>
 <label><input type="radio" name="spons" value="yes"> Yes</label><label><input type="radio" name="spons" value="no" checked> No</label></fieldset>
<fieldset><legend>Language Skill(s) (Check all that apply) *</legend>
 <label><input type="checkbox" name="lang" value="en" required> English</label><label><input type="checkbox" name="lang" value="es"> Spanish</label>
 <label><input type="checkbox" name="lang" value="zh"> Mandarin</label></fieldset>
<div><label for="portfolio2">Portfolio URL</label><input id="portfolio2" name="urls[Portfolio]"></div>
<div><label for="why">Why do you want to work here?</label><textarea id="why" name="why"></textarea></div>
<input type="text" id="trap" name="website2" style="display:none" aria-hidden="true">
<h3>Voluntary Self-Identification</h3>
<div><label for="gender">Gender</label><select id="gender" name="gender"><option value="">Select...</option><option value="m">Male</option><option value="f">Female</option><option value="d">Decline to answer</option></select></div>
<button type="submit" id="submit">Submit Application</button></form>
<script>/* like Lever's location box: typed text is thrown away unless picked from a list */
document.getElementById('portfolio2').addEventListener('change', (e) => { setTimeout(() => { e.target.value = ''; }, 50); });
window.__submits = 0; document.getElementById('app').addEventListener('submit', e => { e.preventDefault(); window.__submits++; });</script>
</body></html>"""


STEPS = """<!doctype html><html><head><title>Apply - Acme (step 1 of 2)</title></head><body>
<h1>Data Analyst</h1><form id="app" novalidate><div id="step">
<div><label for="first_name">First Name *</label><input id="first_name" name="first_name" required></div>
<div><label for="last_name">Last Name *</label><input id="last_name" name="last_name" required></div>
<div><label for="email">Email *</label><input id="email" type="email" name="email" required></div>
</div><button type="button" id="next">Next</button></form>
<script>window.__submits = 0; document.getElementById('app').addEventListener('submit', e => { e.preventDefault(); window.__submits++; });
document.getElementById('next').addEventListener('click', () => { document.getElementById('step').innerHTML =
 '<div><label for="phone">Phone *</label><input id="phone" type="tel" name="phone" required></div>' +
 '<div><label for="linkedin">LinkedIn Profile</label><input id="linkedin" name="linkedin"></div>' +
 '<div><label for="website">Website</label><input id="website" name="website"></div>'; });</script>
</body></html>"""


WORKDAY = """<!doctype html><html><head><title>My Information - Workday</title></head><body>
<div data-automation-id="applyFlowPage"><h2>My Information</h2>
<div><label for="fn">First Name*</label><input id="fn" data-automation-id="legalNameSection_firstName" required></div>
<div><label for="ln">Last Name*</label><input id="ln" data-automation-id="legalNameSection_lastName" required></div>
<div><label for="a1">Address Line 1*</label><input id="a1" data-automation-id="addressSection_addressLine1" required></div>
<div><label for="st">State*</label><button type="button" id="st" aria-haspopup="listbox" aria-required="true">Select One</button></div>
<div><label for="pt">Phone Device Type*</label><button type="button" id="pt" aria-haspopup="listbox" aria-required="true">Select One</button></div>
<div><label for="hh">How Did You Hear About Us?*</label><button type="button" id="hh" aria-haspopup="listbox" aria-required="true">Select One</button></div>
<div><label for="gd">Gender</label><button type="button" id="gd" aria-haspopup="listbox">Select One</button></div>
<button type="button" id="trap" aria-haspopup="listbox">Next</button>
<button type="button" id="save" data-automation-id="bottom-navigation-next-button">Save and Continue</button>
</div>
<script>
window.__pressed = [];
const OPTIONS = { st: ["Alabama", "California", "Michigan"], pt: ["Home", "Mobile"], hh: ["Company Website", "LinkedIn"],
                  gd: ["Female", "Male", "I do not wish to answer"] };
document.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  window.__pressed.push(b.id);
  if (!OPTIONS[b.id]) return;
  document.querySelectorAll("[role=listbox]").forEach((l) => l.remove());
  const ul = document.createElement("ul"); ul.setAttribute("role", "listbox"); ul.id = b.id + "-list";
  b.setAttribute("aria-controls", ul.id);
  for (const o of OPTIONS[b.id]) {
    const li = document.createElement("li"); li.setAttribute("role", "option"); li.textContent = o;
    li.addEventListener("click", () => { b.textContent = o; ul.remove(); });
    ul.appendChild(li);
  }
  document.body.appendChild(ul);
});
</script></body></html>"""

THANKS = """<!doctype html><html><head><title>Acme Careers</title></head><body>
<h1>Thank you for applying!</h1><p>We have received your application for the Data Analyst role at Acme.</p></body></html>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (STEPS if self.path.startswith("/steps") else THANKS if self.path.startswith("/thanks")
                else WORKDAY if self.path.startswith("/workday") else FORM).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *a):
        pass


class ExtensionE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        settings = Settings(data_dir=tmp / "data")
        settings.ensure_dirs()
        pdf = settings.output_dir / "current_resume.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% e2e test resume\n")

        cls.api_port, cls.site_port = free_port(), free_port()
        app = create_app(settings, StubClient(DRAFT), PROFILE, TOKEN, pdf)
        cls.api = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.api_port, log_level="error"))
        threading.Thread(target=cls.api.run, daemon=True).start()
        cls.site = http.server.ThreadingHTTPServer(("127.0.0.1", cls.site_port), _Quiet)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        while not cls.api.started:
            pass

        # a test copy of the extension that is also allowed on the local fixture site and test API port
        ext = tmp / "ext"
        shutil.copytree(EXT, ext)
        manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
        manifest["host_permissions"].append(f"http://127.0.0.1:{cls.api_port}/*")
        manifest["content_scripts"][0]["matches"].append(f"http://127.0.0.1:{cls.site_port}/*")
        (ext / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        apply_environment(load_settings())
        cls.pw = sync_playwright().start()
        try:
            cls.ctx = cls.pw.chromium.launch_persistent_context(
                str(tmp / "profile"), channel="chromium", headless=True,
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        except Exception:
            # stop Playwright's event loop here, or every later sync_playwright() in the run fails too
            cls.pw.stop()
            cls.api.should_exit = True
            cls.site.shutdown()
            cls.tmp.cleanup()
            raise
        cls.sw = start_or_clean_up(cls, cls.ctx, cls.pw, lambda: (setattr(cls.api, "should_exit", True), cls.site.shutdown(), cls.tmp.cleanup()))
        cls.set_config(TOKEN)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.close()
        cls.pw.stop()
        cls.api.should_exit = True
        cls.site.shutdown()
        cls.tmp.cleanup()

    @classmethod
    def set_config(cls, token):
        cls.sw.evaluate("cfg => chrome.storage.local.set(cfg)",
                        {"backendUrl": f"http://127.0.0.1:{cls.api_port}", "token": token, "autoFill": False})
        cls.sw.evaluate("chrome.storage.session.clear()")

    def open_form(self):
        page = self.ctx.new_page()
        page.goto(f"http://127.0.0.1:{self.site_port}/apply")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        return page

    def status(self, page):
        return page.evaluate("document.querySelector('#tailor-panel-host').shadowRoot.getElementById('status').textContent")

    def click_fill(self, page):
        page.get_by_role("button", name="Fill this page").click()
        page.wait_for_function(
            "() => { const t = document.querySelector('#tailor-panel-host').shadowRoot.getElementById('status').textContent;"
            " return /need you|filled\\.|Cannot|token|No access/.test(t) && !/Drafting|Reading|Tailoring|Writing|Adding/.test(t); }", timeout=20000)

    def test_1_fills_known_fields_flags_the_rest_and_never_submits(self):
        page = self.open_form()
        url_before = page.url
        self.click_fill(page)
        val = lambda i: page.evaluate("id => document.getElementById(id).value", i)  # noqa: E731
        cls_of = lambda i: page.evaluate("id => document.getElementById(id).className", i)  # noqa: E731

        # filled from the profile
        self.assertEqual([val(i) for i in ("first_name", "last_name", "email", "phone")],
                         ["Jane", "Doe", "jane@example.com", "(555) 010-0000"])
        self.assertEqual(val("linkedin"), "https://www.linkedin.com/in/janedoe")
        self.assertEqual(val("website"), "https://janedoe.example/")
        for i in ("first_name", "email", "linkedin"):
            self.assertIn("tailor-filled", cls_of(i))

        # resume attached to the hidden file input
        self.assertEqual(page.evaluate("document.getElementById('resume').files.length"), 1)
        self.assertEqual(page.evaluate("document.getElementById('resume').files[0].name"), "Jane_Doe_Resume.pdf")

        # model draft, marked for review
        self.assertEqual(val("why"), DRAFT)
        self.assertIn("tailor-draft", cls_of("why"))

        # flagged for the person, never filled
        self.assertEqual(val("city"), "")
        self.assertIn("tailor-needs", cls_of("city"))
        self.assertEqual(val("hear"), "")
        self.assertIn("tailor-needs", cls_of("hear"))
        self.assertEqual(val("gender"), "")
        self.assertIn("tailor-needs", cls_of("gender"))
        self.assertIn("sensitive", page.evaluate("document.getElementById('gender').title"))
        self.assertEqual(page.evaluate("document.querySelectorAll('input[name=auth]:checked').length"), 0)
        self.assertIn("tailor-needs", page.evaluate("document.querySelector('input[name=auth]').className"))

        # a box that threw the typed text away is flagged, not counted as filled
        self.assertEqual(val("portfolio2"), "")
        self.assertIn("tailor-needs", cls_of("portfolio2"))
        # a check-all-that-apply question is listed once, however many boxes it has
        needs = page.evaluate("[...document.querySelector('#tailor-panel-host').shadowRoot.querySelectorAll('#needs li')].map(li => li.textContent)")
        self.assertEqual(sum("Language Skill" in n for n in needs), 1, needs)
        self.assertTrue(any("pick from the site's list" in n for n in needs), needs)

        # a question already answered on the page is left alone; a sensitive one stays flagged even when pre-selected
        self.assertNotIn("tailor-needs", page.evaluate("document.querySelector('input[name=prior]').className"))
        self.assertIn("tailor-needs", page.evaluate("document.querySelector('input[name=spons]').className"))
        self.assertEqual(page.evaluate("document.querySelector('input[name=spons]:checked').value"), "no")  # untouched

        # the hidden trap field is not touched, and NOTHING was submitted or navigated
        self.assertEqual(val("trap"), "")
        self.assertEqual(page.evaluate("window.__submits"), 0)
        self.assertEqual(page.url, url_before)

        # the panel tells the person what is left, and an alert was raised for them
        text = self.status(page)
        self.assertRegex(text, r"\d+ filled")
        self.assertIn("need you", text)
        alert = self.sw.evaluate("chrome.storage.session.get('lastAlert')")["lastAlert"]
        self.assertEqual(alert["kind"], "needs_you")
        page.close()

    def test_1b_the_chat_box_writes_into_the_chosen_question_and_never_submits(self):
        page = self.open_form()
        page.get_by_text("Help me write an answer").click()
        page.wait_for_function(
            "() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('chatfield').options.length > 0")
        options = page.evaluate(
            "[...document.querySelector('#tailor-panel-host').shadowRoot.getElementById('chatfield').options].map(o => o.textContent)")
        # the free-text question (never the name, email or city boxes), then the resume
        self.assertEqual(options, ["Why do you want to work here?", "Cover letter for this posting", "My resume for this posting"])
        page.locator("#chatin").fill("3-4 sentences, lead with my analytics work")
        page.get_by_role("button", name="Write it").click()
        page.wait_for_function("() => document.getElementById('why').value.length > 0", timeout=15000)
        self.assertEqual(page.evaluate("document.getElementById('why').value"), DRAFT)
        self.assertIn("tailor-draft", page.evaluate("document.getElementById('why').className"))
        note = page.evaluate("document.querySelector('#tailor-panel-host').shadowRoot.getElementById('chatnote').textContent")
        self.assertIn("Placed in the box", note)
        self.assertEqual(page.evaluate("window.__submits"), 0)
        # nothing else on the form was touched by the chat
        self.assertEqual(page.evaluate("document.getElementById('first_name').value"), "")
        page.close()

    def test_1c_the_chat_box_can_retailor_the_resume_and_never_submits(self):
        page = self.open_form()
        page.get_by_text("Help me write an answer").click()
        page.wait_for_function(
            "() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('chatfield').options.length > 1")
        page.evaluate("""() => { const s = document.querySelector('#tailor-panel-host').shadowRoot.getElementById('chatfield');
                                 s.selectedIndex = s.options.length - 1; }""")
        page.locator("#chatin").fill("lean on the AI/ML qualification")
        page.get_by_role("button", name="Write it").click()
        page.wait_for_function("() => document.getElementById('resume').files.length === 1", timeout=15000)
        note = page.evaluate("document.querySelector('#tailor-panel-host').shadowRoot.getElementById('chatnote').textContent")
        self.assertIn("Attached to the form", note)
        self.assertEqual(page.evaluate("document.getElementById('why').value"), "")  # no answer was touched
        self.assertEqual(page.evaluate("window.__submits"), 0)
        page.close()

    def test_1d_save_to_desk_reads_the_page_and_never_submits(self):
        page = self.open_form()
        got = self.sw.evaluate("""async () => {
            for (const t of await chrome.tabs.query({})) {  // tab URLs are hidden without the "tabs" permission: ask each
              const r = await chrome.tabs.sendMessage(t.id, { type: 'posting' }).catch(() => null);
              if (r && r.url.includes('/apply')) return r;
            }
            return null; }""")
        self.assertIn("/apply", got["url"])
        self.assertFalse(got["isPosting"])  # a form with a line of requirements is not a posting (no heading)
        self.assertIn("We are hiring an analyst", got["text"])
        self.assertEqual(page.evaluate("window.__submits"), 0)
        page.close()

    def test_1e_each_new_step_is_filled_after_the_person_clicks_next(self):
        for auto in (True, False):
            page = self.ctx.new_page()
            page.goto(f"http://127.0.0.1:{self.site_port}/steps")
            page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
            if not auto:
                page.evaluate("document.querySelector('#tailor-panel-host').shadowRoot.getElementById('autostep').click()")
            self.click_fill(page)
            self.assertEqual(page.evaluate("document.getElementById('email').value"), "jane@example.com")
            page.click("#next")  # the person moves to the next step; the extension never does
            if auto:
                page.wait_for_function("() => document.getElementById('phone') && document.getElementById('phone').value", timeout=15000)
                self.assertEqual(page.evaluate("document.getElementById('linkedin').value"), "https://www.linkedin.com/in/janedoe")
            else:
                page.wait_for_timeout(2500)
                self.assertEqual(page.evaluate("document.getElementById('phone').value"), "")
            self.assertEqual(page.evaluate("window.__submits"), 0)
            page.close()

    def test_1f_a_follow_up_question_is_not_a_new_step_and_edits_are_kept(self):
        page = self.open_form()
        self.click_fill(page)
        page.fill("#why", "My own words.")  # the person edits the draft
        page.evaluate("""() => { const d = document.createElement('div');  // a follow-up question appears on the same page
            d.innerHTML = '<label for="fu1">Which office?</label><input id="fu1" name="fu1"><label for="fu2">Team?</label><input id="fu2" name="fu2">';
            document.getElementById('app').prepend(d); }""")
        page.wait_for_timeout(2500)
        self.assertEqual(page.evaluate("document.getElementById('why').value"), "My own words.")
        page.close()

    def test_1g_new_roles_from_a_search_show_on_the_icon_and_as_a_notification(self):
        from tailor.jobs.search import SearchStore

        store = SearchStore(Path(self.tmp.name) / "data")
        rec = {"id": "abc123abc123", "source": "greenhouse", "company": "acme", "title": "Associate Product Manager",
               "url": "https://x.example/1", "location": "New York, NY", "text": "", "posted": "", "fit": 70,
               "years": None, "deadline": None, "sponsorship": "not_stated", "clearance": False,
               "first_seen": "2026-09-30T08:00:00.000001", "last_seen": "2026-09-30T08:00:00.000001",
               "dismissed": False, "desk_id": None}
        store.save({"runs": [{"at": "2026-09-30T08:00:00.000001", "read": 10, "kept": 1, "new": 1, "left_out": {},
                              "feeds": 1, "errors": []}], "results": {rec["id"]: rec}})
        self.sw.evaluate("chrome.storage.local.remove('notifiedRun')")
        self.sw.evaluate("checkNewRoles()")
        self.assertEqual(self.sw.evaluate("chrome.action.getBadgeText({})"), "1")
        self.assertEqual(self.sw.evaluate("chrome.storage.local.get('notifiedRun').then(r => r.notifiedRun)"),
                         "2026-09-30T08:00:00.000001")
        self.sw.evaluate("checkNewRoles()")  # the same search is never announced twice
        self.assertEqual(self.sw.evaluate("chrome.storage.local.get('notifiedRun').then(r => r.notifiedRun)"),
                         "2026-09-30T08:00:00.000001")

    def test_1h_a_thank_you_page_marks_the_posting_applied(self):
        import httpx

        api = f"http://127.0.0.1:{self.api_port}"
        hdr = {"X-Tailor-Token": TOKEN}
        pid = httpx.post(f"{api}/api/desk/postings", headers=hdr, json={
            "url": f"http://127.0.0.1:{self.site_port}/apply", "title": "Data Analyst", "source": "text",
            "text": "Data Analyst\n\nMinimum qualifications: SQL and dashboards. " * 20}).json()["id"]
        page = self.ctx.new_page()
        page.goto(f"http://127.0.0.1:{self.site_port}/thanks")
        for _ in range(40):
            page.wait_for_timeout(250)
            if httpx.get(f"{api}/api/desk/postings/{pid}", headers=hdr).json()["stage"] == "applied":
                break
        self.assertEqual(httpx.get(f"{api}/api/desk/postings/{pid}", headers=hdr).json()["stage"], "applied")
        page.close()

    def test_1i_postings_due_soon_get_one_reminder_a_day(self):
        import httpx
        from datetime import date, timedelta

        api = f"http://127.0.0.1:{self.api_port}"
        soon = (date.today() + timedelta(days=2)).strftime("%B %d, %Y")
        httpx.post(f"{api}/api/desk/postings", headers={"X-Tailor-Token": TOKEN}, json={
            "title": "Strategy Associate", "source": "text",
            "text": f"Strategy Associate\n\nPlease apply by {soon}.\n\nMinimum qualifications: analysis. " * 10})
        self.sw.evaluate("chrome.storage.local.remove('remindedOn')")
        self.sw.evaluate("checkDeadlines()")
        today = date.today().isoformat()
        stamp = self.sw.evaluate("chrome.storage.local.get('remindedOn').then(r => r.remindedOn)")
        # the extension stamps its own local date; allow for a test run that crosses midnight in UTC
        self.assertIn(stamp, {today, (date.today() - timedelta(days=1)).isoformat(), (date.today() + timedelta(days=1)).isoformat()})

    def test_1j_workday_list_buttons_are_filled_from_my_info_and_nothing_else_is_pressed(self):
        import httpx

        api = f"http://127.0.0.1:{self.api_port}"
        hdr = {"X-Tailor-Token": TOKEN}
        httpx.put(f"{api}/api/desk/me", headers=hdr, json={"address": "1 Main St", "state": "Michigan",
                                                            "phone_type": "Mobile", "how_heard": "LinkedIn"})
        try:
            page = self.ctx.new_page()
            page.goto(f"http://127.0.0.1:{self.site_port}/workday")
            page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
            self.click_fill(page)
            txt = lambda i: page.evaluate("id => document.getElementById(id).textContent", i)  # noqa: E731
            self.assertEqual(page.evaluate("document.getElementById('fn').value"), "Jane")
            self.assertEqual(page.evaluate("document.getElementById('a1').value"), "1 Main St")
            self.assertEqual((txt("st"), txt("pt"), txt("hh")), ("Michigan", "Mobile", "LinkedIn"))
            self.assertEqual(txt("gd"), "Select One")  # self-ID: only from a saved answer, never guessed
            pressed = page.evaluate("window.__pressed")
            self.assertNotIn("trap", pressed)  # a list button that says "Next" is never pressed
            self.assertNotIn("save", pressed)
            self.assertLessEqual(set(pressed), {"st", "pt", "hh"})
            page.close()
        finally:
            httpx.put(f"{api}/api/desk/me", headers=hdr, json={})

    def test_1l_fills_by_itself_when_the_person_turned_that_on_and_never_submits(self):
        self.sw.evaluate("chrome.storage.local.set({autoFill: true, filledPages: {}})")
        try:
            page = self.ctx.new_page()
            page.goto(f"http://127.0.0.1:{self.site_port}/apply")
            page.wait_for_function("() => document.getElementById('first_name').value === 'Jane'", timeout=20000)
            self.assertEqual(page.evaluate("window.__submits"), 0)
            page.close()
            # the same page opened again (Chrome reloads a tab it put to sleep): not filled again on its own
            page = self.ctx.new_page()
            page.goto(f"http://127.0.0.1:{self.site_port}/apply")
            try:
                page.wait_for_function("() => /Filled earlier/.test(document.querySelector('#tailor-panel-host').shadowRoot.textContent)", timeout=20000)
            except Exception:
                Path("/tmp/e2e_1l.txt").write_text(page.evaluate("() => (document.querySelector('#tailor-panel-host') ? document.querySelector('#tailor-panel-host').shadowRoot.getElementById('status').textContent : 'no panel') + ' | first=' + document.getElementById('first_name').value"))
                raise
            self.assertEqual(page.evaluate("document.getElementById('first_name').value"), "")
            page.close()
        finally:
            self.sw.evaluate("chrome.storage.local.set({autoFill: false})")

    def test_1k_apply_from_the_desk_fills_only_with_a_real_one_time_code(self):
        import httpx

        api = f"http://127.0.0.1:{self.api_port}"
        hdr = {"X-Tailor-Token": TOKEN}
        pid = httpx.post(f"{api}/api/desk/postings", headers=hdr, json={
            "url": f"http://127.0.0.1:{self.site_port}/apply?job=autofill", "title": "Data Analyst", "source": "text",
            "text": "Data Analyst\n\nMinimum qualifications: SQL and dashboards. " * 20}).json()["id"]
        url = httpx.post(f"{api}/api/desk/postings/{pid}/open", headers=hdr).json()["url"]
        self.assertRegex(url, r"#tailor-fill=[\w-]{8,}$")
        self.assertEqual(httpx.get(f"{api}/api/desk/postings/{pid}", headers=hdr).json()["stage"], "applying")

        def first_name_after(link):
            page = self.ctx.new_page()
            page.goto(link)
            page.wait_for_timeout(3500)
            v = page.evaluate("document.getElementById('first_name').value")
            submits = page.evaluate("window.__submits")
            page.close()
            return v, submits

        self.assertEqual(first_name_after(url), ("Jane", 0))  # filled with no click, never submitted
        self.assertEqual(first_name_after(url)[0], "")  # the same code twice: nothing
        self.assertEqual(first_name_after(f"http://127.0.0.1:{self.site_port}/apply#tailor-fill=madeUpCode123")[0], "")

    def test_2_wrong_token_is_reported_not_swallowed(self):
        self.set_config("wrong-token")
        try:
            page = self.open_form()
            self.click_fill(page)
            self.assertIn("token", self.status(page))
            self.assertEqual(page.evaluate("document.getElementById('first_name').value"), "")
            # once the token is fixed, the next Fill tries again instead of reusing the failure
            self.set_config(TOKEN)
            self.click_fill(page)
            self.assertNotIn("token", self.status(page))
            self.assertNotIn("No access", page.evaluate(
                "document.querySelector('#tailor-panel-host').shadowRoot.getElementById('tailornote').textContent"))
            self.assertEqual(page.evaluate("document.getElementById('first_name').value"), "Jane")
            self.assertEqual(page.evaluate("window.__submits"), 0)
            page.close()
        finally:
            self.set_config(TOKEN)

    def test_3_options_page_loads(self):
        ext_id = re.match(r"chrome-extension://([a-p]{32})/", self.sw.url).group(1)
        page = self.ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"chrome-extension://{ext_id}/options.html")
        self.assertIn("Alert sounds", page.inner_text("body"))
        self.assertEqual(page.input_value("#token"), TOKEN)
        self.assertEqual(errors, [])
        page.close()


if __name__ == "__main__":
    unittest.main()

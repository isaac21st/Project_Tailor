"""A multi-step application on a site Project Tailor has no rules for, end to end (the person, 2026-10-06: "navigate
throughout multiple pages on its own ... and finally stop at review"). Four steps: My Information, Experience (one
school and one job to start, "+ Add education" / "+ Add experience"), Questions (sponsorship, authorization, veteran,
disability), Review (only "Submit application"). Run twice: as a single-page app, and with each step as its own page.
It must fill every step, go on by itself, stop at Review, and never press Submit, Back or Save draft.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402
from ext_helpers import start_or_clean_up  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from test_workday_experience_e2e import PROFILE, free_port  # noqa: E402
from tailor.config import Settings, apply_environment, load_settings  # noqa: E402
from tailor.llm import StubClient  # noqa: E402
from tailor.server.app import create_app  # noqa: E402

EXT = Path(__file__).resolve().parent.parent / "extension"
TOKEN = "multistep-e2e-token"
HOST = "jobs.acme-steps.test"
STANDING = {"disability": "Yes", "veteran": "I am not a protected veteran", "sponsorship_us": "No",
            "work_authorization_us": "Yes", "us_citizen": "Yes", "us_answers_for_us_roles": True}

STEPS = ["My Information", "Experience", "Application Questions", "Review"]
STEP_HTML = {
    0: """<h2>My Information</h2>
      <div><label for="fn">First Name *</label><input id="fn" name="first_name" required></div>
      <div><label for="ln">Last Name *</label><input id="ln" name="last_name" required></div>
      <div><label for="em">Email *</label><input id="em" name="email" type="email" required></div>
      <div><label for="ph">Phone *</label><input id="ph" name="phone" type="tel" required></div>""",
    1: """<h2>Experience</h2>
      <section><h3>Education</h3><div id="edu"></div><button type="button" id="add-edu">+ Add education</button></section>
      <section><h3>Work Experience</h3><div id="work"></div><button type="button" id="add-work">+ Add experience</button></section>""",
    2: """<h2>Application Questions</h2>
      <div><label for="q1">Will you now or in the future require sponsorship for employment visa status (e.g., H-1B)? *</label>
        <select id="q1" required><option value="">Select</option><option>Yes</option><option>No</option></select></div>
      <div><label for="q2">Are you legally authorized to work in the United States? *</label>
        <select id="q2" required><option value="">Select</option><option>Yes</option><option>No</option></select></div>
      <div><label for="q3">Veteran Status *</label><select id="q3" required><option value="">Select</option>
        <option>I identify as one or more of the classifications of protected veteran</option>
        <option>I am not a protected veteran</option><option>I don't wish to answer</option></select></div>
      <fieldset><legend>Please check one of the boxes below: *</legend>
        <label><input type="radio" name="dis" value="yes"> Yes, I have a disability (or previously had a disability)</label>
        <label><input type="radio" name="dis" value="no"> No, I do not have a disability and have not had one in the past</label>
        <label><input type="radio" name="dis" value="na"> I do not want to answer</label></fieldset>""",
    3: """<h2>Review your application</h2><p>Check everything before you submit.</p>
      <div id="summary"></div>""",
}
SCRIPT = """
window.__pressed = JSON.parse(sessionStorage.getItem('pressed') || '[]');
const press = (id) => { window.__pressed.push(id); sessionStorage.setItem('pressed', JSON.stringify(window.__pressed)); };
const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const monthSel = () => `<select><option value="">Month</option>` + MONTHS.map((m) => `<option>${m}</option>`).join("") + `</select>`;
let n = 0;
function addSchool() { const i = n++, d = document.createElement("div"); d.className = "entry";
  d.innerHTML = `<div><label for="s${i}">School</label><input id="s${i}"></div><div><label for="d${i}">Degree</label><input id="d${i}"></div>` +
    `<div><label>Start date</label>${monthSel()}<input aria-label="Start year"></div><div><label>End date</label>${monthSel()}<input aria-label="End year"></div>`;
  document.getElementById("edu").appendChild(d); }
function addJob() { const i = n++, d = document.createElement("div"); d.className = "entry";
  d.innerHTML = `<div><label for="c${i}">Company</label><input id="c${i}"></div><div><label for="t${i}">Job title</label><input id="t${i}"></div>`;
  document.getElementById("work").appendChild(d); }
function saveStep() {  // the site keeps what was entered (sessionStorage stands in for its server)
  const vals = JSON.parse(sessionStorage.getItem('vals') || '{}');
  document.querySelectorAll('#step input, #step select').forEach((el, i) => {
    const k = (el.id || el.name || 'x' + i) + (el.type === 'radio' ? ':' + el.value : '');
    vals[k] = el.type === 'radio' || el.type === 'checkbox' ? el.checked : el.value; });
  vals['edu_count'] = vals['edu_count'] || document.querySelectorAll('#edu .entry').length;
  vals['work_count'] = vals['work_count'] || document.querySelectorAll('#work .entry').length;
  if (document.querySelector('#edu')) {
    vals['edu'] = [...document.querySelectorAll('#edu .entry')].map((e) => [...e.querySelectorAll('input, select')].map((x) => x.value).join(' | '));
    vals['work'] = [...document.querySelectorAll('#work .entry')].map((e) => [...e.querySelectorAll('input, select')].map((x) => x.value).join(' | '));
  }
  sessionStorage.setItem('vals', JSON.stringify(vals)); }
function render(k) {
  document.getElementById("step").innerHTML = STEP_HTML[k];
  document.getElementById("progress").innerHTML = STEPS.map((s, i) => `<li class="step${i === k ? ' active' : ''}"${i === k ? ' aria-current="step"' : ''}>${s}</li>`).join("");
  (document.getElementById("next") || document.querySelector("x-next")).style.display = k < 3 ? "" : "none";
  if (k === 2 && EXTRA) document.getElementById("step").insertAdjacentHTML("beforeend", EXTRA);
  document.getElementById("submit").style.display = k === 3 ? "" : "none";
  if (k === 1) { addSchool(); addJob(); }
}
document.addEventListener("click", (e) => {
  const b = e.composedPath().find((n) => n.tagName === "BUTTON" || (n.tagName === "INPUT" && n.type === "button")); if (!b) return;
  press(b.id || b.className);
  if (b.id === "add-edu") setTimeout(addSchool, 150);
  if (b.id === "add-work") setTimeout(addJob, 150);
  if (b.id === "submit") window.__submitted = true;
  if (b.id === "next") {
    const missing = [...document.querySelectorAll('#step [required]')].filter((x) => x.type === 'radio' ? false : !x.value);
    const radios = document.querySelectorAll('#step input[type=radio]');
    if (missing.length || (radios.length && ![...radios].some((r) => r.checked))) { document.getElementById("err").textContent = "Please complete the required fields."; return; }
    saveStep();
    const k = CURRENT + 1;
    if (MULTIPAGE) location.href = "/apply/step" + k; else { CURRENT = k; render(k); }
  }
});
render(CURRENT);
"""


# a written question Tailor drafts: the draft goes on the review list and the flow goes on (2026-10-13)
EXTRA = """<div><label for="q6">Why do you want to work at Acme Steps?</label><textarea id="q6"></textarea></div>"""
# a Next button inside a web component (SmartRecruiters' spl-button, 2026-10-13)
SHADOW_NEXT = """customElements.define("x-next", class extends HTMLElement { constructor() { super();
  this.attachShadow({ mode: "open" }).innerHTML = '<button type="button" id="next">Next</button>'; } });"""


def page(k: int, multipage: bool, input_buttons: bool = False, shadow: bool = False) -> str:
    return f"""<!doctype html><html><head><title>Apply - Acme Steps</title></head><body>
<ol id="progress"></ol><div id="err" role="alert"></div>
<form id="app" onsubmit="window.__submitted = true; return false;"><div id="step"></div>
<div class="footer"><button type="button" id="back">Back</button><button type="button" id="save">Save draft</button>
{'<input type="button" id="next" value="Save and Continue">' if input_buttons else '<x-next></x-next>' if shadow else '<button type="button" id="next">Next</button>'}<button type="button" id="submit">Submit application</button></div></form>
<script>const STEPS = {json.dumps(STEPS)}; const STEP_HTML = {json.dumps(STEP_HTML)}; let CURRENT = {k};
const MULTIPAGE = {str(multipage).lower()}; const EXTRA = {json.dumps(EXTRA if shadow else "")};
{SHADOW_NEXT if shadow else ""}
{SCRIPT}</script></body></html>"""


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        multipage = self.path.startswith("/apply/step")
        k = int(self.path.rsplit("step", 1)[1]) if multipage else 0
        body = page(k, multipage or self.path.startswith("/multi"), self.path.startswith("/taleo"), self.path.startswith("/shadow")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class MultiStepE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        settings = Settings(data_dir=tmp / "data")
        settings.ensure_dirs()
        (settings.profile_dir / "standing_answers.json").write_text(json.dumps(STANDING), encoding="utf-8")
        pdf = settings.output_dir / "current_resume.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% e2e test resume\n")
        cls.api_port, cls.site_port = free_port(), free_port()
        app = create_app(settings, StubClient("Fine."), PROFILE, TOKEN, pdf)
        cls.api = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.api_port, log_level="error"))
        threading.Thread(target=cls.api.run, daemon=True).start()
        cls.site = http.server.ThreadingHTTPServer(("127.0.0.1", cls.site_port), _Site)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        while not cls.api.started:
            pass
        ext = tmp / "ext"
        shutil.copytree(EXT, ext)
        manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
        manifest["host_permissions"].append(f"http://127.0.0.1:{cls.api_port}/*")
        manifest["content_scripts"][0]["matches"].append(f"http://{HOST}/*")
        (ext / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        apply_environment(load_settings())
        cls.pw = sync_playwright().start()
        try:
            cls.ctx = cls.pw.chromium.launch_persistent_context(
                str(tmp / "profile"), channel="chromium", headless=True,
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}",
                      f"--host-resolver-rules=MAP {HOST} 127.0.0.1"])
        except Exception:
            cls.pw.stop()
            cls.api.should_exit = True
            cls.site.shutdown()
            cls.tmp.cleanup()
            raise
        cls.sw = start_or_clean_up(cls, cls.ctx, cls.pw, lambda: (setattr(cls.api, "should_exit", True), cls.site.shutdown(), cls.tmp.cleanup()))
        cls.sw.evaluate("cfg => chrome.storage.local.set(cfg)", {"backendUrl": f"http://127.0.0.1:{cls.api_port}", "token": TOKEN})

    @classmethod
    def tearDownClass(cls):
        cls.ctx.close()
        cls.pw.stop()
        cls.api.should_exit = True
        cls.site.shutdown()
        cls.tmp.cleanup()

    def walk(self, start: str):
        self.sw.evaluate("() => chrome.storage.local.remove('autoFlow')")
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}{start}")
        # the person presses Fill once (4 boxes is too few to fill on open); every later step happens by itself
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        page.evaluate("() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('fill').click()")
        if os.environ.get("MS_DEBUG"):  # a timeline of the panel and the page, for working on this test
            import time
            t0, last = time.time(), None
            while time.time() - t0 < float(os.environ["MS_DEBUG"]):
                try:
                    cur = page.evaluate("""() => (document.querySelector('#tailor-panel-host')?.shadowRoot?.getElementById('status')?.textContent || '-').slice(0, 140)
                        + ' || ' + ((document.querySelector('[aria-current=step]') || {}).textContent) + ' | err=' + document.getElementById('err').textContent
                        + ' | pressed=' + sessionStorage.getItem('pressed')
                        + ' | needs=' + (document.querySelector('#tailor-panel-host')?.shadowRoot?.getElementById('needs')?.innerText || '').slice(0, 200)""")
                except Exception as e:  # mid-navigation
                    cur = "(navigating) " + str(e)[:40]
                if cur != last:
                    print(f"{time.time() - t0:5.1f}s {cur}", flush=True)
                    last = cur
                page.wait_for_timeout(400)
        # fill on open, then on to the next step by itself (3 s countdowns), until Review
        page.wait_for_function("() => /Review your application/.test(document.body.innerText) && "
                               "/Ready for your review/.test(document.querySelector('#tailor-panel-host')?.shadowRoot?.getElementById('status')?.textContent || '')",
                               timeout=90000)
        page.wait_for_timeout(1500)
        got = page.evaluate("""() => ({ vals: JSON.parse(sessionStorage.getItem('vals') || '{}'),
                                        pressed: JSON.parse(sessionStorage.getItem('pressed') || '[]'), submitted: !!window.__submitted,
                                        review: document.querySelector('#tailor-panel-host').shadowRoot.getElementById('reviewlist')?.innerText || '' })""")
        page.close()
        return got

    def check(self, got):
        v = got["vals"]
        self.assertEqual((v["fn"], v["ln"], v["em"]), ("Jane", "Doe", "jane@example.com"))
        self.assertEqual(len(v["edu"]), 3)  # three schools
        self.assertTrue(v["edu"][0].startswith("University of Michigan, Harbor School of Business | Master of Business Administration | August | 2025"), v["edu"])
        self.assertTrue(v["edu"][2].startswith("University of California, Davis | Bachelor of Arts | September | 2018"), v["edu"])
        self.assertEqual([w.split(" | ")[0] for w in v["work"]], ["University of Michigan", "Summit Club", "Acme Rental Group", "Acme Rental Group"])
        self.assertEqual((v["q1"], v["q2"], v["q3"]), ("No", "Yes", "I am not a protected veteran"))
        self.assertTrue(v["dis:yes"])
        self.assertFalse(v["dis:no"] or v["dis:na"])
        self.assertEqual(got["pressed"].count("next"), 3)  # three steps on, then stopped at Review
        for never in ("submit", "back", "save"):
            self.assertNotIn(never, got["pressed"])
        self.assertFalse(got["submitted"])

    def test_single_page_app(self):
        self.check(self.walk("/apply"))

    def test_each_step_its_own_page(self):
        self.check(self.walk("/multi"))

    def test_an_input_step_button(self):  # Taleo: <input type="button" value="Save and Continue">
        self.check(self.walk("/taleo"))

    def test_a_shadow_dom_next_and_a_draft_left_for_the_review(self):
        got = self.walk("/shadow")
        self.check(got)
        self.assertIn("Drafted answer: Why do you want to work at Acme Steps?", got["review"])  # listed at the end, before Submit


if __name__ == "__main__":
    unittest.main()

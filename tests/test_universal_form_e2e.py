"""A site Project Tailor has no rules for, end to end (the person's goal, 2026-10-06: one way to fill applications on
every site). The page uses its own words: "Academic background" and "Where you've worked" (no known section title),
"+ Add school" and a bare "Add another" button, Yes/No answer buttons with a pressed state, unlabeled month dropdowns.
It must get one school entry per degree and one work entry per resume job, in order, and never press "Save draft",
"Remove" or "Submit application".
"""
from __future__ import annotations

import http.server
import json
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
TOKEN = "universal-e2e-token"
HOST = "careers.acme-widgets.test"  # no site rules match this name

PAGE = """<!doctype html><html><head><title>Apply: Associate Product Manager | Acme Widgets</title></head><body>
<form id="app" onsubmit="window.__submitted = true; return false;">
  <div class="row"><label for="fn">First name *</label><input id="fn"></div>
  <div class="row"><label for="ln">Last name *</label><input id="ln"></div>
  <div class="row"><label for="em">Email *</label><input id="em" type="email"></div>
  <div class="q"><span class="title">Have you attended a university or college? *</span>
    <div class="opts"><button type="button" aria-pressed="false" class="opt">Yes</button><button type="button" aria-pressed="false" class="opt">No</button></div></div>
  <section><h3>Academic background</h3><div id="edu"></div>
    <button type="button" id="add-edu">+ Add school</button></section>
  <section><h3>Where you've worked</h3><div id="work"></div>
    <button type="button" id="add-work">Add another</button></section>
  <button type="button" id="save">Save draft</button>
  <button type="submit" id="submit">Submit application</button>
</form>
<script>
window.__pressed = [];
const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const monthSel = () => `<select><option value="">Month</option>` + MONTHS.map((m) => `<option>${m}</option>`).join("") + `</select>`;
let n = 0;
function addSchool() {
  const i = n++, d = document.createElement("div"); d.className = "entry";
  d.innerHTML = `<div><label for="s${i}">School</label><input id="s${i}"></div>` +
    `<div><label for="d${i}">Degree</label><input id="d${i}"></div>` +
    `<div><label for="m${i}">Major</label><input id="m${i}"></div>` +
    `<div><label>Start date</label>${monthSel()}<input class="yr" aria-label="Start year"></div>` +
    `<div><label>End date</label>${monthSel()}<input class="yr" aria-label="End year"></div>` +
    `<button type="button" class="rm">Remove</button>`;
  document.getElementById("edu").appendChild(d);
}
function addJob() {
  const i = n++, d = document.createElement("div"); d.className = "entry";
  d.innerHTML = `<div><label for="c${i}">Company</label><input id="c${i}"></div>` +
    `<div><label for="t${i}">Job title</label><input id="t${i}"></div>` +
    `<div><label>Start date</label>${monthSel()}<input class="yr" aria-label="Start year"></div>` +
    `<div><label>End date</label>${monthSel()}<input class="yr" aria-label="End year"></div>` +
    `<label><input type="checkbox" class="cur"> I currently work here</label>` +
    `<button type="button" class="rm">Remove</button>`;
  document.getElementById("work").appendChild(d);
}
addSchool(); addJob();
document.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  window.__pressed.push(b.id || b.className);
  if (b.id === "add-edu") setTimeout(addSchool, 150);
  if (b.id === "add-work") setTimeout(addJob, 150);
  if (b.classList.contains("opt")) b.parentElement.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
});
</script></body></html>"""


# A second layout (iCIMS / Oracle / SuccessFactors style): entries in fieldsets, boxes named only by aria-label,
# "Graduation Date" as MM/YYYY, month boxes for From/To, a "Current Position" box, "+ Add Another Education".
PAGE2 = """<!doctype html><html><head><title>Careers - Apply</title></head><body>
<form id="app" onsubmit="window.__submitted = true; return false;">
  <input aria-label="First Name" id="fn"><input aria-label="Last Name" id="ln"><input aria-label="Email Address" type="email" id="em">
  <div id="edu"></div><button type="button" id="add-edu">+ Add Another Education</button>
  <div id="work"></div><button type="button" id="add-work">Add Work Experience</button>
  <button type="button" id="submit">Submit</button>
</form>
<script>
window.__pressed = [];
let e = 0, w = 0;
function addSchool() { e++; const f = document.createElement("fieldset"); f.className = "entry";
  f.innerHTML = `<legend>Education ${e}</legend><input aria-label="Institution"><input aria-label="Degree"><input aria-label="Major">` +
    `<input aria-label="Graduation Date" placeholder="MM/YYYY">`; document.getElementById("edu").appendChild(f); }
function addJob() { w++; const f = document.createElement("fieldset"); f.className = "entry";
  f.innerHTML = `<legend>Work Experience ${w}</legend><input aria-label="Employer"><input aria-label="Position Title">` +
    `<input aria-label="From" type="month"><input aria-label="To" type="month">` +
    `<label><input type="checkbox" class="cur"> Current Position</label>`; document.getElementById("work").appendChild(f); }
addSchool(); addJob();
document.addEventListener("click", (ev) => { const b = ev.target.closest("button"); if (!b) return; window.__pressed.push(b.id);
  if (b.id === "add-edu") setTimeout(addSchool, 150); if (b.id === "add-work") setTimeout(addJob, 150); });
</script></body></html>"""


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (PAGE2 if self.path.startswith("/v2") else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class UniversalFormE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        settings = Settings(data_dir=tmp / "data")
        settings.ensure_dirs()
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
        sw = start_or_clean_up(cls, cls.ctx, cls.pw, lambda: (setattr(cls.api, "should_exit", True), cls.site.shutdown(), cls.tmp.cleanup()))
        sw.evaluate("cfg => chrome.storage.local.set(cfg)", {"backendUrl": f"http://127.0.0.1:{cls.api_port}", "token": TOKEN})

    @classmethod
    def tearDownClass(cls):
        cls.ctx.close()
        cls.pw.stop()
        cls.api.should_exit = True
        cls.site.shutdown()
        cls.tmp.cleanup()

    def test_entries_answers_and_never_submit(self):
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}/jobs/42/apply")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        page.evaluate("() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('fill').click()")
        page.wait_for_function("() => document.querySelectorAll('#edu .entry').length === 3 && document.querySelectorAll('#work .entry').length === 4"
                               " && [...document.querySelectorAll('.entry input:not([type=checkbox])')].filter(x => !x.value).length <= 1", timeout=45000)
        page.wait_for_timeout(1000)
        got = page.evaluate("""() => ({
          edu: [...document.querySelectorAll('#edu .entry')].map(e => [...e.querySelectorAll('input, select')].map(x => x.value).join(' | ')),
          work: [...document.querySelectorAll('#work .entry')].map(e => [...e.querySelectorAll('input, select')].map(x => x.type === 'checkbox' ? String(x.checked) : x.value).join(' | ')),
          attended: [...document.querySelectorAll('.opt')].filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.textContent),
          pressed: window.__pressed, submitted: !!window.__submitted })""")
        self.assertEqual(got["edu"], [
            "University of Michigan, Harbor School of Business | Master of Business Administration | Business Administration | August | 2025 | May | 2027",
            "University of Michigan School of Information | Master of Science | Information Technology | August | 2024 | May | 2027",
            "University of California, Davis | Bachelor of Arts | History | September | 2018 | June | 2021",
        ])  # the field of study the person picked for forms; PROFILE lists the MBA first
        self.assertEqual([w.split(" | ")[:2] for w in got["work"]], [
            ["University of Michigan", "Research Associate"], ["Summit Club", "Brand Strategy Associate"],
            ["Acme Rental Group", "Senior Sales Associate"], ["Acme Rental Group", "Sales Associate"]])  # jobs, not projects
        self.assertEqual(got["attended"], ["Yes"])
        self.assertEqual(got["pressed"].count("add-edu"), 2)
        self.assertEqual(got["pressed"].count("add-work"), 3)
        for never in ("save", "submit", "rm"):
            self.assertNotIn(never, got["pressed"])
        self.assertFalse(got["submitted"])
        page.close()

    def test_a_second_layout(self):
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}/v2/apply")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        page.evaluate("() => { const r = document.querySelector('#tailor-panel-host').shadowRoot;"
                      " r.getElementById('autonext').checked = false; r.getElementById('fill').click(); }")
        page.wait_for_function("() => document.querySelectorAll('#edu .entry').length === 3 && document.querySelectorAll('#work .entry').length === 4"
                               " && [...document.querySelectorAll('.entry input:not([type=checkbox])')].filter(x => !x.value).length <= 3", timeout=45000)
        page.wait_for_timeout(1000)
        got = page.evaluate("""() => ({
          edu: [...document.querySelectorAll('#edu .entry')].map(e => [...e.querySelectorAll('input')].map(x => x.value).join(' | ')),
          work: [...document.querySelectorAll('#work .entry')].map(e => [...e.querySelectorAll('input')].map(x => x.type === 'checkbox' ? String(x.checked) : x.value).join(' | ')),
          pressed: window.__pressed, submitted: !!window.__submitted })""")
        self.assertEqual(got["edu"][0], "University of Michigan, Harbor School of Business | Master of Business Administration | Business Administration | 05/2027")
        self.assertEqual(got["edu"][2], "University of California, Davis | Bachelor of Arts | History | 06/2021")
        self.assertEqual(got["work"][0], "University of Michigan | Research Associate | 2026-05 |  | true")
        self.assertEqual(got["work"][1], "Summit Club | Brand Strategy Associate | 2025-06 | 2025-09 | false")
        self.assertEqual((got["pressed"].count("add-edu"), got["pressed"].count("add-work")), (2, 3))
        self.assertNotIn("submit", got["pressed"])
        self.assertFalse(got["submitted"])
        page.close()


if __name__ == "__main__":
    unittest.main()

"""Workday's "Voluntary Disclosures" and "Self Identify" steps, end to end (the person, 2026-10-06: sponsorship none,
veteran none, disabled). Built like Workday's pages: answers in listbox buttons ("Select One"), the disability form as
three separate checkboxes, the date as month/day/year spin boxes, the terms checkbox (the person's to tick).
On a check-one list only the box that IS the answer may be ticked: ticking "Yes" into every box would also say "No".
"""
from __future__ import annotations

import http.server
import json
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date
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
TOKEN = "wd-selfid-token"
HOST = "acme.wd5.myworkdayjobs.com"
STANDING = {"disability": "Yes", "veteran": "I am not a protected veteran", "sponsorship_us": "No", "gender": "Female",
            "work_authorization_us": "Yes", "us_citizen": "Yes", "us_answers_for_us_roles": True}

PAGE = """<!doctype html><html><head><title>Self Identify - Workday</title></head><body>
<div data-automation-id="applyFlowPage">
<div data-automation-id="progressBarActiveStep">current step 4 of 5 Self Identify</div>
<div data-automation-id="jobPostingHeader">Product Manager</div><div data-automation-id="locations">New York, NY</div>
<h2>Voluntary Disclosures</h2>
<div data-automation-id="formField-veteranStatus"><label for="vet">Please select the veteran status which most accurately describes your status *</label>
  <button type="button" id="vet" aria-haspopup="listbox" aria-required="true">Select One</button></div>
<div data-automation-id="formField-gender"><label for="gen">Gender *</label>
  <button type="button" id="gen" aria-haspopup="listbox" aria-required="true">Select One</button></div>
<div data-automation-id="formField-sponsor"><label for="sp">Will you now or in the future require sponsorship for employment visa status? *</label>
  <button type="button" id="sp" aria-haspopup="listbox" aria-required="true">Select One</button></div>
<div data-automation-id="formField-agreementCheckbox"><input type="checkbox" id="terms">
  <label for="terms">I acknowledge that I have read and understand the terms and conditions</label></div>
<h2>Voluntary Self-Identification of Disability</h2>
<p>Please check one of the boxes below:</p>
<div data-automation-id="formField-name"><label for="nm">Name *</label><input id="nm"></div>
<fieldset data-automation-id="formField-dateSignedOn"><legend>Date *</legend>
  <input id="dt-dateSectionMonth-input" role="spinbutton" data-automation-id="dateSectionMonth-input" aria-label="Month">
  <input id="dt-dateSectionDay-input" role="spinbutton" data-automation-id="dateSectionDay-input" aria-label="Day">
  <input id="dt-dateSectionYear-input" role="spinbutton" data-automation-id="dateSectionYear-input" aria-label="Year"></fieldset>
<div data-automation-id="formField-employeeId"><label for="eid">Employee ID</label><input id="eid"></div>
<div data-automation-id="formField-disabilityStatus" role="group" aria-labelledby="dlab"><span id="dlab">Please check one of the boxes below: *</span>
  <div><input type="checkbox" id="d1"><label for="d1">Yes, I have a disability, or have had one in the past</label></div>
  <div><input type="checkbox" id="d2"><label for="d2">No, I do not have a disability and have not had one in the past</label></div>
  <div><input type="checkbox" id="d3"><label for="d3">I do not want to answer</label></div></div>
</div>
<button type="button" data-automation-id="pageFooterNextButton" id="next">Save and Continue</button>
<script>
window.__pressed = [];
const LISTS = { vet: ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
                      "I do not wish to self-identify"],
                gen: ["Female", "Male", "I do not wish to self-identify"], sp: ["Yes", "No"] };
document.addEventListener("input", (e) => {  // Workday's date parts take typing only
  const el = e.target; if (el.getAttribute("role") !== "spinbutton") return;
  if (e.inputType === "insertText") el.dataset.kept = el.value; else el.value = el.dataset.kept || "";
}, true);
document.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  window.__pressed.push(b.id);
  if (!LISTS[b.id]) return;
  document.querySelectorAll("[role=listbox]").forEach((l) => l.remove());
  const ul = document.createElement("ul"); ul.setAttribute("role", "listbox"); ul.id = b.id + "-list";
  b.setAttribute("aria-controls", ul.id);
  for (const o of LISTS[b.id]) {
    const li = document.createElement("li"); li.setAttribute("role", "option"); li.textContent = o;
    li.addEventListener("click", () => { b.textContent = o; ul.remove(); });
    ul.appendChild(li);
  }
  document.body.appendChild(ul);
});
</script></body></html>"""


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class WorkdaySelfIdE2E(unittest.TestCase):
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
        manifest["content_scripts"][0]["matches"].append("http://*.myworkdayjobs.com/*")
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

    def test_disclosures_and_self_identify(self):
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}/en-US/careers/job/apply")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        page.evaluate("() => { const r = document.querySelector('#tailor-panel-host').shadowRoot;"
                      " r.getElementById('autonext').checked = false; r.getElementById('fill').click(); }")
        page.wait_for_function("() => document.getElementById('nm').value && document.getElementById('vet').textContent !== 'Select One'"
                               " && document.getElementById('sp').textContent !== 'Select One'", timeout=30000)
        page.wait_for_timeout(1500)
        got = page.evaluate("""() => ({
          vet: document.getElementById('vet').textContent, gen: document.getElementById('gen').textContent,
          sp: document.getElementById('sp').textContent, name: document.getElementById('nm').value,
          date: ['Month', 'Day', 'Year'].map((p) => document.getElementById('dt-dateSection' + p + '-input').value),
          eid: document.getElementById('eid').value, terms: document.getElementById('terms').checked,
          dis: ['d1', 'd2', 'd3'].map((i) => document.getElementById(i).checked), pressed: window.__pressed })""")
        self.assertEqual(got["vet"], "I am not a protected veteran")
        self.assertEqual(got["gen"], "Female")
        self.assertEqual(got["sp"], "No")
        self.assertEqual(got["name"], "Jane Doe")
        today = date.today()
        self.assertEqual([int(x or 0) for x in got["date"]], [today.month, today.day, today.year])
        self.assertEqual(got["eid"], "")
        self.assertFalse(got["terms"])  # an attestation is the person's to tick
        self.assertEqual(got["dis"], [True, False, False])  # only the box that IS the answer
        self.assertNotIn("next", got["pressed"])
        page.close()


if __name__ == "__main__":
    unittest.main()

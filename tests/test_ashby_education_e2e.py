"""Ashby's "Education History", end to end: the real extension in Chromium, the real server, a copy of the section as
Solace's application showed it on 2026-10-06 (one entry to start, "+ Add Education", every entry with the SAME ids,
a "Search schools..." box, Degree and Field of Study text boxes, unlabeled Month/Year dropdowns under Start Date and
End Date, a "Still Student?" checkbox). The person's three degrees must each get an entry, in their order.
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
TOKEN = "ashby-e2e-token"
HOST = "acme-careers.test"  # mapped to 127.0.0.1 (jobs.ashbyhq.com itself is HTTPS-only): an Ashby form on a company site

PAGE = """<!doctype html><html><head><title>Associate Product Manager @ Acme</title></head><body>
<form class="ashby-application-form-container">
<div class="_fieldEntry ashby-application-form-field-entry" data-field-path="_systemfield_name">
  <label for="_systemfield_name">Name</label><input id="_systemfield_name" type="text"></div>
<div class="_fieldEntry ashby-application-form-field-entry" data-field-path="_systemfield_education_history">
  <label class="_heading _required" for="_systemfield_education_history">Education History</label>
  <div id="entries"></div>
  <button type="button" id="add-edu" class="_button _secondary">+ Add Education</button>
</div>
</form>
<script>
window.__pressed = [];
const P = "_systemfield_education_history";
const SCHOOLS = ["University of Michigan", "University of Michigan-Dearborn", "University of California, Davis",
                 "University of California, Berkeley"];
const months = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const sel = (first, opts) => `<div><select><option value="">${first}</option>` + opts.map((o) => `<option value="${o}">${o}</option>`).join("") + `</select></div>`;
const dates = (side, label) => `<div><label for="${P}-${side}">${label}</label><div id="${P}-${side}">` +
  sel("Month...", months) + sel("Year...", ["2027","2026","2025","2024","2023","2022","2021","2020","2019","2018","2017"]) + `</div></div>`;
function addEntry() {
  const d = document.createElement("div");
  d.className = "_repeatableEducationEntry_1e3gg_176 ashby-application-form-repeatable-entry";
  d.innerHTML = `<div><span class="_repeatableEducationLabel">Education</span><button type="button" class="del">Delete</button></div>` +
    `<div><div><label for="${P}-school">School</label><div><input placeholder="Search schools..." aria-autocomplete="list" ` +
    `aria-expanded="false" aria-haspopup="listbox" role="combobox" value=""><button type="button" class="open"></button></div></div>` +
    `<div><label for="${P}-degree">Degree</label><input id="${P}-degree" type="text" placeholder="e.g. Bachelor of Science"></div>` +
    `<div><label for="${P}-major">Field of Study</label><input id="${P}-major" type="text" placeholder="e.g. Computer Science"></div>` +
    `<div>` + dates("startDate", "Start Date") + dates("endDate", "End Date") +
    `<label for="${P}-isCurrent"><div><span><input type="checkbox" id="${P}-isCurrent"></span>Still Student?</div></label></div></div>`;
  document.getElementById("entries").appendChild(d);
}
// the school search: typing shows matching schools as options under the box; clicking one fills the box
document.addEventListener("input", (e) => {
  const box = e.target; if (box.getAttribute("role") !== "combobox") return;
  document.querySelectorAll(".results").forEach((l) => l.remove());
  const q = box.value.toLowerCase(); if (!q) return;
  const ul = document.createElement("ul"); ul.className = "results"; ul.setAttribute("role", "listbox"); ul.id = "lb" + Math.random().toString(36).slice(2);
  box.setAttribute("aria-controls", ul.id); box.setAttribute("aria-expanded", "true");
  for (const s of SCHOOLS.filter((x) => x.toLowerCase().includes(q))) {
    const li = document.createElement("li"); li.setAttribute("role", "option"); li.textContent = s;
    li.addEventListener("click", () => { box.value = s; ul.remove(); box.setAttribute("aria-expanded", "false"); });
    ul.appendChild(li);
  }
  box.parentElement.appendChild(ul);
});
addEntry();
document.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  window.__pressed.push(b.id || b.className);
  if (b.id === "add-edu") setTimeout(addEntry, 200);  // React re-renders a moment later
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


class AshbyEducationE2E(unittest.TestCase):
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

    def test_three_schools_in_the_persons_order(self):
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}/Acme/application")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        page.evaluate("() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('fill').click()")
        page.wait_for_function("() => { const e = document.querySelectorAll('[class*=repeatableEducationEntry]');"
                               " return e.length === 3 && [...e].every(x => x.querySelector('input[role=combobox]').value"
                               " && x.querySelectorAll('select')[3].value); }", timeout=40000)
        page.wait_for_timeout(800)
        got = page.evaluate("""() => [...document.querySelectorAll('[class*=repeatableEducationEntry]')].map(e => {
            const s = [...e.querySelectorAll('select')].map(x => x.value);
            return [e.querySelector('input[role=combobox]').value, e.querySelector('[id$=-degree]').value,
                    e.querySelector('[id$=-major]').value, s.join(' '), e.querySelector('[type=checkbox]').checked]; })""")
        self.assertEqual(got, [
            ["University of Michigan", "Master of Business Administration", "Business Administration", "August 2025 May 2027", True],
            ["University of Michigan", "Master of Science", "Information Technology", "August 2024 May 2027", True],
            ["University of California, Davis", "Bachelor of Arts", "History", "September 2018 June 2021", False],
        ])  # PROFILE lists the MBA first: the entries follow the profile's order
        pressed = page.evaluate("() => window.__pressed")
        self.assertEqual(pressed.count("add-edu"), 2)
        self.assertNotIn("del", pressed)  # Delete: never
        page.close()


if __name__ == "__main__":
    unittest.main()

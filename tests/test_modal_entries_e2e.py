"""Entries added through an editor (2026-10-07): on some Oracle, SuccessFactors and Avature sites, "Add Education"
opens a small dialog for ONE school with its own Save, and the page then shows the school as a line of text. Tailor
must add every degree and every job that way, in order, pressing only the section's Add and the editor's own Save,
never Cancel or Submit; an editor that refuses to save (a required box it cannot fill) stops it and goes to the person.
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
TOKEN = "modal-e2e-token"
HOST = "jobs.globex-careers.test"  # no site rules match this name

PAGE = """<!doctype html><html><head><title>Apply: Business Analyst | Globex</title>
<style>.err{color:red}</style></head><body>
<form id="app" onsubmit="window.__submitted = true; return false;">
  <div><label for="fn">First Name *</label><input id="fn"></div>
  <div><label for="ln">Last Name *</label><input id="ln"></div>
  <div><label for="em">Email *</label><input id="em" type="email"></div>
  <section><h3>Education</h3><ul id="edu"></ul><button type="button" id="add-edu">Add Education</button></section>
  <section><h3>Work Experience</h3><ul id="work"></ul><button type="button" id="add-work">Add</button></section>
  <button type="submit" id="submit">Submit application</button>
</form>
<div role="dialog" aria-modal="true" id="dlg" style="display:none;position:fixed;top:40px;left:40px;background:#fff;border:1px solid #999;padding:12px"></div>
<script>
window.__pressed = [];
const dlg = document.getElementById("dlg");
const STRICT = location.search.includes("strict");
function open(kind) {
  dlg.dataset.kind = kind;
  dlg.innerHTML = kind === "edu"
    ? `<h4>Add Education</h4><div><label for="x-school">School</label><input id="x-school"></div>
       <div><label for="x-degree">Degree</label><input id="x-degree"></div>
       <div><label for="x-major">Field of Study</label><input id="x-major"></div>
       ${STRICT ? '<div><label for="x-honors">Honors and awards *</label><input id="x-honors" required></div>' : ""}
       <p class="err" id="x-err"></p><button type="button" id="x-cancel">Cancel</button><button type="button" id="x-save">Save</button>`
    : `<h4>Add Work Experience</h4><div><label for="x-company">Company</label><input id="x-company"></div>
       <div><label for="x-title">Job Title</label><input id="x-title"></div>
       <p class="err" id="x-err"></p><button type="button" id="x-cancel">Cancel</button><button type="button" id="x-save">Save and Close</button>`;
  dlg.style.display = "block";
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  window.__pressed.push(b.id);
  if (b.id === "add-edu") setTimeout(() => open("edu"), 120);
  if (b.id === "add-work") setTimeout(() => open("work"), 120);
  if (b.id === "x-cancel") { dlg.style.display = "none"; dlg.innerHTML = ""; }
  if (b.id === "x-save") {
    const v = (id) => (document.getElementById(id) || {}).value || "";
    const edu = dlg.dataset.kind === "edu";
    const main = edu ? v("x-school") : v("x-company");
    if (!main || (STRICT && edu && !v("x-honors"))) { document.getElementById("x-err").textContent = "Please complete the required fields"; return; }
    const li = document.createElement("li");
    li.textContent = edu ? `${main} | ${v("x-degree")} | ${v("x-major")}` : `${main} | ${v("x-title")}`;
    document.getElementById(edu ? "edu" : "work").appendChild(li);
    setTimeout(() => { dlg.style.display = "none"; dlg.innerHTML = ""; }, 100);
  }
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


class ModalEntriesE2E(unittest.TestCase):
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

    def _fill(self, path):
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}{path}")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        page.evaluate("() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('fill').click()")
        return page

    def _state(self, page):
        return page.evaluate("""() => ({
          edu: [...document.querySelectorAll('#edu li')].map(x => x.textContent),
          work: [...document.querySelectorAll('#work li')].map(x => x.textContent),
          first: document.getElementById('fn').value, pressed: window.__pressed, submitted: !!window.__submitted,
          open: document.getElementById('dlg').style.display !== 'none',
          status: document.querySelector('#tailor-panel-host').shadowRoot.getElementById('status').textContent,
          needs: document.querySelector('#tailor-panel-host').shadowRoot.getElementById('needs').textContent })""")

    def test_every_degree_and_job_goes_in_through_the_editor(self):
        page = self._fill("/jobs/7/apply")
        page.wait_for_function("() => document.querySelectorAll('#edu li').length === 3 && document.querySelectorAll('#work li').length === 4"
                               " && document.getElementById('fn').value", timeout=60000)
        page.wait_for_timeout(800)
        got = self._state(page)
        self.assertEqual([x.split(" | ")[0] for x in got["edu"]], [
            "University of Michigan, Harbor School of Business", "University of Michigan School of Information",
            "University of California, Davis"])
        self.assertTrue(got["edu"][0].endswith("Master of Business Administration | Business Administration"), got["edu"][0])
        self.assertEqual(got["work"], ["University of Michigan | Research Associate", "Summit Club | Brand Strategy Associate",
                                       "Acme Rental Group | Senior Sales Associate", "Acme Rental Group | Sales Associate"])
        self.assertEqual(got["pressed"].count("add-edu"), 3)
        self.assertEqual(got["pressed"].count("add-work"), 4)
        self.assertEqual(got["pressed"].count("x-save"), 7)
        for never in ("x-cancel", "submit"):
            self.assertNotIn(never, got["pressed"])
        self.assertFalse(got["submitted"])
        self.assertEqual(got["first"], "Jane")
        page.close()

    def test_an_editor_that_will_not_save_goes_to_the_person(self):
        page = self._fill("/jobs/8/apply?strict=1")
        page.wait_for_function("() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('needs')"
                               ".textContent.includes('did not save')", timeout=60000)
        got = self._state(page)
        self.assertEqual(got["edu"], [])
        self.assertTrue(got["open"])  # left open for the person, with what they typed
        self.assertEqual(got["pressed"].count("add-edu"), 1)  # no second editor on top of the first
        self.assertNotIn("x-cancel", got["pressed"])
        self.assertFalse(got["submitted"])
        page.close()


if __name__ == "__main__":
    unittest.main()

"""SmartRecruiters' Easy apply as seen live (2026-10-14): the City box is a combobox whose list sits in the component
around it, its options are nested components with slotted words, and the two upload boxes have no labels: the
"apply with resume" parser must be left alone and the resume goes to the box marked resume-upload."""
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
TOKEN = "sr-e2e-token"
HOST = "jobs.sr-corp.test"

PAGE = """<!doctype html><html><head><title>Easy apply - Consultant - SR Corp</title></head><body>
<h2>Apply with your resume</h2>
<spl-dropzone data-test="apply-with-resume-container"></spl-dropzone>
<h2>Personal information</h2>
<x-input id="first-name-input" label="First name" required></x-input>
<x-input id="last-name-input" label="Last name" required></x-input>
<x-input id="email-input" label="Email" type="email" required></x-input>
<x-autocomplete></x-autocomplete>
<h2>Resume *</h2>
<spl-dropzone data-test="resume-upload"></spl-dropzone>
<button id="next" style="position:fixed;right:24px;bottom:24px;width:120px;height:40px">Next</button>
<script>
const PLACES = ["Los Gatos, CA, US", "Los Gatos Springs, NY, US", "Los Gatos Springs, UT, US", "Los Gatos, WY, US"];
class XInput extends HTMLElement {
  connectedCallback() {
    if (this.shadowRoot) return;
    const r = this.attachShadow({ mode: "open" });
    const id = this.id || "in-" + Math.random().toString(36).slice(2);
    const combo = this.hasAttribute("combobox");
    r.innerHTML = `<div><label for="${id}">${this.getAttribute("label")}${this.hasAttribute("required") ? "*" : ""}</label>
      <input id="${id}" type="${this.getAttribute("type") || "text"}" ${combo ? `role="combobox" aria-controls="${this.getAttribute("menu")}" aria-expanded="false"` : ""}></div>`;
    const i = r.querySelector("input");
    i.addEventListener("input", () => { this.value = i.value; this.dispatchEvent(new CustomEvent("splinput", { bubbles: true, composed: true, detail: i.value })); });
  }
}
customElements.define("x-input", XInput);
class XItem extends HTMLElement {  // SmartRecruiters' spl-dropdown-item: the words are slotted, the option is inside
  connectedCallback() {
    if (this.shadowRoot) return;
    this.attachShadow({ mode: "open" }).innerHTML = `<div role="option" aria-selected="false"><slot></slot></div>`;
    this.shadowRoot.querySelector("[role=option]").addEventListener("click", () =>
      this.dispatchEvent(new CustomEvent("pick", { bubbles: true, composed: true, detail: this.textContent.trim() })));
  }
}
customElements.define("x-item", XItem);
class XAutocomplete extends HTMLElement {  // spl-autocomplete: the box is an x-input; the list sits next to it
  connectedCallback() {
    const r = this.attachShadow({ mode: "open" });
    r.innerHTML = `<x-input label="City" combobox menu="menu-city"></x-input><div id="menu-city" role="listbox"></div>`;
    const menu = r.getElementById("menu-city");
    r.addEventListener("splinput", (e) => {
      const q = (e.detail || "").toLowerCase();
      const hits = !q ? [] : q.includes("california") ? PLACES.filter((p) => p.endsWith("CA, US"))
        : PLACES.filter((p) => p.toLowerCase().startsWith(q.split(",")[0].trim()));
      setTimeout(() => { menu.innerHTML = hits.map((h) => `<x-item>${h}</x-item>`).join(""); }, 150);
    });
    r.addEventListener("pick", (e) => {
      const box = r.querySelector("x-input").shadowRoot.querySelector("input");
      box.value = e.detail; window.__city = e.detail; menu.innerHTML = "";
    });
  }
}
customElements.define("x-autocomplete", XAutocomplete);
class SplDropzone extends HTMLElement {  // spl-dropzone: a file box with no label, known by data-test
  connectedCallback() {
    const r = this.attachShadow({ mode: "open" });
    r.innerHTML = `<div>Choose a file or drop it here <input type="file"></div>`;
    const i = r.querySelector("input");
    i.addEventListener("change", () => { (window.__files = window.__files || {})[this.getAttribute("data-test")] = [...i.files].map((f) => f.name); });
  }
}
customElements.define("spl-dropzone", SplDropzone);
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


class SmartRecruitersE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        settings = Settings(data_dir=tmp / "data")
        settings.ensure_dirs()
        pdf = settings.output_dir / "current_resume.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% e2e test resume\n")
        cls.api_port, cls.site_port = free_port(), free_port()
        app = create_app(settings, StubClient("Fine."), PROFILE.model_copy(update={"city": "Los Gatos", "state": "California"}), TOKEN, pdf)
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

    def test_city_is_picked_from_its_list_and_the_resume_goes_to_the_resume_box(self):
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}/apply")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        page.evaluate("() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('fill').click()")
        try:
            page.wait_for_function("() => window.__city && window.__files && window.__files['resume-upload']", timeout=45000)
        except Exception:
            Path("/tmp/sr_diag.json").write_text(page.evaluate("() => JSON.stringify({city: window.__city, files: window.__files, panel: document.querySelector('#tailor-panel-host').shadowRoot.textContent.replace(/\\s+/g, ' ').slice(0, 3000)})"))
            raise
        got = page.evaluate("() => ({ city: window.__city, files: window.__files, first: document.querySelector('#first-name-input').value })")
        self.assertEqual(got["city"], "Los Gatos, CA, US")      # "Los Gatos, California" picked from the list's own words
        self.assertEqual(got["first"], "Jane")
        self.assertEqual(len(got["files"]["resume-upload"]), 1)
        self.assertNotIn("apply-with-resume-container", got["files"])  # the parser box is never touched
        page.wait_for_function("() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('box').classList.contains('left')", timeout=8000)
        page.close()


if __name__ == "__main__":
    unittest.main()

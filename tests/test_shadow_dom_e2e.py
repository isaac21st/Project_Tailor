"""Boxes inside web components (SmartRecruiters' new Easy apply, 2026-10-08: every box is in an open shadow root and
the extension found "No fillable fields")."""
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
TOKEN = "shadow-e2e-token"
HOST = "apply.shadow-corp.test"

PAGE = """<!doctype html><html><head><title>Easy apply - Consultant - Shadow Corp</title></head><body>
<h2>Personal information</h2>
<x-input id="first-name-input" label="First name" required></x-input>
<x-input id="last-name-input" label="Last name" required></x-input>
<x-input id="email-input" label="Email" type="email" required></x-input>
<x-input id="confirm-email-input" label="Confirm your email" type="email" required></x-input>
<x-input id="phone" label="Phone number" type="tel"></x-input>
<h3>Preliminary questions</h3>
<x-field label="Legal First Name" qid="question_c314ebd9-69db-4b82-aa79-19c3a0487bbf"></x-field>
<div class="q"><div class="label">Legal Last Name *</div><x-field qid="question_0793a3ad-4188-426c-a5bc-94ca4aa2637e"></x-field></div>
<button id="next" disabled>Next</button>
<script>
class XInput extends HTMLElement {
  connectedCallback() {
    const r = this.attachShadow({ mode: "open" });
    const id = this.id;
    r.innerHTML = `<div><label for="${id}">${this.getAttribute("label")}${this.hasAttribute("required") ? "*" : ""}</label>
      <input id="${id}" type="${this.getAttribute("type") || "text"}"></div>`;
    const i = r.querySelector("input");
    i.addEventListener("input", () => { this.value = i.value; this.dispatchEvent(new Event("valuechange", { bubbles: true, composed: true })); });
  }
}
customElements.define("x-input", XInput);
class XField extends HTMLElement {  // SmartRecruiters' screening page: the box's id is an internal question id, no label
  connectedCallback() {
    const r = this.attachShadow({ mode: "open" });
    r.innerHTML = `<div class="wrap"><input id="${this.getAttribute("qid")}" name="${this.getAttribute("qid")}"></div>`;
  }
}
customElements.define("x-field", XField);
window.__changes = 0;
document.addEventListener("input", () => window.__changes++);  // the page hears composed events from inside the components
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


class ShadowDomE2E(unittest.TestCase):
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

    def test_boxes_inside_web_components_are_filled(self):
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}/apply")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        page.evaluate("() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('fill').click()")
        page.wait_for_function("() => document.querySelector('#email-input').value", timeout=30000)
        got = page.evaluate("""() => ({ first: document.querySelector('#first-name-input').value,
            last: document.querySelector('#last-name-input').value, email: document.querySelector('#email-input').value,
            confirm: document.querySelector('#confirm-email-input').value, heard: window.__changes,
            panelInputs: document.querySelector('#tailor-panel-host').shadowRoot.querySelector('#chatin').value })""")
        self.assertEqual((got["first"], got["last"], got["email"]), ("Jane", "Doe", "jane@example.com"))
        self.assertEqual(got["confirm"], "jane@example.com")
        legal = page.evaluate("""() => [...document.querySelectorAll('x-field')].map(h => h.shadowRoot.querySelector('input').value)""")
        self.assertEqual(legal, ["Jane", "Doe"])  # a label the component carries, and one written next to it
        self.assertGreater(got["heard"], 0)
        self.assertEqual(got["panelInputs"], "")  # Tailor never fills its own panel
        page.close()


if __name__ == "__main__":
    unittest.main()

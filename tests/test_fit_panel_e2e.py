"""The role-fit read in the panel, end to end: the real extension in Chromium and the real server, on a made-up person.
A job description with no form on it shows the read; so does a page with the form."""
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

from test_fit import RESUME  # noqa: E402
from test_workday_experience_e2e import PROFILE, free_port  # noqa: E402
from tailor.config import Settings, apply_environment, load_settings  # noqa: E402
from tailor.llm import StubClient  # noqa: E402
from tailor.server.app import create_app  # noqa: E402

EXT = Path(__file__).resolve().parent.parent / "extension"
TOKEN = "fit-e2e-token"
HOST = "acme-careers.test"
LINES = "".join(f"<li>{t}</li>" for t in (
    "Analyze hospital workflow data and diagnose clinical process bottlenecks for health system clients",
    "Present process improvement recommendations to client stakeholders and senior leaders",
    "Build operating models and staffing plans for healthcare service lines",
    "Coordinate cross-functional implementation across hospital operations teams"))
POSTING = (f"<h1>Healthcare Consultant</h1><p>{'We advise hospitals and health systems on clinical operations and healthcare strategy. ' * 12}</p>"
           f"<h2>Responsibilities</h2><ul>{LINES}</ul><h2>Qualifications</h2><ul>{LINES}<li>Master's degree in business or a related field</li></ul>")
PAGES = {"/job": f"<!doctype html><html><head><title>Healthcare Consultant</title></head><body>{POSTING}</body></html>",
         "/apply": f"<!doctype html><html><head><title>Healthcare Consultant</title></head><body>{POSTING}<form>"
                   + "".join(f'<label for="{n}">{n}</label><input id="{n}" name="{n}">' for n in ("First name", "Last name", "Email", "Phone"))
                   + "</form></body></html>"}
CAREER = {"tracks": [{"id": "health", "name": "Healthcare consulting", "priority": 1, "role_terms": ["healthcare consultant"],
                      "industry_terms": ["healthcare", "hospital", "clinical"], "positioning": "Lead with the hospital work."}],
          "entry_kinds": {"HOSPITAL CLIENT": "client_project"}}


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path, "<html></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class FitPanelE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        settings = Settings(data_dir=tmp / "data")
        settings.ensure_dirs()
        settings.profile_dir.mkdir(parents=True, exist_ok=True)
        (settings.profile_dir / "career_profile.json").write_text(json.dumps(CAREER), encoding="utf-8")
        pdf = settings.output_dir / "current_resume.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% e2e test resume\n")
        cls.api_port, cls.site_port = free_port(), free_port()
        app = create_app(settings, StubClient("Fine."), PROFILE, TOKEN, pdf, base_resume=RESUME)
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
        cls.ctx = cls.pw.chromium.launch_persistent_context(
            str(tmp / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}", f"--host-resolver-rules=MAP {HOST} 127.0.0.1"])
        sw = start_or_clean_up(cls, cls.ctx, cls.pw, lambda: (setattr(cls.api, "should_exit", True), cls.site.shutdown(), cls.tmp.cleanup()))
        sw.evaluate("cfg => chrome.storage.local.set(cfg)", {"backendUrl": f"http://127.0.0.1:{cls.api_port}", "token": TOKEN})

    @classmethod
    def tearDownClass(cls):
        cls.ctx.close()
        cls.pw.stop()
        cls.api.should_exit = True
        cls.site.shutdown()
        cls.tmp.cleanup()

    def read(self, path: str) -> str:
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}{path}")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=15000)
        page.wait_for_function("() => document.querySelector('#tailor-panel-host').shadowRoot.getElementById('fitbox').style.display === 'block'", timeout=20000)
        text = page.evaluate("() => { const r = document.querySelector('#tailor-panel-host').shadowRoot; return r.getElementById('fitsum').textContent + ' || ' + r.getElementById('fitbody').textContent; }")
        page.close()
        return text

    def test_a_job_description_with_no_form_shows_the_read(self):
        text = self.read("/job")
        self.assertRegex(text, r"Role fit: \d+/100")
        self.assertIn("Healthcare consulting", text)
        self.assertIn("Lead with the hospital work.", text)

    def test_a_page_with_the_form_shows_it_too(self):
        self.assertRegex(self.read("/apply"), r"Role fit: \d+/100")


if __name__ == "__main__":
    unittest.main()

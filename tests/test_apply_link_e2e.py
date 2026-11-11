"""From the Desk to the form by itself (2026-11-03): a job page's own Apply link is followed, the application on the
next page is filled, and a sign-in page gets nothing typed into it."""
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

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from ext_helpers import start_or_clean_up  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from test_workday_experience_e2e import PROFILE, free_port  # noqa: E402
from tailor.config import Settings, apply_environment, load_settings  # noqa: E402
from tailor.llm import StubClient  # noqa: E402
from tailor.server.app import create_app  # noqa: E402

EXT = Path(__file__).resolve().parent.parent / "extension"
TOKEN = "apply-link-token"
HOST = "jobs.acme-apply.test"
POSTING = "Product Manager. Requirements: product strategy, SQL, user research, roadmaps. " * 12
JOB = f"<!doctype html><title>Product Manager</title><h1>Product Manager</h1><p>{POSTING}</p><a href='/form' id='apply'>Apply</a>"
FORM = """<!doctype html><title>Apply</title><h1>Application</h1>
<label for=fn>First name</label><input id=fn><label for=ln>Last name</label><input id=ln>
<label for=em>Email</label><input id=em type=email><label for=ph>Phone</label><input id=ph type=tel>"""
SIGNIN = """<!doctype html><title>Create Account</title><label for=em>Email Address</label><input id=em type=email>
<label for=pw>Password</label><input id=pw type=password><label for=pw2>Verify Password</label><input id=pw2 type=password>
<label for=fn>First name</label><input id=fn>"""


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"/job": JOB, "/form": FORM, "/signin": SIGNIN}.get(self.path.split("?")[0], "nope").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ApplyLinkE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        settings = Settings(data_dir=tmp / "data")
        settings.ensure_dirs()
        pdf = settings.output_dir / "current_resume.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% e2e\n")
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
        cls.ctx = cls.pw.chromium.launch_persistent_context(
            str(tmp / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}", f"--host-resolver-rules=MAP {HOST} 127.0.0.1"])
        cls.sw = start_or_clean_up(cls, cls.ctx, cls.pw, lambda: (setattr(cls.api, "should_exit", True), cls.site.shutdown(), cls.tmp.cleanup()))
        cls.sw.evaluate("cfg => chrome.storage.local.set(cfg)", {"backendUrl": f"http://127.0.0.1:{cls.api_port}", "token": TOKEN})

    @classmethod
    def tearDownClass(cls):
        cls.ctx.close()
        cls.pw.stop()
        cls.api.should_exit = True
        cls.site.shutdown()
        cls.tmp.cleanup()

    def api_post(self, path, **body):
        return httpx.post(f"http://127.0.0.1:{self.api_port}{path}", json=body, headers={"X-Tailor-Token": TOKEN}, timeout=60).json()

    def status(self, page):
        return page.evaluate("document.querySelector('#tailor-panel-host')?.shadowRoot?.getElementById('status')?.textContent || ''")

    def test_the_jobs_apply_link_leads_to_a_filled_form(self):
        url = f"http://{HOST}:{self.site_port}/job"
        pid = self.api_post("/api/desk/postings", text=POSTING, url=url, source="text")["id"]
        opened = self.api_post(f"/api/desk/postings/{pid}/open")["url"]
        page = self.ctx.new_page()
        page.goto(opened)
        page.wait_for_url("**/form", timeout=40000)  # up to 20 s looking for a form, then the page's own Apply link
        page.wait_for_function("() => document.getElementById('fn')?.value", timeout=40000)
        self.assertEqual(page.evaluate("document.getElementById('em').value"), "jane@example.com")
        page.close()

    def test_a_sign_in_page_gets_nothing(self):
        self.sw.evaluate("h => chrome.storage.local.set({ autoFlow: { host: h, at: Date.now(), steps: 0 } })", HOST)  # opened from the Desk
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}/signin")
        page.wait_for_function("() => /sign in first/.test(document.querySelector('#tailor-panel-host')?.shadowRoot?.getElementById('status')?.textContent || '')", timeout=20000)
        page.wait_for_timeout(1500)
        self.assertEqual(page.evaluate("[...document.querySelectorAll('input')].map((i) => i.value).join('')"), "")
        page.close()


if __name__ == "__main__":
    unittest.main()

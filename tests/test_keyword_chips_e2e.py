"""The tailoring guide's step 3 in the panel, end to end (2026-10-06): the posting's keyword match before and after
tailoring, the keywords the resume lacks as chips, and "I have this": the person confirms one, it is saved as their
skill and the resume is tailored again with it. Uses the real base resume (skipped without it).
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
from tailor.resume.schema import Resume  # noqa: E402
from tailor.server.app import create_app  # noqa: E402

EXT = Path(__file__).resolve().parent.parent / "extension"
TOKEN = "chips-e2e-token"
HOST = "careers.acme-chips.test"
REAL_BASE = load_settings().profile_dir / "base_resume.json"

PAGE = """<!doctype html><html><head><title>Strategy Consultant | Acme</title></head><body>
<h1>Strategy Consultant</h1>
<p>Acme is a strategy consulting firm that helps healthcare, consumer and technology clients make better decisions. Our
consultants work in small teams on short engagements with senior client leadership, from first hypothesis to final
recommendation, and we invest heavily in training new consultants through mentorship and real client work.</p>
<h3>Responsibilities</h3>
<ul><li>Develop strategic recommendations for client leaders based on primary research and market research.</li>
<li>Build PowerPoint presentations and present findings to executives.</li>
<li>Analyze data and synthesize qualitative and quantitative findings.</li></ul>
<h3>Requirements</h3>
<ul><li>Advanced PowerPoint and Excel skills.</li><li>Experience with Tableau dashboards.</li>
<li>Strong stakeholder management and communication skills.</li></ul>
<form onsubmit="return false">
<label for="fn">First Name</label><input id="fn"><label for="ln">Last Name</label><input id="ln">
<label for="em">Email</label><input id="em" type="email"><label for="ph">Phone</label><input id="ph" type="tel">
<label for="cv">Resume/CV</label><input id="cv" type="file">
<label for="li">LinkedIn Profile</label><input id="li">
<button type="button" id="submit">Submit application</button></form></body></html>"""


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


@unittest.skipUnless(REAL_BASE.exists(), "real base resume not present")
class KeywordChipsE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.settings = settings = Settings(data_dir=tmp / "data")
        settings.ensure_dirs()
        pdf = settings.output_dir / "current_resume.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% e2e test resume\n")
        base = Resume.model_validate_json(REAL_BASE.read_text(encoding="utf-8"))
        cls.api_port, cls.site_port = free_port(), free_port()
        app = create_app(settings, StubClient("Fine."), PROFILE, TOKEN, pdf, base_resume=base)
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

    def test_score_chips_and_i_have_this(self):
        page = self.ctx.new_page()
        page.on("dialog", lambda d: d.accept())  # the person confirms "Add PowerPoint to your skills?"
        page.goto(f"http://{HOST}:{self.site_port}/jobs/7/apply")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        page.evaluate("() => { const r = document.querySelector('#tailor-panel-host').shadowRoot;"
                      " r.getElementById('autonext').checked = false; r.getElementById('fill').click(); }")
        root = "document.querySelector('#tailor-panel-host').shadowRoot"
        page.wait_for_function(f"() => /ATS keyword match: \\d+% → \\d+%/.test({root}.getElementById('ats').textContent)", timeout=60000)
        chips = page.evaluate(f"() => [...{root}.getElementById('kwchips').querySelectorAll('button')].map(b => b.textContent)")
        self.assertIn("+ PowerPoint", chips)
        self.assertIn("+ Tableau", chips)
        before = int(page.evaluate(f"() => {root}.getElementById('ats').textContent.match(/→ (\\d+)%/)[1]"))
        page.evaluate(f"() => [...{root}.getElementById('kwchips').querySelectorAll('button')].find(b => b.textContent === '+ PowerPoint').click()")
        page.wait_for_function(f"() => /Saved \\\"PowerPoint\\\"/.test({root}.getElementById('status').textContent)", timeout=60000)
        after = int(page.evaluate(f"() => {root}.getElementById('ats').textContent.match(/→ (\\d+)%/)[1]"))
        self.assertGreater(after, before)
        over = json.loads((self.settings.profile_dir / "profile_overrides.json").read_text(encoding="utf-8"))
        self.assertEqual(over["confirmed_skills"], ["PowerPoint"])
        chips = page.evaluate(f"() => [...{root}.getElementById('kwchips').querySelectorAll('button')].map(b => b.textContent)")
        self.assertNotIn("+ PowerPoint", chips)  # it is on the resume now
        self.assertIn("+ Tableau", chips)  # never added without the click
        self.assertFalse(page.evaluate("() => window.__submitted || false"))
        page.close()


if __name__ == "__main__":
    unittest.main()

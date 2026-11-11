"""The tailoring API: anyone's resume, a key, nothing kept, and the same truth check as the desktop tool."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from tailor.api import create_api, hash_key  # noqa: E402
from tailor.config import apply_environment, load_settings  # noqa: E402

RESUME = json.loads((ROOT / "examples" / "profile" / "base_resume.json").read_text(encoding="utf-8"))
JOB = {"title": "Operations Analyst", "text": ("Operations Analyst. Responsibilities: build dashboards in Tableau, analyze data, "
       "map workflows and interview stakeholders to improve processes. Qualifications: SQL, Tableau, stakeholder management, "
       "process improvement and strong communication. ") * 6}
H = {"Authorization": "Bearer test-key"}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        apply_environment(load_settings())
        cls.c = TestClient(create_api({hash_key("test-key")}))

    def test_a_key_is_required(self):
        self.assertEqual(self.c.post("/v1/score", json={"resume": RESUME, "job": JOB}).status_code, 401)
        self.assertEqual(self.c.post("/v1/score", json={"resume": RESUME, "job": JOB}, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(TestClient(create_api(set())).post("/v1/score", json={"resume": RESUME, "job": JOB}, headers=H).status_code, 401)
        self.assertTrue(self.c.get("/v1/health").json()["ok"])

    def test_score(self):
        r = self.c.post("/v1/score", json={"resume": RESUME, "job": JOB}, headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(0 <= r.json()["percent"] <= 100)

    def test_tailor_returns_a_truthful_one_page_resume_and_form_content(self):
        r = self.c.post("/v1/tailor", json={"resume": RESUME, "job": JOB}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["report"]["fits_one_page"])
        self.assertGreaterEqual(d["report"]["keyword_match_after"], d["report"]["keyword_match_before"])
        self.assertTrue(d["pdf_base64"])
        self.assertTrue(any(x["employer"] == "BRIGHTPATH CLINICS" for x in d["role_descriptions"]))
        self.assertIsInstance(d["wanted_skills"], list)

    def test_parse_resume(self):
        text = "Sam Lee\nsam@example.com\n\nEXPERIENCE\nContoso | Analyst   Jan 2020 - Present\n- Grew sales 12%\n\nEDUCATION\nState University   2015 - 2019\nBachelor of Arts\n"
        r = self.c.post("/v1/parse-resume", json={"text": text}, headers=H).json()
        self.assertEqual(r["resume"]["header"]["name"], "Sam Lee")
        self.assertEqual(self.c.post("/v1/parse-resume", json={"text": "hello"}, headers=H).status_code, 422)

    def test_a_posting_too_short_is_refused(self):
        self.assertEqual(self.c.post("/v1/tailor", json={"resume": RESUME, "job": {"text": "PM"}}, headers=H).status_code, 422)

    def test_fill_plan_leaves_sensitive_questions_to_the_person(self):
        profile = {"first_name": "Jordan", "last_name": "Rivera", "email": "j@example.com", "phone": "555"}
        fields = [{"id": "a", "label": "First name"}, {"id": "b", "label": "Are you a veteran?", "type": "select",
                   "options": [{"value": "y", "label": "Yes"}, {"value": "n", "label": "No"}]}]
        r = self.c.post("/v1/fill-plan", json={"profile": profile, "fields": fields}, headers=H).json()
        self.assertEqual([(a["kind"], a["value"]) for a in r["actions"]], [("fill", "Jordan"), ("needs_you", None)])


if __name__ == "__main__":
    unittest.main()

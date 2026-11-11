"""Local API tests: behaviour and, more importantly, the protections."""
from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from tailor.config import Settings  # noqa: E402
from tailor.fill.profile import Profile  # noqa: E402
from tailor.llm import StubClient  # noqa: E402
from tailor.server.app import create_app  # noqa: E402

TOKEN = "test-token-123"
H = {"X-Tailor-Token": TOKEN}
EXT = "chrome-extension://" + "a" * 32
PROFILE = Profile(first_name="Jane", last_name="Doe", email="jane@example.com", phone="(555) 010-0000")


class Boom:
    def complete(self, *a, **k):
        raise ConnectionError("model is not running")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.settings.ensure_dirs()
        self.pdf = self.settings.output_dir / "current_resume.pdf"
        self.make(StubClient("I enjoy analytical work."))

    def make(self, client):
        self.c = TestClient(create_app(self.settings, client, PROFILE, TOKEN), base_url="http://127.0.0.1:8765")

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_needs_no_token_and_reveals_nothing_personal(self):
        r = self.c.get("/api/health")
        self.assertEqual(r.json(), {"ok": True, "model": "stub"})

    def test_cover_letter_text_and_pdf(self):
        from tailor.config import apply_environment, load_settings

        apply_environment(load_settings())  # the real browser folder for the PDF
        self.make(StubClient("I want to help Brattle's clients with clear financial analysis.\n\nAt Acme I sold upgrades."))
        r = self.c.post("/api/cover-letter", headers=H, json={
            "title": "Job Application for Associate, MBA - Economics/Finance at The Brattle Group",
            "page_text": "Associate. Qualifications: analysis, communication. " * 20})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("Brattle", body["text"])
        self.assertTrue(body["name"].endswith("_Cover_Letter.pdf"))
        pdf = self.c.get(f"/api/cover-letter?id={body['id']}", headers=H)
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertIn("Cover_Letter.pdf", pdf.headers["content-disposition"])
        self.assertEqual(self.c.get("/api/cover-letter?id=../../x", headers=H).status_code, 404)

    def test_cover_letter_never_invents_a_referral(self):
        from tailor.fill.cover import write_cover_letter
        from tailor.fill.profile import Profile as P

        p = P(first_name="Jane", last_name="Doe", email="j@example.com", phone="1")
        made_up = ("A former professor referred me to Brattle. I want to do economic analysis.\n\n"
                   "My research taught me careful work.\n\nThank you for your time.")
        d = write_cover_letter(StubClient(made_up), p, "The Brattle Group", "Associate", "Associate. " * 30)
        self.assertNotIn("referred", d.text)
        self.assertIn("economic analysis", d.text)
        d2 = write_cover_letter(StubClient(made_up), p, "The Brattle Group", "Associate", "Associate. " * 30,
                                instruction="referral: a former professor referred me")
        self.assertIn("referred", d2.text)  # the person gave it: it stays

    def test_country_of_employment_from_the_page(self):
        st = Path(self.settings.profile_dir) / "standing_answers.json"
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps({"work_authorization_us": "Yes", "sponsorship_us": "No", "us_answers_for_us_roles": True}))
        q = {"id": "s", "label": "Do you currently need, or will you someday require, Google to sponsor work authorization "
             "for you to work in the country of employment?", "type": "radio", "required": True,
             "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]}
        page = "Welcome, Jordan. Associate Product Manager, University Graduate, 2027 Start - Mountain View, New York, San Bruno or San Jose"
        a = self.c.post("/api/fill-plan", headers=H, json={"url": "https://www.google.com/x", "page_text": page, "fields": [q]}).json()
        self.assertEqual(a["actions"][0]["value"], "no")
        b = self.c.post("/api/fill-plan", headers=H, json={"url": "https://www.google.com/x", "page_text": "Product Manager - London, UK",
                                                         "fields": [q]}).json()
        self.assertEqual(b["actions"][0]["kind"], "needs_you")


    def test_no_drafts_where_the_employer_asks_for_your_own_words(self):  # Anthropic and Solace, 2026-10-07
        q = {"id": "e1", "label": "Why do you want to work here?", "type": "textarea", "required": True}
        page = "Why Anthropic? We invite you to review our AI partnership guidelines for candidates."
        a = self.c.post("/api/fill-plan", headers=H, json={"url": "https://x.example/apply", "page_text": page, "fields": [q]}).json()
        self.assertEqual(a["actions"][0]["kind"], "needs_you")
        self.assertIn("own words", a["actions"][0]["reason"])
        b = self.c.post("/api/fill-plan", headers=H, json={"url": "https://x.example/apply", "page_text": "Apply now", "fields": [q]}).json()
        self.assertEqual(b["actions"][0]["kind"], "draft")

    def test_cover_letters_follow_the_setting_and_the_employers_ai_policy(self):
        up = {"id": "c1", "label": "Cover Letter", "type": "file", "required": False}
        box = {"id": "c2", "label": "Cover Letter", "type": "textarea", "required": False}
        plan = lambda fields, page="Apply now": {a["field_id"]: a for a in self.c.post(
            "/api/fill-plan", headers=H, json={"url": "https://x.example/apply", "page_text": page, "fields": fields}).json()["actions"]}
        a = plan([up, box])
        self.assertEqual((a["c1"]["kind"], a["c2"]["kind"]), ("cover", "skip"))  # one letter: the upload gets it
        self.assertEqual(plan([box])["c2"]["kind"], "cover")
        no_ai = plan([up], "Please do not use AI tools to write your application materials or answers.")
        self.assertEqual(no_ai["c1"]["kind"], "skip")  # optional: left empty; the person may still add one
        over = self.settings.profile_dir / "profile_overrides.json"
        over.write_text(json.dumps({"cover_letters": "required"}))
        self.assertEqual(plan([up])["c1"]["kind"], "skip")
        self.assertEqual(plan([dict(up, required=True)])["c1"]["kind"], "cover")
        over.write_text(json.dumps({"cover_letters": "never"}))
        self.assertEqual(plan([dict(up, required=True)])["c1"]["kind"], "needs_you")

    def test_the_transcript_is_attached_only_when_one_is_saved(self):
        box = {"id": "t1", "label": "Please share your university transcript(s)", "type": "file", "required": True}
        plan = lambda: self.c.post("/api/fill-plan", headers=H, json={"url": "https://x.example/apply", "page_text": "Apply",
                                                                      "fields": [box]}).json()["actions"][0]
        self.assertEqual(plan()["kind"], "needs_you")
        self.assertEqual(self.c.get("/api/transcript", headers=H).status_code, 404)
        (self.settings.profile_dir / "transcript.pdf").write_bytes(b"%PDF-1.4 transcript")
        self.assertEqual(plan()["kind"], "transcript")
        r = self.c.get("/api/transcript", headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Jane_Doe_Transcript.pdf", r.headers["content-disposition"])
        self.assertEqual(self.c.get("/api/transcript").status_code, 401)

    def test_an_undergraduate_transcript_box_gets_the_undergraduate_one_only_from_a_new_extension(self):
        box = {"id": "t1", "label": "Please provide a recent transcript of your undergraduate studies.", "type": "file",
               "required": True}
        plan = lambda **kw: self.c.post("/api/fill-plan", headers=H, json=dict(
            {"url": "https://x.example/apply", "page_text": "Apply", "fields": [box]}, **kw)).json()["actions"][0]
        (self.settings.profile_dir / "transcript.pdf").write_bytes(b"%PDF-1.4 graduate")
        self.assertEqual(plan(features=["transcript_which"])["kind"], "needs_you")  # none saved yet
        (self.settings.profile_dir / "undergrad_transcript.pdf").write_bytes(b"%PDF-1.4 undergraduate")
        self.assertEqual(plan()["kind"], "needs_you")  # an older extension would attach the graduate one there
        a = plan(features=["transcript_which"])
        self.assertEqual((a["kind"], a["value"]), ("transcript", "undergraduate"))
        r = self.c.get("/api/transcript?which=undergraduate", headers=H)
        self.assertEqual(r.content, b"%PDF-1.4 undergraduate")
        self.assertIn("Jane_Doe_Undergraduate_Transcript.pdf", r.headers["content-disposition"])
        self.assertEqual(self.c.get("/api/transcript", headers=H).content, b"%PDF-1.4 graduate")

    def test_a_cover_letter_is_written_once_per_posting_and_reused_by_the_fill(self):
        from tailor.config import apply_environment, load_settings

        apply_environment(load_settings())
        stub = StubClient("Brattle's clients need careful analysis.\n\nMy research work taught me that.\n\nThank you.")
        self.make(stub)
        req = {"url": "https://boards.example/brattle/jobs/1?gh_src=x", "title": "Associate at The Brattle Group",
               "page_text": "Associate. Qualifications: analysis, communication. " * 20, "reuse": True}
        first = self.c.post("/api/cover-letter", headers=H, json=req).json()
        calls = len(stub.calls)
        again = self.c.post("/api/cover-letter", headers=H, json=dict(req, url="https://boards.example/brattle/jobs/1")).json()
        self.assertEqual((again["id"], again.get("reused")), (first["id"], True))
        self.assertEqual(len(stub.calls), calls)  # no second letter written
        fresh = self.c.post("/api/cover-letter", headers=H, json=dict(req, reuse=False)).json()
        self.assertNotEqual(fresh["id"], first["id"])  # the chat's "write it" always writes a new one

    def test_a_box_for_resume_and_cover_letter_together_gets_one_pdf_with_both(self):
        # Simon-Kucher (Cornerstone, 2026-10-10): "Please upload your Resume and Cover Letter together in one document"
        import io

        from pypdf import PdfReader, PdfWriter

        from tailor.config import apply_environment, load_settings

        apply_environment(load_settings())
        self.make(StubClient("Simon-Kucher's clients grow through pricing.\n\nAt Acme I sold upgrades.\n\nThank you."))
        w = PdfWriter()
        w.add_blank_page(612, 792)
        w.write(str(self.pdf))  # the default resume
        url = "https://sk.csod.com/ux/ats/careersite/6/requisition/4397/application?c=sk#7"
        box = {"id": "u1", "label": "Upload Document", "type": "file", "required": True,
               "context": "Upload Attachments. Please upload your Resume and Cover Letter together in one document."}
        a = self.c.post("/api/fill-plan", headers=H, json={"url": url, "page_text": "Step 7", "fields": [box]}).json()["actions"][0]
        self.assertEqual((a["kind"], a["value"]), ("cover", "with_resume"))
        body = self.c.post("/api/cover-letter", headers=H, json={"url": url, "title": "Consultant at Simon-Kucher",
                                                                 "page_text": "Consultant. Pricing, analysis. " * 30}).json()
        self.assertEqual(body["name"], "Jane_Doe_Resume_and_Cover_Letter.pdf")
        pdf = self.c.get(f"/api/cover-letter?id={body['id']}", headers=H)
        letter = PdfReader(self.settings.output_dir / "cover_letters" / f"{body['id']}.pdf")
        self.assertEqual(len(PdfReader(io.BytesIO(pdf.content)).pages), 1 + len(letter.pages))
        # the sentence above the box, not on it: the page text says it
        page = "Upload Attachments* Please upload your Resume and Cover Letter together in one document."
        a = self.c.post("/api/fill-plan", headers=H, json={"url": url, "page_text": page, "fields": [
            dict(box, context="", label="Resume")]}).json()["actions"][0]
        self.assertEqual((a["kind"], a["value"]), ("cover", "with_resume"))
        # a plain resume box elsewhere is still just the resume
        plain = dict(box, id="u2", label="Resume", context="")
        a = self.c.post("/api/fill-plan", headers=H, json={"url": "https://x.example/apply", "fields": [plain]}).json()["actions"][0]
        self.assertEqual(a["kind"], "upload")

    def test_a_letter_claiming_what_the_resume_does_not_show_loses_that_sentence(self):
        from tailor.fill.cover import write_cover_letter

        letter = ("I want to help Brattle's clients. I used SQL and Tableau daily to build models for senior executives.\n\n"
                  "My research work taught me careful analysis.\n\nThank you for your time.")
        p = PROFILE.model_copy(update={"skills": ["Excel"]})
        d = write_cover_letter(StubClient(letter), p, "The Brattle Group", "Associate", "Associate. " * 30)
        self.assertNotIn("SQL", d.text)
        self.assertIn("careful analysis", d.text)
        self.assertTrue(any("does not show" in w for w in d.warnings), d.warnings)
    def test_everything_else_requires_the_token(self):
        for method, path in [("POST", "/api/fill-plan"), ("POST", "/api/answer"), ("GET", "/api/resume"),
                             ("POST", "/api/answer-sheet")]:
            body = {} if method == "POST" else None
            r = self.c.request(method, path, json=body)
            self.assertEqual(r.status_code, 401, path)
            r = self.c.request(method, path, json=body, headers={"X-Tailor-Token": "wrong"})
            self.assertEqual(r.status_code, 401, path)

    def test_docs_are_not_exposed(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.c.get(path).status_code, 404, path)

    def test_foreign_host_header_is_rejected(self):
        r = self.c.get("/api/health", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 421)

    def test_cors_allows_only_extension_origins(self):
        ok = self.c.options("/api/fill-plan", headers={"Origin": EXT, "Access-Control-Request-Method": "POST",
                                                       "Access-Control-Request-Headers": "x-tailor-token"})
        self.assertEqual(ok.headers.get("access-control-allow-origin"), EXT)
        bad = self.c.options("/api/fill-plan", headers={"Origin": "https://evil.example",
                                                        "Access-Control-Request-Method": "POST"})
        self.assertIsNone(bad.headers.get("access-control-allow-origin"))

    def test_fill_plan_fills_known_fields_and_flags_sensitive_ones(self):
        body = {"fields": [
            {"id": "a", "label": "First Name", "required": True},
            {"id": "b", "label": "Email", "type": "email", "required": True},
            {"id": "c", "label": "Gender", "type": "select"},
            {"id": "d", "label": "Location (City)", "required": True},
        ]}
        r = self.c.post("/api/fill-plan", json=body, headers=H).json()
        by = {a["field_id"]: a for a in r["actions"]}
        self.assertEqual((by["a"]["kind"], by["a"]["value"]), ("fill", "Jane"))
        self.assertEqual(by["b"]["value"], "jane@example.com")
        self.assertEqual((by["c"]["kind"], by["c"]["category"]), ("needs_you", "demographics"))
        self.assertEqual(r["summary"], {"fill": 2, "needs_you": 2})
        self.assertEqual(sorted(r["needs_you"]), ["Gender", "Location (City)"])

    def test_answer_returns_a_draft(self):
        r = self.c.post("/api/answer", json={"question": "Why do you want to work here?"}, headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["text"], "I enjoy analytical work.")

    def test_sensitive_question_is_refused(self):
        r = self.c.post("/api/answer", json={"question": "Are you legally authorized to work in the US?"}, headers=H)
        self.assertEqual(r.status_code, 422)

    def test_model_outage_is_a_clear_503_not_a_crash(self):
        self.make(Boom())
        r = self.c.post("/api/answer", json={"question": "Why this role?"}, headers=H)
        self.assertEqual(r.status_code, 503)
        self.assertIn("ConnectionError", r.json()["detail"])

    def test_resume_download(self):
        self.assertEqual(self.c.get("/api/resume", headers=H).status_code, 404)
        self.pdf.write_bytes(b"%PDF-1.4 test")
        r = self.c.get("/api/resume", headers=H)
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "application/pdf"))
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_remembered_answers_are_used_next_time_but_sensitive_ones_are_refused(self):
        q = "How did you hear about this job?"
        self.assertEqual(self.c.post("/api/answer-sheet", json={"question": q, "answer": "LinkedIn"}, headers=H).json(),
                         {"saved": 1})
        field = {"id": "x", "label": q, "required": True}
        a = self.c.post("/api/fill-plan", json={"fields": [field]}, headers=H).json()["actions"][0]
        self.assertEqual((a["kind"], a["value"], a["source"]), ("fill", "LinkedIn", "answer_sheet"))
        r = self.c.post("/api/answer-sheet", json={"question": "Gender", "answer": "x"}, headers=H)
        self.assertEqual(r.status_code, 422)

    def test_the_chat_fields_reach_the_model_and_the_placeholder_model_is_refused(self):
        client = StubClient("Because I like analytical work.")
        self.make(client)
        r = self.c.post("/api/answer", headers=H, json={"question": "Why do you want to join Acme?",
                                                         "instruction": "3-4 sentences", "current": "old text"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("- 3-4 sentences", client.calls[0])
        self.assertIn("old text", client.calls[0])
        self.make(StubClient())  # the default placeholder reply: never put "[stub reply]" in someone's application
        r = self.c.post("/api/answer", headers=H, json={"question": "Why do you want to join Acme?"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("not running", r.json()["detail"])

    def test_tailoring_needs_the_token_and_a_base_resume(self):
        self.assertEqual(self.c.post("/api/tailor", json={}).status_code, 401)
        r = self.c.post("/api/tailor", json={"page_text": "x" * 900}, headers=H)  # this app has no base resume loaded
        self.assertEqual((r.status_code, r.json()["applied"]), (200, False))
        self.assertIn("no base resume", r.json()["summary"])

    def test_a_tailored_resume_can_only_be_fetched_by_an_id_the_server_made(self):
        for bad in ("../../secret", "abcdef123456", "ZZZZZZZZZZZZ", "a" * 40):
            self.assertEqual(self.c.get("/api/resume", params={"id": bad}, headers=H).status_code, 404, bad)


try:
    from test_auto_tailor import BASE_PATH, SETTINGS, UX_JOB  # noqa: E402
except ImportError:  # run as tests.test_server
    from tests.test_auto_tailor import BASE_PATH, SETTINGS, UX_JOB  # noqa: E402


@unittest.skipUnless(BASE_PATH.exists(), "real base resume not present")
class TailorEndpointTests(unittest.TestCase):
    """The whole path the extension uses: post the page text, get an id, fetch that resume."""

    def test_tailor_then_fetch_the_tailored_pdf(self):
        from tailor.resume.schema import Resume

        base = Resume.model_validate_json(BASE_PATH.read_text(encoding="utf-8"))
        default_pdf = SETTINGS.output_dir / "tailor_endpoint_default.pdf"
        default_pdf.write_bytes(b"%PDF-1.4 default")
        c = TestClient(create_app(SETTINGS, StubClient(), PROFILE, TOKEN, default_pdf, base_resume=base),
                       base_url="http://127.0.0.1:8765")
        r = c.post("/api/tailor", json={"url": "https://x.example/job", "title": "User Experience Researcher",
                                        "page_text": UX_JOB}, headers=H)
        body = r.json()
        self.assertTrue(body["applied"], body)
        self.assertRegex(body["id"], r"^[0-9a-f]{12}$")
        self.assertIn("MS in Information", body["summary"])
        pdf = c.get("/api/resume", params={"id": body["id"]}, headers=H)
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertNotEqual(pdf.content, b"%PDF-1.4 default")
        self.assertEqual(c.get("/api/resume", headers=H).content, b"%PDF-1.4 default")  # no id: the default
        again = c.post("/api/tailor", json={"title": "User Experience Researcher", "page_text": UX_JOB}, headers=H).json()
        self.assertEqual(again["id"], body["id"])  # same posting, same resume
        # the same posting's page with the form half filled in (a reloaded tab): reused, not tailored again
        reloaded = c.post("/api/tailor", json={"url": "https://x.example/job#apply", "title": "User Experience Researcher",
                                               "page_text": UX_JOB + "\nFirst name: Jane\nEmail: jane@example.com"}, headers=H).json()
        self.assertEqual(reloaded["id"], body["id"])
        steered = c.post("/api/tailor", json={"url": "https://x.example/job", "title": "User Experience Researcher",
                                              "page_text": UX_JOB, "instruction": "lead with research"}, headers=H).json()
        self.assertNotEqual(steered.get("id"), body["id"])  # an instruction always tailors again
        empty = c.post("/api/tailor", json={"page_text": "Apply now. First name. Email. Submit."}, headers=H).json()
        self.assertFalse(empty["applied"])


if __name__ == "__main__":
    unittest.main()


class FromPageTests(unittest.TestCase):
    """2026-10-02: "Write it" did nothing: /api/tailor refused a null title and a chat note over 300 characters."""

    def test_page_requests_are_tidied_not_refused(self):
        from tailor.server.app import AnswerRequest, TailorRequest

        r = TailorRequest.model_validate({"url": "x", "title": None, "page_text": "p", "instruction": "a" * 350})
        self.assertEqual(len(TailorRequest.model_validate({"instruction": "a" * 2500}).instruction), 2000)
        self.assertEqual((r.title, len(r.instruction)), ("", 350))
        a = AnswerRequest.model_validate({"question": "Why?", "max_chars": 10, "hint": None})
        self.assertEqual((a.max_chars, a.hint), (None, ""))


class FactQuestionTests(unittest.TestCase):
    def test_the_chat_answers_a_profile_question_with_the_fact(self):
        from tailor.fill.forms import FormField
        from tailor.fill.mapper import plan_fields
        from tailor.fill.profile import Profile

        p = Profile(first_name="Jane", last_name="Doe", email="j@example.com", phone="1", how_heard="LinkedIn")
        a = plan_fields([FormField(id="q", label="How did you hear about this job?", type="text")], p, {})[0]
        self.assertEqual((a.kind, a.source, a.value), ("fill", "profile", "LinkedIn"))
        b = plan_fields([FormField(id="q", label="Why do you want to work at Oscar?", type="text")], p, {})[0]
        self.assertNotEqual(b.kind, "fill")



class CoverLetterWritingTests(unittest.TestCase):
    def test_one_model_call_when_nothing_is_untrue_and_stock_phrases_said_plainly(self):
        from tailor.fill.cover import write_cover_letter

        letter = ("Brattle's work aligns with my research. I am excited to help its clients.\n\n"
                  "My research work taught me careful analysis.\n\nThank you for your time.")
        stub = StubClient(letter)
        d = write_cover_letter(stub, PROFILE, "The Brattle Group", "Associate", "Associate. " * 30)
        self.assertEqual(len(stub.calls), 1)
        self.assertIn("Brattle's work fits my research.", d.text)
        self.assertIn("I would be glad to help", d.text)

    def test_an_untrue_letter_is_written_again_once(self):
        from tailor.fill.cover import write_cover_letter

        stub = StubClient("I managed a budget of 2 million at Brattle.\n\nMy research work taught me care.\n\nThanks.")
        d = write_cover_letter(stub, PROFILE, "The Brattle Group", "Associate", "Associate. " * 30)
        self.assertEqual(len(stub.calls), 2)
        self.assertNotIn("budget", d.text)  # still there after the second try: the sentence goes


class LetterAskTests(unittest.TestCase):
    def test_what_the_posting_asks_a_letter_to_say_reaches_the_model(self):
        from tailor.fill.cover import letter_asks, write_cover_letter

        page = ("Associate. To be considered, please submit: Cover letter that describes your interest in economic "
                "consulting and states your location preference. Resume. " * 3)
        self.assertTrue(letter_asks(page).startswith("Cover letter that describes your interest"))
        stub = StubClient("My research work taught me careful analysis.\n\nThank you.\n\nRegards.")
        p = PROFILE.model_copy(update={"location_preferences": ["New York", "Boston"]})
        write_cover_letter(stub, p, "The Brattle Group", "Associate", page)
        self.assertIn("states your location preference", stub.calls[0])
        self.assertIn("My office preferences, in order: New York, Boston", stub.calls[0])

    def test_office_preferences_are_not_where_the_person_lives(self):
        from tailor.fill.cover import write_cover_letter

        stub = StubClient("My research work taught me careful analysis. I'm based in New York or Boston.\n\nThank you.\n\nRegards.")
        p = PROFILE.model_copy(update={"location_preferences": ["New York", "Boston"], "city": "Ann Arbor"})
        d = write_cover_letter(stub, p, "The Brattle Group", "Associate", "Associate. " * 30)
        self.assertNotIn("based in", d.text)
        self.assertIn("careful analysis", d.text)

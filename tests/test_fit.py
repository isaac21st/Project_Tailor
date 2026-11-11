"""The role-fit read and the career profile, on a made-up person."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.resume.career import CareerProfile, NeverClaim, Track, best_track, emphasized, load_career  # noqa: E402
from tailor.resume.fit import assess_fit, years_worked  # noqa: E402
from tailor.resume.schema import Entry, Header, LabeledLine, Resume, Role, Section  # noqa: E402

RESUME = Resume(header=Header(name="Jordan Rivera", contact=[]), sections=[
    Section(heading="EDUCATION", entries=[Entry(title="Example University", roles=[Role(title="Master of Business Administration", dates="Expected May 2027")])]),
    Section(heading="PROFESSIONAL EXPERIENCE", entries=[
        Entry(title="ACME LOGISTICS", roles=[Role(title="Operations Analyst", dates="June 2022 - June 2024", bullets=[
            "Analyzed warehouse workflow data to diagnose process bottlenecks, improving operational efficiency across three sites",
            "Presented process improvement recommendations to senior stakeholders and coordinated cross-functional implementation"])]),
        Entry(title="CITY LAB", roles=[Role(title="Research Assistant", dates="May 2024 - August 2024", bullets=["Coded survey responses into a structured dataset"])])]),
    Section(heading="PROJECT EXPERIENCE", entries=[
        Entry(title="HOSPITAL CLIENT", roles=[Role(title="Consultant", dates="March 2026", bullets=["Co-developed a clinical workflow and staffing plan for a hospital service line, presenting recommendations to client stakeholders"])]),
        Entry(title="CLASS PROJECT", roles=[Role(title="Student", dates="2025", bullets=["Built a Python data analysis of retail pricing for a course"])])]),
    Section(heading="SKILLS", lines=[LabeledLine(label="Tools", text="Excel, SQL, Survey Tool")]),
])
HEALTH = Track(id="health", name="Healthcare consulting", priority=1, firms=["Famous Health Partners"],
               role_terms=["healthcare consultant"], industry_terms=["healthcare", "hospital", "clinical", "health system"],
               emphasize=["healthcare"], evidence=["HOSPITAL CLIENT"], positioning="Lead with hospital work.")
CAREER = CareerProfile(tracks=[HEALTH], entry_kinds={"HOSPITAL CLIENT": "client_project", "CLASS PROJECT": "academic"},
                       outside_targets=["investment banking"], not_my_function=["software engineer"],
                       context=[], never_claim=[])


def posting(title: str, required: list[str], extra: str = "") -> tuple[str, str]:
    lines = "\n".join(f"- {r}" for r in required)
    body = (f"{title}\n\nAbout the role\nWork with hospital and health system clients on clinical operations and healthcare strategy.\n{extra}\n\n"
            f"Responsibilities\n- Analyze workflow data and present recommendations to client stakeholders\n"
            f"- Support process improvement across hospital operations\n\nRequired qualifications\n{lines}\n")
    return body * 2, title


class FitTests(unittest.TestCase):
    def fit(self, title, required, career=CAREER, company="", extra=""):
        text, t = posting(title, required, extra)
        return assess_fit(RESUME, text, t, company, career)

    def test_a_target_role_with_evidence_fits(self):
        f = self.fit("Healthcare Consultant", ["Experience analyzing workflow data and process bottlenecks", "Experience presenting recommendations to stakeholders"])
        self.assertGreaterEqual(f["score"], 60)
        self.assertEqual(f["track"]["name"], "Healthcare consulting")
        self.assertGreaterEqual(f["counts"]["direct"], 1)

    def test_six_dimensions_and_the_report_parts(self):
        f = self.fit("Healthcare Consultant", ["Experience analyzing workflow data"])
        self.assertEqual([d["name"] for d in f["dimensions"]], ["Industry alignment", "Functional alignment", "Relevant experience",
                                                                "Technical skills", "Education alignment", "Seniority and experience"])
        for key in ("score", "strongest", "missing", "experiences", "positioning", "counts"):
            self.assertIn(key, f)

    def test_keywords_do_not_make_another_profession_fit(self):
        words = ["Experience analyzing workflow data, process improvement and stakeholders in healthcare hospital clinical settings"]
        f = self.fit("Software Engineer", words)
        self.assertLessEqual(f["score"], 40)

    def test_far_too_senior_is_capped(self):
        f = self.fit("Director, Healthcare Consulting", ["Experience analyzing workflow data"])
        self.assertLessEqual(f["score"], 45)

    def test_years_asked_are_compared_with_paid_work_only(self):
        self.assertAlmostEqual(years_worked(RESUME, CAREER), 2.2, places=1)
        f = self.fit("Healthcare Consultant", ["7+ years of consulting experience"])
        self.assertLessEqual(f["score"], 45)

    def test_a_named_tool_nobody_shows_is_missing_not_transferable(self):
        f = self.fit("Healthcare Consultant", ["Experience with Salesforce required for client data workflow reporting"])
        self.assertTrue(any("Salesforce" in r["text"] for r in f["missing"]))

    def test_a_required_license_is_missing(self):
        f = self.fit("Healthcare Consultant", ["Active RN license and clinical workflow experience"])
        self.assertTrue(any("RN license" in r["text"] for r in f["missing"]))

    def test_coursework_is_labeled_academic(self):
        f = self.fit("Healthcare Consultant", ["Python data analysis of retail pricing in a business setting"])
        rows = f["strongest"] + f["missing"]
        self.assertIn("academic", {r["label"] for r in rows})

    def test_a_listed_firm_alone_adds_nothing_for_a_role_outside_the_targets(self):
        other = [("Warehouse Lead", "Run shifts at the loading dock and manage forklift schedules. " * 40)]
        title, body = other[0]
        self.assertEqual(best_track(CAREER, title, "Famous Health Partners", body), (None, 0))

    def test_never_claim_keeps_that_entry_out_of_the_evidence(self):
        barred = CAREER.model_copy(update={"never_claim": [NeverClaim(term="Survey Tool", entry="CITY LAB")]})
        f = self.fit("Healthcare Consultant", ["Coded survey responses with Survey Tool into a structured dataset"], career=barred)
        self.assertFalse(any(r["entry"] == "CITY LAB" for r in f["strongest"]))

    def test_outside_the_targets_is_said_and_capped(self):
        f = self.fit("Investment Banking Analyst", ["Experience analyzing workflow data"])
        self.assertLessEqual(f["score"], 45)

    def test_not_a_job_description(self):
        self.assertIsNone(assess_fit(RESUME, "Sign in to continue", "Sign in", "", CAREER))

    def test_context_never_reaches_the_resume_model(self):
        self.assertNotIn("context", Resume.model_fields)


class CareerTests(unittest.TestCase):
    def test_no_file_means_no_career_profile(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(load_career(SimpleNamespace(profile_dir=Path(d))))

    def test_a_broken_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "career_profile.json").write_text("{nope", encoding="utf-8")
            self.assertIsNone(load_career(SimpleNamespace(profile_dir=Path(d))))

    def test_the_example_profile_is_valid(self):
        ex = Path(__file__).resolve().parent.parent / "examples" / "profile" / "career_profile.json"
        self.assertTrue(CareerProfile.model_validate(json.loads(ex.read_text(encoding="utf-8"))).tracks)

    def test_emphasis_cannot_invent_what_the_posting_lacks(self):
        vec = {"healthcare": 0.0, "operations": 2.0}
        self.assertEqual(emphasized(vec, HEALTH), {"healthcare": 0.0, "operations": 2.0})
        self.assertEqual(emphasized({"healthcare": 2.0}, HEALTH), {"healthcare": 2.5})
        self.assertEqual(emphasized(vec, None), vec)


if __name__ == "__main__":
    unittest.main()

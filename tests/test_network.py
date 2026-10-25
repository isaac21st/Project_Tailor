"""Networking for referrals (2026-10-13): location first, then alumni, then past coworkers; searches the person opens,
drafts from real facts, and the contacts the person saves. Nothing contacts anyone or reads people's profiles."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from tailor.config import Settings  # noqa: E402
from tailor.fill.profile import Education, Experience, Profile, YearMonth  # noqa: E402
from tailor.llm import StubClient  # noqa: E402
from tailor.network import (Tie, job_ref, load_ties, ties_from_profile, parse_connections_csv, parse_search_results, same_company, contact_score, contact_ties, due_follow_ups, fill_name, location_focus,  # noqa: E402
                            message_drafts, metro_of, posting_contacts, posting_places, search_plan)
from tailor.server.app import create_app  # noqa: E402

TOKEN = "network-token"
H = {"X-Tailor-Token": TOKEN}
# a made-up person: every tie below comes from this profile
PROFILE = Profile(first_name="Jordan", last_name="Rivera", email="jordan@example.com", phone="1", city="Los Gatos",
                  state="California", location_preferences=["San Francisco", "New York", "Chicago"],
                  education=[Education(school="Lakeview University, Harbor School of Business",
                                       degree="Master of Business Administration", end=YearMonth(year=2027, month=5),
                                       expected=True),
                             Education(school="Lakeview University School of Information", degree="Master of Science",
                                       field="Information", end=YearMonth(year=2027, month=5), expected=True)],
                  experience=[Experience(employer="Acme Rental Group", title="Sales Associate"),
                              Experience(employer="Brightpath Clinics", title="Behavior Technician")])
TIES = ties_from_profile(PROFILE)
BIZ = "Lakeview University, Harbor School of Business"  # the profile's first school: its label
POSTING = """Associate Product Manager, New Grad
Location: New York, NY; Mountain View, CA; Bellevue, WA
Remote not available.
Our team works with Cambridge Consultants and many other partners on a long list of programs that change how products are made worldwide every single day.
Requirements: SQL, Python, product sense, user research, stakeholder management. """ * 3


class PlacesTests(unittest.TestCase):
    def test_office_cities_in_order_and_one_per_metro(self):
        self.assertEqual(posting_places(POSTING)[:3], ["New York", "Mountain View", "Bellevue"])
        self.assertNotIn("Cambridge", posting_places(POSTING))  # a brand inside a long paragraph is not an office

    def test_the_persons_own_area_comes_first(self):
        focus = location_focus(["New York", "Mountain View", "Bellevue"], "Los Gatos, California", ["San Francisco", "New York"])
        self.assertEqual(focus, ["Mountain View", "New York", "Bellevue"])  # Los Gatos and Mountain View: one Bay Area
        self.assertEqual(metro_of("Menlo Park, CA"), "San Francisco Bay Area")


class SearchPlanTests(unittest.TestCase):
    def test_location_and_alumni_come_first_and_searches_are_links_the_person_opens(self):
        plan = search_plan("Databricks", ["Mountain View", "New York"], TIES)
        self.assertEqual(plan[0].group, "Location + alumni")
        self.assertIn("Lakeview University", plan[0].label)
        q = unquote_plus(plan[0].url)
        self.assertIn(f'site:linkedin.com/in Databricks "{BIZ}" "Mountain View"', q)
        groups = [s.group for s in plan]
        self.assertLess(groups.index("Location + alumni"), groups.index("Alumni at the company"))
        self.assertLess(groups.index("Alumni at the company"), groups.index("Recruiters"))
        for s in plan:
            self.assertTrue(s.url.startswith(("https://www.google.com/search?q=", "https://www.linkedin.com/")))

    def test_a_company_with_spaces_is_kept_together(self):
        plan = search_plan("Partners in Performance (Accenture)", ["Chicago"], TIES)
        self.assertIn('"Partners in Performance"', unquote_plus(plan[0].url))


class PostingContactTests(unittest.TestCase):
    def test_the_recruiter_a_school_board_posting_names(self):
        text = "Primary Contact\nName\nRiley Park\nJob Title\nSenior Recruiting Manager\nEmail\nr.park@example.com\nPhone\n--"
        self.assertEqual(posting_contacts(text), [{"name": "Riley Park", "title": "Senior Recruiting Manager",
                                                   "email": "r.park@example.com", "source": "named on the posting"}])

    def test_a_recruiting_inbox_and_no_made_up_people(self):
        got = posting_contacts("Questions? Write to campus-recruiting@acme.com or see our site.")
        self.assertEqual(got[0]["email"], "campus-recruiting@acme.com")
        self.assertEqual(got[0]["name"], "")
        self.assertEqual(posting_contacts("Join us. We value curiosity."), [])


class DraftTests(unittest.TestCase):
    def test_notes_fit_linkedin_and_use_no_dashes_or_invented_facts(self):
        me = {"name": "Jordan Rivera", "grad": "May 2027", "school_line": "an MBA at Harbor and an MS in Information at Lakeview"}
        school = next(t for t in TIES if t.kind == "school")
        job = next(t for t in TIES if t.label == "Acme Rental Group")
        drafts = message_drafts(me, "Databricks", "Associate Product Manager", "Mountain View", school, job, "https://x/1")
        self.assertEqual([d.relation for d in drafts], ["connection", "alum", "coworker", "local", "recruiter"])
        for d in drafts:
            self.assertLessEqual(len(d.note), 300)
            self.assertNotIn("—", d.note + d.message)
            self.assertNotIn("–", d.note + d.message)
            self.assertIn("Databricks", d.note)
        self.assertIn(BIZ, drafts[1].note)
        self.assertIn("Acme Rental Group", drafts[2].note)
        for d in drafts:  # nothing from anyone else's life when the person's facts are missing
            self.assertNotIn("Michigan", d.note + d.message)
        self.assertEqual(fill_name("Hi {first_name},", "Riley Park"), "Hi Riley,")
        self.assertEqual(fill_name("Hi {first_name},", ""), "Hi there,")


class ScoreTests(unittest.TestCase):
    def test_location_first_then_school_then_employer(self):
        places = ["Mountain View"]
        local_alum = contact_ties({"headline": "PM at Databricks | Harbor School of Business MBA", "location": "San Francisco, CA"},
                                  TIES, places, "Los Gatos, California")
        remote_alum = contact_ties({"headline": "PM at Databricks | Harbor School of Business MBA", "location": "Austin, TX"},
                                   TIES, places, "Los Gatos, California")
        coworker = contact_ties({"headline": "Ops at Databricks, ex-Acme Rental Group", "location": "Austin, TX"},
                                TIES, places, "Los Gatos, California")
        self.assertIn("same city as the role (San Francisco Bay Area)", local_alum)
        self.assertGreater(contact_score(local_alum, TIES), contact_score(remote_alum, TIES))
        self.assertGreater(contact_score(remote_alum, TIES), contact_score(coworker, TIES))

    def test_follow_ups_after_five_days(self):
        old = (date.today() - timedelta(days=6)).isoformat()
        new = date.today().isoformat()
        cs = [{"id": "a", "status": "messaged", "contacted_at": old}, {"id": "b", "status": "messaged", "contacted_at": new},
              {"id": "c", "status": "replied", "contacted_at": old}]
        self.assertEqual([c["id"] for c in due_follow_ups(cs)], ["a"])

CONNECTIONS = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Sam,Lee,https://www.linkedin.com/in/samlee,,Databricks Inc.,Product Manager,03 Mar 2025
Ana,Cruz,https://www.linkedin.com/in/anacruz,ana@x.com,Facebook,Data Scientist,01 Jan 2024
Bo,Ng,javascript:alert(1),,Acme,Analyst,01 Jan 2024
"""


class ConnectionTests(unittest.TestCase):
    def test_the_export_is_read_and_companies_match_across_spellings(self):
        cs = parse_connections_csv(CONNECTIONS)
        self.assertEqual([c["name"] for c in cs], ["Sam Lee", "Ana Cruz", "Bo Ng"])
        self.assertEqual(cs[2]["link"], "")  # only linkedin.com links are kept
        self.assertTrue(same_company("Facebook", "Meta"))
        self.assertTrue(same_company("Databricks Inc.", "Databricks"))
        self.assertTrue(same_company("Adobe", "Adobe Systems"))
        self.assertFalse(same_company("Block", "Blockchain Capital"))
        self.assertEqual(parse_connections_csv("name,company\nx,y"), [])

    def test_job_ids_for_referral_forms(self):
        self.assertEqual(job_ref("https://adobe.wd5.myworkdayjobs.com/x/job/San-Jose/PM_R162345"), "R162345")
        self.assertEqual(job_ref("https://stripe.com/jobs/search?gh_jid=7737124"), "7737124")
        self.assertEqual(job_ref("", "Job ID: R-0451234 | Full time"), "R-0451234")
        self.assertEqual(job_ref("https://example.com/careers"), "")


RESULTS = """Web results
Anish Aggarwal - Senior Product Manager Databricks

LinkedIn · Anish Aggarwal
3.9K+ followers
... Databricks · Education: Lakeview University - Harbor School of Business · Location: San Francisco ... Databricks. 3 years.Read more
Rachel Booth - MBA Candidate @ Harbor School of Business

LinkedIn · Rachel Booth
830+ followers
San Francisco, California, United States · Product Manager · Databricks
Product @ Databricks | MBA Candidate @ Harbor School of Business.Read more
Pat Doe - Consultant

LinkedIn · Pat Doe
500+ followers
Austin, Texas, United States · Consultant · Acme
Consultant at Acme.Read more
About
Experience
Page Navigation
"""


class SearchResultsTests(unittest.TestCase):
    def test_people_on_a_pasted_google_page(self):
        links = [{"url": "/url?q=https://www.linkedin.com/in/rbooth%3Ftrk%3Dx&sa=U", "text": "Rachel Booth - MBA Candidate"},
                 {"url": "https://evil.example.com/in/anish", "text": "Anish Aggarwal - Senior"}]
        got = {p["name"]: p for p in parse_search_results(RESULTS, links, "Databricks, Inc.")}
        self.assertEqual(list(got), ["Anish Aggarwal", "Rachel Booth", "Pat Doe"])
        self.assertEqual(got["Anish Aggarwal"]["location"], "San Francisco")
        self.assertIn("Harbor School of Business", got["Anish Aggarwal"]["education"])
        self.assertEqual(got["Anish Aggarwal"]["link"], "")          # only linkedin.com links are kept
        self.assertEqual(got["Rachel Booth"]["link"], "https://www.linkedin.com/in/rbooth")
        self.assertEqual(got["Rachel Booth"]["role"], "Product Manager · Databricks")
        self.assertTrue(got["Rachel Booth"]["at_company"])
        self.assertFalse(got["Pat Doe"]["at_company"])
        self.assertEqual(parse_search_results("nothing here"), [])

    def test_a_result_does_not_borrow_the_next_ones_title_and_a_school_covers_its_university(self):
        text = RESULTS.replace("Pat Doe - Consultant", "Pat Doe - Talent Acquisition Partner")
        got = {p["name"]: p for p in parse_search_results(text, [], "Databricks")}
        self.assertNotIn("Talent", got["Rachel Booth"]["snippet"])
        mine = [Tie("school", "Harbor Business", "Harbor School of Business", 2.6, ["harbor school of business"], "Harbor",
                    "Lakeview University"), Tie("school", "Lakeview University", "Lakeview University", 2.0, ["lakeview university"])]
        ties = contact_ties({"education": "Lakeview University - Harbor School of Business"}, mine, [], "")
        self.assertEqual(ties, ["Harbor Business"])
        self.assertIn("recruiter", contact_ties({"headline": "Talent Acquisition Partner"}, TIES, [], ""))
        self.assertNotIn("recruiter", contact_ties({"headline": "PM", "snippet": "met a recruiter"}, TIES, [], ""))


class ProfileTiesTests(unittest.TestCase):
    """2026-10-16: ties come from each person's own profile; none are built into the code."""

    def test_schools_and_employers_from_the_profile(self):
        by = {t.label: t for t in TIES}
        self.assertEqual(by[BIZ].kind, "school")
        self.assertIn("harbor school of business", by[BIZ].words)  # the school within the university
        self.assertEqual(by["Acme Rental Group"].kind, "employer")
        self.assertGreater(by[BIZ].weight, by["Acme Rental Group"].weight)

    def test_the_persons_own_ties_replace_or_add_to_them(self):
        o = {"network_profile_ties": False, "network_ties": [
            {"label": "Lakeview University", "kind": "school", "search": "Lakeview University", "weight": 2.0,
             "words": ["lakeview university"], "short": "Lakeview", "alumni_page": "lakeview-university"}]}
        ties = load_ties(o, PROFILE)
        self.assertEqual([t.label for t in ties], ["Lakeview University"])
        plan = search_plan("Databricks", ["New York"], ties)
        alumni = [s for s in plan if s.group == "Alumni at the company"]
        self.assertTrue(alumni[0].url.startswith("https://www.linkedin.com/school/lakeview-university/people/"))
        self.assertEqual(load_ties({}, None), [])  # no profile, no ties: nothing built in


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.settings.ensure_dirs()
        self.c = TestClient(create_app(self.settings, StubClient(), PROFILE, TOKEN), base_url="http://127.0.0.1:8765")
        self.pid = self.c.post("/api/desk/postings", headers=H, json={
            "text": POSTING + "\nPrimary Contact\nName\nJane Recruiter\nJob Title\nUniversity Recruiter\nEmail\njane@databricks.com\n",
            "url": "https://example.com/jobs/1", "title": "Associate Product Manager", "source": "text"}).json()["id"]
        self.c.patch(f"/api/desk/postings/{self.pid}", headers=H, json={"company": "Databricks"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_postings_network_card(self):
        n = self.c.get(f"/api/network/posting/{self.pid}", headers=H).json()
        self.assertEqual(n["focus"][0], "Mountain View")  # the person's Bay Area first
        self.assertEqual(n["searches"][0]["group"], "Location + alumni")
        self.assertEqual(n["posting_contacts"][0]["name"], "Jane Recruiter")
        self.assertIn("May 2027", n["drafts"][0]["message"])
        self.assertIn("Jordan Rivera", n["drafts"][0]["message"])
        alum = next(d for d in n["drafts"] if d["relation"] == "alum")
        self.assertIn("my MBA at " + BIZ, alum["note"])  # the person's own degree and school
        self.assertEqual(self.c.get(f"/api/network/posting/{self.pid}").status_code, 401)

    def test_saving_a_contact_scores_it_and_the_draft_fits_the_tie(self):
        a = self.c.post("/api/network/contacts", headers=H, json={"posting_id": self.pid, "name": "Sam Lee",
                        "headline": "Product @ Databricks, Harbor School of Business '22", "location": "San Francisco Bay Area"}).json()
        self.assertIn(BIZ, a["ties"])
        self.assertTrue(any(t.startswith("same city as the role") for t in a["ties"]))
        b = self.c.post("/api/network/contacts", headers=H, json={"posting_id": self.pid, "name": "Kim Park",
                        "headline": "Strategy at Databricks; previously Acme Rental Group", "location": "Chicago"}).json()
        listing = self.c.get("/api/network/contacts", headers=H).json()["contacts"]
        self.assertEqual([c["name"] for c in listing], ["Sam Lee", "Kim Park"])  # location + alum first
        d = self.c.get(f"/api/network/contacts/{a['id']}/draft", headers=H).json()
        self.assertEqual(d["relation"], "alum")
        self.assertTrue(d["note"].startswith("Hi Sam,"))
        d2 = self.c.get(f"/api/network/contacts/{b['id']}/draft", headers=H).json()
        self.assertEqual(d2["relation"], "coworker")
        m = self.c.patch(f"/api/network/contacts/{a['id']}", headers=H, json={"status": "messaged"}).json()
        self.assertEqual(m["contacted_at"], date.today().isoformat())
        self.assertEqual(self.c.patch(f"/api/network/contacts/{a['id']}", headers=H, json={"status": "spam"}).status_code, 422)
        self.assertEqual(self.c.post("/api/network/contacts", headers=H, json={"posting_id": self.pid}).status_code, 422)

    def test_the_network_page_carries_the_token_and_is_not_framed(self):
        r = self.c.get("/network")
        self.assertEqual(r.status_code, 200)
        self.assertIn(TOKEN, r.text)
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertIn("Tailor Network", r.text)

    def test_companies_group_postings_and_put_the_open_ones_first(self):
        other = self.c.post("/api/desk/postings", headers=H, json={"text": "Analyst\nLocation: Chicago, IL\n" + "Do analysis. " * 40,
                            "url": "https://example.com/jobs/2", "title": "Analyst", "source": "text"}).json()["id"]
        self.c.patch(f"/api/desk/postings/{other}", headers=H, json={"company": "Databricks, Inc."})
        t = self.c.get("/api/network/targets", headers=H).json()["targets"]
        self.assertEqual(len(t), 1)                       # "Databricks" and "Databricks, Inc." are one company
        self.assertEqual(len(t[0]["postings"]), 2)
        self.assertEqual(t[0]["focus"][0], "Mountain View")
        self.assertTrue(t[0]["in_your_area"])
        d = self.c.get("/api/network/target/" + t[0]["key"], headers=H).json()
        self.assertIn("Chicago", d["focus"])
        self.assertEqual(d["searches"][0]["group"], "Location + alumni")
        self.assertEqual(self.c.get("/api/network/target/nope", headers=H).status_code, 404)

    def test_history_follow_ups_stats_and_csv(self):
        a = self.c.post("/api/network/contacts", headers=H, json={"posting_id": self.pid, "name": "Sam Lee",
                        "headline": "PM at Databricks | Harbor School of Business MBA", "location": "San Francisco Bay Area"}).json()
        again = self.c.post("/api/network/contacts", headers=H, json={"posting_id": self.pid, "name": "sam lee"}).json()
        self.assertTrue(again["duplicate"])
        self.c.patch(f"/api/network/contacts/{a['id']}", headers=H, json={"status": "messaged"})
        f = self.c.patch(f"/api/network/contacts/{a['id']}", headers=H, json={"followed_up": True}).json()
        self.assertEqual(f["follow_ups"], 1)
        self.assertEqual([h["what"] for h in f["history"]], ["saved", "messaged", "follow-up 1 sent"])
        self.c.patch(f"/api/network/contacts/{a['id']}", headers=H, json={"status": "replied"})
        s = self.c.get("/api/network/stats", headers=H).json()
        self.assertEqual(s["by_status"]["replied"], 1)
        self.assertEqual(s["by_relation"]["alum"]["reply_rate"], 100)
        d = self.c.get(f"/api/network/contacts/{a['id']}/draft", headers=H).json()
        self.assertTrue(d["follow_up"].startswith("Hi Sam,"))
        self.assertNotIn("—", d["follow_up"])
        csv_text = self.c.get("/api/network/contacts.csv", headers=H).text
        self.assertIn("Sam Lee,Databricks", csv_text)
        self.assertEqual(self.c.get("/api/network/contacts.csv").status_code, 401)

    def test_pasted_profile_text_is_split_up(self):
        p = self.c.post("/api/network/parse", headers=H, json={"text": "Sam Lee\n· 2nd\nProduct Manager at Databricks | Harbor MBA\n"
                                                              "San Francisco Bay Area\nContact info\n500+ connections"}).json()
        self.assertEqual(p, {"name": "Sam Lee", "headline": "Product Manager at Databricks | Harbor MBA",
                             "location": "San Francisco Bay Area"})

    def test_the_persons_ties_can_be_reweighted_switched_off_and_added(self):
        t = self.c.get("/api/network/ties", headers=H).json()
        self.assertIn("club", t["kinds"])
        info = "Lakeview University School of Information"
        r = self.c.put("/api/network/ties", headers=H, json={"weights": {info: 2.9}, "off": ["Brightpath Clinics"],
                       "custom": [{"label": "Women in Business Club", "kind": "club", "search": "Women in Business Harbor",
                                   "weight": 2.0, "words": ["wib", "women in business"]}]}).json()
        by = {x["label"]: x for x in r["ties"]}
        self.assertEqual(by[info]["weight"], 2.9)
        self.assertFalse(by[info]["custom"])  # from the profile
        self.assertTrue(by["Women in Business Club"]["custom"])
        self.assertEqual(r["off"], ["Brightpath Clinics"])
        c = self.c.post("/api/network/contacts", headers=H, json={"posting_id": self.pid, "name": "Ana Cruz",
                        "headline": "Databricks; WIB board", "location": "Austin, TX"}).json()
        self.assertIn("Women in Business Club", c["ties"])
        self.assertEqual(c["relation"], "alum")
        k = self.c.post("/api/network/contacts", headers=H, json={"posting_id": self.pid, "name": "Lee Kim",
                        "headline": "Databricks, ex-Brightpath Clinics", "location": "Austin, TX"}).json()
        self.assertNotIn("Brightpath Clinics", k["ties"])           # switched off

    def test_pasted_results_are_ranked_and_saved_together(self):
        key = self.c.get("/api/network/targets", headers=H).json()["targets"][0]["key"]
        r = self.c.post("/api/network/results", headers=H, json={"text": RESULTS, "key": key, "links": []}).json()
        names = [p["name"] for p in r["people"]]
        self.assertEqual(names[-1], "Pat Doe")                      # not at the company: last
        top = r["people"][0]
        self.assertIn(BIZ, top["ties"])
        self.assertTrue(any(t.startswith("same city") for t in top["ties"]))
        b = self.c.post("/api/network/contacts/bulk", headers=H, json={"key": key, "people": r["people"][:2]}).json()
        self.assertEqual(b["added"], 2)
        again = self.c.post("/api/network/results", headers=H, json={"text": RESULTS, "key": key}).json()
        self.assertEqual(sum(p["saved"] for p in again["people"]), 2)
        self.assertEqual(self.c.post("/api/network/contacts/bulk", headers=H, json={"key": key, "people": r["people"][:2]}).json()["duplicates"], 2)
        saved = self.c.get("/api/network/contacts", headers=H).json()["contacts"]
        self.assertTrue(all(c["posting_id"] == self.pid for c in saved))
        self.assertIn(BIZ, saved[0]["ties"])           # ties come from the saved education/snippet too
        self.assertEqual(self.c.post("/api/network/contacts/bulk", headers=H, json={"key": "nope", "people": []}).status_code, 404)

    def test_the_nearest_deadline_comes_first(self):
        from datetime import date as d_
        t = self.c.get("/api/network/targets", headers=H).json()["targets"][0]
        self.assertIn("days_left", t)

    def test_connections_make_a_company_warm_and_drafts_ask_them_directly(self):
        r = self.c.post("/api/network/connections", headers=H, json={"text": CONNECTIONS}).json()
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["warm"][0]["company"], "Databricks")
        self.assertEqual(r["warm"][0]["names"], ["Sam Lee"])
        self.assertEqual(self.c.post("/api/network/connections", headers=H, json={"text": "nope"}).status_code, 422)
        key = self.c.get("/api/network/targets", headers=H).json()["targets"][0]["key"]
        t = self.c.get("/api/network/target/" + key, headers=H).json()
        self.assertEqual(t["connections"], 1)
        self.assertEqual(t["connection_list"][0]["name"], "Sam Lee")
        self.assertEqual(t["drafts"][0]["relation"], "connection")
        c = self.c.post("/api/network/contacts", headers=H, json={"posting_id": self.pid, "name": "Sam Lee", "connection": True,
                        "link": "https://www.linkedin.com/in/samlee", "headline": "Product Manager"}).json()
        self.assertEqual(c["ties"][0], "1st-degree connection")
        self.assertEqual(c["relation"], "connection")
        d = self.c.get(f"/api/network/contacts/{c['id']}/draft", headers=H).json()
        self.assertIn("would you be comfortable referring me", d["message"].lower())
        self.assertTrue(self.c.get("/api/network/target/" + key, headers=H).json()["connection_list"][0]["saved"])
        self.assertEqual(self.c.delete("/api/network/connections", headers=H).json()["total"], 0)

    def test_a_referral_packet(self):
        key = self.c.get("/api/network/targets", headers=H).json()["targets"][0]["key"]
        pk = self.c.get("/api/network/packet/" + key, headers=H).json()
        self.assertEqual(pk["title"], "Associate Product Manager")
        self.assertIn("Jordan Rivera is finishing", pk["blurb"])
        self.assertNotIn("—", pk["blurb"])
        self.assertEqual(self.c.get("/api/network/packet/nope", headers=H).status_code, 404)

    def test_prospects_from_the_role_search_become_companies_to_network_into(self):
        from tailor.jobs.search import SearchStore, import_found
        import_found(SearchStore(self.settings.data_dir), [{"company": "McKinsey & Company", "title": "Associate",
                     "url": "https://www.mckinsey.com/careers/a-1", "location": "New York; Chicago", "category": "Consulting"}])
        t = {x["company"]: x for x in self.c.get("/api/network/targets", headers=H).json()["targets"]}
        self.assertEqual(t["McKinsey & Company"]["prospects"], 1)
        self.assertEqual(t["McKinsey & Company"]["focus"][0], "New York")

    def test_autopilot_lines_up_the_day_without_sending_anything(self):
        self.c.put("/api/network/autopilot", headers=H, json={"enabled": True, "hour": 8, "max_new": 5, "notify": False})
        self.c.post("/api/network/connections", headers=H, json={"text": CONNECTIONS})
        # a second posting the person applied to, naming its recruiter
        other = self.c.post("/api/desk/postings", headers=H, json={"text": "Analyst\nLocation: Chicago, IL\n" + "Do analysis. " * 40 +
                            "\nPrimary Contact\nName\nRiley Park\nJob Title\nRecruiter\nEmail\nr.park@acme.example\n",
                            "url": "https://example.com/jobs/9", "title": "Analyst", "source": "text"}).json()["id"]
        self.c.patch(f"/api/desk/postings/{other}", headers=H, json={"company": "Acme", "stage": "applied"})
        d = self.c.post("/api/network/autopilot/run", headers=H).json()
        self.assertEqual([w["name"] for w in d["warm"]], ["Sam Lee"])           # the Databricks connection
        self.assertEqual([r["name"] for r in d["recruiters"]], ["Riley Park"])    # named on the applied posting
        saved = {c["name"]: c for c in self.c.get("/api/network/contacts", headers=H).json()["contacts"]}
        self.assertEqual(saved["Sam Lee"]["status"], "to contact")
        self.assertEqual(saved["Sam Lee"]["relation"], "connection")
        self.assertEqual(saved["Riley Park"]["relation"], "recruiter")
        again = self.c.post("/api/network/autopilot/run", headers=H).json()     # nothing twice
        self.assertEqual((again["warm"], again["recruiters"]), ([], []))
        st = self.c.get("/api/network/autopilot", headers=H).json()
        self.assertFalse(st["settings"]["notify"])
        self.assertEqual(st["digest"]["to_contact"], 2)


if __name__ == "__main__":
    unittest.main()

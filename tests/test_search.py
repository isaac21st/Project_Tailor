"""Role search: the four feeds, the filters (with titles seen in real searches), ranking, merging runs, and the routes.
Every feed is faked with httpx.MockTransport: nothing here reaches the internet."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tailor.config import Settings  # noqa: E402
from tailor.fill.profile import Profile  # noqa: E402
from tailor.jobs import search  # noqa: E402
from tailor.jobs.search import Filter, Prefs, SearchStore, grouped, run_search, spread  # noqa: E402
from tailor.llm import StubClient  # noqa: E402
from tailor.server.app import create_app  # noqa: E402

RESUME = ("Product strategy, user research, data analysis, stakeholder presentations, operating-model design, SQL, "
          "prototyping, onboarding conversion, cross-functional teams, consulting engagements")
BODY = ("Minimum qualifications: experience with product strategy, user research and data analysis; working with "
        "cross-functional teams and stakeholders. Responsibilities: drive product development and onboarding.")


def feeds(greenhouse=None, lever=None, ashby=None, workday=None):
    """A fake internet: board name -> list of (title, location, text)."""
    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "boards-api.greenhouse.io" in u:
            board = u.split("/boards/")[1].split("/")[0]
            jobs = [{"title": t, "absolute_url": f"https://job-boards.greenhouse.io/{board}/jobs/{i}",
                     "location": {"name": loc}, "content": f"&lt;p&gt;{text}&lt;/p&gt;", "updated_at": "2026-09-01"}
                    for i, (t, loc, text) in enumerate((greenhouse or {}).get(board, []))]
            return httpx.Response(200 if board in (greenhouse or {}) else 404, json={"jobs": jobs})
        if "api.lever.co" in u:
            co = u.split("/postings/")[1].split("?")[0]
            return httpx.Response(200, json=[{"text": t, "hostedUrl": f"https://jobs.lever.co/{co}/{i}", "createdAt": 1790000000000,
                                              "categories": {"location": loc}, "descriptionPlain": text, "lists": []}
                                             for i, (t, loc, text) in enumerate((lever or {}).get(co, []))])
        if "api.ashbyhq.com" in u:
            org = u.split("/job-board/")[1].split("?")[0]
            return httpx.Response(200, json={"jobs": [{"title": t, "jobUrl": f"https://jobs.ashbyhq.com/{org}/{i}", "location": loc,
                                                       "isRemote": "Remote" in loc, "isListed": True, "descriptionPlain": text}
                                                      for i, (t, loc, text) in enumerate((ashby or {}).get(org, []))]})
        if "myworkdayjobs.com" in u:
            items = (workday or {}).get("acme", [])
            if req.method == "POST":
                return httpx.Response(200, json={"total": len(items), "jobPostings": [
                    {"title": t, "externalPath": f"/job/{i}"} for i, (t, _, _) in enumerate(items)]})
            i = int(u.rsplit("/", 1)[1])
            t, loc, text = items[i]
            return httpx.Response(200, json={"jobPostingInfo": {"title": t, "jobDescription": text, "location": loc.split(",")[0],
                                                                "country": {"descriptor": loc.split(",")[-1].strip()},
                                                                "externalUrl": f"https://acme.wd1.myworkdayjobs.com/Careers/job/{i}"}})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def prefs(**kw) -> Prefs:
    base = dict(companies={"greenhouse": ["acme"], "lever": [], "ashby": [], "workday": []})
    base.update(kw)
    return Prefs(**base).validated()


class FilterTests(unittest.TestCase):
    def test_titles_from_real_searches(self):
        f = Filter(prefs())
        keep = ["[2027] Associate Product Manager, Early Career", "Associate Product Manager (New Grad)",
                "2027 MBA University Graduate - Product Manager", "Rotational Development Program",
                "Business Operations Associate", "User Experience Researcher", "GTM Strategy & Operations Analyst"]
        drop = {"Researcher, Alignment": "AI research is not UX research",
                "NVIDIA 2027 Internships: Hardware ASIC Design": "internships and hardware",
                "Area Manager - New Grad": "warehouse management", "Member of Technical Staff (New Grad)": "engineering",
                "Talent Strategist": "recruiting", "Senior Product Manager, Growth": "too senior",
                "Group Product Manager, Verticals": "too senior", "Manager, Strategy and Business Operations": "mid-level",
                "Software Engineer, New Grad": "engineering", "Senior University Recruiter": "recruiting",
                "Product Design, Entry-Level (2027 start)": "design (unless asked for)",
                "Strategic Cuisines - Korean AE (Central)": "sales", "Field Marketing Specialist, Key & Strategic Accounts": "marketing",
                "Applied AI Architect, Digital Natives (Strategics)": "technical", "Business Development Associate, Early Career": "sales",
                "Solutions Consultant": "pre-sales", "Onboarding Consultant I, SMB - Bilingual Thai": "customer onboarding",
                "Content Creator & Strategist": "content"}
        for t in keep:
            self.assertIsNone(f.check_title(t), t)
        for t, why in drop.items():
            self.assertIsNotNone(f.check_title(t), f"{t} ({why})")

    def test_the_persons_words_win(self):
        f = Filter(prefs(include_words=["product design"], exclude_words=["crypto"]))
        self.assertIsNone(f.check_title("Product Design, Entry-Level (2027 start)"))
        self.assertIsNotNone(f.check_title("Associate Product Manager, Crypto"))

    def test_locations(self):
        f = Filter(prefs())
        for loc in ("San Mateo, CA, United States", "New York, NY", "Remote - United States", "Bellevue, Washington",
                    "US, CA, Santa Clara, United States of America", "Remote, US", "USA", ""):
            self.assertIsNone(f.check_location(loc), loc)
        for loc in ("Remote - Estonia", "Mexico City, MX", "Buenos Aires, Argentina", "London, UK", "Toronto, Canada",
                    "Israel - Hybrid (3 days/week in office with us)"):
            self.assertIsNotNone(f.check_location(loc), loc)
        ny = Filter(prefs(locations=["New York"], remote_ok=False))
        self.assertIsNone(ny.check_location("New York, NY"))
        self.assertIsNotNone(ny.check_location("San Francisco, CA"))
        self.assertIsNotNone(ny.check_location("Remote - United States"))

    def test_prefs_are_cleaned(self):
        p = Prefs(focus=["product", "astrology"], max_years=99, daily_hour=31,
                  companies={"greenhouse": ["Figma ", "../etc", "figma"], "workday": ["https://evil.example/x",
                             "https://acme.wd1.myworkdayjobs.com/Careers"]}).validated()
        self.assertEqual(p.focus, ["product"])
        self.assertEqual((p.max_years, p.daily_hour), (15, 23))
        self.assertEqual(p.companies["greenhouse"], ["figma", "etc"])
        self.assertEqual(p.companies["workday"], ["https://acme.wd1.myworkdayjobs.com/Careers"])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SearchStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_four_feeds_filter_rank_and_merge_runs(self):
        seven = BODY + " Minimum qualifications: a bachelor's degree plus seven (7) years of related experience."
        client = feeds(
            greenhouse={"acme": [("Associate Product Manager, New Grad", "New York, NY", BODY),
                                 ("Associate Product Manager, New Grad", "Seattle, WA", BODY),
                                 ("GTM Strategy & Operations Analyst", "New York, NY", seven),
                                 ("Software Engineer, New Grad", "New York, NY", BODY)]},
            lever={"beta": [("Business Operations Associate", "San Francisco, CA", BODY),
                            ("Business Operations Associate", "London, UK", BODY)]},
            ashby={"gamma": [("User Research Associate", "Remote (US)", BODY)]},
            workday={"acme": [("Strategy Consultant", "Chicago, United States of America", BODY),
                              ("Strategy Consultant", "Buenos Aires, Argentina", BODY)]})
        p = prefs(companies={"greenhouse": ["acme"], "lever": ["beta"], "ashby": ["gamma"],
                             "workday": ["https://acme.wd1.myworkdayjobs.com/Careers"]})
        s1 = run_search(p, self.store, RESUME, client=client)
        self.assertEqual(s1["read"], 9)  # 4 Greenhouse + 2 Lever + 1 Ashby + 2 Workday
        self.assertEqual(s1["left_out"].get("asks for more years"), 1)  # "seven (7) years"
        self.assertEqual(s1["left_out"].get("outside the U.S."), 2)
        rows = grouped(list(self.store.load()["results"].values()))
        titles = sorted(r["title"] for r in rows)
        self.assertEqual(titles, ["Associate Product Manager, New Grad", "Business Operations Associate",
                                  "Strategy Consultant", "User Research Associate"])
        apm = next(r for r in rows if r["title"].startswith("Associate Product"))
        self.assertEqual(len(apm["also"]), 1)  # New York and Seattle are one role
        self.assertTrue(all(0 < r["fit"] <= 100 for r in rows))
        self.assertTrue(all(r["source"] in {"greenhouse", "lever", "ashby", "workday"} for r in rows))

        # a second run: nothing new; a dismissed role stays dismissed; a role that disappears drops out
        data = self.store.load()
        rid = next(k for k, v in data["results"].items() if v["title"] == "Strategy Consultant")
        data["results"][rid]["dismissed"] = True
        self.store.save(data)
        client2 = feeds(greenhouse={"acme": [("Associate Product Manager, New Grad", "New York, NY", BODY)]},
                        workday={"acme": [("Strategy Consultant", "Chicago, United States of America", BODY)]})
        s2 = run_search(p, self.store, RESUME, client=client2)
        self.assertEqual(s2["new"], 0)
        after = self.store.load()["results"]
        self.assertTrue(after[rid]["dismissed"])
        self.assertEqual(sorted(v["title"] for v in after.values()), ["Associate Product Manager, New Grad", "Strategy Consultant"])

    def test_language_variants_are_one_role(self):
        rows = [{"id": str(i), "company": "sierra", "fit": 60, "title": t, "location": "SF", "url": f"u{i}",
                 "first_seen": "a", "dismissed": False, "desk_id": None}
                for i, t in enumerate(["Strategist, Agent Development", "Strategist, Agent Development (Spanish speaking)",
                                       "Strategist, Agent Development (Brazilian Portuguese speaking)"])]
        g = grouped(rows)
        self.assertEqual(len(g), 1)
        self.assertEqual(len(g[0]["also"]), 2)

    def test_spread_keeps_one_company_from_filling_the_top(self):
        rows = [{"company": "palantir", "fit": 90 - i, "title": f"Deployment Strategist {i}"} for i in range(6)]
        rows.append({"company": "roblox", "fit": 70, "title": "APM"})
        order = [r["company"] for r in spread(rows, per_company=3)]
        self.assertEqual(order[:4], ["palantir", "palantir", "palantir", "roblox"])


class DiscoverAndExplainTests(unittest.TestCase):
    def test_discover_suggests_only_new_boards_with_matching_roles(self):
        client = feeds(greenhouse={"acme": [("Associate Product Manager", "New York, NY", BODY)],
                                   "newco": [("Business Operations Associate", "Austin, TX", BODY),
                                             ("Software Engineer", "Austin, TX", BODY)],
                                   "salesy": [("Account Executive", "Austin, TX", BODY)]},
                       ashby={"aico": [("Product Manager", "San Francisco", BODY)]})
        out = search.discover(prefs(), client=client, candidates=["acme", "newco", "salesy", "aico", "ghost"])
        got = {(r["kind"], r["name"]): r["matching"] for r in out}
        self.assertEqual(got, {("greenhouse", "newco"): 1, ("ashby", "aico"): 1})  # acme is already searched

    def test_why_it_fits_names_real_resume_entries(self):
        resume = json.dumps({"sections": [
            {"heading": "EDUCATION", "entries": [{"title": "UNIVERSITY OF MICHIGAN", "roles": [{"title": "MBA", "bullets": ["product"]}]}]},
            {"heading": "PROFESSIONAL EXPERIENCE", "entries": [
                {"title": "SUMMIT-CLUB | CASE STUDY", "roles": [{"title": "Brand Strategy Associate", "bullets": [
                    "Owned product strategy for an onboarding flow"]}]},
                {"title": "UNIVERSITY OF MICHIGAN, SCHOOL OF INFORMATION", "roles": [{"title": "Research Associate", "bullets": [
                    "Coding findings from 80 studies into information architecture notes"]}]}]}]})
        entries = search.resume_entries(resume)
        self.assertEqual([n for n, _ in entries], ["Summit-Club", "University of Michigan"])
        job = {"title": "Associate Product Manager", "text": "Minimum qualifications: product management, product strategy, "
               "roadmaps and product development. Experience with software engineering and APIs."}
        why = search.explain(job, entries)
        self.assertTrue(any(w.startswith("Product: Summit-Club") for w in why["why"]), why)
        self.assertFalse(any("University of Michigan" in w and w.startswith("Technical") for w in why["why"]), why)


TOKEN = "search-token"
H = {"X-Tailor-Token": TOKEN}


class ConsultingFirmTitleTests(unittest.TestCase):
    """2026-10-06: Secretariat's "Associate, Damages & Valuations" and Compass Lexecon's "Analyst | Competition
    Practice" are consulting roles; at other companies a bare "Analyst" is not."""

    def test_firm_entry_titles(self):
        f = Filter(Prefs(focus=["consulting"]))
        self.assertIsNone(f.check_title("Associate, Damages & Valuations", "secretariatadvisorsllc"))
        self.assertIsNone(f.check_title("Analyst | Competition Practice | New York", "fticonsulting"))
        self.assertIsNotNone(f.check_title("Associate Director, Construction Delay", "secretariatadvisorsllc"))
        self.assertIsNotNone(f.check_title("Associate, Damages & Valuations", "stripe"))
        for wrong in ("Associate, PhD - Early Decision (Economics Focus) (2027)", "Billing Analyst",
                      "Associate, Construction Delay"):
            self.assertIsNotNone(f.check_title(wrong, "thebrattlegroup"), wrong)


class DeskLinkTests(unittest.TestCase):
    """2026-10-02: roles saved from the extension or by link stayed "open" in Find roles and could be prepared twice."""

    def test_same_opening_under_different_links(self):
        k = search.job_key
        self.assertEqual(k("https://stripe.com/jobs/search?gh_jid=7737124"),
                         k("https://job-boards.greenhouse.io/stripe/jobs/7737124"))
        self.assertEqual(k("https://boards.greenhouse.io/robinhood/jobs/8199973?t=gh_src=&gh_jid=8199973"), "gh:8199973")
        self.assertNotEqual(k("https://job-boards.greenhouse.io/flexport/jobs/8224715"),
                            k("https://job-boards.greenhouse.io/flexport/jobs/8172300"))
        self.assertEqual(k("https://www.example.com/careers/role-1/?src=x"), "example.com/careers/role-1")
        self.assertEqual(k("https://job-boards.greenhouse.io/embed/job_app?for=stripe&token=7737124"), "gh:7737124")

    def test_link_desk(self):
        results = {"a": {"url": "https://stripe.com/jobs/search?gh_jid=7737124", "desk_id": None},
                   "b": {"url": "https://job-boards.greenhouse.io/flexport/jobs/8172300", "desk_id": None},
                   "c": {"url": "https://x.com/1", "desk_id": "kept"}}
        desk = [{"id": "d1", "url": "https://job-boards.greenhouse.io/stripe/jobs/7737124"}, {"id": "d2", "url": ""}]
        self.assertEqual(search.link_desk(results, desk), 1)
        self.assertEqual([results[r]["desk_id"] for r in "abc"], ["d1", None, "kept"])


GOOGLE_PAGE = """<ul><li><div><h3 class="t">Commercial Strategy and Operations Associate</h3><span>corporate_fare</span>
<span>Google</span><span>place</span><span>New York, NY, USA</span><span>bar_chart</span><span>Early</span>
<ul><li>Bachelor's degree or equivalent practical experience.</li></ul>
<a href="jobs/results/87964979893805766-commercial-strategy">Learn more</a></div></li>
<li><div><h3>Customer Engineer, Google Cloud (English, Korean)</h3><span>Seoul, South Korea</span>
<a href="jobs/results/111-customer">Learn more</a></div></li></ul>"""
BCG_PAGE = ("<script>phApp.ddo = " + json.dumps({"eagerLoadRefineSearch": {"totalHits": 2, "data": {"jobs": [
    {"title": "Consultant, Advanced Degree, United States", "city": "Atlanta", "country": "United States of America",
     "jobSeqNo": "BCG1US58829", "descriptionTeaser": "Join BCG as a consultant."},
    {"title": "Consultant", "city": "Paris", "country": "France", "jobSeqNo": "BCG1FR1", "descriptionTeaser": "x"}]}}})
    + "; phApp.other = 1;</script>")
NEWGRAD_MD = """## 💻 Software Engineering New Grad Roles
<tr><td>Acme</td><td>Software Engineer</td><td>SF</td><td><a href="https://x/1">Apply</a></td><td>1d</td></tr>
## 📱 Product Management New Grad Roles
<table><tbody>
<tr><td><strong><a href="https://simplify.jobs/c/IXL">IXL Learning</a></strong></td><td>Associate Product Manager New Grad</td>
<td>San Mateo, CA</td><td><a href="https://www.ixl.com/company/jobs?gh_jid=8862211002&utm_source=Simplify&ref=Simplify">Apply</a></td><td>6d</td></tr>
<tr><td><strong>Uncountable</strong></td><td>Product Manager – New Grad</td><td>SF</br>NYC</td>
<td><a href="https://jobs.ashbyhq.com/uncountable/1f84?utm_source=Simplify&ref=Simplify">Apply</a></td><td>6mo</td></tr>
</tbody></table>
<summary>🗃️ Inactive roles (32)</summary>
<tr><td>Old Co</td><td>Old PM</td><td>NYC</td><td><a href="https://old/1">Apply</a></td><td>9mo</td></tr>
"""


def sites_client(google=GOOGLE_PAGE, bcg=BCG_PAGE, newgrad=NEWGRAD_MD):
    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "google.com/about/careers" in u:
            return httpx.Response(200, text=google if "page=1" in u and "q=MBA" in u else "<html></html>")
        if "careers.bcg.com" in u:
            return httpx.Response(200, text=bcg if "keywords=consultant" in u and "from=0" in u else "<html></html>")
        if "raw.githubusercontent.com" in u:
            return httpx.Response(200, text=newgrad)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


class SiteFeedTests(unittest.TestCase):
    """Careers sites read as a visitor would (2026-10-14): Google early-career, BCG, the public new-grad list."""

    def test_google_keeps_us_roles_with_their_cities(self):
        jobs = search.google_careers(sites_client(), ["MBA"], pages=1)
        self.assertEqual([(j["title"], j["location"]) for j in jobs],
                         [("Commercial Strategy and Operations Associate", "New York, NY, USA")])
        self.assertTrue(jobs[0]["url"].endswith("/87964979893805766"))
        self.assertEqual(search.company_name(jobs[0]), "Google")

    def test_bcg_reads_the_embedded_results_and_keeps_the_us(self):
        jobs = search.bcg_careers(sites_client(), ("consultant",))
        self.assertEqual([j["url"] for j in jobs], ["https://careers.bcg.com/global/en/job/BCG1US58829"])
        self.assertEqual(search.company_name(jobs[0]), "BCG")

    def test_newgrad_list_reads_only_active_product_roles(self):
        jobs = search.newgrad_pm(sites_client())
        self.assertEqual([(j["company"], j["title"]) for j in jobs],
                         [("IXL Learning", "Associate Product Manager New Grad"), ("Uncountable", "Product Manager – New Grad")])
        self.assertEqual(jobs[0]["url"], "https://www.ixl.com/company/jobs?gh_jid=8862211002")  # tracking removed
        self.assertIsNone(Filter(prefs()).check_location(jobs[1]["location"]))  # "SF; NYC" is the U.S.

    def test_old_prefs_get_the_new_feed_and_bad_names_are_dropped(self):
        p = Prefs(companies={"greenhouse": ["acme"], "workday": []}).validated()
        self.assertEqual(p.companies["sites"], ["google", "bcg", "newgrad_pm"])
        p = Prefs(companies={"sites": ["google", "evil"]}).validated()
        self.assertEqual(p.companies["sites"], ["google"])


class DiscoverWorkdayTests(unittest.TestCase):
    def test_sites_with_matching_graduate_roles_are_suggested(self):
        client = feeds(workday={"acme": [("2027 MBA Leadership Development Program", "Minneapolis, US", BODY),
                                         ("Warehouse Associate", "Dallas, US", BODY)]})
        got = search.discover_workday(prefs(), client, ["acme.wd1/Careers"])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "https://acme.wd1.myworkdayjobs.com/Careers")
        self.assertEqual(got[0]["sample"], ["2027 MBA Leadership Development Program"])
        have = prefs(companies={"greenhouse": [], "workday": ["https://acme.wd1.myworkdayjobs.com/Careers"]})
        self.assertEqual(search.discover_workday(have, client, ["acme.wd1/Careers"]), [])


class WeeklyDiscoverTests(unittest.TestCase):
    def test_boards_with_two_matches_are_added_once_a_week(self):
        from datetime import datetime as dt
        from unittest import mock
        from tailor.server import search_routes

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SearchStore(Path(tmp.name))
        sugg = [{"kind": "greenhouse", "name": "newco", "company": "Newco", "openings": 9, "matching": 3, "sample": []},
                {"kind": "greenhouse", "name": "thin", "company": "Thin", "openings": 9, "matching": 1, "sample": []}]
        with mock.patch.object(search_routes, "discover", return_value=sugg), \
                mock.patch.object(search_routes, "discover_workday", return_value=[]):
            picks = search_routes.weekly_discover(store, dt(2026, 10, 14, 9))
            again = search_routes.weekly_discover(store, dt(2026, 10, 16, 9))  # two days later: not yet
        self.assertEqual([p["name"] for p in picks], ["newco"])
        self.assertEqual(again, [])
        self.assertIn("newco", store.prefs().companies["greenhouse"])
        self.assertNotIn("thin", store.prefs().companies["greenhouse"])
        self.assertEqual(store.load()["discovered"]["added"][0]["company"], "Newco")


class FoundRolesTests(unittest.TestCase):
    def test_found_roles_are_kept_across_runs_and_ranked_once_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SearchStore(Path(tmp.name))
        got = search.import_found(store, [
            {"company": "McKinsey & Company", "title": "Associate", "url": "https://www.mckinsey.com/careers/a-1",
             "location": "US offices", "category": "Consulting", "note": "core MBA hire"},
            {"company": "Bad", "title": "x", "url": "javascript:alert(1)"}])
        self.assertEqual(got, {"added": 1})
        self.assertEqual(search.import_found(store, [{"company": "McKinsey & Company", "title": "Associate",
                                                      "url": "https://www.mckinsey.com/careers/a-1"}]), {"added": 0})
        run_search(prefs(), store, RESUME, client=feeds(greenhouse={"acme": []}))
        rec = next(iter(store.load()["results"].values()))
        self.assertEqual((rec["source"], search.company_name(rec)), ("found", "McKinsey & Company"))
        search.rank_found(store, rec["id"], BODY * 3 + " Requires 2+ years of experience.", RESUME)
        rec = store.load()["results"][rec["id"]]
        self.assertTrue(rec["checked"])
        self.assertGreater(rec["fit"], 0)
        self.assertEqual(rec["why"][0], "core MBA hire")
        self.assertEqual(rec["years"], 2)
        search.import_found(store, [{"company": "Ford", "title": "FBLP", "url": "https://lakeviewharbor.12twenty.com/jobPostings#/jobPostings/1",
                                     "deadline": "2026-10-16"}, {"company": "X", "title": "Y", "url": "https://x/2", "deadline": "soon"}])
        dues = {r["company"]: r["deadline"] for r in store.load()["results"].values()}
        self.assertEqual((dues["Ford"], dues["X"]), ("2026-10-16", None))


class EarlyAnywhereTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SearchStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_grad_programs_outside_the_persons_cities_are_kept_and_marked(self):
        grad = BODY + " This MBA Leadership Development Program is for recent graduates."
        client = feeds(greenhouse={"acme": [("Strategy Associate", "Minneapolis, MN", grad),
                                            ("Business Operations Analyst", "Minneapolis, MN", BODY),
                                            ("Business Operations Associate", "New York, NY", BODY)]})
        p = prefs(locations=["New York"], companies={"greenhouse": ["acme"], "sites": []})
        s = run_search(p, self.store, RESUME, client=client)
        rows = {r["title"]: r for r in self.store.load()["results"].values()}
        self.assertTrue(rows["Strategy Associate"]["outside_cities"])
        self.assertTrue(rows["Strategy Associate"]["early"])
        self.assertFalse(rows["Business Operations Associate"].get("outside_cities", False))
        self.assertNotIn("Business Operations Analyst", rows)  # not a graduate role: the city rule holds
        self.assertEqual(s["left_out"].get("not in your locations"), 1)
        p.early_anywhere = False
        run_search(p, self.store, RESUME, client=client)
        self.assertNotIn("Strategy Associate", {r["title"] for r in self.store.load()["results"].values()})

    def test_senior_consulting_titles_and_sales_are_left_out(self):
        f = Filter(prefs())
        self.assertEqual(f.check_title("Expert Consultant /Project Leader, CFO Excellence"), "too senior")
        self.assertEqual(f.check_title("Consultant, Experienced Hire, United States"), "too senior")
        self.assertEqual(f.check_title("Account Strategist, Google Marketing Platform"), "different kind of role")
        self.assertIsNone(f.check_title("Consultant, Advanced Degree, United States"))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.settings.ensure_dirs()
        self.c = TestClient(create_app(self.settings, StubClient(), Profile(first_name="J", last_name="D", email="j@x.io",
                                                                            phone="5"), TOKEN), base_url="http://127.0.0.1:8765")

    def tearDown(self):
        self.tmp.cleanup()

    def test_needs_the_token(self):
        for method, path in [("GET", "/api/search/prefs"), ("PUT", "/api/search/prefs"), ("POST", "/api/search/run"),
                             ("GET", "/api/search/results"), ("POST", "/api/search/results/x/save")]:
            self.assertEqual(self.c.request(method, path, json={}).status_code, 401, path)

    def test_prefs_round_trip(self):
        got = self.c.get("/api/search/prefs", headers=H).json()
        self.assertIn("product", got["prefs"]["focus"])
        self.assertIn("ux_research", got["focus_labels"])
        body = dict(got["prefs"], focus=["consulting"], locations=["Chicago"], max_years=2)
        saved = self.c.put("/api/search/prefs", json=body, headers=H).json()["prefs"]
        self.assertEqual((saved["focus"], saved["locations"], saved["max_years"]), (["consulting"], ["Chicago"], 2))
        self.assertEqual(self.c.put("/api/search/prefs", json=dict(body, focus=["astrology"]), headers=H).status_code, 422)

    def test_results_dismiss_and_save_to_the_desk(self):
        store = SearchStore(self.settings.data_dir)
        run_search(prefs(), store, RESUME, client=feeds(greenhouse={"acme": [
            ("Associate Product Manager, New Grad", "New York, NY", BODY), ("Business Operations Associate", "Austin, TX", BODY)]}))
        rows = self.c.get("/api/search/results", headers=H).json()["rows"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["new"] and "text" not in r for r in rows))
        apm, ops = sorted(rows, key=lambda r: r["title"])
        self.c.post(f"/api/search/results/{ops['id']}/dismiss", headers=H)
        self.assertEqual([r["title"] for r in self.c.get("/api/search/results", headers=H).json()["rows"]], [apm["title"]])
        self.assertEqual(len(self.c.get("/api/search/results?show=dismissed", headers=H).json()["rows"]), 1)
        posting = self.c.post(f"/api/search/results/{apm['id']}/save", json={"tailor": False}, headers=H).json()
        self.assertEqual(posting["company"], "Acme")
        self.assertIn("Minimum qualifications", self.c.get(f"/api/desk/postings/{posting['id']}", headers=H).json()["text"])
        self.assertEqual(self.c.get("/api/search/results", headers=H).json()["rows"], [])  # saved and dismissed are reviewed
        self.assertEqual(len(self.c.get("/api/search/results?show=saved", headers=H).json()["rows"]), 1)
        self.assertEqual(self.c.post("/api/search/results/nope/save", json={}, headers=H).status_code, 404)

    def test_each_search_prepares_the_best_new_roles(self):
        import time
        from unittest import mock
        from tailor.server import search_routes

        clearance = BODY + " An active security clearance is required."
        client = feeds(greenhouse={"acme": [("Associate Product Manager, New Grad", "New York, NY", BODY),
                                            ("Business Operations Associate", "Austin, TX", BODY),
                                            ("Strategy Associate, US Government", "Washington, DC", clearance)]})
        real = search_routes.run_search
        self.c.put("/api/search/prefs", headers=H, json=dict(self.c.get("/api/search/prefs", headers=H).json()["prefs"],
                   auto_save=True, auto_max=1, auto_min_fit=0, companies={"greenhouse": ["acme"]}))

        def run_once():
            with mock.patch.object(search_routes, "run_search", lambda p, st, rt, cb: real(p, st, rt, cb, client=client)):
                self.c.post("/api/search/run", headers=H)
                for _ in range(100):
                    time.sleep(0.05)
                    if not self.c.get("/api/search/status", headers=H).json()["running"]:
                        break
            return self.c.get("/api/search/status", headers=H).json()["last"]

        last = run_once()
        self.assertEqual(len(last["prepared"]), 1)  # the limit
        desk = self.c.get("/api/desk/postings", headers=H).json()
        self.assertEqual(len(desk), 1)
        self.assertNotIn("Government", desk[0]["title"])  # clearance roles are never prepared automatically
        again = run_once()  # nothing is new the second time: nothing more is prepared
        self.assertEqual(again.get("prepared", []), [])
        self.assertEqual(len(self.c.get("/api/desk/postings", headers=H).json()), 1)

    def test_add_suggested_companies(self):
        r = self.c.post("/api/search/companies", json={"add": [{"kind": "ashby", "name": "decagon"},
                                                                {"kind": "workday", "name": "x"}, {"kind": "lever", "name": ""}]},
                        headers=H).json()
        self.assertIn("decagon", r["prefs"]["companies"]["ashby"])
        self.assertNotIn("x", r["prefs"]["companies"]["workday"])  # Workday takes a careers-site link, added by hand
        # the default Greenhouse list is already 50 long: more still fit (a 50 cap once dropped them silently)
        many = [{"kind": "greenhouse", "name": f"co{i}"} for i in range(20)]
        r = self.c.post("/api/search/companies", json={"add": many}, headers=H).json()
        self.assertIn("co19", r["prefs"]["companies"]["greenhouse"])


if __name__ == "__main__":
    unittest.main()


class UndergradOnlyTests(unittest.TestCase):
    def test_bachelors_only_campus_roles_are_left_out(self):
        from tailor.jobs.search import undergrad_only
        self.assertTrue(undergrad_only("Required Qualifications Bachelor’s degree in progress, with an expected graduation "
                                       "date between December 2026 and August 2027. Minimum cumulative GPA of 3.0"))
        self.assertFalse(undergrad_only("Currently pursuing a Bachelor's or Master's degree in Information Systems"))
        self.assertFalse(undergrad_only("Minimum Degree Status: Master's degree (Must still be enrolled in an accredited "
                                        "graduate degree program through Fall 2026)"))


class PostingUrlTests(unittest.TestCase):
    def test_an_application_form_link_leads_to_its_posting_page(self):
        # Simon-Kucher (Cornerstone, 2026-10-10): the 9-step form never shows the job description
        form = "https://simon-kucher.csod.com/ux/ats/careersite/6/requisition/4397/application?c=simon-kucher&jobboardid=0#3"
        page = "https://simon-kucher.csod.com/ux/ats/careersite/6/home/requisition/4397?c=simon-kucher"
        self.assertEqual(search.posting_url(form), page)
        self.assertEqual(search.job_key(form), search.job_key(page))
        self.assertEqual(search.posting_url("https://acme.com/careers/jobs/123/apply"), "https://acme.com/careers/jobs/123")
        self.assertEqual(search.job_key("https://acme.com/careers/jobs/123/apply"), search.job_key("https://acme.com/careers/jobs/123"))
        self.assertEqual(search.posting_url("https://acme.com/careers/jobs/123"), "")


class HashRouteKeyTests(unittest.TestCase):
    def test_postings_routed_after_the_hash_are_different_jobs(self):
        a = "https://lakeviewharbor.12twenty.com/jobPostings#/jobPostings/35006705524687"
        b = "https://lakeviewharbor.12twenty.com/jobPostings#/jobPostings/35006705629485?p=false"
        self.assertNotEqual(search.job_key(a), search.job_key(b))
        self.assertEqual(search.job_key(a), search.job_key(a + "/application?r=&p=false"))
        self.assertEqual(search.job_key("https://x.com/jobs/1#top"), search.job_key("https://x.com/jobs/1"))

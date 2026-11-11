"""Workday's "My Experience" step, end to end: the real extension in Chromium, the real server, a copy of the step built
from what Adobe's page showed on 2026-10-02 (entry ids with Workday's own counter, "From"/"To" month and year boxes,
a Degree list button, "Add Another" per group). The work and education entries are the part that most needs to be
right: one entry per resume job (never a project), the right dates, and the missing entries added.
"""
from __future__ import annotations

import http.server
import json
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402
from ext_helpers import start_or_clean_up  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tailor.config import Settings, apply_environment, load_settings  # noqa: E402
from tailor.fill.profile import Education, Experience, Profile, YearMonth  # noqa: E402
from tailor.llm import StubClient  # noqa: E402
from tailor.server.app import create_app  # noqa: E402

EXT = Path(__file__).resolve().parent.parent / "extension"
TOKEN = "wd-e2e-token"
HOST = "acme.wd5.myworkdayjobs.com"  # mapped to 127.0.0.1: the extension's Workday-only steps need a Workday name
YM = YearMonth
PROFILE = Profile(
    first_name="Jane", last_name="Doe", email="jane@example.com", phone="(555) 010-0000",
    linkedin_url="https://www.linkedin.com/in/janedoe",
    experience=[
        Experience(employer="University of Michigan", title="Research Associate", location="Ann Arbor, MI",
                   start=YM(year=2026, month=5), current=True, bullets=["Ran interviews with 30 users"]),
        Experience(employer="Summit Club", title="Brand Strategy Associate", location="Miami, FL",
                   start=YM(year=2025, month=6), end=YM(year=2025, month=9), bullets=["Built a brand plan"]),
        Experience(employer="Lakeside Health", title="Operations Consultant", location="Detroit, MI",
                   start=YM(year=2026, month=3), end=YM(year=2026, month=4), kind="project"),
        Experience(employer="Acme Rental Group", title="Senior Sales Associate", location="San Jose, CA",
                   start=YM(year=2023, month=6), end=YM(year=2024, month=5), bullets=["Led the counter team"]),
        Experience(employer="Acme Rental Group", title="Sales Associate", location="San Jose, CA",
                   start=YM(year=2022, month=6), end=YM(year=2023, month=6), bullets=["Sold upgrades"]),
        Experience(employer="Operations Center", title="Analytics Lead", location="Ann Arbor, MI", kind="project"),
    ],
    education=[
        Education(school="University of Michigan, Harbor School of Business", degree="Master of Business Administration",
                  field="Data and Business Analytics", form_field="Business Administration", start=YM(year=2025, month=8), end=YM(year=2027, month=5), expected=True),
        Education(school="University of Michigan School of Information", degree="Master of Science",
                  field="Information, User Experience Research and Design", form_field="Information Technology",
                  start=YM(year=2024, month=8),
                  end=YM(year=2027, month=5), expected=True),
        Education(school="University of California, Davis", degree="Bachelor of Arts", field="Cultural History",
                  form_field="History",
                  start=YM(year=2018, month=9), end=YM(year=2021, month=6)),
    ],
)

PAGE = """<!doctype html><html><head><title>My Experience - Workday</title></head><body>
<div data-automation-id="applyFlowPage">
<div data-automation-id="progressBarActiveStep">current step 2 of 5 My Experience</div>
<h3>My Experience</h3>
<div data-automation-id="formField-source"><label for="source--source">How Did You Hear About Us?*</label>
  <input id="source--source" data-uxi-widget-type="selectinput" data-uxi-multiselect-id="ms-source--source" placeholder="Search"></div>
<div role="group" id="we"><h4>Work Experience</h4><div id="we-list"></div>
  <button type="button" data-automation-id="add-button" id="add-we">Add Another</button></div>
<div role="group" id="ed"><h4>Education</h4><div id="ed-list"></div>
  <button type="button" data-automation-id="add-button" id="add-ed">Add Another</button></div>
<div role="group" id="ws"><h4>Websites</h4><div id="ws-list"></div>
  <button type="button" data-automation-id="add-button" id="add-ws">Add</button></div>
<div role="group" id="ce"><h4>Certifications</h4>
  <button type="button" data-automation-id="add-button" id="add-ce">Add</button></div>
<div role="group" id="resume-group"><h4>Resume/CV</h4><input type="file" id="resume" data-automation-id="file-upload-input-ref"></div>
<div data-automation-id="formField-linkedInAccount"><label for="socialNetworkAccounts--linkedInAccount">LinkedIn</label>
  <input id="socialNetworkAccounts--linkedInAccount"></div>
</div>
<button type="button" data-automation-id="pageFooterNextButton" id="next">Save and Continue</button>
<script>
window.__pressed = [];
let counter = 4, works = 0, schools = 0;  // Workday's own counter: the first entry is not "0"
const DEGREES = ["Select One", "GED", "High School", "Associates", "Bachelors", "Masters", "Doctorate", "JD"];  // Adobe's
function field(id, label, extra) {
  return `<div data-automation-id="formField-${id.split("--")[1]}"><label for="${id}">${label}</label><input id="${id}" ${extra || ""}></div>`;
}
// Huron's site (2026-10-28): its date parts sit deeper under the "From" heading, and the resume box is labelled
// only "Upload a file (5MB max)"
const HURON = location.pathname.startsWith("/empty");
function dates(prefix, legend, parts) {
  const inputs = parts.map((p) => `<input id="${prefix}-dateSection${p}-input" role="spinbutton" data-automation-id="dateSection${p}-input" aria-label="${p}">`).join("");
  return `<fieldset data-automation-id="formField-${prefix.split("--")[1]}"><legend><label><span>${legend.replace("*", "")}<abbr>*</abbr></span></label></legend>` +
    (HURON ? `<div><div><div id="${prefix}" role="group"><div>${inputs}</div></div></div></div>` : inputs) + "</fieldset>";
}
if (HURON) document.getElementById("resume-group").innerHTML = `<div data-automation-id="formField-"><p>Upload a file (5MB max)*</p>` +
  `<div data-automation-id="attachments-FileUpload"><input type="file" id="resume" data-automation-id="file-upload-input-ref"></div><div id="uploaded"></div></div>`;
// like Huron: an uploaded file is listed under the box and the box is emptied; a text area keeps only typing
window.__uploads = 0;
if (HURON) document.addEventListener("change", (e) => {
  if (e.target.id !== "resume" || !e.target.files.length) return;
  window.__uploads += e.target.files.length;
  document.getElementById("uploaded").insertAdjacentHTML("beforeend", `<div>${e.target.files[0].name} Successfully Uploaded!</div>`);
  e.target.value = "";
}, true);
if (HURON) document.addEventListener("input", (e) => {
  const el = e.target; if (el.tagName !== "TEXTAREA") return;
  if (e.inputType === "insertText" || e.inputType === "insertFromPaste") el.dataset.kept = el.value;
  else setTimeout(() => { el.value = el.dataset.kept || ""; }, 0);
}, true);
function addWork() {
  const n = counter; counter += 7; works++;
  const d = document.createElement("div");
  d.innerHTML = `<h4>Work Experience ${works}</h4>` + field(`workExperience-${n}--jobTitle`, "Job Title*", "aria-required=true") +
    field(`workExperience-${n}--companyName`, "Company*", "aria-required=true") + field(`workExperience-${n}--location`, "Location") +
    `<div data-automation-id="formField-currentlyWorkHere"><input type="checkbox" id="workExperience-${n}--currentlyWorkHere">` +
    `<label for="workExperience-${n}--currentlyWorkHere">I currently work here</label></div>` +
    dates(`workExperience-${n}--startDate`, "From*", ["Month", "Year"]) + dates(`workExperience-${n}--endDate`, "To*", ["Month", "Year"]) +
    `<div data-automation-id="formField-roleDescription"><label for="workExperience-${n}--roleDescription">Role Description</label>` +
    `<textarea id="workExperience-${n}--roleDescription"></textarea></div>`;
  document.getElementById("we-list").appendChild(d);
}
// Workday's search box: Enter searches, a result clicked becomes a tag under the box (as on Adobe's form)
const LISTS = { schoolName: ["University of Michigan", "University of Michigan - Dearborn", "University of California, Davis",
                             "University of California, Berkeley"],
                fieldOfStudy: ["Business Administration/Management", "Business Analytics", "Information Science",
                               "Information Technology", "History", "Biological History"] };
function prompt(id, label) {
  const kind = id.split("--")[1];
  return `<div data-automation-id="formField-${kind}"><label for="${id}">${label}</label>` +
    `<input id="${id}" data-uxi-widget-type="selectinput" data-uxi-multiselect-id="ms-${id}" placeholder="Search"></div>`;
}
document.addEventListener("keydown", (e) => {
  const input = e.target; if (e.key !== "Enter" || input.getAttribute("data-uxi-widget-type") !== "selectinput") return;
  document.querySelectorAll(".results").forEach((l) => l.remove());
  const q = input.value.toLowerCase(), list = LISTS[input.id.split("--")[1]] || [];
  const found = list.filter((x) => x.toLowerCase().includes(q));
  const tagIt = (item) => {
    input.parentElement.querySelectorAll(".tag").forEach((t) => t.remove());  // one answer: a new one replaces it
    const tag = document.createElement("div"); tag.setAttribute("data-uxi-widget-type", "selectinputlistitem");
    tag.setAttribute("data-uxi-multiselect-id", "ms-" + input.id); tag.className = "tag"; tag.textContent = item;
    input.parentElement.appendChild(tag); input.value = "";
  };
  if (found.length === 1) return tagIt(found[0]);  // like Workday: one match is picked on Enter, no list
  const box = document.createElement("div"); box.className = "results"; box.setAttribute("role", "listbox");
  for (const item of found) {
    const o = document.createElement("div"); o.setAttribute("role", "option"); o.setAttribute("data-automation-id", "menuItem");
    o.innerHTML = `<div data-automation-id="promptLeafNode">${item}</div>`;
    o.addEventListener("click", () => { tagIt(item); box.remove(); });
    box.appendChild(o);
  }
  input.parentElement.appendChild(box);
});
function addSchool() {
  const n = counter; counter += 7; schools++;
  const d = document.createElement("div");
  d.innerHTML = `<h4>Education ${schools}</h4>` + prompt(`education-${n}--schoolName`, "School or University*") +
    `<div data-automation-id="formField-degree"><label for="education-${n}--degree">Degree*</label>` +
    `<button type="button" id="education-${n}--degree" aria-haspopup="listbox" aria-required="true">Select One</button></div>` +
    prompt(`education-${n}--fieldOfStudy`, "Field of Study") + field(`education-${n}--gradeAverage`, "Overall Result (GPA)") +
    dates(`education-${n}--firstYearAttended`, "From", ["Year"]) + dates(`education-${n}--lastYearAttended`, "To (Actual or Expected)", ["Year"]);
  document.getElementById("ed-list").appendChild(d);
}
// Workday's date parts take typing only (Adobe, 2026-10-03): a value set by script is thrown back out
document.addEventListener("input", (e) => {
  const el = e.target; if (el.getAttribute("role") !== "spinbutton") return;
  if (e.inputType === "insertText") el.dataset.kept = el.value; else el.value = el.dataset.kept || "";
}, true);
// Huron's site (2026-10-28) starts with no entry at all, only the Add buttons
if (!location.pathname.startsWith("/empty")) { addWork(); addSchool(); }
// "How Did You Hear About Us?" in categories (Huron): the search finds nothing; the list opens on a press of the box,
// a category shows its own options, an option clicked becomes the tag
const SOURCES = { "Job Board": ["Indeed", "Glassdoor"], "Social Media": ["Facebook", "LinkedIn", "Twitter"], "Referral": ["Employee Referral"] };
function sourceList(items, leaf) {
  document.querySelectorAll(".srclist").forEach((l) => l.remove());
  const input = document.getElementById("source--source");
  const box = document.createElement("div"); box.className = "srclist"; box.setAttribute("role", "listbox");
  for (const item of items) {
    const o = document.createElement("div"); o.setAttribute("role", "option"); o.setAttribute("data-automation-id", "menuItem");
    o.innerHTML = leaf ? `<div data-automation-id="promptLeafNode">${item}</div>` : `<div data-automation-id="promptOption">${item}</div>`;
    o.addEventListener("click", () => {
      window.__pressed.push("src:" + item);
      if (!leaf) return sourceList(SOURCES[item], true);
      const tag = document.createElement("div"); tag.setAttribute("data-uxi-widget-type", "selectinputlistitem");
      tag.setAttribute("data-uxi-multiselect-id", "ms-source--source"); tag.className = "srctag"; tag.textContent = item;
      input.parentElement.appendChild(tag); box.remove();
    });
    box.appendChild(o);
  }
  input.parentElement.appendChild(box);
}
document.getElementById("source--source").addEventListener("mousedown", () => sourceList(Object.keys(SOURCES), false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll(".srclist").forEach((l) => l.remove()); });
document.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  window.__pressed.push(b.id);
  if (b.id === "add-we") return addWork();
  if (b.id === "add-ed") return addSchool();
  if (b.id === "add-ws") {  // Workday's Websites list: one URL box per press
    const n = counter; counter += 7;
    document.getElementById("ws-list").insertAdjacentHTML("beforeend",
      `<div data-automation-id="formField-url"><label for="webAddress-${n}--url">URL*</label><input id="webAddress-${n}--url" aria-required="true"></div>`);
    return;
  }
  if (!/--degree$/.test(b.id)) return;
  document.querySelectorAll("[role=listbox]").forEach((l) => l.remove());
  const ul = document.createElement("ul"); ul.setAttribute("role", "listbox"); ul.id = b.id + "-list";
  b.setAttribute("aria-controls", ul.id);
  for (const o of DEGREES) {
    const li = document.createElement("li"); li.setAttribute("role", "option"); li.textContent = o;
    li.addEventListener("click", () => { b.textContent = o; ul.remove(); });
    ul.appendChild(li);
  }
  document.body.appendChild(ul);
});
</script></body></html>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


class WorkdayExperienceE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        settings = Settings(data_dir=tmp / "data")
        settings.ensure_dirs()
        pdf = settings.output_dir / "current_resume.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% e2e test resume\n")
        cls.api_port, cls.site_port = free_port(), free_port()
        app = create_app(settings, StubClient("Fine."), PROFILE.model_copy(update={"how_heard": "LinkedIn", "portfolio_url": "https://janedoe.example.com",
                                                                          "github_url": "https://github.com/janedoe"}), TOKEN, pdf)
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
        manifest["content_scripts"][0]["matches"].append("http://*.myworkdayjobs.com/*")
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

    def fill(self, auto_next: bool, path: str = "/en-US/careers/job/apply"):
        page = self.ctx.new_page()
        page.goto(f"http://{HOST}:{self.site_port}{path}")
        page.wait_for_selector("#tailor-panel-host", state="attached", timeout=10000)
        page.evaluate("""on => { const r = document.querySelector('#tailor-panel-host').shadowRoot;
                                 r.getElementById('autonext').checked = on; r.getElementById('fill').click(); }""", auto_next)
        # the first run adds the missing entries, then fills again: wait for the 4th job's dates
        page.wait_for_function("() => [...document.querySelectorAll('[id$=--jobTitle]')].length === 4 && "
                               "[...document.querySelectorAll('[id$=--jobTitle]')].every(e => e.value)", timeout=40000)
        page.wait_for_function("() => [...document.querySelectorAll('[id$=--degree]')].every(b => b.textContent !== 'Select One')",
                               timeout=20000)
        page.wait_for_function("() => document.querySelectorAll('.tag').length >= 6", timeout=30000)  # school + field x3
        page.wait_for_timeout(1000)
        return page

    def entries(self, page):
        return page.evaluate("""() => {
          const tags = (el) => [...el.parentElement.querySelectorAll('.tag')].map(t => t.textContent).join('; ');
          const v = (el) => el ? (el.type === 'checkbox' ? el.checked : el.tagName === 'BUTTON' ? el.textContent :
                                   el.getAttribute('data-uxi-widget-type') === 'selectinput' ? tags(el) : el.value) : null;
          const by = (re) => { const out = {}; for (const el of document.querySelectorAll('[id]')) { const m = el.id.match(re);
            if (m) { (out[m[1]] = out[m[1]] || {})[m[2]] = v(el); } } return Object.values(out); };
          return { work: by(/^workExperience-(\\d+)--(.+?)(?:-input)?$/), school: by(/^education-(\\d+)--(.+?)(?:-input)?$/),
                   linkedin: document.getElementById('socialNetworkAccounts--linkedInAccount').value,
                   source: [...document.querySelectorAll('.srctag')].map(t => t.textContent),
                   websites: [...document.querySelectorAll('[id^=webAddress-]')].map(i => i.value),
                   pressed: window.__pressed }; }""")

    def test_work_and_education_entries(self):
        page = self.fill(auto_next=False)
        got = self.entries(page)
        work = [(w["jobTitle"], w["companyName"], w["location"], w["currentlyWorkHere"],
                 w["startDate-dateSectionMonth"], w["startDate-dateSectionYear"],
                 w["endDate-dateSectionMonth"], w["endDate-dateSectionYear"]) for w in got["work"]]
        self.assertEqual(work, [
            ("Research Associate", "University of Michigan", "Ann Arbor, MI", True, "05", "2026", "", ""),
            ("Brand Strategy Associate", "Summit Club", "Miami, FL", False, "06", "2025", "09", "2025"),
            ("Senior Sales Associate", "Acme Rental Group", "San Jose, CA", False, "06", "2023", "05", "2024"),
            ("Sales Associate", "Acme Rental Group", "San Jose, CA", False, "06", "2022", "06", "2023"),
        ])  # the projects (Lakeside, Center) are not jobs: no entry for them
        self.assertIn("Built a brand plan", got["work"][1]["roleDescription"])
        schools = [(s["schoolName"], s["degree"], s["fieldOfStudy"], s["firstYearAttended-dateSectionYear"],
                    s["lastYearAttended-dateSectionYear"]) for s in got["school"]]
        self.assertEqual(schools, [
            ("University of Michigan", "Masters", "Business Administration/Management", "2025", "2027"),  # the person's picks
            ("University of Michigan", "Masters", "Information Technology", "2024", "2027"),
            ("University of California, Davis", "Bachelors", "History", "2018", "2021"),
        ])
        self.assertEqual(got["linkedin"], "https://www.linkedin.com/in/janedoe")
        self.assertEqual(got["pressed"].count("add-we"), 3)
        self.assertEqual(got["pressed"].count("add-ed"), 2)
        self.assertNotIn("add-ce", got["pressed"])  # Certifications: never
        self.assertNotIn("next", got["pressed"])  # auto-next off: stays on the step
        self.assertEqual(got["source"], ["LinkedIn"])  # found inside "Social Media"
        self.assertEqual(got["websites"], ["https://janedoe.example.com", "https://github.com/janedoe"])  # Huron, 2026-10-30
        self.assertEqual(got["pressed"].count("add-ws"), 2)
        self.assertEqual([p for p in got["pressed"] if p.startswith("src:")][-1], "src:LinkedIn")
        page.close()

    def test_a_my_experience_step_with_no_entries_gets_every_job_and_school(self):  # Huron, 2026-10-28
        page = self.fill(auto_next=False, path="/empty/en-US/careers/job/apply")
        got = self.entries(page)
        self.assertEqual([w["companyName"] for w in got["work"]], ["University of Michigan", "Summit Club", "Acme Rental Group", "Acme Rental Group"])
        self.assertEqual([s["schoolName"] for s in got["school"]],
                         ["University of Michigan", "University of Michigan", "University of California, Davis"])
        self.assertEqual(got["pressed"].count("add-we"), 4)
        self.assertEqual(got["pressed"].count("add-ed"), 3)
        first = got["work"][0]  # dates read from the "From" heading above them
        self.assertEqual((first["startDate-dateSectionMonth"], first["startDate-dateSectionYear"]), ("05", "2026"))
        self.assertEqual(page.evaluate("() => window.__uploads"), 1)  # "Upload a file": the resume, once
        page.wait_for_timeout(500)
        self.assertTrue(got["work"][1]["roleDescription"].startswith("• "), got["work"][1])  # typed, so it stays
        self.assertIn("Built a brand plan", got["work"][1]["roleDescription"])
        self.assertNotIn("add-ce", got["pressed"])
        page.close()


if __name__ == "__main__":
    unittest.main()

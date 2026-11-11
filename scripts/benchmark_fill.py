"""How well does Tailor Assist fill real application forms? A scorecard, and the list of what to fix next.

    python scripts/benchmark_fill.py --per-ats 12          # real postings from the role search, grouped by job board
    python scripts/benchmark_fill.py --urls forms.txt      # or a list of form links, one per line

It runs the real extension in a headless browser as the made-up person in examples/profile, with its own server and a
throwaway data folder: the owner's resume, answers and Desk are not used. Nothing can reach an employer:
  - every request carrying that person's details, any file upload, and anything that says "submit" is stopped;
  - form submits and clicks on Submit / Apply are stopped in the page, and "Submit for me" is off.

For each form it records how long the fill took, what was filled, what was left to the person and why, and whether what
was filled is still there afterwards (a value the site threw away counts as a failure). Results go to
<data_dir>/benchmarks/<time>/ as results.json and scorecard.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(line_buffering=True)

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tailor.config import Settings, apply_environment, load_settings  # noqa: E402
from tailor.fill.profile import profile_from_resume  # noqa: E402
from tailor.llm import StubClient  # noqa: E402
from tailor.resume.render import render_resume  # noqa: E402
from tailor.resume.schema import Resume  # noqa: E402
from tailor.server.app import create_app  # noqa: E402
from try_fill import _BLOCK_SUBMITS, _LOOKS_LIKE_APPLICATION  # noqa: E402

EXT = ROOT / "extension"
EXAMPLE = ROOT / "examples" / "profile"
TOKEN = "benchmark-token"
BUSY = re.compile(r"Reading|Filling|Drafting|Tailoring|Writing|Adding|Fixing|Going to the next|Submitting", re.I)
DONE = re.compile(r"filled|need you|Ready for your review", re.I)
# someone who has answered everything once: what is left is what Tailor could not do, not what was never saved
STANDING = {"work_authorization_us": "Yes", "sponsorship_us": "No", "us_citizen": "Yes", "us_answers_for_us_roles": True,
            "background_check_ok": "Yes", "relocation_ok_cities": ["San Francisco", "New York"], "in_person_max_percent": 100,
            "gender": "Female", "pronouns": "They/them", "transgender": "No", "hispanic_latino": "No",
            "race": "Two or More Races", "veteran": "I am not a protected veteran", "disability": "No",
            "company_questions": "No", "government_employment": "No", "acknowledge_notices": "Yes", "sms_opt_in": "No",
            "arbitration": "Accept"}

# what became of each thing the extension marked, read in the page
_RESULT = r"""
() => {
  const label = (el) => {
    const g = (el.type === 'radio' || el.type === 'checkbox') && el.closest('fieldset, [role=radiogroup], [role=group]');
    const l = (g && (g.querySelector('legend') || g.querySelector('label'))) ||
      (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label');
    return ((l && l.textContent) || el.getAttribute('aria-label') || el.name || '').trim().replace(/\s+/g, ' ').slice(0, 140);
  };
  const holds = (el) => {
    // "Still Student?": ticking it clears the end date, and unticked is an answer too. Neither is a value lost.
    const still = /still (?:a )?student|currently (?:attend|work|enrol)|current(?:ly)? (?:student|role|job)|i (?:currently )?work here/i;
    if (el.type === 'checkbox' && still.test(label(el))) return true;
    if (el.tagName === 'SELECT' && !el.value) {
      for (let n = el.parentElement, i = 0; n && i < 7; n = n.parentElement, i++)
        if ([...n.querySelectorAll('input[type=checkbox]')].some((c) => c.checked && still.test(label(c)))) return true;
    }
    if (el.type === 'radio' || el.type === 'checkbox') {
      const box = el.closest('fieldset, [role=radiogroup], [role=group]');
      const mates = el.name ? [...document.querySelectorAll('input[name="' + CSS.escape(el.name) + '"]')] : box ? [...box.querySelectorAll('input')] : [el];
      return mates.some((m) => m.checked);
    }
    if (el.type === 'file') return !!(el.files && el.files.length);
    if (el.tagName === 'BUTTON') return !/^(select|select one|choose)?$/i.test((el.textContent || '').trim());
    if (el.tagName === 'SELECT') return !!el.value;
    if ((el.value || '').trim()) return true;
    let n = el.parentElement;
    for (let i = 0; i < 5 && n; i++, n = n.parentElement)
      if (n.querySelector('[class*=single-value], [class*=singleValue], [class*=multi-value], [class*=multiValue], [data-uxi-widget-type=selectinputlistitem]')) return true;
    return false;
  };
  const rows = (cls) => [...document.querySelectorAll('.' + cls)].map((el) => ({ label: label(el), type: el.type || el.tagName.toLowerCase(), holds: holds(el), reason: el.title || '' }));
  const panel = document.querySelector('#tailor-panel-host')?.shadowRoot;
  return { filled: rows('tailor-filled'), draft: rows('tailor-draft'), needs: rows('tailor-needs'),
           status: panel?.getElementById('status')?.textContent || '',
           panel_needs: [...(panel?.getElementById('needs')?.querySelectorAll('li') || [])].map((l) => l.textContent.trim().replace(/\s+/g, ' ').slice(0, 600)) };
}
"""


def board_of(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    q = parse_qs(urlparse(url).query)
    for key, name in (("greenhouse.io", "greenhouse"), ("lever.co", "lever"), ("ashbyhq.com", "ashby"), ("myworkday", "workday"),
                      ("smartrecruiters", "smartrecruiters"), ("icims", "icims"), ("taleo", "taleo"), ("oraclecloud", "oracle"),
                      ("successfactors", "successfactors"), ("workable", "workable"), ("bamboohr", "bamboohr"), ("jobvite", "jobvite"),
                      ("csod.com", "cornerstone"), ("rippling", "rippling"), ("teamtailor", "teamtailor"), ("recruitee", "recruitee"),
                      ("applytojob", "jazzhr"), ("breezy", "breezy"), ("avature", "avature"), ("ultipro", "ukg"), ("dayforce", "dayforce"),
                      ("paylocity", "paylocity"), ("adp.com", "adp"), ("pinpointhq", "pinpoint")):
        if key in h:
            return name
    return "greenhouse" if "gh_jid" in q else "company site"


def form_url(r: dict) -> str:
    """The application form itself, as the Desk's queue opens it."""
    url = (r.get("url") or "").split("#")[0]
    u = urlparse(url)
    gh = parse_qs(u.query).get("gh_jid", [""])[0]
    if gh and not (u.hostname or "").endswith("greenhouse.io") and r.get("source") == "greenhouse":
        return f"https://job-boards.greenhouse.io/embed/job_app?for={r.get('company')}&token={gh}"
    if u.hostname == "jobs.lever.co" and not u.path.rstrip("/").endswith("/apply"):
        return url.split("?")[0].rstrip("/") + "/apply"
    if u.hostname == "jobs.ashbyhq.com" and not u.path.rstrip("/").endswith("/application"):
        return url.split("?")[0].rstrip("/") + "/application"
    if ".myworkday" in (u.hostname or "") and "/job/" in u.path and "/apply" not in u.path:
        return url.split("?")[0].rstrip("/") + "/apply/applyManually"
    return url


def sample(settings, per_ats: int) -> list[str]:
    """Real postings from the role search, spread over companies and job boards."""
    path = settings.data_dir / "search" / "results.json"
    rows = json.loads(path.read_text(encoding="utf-8")).get("results", {}) if path.exists() else {}
    rows = rows.values() if isinstance(rows, dict) else rows
    by_board: dict[str, list[str]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for r in sorted(rows, key=lambda x: str(x.get("last_seen", "")), reverse=True):
        url = form_url(r)
        board = board_of(url)
        who = (board, str(r.get("company") or urlparse(url).hostname))
        if url and who not in seen and len(by_board[board]) < per_ats:
            seen.add(who)
            by_board[board].append(url)
    return [u for urls in by_board.values() for u in urls]


def bucket(reason: str) -> str:
    r = reason.lower()
    if "unrecognized" in r:
        return "question not understood"
    if "could not fill" in r:
        return "could not operate the control"
    if "sensitive" in r:
        return "sensitive, no rule"
    if "check all" in r:
        return "checklist not answered"
    if "not in your profile" in r or "no saved answer" in r:
        return "fact not on file"
    if "only you know" in r or "own words" in r or "without ai" in r:
        return "only the person can write this"
    return "other"


def _safe_eval(frame, script):
    try:
        return frame.evaluate(script)
    except Exception:
        return False


def run_form(ctx, url: str, identifiers: list[str], timeout_s: int) -> dict:
    out: dict = {"url": url, "board": board_of(url), "outcome": "no form", "seconds": None, "blocked": 0}
    page = ctx.new_page()
    if os.environ.get("BENCH_CONSOLE"):
        page.on("console", lambda m: print("console:", m.text[:220], flush=True) if m.text.startswith("[tailor]") else None)
    started = time.time()
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        out["final_url"] = page.url
        out["board"] = board_of(page.url) if board_of(url) == "company site" else out["board"]
        html = ""
        frame = None
        for _ in range(12):
            page.wait_for_timeout(1500)
            for f in page.frames:
                try:
                    if f.evaluate(_LOOKS_LIKE_APPLICATION):
                        frame = f
                        break
                except Exception:
                    continue
            if frame:
                break
        if not frame:  # the job page's own Apply link, as Tailor follows it from the Desk
            link = page.evaluate("""() => { const a = [...document.querySelectorAll('a[href]')].find((a) => a.offsetParent &&
                /^(apply|apply now|apply for this job|apply to this job|apply for this role|apply for this position)$/i.test((a.textContent || '').trim()) && /^https?:/.test(a.href));
                return a ? a.href : ''; }""")
            if link:
                out["followed_apply"] = True
                page.goto(link, timeout=45000, wait_until="domcontentloaded")
                for _ in range(10):
                    page.wait_for_timeout(1500)
                    frame = next((f for f in page.frames if _safe_eval(f, _LOOKS_LIKE_APPLICATION)), None)
                    if frame:
                        break
        html = (page.content() or "")[:400000].lower()
        if not frame:
            if page.query_selector("input[type=password]") or re.search(r"create account|sign in to|sign in$", (page.title() or ""), re.I):
                out["outcome"] = "sign-in wall"
            elif re.search(r"captcha|verify you are human|are you a robot", html):
                out["outcome"] = "bot check"
            elif re.search(r"job not found|no longer available|position (?:has been )?(?:closed|filled)|page (?:you(?:'re| are) looking for )?(?:doesn.t exist|could not be found)", html):
                out["outcome"] = "closed"
            return out
        started = time.time()
        status = frame.evaluate("document.querySelector('#tailor-panel-host').shadowRoot.getElementById('status').textContent")
        if not BUSY.search(status or ""):
            frame.evaluate("document.querySelector('#tailor-panel-host').shadowRoot.getElementById('fill').click()")
        calm = 0
        timeline: list[list] = []
        while time.time() - started < timeout_s:
            page.wait_for_timeout(500 if calm == 0 else 1500)
            status = frame.evaluate("document.querySelector('#tailor-panel-host').shadowRoot.getElementById('status').textContent") or ""
            if not timeline or timeline[-1][1] != status[:50]:
                timeline.append([round(time.time() - started, 1), status[:50]])
            calm = calm + 1 if DONE.search(status) and not BUSY.search(status) else 0
            if calm >= 4:
                break
        res = frame.evaluate(_RESULT)
        if os.environ.get("BENCH_DUMP"):  # what the page holds now, for looking into one form
            print(json.dumps(frame.evaluate("""() => ({selects: [...document.querySelectorAll('select')].map((s) => [s.value, s.className.includes('tailor-filled')]),
              marked: [...document.querySelectorAll('.tailor-filled, .tailor-draft, .tailor-needs')].filter((e) => /education|school|degree|student/i.test(e.id + e.name + (e.placeholder||''))).map((e) => [e.id||e.placeholder, e.value, e.checked]),
              adds: [...document.querySelectorAll('button')].filter((b) => /add education/i.test(b.textContent)).length,
              race: [...document.querySelectorAll('input, select')].filter((e) => { const l = (e.labels && e.labels[0] && e.labels[0].textContent) || ''; return /race|ethnic/i.test(l + ' ' + e.id + ' ' + e.name); })
                .map((e) => { if (e.type === 'radio' || e.type === 'checkbox') return e.checked ? e.labels[0].textContent.trim() : null; if (e.tagName === 'SELECT') return e.selectedOptions[0] && e.selectedOptions[0].text;
                  let n = e.parentElement; for (let i = 0; i < 5 && n; i++, n = n.parentElement) { const v = n.querySelector('[class*=single-value], [class*=singleValue], [class*=multi-value], [class*=multiValue]'); if (v) return v.textContent.trim(); } return e.value; }).filter(Boolean)})""")))
        out.update(outcome="filled" if calm >= 4 else "timed out", seconds=round(time.time() - started, 1), title=page.title(),
                   status=res["status"][:200], timeline=timeline, panel_needs=res["panel_needs"], filled=len(res["filled"]),
                   drafted=len(res["draft"]), needs=len(res["needs"]),
                   lost=[r["label"] or r["type"] for r in res["filled"] + res["draft"] if not r["holds"]],
                   blocked=frame.evaluate("window.__tailorBlocked || 0"))
    except Exception as e:
        out.update(outcome="error", error=f"{type(e).__name__}: {str(e)[:160]}")
    finally:
        try:
            page.close()
        except Exception:
            pass
    return out


def scorecard(rows: list[dict], stamp: str) -> str:
    done = [r for r in rows if r["outcome"] == "filled"]
    def pct(a, b):
        return f"{100 * a / b:.0f}%" if b else "n/a"
    lines = [f"# Tailor Assist fill benchmark, {stamp}", "",
             f"{len(rows)} real forms tried; {len(done)} reached and filled. Made-up applicant, nothing sent.", "",
             "| Board | Tried | Filled | Blocked by a wall | Fields filled | Kept by the site | Nothing left to do | Median seconds |",
             "|---|---|---|---|---|---|---|---|"]
    boards = defaultdict(list)
    for r in rows:
        boards[r["board"]].append(r)
    for board, rs in sorted(boards.items(), key=lambda kv: -len(kv[1])):
        ok = [r for r in rs if r["outcome"] == "filled"]
        got = sum(r["filled"] + r["drafted"] for r in ok)
        total = got + sum(len(r["panel_needs"]) for r in ok)
        lost = sum(len(r["lost"]) for r in ok)
        secs = sorted(r["seconds"] for r in ok)
        lines.append(f"| {board} | {len(rs)} | {len(ok)} | {sum(r['outcome'] in ('sign-in wall', 'bot check') for r in rs)} | "
                     f"{pct(got, total)} | {pct(got - lost, got)} | {pct(sum(not r['panel_needs'] for r in ok), len(ok))} | "
                     f"{secs[len(secs) // 2] if secs else 'n/a'} |")
    got = sum(r["filled"] + r["drafted"] for r in done)
    total = got + sum(len(r["panel_needs"]) for r in done)
    lost = sum(len(r["lost"]) for r in done)
    lines += ["", f"**Overall:** {pct(got, total)} of fields filled; {pct(got - lost, got)} of those kept by the site; "
                  f"{pct(sum(not r['panel_needs'] for r in done), len(done))} of forms left nothing to do.", "",
              "Outcomes: " + ", ".join(f"{k} {v}" for k, v in Counter(r["outcome"] for r in rows).most_common()), "",
              "## Why fields were left to the person", ""]
    why = Counter(bucket(n[-120:]) for r in done for n in r["panel_needs"])
    lines += [f"- {k}: {v}" for k, v in why.most_common()]
    lines += ["", "## Most common questions left", ""]
    qs = Counter(re.sub(r"\s*\([^()]*\)\s*$", "", n).strip().lower()[:110] for r in done for n in r["panel_needs"])
    lines += [f"- {v}x {k}" for k, v in qs.most_common(40)]
    lines += ["", "## Filled, but the site did not keep it", ""]
    gone = Counter(x.lower()[:90] for r in done for x in r["lost"])
    lines += [f"- {v}x {k}" for k, v in gone.most_common(25)] or ["- none"]
    lines += ["", "## Forms not reached", ""]
    lines += [f"- {r['outcome']}: {r['url'][:110]}" for r in rows if r["outcome"] not in ("filled",)]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--per-ats", type=int, default=12)
    ap.add_argument("--urls", help="a file of form links, one per line")
    ap.add_argument("--timeout", type=int, default=150, help="seconds allowed per form")
    ap.add_argument("--pause", type=float, default=4.0, help="seconds between forms, to go easy on the sites")
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()

    real = load_settings()
    apply_environment(real)
    urls = [x.strip() for x in Path(args.urls).read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")] if args.urls else sample(real, args.per_ats)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = real.data_dir / "benchmarks" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        settings = Settings(data_dir=tmp / "data")
        settings.ensure_dirs()
        # the made-up person, but with real schools and a real city: lists on real forms can only find real places
        def real_places(text: str) -> str:
            for fake, real in (("State University", "University of California, Davis"), ("Lakeview University", "Ohio State University"),
                               ("Riverton", "San Francisco")):
                text = text.replace(fake, real)
            return text

        for f in EXAMPLE.glob("*.json"):
            (settings.profile_dir / f.name).write_text(real_places(f.read_text(encoding="utf-8")), encoding="utf-8")
        (settings.profile_dir / "standing_answers.json").write_text(json.dumps(STANDING), encoding="utf-8")
        resume = Resume.model_validate_json((settings.profile_dir / "base_resume.json").read_text(encoding="utf-8"))
        extra = json.loads((settings.profile_dir / "profile_overrides.json").read_text(encoding="utf-8"))
        profile = profile_from_resume(resume, overrides=extra.get("name_overrides"), extras=extra.get("extras"),
                                      education_starts=extra.get("education_starts"), education_order=extra.get("education_order"),
                                      fields_of_study=extra.get("fields_of_study"))
        pdf = settings.output_dir / "current_resume.pdf"
        render_resume(resume, pdf, settings)
        app = create_app(settings, StubClient("Fine."), profile, TOKEN, pdf, base_resume=resume)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="critical"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.1)

        ext = tmp / "ext"
        shutil.copytree(EXT, ext)
        manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
        manifest["content_scripts"][0]["matches"] = ["<all_urls>"]
        manifest["host_permissions"].append(f"http://127.0.0.1:{args.port}/*")
        (ext / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        identifiers = [profile.email.lower(), profile.last_name.lower(), re.sub(r"\D", "", profile.phone)]
        stopped: list[str] = []

        def guard(route):
            req = route.request
            host = urlparse(req.url).hostname or ""
            if host == "127.0.0.1" or req.url.startswith(("chrome-extension:", "data:", "blob:")):
                return route.continue_()
            body = (req.post_data_buffer or b"")
            text = (req.url + " " + body[:200000].decode("utf-8", "ignore")).lower()
            ctype = (req.headers.get("content-type") or "").lower()
            carries = any(i and i in text for i in identifiers)
            sends = req.method != "GET" and ("multipart" in ctype or re.search(r"submit|jobapplication|apply", text))
            if carries or sends:
                stopped.append(f"{req.method} {host}")
                return route.abort()
            return route.continue_()

        rows: list[dict] = []
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(tmp / "browser"), channel="chromium", headless=True, viewport={"width": 1400, "height": 900},
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
            ctx.add_init_script(_BLOCK_SUBMITS)
            ctx.route("**/*", guard)
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=40000)
            sw.evaluate("cfg => chrome.storage.local.set(cfg)", {"backendUrl": f"http://127.0.0.1:{args.port}", "token": TOKEN,
                                                                 "autoSubmit": False, "autoNext": False})
            for i, url in enumerate(urls, 1):
                r = run_form(ctx, url, identifiers, args.timeout)
                rows.append(r)
                print(f"{i}/{len(urls)} {r['outcome']:12} {r['board']:14} filled {r.get('filled', '-')} left {len(r.get('panel_needs', []))} "
                      f"lost {len(r.get('lost', []))} {r.get('seconds') or ''}s  {url[:80]}")
                (out_dir / "results.json").write_text(json.dumps({"stamp": stamp, "rows": rows, "requests_stopped": len(stopped)}, indent=1), encoding="utf-8")
                (out_dir / "scorecard.md").write_text(scorecard(rows, stamp), encoding="utf-8")
                time.sleep(args.pause)
            ctx.close()
        server.should_exit = True
    print(f"\nRequests stopped: {len(stopped)}\nScorecard: {out_dir / 'scorecard.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

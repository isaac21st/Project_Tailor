"""Trial run: the real extension fills a real application in a separate test browser, and nothing can be submitted.

    python scripts/try_fill.py https://job-boards.greenhouse.io/figma/jobs/123
    python scripts/try_fill.py https://jobs.lever.co/acme/abc --no-fill   # just open it; you click Fill yourself
    python scripts/try_fill.py <form url> --posting <posting url>        # the posting and the form are separate pages

Needs the server running (scripts/run_server.py). A visible Chromium opens with a copy of the extension already set up
(token included) and allowed on every site. The script waits for the application form, clicks Fill this page, and
writes what happened to each field to <data_dir>/trials/<time>_<site>/ (report.json, report.md, screenshot.png).
Leave the window open to look around; close it when done. Logins made in it are kept for the next trial.

Submits are blocked three ways: form submit events are cancelled, form.submit()/requestSubmit() do nothing, and
POST/PUT requests whose URL looks like an application submission are aborted. Review the report, not the site.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(line_buffering=True)  # progress shows while the browser is open

import httpx  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tailor.config import apply_environment, load_settings  # noqa: E402

EXT = Path(__file__).resolve().parent.parent / "extension"
API = "http://127.0.0.1:8765"
# matched against the path only. Not "/applications" or "/apply": whole sites live under those (Google Careers),
# and blocking them breaks starting an application. Submit buttons are stopped in the page instead.
_SUBMIT_URL = re.compile(r"submit", re.I)
_AUTH_HOST = re.compile(r"(^|\.)(accounts\.google\.com|login\.microsoftonline\.com|okta\.com|duosecurity\.com|onelogin\.com|auth0\.com)$"
                        r"|(^|\.)(login|logon|sso|auth|idp|signin|weblogin|shibboleth|okta|duo|adfs|cas)[.-]", re.I)

# runs in every frame before the page's own scripts
_BLOCK_SUBMITS = r"""
(() => {
  // sign-in pages (Google, university SSO, Okta, Duo...) must work: their forms, and the automatic SAML hand-offs, submit
  const AUTH = /(^|\.)(accounts\.google\.com|login\.microsoftonline\.com|okta\.com|duosecurity\.com|onelogin\.com|auth0\.com)$|(^|\.)(login|logon|sso|auth|idp|signin|weblogin|shibboleth|okta|duo|adfs|cas)[.-]/i;
  if (AUTH.test(location.hostname)) return;
  const isLogin = form => !!(form && form.querySelector && form.querySelector('input[type=password]'));  // sign in / create account
  // a form with nothing to fill in is a sign-in hand-off (SAML/OAuth auto-post), never an application
  const isHandoff = form => !!(form && form.querySelectorAll &&
    ![...form.querySelectorAll('input, select, textarea')].some(el => !/^(hidden|submit|button|image|reset)$/i.test(el.type || '')));
  const allowed = form => isLogin(form) || isHandoff(form);
  const count = () => { window.__tailorBlocked = (window.__tailorBlocked || 0) + 1; console.warn('[tailor-trial] submit blocked'); };
  const stop = e => { e.preventDefault(); e.stopImmediatePropagation(); count(); };
  window.addEventListener('submit', e => { if (!allowed(e.target)) stop(e); }, true);
  window.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('button, input[type=submit], [role=button]');
    // a form's own controls are often type=submit too (Ashby's "+ Add Education", its Yes/No answers): those are let
    // through by their words; anything that could send the application is stopped
    const words = (b && (b.textContent || b.value || b.getAttribute('aria-label') || '') || '').trim();
    const control = /^(\+\s*)?add\b|^(yes|no)$/i.test(words) || (b && b.hasAttribute && b.hasAttribute('aria-pressed'));
    if (b && !control && (b.type === 'submit' || /submit|apply|send/i.test(words)) && !allowed(b.form || b.closest('form'))) stop(e);
  }, true);
  const submit = HTMLFormElement.prototype.submit, requestSubmit = HTMLFormElement.prototype.requestSubmit;
  HTMLFormElement.prototype.submit = function () { return allowed(this) ? submit.call(this) : count(); };
  HTMLFormElement.prototype.requestSubmit = function (b) { return allowed(this) ? requestSubmit.call(this, b) : count(); };
})();
"""

# every field the extension marked, in every frame
_REPORT = """
() => [...document.querySelectorAll('.tailor-filled, .tailor-draft, .tailor-needs, .tailor-drafting')].map(el => {
  const kind = ['tailor-filled', 'tailor-draft', 'tailor-needs', 'tailor-drafting'].find(c => el.classList.contains(c));
  const group = (el.type === 'radio' || el.type === 'checkbox') && el.closest('fieldset, [role=radiogroup], [role=group]');
  const lab = (group && (group.querySelector('legend') || group.querySelector('[id$=label], label')))
           || (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label') || null;
  let value = el.type === 'file' ? [...(el.files || [])].map(f => f.name).join(', ')
            : el.type === 'checkbox' || el.type === 'radio' ? (el.checked ? 'checked' : '')
            : el.tagName === 'SELECT' ? (el.selectedOptions[0]?.textContent || '') : (el.value ?? el.textContent ?? '');
  return { kind: kind.replace('tailor-', ''), tag: el.tagName.toLowerCase(), type: el.type || '', name: el.name || '',
           id: el.id || '', label: (lab?.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 160),
           reason: el.title || '', value: String(value).slice(0, 300) };
})
"""


# an application form, not a job search page: the panel is up and some field asks for a name, email, phone or resume
_LOOKS_LIKE_APPLICATION = """
(() => {
  if (!document.querySelector('#tailor-panel-host')) return false;
  if (document.querySelector('input[type=email], input[type=tel], input[type=file], input[autocomplete~=given-name]')) return true;
  const words = /first name|last name|full name|legal name|e-?mail|phone|resume|cv\\b/i;
  return [...document.querySelectorAll('input:not([type=hidden]), textarea, select')].some(el => {
    const lab = (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label');
    return words.test([lab?.textContent, el.getAttribute('aria-label'), el.placeholder, el.name].join(' '));
  });
})()
"""

# what fields a page has (no values), for when no form was recognised
_INVENTORY = r"""
() => [...document.querySelectorAll('input:not([type=hidden]), textarea, select, [role=combobox], [role=listbox], [role=radio], [role=radiogroup], [role=checkbox], [contenteditable=true]')].map(el => {
  const lab = (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label');
  const r = el.getBoundingClientRect();
  return { tag: el.tagName.toLowerCase(), type: el.type || '', role: el.getAttribute('role') || '', name: el.name || '', id: el.id || '',
           autocomplete: el.autocomplete || '', visible: r.width > 0 && r.height > 0, marked: el.className?.toString().match(/tailor-\w+/)?.[0] || '',
           labelledby: el.getAttribute('aria-labelledby') ? (document.getElementById(el.getAttribute('aria-labelledby').split(' ')[0])?.textContent || '').trim().slice(0, 80) : '',
           label: (lab?.textContent || el.getAttribute('aria-label') || el.placeholder || '').trim().slice(0, 120),
           group: (el.closest('fieldset, [role=radiogroup], [role=group]')?.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100) };
})
"""


def panel_status(page) -> str:
    for frame in page.frames:
        try:
            t = frame.evaluate("document.querySelector('#tailor-panel-host')?.shadowRoot?.getElementById('status')?.textContent")
        except Exception:
            continue
        if t:
            return t
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("url")
    ap.add_argument("--no-fill", action="store_true", help="open the page with the extension, but let me click Fill")
    ap.add_argument("--wait", type=int, default=600, help="seconds to wait for a form to appear (default 600)")
    ap.add_argument("--exit", action="store_true", help="close the browser once the report is written (batch trials)")
    ap.add_argument("--posting", help="open this posting page first, as a person would, so the extension remembers it")
    args = ap.parse_args()

    settings = load_settings()
    apply_environment(settings)
    token_file = settings.data_dir / "server_token.txt"
    try:
        httpx.get(f"{API}/api/health", timeout=3).raise_for_status()
    except Exception:
        print("The server is not running. Start it first:  python scripts/run_server.py --model ollama "
              "--ollama-model qwen3:8b")
        return 1

    # a test copy of the extension that runs on every site (your own Chrome's copy is not touched)
    # A new folder each run: Chrome keeps running an unpacked extension's old background script in a reused profile,
    # whatever the files say (found 2026-10-02). A new folder is a new extension, so it starts from the files.
    for old in settings.tmp_dir.glob("trial_extension*"):
        shutil.rmtree(old, ignore_errors=True)
    ext = settings.tmp_dir / f"trial_extension_{int(time.time())}"
    shutil.copytree(EXT, ext)
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    manifest["content_scripts"][0]["matches"] = ["<all_urls>"]
    (ext / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = settings.data_dir / "trials" / f"{stamp}_{urlparse(args.url).hostname or 'page'}"
    out.mkdir(parents=True, exist_ok=True)
    blocked_requests: list[str] = []

    profile = settings.data_dir / "trial_browser"
    for _ in range(20):  # a trial window that is still closing holds the profile; opening it then half-works
        if not (profile / "SingletonLock").exists():
            break
        time.sleep(0.5)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile), channel="chromium", headless=False, viewport=None,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        ctx.add_init_script(_BLOCK_SUBMITS)
        console_lines: list[str] = []  # the extension's own "[tailor]" debug lines, kept in the report

        def on_page(pg):
            pg.on("console", lambda m: console_lines.append(m.text[:400]) if m.text.startswith("[tailor]") else None)

        for pg in ctx.pages:
            on_page(pg)
        ctx.on("page", on_page)

        def guard(route):
            req = route.request
            u = urlparse(req.url)
            if (req.method in ("POST", "PUT") and u.hostname not in ("127.0.0.1", None)
                    and not _AUTH_HOST.search(u.hostname) and _SUBMIT_URL.search(u.path)):
                blocked_requests.append(f"{req.method} {req.url}")
                return route.abort()
            return route.continue_()

        ctx.route("**/*", guard)
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        sw.evaluate("cfg => chrome.storage.local.set(cfg)",
                    {"backendUrl": API, "token": token_file.read_text(encoding="utf-8").strip()})

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if args.posting:
            page.goto(args.posting)
            key = "posting:" + (urlparse(args.posting).hostname or "")
            saved = 0
            for _ in range(30):  # wait until the extension has saved it: pages load slower with submits guarded
                page.wait_for_timeout(1000)
                saved = sw.evaluate("k => chrome.storage.session.get(k).then(r => r[k] ? r[k].text.length : 0)", key)
                if saved:
                    break
            print(f"Read the posting at {args.posting}: " + (f"saved {saved} characters" if saved else
                  "the extension did not recognise a posting there"))
        page.goto(args.url)
        print(f"Opened {args.url}. If the form is behind an Apply button or a login, get to it in the window.")

        deadline = time.time() + args.wait
        filled = False
        visited: list[str] = []
        while time.time() < deadline and ctx.pages:
            page = ctx.pages[-1]  # follow a new tab opened by an Apply button
            if not visited or visited[-1] != page.url:
                visited.append(page.url)
                print(f"  at {page.url[:120]}")
            if re.search(r"filled\.|need you|Filling|Reading|Drafting|Tailoring", panel_status(page)):
                filled = True  # the person clicked Fill this page in the panel
                break
            if not args.no_fill:
                for frame in page.frames:
                    try:
                        has_form = frame.evaluate(_LOOKS_LIKE_APPLICATION)
                    except Exception:
                        continue
                    if has_form:
                        frame.evaluate("document.querySelector('#tailor-panel-host').shadowRoot.getElementById('fill').click()")
                        filled = True
                        break
                if filled:
                    break
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass  # the tab closed or navigated; look again

        if not filled:
            print("No application form was recognised. Nothing was filled.")
            if ctx.pages:  # keep what the last page looked like, so the detection can be fixed
                page = ctx.pages[-1]
                inventory = []
                for frame in page.frames:
                    try:
                        inventory += [dict(f, frame=frame.url) for f in frame.evaluate(_INVENTORY)]
                    except Exception:
                        pass
                (out / "no_form.json").write_text(json.dumps({"visited": visited, "last_url": page.url,
                                                              "title": page.title(), "inputs": inventory}, indent=2),
                                                  encoding="utf-8")
                page.screenshot(path=str(out / "screenshot.png"), full_page=True)
                print(f"What the page looked like: {out}")
            else:
                (out / "no_form.json").write_text(json.dumps({"visited": visited, "window_closed": True}, indent=2),
                                                  encoding="utf-8")
        else:
            print("Filling...")
            end = time.time() + 480  # tailoring, a cover letter (about two minutes) and drafts can take a few minutes
            last_click = time.time()
            while time.time() < end:
                s = panel_status(page)
                if re.search(r"filled\.|need you|Cannot|token|No access", s) and not re.search(r"Drafting|Reading|Tailoring|Writing|Adding", s):
                    break
                if s.startswith("Ready") and not args.no_fill and time.time() - last_click > 12:
                    # a click made while the panel was still starting up does nothing: click again
                    for frame in page.frames:
                        try:
                            frame.evaluate("document.querySelector('#tailor-panel-host').shadowRoot.getElementById('fill').click()")
                            break
                        except Exception:
                            continue
                    last_click = time.time()
                page.wait_for_timeout(1500)
            # a second round may follow: Tailor adds the work/education entries the resume needs, then fills again
            page.wait_for_timeout(2500)
            if re.search(r"Adding|Reading|Filling|Tailoring|Writing|Drafting", panel_status(page)):
                end = time.time() + 300
                while time.time() < end:
                    page.wait_for_timeout(1500)
                    s = panel_status(page)
                    if re.search(r"filled\.|need you", s) and not re.search(r"Adding|Reading|Filling|Tailoring|Drafting|Writing", s):
                        break

            fields = []
            for frame in page.frames:
                try:
                    fields += [dict(f, frame=frame.url) for f in frame.evaluate(_REPORT)]
                except Exception:
                    pass
            blocked = sum(f.evaluate("window.__tailorBlocked || 0") for f in page.frames if not f.is_detached())
            inventory = []
            for frame in page.frames:
                try:
                    inventory += [dict(f, frame=frame.url) for f in frame.evaluate(_INVENTORY)]
                except Exception:
                    pass
            report = {"url": page.url, "title": page.title(), "time": stamp, "status": panel_status(page), "inventory": inventory,
                      "counts": {k: sum(f["kind"] == k for f in fields) for k in ("filled", "draft", "needs")},
                      "submits_blocked": blocked, "requests_blocked": blocked_requests, "fields": fields,
                      "console": console_lines[-200:]}
            (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            lines = [f"# {report['title']}", "", report["url"], "", f"Panel: {report['status']}", ""]
            for kind in ("needs", "draft", "filled"):
                rows = [f for f in fields if f["kind"] == kind]
                if rows:
                    lines += [f"## {kind} ({len(rows)})", ""]
                    lines += [f"- **{f['label'] or f['name'] or f['id']}** ({f['type'] or f['tag']})"
                              + (f": {f['reason']}" if f["reason"] else "") for f in rows]
                    lines.append("")
            (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
            page.screenshot(path=str(out / "screenshot.png"), full_page=True)
            print(f"{report['status']}\nReport: {out}")

        if args.exit:
            ctx.close()
            return 0
        print("Leave the window open to review; close it when you are done.")
        while ctx.pages:
            try:
                ctx.pages[0].wait_for_event("close", timeout=0)
            except Exception:
                break
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

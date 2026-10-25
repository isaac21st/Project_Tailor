// School job boards on 12twenty (<school>.12twenty.com): while the person looks at the job listings, the full-time roles that fit
// what they search for (product, consulting, strategy, MBA leadership programs) go to Tailor's Find roles, with their
// "Apply by" date. It only reads the rows on screen: it never clicks, opens or applies to anything (2026-10-14).
"use strict";

(() => {
  if (window.__tailorSchoolBoard) return;
  window.__tailorSchoolBoard = true;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const WANT = /product|consult|strateg|leadership|rotation|development program|general management|business operations|\bmba\b|associate|analyst/i;
  const NEVER = /\bintern(ship)?\b|teacher|investment|private equity|banking|\bbank\b|wealth|finance manager|financial|audit|tax\b|vice president|\bvp\b|director|head of|\b(?:senior|sr\.?)\s+(?!associate|consultant)|principal|engineer|medical|nurse|trading|marketing|sales/i;
  const sent = new Set();
  // the board's own name ("Example Recruit" shows in the page title) or its host
  const schoolBoard = () => clean((document.title.split(/\s+-\s+/)[1] || "").trim()) || location.hostname.split(".")[0];

  function category(title, company) {
    if (/product/i.test(title)) return "Product management";
    if (/consult/i.test(title + " " + company)) return "Consulting";
    if (/leadership|rotation|development program|general management/i.test(title)) return "MBA leadership program";
    return "Strategy & operations";
  }

  function deadlineOf(text) {
    const m = (text || "").match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
    return m ? `${m[3]}-${m[1].padStart(2, "0")}-${m[2].padStart(2, "0")}` : "";
  }

  function rows() {
    const out = [];
    for (const a of document.querySelectorAll("a.job-title[href*='#/jobPostings/']")) {
      const id = (a.getAttribute("href").match(/jobPostings\/(\d{8,})/) || [])[1];
      const row = a.closest(".entity-short-summary-primary") || a.parentElement;
      if (!id || sent.has(id) || !row) continue;
      const item = (icon) => { const i = row.querySelector(`.sub-info-item i.${icon}`); return i ? clean(i.parentElement.textContent) : ""; };
      const title = clean((a.querySelector(".primary-item-text") || a).textContent);
      const company = clean((row.querySelector(":scope > span.sub-info") || {}).textContent);
      const type = item("fa-briefcase");
      const due = item("fa-alarm-clock");
      if (!title || !company || !WANT.test(title) || NEVER.test(title) || (type && !/full-?time/i.test(type))) { sent.add(id); continue; }
      sent.add(id);
      out.push({ company, title, url: `${location.origin}/jobPostings#/jobPostings/${id}`, location: item("fa-map-marker-alt"),
                 category: category(title, company), deadline: deadlineOf(due),
                 note: `${schoolBoard()}${due ? " · " + due.replace(/,?\s*\d{1,2}:\d{2}\s*[ap]m.*$/i, "") : ""}` });
    }
    return out;
  }

  let timer = null;
  function scan() {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!/#\/jobPostings(?:\/index|$|\?)/.test(location.hash) && !/jobPostings\/?$/.test(location.pathname + location.hash)) return;
      const found = rows();
      if (found.length) chrome.runtime.sendMessage({ type: "import_found", rows: found }).catch(() => {});
    }, 1200);
  }
  new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("hashchange", scan);
  scan();
})();

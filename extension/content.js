// Content script: reads the form, asks the local server what to do, applies it, and shows a small panel.
//
// HARD RULE: this script never submits anything. It types into text fields, chooses <select> options, ticks checkboxes
// and radios, attaches the resume file and picks options in custom dropdowns. Every other press is one of a few
// guarded clicks, each checked right before it happens (tests/test_extension_static.py counts them): a section's own
// Add button (work or education entries), a question's own Yes/No answer button, and a step's Next / Continue button
// (never one that says submit, apply, send, finish, confirm or sign, never on the Review page). No code path presses
// Submit or Apply. The person reviews and clicks Submit.
"use strict";

(() => {
  if (window.__tailorAssist) return;
  window.__tailorAssist = true;

  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "image", "password"]);
  const HIGHLIGHTS = ["tailor-filled", "tailor-drafting", "tailor-draft", "tailor-needs"];
  const registry = new Map();   // field id -> { els: [...] }
  const fieldInfo = new Map();  // field id -> the description sent to the server
  let counter = 0;
  // A field keeps its id for as long as it is on the page. Ids used to be handed out by position on every read of the
  // form, so when "Add another" put new fields in the middle while answers were on their way, the answers landed on
  // the wrong fields (Keystone, 2026-10-06: "Veteran Status" got "No", "Hispanic/Latino" got "Male").
  const fieldIds = new WeakMap();
  const idFor = (el) => { if (!fieldIds.has(el)) fieldIds.set(el, "t" + (++counter)); return fieldIds.get(el); };
  let running = false;
  let panel = null;
  const tailoredFor = new Map();  // page URL -> { id, summary }: the resume tailored to that posting
  let resumeId = "";            // which tailored resume the next upload should use ("" = the default)

  const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/\*/g, "").replace(/\((?:required|optional)\)/gi, "").trim();
  const send = (message) => chrome.runtime.sendMessage(message);

  // ---------- reading the form ----------

  // Boxes inside web components (SmartRecruiters' new "Easy apply" puts every box in an open shadow root: spl-input,
  // spl-dropdown-search, 2026-10-08): the page's boxes, wherever they are. Never Tailor's own panel.
  function deepAll(selector, root = document, out = []) {
    out.push(...root.querySelectorAll(selector));
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot && el.id !== "tailor-panel-host") deepAll(selector, el.shadowRoot, out);
    }
    return out;
  }

  function isVisible(el) {
    if (el.getAttribute("aria-hidden") === "true") return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }

  function labelText(label) {
    const copy = label.cloneNode(true);
    copy.querySelectorAll("input,select,textarea,button").forEach((n) => n.remove());
    return clean(copy.textContent);
  }

  // the element around this one, past a web component's wall (a box inside a shadow root -> its component)
  const up = (n) => n.parentElement || (n.parentNode instanceof ShadowRoot ? n.parentNode.host : null);
  const byIdNear = (el, id) => (el.getRootNode && el.getRootNode().getElementById ? el.getRootNode().getElementById(id) : null) || document.getElementById(id);
  // the label a web component carries for its box (SmartRecruiters: <spl-textfield label="Legal First Name">)
  function hostLabel(el) {
    for (let root = el.getRootNode(), i = 0; root instanceof ShadowRoot && i < 4; root = root.host.getRootNode(), i++) {
      const t = clean(root.host.getAttribute("label") || root.host.getAttribute("aria-label"));
      if (t) return t;
    }
    return "";
  }
  // SmartRecruiters' upload boxes are spl-dropzone components with no label of their own, told apart by data-test:
  // "resume-upload" is the Resume field; "apply-with-resume-container" is the Easy-apply parser (2026-10-14)
  function dropzoneRole(el) {
    const r = el.getRootNode();
    const host = r instanceof ShadowRoot ? r.host : null;
    if (!host || host.tagName !== "SPL-DROPZONE") return "";
    const t = host.getAttribute("data-test") || "";
    return /apply-with-resume/i.test(t) ? "parser" : /resume/i.test(t) ? "resume" : /cover/i.test(t) ? "cover" : "";
  }

  function labelOf(el) {
    const zone = el.type === "file" ? dropzoneRole(el) : "";
    if (zone === "resume") return "Resume";
    if (zone === "cover") return "Cover letter";
    const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    if (ids.length) {
      const t = ids.map((i) => byIdNear(el, i)).filter(Boolean).map((n) => clean(n.textContent)).join(" ").trim();
      if (t) return t;
    }
    if (el.labels && el.labels.length) { const t = labelText(el.labels[0]); if (t) return t; }
    const aria = clean(el.getAttribute("aria-label"));
    if (aria) return aria;
    const wrap = el.closest("label");
    if (wrap) { const t = labelText(wrap); if (t) return t; }
    const carried = hostLabel(el);
    if (carried) return carried;
    let node = up(el);
    for (let i = 0; i < 6 && node; i++, node = up(node)) {
      const own = [...node.querySelectorAll(":scope > label, :scope > legend, :scope > .label, :scope > [class*='label']")]
        .find((n) => !n.querySelector("input,select,textarea"));
      if (own) { const t = clean(own.textContent); if (t) return t; }
      const prev = node.previousElementSibling;
      if (prev && prev.matches("label, legend, .label, [class*='label']") && !prev.querySelector("input,select,textarea")) {
        const t = clean(prev.textContent);
        if (t) return t;
      }
    }
    const name = clean(el.getAttribute("name"));
    return clean(el.getAttribute("placeholder")) || (/^question_[0-9a-f-]{16,}$/i.test(name) ? "" : name) || "";  // never an internal id
  }

  function groupLabel(first) {
    const fs = first.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg) return clean(lg.textContent); }
    const grp = first.closest("[role='radiogroup'],[role='group']");
    if (grp) {
      const id = grp.getAttribute("aria-labelledby");
      const byId = id && document.getElementById(id);
      if (byId) return clean(byId.textContent);
      const aria = clean(grp.getAttribute("aria-label"));
      if (aria) return aria;
    }
    const carried = hostLabel(first);
    if (carried) return carried;
    const start = first.closest("label") ? first.closest("label").parentElement : up(first);
    for (let node = start, i = 0; i < 6 && node; i++, node = up(node)) {
      const prev = node.previousElementSibling;
      if (prev && !prev.querySelector("input,select,textarea")) { const t = clean(prev.textContent); if (t) return t; }
    }
    return labelOf(first);
  }

  function contextOf(el) {
    const bits = [];
    // an upload box labelled only "Attach" (Greenhouse): its question is the label of the group around it
    // ("Please share your university transcript(s)", 2026-10-07)
    const grp = el.type === "file" && el.closest("[role='group'][aria-labelledby]");
    if (grp) {
      const t = grp.getAttribute("aria-labelledby").split(/\s+/).map((i) => document.getElementById(i)).filter(Boolean)
        .map((n) => clean(n.textContent)).join(" ");
      if (t) bits.push(t);
    }
    const fs = el.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg) bits.push(clean(lg.textContent)); }
    for (let node = el, i = 0; i < 6 && node && bits.length < 2; i++, node = node.parentElement) {
      let p = node.previousElementSibling;
      for (let k = 0; k < 3 && p; k++, p = p.previousElementSibling) {
        if (/^H[1-5]$/.test(p.tagName)) { bits.push(clean(p.textContent)); break; }
      }
    }
    const desc = (el.getAttribute("aria-describedby") || "").split(/\s+/).map((i) => document.getElementById(i))
      .filter(Boolean).map((n) => clean(n.textContent)).join(" ");
    if (desc) bits.push(desc);
    return bits.filter(Boolean).join(" | ").slice(0, 300);
  }

  const SECTION_EDUCATION = /^(?:higher |post-?secondary |your )?education(?: history| background)?$/;
  const SECTION_WORK = /^(?:(?:work|professional|relevant|your) )?(?:experience|employment(?: history)?|work history)$/;

  // Ashby (2026-10-06): "Education History" is <div data-field-path="_systemfield_education_history"> holding one
  // [class*=repeatableEducationEntry] block per school; its School box and date dropdowns have no ids of their own
  const ASHBY_ENTRY = "[class*='repeatableEducationEntry'], [class*='repeatableWorkEntry'], [class*='repeatableExperienceEntry'], [class*='repeatableEmploymentEntry']";
  const ashbyField = (el) => el.closest("[data-field-path]");
  const ashbySection = (path) => /education/i.test(path) ? "education" : /work|employ|experience/i.test(path) ? "experience" : "";
  function sectionOf(el) {
    if (modalEntry && modalEntry.root.contains(el)) return { section: modalEntry.section, index: modalEntry.index };
    const entry = el.closest(ASHBY_ENTRY);
    const af = entry && ashbyField(entry);
    if (af && ashbySection(af.getAttribute("data-field-path"))) {
      return { section: ashbySection(af.getAttribute("data-field-path")), index: Math.max(0, [...af.querySelectorAll(ASHBY_ENTRY)].indexOf(entry)) };
    }
    const wd = el.closest("[data-automation-id^='workExperience'], [data-automation-id^='education']");
    if (wd) {
      const id = wd.getAttribute("data-automation-id");
      const m = id.match(/(\d+)\s*$/);
      return { section: id.startsWith("education") ? "education" : "experience", index: m ? Math.max(0, parseInt(m[1], 10) - 1) : 0 };
    }
    const wdId = (el.id || "").match(/^(workExperience|education)-(\d+)--/);
    if (wdId) {
      // Workday numbers its entries with its own counter ("workExperience-4", "education-5"): the first one on the
      // page is the first entry, whatever its number
      const seen = [];
      for (const x of document.querySelectorAll(`[id^='${wdId[1]}-']`)) {
        const m = x.id.match(/^(?:workExperience|education)-(\d+)--/);
        if (m && !seen.includes(m[1])) seen.push(m[1]);
      }
      return { section: wdId[1] === "education" ? "education" : "experience", index: Math.max(0, seen.indexOf(wdId[2])) };
    }
    const nm = el.getAttribute("name") || el.id || "";
    // Greenhouse's newer boards: "school--0", "degree--0", "end-month--0" / "company-name--0", "title--0"
    const gh = (el.id || "").match(/^([a-z-]+?)--(\d+)$/);
    if (gh) {
      const n = gh[2];
      if (/^(school|degree|discipline)$/.test(gh[1])) return { section: "education", index: parseInt(n, 10) };
      if (/^(company-name|company|employer|title)$/.test(gh[1])) return { section: "experience", index: parseInt(n, 10) };
      // a start/end date: whichever entry the nearest enclosing block holds
      const form = el.closest("[class*='education--'], [class*='employment--']");
      if (form) return { section: /education/.test(form.className) ? "education" : "experience", index: parseInt(n, 10) };
      for (let node = el.parentElement, i = 0; node && i < 14; node = node.parentElement, i++) {
        if (node.querySelector("#school--" + n)) return { section: "education", index: parseInt(n, 10) };
        if (node.querySelector("#company-name--" + n + ", #title--" + n)) return { section: "experience", index: parseInt(n, 10) };
      }
    }
    const idx = (nm.match(/(\d+)(?!.*\d)/) || [])[1];
    if (/education/i.test(nm)) return { section: "education", index: idx ? parseInt(idx, 10) : 0 };
    if (/(employment|experience|work_history)/i.test(nm)) return { section: "experience", index: idx ? parseInt(idx, 10) : 0 };
    // Google Careers: sections are marked only by a heading ("Higher education", "Work experience")
    for (let node = el.parentElement, i = 0; node && i < 10; node = node.parentElement, i++) {
      const h = node.querySelector(":scope > h2, :scope > h3, :scope > div > h2, :scope > div > h3");
      if (!h) continue;
      // the heading must BE a section title: "Equal Employment Opportunity Information" is not work history
      const t = clean(h.textContent).toLowerCase().replace(/[*:]/g, "").trim();
      const section = SECTION_EDUCATION.test(t) ? "education" : SECTION_WORK.test(t) ? "experience" : "";
      if (!section) break;
      // which entry: the n-th field with this same label inside the section is the n-th entry
      const lab = labelOf(el);
      const same = [...node.querySelectorAll("input, textarea, select, [role='combobox']")].filter((x) => labelOf(x) === lab);
      return { section, index: Math.max(0, same.indexOf(el)) };
    }
    return universalEntry(el);
  }

  // Any site (2026-10-06): an entry is the largest block around a field that holds exactly one "School" box (or one
  // "Company" box) and nothing that belongs to the person (email, phone, files, written answers). The n-th School box
  // on the page is the n-th school. Built once per read of the form (anchors), so a page of 200 fields stays fast.
  const EDU_ANCHOR = /^(?:school|school name|university|institution|college|school or university|name of (?:school|institution))$/i;
  const WORK_ANCHOR = /^(?:company|company name|employer|employer name|organi[sz]ation|organi[sz]ation name)$/i;
  let anchors = null;
  function anchorList() {
    if (anchors) return anchors;
    anchors = { education: [], experience: [] };
    for (const x of document.querySelectorAll("input, select, [role='combobox']")) {
      if (x.closest("#tailor-panel-host") || !isVisible(x)) continue;
      const t = clean(labelOf(x)).replace(/[:?]+$/, "");
      if (EDU_ANCHOR.test(t)) anchors.education.push(x);
      else if (WORK_ANCHOR.test(t)) anchors.experience.push(x);
    }
    return anchors;
  }
  const PERSONAL = "input[type=email], input[type=tel], input[type=file], textarea, input[type=radio]";
  function universalEntry(el) {
    const a = anchorList();
    let found = null;
    for (let node = el.parentElement, i = 0; node && i < 10 && node !== document.body; node = node.parentElement, i++) {
      const edu = a.education.filter((x) => node.contains(x)), work = a.experience.filter((x) => node.contains(x));
      if (edu.length + work.length > 1 || node.querySelector(PERSONAL) || node.querySelectorAll("input, select").length > 14) break;
      if (edu.length === 1) found = { section: "education", index: a.education.indexOf(edu[0]) };
      else if (work.length === 1) found = { section: "experience", index: a.experience.indexOf(work[0]) };
    }
    return found || { section: "", index: 0 };
  }

  // What an education or work entry already says (a school or degree the site filled in from the resume), so the
  // server can match the entry to the right resume line instead of relying on the order alone.
  function entryHint(el) {
    // Ashby: only this entry's own boxes (the section holds every school: the first one's text would match them all)
    const own = el.closest(ASHBY_ENTRY);
    if (own) return [...own.querySelectorAll("input[type='text'], input[role='combobox']")].map((x) => x.value || "").join(" ").replace(/\s+/g, " ").trim().slice(0, 200);
    for (let node = el.parentElement, i = 0; node && i < 6; node = node.parentElement, i++) {
      const schools = node.querySelectorAll("input[role='combobox'], [role='combobox']");
      if (schools.length >= 2 && node.innerText.length < 1500) {
        return [...node.querySelectorAll("input, [role='combobox']")].map((x) => (x.value || x.textContent || "")).join(" ").replace(/\s+/g, " ").slice(0, 200);
      }
    }
    return "";
  }

  function isRequired(el) {
    return el.required || el.getAttribute("aria-required") === "true" ||
      !!el.closest("[role='radiogroup'][aria-required='true'],[role='group'][aria-required='true']") ||
      (el.labels && [...el.labels].some((l) => l.textContent.includes("*")));
  }

  const optionLabel = (radio) => (radio.labels && radio.labels[0] ? labelText(radio.labels[0]) : clean(radio.getAttribute("aria-label")) || radio.value);

  // Google labels the start and end dates of a work entry just "Month" and "Year", under a "Start Date:" or
  // "End Date:" heading. Say which one, so the start month is not filled with the end month.
  // "To (Actual or Expected)" on Adobe's education entries is the end date too (it was read as a second "From")
  const DATE_SIDE = /^(start date|end date|from|to|start|end)(?:\s*\([^)]*\))?\s*:?$/i;
  function withDateSide(el, label) {
    if (!/^(month|year|day)\*?$/i.test(label)) return label;
    const root = el.closest("li") || el.parentElement?.parentElement?.parentElement?.parentElement;
    if (!root) return label;
    const marks = [...root.querySelectorAll("span, div, p, label, legend, h3, h4, h5")].filter((n) =>
      ![...n.children].some((k) => clean(k.textContent)) && DATE_SIDE.test(clean(n.textContent)) &&
      (n.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
    return marks.length ? clean(marks[marks.length - 1].textContent).replace(/\s*\([^)]*\)/, "").replace(/:$/, "") + " " + label : label;
  }
  // Any site: an unlabeled month or year dropdown under a "Start Date" / "End Date" label (Ashby: two selects with
  // "Month..." and "Year..." inside div#...-startDate). What it is comes from its options; which date from the label.
  function withDateDropdown(el, label) {
    if (el.tagName !== "SELECT" || /\b(?:month|year)\b/i.test(label)) return label;
    const texts = [...el.options].map((o) => clean(o.textContent));
    const part = texts.some((t) => /^jan(?:uary)?$/i.test(t)) ? "Month" : texts.filter((t) => /^(?:19|20)\d\d$/.test(t)).length > 3 ? "Year" : "";
    if (!part) return label;
    const box = el.closest("[id$='-startDate'], [id$='-endDate'], [id$='_start_date'], [id$='_end_date']");
    const side = box ? (/start/i.test(box.id) ? "Start Date" : "End Date") : label;
    return (side ? side + " " : "") + part;
  }
  function withDatePart(el, label) {  // Workday: "...startDate-dateSectionMonth-input" is the month of "From"
    const id = el.id || "";
    if (/dateSectionMonth/i.test(id) && !/month/i.test(label)) return label + " Month";
    if (/dateSectionYear/i.test(id) && !/year/i.test(label)) return label + " Year";
    if (/dateSectionDay/i.test(id) && !/day/i.test(label)) return label + " Day";
    return label;
  }
  const valueOf = (el) => el.type === "checkbox" ? (el.checked ? "true" : "") : (el.value || "").slice(0, 300);

  function collectFields() {
    registry.clear(); fieldInfo.clear();
    anchors = null;
    const fields = [];
    const doneGroups = new Set();
    const all = deepAll("input, select, textarea");
    for (const el of all) {
      const tag = el.tagName;
      const type = (el.type || "text").toLowerCase();
      if (tag === "INPUT" && SKIP_TYPES.has(type)) continue;
      if (el.disabled || el.readOnly) continue;
      const shownByLabel = (type === "checkbox" || type === "radio") && el.labels && el.labels[0] && isVisible(el.labels[0]);
      if (type !== "file" && !isVisible(el) && !shownByLabel) continue;
      if (type === "file" && dropzoneRole(el) === "parser") continue;  // "apply with resume": it re-reads and overwrites the form
      // Workday's tag boxes ("How Did You Hear About Us?", "Country Phone Code"): the answer is the tags under the
      // search box, not its text. A box with a tag is answered: leave it. An empty one is picked from Workday's list.
      const multi = el.getAttribute("data-uxi-widget-type") === "selectinput" && el.getAttribute("data-uxi-multiselect-id");
      if (multi && document.querySelector(`[data-uxi-widget-type='selectinputlistitem'][data-uxi-multiselect-id='${CSS.escape(multi)}']`)) continue;
      if (el.parentElement && choiceButtons(el.parentElement)) continue;  // the hidden box behind Yes/No buttons
      const { section, index } = sectionOf(el);
      let id = idFor(el);
      let field;
      if (type === "radio") {
        const key = (el.name || id) + "|" + (el.form ? [...document.forms].indexOf(el.form) : -1);
        if (doneGroups.has(key)) continue;
        doneGroups.add(key);
        const radios = all.filter((r) => r.type === "radio" && r.name === el.name && r.form === el.form);
        registry.set(id, { els: radios });
        field = { id, label: groupLabel(radios[0]), name: el.name || "", type: "radio", required: radios.some(isRequired),
                  options: radios.map((r) => ({ value: r.value, label: optionLabel(r) })), section, section_index: index,
                  context: contextOf(radios[0]) };
      } else {
        registry.set(id, { els: [el] });
        field = { id, label: withDateDropdown(el, withDatePart(el, withDateSide(el, labelOf(el)))), name: el.getAttribute("name") || el.id || "", value: valueOf(el), placeholder: el.getAttribute("placeholder") || "",
                  autocomplete: el.getAttribute("autocomplete") || "", required: !!isRequired(el), section, section_index: index,
                  maxlength: el.maxLength > 0 ? el.maxLength : null, context: contextOf(el),
                  type: tag === "SELECT" ? "select" : tag === "TEXTAREA" ? "textarea" : (isCombobox(el) || multi) ? "combobox" : type,
                  entry_hint: section ? entryHint(el) : "" };
        if (tag === "SELECT") {
          field.options = [...el.options].filter((o) => o.value !== "").map((o) => ({ value: o.value, label: clean(o.textContent) }));
        }
      }
      fieldInfo.set(id, field);
      fields.push(field);
    }
    // Choice buttons (Ashby's Yes/No, 2026-10-06; many sites): 2-6 sibling buttons that each carry a pressed state,
    // under one question. Read as a radio question whose options are the buttons' words.
    const groups = new Set();
    for (const b of document.querySelectorAll("button[aria-pressed], [role='button'][aria-pressed]")) {
      const g = b.parentElement;
      if (!g || groups.has(g) || g.closest("#tailor-panel-host")) continue;
      groups.add(g);
      const btns = choiceButtons(g);
      if (!btns) continue;
      const id = idFor(g);
      const { section, index } = sectionOf(g);
      registry.set(id, { els: btns });
      const label = groupLabel(btns[0]);
      const field = { id, label, name: (g.querySelector("input") || {}).name || "", type: "radio", section, section_index: index,
                      required: /\*/.test(label) || !!g.parentElement?.querySelector("[class*='required'], [aria-required='true']"),
                      options: btns.map((x) => ({ value: clean(x.textContent), label: clean(x.textContent) })), context: contextOf(g) };
      fieldInfo.set(id, field);
      fields.push(field);
    }
    for (const btn of document.querySelectorAll("button[aria-haspopup='listbox'], button[aria-haspopup='Listbox'], div[role='combobox'][aria-haspopup='listbox']")) {
      if (!isListboxButton(btn) || !isVisible(btn) || btn.closest("#tailor-panel-host") || ACTION_WORDS.test(clean(btn.textContent))) continue;
      const id = idFor(btn);
      const { section, index } = sectionOf(btn);
      const label = labelOf(btn) || clean(btn.getAttribute("aria-label")).replace(clean(btn.textContent), "").replace(/\brequired\b/i, "").trim();
      registry.set(id, { els: [btn] });
      const hint = section ? entryHint(btn) : "";
      const ids = (btn.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
      const parts = ids.map((i) => clean((document.getElementById(i) || {}).textContent || ""));
      // Google: aria-labelledby = [label, value] ("Month", "May"); elsewhere the label minus the button's text
      const bare = ids.length >= 2 && parts[0] ? parts[0] : (label.replace(clean(btn.textContent), "").trim() || label);
      const shown = ids.length >= 2 && parts[0] ? parts.slice(1).join(" ") : clean(btn.textContent).replace(bare, "").trim();
      const field = { id, label: withDateSide(btn, bare), name: btn.getAttribute("name") || btn.id || "", value: shown.slice(0, 300),
                      type: "combobox", section, section_index: index, entry_hint: hint,
                      required: btn.getAttribute("aria-required") === "true" || /required|\*/i.test(btn.getAttribute("aria-label") || "") || isRequired(btn),
                      context: contextOf(btn) };
      fieldInfo.set(id, field);
      fields.push(field);
    }
    return fields;
  }

  // ---------- writing to the form (the only things this script may touch) ----------

  function assertSafe(el) {
    const tag = el.tagName;
    const type = (el.type || "").toLowerCase();
    const ok = tag === "SELECT" || tag === "TEXTAREA" || (tag === "INPUT" && !SKIP_TYPES.has(type));
    if (!ok) throw new Error("refusing to touch a " + tag.toLowerCase() + (type ? "[" + type + "]" : ""));
  }

  // composed: an event from a box inside a web component also reaches the page around it
  function fire(el, names) { names.forEach((n) => el.dispatchEvent(new Event(n, { bubbles: true, composed: true }))); }

  function setNative(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);  // works with React-style controlled inputs
  }

  function fillText(el, value) {
    assertSafe(el);
    if (el.tagName === "SELECT" || ["radio", "checkbox", "file"].includes((el.type || "").toLowerCase())) throw new Error("not a text field");
    if (el.getAttribute("role") === "spinbutton" && /dateSection/i.test(el.getAttribute("data-automation-id") || el.id)) {
      // Workday's MM/YYYY date: the value only takes when it arrives as typing (Adobe, 2026-10-03). Months as "05".
      const text = /dateSectionMonth|dateSectionDay/i.test(el.getAttribute("data-automation-id") || el.id) && /^\d$/.test(value) ? "0" + value : value;
      el.focus();
      if (el.select) el.select();
      if (document.execCommand("insertText", false, text) && (el.value || "").replace(/^0/, "") === text.replace(/^0/, "")) return;
    }
    setNative(el, value);
    fire(el, ["input", "change", "blur"]);
  }

  function chooseOption(entry, value) {
    const first = entry.els[0];
    assertSafe(first);
    if (first.tagName === "SELECT") {
      const opt = [...first.options].find((o) => o.value === value);
      if (!opt) throw new Error("option not found");
      setNative(first, opt.value);
      fire(first, ["input", "change", "blur"]);
      return;
    }
    const radio = entry.els.find((r) => r.value === value);
    if (!radio) throw new Error("option not found");
    assertSafe(radio);
    if (!radio.checked) radio.click();  // radios and checkboxes are the only elements ever clicked
  }

  function setCheckbox(el, want) {
    assertSafe(el);
    if (el.type !== "checkbox") throw new Error("not a checkbox");
    if (el.checked !== want) el.click();
  }
  const isPressed = (b) => b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-checked") === "true";
  function choiceButtons(g) {
    const btns = [...g.children].filter((c) => (c.tagName === "BUTTON" || c.getAttribute("role") === "button") && c.hasAttribute("aria-pressed"));
    if (btns.length < 2 || btns.length > 6) return null;
    if (btns.some((b) => { const t = clean(b.textContent); return !t || t.length > 30 || ACTION_WORDS.test(t) || !isVisible(b); })) return null;
    return btns;
  }
  function assertChoiceButton(btn, els) {
    if (!btn || !els.includes(btn) || btn.closest("#tailor-panel-host")) throw new Error("not one of this question's answers");
    const all = choiceButtons(btn.parentElement);
    if (!all || !all.includes(btn)) throw new Error("not a choice button");
  }
  function chooseButton(entry, value) {
    const btn = entry.els.find((b) => clean(b.textContent) === value);
    if (!btn) throw new Error("option not found");
    assertChoiceButton(btn, entry.els);
    if (!isPressed(btn)) btn.click();  // one of this question's own answer buttons (Yes / No), checked right above
  }


  // ---------- the cover letter for this posting (written once per page, from the tailored resume) ----------
  let coverPromise = null;
  function coverLetter() {
    if (!coverPromise) {
      coverPromise = (async () => {
        const posting = await postingText();
        return send({ type: "cover_letter", payload: { url: location.href, title: posting.title || document.title,
          page_text: posting.text, resume_id: resumeId || "", reuse: true } });
      })().catch((e) => ({ ok: false, error: e.message }));
      coverPromise.then((r) => { if (!r || !r.ok) coverPromise = null; });  // a failure is tried again on the next Fill
    }
    return coverPromise;
  }

  function showCover(r) {
    const box = ensurePanel().root.getElementById("coverdl");
    box.textContent = "";
    const bytes = Uint8Array.from(atob(r.data.base64), (ch) => ch.charCodeAt(0));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
    a.download = r.data.name;
    a.textContent = "⬇ Read the cover letter written for this posting";
    box.append(a, " (a draft from your tailored resume: add something only you know before you submit" +
      (r.data.warnings && r.data.warnings.length ? "; " + r.data.warnings.join("; ") : "") + ")");
    chrome.storage.local.set({ lastCover: { host: location.hostname, id: r.data.id, name: r.data.name, at: Date.now() } }).catch(() => {});
  }

  function attachCover(el, r) {
    assertSafe(el);
    if (el.type !== "file") throw new Error("not a file input");
    const bytes = Uint8Array.from(atob(r.data.base64), (ch) => ch.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], r.data.name, { type: "application/pdf" }));
    el.files = dt.files;
    fire(el, ["input", "change"]);
  }

  async function uploadTranscript(el, which) {
    assertSafe(el);
    if (el.type !== "file") throw new Error("not a file input");
    const r = await send({ type: "transcript", which: which || "" });  // "undergraduate": the undergraduate one
    if (!r.ok) throw new Error(r.error);
    const bytes = Uint8Array.from(atob(r.data.base64), (c) => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], r.data.name, { type: "application/pdf" }));
    el.files = dt.files;
    fire(el, ["input", "change"]);
  }

  // JazzHR (Cicero/MGT, 2026-10-14): the resume box sits in a hidden "upload" panel until the person picks "Attach
  // resume" over "Paste resume"; a file put into the hidden box is not counted. Pick that option first: the only thing
  // clicked is JazzHR's own "Attach resume" link (href="#") in the resume question.
  function revealUpload(el) {
    if (el.id !== "resumator-resume-value" || isVisible(el)) return;
    const choose = document.querySelector("#resumator-resume #resumator-choose-upload, #resumator-resume #resumator-resume-switch-upload");
    if (choose && choose.tagName === "A" && (choose.getAttribute("href") || "#") === "#" && isVisible(choose)) choose.click();
  }

  async function uploadResume(el) {
    assertSafe(el);
    if (el.type !== "file") throw new Error("not a file input");
    revealUpload(el);
    const r = await send({ type: "resume", id: resumeId });
    if (!r.ok) throw new Error(r.error);
    const bytes = Uint8Array.from(atob(r.data.base64), (c) => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], r.data.name, { type: "application/pdf" }));
    el.files = dt.files;
    fire(el, ["input", "change"]);
    // which version went to this site (the tailoring guide: know what the recruiter read): sent with "applied"
    chrome.storage.local.set({ lastResume: { host: location.hostname, id: resumeId || "", at: Date.now() } }).catch(() => {});
  }

  // ---------- custom dropdowns (react-select style comboboxes) ----------
  // A combobox is a text input with role="combobox"; its options live in the list named by its aria-controls. It is
  // opened with the pointer events a mouse press makes on ITS OWN control box, the answer is typed to filter it, and
  // the only thing ever clicked is an element with role="option" inside that same list.

  const isCombobox = (el) => el.tagName === "INPUT" && (el.getAttribute("role") || "").toLowerCase() === "combobox";
  // A hidden tab's timers run about once a minute after a few minutes (Chrome's intensive throttling), which made a
  // fill in a background tab take many minutes (2026-10-06). Hidden, the short waits are done by the extension's
  // background worker, which is not throttled that way.
  const sleep = (ms) => document.visibilityState === "hidden"
    ? send({ type: "sleep", ms }).then(() => {}, () => new Promise((r) => setTimeout(r, ms)))
    : new Promise((r) => setTimeout(r, ms));
  // "female" contains "male": an answer found inside an option must be whole words of it
  const hasWords = (text, want) => !!want && new RegExp("(^|[^a-z0-9])" + want.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "($|[^a-z0-9])").test(text);

  function comboControl(input) {
    let node = input.parentElement;
    for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
      if (/control/i.test(node.className || "")) {
        // A press on this box bubbles UP, never down to buttons inside it (a dropdown box usually holds a "toggle" button).
        // So refuse only if the box itself sits inside something a press could activate.
        return node.closest("button, a, label, summary, [role='button']") ? null : node;
      }
    }
    return null;
  }

  // an id can live in any shadow root around the box: SmartRecruiters' City box (inside spl-input) names a list that sits
  // in the spl-autocomplete around it (2026-10-14)
  function byIdUp(el, id) {
    for (let root = el.getRootNode(), i = 0; root && i < 6; i++) {
      const hit = root.getElementById ? root.getElementById(id) : null;
      if (hit) return hit;
      if (!(root instanceof ShadowRoot)) break;
      root = root.host.getRootNode();
    }
    return document.getElementById(id);
  }
  // an option's words: its own text, or (a web component's option, SmartRecruiters' spl-dropdown-item) its host's
  function optText(o) {
    const own = clean(o.textContent);
    if (own) return own;
    const r = o.getRootNode();
    return r instanceof ShadowRoot ? clean(r.host.textContent) : "";
  }

  function comboOptions(input) {
    const id = input.getAttribute("aria-controls") || input.getAttribute("aria-owns");
    const list = id && byIdUp(input, id);
    if (list) return deepAll('[role="option"]', list).filter((o) => optText(o));
    // no link to its list (Google's School name): the list shown next to this box
    const near = input.closest("div") && input.closest("div").parentElement;
    const own = near && near.querySelector("[role='listbox']");
    return own ? [...own.querySelectorAll('[role="option"]')].filter(isVisible) : [];
  }

  // ---------- Workday's search boxes ("How Did You Hear About Us?", "Field of Study", "School") ----------
  // Seen on Adobe's form (2026-10-03): type the answer, press Enter, Workday lists matches as options; picking one
  // makes it a tag under the box. Enter here only runs Workday's search (it is the box's own search key). A long answer
  // that finds nothing is searched again shorter ("Data and Business Analytics" -> "Data and Business Analytics" ->
  // "Business Analytics"). The only thing ever clicked is a result option of that search.
  const promptResults = () => [...document.querySelectorAll("[role='option'][data-automation-id='menuItem']")]
    .filter((o) => isVisible(o) && o.querySelector("[data-automation-id='promptLeafNode']"));
  const tagsOf = (input) => [...document.querySelectorAll(`[data-uxi-widget-type='selectinputlistitem'][data-uxi-multiselect-id='${CSS.escape(input.getAttribute("data-uxi-multiselect-id") || "")}']`)];
  async function chooseWorkdayPrompt(input, wanted, key) {
    assertSafe(input);
    const tries = [...new Set([wanted, wanted.split(",")[0], wanted.split(/\s+/).slice(-2).join(" ")].map(clean).filter((x) => x.length > 1))];
    for (const query of tries) {
      input.focus();
      setNative(input, query);
      fire(input, ["input"]);
      const tagsBefore = tagsOf(input).map((x) => clean(x.textContent));
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
      let options = [], tagged = null;
      for (let i = 0; i < 15 && !options.length && !tagged; i++) {
        await sleep(200);
        options = promptResults();
        // one match: Workday picks it on Enter by itself (Adobe, 2026-10-03), replacing what was there
        const now = tagsOf(input).map((x) => clean(x.textContent));
        if (now.join("|") !== tagsBefore.join("|")) tagged = now.find((x) => !tagsBefore.includes(x)) || now[0] || "";
      }
      if (tagged !== null) {
        const r = await send({ type: "choose", payload: { value: wanted, key, options: [tagged] } }).catch(() => null);
        if (r && r.ok && r.data && r.data.choice) return;  // the one match is the answer
        throw new Error(`Workday picked '${tagged}' for '${wanted}' by itself: check it`);
      }
      if (!options.length) continue;
      const want = clean(query).toLowerCase();
      const text = (o) => clean(o.textContent).toLowerCase();
      let pick = options.find((o) => text(o) === want) || options.find((o) => text(o).startsWith(want));
      if (!pick) {
        const r = await send({ type: "choose", payload: { value: wanted, key, options: options.map((o) => clean(o.textContent)) } }).catch(() => null);
        const choice = r && r.ok && r.data && r.data.choice;
        if (choice) pick = options.find((o) => clean(o.textContent) === choice);
      }
      if (!pick) continue;
      if (pick.getAttribute("role") !== "option" || pick.getAttribute("data-automation-id") !== "menuItem" ||
          pick.closest("a, button, #tailor-panel-host")) throw new Error("not a search result");
      const before = tagsOf(input).length;
      pick.click();  // a result of this box's own search, checked right above
      for (let i = 0; i < 10 && tagsOf(input).length <= before; i++) await sleep(200);
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      if (tagsOf(input).length > before) return;
    }
    setNative(input, ""); fire(input, ["input"]);
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    throw new Error("Workday's list has nothing for '" + wanted + "': pick it yourself");
  }

  async function chooseCombobox(entry, wanted, key = "") {
    const input = entry.els[0];
    assertSafe(input);
    if (input.getAttribute("data-uxi-widget-type") === "selectinput") return chooseWorkdayPrompt(input, wanted, key);
    if (!isCombobox(input)) throw new Error("not a dropdown");
    const control = comboControl(input);
    // already answered (a second Fill: a multi-select drops chosen options from its list, Databricks 2026-10-07)
    const shown = clean((control || input.parentElement || {}).innerText || "").toLowerCase();
    if (shown && hasWords(shown, clean(wanted).toLowerCase())) return;
    const press = () => {  // react-select: open it by pressing its own box; a plain typed combobox opens as you type
      if (!control) return;
      for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
        const Ev = type.startsWith("pointer") ? PointerEvent : MouseEvent;
        control.dispatchEvent(new Ev(type, { bubbles: true, cancelable: true, button: 0, buttons: type.endsWith("down") ? 1 : 0, view: window }));
      }
    };
    input.focus();
    press();
    // "University of Michigan\nAnn Arbor, MI": the school is searched, the city on the second line picks the campus
    const [first, ...more] = String(wanted).split("\n");
    const detail = clean(more.join(" ")).toLowerCase().split(",")[0].trim();
    setNative(input, more.length ? first : wanted);  // typing filters the list
    fire(input, ["input"]);
    let options = [];
    for (let i = 0; i < 12 && !options.length; i++) { await sleep(200); options = comboOptions(input); }
    const want = clean(more.length ? first : wanted).toLowerCase();
    const text = (o) => optText(o).toLowerCase();
    const matches = options.filter((o) => text(o) === want || text(o).startsWith(want) || hasWords(text(o), want));
    const byDetail = detail ? matches.filter((o) => hasWords(text(o), detail)) : [];
    let pick = byDetail.length === 1 ? byDetail[0]
      : (detail || /school/.test(key)) && matches.length > 1 ? null  // several campuses: not the first one listed
      : options.find((o) => text(o) === want) || options.find((o) => text(o).startsWith(want)) ||
        options.find((o) => hasWords(text(o), want));
    if (!pick && matches.length > 1) {  // let the server say which (it reads the city), or nobody guesses
      const r = await send({ type: "choose", payload: { value: wanted, key, options: matches.map((o) => optText(o)) } }).catch(() => null);
      const choice = r && r.ok && r.data && r.data.choice;
      pick = choice && matches.find((o) => optText(o) === choice);
      if (!pick) {
        setNative(input, ""); fire(input, ["input"]);
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        throw new Error("several options match '" + first + "': pick yours");
      }
    }
    if (!pick && options.length && options.length <= 50) {
      // the list words the answer its own way ("Springfield, Illinois" -> "Springfield, IL, US" on SmartRecruiters,
      // 2026-10-14): let the server say which of these options it is, before the list is cleared
      const r = await send({ type: "choose", payload: { value: wanted, key, options: options.map((o) => optText(o)) } }).catch(() => null);
      const choice = r && r.ok && r.data && r.data.choice;
      if (choice) pick = options.find((o) => optText(o) === choice);
    }
    if (!pick && !options.length) {
      // a list searched as you type that has nothing for the full name ("University of California, Davis"
      // on Greenhouse): search again shorter ("Davis"), and let the server say which result it is
      // ... or by its first word: a long list that loads only its first 100 options ("Information" finds "Information
      // Systems" for "Information Technology", Robinhood 2026-10-07)
      const words = first.split(/\s+/);  // the school's own name, never the city line under it
      const shorter = [...new Set([first.split(",").slice(1).join(",").trim(), words.slice(-2).join(" "), words[0]])]
        .filter((q) => q.length > 3 && q !== first);
      for (const q of shorter) {
        setNative(input, q); fire(input, ["input"]);
        let found = [];
        for (let i = 0; i < 10 && !found.length; i++) { await sleep(200); found = comboOptions(input); }
        if (!found.length) continue;
        const r = await send({ type: "choose", payload: { value: wanted, key, options: found.map((o) => optText(o)) } }).catch(() => null);
        const choice = r && r.ok && r.data && r.data.choice;
        pick = choice && found.find((o) => optText(o) === choice);
        if (pick) { options = found; break; }
      }
    }
    if (!pick) {
      // The list words it differently ("May" for 5, "I have never worked here" for No): clear the filter, read the
      // whole list, and let the server say which option means the answer.
      setNative(input, ""); fire(input, ["input"]);
      let all = [];
      for (let i = 0; i < 6 && !all.length; i++) { await sleep(200); all = comboOptions(input); }
      if (!all.length && control) {
        // Greenhouse loads a list's options the first time one opens (Guidepoint, 2026-10-07: the first Degree list
        // came up empty, the later ones had 10 options): open it again and wait, up to about 9 s
        for (let round = 0; round < 3 && !all.length; round++) {
          press();
          for (let i = 0; i < 15 && !all.length; i++) { await sleep(200); all = comboOptions(input); }
        }
      }
      console.debug("[tailor] list fallback", input.id, JSON.stringify(wanted), "typed:", options.length, "all:", all.length);
      if (all.length && all.length <= 1000) {
        const r = await send({ type: "choose", payload: { value: wanted, key, options: all.map((o) => optText(o)) } });
        const choice = r && r.ok && r.data && r.data.choice;
        console.debug("[tailor] server chose", JSON.stringify(choice), r && r.ok ? "" : JSON.stringify(r));
        if (choice) pick = all.find((o) => optText(o) === choice);
      }
      options = all.length ? all : options;
    }
    if (!pick && !options.length && !control) {
      // a typed box with no list at all (Google's Employer name takes any name): the typed text is the answer
      setNative(input, wanted); fire(input, ["input", "change"]);
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      input.blur();
      return;
    }
    if (!pick || pick.getAttribute("role") !== "option") {
      setNative(input, ""); fire(input, ["input"]);
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      throw new Error(options.length ? "no option matches '" + wanted + "'" : "the dropdown did not open");
    }
    pick.click();  // an option inside this dropdown's own list
    await sleep(200);
  }

  // ---------- suggestion lists under a plain text box (Lever's "Current location") ----------
  // The box throws typed text away unless a suggestion is picked. The only thing ever clicked is a plain text item
  // (a DIV or LI with no link, button or form control in or around it) inside the box's own container, and only the
  // one the server says means the answer ("Springfield, IL, USA" for "Springfield, Illinois").

  function suggestionsFor(input) {
    const box = input.parentElement;
    if (!box) return [];
    return [...box.querySelectorAll("div, li")].filter((el) => el !== input && isVisible(el) &&
      !el.children.length && clean(el.textContent) && /dropdown|suggest|autocomplete|result|location|option/i.test(
        (el.className || "") + " " + ((el.parentElement && el.parentElement.className) || "")));
  }

  function assertSuggestion(el, input) {
    const box = input.parentElement;
    if (!box || !box.contains(el) || el === input) throw new Error("not in this box's own list");
    if (!/^(DIV|LI)$/.test(el.tagName)) throw new Error("refusing to press a " + el.tagName.toLowerCase());
    if (el.closest("a, button, [role='button']")) throw new Error("refusing to press something inside a link or button");
    const lab = el.closest("label");  // Lever wraps each question in a label: fine only if it is this box's own
    if (lab && lab.control && lab.control !== input) throw new Error("refusing to press inside another control's label");
    if (el.querySelector("input, select, textarea, button, a")) throw new Error("refusing to press a control");
    if (/submit|apply|next|continue|send/i.test(clean(el.textContent)) && clean(el.textContent).length < 25)
      throw new Error("refusing to press an action");
  }

  async function pickSuggestion(input, wanted, key) {
    assertSafe(input);
    input.focus();
    setNative(input, wanted);
    fire(input, ["input"]);
    // Lever searches on key presses, not on input events. A letter key only (never Enter: that can send a form).
    for (const type of ["keydown", "keyup"]) input.dispatchEvent(new KeyboardEvent(type, { key: "a", bubbles: true }));
    let items = [];
    for (let i = 0; i < 15 && !items.length; i++) { await sleep(200); items = suggestionsFor(input); }
    if (!items.length) throw new Error("no suggestions appeared");
    const r = await send({ type: "choose", payload: { value: wanted, key, options: items.map((o) => clean(o.textContent)) } });
    const choice = r && r.ok && r.data && r.data.choice;
    const pick = choice && items.find((o) => clean(o.textContent) === choice);
    console.debug("[tailor] suggestions", input.id || input.name, JSON.stringify(wanted), items.length, "->", JSON.stringify(choice));
    if (!pick) throw new Error("no suggestion matches '" + wanted + "'");
    assertSuggestion(pick, input);
    pick.click();  // a plain text item in this box's own suggestion list
    await sleep(300);
    if (!(input.value || "").trim()) throw new Error("the suggestion did not stick");
  }

  // ---------- list buttons (Workday's dropdowns: Country, State, Phone type, Degree, self-ID...) ----------
  // The one kind of button this script ever presses: one that says it opens a list (aria-haspopup="listbox"), is not a
  // submit button, and whose text is not a form action. Then the only other click is an option inside that list.
  const ACTION_WORDS = /\b(?:submit|next|apply|save|continue|sign|log ?in|delete|remove|cancel|back|upload|add|review|finish)\b/i;
  const isListboxButton = (el) => !!el && (el.tagName === "BUTTON" || (el.tagName === "DIV" && el.getAttribute("role") === "combobox")) &&
    (el.getAttribute("aria-haspopup") || "").toLowerCase() === "listbox" && (el.type || "").toLowerCase() !== "submit";

  function assertListboxButton(el) {
    if (!isListboxButton(el) || ACTION_WORDS.test(clean(el.textContent)) || el.closest("#tailor-panel-host")) {
      throw new Error("refusing to press this button");
    }
  }

  function listboxOptions(btn) {
    const id = btn.getAttribute("aria-controls") || btn.getAttribute("aria-owns");
    const own = id && document.getElementById(id);
    const lists = own ? [own] : [...document.querySelectorAll("[role='listbox']")].filter(isVisible);
    const list = lists[lists.length - 1];
    // never the tags of another box (Workday's Field of Study tags are role=option too: pressing one removes it)
    return list ? [...list.querySelectorAll("[role='option']")].filter((o) => isVisible(o) &&
      o.getAttribute("data-automation-id") !== "selectedItem" && !o.closest("[data-automation-id='selectedItemList']")) : [];
  }

  async function chooseListbox(entry, wanted, key = "") {
    const btn = entry.els[0];
    const want = clean(wanted).toLowerCase();
    const cur = clean(btn.textContent).toLowerCase();
    // already chosen ("United States" for "United States of America", "Master of Science" for "Master of Science")
    if (cur && !/^(?:select|choose)/.test(cur) && (cur === want || cur.includes(want) || want.includes(cur))) return;
    assertListboxButton(btn);
    btn.click();  // a dropdown's own button: it only opens its list
    let options = [];
    for (let i = 0; i < 6 && !options.length; i++) { await sleep(200); options = listboxOptions(btn); }
    if (!options.length) {  // Google's lists open from the keyboard, not a click
      btn.focus();
      for (const key of ["ArrowDown", "Enter", " "]) {
        btn.dispatchEvent(new KeyboardEvent("keydown", { key, code: key === " " ? "Space" : key, bubbles: true }));
        for (let i = 0; i < 4 && !options.length; i++) { await sleep(200); options = listboxOptions(btn); }
        if (options.length) break;
      }
    }
    const text = (o) => clean(o.textContent).toLowerCase();
    let pick = options.find((o) => text(o) === want) || options.find((o) => text(o).startsWith(want)) ||
      options.find((o) => hasWords(text(o), want));
    if (!pick && options.length) {  // "Master of Business Administration" in a list that says "MBA"
      const r = await send({ type: "choose", payload: { value: wanted, key, options: options.map((o) => clean(o.textContent)) } }).catch(() => null);
      const choice = r && r.ok && r.data && r.data.choice;
      if (choice) pick = options.find((o) => clean(o.textContent) === choice);
    }
    if (!pick || pick.getAttribute("role") !== "option") {
      btn.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));  // close the list again
      throw new Error(options.length ? "no option matches '" + wanted + "'" : "the list did not open");
    }
    pick.click();  // an option inside the list this button opened
    await sleep(300);
  }

  // The visible box around a custom dropdown, only to draw an outline on it. Nothing is ever pressed here, so unlike
  // comboControl it may sit inside a <label> (Greenhouse wraps its dropdowns in one).
  function outlineBox(input) {
    let node = input.parentElement;
    for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
      if (/control/i.test(node.className || "")) return node;
    }
    return null;
  }

  function mark(entry, cls, title) {
    // A custom dropdown's typing box is tiny and often invisible: outline its visible box too (Greenhouse, Lever).
    const boxes = entry.els.flatMap((e) => (isCombobox(e) && outlineBox(e) ? [e, outlineBox(e)] : [e]));
    // The attribute too: React sites rewrite class lists on every update, but leave attributes they do not own alone.
    boxes.forEach((e) => { HIGHLIGHTS.forEach((c) => e.classList.remove(c)); e.classList.add(cls); e.title = title || "";
                           e.setAttribute("data-tailor", cls.replace("tailor-", "")); });
  }

  // ---------- panel ----------

  function pageText() { return (document.body ? document.body.innerText : "").slice(0, 20000); }

  // A job posting, not a form: long, and talks about qualifications or duties.
  const POSTING_WORDS = /qualifications|requirements|what we require|responsibilit|what you(?:'|’)ll do|what you will do|about the (?:job|role)|the role\b|who you are|what we(?:'re| are) looking for|what we value|your impact|duties/i;
  // The words alone are not enough (Google's application form says "legal requirements" in its footer): a posting has
  // a heading such as "Minimum qualifications" or "Responsibilities".
  const postingHead = () => [...document.querySelectorAll("h1,h2,h3,h4,h5,h6,strong,b")]
    .find((e) => e.textContent.length < 80 && POSTING_WORDS.test(e.textContent));
  const looksLikePosting = (t) => t.length > 1500 && (!!postingHead() ||
    !!document.querySelector("[data-automation-id='jobPostingDescription']"));

  // Only the posting, not the page around it: job boards show a list of other jobs next to it (Google Careers does),
  // and those would tailor the resume to the wrong mix. Start at the qualifications heading, go out until the job
  // title (the first h1/h2) is inside, then keep going only while the text barely grows.
  function postingOnlyText() {
    // Workday marks the posting itself: use it, not the cookie banner and menus around it
    const wdBody = document.querySelector("[data-automation-id='jobPostingDescription']");
    if (wdBody && wdBody.innerText.length > 300) {
      const wdTitle = document.querySelector("[data-automation-id='jobPostingHeader']");
      return [wdTitle ? wdTitle.innerText : "", wdBody.innerText].join("\n\n").slice(0, 20000);
    }
    const head = postingHead();
    if (!head) return pageText();
    let node = head;
    while (node.parentElement && node.parentElement !== document.body) {
      const up = node.parentElement;
      const hasTitle = !!node.querySelector("h1,h2");
      if (hasTitle && up.innerText.length > node.innerText.length * 1.1) break;
      node = up;
    }
    const text = node.innerText;
    return text.length > 800 ? text.slice(0, 20000) : pageText();
  }

  // The posting to tailor to: this page's, or the one seen last on this site (Google, Workday and others put the
  // posting and the form on different pages).
  let deskPosting = null;  // set when the Tailor Desk opened this form (one-click Apply): the posting it saved
  async function postingText() {
    const here = pageText();
    if (deskPosting && deskPosting.text) return { text: deskPosting.text + "\n\n" + here, title: deskPosting.title, from: deskPosting.url };
    if (looksLikePosting(here)) return { text: postingOnlyText(), title: document.title, from: "" };
    const r = await send({ type: "recall_posting", host: location.hostname }).catch(() => null);
    if (r && r.ok && r.data) return { text: r.data.text + "\n\n" + here, title: r.data.title, from: r.data.url };
    return { text: here, title: document.title, from: "" };
  }

  // After the person submits, most sites show a confirmation. Seeing one marks the posting "applied" on the Tailor
  // Desk. This only reads the page; it never submits anything.
  const CONFIRMED = /thank(?:s| you) for (?:applying|your application|submitting)|^\s*application submitted\b|\bapplication submitted(?: successfully)?[.!]|application (?:has been|was) (?:successfully )?(?:submitted|received)|we(?:'ve| have) received your application|your application (?:is complete|has been sent)|successfully (?:applied|submitted your application)/i;
  let reportedFor = "";
  // Answers the person kept (as drafted or after editing) are remembered for this site, so that when the confirmation
  // page appears they go to the answer library: the next similar question starts from their own words.
  function watchAnswer(el, question) {
    const save = () => send({ type: "keep_answer", host: location.hostname, question, text: (el.value || "").slice(0, 6000) }).catch(() => {});
    el.addEventListener("input", () => { clearTimeout(el.__tailorKeep); el.__tailorKeep = setTimeout(save, 800); });
    save();
  }

  function reportIfApplied() {
    if (reportedFor === location.href || window !== window.top) return;
    const text = pageText();
    if (text.length > 30000 || !CONFIRMED.test(text.slice(0, 8000))) return;
    reportedFor = location.href;
    chrome.storage.local.get("lastResume").catch(() => ({})).then((s) => {
      const r = s && s.lastResume && s.lastResume.host === location.hostname && Date.now() - s.lastResume.at < 6 * 3600 * 1000
        ? s.lastResume.id : "";
      return send({ type: "applied", payload: { url: location.href, title: document.title, page_text: text.slice(0, 8000),
                                                host: location.hostname, resume_id: r || "" } });
    }).catch(() => {});
  }

  let rememberedFor = "";  // the URL whose posting was saved (single-page sites change the URL without a reload)
  function rememberPosting() {
    if (rememberedFor === location.href) return;
    const t = pageText();
    if (looksLikePosting(t) && !document.querySelector("input[type=file]")) {
      rememberedFor = location.href;
      send({ type: "remember_posting", payload: { host: location.hostname, url: location.href, title: document.title,
                                                   text: postingOnlyText() } })
        .catch(() => {});
    }
  }

  // The panel sits bottom-right, where many forms put their Next / Submit button (SmartRecruiters, 2026-10-13): when it
  // covers one of the page's own step buttons, it moves to the bottom-left corner (and back when the button is gone).
  const STEP_BUTTON = /^(?:next|continue|submit|review|apply|save(?: and continue)?|proceed)\b/i;
  function keepClearOfButtons(root) {
    const check = () => {
      const box = root.getElementById("box");
      if (!box || document.visibilityState === "hidden") return;
      const left = box.classList.contains("left");
      if (left) box.classList.remove("left");  // measure where it would sit on the right
      const r = box.getBoundingClientRect();
      const hit = deepAll("button, [role='button']").some((b) => {
        if (b.closest("#tailor-panel-host") || !STEP_BUTTON.test(clean(b.textContent))) return false;
        const q = b.getBoundingClientRect();
        return q.width && q.right > r.left && q.left < r.right && q.bottom > r.top && q.top < r.bottom;
      });
      box.classList.toggle("left", hit);
    };
    setInterval(check, 2500);
    setTimeout(check, 300);
  }

  function ensurePanel() {
    if (panel) return panel;
    const host = document.createElement("div");
    host.id = "tailor-panel-host";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host { all: initial; }
        .box.left { right: auto; left: 16px; }  /* moved off the page's own Next / Submit button */
        .box { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; width: 300px; font: 13px/1.4 system-ui, sans-serif;
               color: #111; background: #fff; border: 1px solid #c7c7d1; border-radius: 12px; box-shadow: 0 8px 28px rgba(0,0,0,.22); }
        header { display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; font-weight: 600;
                 background: #4f46e5; color: #fff; border-radius: 12px 12px 0 0; }
        header button { all: unset; cursor: pointer; padding: 0 4px; }
        .body { padding: 10px 12px; max-height: min(430px, 70vh); overflow: auto; }
        .status { margin-bottom: 8px; }
        .row { display: flex; gap: 8px; margin: 8px 0; }
        .row button { flex: 1; padding: 7px 8px; border: 1px solid #4f46e5; background: #4f46e5; color: #fff; border-radius: 8px; cursor: pointer; font: inherit; }
        .row button.secondary { background: #fff; color: #4f46e5; }
        .row button:disabled { opacity: .5; cursor: default; }
        .row a.dlbtn { flex: 1; padding: 7px 8px; border: 1px solid #4f46e5; background: #fff; color: #4f46e5; border-radius: 8px;
                       font: inherit; text-align: center; text-decoration: none; }
        .row a.dlbtn.off { opacity: .5; pointer-events: none; }
        #tailornote { max-height: 4.4em; overflow-y: auto; }  /* the long tailoring summary must not push the buttons out of view */
        .ats { font-weight: 600; color: #065f46; margin-top: 8px; }
        .chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
        .chips button { all: unset; cursor: pointer; font-size: 11.5px; padding: 2px 7px; border: 1px solid #c7c7d1; border-radius: 999px; color: #374151; }
        .chips button:hover { border-color: #4f46e5; color: #4f46e5; }
        ul { margin: 6px 0 0; padding-left: 16px; } li { margin: 3px 0; cursor: pointer; color: #b91c1c; }
        .note { margin-top: 8px; font-size: 11.5px; color: #555; }
        .min .body { display: none; }
        .chat { margin-top: 8px; border-top: 1px solid #e5e5ee; padding-top: 6px; }
        .chat summary { cursor: pointer; font-weight: 600; color: #4f46e5; }
        .dl a { color: #4f46e5; font-weight: 600; }
        .chat select, .chat textarea { width: 100%; box-sizing: border-box; margin: 6px 0 0; font: inherit; padding: 5px;
                                       border: 1px solid #c7c7d1; border-radius: 6px; }
      </style>
      <div class="box" id="box">
        <header><span>Tailor Assist</span><button id="min" title="Minimize">&#8211;</button></header>
        <div class="body">
          <div class="status" id="status">Ready. Fill checks this page against your profile.</div>
          <div class="row"><button id="fill">Fill this page</button><button id="rescan" class="secondary">Re-scan</button></div>
          <div class="row"><a id="chatdl" class="dlbtn off" title="The resume this page gets: tailored to this posting when it could be. Ready after Fill this page.">⬇ Download resume PDF</a></div>
          <div id="needs"></div>
          <label class="note" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="autofill" checked>
            Fill automatically when an application opens</label>
          <label class="note" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="autostep" checked>
            Fill each new step after I click Next</label>
          <label class="note" id="autonextrow" style="display:none;gap:6px;align-items:center"><input type="checkbox" id="autonext" checked>
            Go to the next step for me (stops at Review, never submits)</label>
          <div class="row" id="stoprow" style="display:none"><button id="stopnext" class="secondary">Stop: stay on this step</button></div>
          <div id="reviewlist" class="note" style="display:none;margin:8px 0;padding:8px;border:1px solid #f59e0b;border-radius:8px;background:#fffbeb"></div>
          <div class="row" id="queuerow" style="display:none"><button id="queuenext" class="secondary">I submitted it: open the next application</button></div>
          <div class="ats" id="ats"></div>
          <div id="kwbox" style="display:none"><div class="note">The posting asks for these and your resume does not show them.
            Click one only if you really have it: it is added to your skills where postings ask for it.</div>
            <div class="chips" id="kwchips"></div></div>
          <div class="note" id="tailornote"></div>
          <div class="note dl" id="download"></div>
          <div class="note dl" id="coverdl"></div>
          <details class="chat" id="chat">
            <summary>Help me write an answer or tailor my resume</summary>
            <select id="chatfield"></select>
            <textarea id="chatin" rows="3" placeholder="Tell me what you want, e.g. 3-4 sentences, lead with my research work. For a cover letter: a referral's name, why this company"></textarea>
            <div class="row"><button id="chatgo">Write it</button></div>
            <div class="note" id="chatnote"></div>
          </details>
          <div class="note">You review everything and click Submit yourself. Tailor Assist never submits.
            <span style="float:right;color:#999">v${chrome.runtime.getManifest().version}</span></div>
        </div>
      </div>`;
    keepClearOfButtons(root);
    root.getElementById("fill").addEventListener("click", () => run());
    root.getElementById("rescan").addEventListener("click", () => run());
    root.getElementById("min").addEventListener("click", () => root.getElementById("box").classList.toggle("min"));
    root.getElementById("chat").addEventListener("toggle", () => { if (root.getElementById("chat").open) refreshChatFields(); });
    root.getElementById("chatgo").addEventListener("click", () => chatWrite());
    root.getElementById("stopnext").addEventListener("click", () => { cancelNext = true; });
    root.getElementById("queuenext").addEventListener("click", async (e) => {
      // the person says they submitted: the posting is marked applied and the next one in the queue opens
      e.target.disabled = true;
      const r = await send({ type: "queue_next", payload: { url: location.href, title: document.title,
        page_text: pageText().slice(0, 8000), host: location.hostname } }).catch(() => null);
      const next = r && r.ok && r.data && r.data.next;
      setStatus(next ? `Marked applied. Next: ${next.company ? next.company + ": " : ""}${next.title} (opened in a new tab).`
        : "Marked applied. Nothing else is queued (Start applying on the Desk queues your saved postings).");
    });
    const auto = root.getElementById("autofill");
    chrome.storage.local.get("autoFill").then((s) => { auto.checked = s.autoFill !== false; }).catch(() => {});
    auto.addEventListener("change", () => chrome.storage.local.set({ autoFill: auto.checked }).catch(() => {}));
    {  // every site with steps (2026-10-06); on a one-page form it only says when the page is ready to submit
      root.getElementById("autonextrow").style.display = "flex";
      const box = root.getElementById("autonext");
      chrome.storage.local.get("autoNext").then((s) => { box.checked = s.autoNext !== false; }).catch(() => {});
      box.addEventListener("change", () => chrome.storage.local.set({ autoNext: box.checked }).catch(() => {}));
    }
    document.documentElement.appendChild(host);
    panel = { host, root, status: root.getElementById("status"), needs: root.getElementById("needs"),
              fill: root.getElementById("fill"), rescan: root.getElementById("rescan"), tailor: root.getElementById("tailornote"),
              download: root.getElementById("download"), chatdl: root.getElementById("chatdl") };
    return panel;
  }

  function setStatus(text) { ensurePanel().status.textContent = text; }

  // ---------- "Help me write an answer": a small chat for the free-text questions ----------
  // Pick a question, say what you want ("3-4 sentences, lead with my research work"), and the local model writes into
  // that box. Ask again to change it: the text already in the box is sent along so it is revised, not restarted.
  // Only text boxes are ever written to, and the server refuses sensitive questions.

  const RESUME_KEY = "__resume__";
  const COVER_KEY = "__cover__";
  // a cover-letter box: an upload ("Cover Letter" / "cover_letter") or a text box asking for one
  const coverBoxes = () => [...document.querySelectorAll("input[type=file], textarea")].filter((el) =>
    /cover\s*letter|cover_letter|coverletter/i.test(labelOf(el) + " " + (el.name || "") + " " + (el.id || "")));
  const isWritable = (f) => f.type === "textarea" || (f.type === "text" && (f.label || "").length >= 25);

  function refreshChatFields() {
    const p = ensurePanel();
    const sel = p.root.getElementById("chatfield");
    const previous = sel.selectedOptions[0] && sel.selectedOptions[0].dataset.key;
    const fields = collectFields().filter(isWritable);
    sel.textContent = "";
    for (const f of fields) {
      const o = document.createElement("option");
      o.dataset.key = fieldKey(f);
      o.textContent = (f.label || f.name || "text box").slice(0, 60);
      o.selected = o.dataset.key === previous;
      sel.appendChild(o);
    }
    const mine = document.createElement("option");  // last, so the chat still opens on a question
    mine.dataset.key = RESUME_KEY;
    mine.textContent = "My resume for this posting";
    const cover = document.createElement("option");
    cover.dataset.key = COVER_KEY;
    cover.textContent = "Cover letter for this posting";
    cover.selected = previous === COVER_KEY;
    sel.appendChild(cover);
    mine.selected = previous === RESUME_KEY || !fields.length;
    sel.appendChild(mine);
    p.root.getElementById("chatgo").disabled = false;
    p.root.getElementById("chatnote").textContent = fields.length ? "" :
      "No free-text questions on this page. You can still re-tailor your resume, e.g. \"lean on the AI/ML qualification\".";
  }

  async function chatWrite() {
    const p = ensurePanel();
    const note = p.root.getElementById("chatnote");
    const go = p.root.getElementById("chatgo");
    const chosen = p.root.getElementById("chatfield").selectedOptions[0];
    if (!chosen) return;
    go.disabled = true;
    if (chosen.dataset.key === COVER_KEY) {  // write a letter, attach it (or put it in the letter box), offer it
      go.disabled = true;
      note.textContent = "Writing your cover letter (about two minutes)...";
      try {
        const posting = await postingText();
        const r = await send({ type: "cover_letter", payload: { url: location.href, title: posting.title || document.title,
          page_text: posting.text, instruction: p.root.getElementById("chatin").value.trim(), resume_id: resumeId || "" } });
        if (!r || !r.ok) { note.textContent = "Could not write it: " + ((r && r.error) || "the server did not answer"); return; }
        const bytes = Uint8Array.from(atob(r.data.base64), (ch) => ch.charCodeAt(0));
        const boxes = coverBoxes();
        const file = boxes.find((el) => el.type === "file");
        const area = boxes.find((el) => el.tagName === "TEXTAREA");
        let where = "";
        if (file) {
          assertSafe(file);
          const dt = new DataTransfer();
          dt.items.add(new File([bytes], r.data.name, { type: "application/pdf" }));
          file.files = dt.files;
          fire(file, ["input", "change"]);
          where = "Attached to the form's cover letter upload. ";
        } else if (area) {
          fillText(area, r.data.text);
          mark({ els: [area] }, "tailor-draft", "Draft: read it, edit it, make it yours");
          where = "Placed in the cover letter box. ";
        }
        const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
        // Michigan Career Center: AI helps draft and tailor; personalization and the person's review make it theirs
        note.textContent = where + "It is a draft: read it, and add something only you know (a referral, why this company) before you submit. ";
        const a = document.createElement("a");
        a.href = url; a.download = r.data.name; a.textContent = "⬇ Download " + r.data.name;
        note.append(a);
        if (r.data.warnings && r.data.warnings.length) note.append(" (" + r.data.warnings.join("; ") + ")");
        if (!where) note.prepend("No cover letter box on this page: ");
      } catch (e) {
        note.textContent = "Could not write it: " + e.message;
      } finally {
        go.disabled = false;
      }
      return;
    }
    if (chosen.dataset.key === RESUME_KEY) {  // re-tailor the resume, steered by what the person typed
      try {
        note.textContent = "Tailoring your resume...";
        const hit = await tailorResume(p, { force: true, instruction: p.root.getElementById("chatin").value.trim() });
        let attached = 0;
        for (const el of document.querySelectorAll("input[type=file]")) {
          if (/r[eé]sum[eé]|\bcv\b/i.test(labelOf(el) + " " + (el.name || "") + " " + (el.id || ""))) {
            await uploadResume(el);
            attached++;
          }
        }
        // when tailoring failed, say why (your default resume is used), instead of looking as if nothing happened
        const why = hit && hit.id ? "Done. " : ((hit && hit.summary) || "Not tailored: the server did not answer.") +
          " Your default resume is used. ";
        note.textContent = why + (attached ? "Attached to the form. " :
          "Click ⬇ Download resume PDF (under Fill this page) and drop it into the form's resume box (remove the old file first). ") + "Ask again to change the emphasis.";
      } catch (e) {
        note.textContent = "Could not tailor: " + e.message;
      } finally {
        go.disabled = false;
      }
      return;
    }
    note.textContent = "Writing...";
    try {
      const f = collectFields().find((x) => fieldKey(x) === chosen.dataset.key);
      const entry = f && registry.get(f.id);
      if (!entry) { note.textContent = "That question is no longer on the page. Re-scan and pick it again."; return; }
      const el = entry.els[0];
      const r = await send({ type: "answer", payload: { question: f.label || f.context, page_text: pageText(), hint: f.context || "",
        max_chars: f.maxlength, instruction: p.root.getElementById("chatin").value.trim(), current: el.value || "" } });
      if (!r || !r.ok) { note.textContent = (r && r.error) || "The server did not answer."; return; }
      fillText(el, r.data.text);
      mark(entry, "tailor-draft", "Draft: read it, edit it, make it yours");
      el.scrollIntoView({ block: "center" });
      note.textContent = "Placed in the box. Read it and edit it, or ask again to change it (shorter, another project...)." +
        (r.data.warnings.length ? " (" + r.data.warnings.join("; ") + ")" : "");
    } catch (e) {
      note.textContent = "Could not write: " + e.message;
    } finally {
      go.disabled = false;
    }
  }

  function renderNeeds(items) {
    const box = ensurePanel().needs;
    box.textContent = "";
    if (!items.length) return;
    const title = document.createElement("div");
    title.textContent = "Needs you:";
    title.style.fontWeight = "600";
    const ul = document.createElement("ul");
    for (const it of items) {
      const li = document.createElement("li");
      li.textContent = it.label + (it.reason ? " (" + it.reason + ")" : "");
      if (it.entry) li.addEventListener("click", () => { const el = it.entry.els[0]; el.scrollIntoView({ block: "center" }); el.focus({ preventScroll: true }); });
      ul.appendChild(li);
    }
    box.append(title, ul);
  }

  // ---------- the run ----------

  const fieldKey = (f) => [f.name, f.label, f.type].join("|");

  // If the page asks for a resume, ask the local server to tailor it to the posting (this page's, or the one seen
  // just before on this site). It only uses what the resume already says, and wordings the person approved. If it
  // cannot, the default resume is used. `instruction` comes from the chat box ("lean on the AI/ML qualification").
  const asksForResume = () => !!document.querySelector("input[type=file]") ||
    /(?:upload|attach|add)[^.\n]{0,40}r[eé]sum[eé]|r[eé]sum[eé]\s*\/?\s*cv/i.test(pageText());

  async function tailorResume(p, opts = {}) {
    resumeId = "";
    if (!opts.force && !asksForResume()) return null;
    const key = location.href + "|" + (opts.instruction || "");
    let hit = opts.force ? null : tailoredFor.get(key);
    if (!hit) {
      setStatus("Tailoring your resume to this posting...");
      const posting = await postingText();
      const t = await send({ type: "tailor", payload: { url: location.href, title: posting.title, page_text: posting.text,
                                                         instruction: opts.instruction || "" } });
      hit = t && t.ok && t.data ? { id: t.data.applied ? t.data.id : "", summary: t.data.summary || "", report: t.data.report || {} }
                                : { id: "", summary: "Not tailored: " + ((t && t.error) || "the server did not answer") };
      if (hit.id && posting.from) hit.summary += " (posting read from the previous page)";
      // Remember only real answers from the server. A failure (no token yet, server not running) must be retried on the
      // next Fill, or the default resume keeps being used after the problem is fixed.
      if (t && t.ok) tailoredFor.set(key, hit);
    }
    resumeId = hit.id;
    p.tailor.textContent = hit.id ? hit.summary.replace(/^ATS match [^.]*\.\s*/, "") : hit.summary + " Using your default resume.";
    showKeywords(p, hit);
    await offerDownload(p);
    return hit;
  }

  // The guide's step 3 (2026-10-06): the keyword match before and after tailoring, and the posting's keywords the
  // resume lacks. "I have this" saves one as the person's own skill and tailors again (never added without the click).
  function showKeywords(p, hit) {
    const rep = (hit && hit.report) || {};
    const ats = p.root.getElementById("ats"), box = p.root.getElementById("kwbox"), chips = p.root.getElementById("kwchips");
    ats.textContent = hit && hit.id && rep.ats_after ? `ATS keyword match: ${rep.ats_before}% → ${rep.ats_after}%` +
      (rep.keywords_added && rep.keywords_added.length ? ` (added: ${rep.keywords_added.join(", ")})` : "") +
      (rep.ai_keywords && rep.ai_keywords.length ? `. AI-worded bullets for: ${rep.ai_keywords.map((k) => k.toLowerCase()).join(", ")}` : "") : "";
    chips.textContent = "";
    const missing = (rep.missing_keywords || []).slice(0, 8);
    box.style.display = missing.length ? "block" : "none";
    for (const name of missing) {
      const b = document.createElement("button");
      b.textContent = "+ " + name;
      b.title = "I have this skill: add it where postings ask for it";
      b.addEventListener("click", () => confirmSkill(p, name, b));
      chips.appendChild(b);
    }
  }

  async function confirmSkill(p, name, chip) {
    if (!window.confirm(`Add "${name}" to your skills?\n\nOnly if you really have it: tailoring will then put it on resumes whose postings ask for it.`)) return;
    chip.textContent = "✓ " + name;
    const r = await send({ type: "confirm_skill", name });
    if (!r || !r.ok) { setStatus("Could not save that skill: " + ((r && r.error) || "the server did not answer")); return; }
    tailoredFor.clear();
    const hit = await tailorResume(p, { force: true });
    // the page already holds the earlier resume: attach the new one in its place
    for (const el of document.querySelectorAll("input[type=file][data-tailor='filled']")) {
      if (!(el.files && el.files[0] && /resume|cv/i.test(el.files[0].name))) continue;  // only the box holding the resume
      try { await uploadResume(el); } catch (e) { /* a site that refuses a second file: the download button has it */ }
    }
    setStatus(`Saved "${name}" as your skill and tailored again` + (hit && hit.report && hit.report.ats_after ? ` (ATS ${hit.report.ats_after}%).` : "."));
  }

  // For upload boxes that are not a plain file input (Google Careers): a link the person clicks to save the PDF.
  async function offerDownload(p) {
    p.download.textContent = "";
    const r = await send({ type: "resume", id: resumeId });
    if (!r || !r.ok) return;
    const bytes = Uint8Array.from(atob(r.data.base64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
    // the panel's own button: a plain download link the person clicks, on every page
    if (p.chatdl.href) URL.revokeObjectURL(p.chatdl.href);
    p.chatdl.href = url;
    p.chatdl.download = r.data.name;
    p.chatdl.classList.remove("off");
    if (document.querySelector("input[type=file]")) return;
    const a = document.createElement("a");
    a.href = url;
    a.download = r.data.name;
    a.textContent = "Download this PDF";
    p.download.append(a, " and drop it into the form's resume box.");
  }

  // ---------- going to the next step (Workday only; never Submit) ----------
  // The person asked for it (2026-10-02): fill a step, go on to the next, and stop at Review for them to check and
  // submit. Only Workday's own step button ("Save and Continue" / "Next", data-automation-id pageFooterNextButton), only
  // when the step has nothing left for the person and no error on the page, after a 3-second countdown they can stop,
  // never on the Review step, never more than 8 steps in a row, and never a button that says submit.
  // Any site since 2026-10-06 (the person: "navigate throughout multiple pages on its own ... stop at review"): a step
  // button whose whole text is one of these words, never one that also says submit/apply/send/finish/confirm/sign.
  const NEXT_TEXT = /^(?:save and continue|save & continue|save and next|next|next step|continue|continue to next step|proceed|next page|review|review application|review (?:my|your) application)$/i;
  const NEVER_PRESS = /submit|apply|send|finish|confirm|sign|delete|remove|withdraw|cancel|previous|back|exit|draft|later|upload/i;
  const FINAL_TEXT = /^(?:submit|submit application|submit my application|submit your application|send application|send|apply|apply now|finish|complete|complete application)$/i;
  const isWorkday = () => /(?:^|\.)myworkdayjobs\.com$|(?:^|\.)myworkday\.com$|(?:^|\.)myworkdaysite\.com$/.test(location.hostname);
  const activeStepName = () => clean((document.querySelector("[data-automation-id='progressBarActiveStep']") || {}).innerText || "");
  const stepWords = (b) => clean((b.tagName === "INPUT" ? b.value : b.textContent) || b.getAttribute("aria-label") || "").replace(/\s*[›»→>]+$/, "").replace(/^[‹«←<]+\s*/, "");
  // older systems (Taleo) draw the step button as <input type="button" value="Save and Continue">
  const isStepControl = (b) => b.tagName === "BUTTON" || b.getAttribute("role") === "button" || (b.tagName === "INPUT" && b.type === "button");
  // The step's own button: Workday's footer button, elsewhere the one visible button with Next-type words (two
  // different ones, "Next" and "Continue", is not a clear step button: nothing is pressed).
  function nextButton() {
    if (isWorkday()) {
      const b = document.querySelector("[data-automation-id='pageFooterNextButton']");
      return b && isVisible(b) && !b.disabled ? b : null;
    }
    if (NO_AUTO_NEXT.test(location.hostname)) return null;
    // inside web components too (SmartRecruiters' Next is a <button> in spl-button's shadow root, 2026-10-13)
    const found = deepAll("button, [role='button'], input[type='button']").filter((b) => isVisible(b) && !b.disabled &&
      b.getAttribute("aria-disabled") !== "true" && !b.closest("#tailor-panel-host") && NEXT_TEXT.test(stepWords(b)) &&
      !NEVER_PRESS.test(stepWords(b)));
    return found.length && new Set(found.map((b) => stepWords(b).toLowerCase())).size === 1 ? found[found.length - 1] : null;
  }
  // The Review step (or the last page): Workday's progress bar says Review; elsewhere the current step or a heading
  // says Review, or the page offers Submit and no step button.
  // a page that warns its button sends the application ("By clicking Continue you submit your application") is the end
  const SENDS_ON_NEXT = /(?:click|press|select)\w*\s+(?:on\s+)?["“']?(?:next|continue|proceed)["”']?[^.]{0,100}\b(?:submit|send|agree|consent|certify|attest)|application will be (?:submitted|sent)|(?:this is the|you(?:'re| are) on the) (?:final|last) step|submit your application on the next/i;
  // a school job board whose "Continue" hands the documents in (a business school's board on 12twenty, 2026-10-13): never stepped
  const NO_AUTO_NEXT = /(?:^|\.)12twenty\.com$/;
  function onReviewStep() {
    if (NO_AUTO_NEXT.test(location.hostname)) return true;
    if (SENDS_ON_NEXT.test(pageText().slice(0, 20000))) return true;
    if (isWorkday()) return /\breview\b/i.test(activeStepName());
    const current = document.querySelector("[aria-current='step'], li[class*='active'][class*='step'], [class*='step'][class*='current']");
    if (current && /\breview\b/i.test(clean(current.textContent))) return true;
    if ([...document.querySelectorAll("h1, h2, h3")].some((h) => isVisible(h) && /^review\b|review (?:your|and submit)|application review/i.test(clean(h.textContent)))) return true;
    const finals = deepAll("button, [role='button'], input").some((b) => isVisible(b) && !b.closest("#tailor-panel-host") &&
      FINAL_TEXT.test(clean(b.textContent || b.value || "")));
    return finals && !nextButton();
  }
  const pageErrors = () => !!document.querySelector("[data-automation-id='errorBanner'], [data-automation-id='errorMessage']") ||
    [...document.querySelectorAll("[aria-invalid='true'], [role='alert']")].some((e) => isVisible(e) && !e.closest("#tailor-panel-host") &&
      (e.getAttribute("aria-invalid") === "true" || clean(e.textContent).length > 3));
  let cancelNext = false;
  let autoNextCount = 0;
  const FLOW_MS = 10 * 60 * 1000;  // a step loaded as a new page continues the flow on the same site for 10 minutes

  function assertNextButton(btn) {
    if (!btn || !isStepControl(btn) || btn.closest("#tailor-panel-host"))
      throw new Error("not the step's Next button");
    if (isWorkday() && btn.getAttribute("data-automation-id") !== "pageFooterNextButton") throw new Error("not Workday's step button");
    const text = stepWords(btn);
    if (!NEXT_TEXT.test(text) || NEVER_PRESS.test(text) || FINAL_TEXT.test(text)) throw new Error("refusing to press '" + text + "'");
    if (onReviewStep()) throw new Error("this is the Review step: you check it and submit");
  }

  // Workday starts My Experience with one work entry and one school. The server says how many the resume needs
  // ({experience: 3}); the group's own Add button is pressed that many times (at most 6), then the page is filled again.
  const groupHeading = (b) => { const g = b.closest("[role='group']"); const h = g && g.querySelector("h3, h4"); return h ? clean(h.textContent).toLowerCase() : ""; };
  // Greenhouse (2026-10-06): "Add another" (button.add-another-button) inside .education--container / .employment--container
  const isGreenhouse = () => /(?:^|\.)greenhouse\.io$/.test(location.hostname);
  const isGoogleCareers = () => location.hostname === "www.google.com" && location.pathname.startsWith("/about/careers/");
  // Ashby: its form marks every field with data-field-path="_systemfield_..." (also on company sites that host it)
  const isAshby = () => location.hostname === "jobs.ashbyhq.com" || !!document.querySelector(".ashby-application-form-container [data-field-path^='_systemfield_']");
  const ASHBY_ADD = { education: /^\+?\s*add education$/i, "work experience": /^\+?\s*add (?:work )?(?:experience|employment|work history)$/i };
  const ashbyAddButton = (heading) => [...document.querySelectorAll("[data-field-path] button")].find((b) =>
    ASHBY_ADD[heading].test(clean(b.textContent)) && ashbySection(ashbyField(b).getAttribute("data-field-path")) === (heading === "education" ? "education" : "experience") && isVisible(b));
  // Google: the card a button sits in is named by its heading ("Work experience", "Higher education")
  const googleCard = (el) => { for (let n = el; n; n = n.parentElement) { const h = n.querySelector(":scope > h2, :scope > div > h2, :scope > div > div > h2"); if (h) return clean(h.textContent).toLowerCase(); } return ""; };
  const ghContainer = (heading) => heading === "education" ? ".education--container" : ".employment--container";
  // Any site (the person's idea, 2026-10-06): a button whose words start with "Add" or "+", never one that also says
  // submit/apply/delete/save or adds a file or a link, that belongs to the work or education section: its own words
  // name the section ("Add Education", "Add another job"), or the smallest block holding it and that section's fields
  // holds no field of the other section.
  const ADD_WORDS = /^\+?\s*add\b|^\+$/i;
  const NOT_AN_ENTRY = /submit|apply|send|delete|remove|cancel|save|next|continue|cover|letter|attach|file|resume|link|website|url|portfolio|question|social|language|skill|certific|referen|award|publication/i;
  const EDU_WORDS = /educat|school|degree|universit|college|academic/i;
  const WORK_WORDS = /experience|employ|work|job|position|career/i;
  const wordsOf = (b) => clean(b.textContent) || clean(b.getAttribute("aria-label"));
  function sectionOfButton(btn) {
    const t = wordsOf(btn);
    if (EDU_WORDS.test(t) !== WORK_WORDS.test(t)) return EDU_WORDS.test(t) ? "education" : "experience";
    const placed = [...fieldInfo.values()].filter((f) => f.section).map((f) => [registry.get(f.id)?.els[0], f.section]).filter((x) => x[0]);
    for (let node = btn.parentElement, i = 0; node && i < 8 && node !== document.body; node = node.parentElement, i++) {
      const inside = new Set(placed.filter(([el]) => node.contains(el)).map((x) => x[1]));
      if (inside.size === 1) return [...inside][0];
      if (inside.size > 1) return "";
    }
    // an empty section (its entries are added through an editor): the section's own title, when it is the only one
    for (let node = btn.parentElement, i = 0; node && i < 4 && node !== document.body; node = node.parentElement, i++) {
      const titles = [...node.querySelectorAll("h1, h2, h3, h4, h5, legend")].map((h) => clean(h.textContent).toLowerCase().replace(/[*:]/g, "").trim())
        .map((t) => SECTION_EDUCATION.test(t) ? "education" : SECTION_WORK.test(t) ? "experience" : "").filter(Boolean);
      if (titles.length) return new Set(titles).size === 1 ? titles[0] : "";
    }
    return "";
  }
  function isAddButtonFor(btn, heading) {
    const t = wordsOf(btn);
    const link = btn.tagName === "A" && !/^(?:#|javascript:|$)/i.test(btn.getAttribute("href") || "");
    return (btn.tagName === "BUTTON" || btn.getAttribute("role") === "button" || (btn.tagName === "A" && !link)) &&
      t.length <= 40 && ADD_WORDS.test(t) && !NOT_AN_ENTRY.test(t) && !btn.disabled && isVisible(btn) &&
      !btn.closest("#tailor-panel-host") && sectionOfButton(btn) === (heading === "education" ? "education" : "experience");
  }
  // A button that would send the application's own form: a submit-type button (a <button> in a form is one unless it
  // says otherwise) of a form holding the email, phone or resume box. Pressing it could submit the application, so the
  // universal Add and the editors' Save never do; the person presses it. (An editor's own small form is fine.)
  function sendsApplication(btn, editor = null) {
    if (String(btn.type || "").toLowerCase() !== "submit" || !btn.form) return false;
    if (editor && editor.contains(btn.form)) return false;
    return !!btn.form.querySelector("input[type=email], input[type=file], input[type=tel]");
  }
  const universalAddButton = (heading) => [...document.querySelectorAll("button, [role='button'], a")].find((b) => isAddButtonFor(b, heading));
  function assertAddButton(btn, heading) {
    if (!btn || !(btn.tagName === "BUTTON" || btn.getAttribute("role") === "button" || btn.tagName === "A") || btn.closest("#tailor-panel-host")) throw new Error("not an Add button");
    if (!/^(?:work experience|education)$/.test(heading)) throw new Error("not a work or education group");
    if (isAshby()) {
      const f = ashbyField(btn);
      if (!ASHBY_ADD[heading].test(clean(btn.textContent)) || !f || ashbySection(f.getAttribute("data-field-path")) !== (heading === "education" ? "education" : "experience")) throw new Error("not the " + heading + " section");
      return;
    }
    if (isWorkday() || isGreenhouse() || isGoogleCareers()) {  // the sites with known buttons: only those exact buttons
      if (btn.tagName !== "BUTTON") throw new Error("not an Add button");
      if (!/^add(?: another(?: job| degree)?)?$/i.test(clean(btn.textContent))) throw new Error("refusing to press '" + clean(btn.textContent) + "'");
    }
    if (isWorkday()) {
      if (btn.getAttribute("data-automation-id") !== "add-button" || groupHeading(btn) !== heading) throw new Error("not the " + heading + " group");
    } else if (isGreenhouse()) {
      if (!btn.classList.contains("add-another-button") || !btn.closest(ghContainer(heading))) throw new Error("not the " + heading + " section");
    } else if (isGoogleCareers()) {
      const want = heading === "education" ? /^add another degree$/i : /^add another job$/i;
      const card = heading === "education" ? "higher education" : "work experience";
      if (!want.test(clean(btn.textContent)) || googleCard(btn) !== card) throw new Error("not the " + card + " section");
    } else if (!isAddButtonFor(btn, heading)) throw new Error("not the " + heading + " section's Add button");
    else if (sendsApplication(btn)) throw new Error("this Add button would send the application's form");
  }
  async function addEntries(add) {
    let pressed = 0;
    for (const [section, n] of Object.entries(add || {})) {
      const heading = section === "experience" ? "work experience" : section === "education" ? "education" : "";
      for (let i = 0; i < Math.min(n, 6); i++) {
        const btn = isAshby() ? ashbyAddButton(heading) : isGreenhouse()
          ? document.querySelector(`${ghContainer(heading)} .add-another-button`)
          : isGoogleCareers()
            ? [...document.querySelectorAll("button")].filter((b) => (heading === "education" ? /^add another degree$/i : /^add another job$/i)
                .test(clean(b.textContent)) && isVisible(b)).pop()
            : isWorkday() ? [...document.querySelectorAll("[data-automation-id='add-button']")].find((b) => groupHeading(b) === heading)
            : universalAddButton(heading);
        console.debug("[tailor] add", heading, i, btn ? clean(btn.textContent) : "no button");
        if (!btn) break;
        assertAddButton(btn, heading);
        const before = deepAll("input, textarea").length;
        btn.click();  // the Work Experience / Education group's own Add button, checked right above
        pressed++;
        let grew = false;
        for (let k = 0; k < 12; k++) { await sleep(300); if (deepAll("input, textarea").length > before) { grew = true; break; } }
        if (!grew) break;  // nothing new appeared: not the right button, or the site will not add more; never press again
        await sleep(700);  // let the site finish drawing the new entry: Ashby ignored a second press 0.1 s after the first
      }
    }
    return pressed;
  }
  let addRounds = 0;

  // Any site (2026-10-07): "Add" opens a small editor (a dialog) for one school or job, with its own Save (some Oracle,
  // SuccessFactors and Avature sites). When a section shows none of the person's entries, each one is added that way:
  // Add, fill only the editor's boxes as the k-th school or job, press the editor's own Save, wait for it to close.
  let modalEntry = null;
  const EDITOR = "[role='dialog'], [aria-modal='true'], dialog[open]";
  function entryEditor() {
    return [...document.querySelectorAll(EDITOR)].filter((d) => isVisible(d) && !d.closest("#tailor-panel-host") &&
      d.querySelectorAll("input, select, textarea").length > 0 && d.querySelectorAll("input, select, textarea").length <= 24 &&
      !d.querySelector("input[type=file], input[type=email], input[type=password]")).pop() || null;
  }
  const EDITOR_SAVE = /^(?:save|done|ok|add|update|save (?:and|&) close|save changes|save entry|add (?:this )?(?:education|experience|degree|school|job|position|employment|work experience))$/i;
  const buttonWords = (b) => wordsOf(b) || clean(b.value || "");
  function editorSaveButton(dlg) {
    return [...dlg.querySelectorAll("button, [role='button'], input[type=button]")]
      .find((b) => isVisible(b) && !b.disabled && EDITOR_SAVE.test(buttonWords(b))) || null;
  }
  function assertEditorSave(btn, dlg, section) {
    if (!btn || !dlg || !dlg.isConnected || !dlg.contains(btn) || btn.closest("#tailor-panel-host")) throw new Error("not the editor's Save");
    const t = buttonWords(btn);
    if (!EDITOR_SAVE.test(t) || /submit|apply|send|sign|finish/i.test(t)) throw new Error("refusing to press '" + t + "'");
    // the editor holds one school or one job: never a dialog of the application itself
    const boxes = [...dlg.querySelectorAll("input, select, [role='combobox']")].map((x) => clean(labelOf(x)).replace(/[:?*]+$/, "").trim());
    const anchor = section === "education" ? EDU_ANCHOR : WORK_ANCHOR;
    if (boxes.filter((t) => anchor.test(t)).length !== 1) throw new Error("not one " + (section === "education" ? "school" : "job") + "'s editor");
    if (dlg.querySelector("input[type=file], input[type=email], input[type=password]")) throw new Error("not an entry editor");
    if (sendsApplication(btn, dlg)) throw new Error("this Save would send the application's form");
  }
  // the section an Add button belongs to: the nearest block with a title (never the whole form, whose other sections
  // name the same university)
  function sectionBox(btn) {
    for (let n = btn.parentElement, i = 0; n && i < 6 && n !== document.body; n = n.parentElement, i++) {
      if (n.querySelector("h1, h2, h3, h4, h5, legend")) return n;
    }
    return btn.parentElement || document.body;
  }
  async function modalEntries(state) {
    if (addRounds >= 2 || isWorkday() || isGreenhouse() || isGoogleCareers() || isAshby()) return 0;
    let list = null, added = 0;
    for (const [section, heading] of [["education", "education"], ["experience", "work experience"]]) {
      if (!universalAddButton(heading)) continue;
      anchors = null;
      if (anchorList()[section].length) continue;  // the section has its own boxes: Add draws entries in the page
      list = list || await send({ type: "entries", resume_id: resumeId || "" });
      if (!list || !list.ok) return added;
      const mine = list.data[section] || [];
      const shown = sectionBox(universalAddButton(heading)).innerText || "";
      if (mine.some((m) => m.keys.some((k) => shown.toLowerCase().includes(k.toLowerCase())))) continue;  // entries already there
      for (let k = 0; k < Math.min(mine.length, 6); k++) {
        const btn = universalAddButton(heading);
        if (!btn) break;
        assertAddButton(btn, heading);
        const before = document.querySelectorAll("input, textarea, select").length;
        btn.click();  // this section's own Add button, checked right above
        let dlg = null;
        for (let t = 0; t < 16 && !dlg; t++) { await sleep(250); dlg = entryEditor(); }
        if (!dlg) return added + (document.querySelectorAll("input, textarea, select").length > before ? 1 : 0);  // boxes in the page: the usual way
        const what = (section === "education" ? "school " : "job ") + (k + 1) + " of " + mine.length;
        setStatus(`Adding ${what}: ${mine[k].label}...`);
        const one = { seen: new Set(), filled: 0, drafted: 0, needs: state.needs, done: [] };
        modalEntry = { root: dlg, section, index: k };
        try { await runPass(one, 1, dlg); } finally { modalEntry = null; }
        state.filled += one.filled;
        const save = editorSaveButton(dlg);
        if (!save) {
          state.needs.push({ label: mine[k].label, reason: "the site's editor has no Save I can press: check it and save it", entry: null });
          renderNeeds(state.needs);
          return -1;
        }
        assertEditorSave(save, dlg, section);
        save.click();  // the entry editor's own Save, checked right above
        let closed = false;
        for (let t = 0; t < 24 && !closed; t++) { await sleep(250); closed = !dlg.isConnected || !isVisible(dlg); }
        if (!closed) {
          state.needs.push({ label: mine[k].label, reason: "the site's editor did not save it: fix what it shows, then Save", entry: null });
          renderNeeds(state.needs);
          return -1;
        }
        added++;
        await sleep(400);
      }
    }
    return added;
  }

  // Google fills work entries from the resume, projects included. An entry the server says is a project on the resume
  // (not a job) is removed with that entry's own "Remove this job" button (Google Careers only, checked right before).
  const topEntry = (el) => { let li = el.closest("li"); while (li && li.parentElement && li.parentElement.closest("li")) li = li.parentElement.closest("li"); return li; };
  function assertRemoveButton(btn, entry) {
    if (!isGoogleCareers() || !btn || btn.tagName !== "BUTTON" || btn.closest("#tailor-panel-host")) throw new Error("not a Remove button");
    if (!/^remove this job$/i.test(clean(btn.textContent))) throw new Error("refusing to press '" + clean(btn.textContent) + "'");
    if (!entry || !entry.contains(btn) || !entry.querySelector("input[aria-label='Employer name']")) throw new Error("not this work entry's button");
  }
  async function removeEntries(fieldIds) {
    let removed = 0;
    for (const id of fieldIds.slice(0, 6)) {
      const reg = registry.get(id);
      const entry = reg && reg.els[0] && reg.els[0].isConnected ? topEntry(reg.els[0]) : null;
      if (!entry) continue;
      const btn = [...entry.querySelectorAll("button")].find((b) => /^remove this job$/i.test(clean(b.textContent)));
      if (!btn) continue;
      assertRemoveButton(btn, entry);
      btn.click();  // this project entry's own "Remove this job", checked right above
      removed++;
      for (let k = 0; k < 10 && entry.isConnected; k++) await sleep(300);
    }
    return removed;
  }

  let reviewAnnounced = false;
  // a form with steps shows them (Workday's progress bar, a list of steps, "Step 2 of 4"); a one-page form does not
  const hasSteps = () => isWorkday() || !!document.querySelector("[aria-current='step'], li[class*='step'], [class*='stepper'], [class*='progress'] li") ||
    /\bstep \d+ of \d+\b/i.test(pageText().slice(0, 3000));
  async function announceReview() {
    if (reviewAnnounced) return;
    reviewAnnounced = true;
    ensurePanel();
    setStatus(hasSteps() ? "Ready for your review: this is the last step. Check everything, then click Submit yourself. Tailor Assist never submits."
      : "Ready for your review: everything this page asks is filled. Check it, then click Submit yourself. Tailor Assist never submits.");
    // what came up on the earlier steps, listed here so nothing is missed before Submit
    const s = await chrome.storage.local.get("reviewList").catch(() => ({}));
    const list = s.reviewList && s.reviewList.host === location.hostname ? s.reviewList.items : [];
    if (list.length) {
      const box = panel.root.getElementById("reviewlist");
      if (box) {
        box.textContent = "";
        const h = document.createElement("div");
        h.textContent = "Check before you submit:";
        h.style.fontWeight = "600";
        box.append(h);
        for (const it of list.slice(0, 20)) {
          const d = document.createElement("div");
          d.textContent = "• " + it.what + (it.step ? " (on: " + it.step + ")" : "");
          box.append(d);
        }
        box.style.display = "block";
      }
    }
    await chrome.storage.local.remove("reviewList").catch(() => {});
    // the apply queue: when the person has submitted, this opens the next application (a confirmation page does the
    // same by itself; this button is for sites whose confirmation Tailor cannot see, like Airbnb's embedded form)
    const row = panel.root.getElementById("queuerow");
    if (row) row.style.display = "flex";
    // a cover letter written on an earlier step of this application: read it before submitting
    chrome.storage.local.get("lastCover").then((s) => {
      const c = s && s.lastCover;
      if (c && c.host === location.hostname && Date.now() - c.at < 3 * 3600 * 1000 && !panel.root.getElementById("coverdl").textContent)
        setStatus(panel.status.textContent + " A cover letter was written and attached on an earlier step: read it there or on the Desk.");
    }).catch(() => {});
    await chrome.storage.local.remove("autoFlow").catch(() => {});  // the flow ends here
    await alertOnce("needs_you", `The application on ${location.hostname} is at Review: check it and submit it yourself.`).catch(() => {});
  }

  // A question the page requires that Tailor could not answer: the step cannot be left without it (2026-10-13: the
  // person wants the flow to keep going and stop only at Submit, so optional ones and drafts go on the review list).
  function needIsRequired(n) {
    const el = n.entry && n.entry.els && n.entry.els[0];
    if (!el) return false;
    if (el.required || el.getAttribute("aria-required") === "true") return true;
    const group = el.closest("fieldset, [role='group'], [role='radiogroup']");
    if (group && (group.getAttribute("aria-required") === "true" || group.querySelector("[required], [aria-required='true']"))) return true;
    const lab = (el.labels && el.labels[0]) || (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || (group && group.querySelector("legend, label"));
    return !!(lab && /\*\s*$|\*\s*\(|\(required\)|\brequired\b/i.test(lab.textContent || ""));
  }
  // what the person should look at before submitting, gathered across the steps of one application
  async function addToReview(items) {
    if (!items || !items.length) return;
    const s = await chrome.storage.local.get("reviewList").catch(() => ({}));
    const cur = s.reviewList && s.reviewList.host === location.hostname && Date.now() - (s.reviewList.at || 0) < 3 * 3600 * 1000
      ? s.reviewList : { host: location.hostname, items: [] };
    const step = activeStepName() || document.title || "a step";
    for (const it of items) if (!cur.items.some((x) => x.what === it)) cur.items.push({ step: step.slice(0, 60), what: it.slice(0, 160) });
    cur.at = Date.now();
    await chrome.storage.local.set({ reviewList: cur }).catch(() => {});
  }

  async function maybeGoNext(result) {
    const p = panel;
    const box = p && p.root.getElementById("autonext");
    if (!box || !box.checked || !result || !result.ok || result.empty) return;
    const atEnd = onReviewStep();
    if (result.requiredNeeds && !atEnd) {  // a required question only the person can answer: the step waits for it
      setStatus(p.status.textContent + " Answer the required question(s) under Needs you, then click the page's Next: the next step is filled for you.");
      return;
    }
    await addToReview(result.review);  // optional questions and drafts: checked at the end, before Submit
    if (atEnd) { await announceReview(); return; }
    if (pageErrors()) { setStatus("The page shows an error on this step: fix it, then click Next yourself."); return; }
    const flow = (await chrome.storage.local.get("autoFlow").catch(() => ({}))).autoFlow || {};
    const steps = flow.host === location.hostname && Date.now() - (flow.at || 0) < FLOW_MS ? (flow.steps || 0) : 0;
    if (autoNextCount >= 8 || steps >= 10) return;
    const btn = nextButton();
    if (!btn) return;
    cancelNext = false;
    const stopRow = p.root.getElementById("stoprow");
    stopRow.style.display = "flex";
    const done = p.status.textContent;
    for (let s = 3; s > 0; s--) {
      setStatus(done + ` Going to the next step in ${s} s...`);
      await sleep(1000);
      if (cancelNext) { stopRow.style.display = "none"; setStatus(done + " Stayed on this step."); return; }
    }
    stopRow.style.display = "none";
    try { assertNextButton(btn); } catch (e) { setStatus(done + " " + e.message); return; }
    const before = location.href + "|" + activeStepName() + "|" + [...formKeys()].sort().join(",");
    autoNextCount++;
    // a step that loads as a new page (not a single-page app) carries on there: same site, for a few minutes
    await chrome.storage.local.set({ autoFlow: { host: location.hostname, at: Date.now(), steps: steps + 1, nextAt: Date.now() } }).catch(() => {});
    btn.click();  // the step's own Next button, checked right above: never Submit, never on Review
    for (let i = 0; i < 24; i++) {  // the new step is filled by maybeNewStep once its fields appear
      await sleep(500);
      if (location.href + "|" + activeStepName() + "|" + [...formKeys()].sort().join(",") !== before) {
        await sleep(900);
        if (onReviewStep()) await announceReview();  // a Review page has nothing to fill: say so here
        return;
      }
    }
    setStatus("The page did not move on: check its messages, fix them, then click Next yourself.");
  }

  let lastResult = null;
  let lastAdd = null;
  let lastRemove = [];
  let tailoring = null;
  async function run() {
    if (running) return { ok: false, error: "already running" };
    running = true;
    const p = ensurePanel();
    p.fill.disabled = p.rescan.disabled = true;
    const state = { seen: new Set(), filled: 0, drafted: 0, needs: [], done: [] };
    try {
      // the person's order (2026-10-07): the plain facts first, then the tailored resume, then the cover letter, then
      // written answers. Tailoring runs meanwhile; only the resume upload and the letter wait for it.
      tailoring = tailorResume(p).catch(() => null);
      await prepareEntries();
      await modalEntries(state);
      let any = false;
      // Answering one question can reveal follow-ups (a race question after the Hispanic/Latino one), so the form is
      // read once more and only the fields that were not there before are handled. Never more than two passes.
      for (let pass = 0; pass < 2; pass++) {
        const r = await runPass(state, pass);
        if (!r.ok) return r;
        if (r.empty) break;
        any = true;
        if (pass === 0) {
          // Follow-up questions appear after an answer (Greenhouse shows "race" after "Hispanic/Latino: No"), sometimes
          // well after it: wait until a new field shows up, up to 3 s, instead of a fixed pause.
          const before = formKeys();
          for (let t = 0; t < 12; t++) {
            await sleep(250);
            if ([...formKeys()].some((k) => !before.has(k))) { await sleep(300); break; }
          }
          // An answer can also reset an earlier one (Greenhouse clears "race" when "Hispanic/Latino" changes): anything
          // filled in the first pass that is empty again is filled again in the second.
          for (const d of state.done) {
            // (a box already flagged "pick from the site's list" is not retried: it would only fail the same way)
            if (d.entry.els[0].dataset.tailor !== "needs" && isEmptyNow(d.entry.els[0])) { state.seen.delete(d.key); state.filled--; }
          }
          state.done = [];
        }
      }
      if (!any) return { ok: true, empty: true };
      const parts = [`${state.filled} filled`];
      if (state.drafted) parts.push(`${state.drafted} drafted (review)`);
      if (state.needs.length) parts.push(`${state.needs.length} need you`);
      setStatus(parts.join(" · ") + ".");
      lastResult = { ok: true, filled: state.filled, drafted: state.drafted, needs: state.needs.length,
                     requiredNeeds: state.needs.filter(needIsRequired).length,
                     review: [...state.needs.map((n) => (n.label || "a question") + (n.reason ? " (" + n.reason + ")" : "")),
                              ...(state.draftLabels || []).map((l) => "Drafted answer: " + l)] };
      // this site is mid-application: its next page is filled on arrival, even when the person clicks Next themselves
      chrome.storage.local.get("autoFlow").then((s) => {
        const f = s.autoFlow && s.autoFlow.host === location.hostname ? s.autoFlow : { host: location.hostname, steps: 0 };
        return chrome.storage.local.set({ autoFlow: { ...f, at: Date.now() } });
      }).catch(() => {});
      lastAdd = state.add || null;
      lastRemove = state.remove || [];
      // Entries still to add (Ashby, 2026-10-06): the alert comes after they are filled, not now. The alert (a sound
      // in the background page) is never waited on for more than 3 s: it once hung and the entries were never added.
      const fixing = addRounds < 2 && ((lastAdd && (lastAdd.experience || lastAdd.education)) || lastRemove.length);
      if (!fixing) {
        const kind = state.needs.length || state.drafted ? "needs_you" : "done";
        await Promise.race([alertOnce(kind, `${parts.join(", ")} on ${location.hostname}. Review, then submit yourself.`), sleep(3000)]);
      }
      return lastResult;
    } catch (e) {
      // the extension updated itself while this page was being filled: its old script can no longer reach it
      if (/Extension context invalidated/i.test(String(e && e.message))) {
        setStatus("Tailor Assist was updated while filling this page: reload the page and it fills again.");
        return { ok: false, error: "updated" };
      }
      throw e;
    } finally {
      running = false;
      const add = lastAdd; lastAdd = null;
      const remove = isGoogleCareers() ? lastRemove : []; lastRemove = [];
      console.debug("[tailor] entries", JSON.stringify(add), "rounds", addRounds);
      if (addRounds < 2 && (remove.length || (add && (add.experience || add.education)))) {
        addRounds++;
        const held = lastResult;  // the fill's own result: used if there turns out to be nothing to fix
        lastResult = null;
        const before = p.status ? p.status.textContent : "";
        setTimeout(async () => {
          try {
            setStatus("Fixing the work and education entries to match your resume...");
            const changed = (remove.length ? await removeEntries(remove) : 0) + (add ? await addEntries(add) : 0);
            if (changed) { await sleep(800); run(); }
            else {
              // nothing could be added or removed (a form with no entry sections, JazzHR 2026-10-13): never stay on
              // "Fixing...", say where the fill stands and carry on as if no fix was needed
              setStatus(before || "Filled. Review the page, then submit yourself.");
              if (held) setTimeout(() => maybeGoNext(held), 400);
            }
          } catch (e) { console.debug("[tailor] fix entries failed", e.message); setStatus("Could not fix entries: " + e.message); }
        }, 300);
      } else if (lastResult) { const r = lastResult; lastResult = null; setTimeout(() => maybeGoNext(r), 400); }
      p.fill.disabled = p.rescan.disabled = false;
      filledOnce = true;
      markFilled();
      stepKeys = formKeys();
    }
  }

  // ---------- multi-step forms (Google Careers, Workday) ----------
  // After the person has used Fill on this page, a new step appearing (they clicked Next) is filled the same way. This
  // only watches: moving between steps, and submitting, stay with the person.
  let filledOnce = false;
  let stepKeys = new Set();
  let autoRuns = 0;

  function isEmptyNow(el) {
    if (!el || !el.isConnected) return true;
    if (isListboxButton(el)) return /^(?:select(?: one)?|choose|--)?$/i.test(clean(el.textContent));
    if (isCombobox(el)) {
      const box = outlineBox(el);
      return box ? !!box.querySelector("[class*='placeholder']") : !el.value;
    }
    return el.tagName === "SELECT" ? !el.value : !(el.value || "").trim();
  }

  function formKeys() {
    const keys = new Set();
    for (const el of deepAll("input, select, textarea")) {
      const type = (el.type || el.tagName).toLowerCase();
      if (SKIP_TYPES.has(type) || !isVisible(el)) continue;
      keys.add((el.name || el.id || labelOf(el)) + "|" + type);
    }
    return keys;
  }

  function maybeNewStep() {
    if (!filledOnce || running || autoRuns >= 12 || !panel || !panel.root.getElementById("autostep").checked) return;
    // A new step replaces the form (Next): most of the old fields are gone. New fields on the same page (a follow-up
    // question, a site re-render) are not a step, and re-filling would overwrite what the person already edited.
    const now = formKeys();
    let fresh = 0, gone = 0;
    for (const k of now) if (!stepKeys.has(k)) fresh++;
    for (const k of stepKeys) if (!now.has(k)) gone++;
    if (fresh < 2 || gone < Math.max(2, stepKeys.size / 2)) return;
    autoRuns++;
    setStatus("New step found: filling it...");
    run();
  }

  // Structure first, as a person (or Simplify) would: one entry per resume job and degree is added before anything is
  // filled, so the new entries are filled in this same run. Before (Solace, 2026-10-06) they were added only after the
  // written answers were drafted, minutes later, and it looked as if nothing happened.
  async function prepareEntries() {
    if (addRounds >= 2) return;
    const fields = collectFields();
    if (!fields.some((f) => f.section)) return;
    const resp = await send({ type: "plan", payload: { url: location.href, title: document.title, page_text: pageText(), fields,
                                                       resume_id: resumeId || "", features: ["transcript_which"] } });
    const add = resp && resp.ok ? resp.data.add_entries : null;
    if (!add || !(add.experience || add.education)) return;
    addRounds++;
    setStatus("Adding entries for your jobs and degrees...");
    if (await addEntries(add)) await sleep(800);
  }

  async function runPass(state, pass, within = null) {
    {
      if (pass === 0) setStatus("Reading the form...");
      // within: only the boxes of one entry editor (a dialog), when an entry is added that way
      const fields = collectFields().filter((f) => !state.seen.has(fieldKey(f)) &&
        (!within || within.contains(registry.get(f.id).els[0])));
      if (!fields.length) { if (pass === 0) setStatus("No fillable fields found on this page."); return { ok: true, empty: true }; }
      fields.forEach((f) => state.seen.add(fieldKey(f)));
      // resume_id: the resume tailored to this posting, so the form's answers follow it (bullets, degree order, offices)
      const resp = await send({ type: "plan", payload: { url: location.href, title: document.title, page_text: pageText(), fields,
                                                         resume_id: resumeId || "", features: ["transcript_which"] } });
      if (!resp.ok) { setStatus(resp.error); await alertOnce("needs_you", resp.error); return { ok: false, error: resp.error }; }
      if (pass === 0) state.add = resp.data.add_entries || {};
      state.remove = state.remove || [];
      for (const a of resp.data.actions) if (a.kind === "needs_you" && /is a project on your resume/.test(a.reason || "")) state.remove.push(a.field_id);

      let filled = 0;
      const needs = state.needs;
      const drafts = [];
      const covers = [];
      const uploads = [];
      const typed = [];
      const flaggedGroups = new Set();
      // the letter takes a minute or two: it is asked for now and attached after the rest of the page is filled
      for (const a of resp.data.actions) {
        const entry = registry.get(a.field_id);
        const info = fieldInfo.get(a.field_id);
        if (!entry) continue;
        try {
          if (a.kind === "fill") { fillText(entry.els[0], a.value); mark(entry, "tailor-filled", "Filled from your profile"); filled++; typed.push({ entry, info, value: a.value, key: a.reason }); state.done.push({ key: fieldKey(info), entry }); }
          else if (a.kind === "select") { (entry.els[0].tagName === "BUTTON" || entry.els[0].getAttribute("role") === "button" ? chooseButton : chooseOption)(entry, a.value); mark(entry, "tailor-filled", "Filled from your profile"); filled++; state.done.push({ key: fieldKey(info), entry }); }
          else if (a.kind === "combobox") { await (isListboxButton(entry.els[0]) ? chooseListbox(entry, a.value, a.reason) : chooseCombobox(entry, a.value, a.reason)); mark(entry, "tailor-filled", a.source === "answer_sheet" ? "Filled from your saved answer" : "Filled from your profile"); filled++; state.done.push({ key: fieldKey(info), entry }); }
          else if (a.kind === "check") { setCheckbox(entry.els[0], a.value === "true"); mark(entry, "tailor-filled", "Filled from your profile"); filled++; }
          else if (a.kind === "upload") { uploads.push({ entry, info }); mark(entry, "tailor-drafting", "Your tailored resume will go here"); }
          else if (a.kind === "transcript") { await uploadTranscript(entry.els[0], a.value); mark(entry, "tailor-filled", a.value === "undergraduate" ? "Your undergraduate transcript attached" : "Your transcript attached"); filled++; }
          else if (a.kind === "draft") { drafts.push({ a, entry, info }); mark(entry, "tailor-drafting", "A draft will appear here"); }
          else if (a.kind === "cover") { covers.push({ a, entry, info }); mark(entry, "tailor-drafting", "Your cover letter will go here"); }
          else if (a.kind === "needs_you" && info.type === "radio" && entry.els.some((r) => r.checked || isPressed(r)) && !/^sensitive/.test(a.reason || "")) {
            // already answered on the page (the person, or a saved draft): not something to chase. A sensitive question
            // stays flagged even when pre-selected, because a site's default is not the person's answer.
          }
          else if (a.kind === "needs_you") {
            mark(entry, "tailor-needs", a.reason);
            // a check-all-that-apply question (Lever's 40 language boxes) is one thing to answer, not forty
            const group = info.type === "checkbox" && info.name ? "cb|" + info.name : "";
            if (group && flaggedGroups.has(group)) continue;
            if (group && document.querySelectorAll(`input[type=checkbox][name="${CSS.escape(info.name)}"]`).length > 1) {
              flaggedGroups.add(group);
              needs.push({ label: groupLabel(entry.els[0]), reason: "check all that apply", entry });
            } else needs.push({ label: info.label || info.name || "field", reason: a.reason, entry });
          }
        } catch (e) {
          mark(entry, "tailor-needs", "Could not fill: " + e.message);
          needs.push({ label: info.label || info.name || "field", reason: "could not fill: " + e.message, entry });
        }
      }

      // Some boxes only take a pick from their own suggestion list (Lever's "Current location") and throw typed text
      // away. Look again: an empty box was not filled, so it goes back to the person.
      if (typed.length) {
        await sleep(600);
        for (const { entry, info, value, key } of typed) {
          if (!(entry.els[0].value || "").trim()) {
            try {
              await pickSuggestion(entry.els[0], value, key);
              continue;  // picked from the box's own list: it stays filled
            } catch (e) {
              console.debug("[tailor] suggestion pick failed", e.message);
              setNative(entry.els[0], ""); fire(entry.els[0], ["input"]);  // leave the box as the site had it
            }
            filled--;
            mark(entry, "tailor-needs", "The site did not keep typed text: pick from its list");
            needs.push({ label: info.label || info.name || "field", reason: "pick from the site's list", entry });
          }
        }
      }

      for (const note of within ? [] : resp.data.notes || []) {  // e.g. a work entry to add; once, whatever the pass
        // (not for one entry's editor: the other entries are added next, by Tailor itself)
        if (!needs.some((n) => n.label === note)) needs.push({ label: note, reason: "", entry: null });
      }
      let drafted = 0;
      renderNeeds(needs);
      if (uploads.length || covers.length) {
        setStatus(`${state.filled + filled} filled. Tailoring your resume to this posting...`);
        await tailoring;  // resumeId: the tailored version (or the default one when tailoring could not run)
      }
      for (const u of uploads) {
        try { await uploadResume(u.entry.els[0]); mark(u.entry, "tailor-filled", "Tailored resume attached"); filled++; }
        catch (e) {
          mark(u.entry, "tailor-needs", "Could not attach the resume: " + e.message);
          needs.push({ label: u.info.label || "Resume", reason: "could not attach: " + e.message, entry: u.entry });
        }
      }
      if (covers.length) {
        setStatus(`${state.filled + filled} filled. Writing your cover letter from your tailored resume (about two minutes)...`);
        const r = await coverLetter();
        for (const c of covers) {
          if (!r || !r.ok) {
            const why = "Cover letter: " + ((r && r.error) || "the server did not answer");
            mark(c.entry, "tailor-needs", why);
            if (c.info.required) needs.push({ label: c.info.label || "Cover letter", reason: why, entry: c.entry });
            continue;
          }
          try {
            if (c.entry.els[0].type === "file") {
              attachCover(c.entry.els[0], r);
              mark(c.entry, "tailor-filled", "Cover letter written for this posting: read it (link in the panel)");
              filled++;
            } else {  // a box for the letter's text: a draft like any written answer, read before going on
              fillText(c.entry.els[0], r.data.text);
              mark(c.entry, "tailor-draft", "Cover letter draft: read it, edit it, make it yours");
              (state.draftLabels = state.draftLabels || []).push(c.info.label || "Cover letter");
              drafted++;
            }
          } catch (e) {
            mark(c.entry, "tailor-needs", "Could not attach the cover letter: " + e.message);
            needs.push({ label: c.info.label || "Cover letter", reason: "could not attach: " + e.message, entry: c.entry });
          }
        }
        if (r && r.ok) showCover(r);
        renderNeeds(needs);
      }
      for (const [i, d] of drafts.entries()) {
        setStatus(`${state.filled + filled} filled. Drafting answer ${i + 1} of ${drafts.length}...`);
        const r = await send({ type: "answer", payload: { question: d.info.label || d.info.context, page_text: pageText(),
                                                          max_chars: d.info.maxlength, hint: d.info.context || "" } });
        if (r.ok) {
          fillText(d.entry.els[0], r.data.text);
          watchAnswer(d.entry.els[0], d.info.label || d.info.context);
          mark(d.entry, "tailor-draft", "Draft: read it, edit it, make it yours" + (r.data.warnings.length ? " (" + r.data.warnings.join("; ") + ")" : ""));
          (state.draftLabels = state.draftLabels || []).push(d.info.label || d.info.context || "a written answer");
          drafted++;
        } else {
          mark(d.entry, "tailor-needs", r.error);
          needs.push({ label: d.info.label || "question", reason: r.error, entry: d.entry });
        }
        renderNeeds(needs);
      }

      state.filled += filled;
      state.drafted += drafted;
      return { ok: true };
    }
  }

  async function alertOnce(kind, text) {
    await send({ type: "alert", kind, text });
    if (kind === "needs_you") {
      const ack = () => { send({ type: "ack" }); };
      document.addEventListener("pointerdown", ack, { once: true, capture: true });
      document.addEventListener("keydown", ack, { once: true, capture: true });
    }
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.type === "posting") {  // for "Save posting to Desk": only the top page answers
      if (window !== window.top) return false;
      const isPosting = looksLikePosting(pageText());
      sendResponse({ url: location.href, title: document.title, isPosting, text: isPosting ? postingOnlyText() : pageText() });
      return false;
    }
    if (msg && msg.type === "fill") {
      const forms = document.querySelectorAll("input:not([type=hidden]), select, textarea").length;
      if (!forms && window !== window.top) return false;  // an empty iframe: let the frame that has the form answer
      run().then(sendResponse);
      return true;
    }
    return false;
  });

  // Show the panel once the page looks like an application form (3+ fillable fields), including forms added later.
  let timer = null;
  // Fill on open (like Simplify; the person's choice, 2026-10-06): once per page, only on what is clearly an application
  // form (a name or email box and a few more fields), never with a Desk link already starting it, and never submits.
  let autoStarted = false;
  async function maybeAutoFill() {
    if (autoStarted || filledOnce || running || !panel || /tailor-fill=/.test(location.hash)) return;
    const fields = collectFields();
    let looksLikeApplication = fields.length >= 6 && fields.some((f) => /first name|last name|e-?mail|r[eé]sum[eé]|legal name/i.test(f.label + " " + f.name));
    if (!looksLikeApplication && fields.length >= 2) {
      // the next step of an application that loaded as a new page ("Application Questions", "Voluntary Disclosures"),
      // never the site's job search ("Search jobs", "Location") the person opens after applying
      const flow = (await chrome.storage.local.get("autoFlow").catch(() => ({}))).autoFlow || {};
      const searchForm = !!document.querySelector("[role='search'], input[type='search']") && fields.length <= 4;
      const applyPage = /apply|application|candidate|step|questionnaire|disclosure|self-?identif/i.test(location.pathname + location.search) ||
        hasSteps() || !!document.querySelector("input[type=file]");
      looksLikeApplication = flow.host === location.hostname && Date.now() - (flow.at || 0) < FLOW_MS && applyPage && !searchForm;
    }
    if (!looksLikeApplication || autoStarted) return;
    autoStarted = true;
    const s = await chrome.storage.local.get(["autoFill", "filledPages"]).catch(() => ({}));
    if (s.autoFill === false) return;
    // a page Tailor already filled (Chrome reloads tabs it put to sleep: DoorDash 2026-10-09 re-filled and re-tailored
    // after the person came back to it): not again on its own; the Fill button still works
    const at = (s.filledPages || {})[pageKey()];
    if (at && Date.now() - at < 12 * 3600 * 1000) {
      setStatus("Filled earlier. Your entries are kept; click Fill this page to fill it again.");
      return;
    }
    setStatus("Filling this application from your profile...");
    markFilled();  // at the start too: a tab closed or put to sleep mid-fill must not be filled again on its own
    run();
  }

  // a page and the form on it: sites that load every step at the same address (tal.net's save_page) differ by fields
  function pageKey() {
    const labels = deepAll("input, select, textarea").filter((el) => !SKIP_TYPES.has((el.type || "").toLowerCase()) &&
      !el.closest("#tailor-panel-host") && (el.type === "file" || isVisible(el))).map((el) => labelOf(el).toLowerCase()).sort().join("|");
    let h = 0;
    for (let i = 0; i < labels.length; i++) h = (h * 31 + labels.charCodeAt(i)) | 0;
    return location.origin + location.pathname + "#" + (h >>> 0).toString(36);
  }
  function markFilled() {
    chrome.storage.local.get("filledPages").then((s) => {
      const pages = s.filledPages || {};
      pages[pageKey()] = Date.now();
      const keep = Object.entries(pages).filter(([, t]) => Date.now() - t < 2 * 86400 * 1000).slice(-300);
      return chrome.storage.local.set({ filledPages: Object.fromEntries(keep) });
    }).catch(() => {});
  }

  async function maybeReviewArrived() {  // a step loaded as a new page that is the Review page (nothing to fill)
    if (reviewAnnounced || filledOnce || running || window !== window.top) return;
    const flow = (await chrome.storage.local.get("autoFlow").catch(() => ({}))).autoFlow || {};
    // only right after Tailor Assist pressed Next on this site: a page it filled itself is reported by its own run
    if (flow.host === location.hostname && Date.now() - (flow.nextAt || 0) < 60000 && !filledOnce && onReviewStep()) await announceReview();
  }

  function maybeShowPanel() {
    maybeReviewArrived();
    if (panel) {  // some sites re-render the whole document and drop nodes they did not create
      if (!panel.host.isConnected) document.documentElement.appendChild(panel.host);
      maybeAutoFill();
      return;
    }
    const n = deepAll("input, select, textarea").filter((e) => !SKIP_TYPES.has((e.type || "").toLowerCase())).length;
    if (n >= 3) { ensurePanel(); maybeAutoFill(); }
  }
  maybeShowPanel();
  setTimeout(rememberPosting, 1500);
  setTimeout(reportIfApplied, 1500);

  // One-click Apply from the Tailor Desk: the link carries a one-time code. Fill starts only if the local server
  // confirms that code (issued by the Desk a moment ago, usable once), then waits for the form to appear.
  const autoCode = (location.hash.match(/tailor-fill=([\w-]{8,40})/) || [])[1];
  if (autoCode && window === window.top) {
    history.replaceState(null, "", location.pathname + location.search);  // the code is used once, keep it out of history
    send({ type: "autofill_ok", code: autoCode }).then(async (r) => {
      if (!r || !r.ok || !r.data || !r.data.ok) return;
      deskPosting = r.data.posting || null;
      for (let i = 0; i < 40; i++) {  // up to 20 s for the form to render
        if (collectFields().length >= 3) { ensurePanel(); await run(); return; }
        await sleep(500);
      }
      setStatus("Opened from the Tailor Desk, but no application form showed up. Click Apply on the page, then Fill this page.");
    }).catch(() => {});
  }
  // Single-page apps can finish rendering without another DOM change we notice, so also re-check for a few seconds.
  let polls = 0;
  // Postings on such sites also arrive late, so keep looking for one while polling.
  const poll = setInterval(() => { if (++polls > 120) return clearInterval(poll); maybeShowPanel(); rememberPosting(); reportIfApplied(); }, 1000);
  // Single-page application sites (Cornerstone / csod, Simon-Kucher 2026-10-10) show their "thank you" long after the
  // first two minutes, without a page load: look for it whenever the page changes, and every 5 s for a few hours.
  let slowPolls = 0;
  const slowPoll = setInterval(() => { if (++slowPolls > 2160) return clearInterval(slowPoll); if (document.visibilityState !== "hidden") reportIfApplied(); }, 5000);
  new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(() => { maybeShowPanel(); maybeNewStep(); reportIfApplied(); }, 900); })
    .observe(document.documentElement, { childList: true, subtree: true });
})();

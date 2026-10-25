// Service worker: the only part that talks to the local server, and the only part that plays alerts.
// Web pages never see the token: content scripts ask this worker, which adds it.
"use strict";

const DEFAULTS = { backendUrl: "http://127.0.0.1:8765", token: "" };
const REPEAT_ALARM = "tailor-repeat";

class ApiError extends Error {
  constructor(status, detail) { super(detail || ("HTTP " + status)); this.status = status; }
}

async function config() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(["backendUrl", "token"])) };
}

async function api(path, options = {}) {
  const { backendUrl, token } = await config();
  if (!token) throw new ApiError(0, "No access token yet. Open the extension's Options page and paste it in.");
  let res;
  try {
    res = await fetch(backendUrl + path, {
      ...options,
      headers: { "Content-Type": "application/json", "X-Tailor-Token": token, ...(options.headers || {}) },
    });
  } catch (e) {
    throw new ApiError(0, "Cannot reach the local server. Is scripts/run_server.py running?");
  }
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail; } catch (_) { /* not JSON */ }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res;
}

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

// ---- alerts: notification + sound, repeating until the person acknowledges ----

async function ensureOffscreen() {
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (contexts.length) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html", reasons: ["AUDIO_PLAYBACK"], justification: "Play the alert sound when the form needs you",
  });
}

async function playSound(kind) {
  const stored = await chrome.storage.local.get(["sound_" + kind, "volume"]);
  await ensureOffscreen();
  await chrome.runtime.sendMessage({
    target: "offscreen", type: "play", kind, dataUrl: stored["sound_" + kind] || null,
    volume: typeof stored.volume === "number" ? stored.volume : 1,
  });
}

async function alertUser(kind, text) {
  await chrome.storage.session.set({ lastAlert: { kind, text, at: Date.now() } });
  const title = kind === "needs_you" ? "Tailor Assist needs you" : "Tailor Assist is done";
  chrome.notifications.create("tailor-" + kind, {
    type: "basic", iconUrl: "icons/icon128.png", title, message: text || "", requireInteraction: kind === "needs_you",
  });
  try { await playSound(kind); } catch (e) { console.warn("sound failed", e); }
  if (kind === "needs_you") {
    await chrome.storage.session.set({ pendingAlert: { kind, text } });
    chrome.alarms.create(REPEAT_ALARM, { delayInMinutes: 1, periodInMinutes: 1 });
  }
}

async function acknowledge() {
  await chrome.alarms.clear(REPEAT_ALARM);
  await chrome.storage.session.remove("pendingAlert");
  chrome.notifications.clear("tailor-needs_you");
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== REPEAT_ALARM) return;
  const { pendingAlert } = await chrome.storage.session.get("pendingAlert");
  if (!pendingAlert) { await chrome.alarms.clear(REPEAT_ALARM); return; }
  chrome.notifications.create("tailor-needs_you", {
    type: "basic", iconUrl: "icons/icon128.png", title: "Tailor Assist still needs you",
    message: pendingAlert.text || "", requireInteraction: true,
  });
  try { await playSound("needs_you"); } catch (e) { console.warn("sound failed", e); }
});

// ---- new roles from the Tailor Desk's daily search: a notification and a count on the icon ----
const SEARCH_ALARM = "tailor-search-check";
chrome.alarms.get(SEARCH_ALARM).then((a) => a || chrome.alarms.create(SEARCH_ALARM, { delayInMinutes: 1, periodInMinutes: 30 }));

async function checkNewRoles() {
  let res;
  try { res = await (await api("/api/search/results?show=open")).json(); } catch (_) { return; }  // no server, no token
  const open = res.counts ? res.counts.open : 0;
  await chrome.action.setBadgeText({ text: open ? String(Math.min(open, 99)) : "" });
  await chrome.action.setBadgeBackgroundColor({ color: "#4f46e5" });
  const run = res.last;
  if (!run || !run.new) return;
  const { notifiedRun } = await chrome.storage.local.get("notifiedRun");
  if (notifiedRun === run.at) return;  // one notification per search
  await chrome.storage.local.set({ notifiedRun: run.at });
  const prepared = (run.prepared || []).length;
  chrome.notifications.create("tailor-roles", {
    type: "basic", iconUrl: "icons/icon128.png", title: `${run.new} new role${run.new > 1 ? "s" : ""} match your search`,
    message: (prepared ? `${prepared} already have a tailored resume on the Desk. ` : "") +
      "Open the Tailor Desk to review them. Nothing has been applied to.",
  });
}

chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === SEARCH_ALARM) { checkNewRoles(); checkDeadlines(); checkFollowUps(); } });

// ---------- keep itself current ----------
// Chrome reads an unpacked extension's files only when it is reloaded on chrome://extensions, so after an update the
// old code kept running (2026-10-02: the new Resume PDF button never showed). Once a minute the extension compares its
// files on disk with the ones it was loaded from, and reloads itself when they changed. Open pages get the new panel
// when they are next loaded.
const SELF_ALARM = "tailor-self-update";
async function fingerprint() {
  let text = "";
  for (const f of ["manifest.json", "background.js", "content.js", "popup.js", "options.js", "panel.css"]) {
    try { text += await (await fetch(chrome.runtime.getURL(f), { cache: "no-store" })).text(); } catch (_) { /* missing file */ }
  }
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) | 0;
  return text.length + ":" + h;
}
async function recordLoaded(why) {
  await chrome.storage.local.set({ loadedFingerprint: await fingerprint() });
  // tell the local server which version is running (seen in its log): an old copy still loaded is easy to spot
  // the update script restarts the server at the same moment: try again for about a minute (2026-10-07: v7054 loaded
  // while the server was down, so its log never showed it)
  const body = JSON.stringify({ version: chrome.runtime.getManifest().version, why });
  for (let i = 0; i < 6; i++) {
    try { await api("/api/extension-loaded", { method: "POST", body }); return; } catch (_) { await new Promise((r) => setTimeout(r, 10000)); }
  }
}
chrome.runtime.onInstalled.addListener((d) => recordLoaded(d.reason));
// The person (2026-10-13): "get project tailor to apply automatically for me". Fill on open and going to the next
// step were found switched off: this update turns both on once; the panel's boxes still turn them off.
chrome.runtime.onInstalled.addListener(async () => {
  const s = await chrome.storage.local.get("autoDefaults20261013");
  if (!s.autoDefaults20261013) await chrome.storage.local.set({ autoFill: true, autoNext: true, autoDefaults20261013: true });
});

// The apply queue (Desk "Start applying"): after one application is submitted, the next posting opens in a new tab.
// Opening never submits anything; a second request within 20 s (the thank-you page and the button both) is ignored.
let lastQueueOpen = 0;
async function openNextInQueue() {
  if (Date.now() - lastQueueOpen < 20000) return null;
  try {
    const res = await api("/api/desk/queue/next", { method: "POST" });
    const next = await res.json();
    if (!next || next.done || !next.url) return null;
    lastQueueOpen = Date.now();
    await chrome.tabs.create({ url: next.url, active: true });
    chrome.notifications.create("tailor-next-" + next.id, {
      type: "basic", iconUrl: "icons/icon128.png", title: "Next application opened",
      message: `${next.company ? next.company + ": " : ""}${next.title} (${next.left} more after this one)`,
    });
    return next;
  } catch (_) { return null; }
}  // also after every reload: these files are the loaded ones
chrome.runtime.onStartup.addListener(() => recordLoaded("startup"));    // Chrome started: it loaded the files as they are now
// Created once. Creating it on every wake-up (each page load wakes this worker) restarted the countdown, so it never ran.
chrome.alarms.get(SELF_ALARM).then((a) => a || chrome.alarms.create(SELF_ALARM, { delayInMinutes: 1, periodInMinutes: 1 }));
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== SELF_ALARM) return;
  const { loadedFingerprint } = await chrome.storage.local.get("loadedFingerprint");
  const now = await fingerprint();
  if (!loadedFingerprint) return chrome.storage.local.set({ loadedFingerprint: now });
  if (now !== loadedFingerprint) chrome.runtime.reload();  // onInstalled then records the new files
});

// ---- deadlines: once a day, postings on the Desk due within 3 days that are not applied to yet ----
async function checkDeadlines() {
  let items;
  try { items = await (await api("/api/desk/postings")).json(); } catch (_) { return; }
  const today = new Date(new Date().toDateString());
  const due = items.filter((p) => {
    if (!p.facts || !p.facts.deadline || p.stage === "applied" || p.stage === "skipped") return false;
    const days = Math.round((new Date(p.facts.deadline + "T00:00:00") - today) / 864e5);
    return days >= 0 && days <= 3;
  });
  const stamp = today.toISOString().slice(0, 10);
  const { remindedOn } = await chrome.storage.local.get("remindedOn");
  if (!due.length || remindedOn === stamp) return;  // one reminder a day
  await chrome.storage.local.set({ remindedOn: stamp });
  const names = due.map((p) => `${p.company ? p.company + ": " : ""}${p.title} (due ${p.facts.deadline})`);
  chrome.notifications.create("tailor-deadlines", {
    type: "basic", iconUrl: "icons/icon128.png", title: `${due.length} application${due.length > 1 ? "s" : ""} due soon`,
    message: names.slice(0, 3).join("\n"), requireInteraction: true,
  });
}

// People the person messaged 5+ days ago with no reply (the Desk's Network tab): one reminder a day
async function checkFollowUps() {
  let due;
  try { due = (await (await api("/api/network/due")).json()).due || []; } catch (_) { return; }
  const stamp = new Date().toISOString().slice(0, 10);
  const { followUpOn } = await chrome.storage.local.get("followUpOn");
  if (!due.length || followUpOn === stamp) return;
  await chrome.storage.local.set({ followUpOn: stamp });
  chrome.notifications.create("tailor-followups", {
    type: "basic", iconUrl: "icons/icon128.png", title: `${due.length} follow-up${due.length > 1 ? "s" : ""} due`,
    message: due.slice(0, 3).map((c) => `${c.name || "Contact"}${c.company ? " (" + c.company + ")" : ""}`).join("\n") +
      "\nMessaged 5+ days ago, no reply yet.",
  });
}

async function openDesk(hash) {
  const { backendUrl } = await config();
  await chrome.tabs.create({ url: backendUrl + "/desk" + (hash || "") });
}

chrome.notifications.onClicked.addListener((id) => {
  if (id === "tailor-roles") { chrome.notifications.clear(id); openDesk("#find"); return; }
  if (id === "tailor-deadlines" || id.startsWith("tailor-applied-")) { chrome.notifications.clear(id); openDesk(""); return; }
  if (id === "tailor-followups") { chrome.notifications.clear(id); openDesk("#network"); return; }
  acknowledge();
});

// ---- message router ----

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.target === "offscreen") return false;  // meant for the offscreen document
  (async () => {
    try {
      switch (msg.type) {
        case "plan": {
          const res = await api("/api/fill-plan", { method: "POST", body: JSON.stringify(msg.payload) });
          return sendResponse({ ok: true, data: await res.json() });
        }
        case "cover_letter": {  // a letter for this posting: its text, and the PDF to attach
          const res = await api("/api/cover-letter", { method: "POST", body: JSON.stringify(msg.payload) });
          const data = await res.json();
          const pdf = await api("/api/cover-letter?id=" + encodeURIComponent(data.id));
          return sendResponse({ ok: true, data: Object.assign(data, { base64: toBase64(await pdf.arrayBuffer()) }) });
        }
        case "register_site": {  // the person allowed Tailor on this site from the popup: load the panel on all its pages
          const pattern = String(msg.pattern || "");
          if (!/^https:\/\/[a-z0-9.-]+\/\*$/i.test(pattern)) return sendResponse({ ok: false, error: "not a site pattern" });
          if (!(await chrome.permissions.contains({ origins: [pattern] }))) return sendResponse({ ok: false, error: "not allowed" });
          const id = "site-" + pattern.replace(/[^a-z0-9]+/gi, "-");
          const have = await chrome.scripting.getRegisteredContentScripts({ ids: [id] }).catch(() => []);
          if (!have.length) {
            await chrome.scripting.registerContentScripts([{ id, matches: [pattern], js: ["content.js"], css: ["panel.css"],
              allFrames: true, runAt: "document_idle", persistAcrossSessions: true }]);
          }
          return sendResponse({ ok: true });
        }
        case "transcript": {  // the person's transcript PDF, for an upload box that asks for one
          const res = await api("/api/transcript" + (msg.which === "undergraduate" ? "?which=undergraduate" : ""));
          const name = ((res.headers.get("content-disposition") || "").match(/filename="?([^";]+)"?/) || [])[1] || "Transcript.pdf";
          return sendResponse({ ok: true, data: { base64: toBase64(await res.arrayBuffer()), name } });
        }
        case "entries": {  // the person's schools and jobs, to add one by one through a site's entry editor
          const id = /^[0-9a-f]{12}$/.test(msg.resume_id || "") ? msg.resume_id : "";
          const res = await api("/api/entries?resume_id=" + id);
          return sendResponse({ ok: true, data: await res.json() });
        }
        case "sleep": {  // a short wait for a hidden tab (its own timers are throttled)
          await new Promise((r) => setTimeout(r, Math.max(0, Math.min(5000, Number(msg.ms) || 0))));
          return sendResponse({ ok: true });
        }
        case "choose": {  // which option of a dropdown means this answer, in the list's own wording
          const res = await api("/api/choose", { method: "POST", body: JSON.stringify(msg.payload) });
          return sendResponse({ ok: true, data: await res.json() });
        }
        case "answer": {
          const res = await api("/api/answer", { method: "POST", body: JSON.stringify(msg.payload) });
          return sendResponse({ ok: true, data: await res.json() });
        }
        case "remember_posting": {  // the posting page, so a later form page on the same site can be tailored to it
          const p = msg.payload || {};
          await chrome.storage.session.set({ ["posting:" + p.host]: { url: p.url, title: p.title, text: p.text, at: Date.now() } });
          return sendResponse({ ok: true });
        }
        case "recall_posting": {
          const key = "posting:" + msg.host;
          const hit = (await chrome.storage.session.get(key))[key];
          const fresh = hit && Date.now() - hit.at < 2 * 60 * 60 * 1000;
          return sendResponse({ ok: true, data: fresh ? hit : null });
        }
        case "desk_add": {  // a posting saved from a job page into the Tailor Desk queue
          const res = await api("/api/desk/postings", { method: "POST", body: JSON.stringify(msg.payload) });
          return sendResponse({ ok: true, data: await res.json() });
        }
        case "open_desk": {
          await openDesk(msg.hash === "#find" ? "#find" : "");
          return sendResponse({ ok: true });
        }
        case "autofill_ok": {  // is this one-time code one the Desk just issued?
          const res = await api("/api/desk/autofill/" + encodeURIComponent(msg.code), { method: "POST" });
          return sendResponse({ ok: true, data: await res.json() });
        }
        case "keep_answer": {  // the latest text of a drafted answer, per site, until the application is sent
          const key = "answers:" + msg.host;
          const cur = (await chrome.storage.session.get(key))[key] || {};
          cur[msg.question] = msg.text;
          await chrome.storage.session.set({ [key]: cur });
          return sendResponse({ ok: true });
        }
        case "import_found": {  // school job board rows the person is looking at: into Find roles (read only, never applied)
          const rows = (msg.rows || []).slice(0, 100);
          const res = await api("/api/search/import", { method: "POST", body: JSON.stringify({ rows }) });
          const data = await res.json();
          return sendResponse({ ok: true, data });
        }
        case "applied": {  // a "thank you for applying" page: mark the posting applied on the Desk
          const key = "answers:" + (msg.payload.host || "");
          const kept = (await chrome.storage.session.get(key))[key] || {};
          await chrome.storage.session.remove(key);
          msg.payload.answers = Object.entries(kept).filter(([, t]) => t && t.trim().length > 40)
            .map(([question, text]) => ({ question, text }));
          const res = await api("/api/desk/applied", { method: "POST", body: JSON.stringify(msg.payload) });
          const data = await res.json();
          if (data.matched && !data.already) {
            chrome.notifications.create("tailor-applied-" + data.matched, {
              type: "basic", iconUrl: "icons/icon128.png", title: "Marked applied on the Tailor Desk",
              message: `${data.company ? data.company + ": " : ""}${data.title}`,
            });
            await openNextInQueue();  // the apply queue goes on with the next posting
          }
          return sendResponse({ ok: true, data });
        }
        case "queue_next": {  // the person clicked "I submitted it" on the last page: mark it applied, open the next one
          const res = await api("/api/desk/applied", { method: "POST", body: JSON.stringify(msg.payload) });
          const data = await res.json().catch(() => ({}));
          const next = await openNextInQueue();
          return sendResponse({ ok: true, data: { applied: data, next } });
        }
        case "tailor": {
          const res = await api("/api/tailor", { method: "POST", body: JSON.stringify(msg.payload) });
          return sendResponse({ ok: true, data: await res.json() });
        }
        case "confirm_skill": {  // "I have this" on a keyword the posting wants
          const res = await api("/api/skills/confirm", { method: "POST", body: JSON.stringify({ name: msg.name }) });
          return sendResponse({ ok: true, data: await res.json() });
        }
        case "resume": {
          const res = await api("/api/resume" + (msg.id ? "?id=" + encodeURIComponent(msg.id) : ""));
          const match = /filename="?([^";]+)"?/.exec(res.headers.get("content-disposition") || "");
          return sendResponse({ ok: true, data: { name: match ? match[1] : "resume.pdf", base64: toBase64(await res.arrayBuffer()) } });
        }
        case "health": {
          const { backendUrl } = await config();
          const res = await fetch(backendUrl + "/api/health");
          return sendResponse({ ok: true, data: await res.json() });
        }
        case "alert":
          await alertUser(msg.kind, msg.text);
          return sendResponse({ ok: true });
        case "ack":
          await acknowledge();
          return sendResponse({ ok: true });
        default:
          return sendResponse({ ok: false, error: "unknown message type " + msg.type });
      }
    } catch (e) {
      sendResponse({ ok: false, status: e.status || 0, error: String(e.message || e) });
    }
  })();
  return true;  // keep the channel open for the async response
});

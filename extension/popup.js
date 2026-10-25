"use strict";

const $ = (id) => document.getElementById(id);

async function showStatus() {
  const { token } = await chrome.storage.local.get("token");
  const r = await chrome.runtime.sendMessage({ type: "health" }).catch(() => ({ ok: false }));
  if (!r || !r.ok) {
    $("dot").className = "dot bad";
    $("state").textContent = "Local server not running. Start scripts/run_server.py.";
  } else if (!token) {
    $("dot").className = "dot bad";
    $("state").textContent = "Server is up, but no token yet. Open Options.";
  } else {
    $("dot").className = "dot ok";
    $("state").textContent = "Ready. Model: " + r.data.model + ".";
  }
}

// A site Tailor has no built-in rule for (L.E.K.'s Oleeo, lek.tal.net, 2026-10-13): Chrome is asked once for that one site
// and the panel is registered there for good, so every later page of a multi-step form gets it too (adding the site to
// the manifest instead would raise the extension's permissions and Chrome would switch it off until re-approved).
async function rememberSite(url) {
  let host;
  try { host = new URL(url).hostname; } catch (_) { return; }
  if (!/^https:/.test(url) || !host) return;
  const pattern = `https://${host}/*`;
  const known = chrome.runtime.getManifest().content_scripts[0].matches;
  const covered = known.some((m) => new RegExp("^" + m.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*") + "$").test(`https://${host}/x`));
  if (covered) return;
  const ok = await chrome.permissions.request({ origins: [pattern] }).catch(() => false);
  if (ok) await chrome.runtime.sendMessage({ type: "register_site", pattern }).catch(() => {});
}

$("fill").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "fill" });
  } catch (_) {
    // The content script is not on this site yet (a company's own careers page): add it for this tab only, in every
    // frame (application forms are often embedded: iCIMS, a company page around a Greenhouse or Ashby form).
    await chrome.scripting.insertCSS({ target: { tabId: tab.id, allFrames: true }, files: ["panel.css"] }).catch(() => {});
    await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["content.js"] });
    chrome.tabs.sendMessage(tab.id, { type: "fill" }).catch(() => {});  // the fill runs in the page, not here
    // then the site is remembered for its later pages. Last: Chrome's Allow box can close this popup (L.E.K.,
    // 2026-10-13: asking first closed it before the panel was ever added)
    await rememberSite(tab.url || "");
  }
  window.close();
});

// Send this page's posting to the Tailor Desk queue (only the posting, not the page around it).
$("save").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  $("savenote").textContent = "Saving...";
  let got;
  try {
    got = await chrome.tabs.sendMessage(tab.id, { type: "posting" });
  } catch (_) {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
    got = await chrome.tabs.sendMessage(tab.id, { type: "posting" });
  }
  if (!got || !got.text) { $("savenote").textContent = "No posting text found on this page."; return; }
  const r = await chrome.runtime.sendMessage({ type: "desk_add", payload: { url: got.url, title: got.title, text: got.text, source: "extension" } });
  $("savenote").textContent = !r || !r.ok ? "Could not save: " + ((r && r.error) || "the server did not answer") :
    (r.data.duplicate ? "Already on the Desk: " : "Saved to the Desk: ") + r.data.title +
    (got.isPosting ? "" : " (this page did not look like a posting; check it on the Desk)");
});

$("desk").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "open_desk" });
  window.close();
});

$("find").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "open_desk", hash: "#find" });
  window.close();
});

$("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
showStatus();

// Site access: when Chrome holds the job sites back ("On click"), the panel never appears there. One click asks for them
// (Chrome shows its own Allow box); then the job site in this tab is reloaded so the panel comes up.
const JOB_SITES = chrome.runtime.getManifest().content_scripts[0].matches;
chrome.permissions.contains({ origins: JOB_SITES }).then((ok) => {
  if (ok) return;
  $("allow").style.display = "block";
  $("allownote").style.display = "block";
}).catch(() => {});
$("allow").addEventListener("click", async () => {
  const granted = await chrome.permissions.request({ origins: JOB_SITES }).catch(() => false);
  if (!granted) { $("allownote").textContent = "Not allowed. Click again and choose Allow."; return; }
  $("allow").style.display = "none";
  $("allownote").textContent = "Allowed. Reloading this page so the Tailor panel appears.";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab && tab.id) chrome.tabs.reload(tab.id);
});


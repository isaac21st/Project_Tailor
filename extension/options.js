"use strict";

const $ = (id) => document.getElementById(id);
const MAX_SECONDS = 30;
const KINDS = ["needs_you", "done"];

function say(id, text, good) {
  const el = $(id);
  el.textContent = text;
  el.className = "msg " + (good ? "good" : text ? "err" : "");
}

async function load() {
  const s = await chrome.storage.local.get(["backendUrl", "token", "volume", "soundName_needs_you", "soundName_done"]);
  if (s.backendUrl) $("url").value = s.backendUrl;
  if (s.token) $("token").value = s.token;
  if (typeof s.volume === "number") $("volume").value = s.volume;
  for (const k of KINDS) say("msg_" + k, s["soundName_" + k] ? "Using: " + s["soundName_" + k] : "Using the built-in beep.", true);
}

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ backendUrl: $("url").value.trim().replace(/\/+$/, ""), token: $("token").value.trim() });
  say("connMsg", "Saved.", true);
});

$("check").addEventListener("click", async () => {
  await chrome.storage.local.set({ backendUrl: $("url").value.trim().replace(/\/+$/, ""), token: $("token").value.trim() });
  const r = await chrome.runtime.sendMessage({ type: "health" }).catch(() => ({ ok: false }));
  if (!r || !r.ok) return say("connMsg", "Cannot reach the server. Is scripts/run_server.py running?", false);
  const t = await chrome.runtime.sendMessage({ type: "resume" });
  say("connMsg", t.ok ? "Connected. Token accepted." : "Server is up, but: " + t.error, t.ok);
});

$("volume").addEventListener("input", () => chrome.storage.local.set({ volume: parseFloat($("volume").value) }));

function audioSeconds(dataUrl) {
  return new Promise((resolve, reject) => {
    const a = new Audio();
    a.preload = "metadata";
    a.onloadedmetadata = () => resolve(a.duration);
    a.onerror = () => reject(new Error("That file could not be read as audio."));
    a.src = dataUrl;
  });
}

for (const kind of KINDS) {
  $("file_" + kind).addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const dataUrl = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(file); });
    try {
      const seconds = await audioSeconds(dataUrl);
      if (!isFinite(seconds) || seconds > MAX_SECONDS) {
        e.target.value = "";
        return say("msg_" + kind, `That clip is ${Math.round(seconds)} seconds long. The limit is ${MAX_SECONDS}. Pick a shorter clip.`, false);
      }
      await chrome.storage.local.set({ ["sound_" + kind]: dataUrl, ["soundName_" + kind]: file.name });
      say("msg_" + kind, `Saved "${file.name}" (${seconds.toFixed(1)} s).`, true);
    } catch (err) {
      e.target.value = "";
      say("msg_" + kind, err.message, false);
    }
  });
  $("clear_" + kind).addEventListener("click", async () => {
    await chrome.storage.local.remove(["sound_" + kind, "soundName_" + kind]);
    $("file_" + kind).value = "";
    say("msg_" + kind, "Using the built-in beep.", true);
  });
  $("test_" + kind).addEventListener("click", () => chrome.runtime.sendMessage({ type: "alert", kind, text: "This is a test of the " + kind + " sound." }));
}

load();

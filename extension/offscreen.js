// Plays the alert sound. A service worker cannot play audio, so it asks this hidden document to.
"use strict";

const MAX_SECONDS = 30;  // uploaded sounds are capped at 30 s; this is a second safety net
let current = null;

function beep(kind) {
  // Default sounds when none was uploaded: urgent three-note for "needs you", a soft two-note for "done".
  const notes = kind === "needs_you" ? [660, 880, 660] : [523, 784];
  const ctx = new AudioContext();
  notes.forEach((freq, i) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = freq;
    gain.gain.value = kind === "needs_you" ? 0.25 : 0.12;
    osc.connect(gain).connect(ctx.destination);
    const t = ctx.currentTime + i * 0.28;
    osc.start(t);
    osc.stop(t + 0.2);
  });
  setTimeout(() => ctx.close(), 1500);
}

function play(kind, dataUrl, volume) {
  if (current) { current.pause(); current = null; }
  if (!dataUrl) return beep(kind);
  const audio = new Audio(dataUrl);
  audio.volume = Math.min(1, Math.max(0, volume));
  current = audio;
  audio.play().catch(() => beep(kind));
  setTimeout(() => audio.pause(), MAX_SECONDS * 1000);
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.target === "offscreen" && msg.type === "play") play(msg.kind, msg.dataUrl, msg.volume);
});

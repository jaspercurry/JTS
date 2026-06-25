// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /wake/ input-profile/detection card + model-form affordance.
//
// The page is server-rendered. Two pieces of behaviour ride on top:
//
//   1. The input-profile + "Wake detection" cards are live: they poll
//      jasper-control's /aec state (proxied through this page's
//      /detection.json) every few seconds and reconcile the profile radios,
//      layer toggles, and sensitivity slider. User interaction POSTs back to
//      /profile, /layer/<name>, and /sensitivity, which proxy on to
//      jasper-control. This mirrors the
//      optimistic-flip-with-reconcile pattern used elsewhere: a per-control
//      `dirty` flag keeps an in-flight click from being clobbered by a poll.
//
//   2. The model-picker form is a plain POST to ./save; we only disable its
//      submit button on submit so the household sees something happen before
//      the redirect (the daemon restart lands after the redirect — observed in
//      PR #117 that without this the action feels like a no-op).
//
// Confirms use the shared <dialog> helper, never window.confirm (the browser
// can suppress that). Mutating fetches reuse jsonHeaders() from the shared HTTP
// module so the CSRF token is read from the <meta name="jts-csrf"> tag — this
// cached module bakes in no secret. The slider uses an explicit Save button
// rather than apply-on-change so a drag doesn't restart jasper-voice per pixel.

import { jsonHeaders } from "/assets/shared/js/http.js";
import { jtsConfirm, jtsAlert } from "/assets/shared/js/dialog.js";

const PROFILES = [
  "auto",
  "xvf_chip_aec",
  "xvf_chip_aec_testing",
  "xvf_software_aec3",
  "direct_mic",
];
const LAYERS = ["aec", "raw", "dtln", "chip_aec"];
const POLL_MS = 3000;

const dirty = {};
let ignorePollUntil = 0;
let lastServerThreshold = null;

const el = (id) => document.getElementById(id);

function statusLine(active, layerOn, mode) {
  if (mode !== "auto") return "— requires AEC on";
  if (!layerOn) return "— off";
  if (active) return "✓ active";
  return "⏳ starting…";
}

function setText(id, value) {
  const node = el(id);
  if (node) node.textContent = value || "—";
}

function profileLabel(id) {
  switch (id) {
    case "auto":
      return "Automatic";
    case "xvf_chip_aec":
      return "XVF chip-AEC";
    case "xvf_chip_aec_testing":
      return "XVF chip-AEC testing";
    case "xvf_software_aec3":
      return "XVF software AEC3";
    case "direct_mic":
      return "Direct mic";
    case "custom":
      return "Custom";
    default:
      return id || "Unknown";
  }
}

function applyProfileStatus(s) {
  const profile = s.audio_profile || {};
  const legs = s.legs || {};
  const gate = s.chip_aec_gate || {};
  const chipProductionAvailable = !!gate.production_available;
  const chipTestingAvailable = !!gate.testing_available;
  const selection = profile.selection || s.profile || profile.requested || "custom";
  const requested = profile.requested || profile.resolved || selection;
  const active = profile.active || "pending";
  const state = profile.state || "unknown";
  const status = el("profile-status");
  if (status) {
    const bits = [profileLabel(selection)];
    if (requested && requested !== selection) bits.push("resolves to " + profileLabel(requested));
    bits.push(state === "active" ? "active" : state.replace(/_/g, " "));
    if (active && active !== "pending" && active !== requested) {
      bits.push("running " + profileLabel(active));
    }
    if ((selection === "xvf_chip_aec" || selection === "xvf_chip_aec_testing") && gate.status) {
      bits.push("gate " + String(gate.status).replace(/_/g, " "));
    }
    status.textContent = bits.join(" · ");
  }

  PROFILES.forEach((id) => {
    const input = el("profile-" + id);
    const row = el("profile-row-" + id);
    if (!input || !row) return;
    if (!dirty.profile) {
      input.checked = selection === id;
      input.disabled =
        (id === "xvf_chip_aec" && !chipProductionAvailable && selection !== id) ||
        (id === "xvf_chip_aec_testing" && !chipTestingAvailable && selection !== id);
    }
    row.classList.toggle("is-active", selection === id);
    row.classList.toggle("is-disabled", input.disabled);
  });

  const custom = el("profile-custom-warning");
  if (custom) {
    custom.hidden = selection !== "custom";
    custom.textContent =
      "Custom profile active. The layer switches below now own the low-level AEC/wake-leg configuration.";
  }
}

function wakePhraseText(wakeWord, threshold) {
  if (!wakeWord) return "—";
  const bits = [];
  if (wakeWord.label) bits.push(wakeWord.label);
  if (wakeWord.pronunciation) bits.push(wakeWord.pronunciation);
  if (typeof threshold === "number") bits.push("threshold " + threshold.toFixed(2));
  return bits.join(" · ") || "—";
}

function applyMicStatus(s) {
  const mic = s.microphone || {};
  const firmware = mic.firmware || {};
  setText("mic-status-name", mic.name || "unknown");
  setText("mic-status-firmware", firmware.label || "unknown");
  setText("mic-status-mode", mic.processing_mode || "unknown");
  setText("mic-status-session-source", mic.session_source || "unknown");
  setText(
    "mic-status-wake-legs",
    Array.isArray(mic.wake_legs) && mic.wake_legs.length
      ? mic.wake_legs.join(", ")
      : "—",
  );
  setText("mic-status-wake-word", wakePhraseText(s.wake_word, s.threshold));

  const warning = el("mic-status-warning");
  const warnings = Array.isArray(mic.warnings) ? mic.warnings : [];
  if (warning) {
    warning.hidden = warnings.length === 0;
    warning.textContent = warnings.join(" ");
  }
}

// Reconcile server state into the toggles + slider. Skips any control the user
// is mid-interaction with (tracked via `dirty` / the slider's unsaved state).
function applyState(s) {
  const mode = s.mode;
  const bridgeOn = !!s.bridge_active;
  const legs = s.legs || {};
  const gate = s.chip_aec_gate || {};
  const software = s.software_aec3 || {};
  const aecOn = mode === "auto";
  const rawOn = !!(legs.raw && legs.raw.configured);
  const dtlnOn = !!(legs.dtln && legs.dtln.configured);
  const chipOn = !!(legs.chip_aec && legs.chip_aec.configured);
  const chipProductionAvailable = !!gate.production_available;
  const softwareBypassed = !!software.bypassed;
  const softwareConfigured =
    Object.prototype.hasOwnProperty.call(software, "configured")
      ? !!software.configured
      : aecOn && !chipOn;
  const softwareActive =
    Object.prototype.hasOwnProperty.call(software, "active")
      ? !!software.active
      : bridgeOn && softwareConfigured;

  applyProfileStatus(s);
  applyMicStatus(s);

  // Software AEC3 row. Chip-AEC still uses jasper-aec-bridge as a UDP
  // carrier, but WebRTC AEC3 itself is bypassed and should not be toggled.
  if (!dirty.aec) {
    el("layer-aec").checked = softwareConfigured;
    el("layer-aec").disabled = softwareBypassed;
  }
  el("layer-status-aec").textContent = softwareBypassed
    ? "— bypassed (chip-AEC active)"
    : softwareConfigured
      ? softwareActive
        ? "✓ active"
        : "⏳ starting…"
      : "— disabled";
  el("layer-row-aec").classList.toggle(
    "is-disabled",
    softwareBypassed || !softwareConfigured,
  );

  // Raw + DTLN legs require AEC, AND are mutually exclusive with the
  // chip-AEC beams — one chip can't emit both, so when chip-AEC is on the
  // reconciler clears them: grey them out and say why.
  [
    ["raw", rawOn],
    ["dtln", dtlnOn],
  ].forEach(([name, on]) => {
    const blocked = !aecOn || chipOn;
    if (!dirty[name]) {
      el("layer-" + name).checked = on;
      el("layer-" + name).disabled = blocked;
    }
    el("layer-status-" + name).textContent = chipOn
      ? "— paused (chip-AEC active)"
      : statusLine(bridgeOn, on, mode);
    el("layer-row-" + name).classList.toggle("is-disabled", blocked);
  });

  // Chip-AEC beams: require AEC + production-approved mic/DAC gate. Testing
  // unapproved DACs is an explicit input profile, not this expert layer.
  const chipBlocked = !aecOn || !chipProductionAvailable;
  if (!dirty.chip_aec) {
    el("layer-chip_aec").checked = chipOn;
    el("layer-chip_aec").disabled = chipBlocked;
  }
  el("layer-status-chip_aec").textContent = !aecOn
    ? "— requires AEC on"
    : !chipProductionAvailable
      ? "— use chip-AEC testing profile"
      : chipOn
        ? bridgeOn
          ? "✓ active"
          : "⏳ starting…"
        : "— off";
  el("layer-row-chip_aec").classList.toggle("is-disabled", chipBlocked);

  // Sensitivity — only overwrite from the server when the user isn't mid-drag
  // and hasn't queued an unsaved change.
  const slider = el("sensitivity-input");
  const valueLabel = el("sensitivity-value");
  const saveBtn = el("sensitivity-save");
  const serverThr = typeof s.threshold === "number" ? s.threshold : 0.5;
  slider.disabled = false;
  if (
    lastServerThreshold === null ||
    (Math.abs(parseFloat(slider.value) - lastServerThreshold) < 0.001 &&
      !saveBtn.classList.contains("is-dirty"))
  ) {
    slider.value = serverThr.toFixed(2);
    valueLabel.textContent = serverThr.toFixed(2);
    saveBtn.disabled = true;
    saveBtn.classList.remove("is-dirty");
  }
  lastServerThreshold = serverThr;
}

async function pollDetection() {
  if (document.visibilityState === "hidden") return;
  if (Date.now() < ignorePollUntil) return;
  try {
    const r = await fetch("detection.json", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    applyState(await r.json());
  } catch (e) {
    LAYERS.forEach((name) => {
      el("layer-status-" + name).textContent = "Disconnected";
    });
    setText("profile-status", "Disconnected");
    setText("mic-status-name", "Disconnected");
    setText("mic-status-firmware", "—");
    setText("mic-status-mode", "—");
    setText("mic-status-session-source", "—");
    setText("mic-status-wake-legs", "—");
    setText("mic-status-wake-word", "—");
    const warning = el("mic-status-warning");
    if (warning) {
      warning.hidden = false;
      warning.textContent = "Could not reach jasper-control.";
    }
  }
}

async function postProfile(profile) {
  dirty.profile = true;
  ignorePollUntil = Date.now() + 1500;
  try {
    const r = await fetch("profile", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ profile }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
    dirty.profile = false;
    applyState(body);
  } catch (err) {
    await jtsAlert("Profile change failed: " + err.message);
    dirty.profile = false;
    setTimeout(pollDetection, 250);
  }
}

async function postLayer(name, wanted) {
  dirty[name] = true;
  ignorePollUntil = Date.now() + 1500;
  try {
    const r = await fetch("layer/" + name, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ enabled: wanted }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
    // The server returns the full state after applying — reconcile right away
    // so the AEC-off → legs-disabled transition is instant.
    dirty[name] = false;
    applyState(body);
  } catch (err) {
    await jtsAlert("Toggle failed: " + err.message);
    dirty[name] = false;
    el("layer-" + name).checked = !wanted; // roll back the optimistic flip
  }
}

PROFILES.forEach((profile) => {
  const input = el("profile-" + profile);
  if (!input) return;
  input.addEventListener("change", async () => {
    if (!input.checked) return;
    if (
      profile === "direct_mic" &&
      !(await jtsConfirm(
        "Use direct mic input?\n\n" +
          "This disables the AEC bridge. Wake while music is playing may be unreliable until you choose an AEC profile again.",
        { danger: true },
      ))
    ) {
      setTimeout(pollDetection, 0);
      return;
    }
    if (
      profile === "xvf_chip_aec" &&
      !(await jtsConfirm(
        "Use the XVF chip-AEC profile?\n\n" +
          "This uses the active mic profile's validated hardware AEC beam plan and disables the software raw/DTLN wake legs.",
      ))
    ) {
      setTimeout(pollDetection, 0);
      return;
    }
    if (
      profile === "xvf_chip_aec_testing" &&
      !(await jtsConfirm(
        "Use XVF chip-AEC testing?\n\n" +
          "This routes the live mic path through hardware AEC on an unapproved DAC so you can validate it. Use software AEC3 again if wake reliability drops.",
        { danger: true },
      ))
    ) {
      setTimeout(pollDetection, 0);
      return;
    }
    postProfile(profile);
  });
});

// Wire each toggle. AEC-off and DTLN-on get an extra confirm because both carry
// a real cost (restart + RAM); the others apply immediately.
LAYERS.forEach((name) => {
  el("layer-" + name).addEventListener("change", async () => {
    const cb = el("layer-" + name);
    if (
      name === "aec" &&
      !cb.checked &&
      !(await jtsConfirm(
        "Disable software AEC3?\n\n" +
          "jasper-voice will restart — wake unavailable ~15 s. Turning AEC " +
          "off also pauses the raw + DTLN layers (they need the bridge running). " +
          "Chip-AEC profiles bypass software AEC3 automatically.",
        { danger: true },
      ))
    ) {
      cb.checked = true;
      return;
    }
    if (
      name === "dtln" &&
      cb.checked &&
      !(await jtsConfirm(
        "Enable DTLN neural AEC?\n\n" +
          "+~75 MB RAM, +~25% one core. Recommended for 2 GB Pis.\n" +
          "jasper-voice + bridge will restart (~15 s).",
      ))
    ) {
      cb.checked = false;
      return;
    }
    if (
      name === "chip_aec" &&
      cb.checked &&
      !(await jtsConfirm(
        "Use the chip-AEC beams as the wake layers?\n\n" +
          "This switches to the mic array's hardware echo-cancelled " +
          "beam plan and PAUSES the raw + DTLN layers — the chip " +
          "can't do both at once.\n" +
          "jasper-voice + bridge will restart (~15 s).",
      ))
    ) {
      cb.checked = false;
      return;
    }
    postLayer(name, cb.checked);
  });
});

// Sensitivity slider: track unsaved changes, save on explicit click.
const slider = el("sensitivity-input");
const valueLabel = el("sensitivity-value");
const saveBtn = el("sensitivity-save");

slider.addEventListener("input", () => {
  const v = parseFloat(slider.value);
  valueLabel.textContent = v.toFixed(2);
  const changed =
    lastServerThreshold === null || Math.abs(v - lastServerThreshold) > 0.001;
  saveBtn.disabled = !changed;
  saveBtn.classList.toggle("is-dirty", changed);
});

saveBtn.addEventListener("click", async () => {
  const v = parseFloat(slider.value);
  saveBtn.disabled = true;
  saveBtn.textContent = "…";
  try {
    const r = await fetch("sensitivity", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ value: v }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
    saveBtn.classList.remove("is-dirty");
  } catch (err) {
    await jtsAlert("Save failed: " + err.message);
  }
  saveBtn.textContent = "Save";
  setTimeout(pollDetection, 500);
});

// Model-picker form: disable submit so the restart-in-progress is visible
// before the redirect (the redirect fires before the daemon is back up).
const wakeForm = document.getElementById("wake-form");
if (wakeForm) {
  wakeForm.addEventListener("submit", () => {
    const btn = document.getElementById("wake-save");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Saving…";
    }
  });
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") pollDetection();
});
pollDetection();
setInterval(pollDetection, POLL_MS);

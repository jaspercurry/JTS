// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /wake/ microphone/echo/wake card + model-form affordance.
//
// The page is server-rendered. Two pieces of behaviour ride on top:
//
//   1. The microphone, echo-cancellation, and advanced-fusion cards are live:
//      they poll jasper-control's backend-owned `mic_settings` view model
//      (proxied through this page's /detection.json) and render it. User
//      interaction POSTs intent back to /profile, /layer/<name>, and
//      /sensitivity, which proxy on to jasper-control. This mirrors the
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

import { jsonHeaders, postJSON } from "/assets/shared/js/http.js";
import { jtsConfirm, jtsAlert } from "/assets/shared/js/dialog.js";

const LAYERS = ["raw", "dtln", "chip_aec_150", "chip_aec_210"];
const POLL_MS = 3000;

const dirty = {};
let ignorePollUntil = 0;
let lastServerThreshold = null;
let profileChoices = {};
let fusionToggles = {};
let firmwareUpdateBusy = false;
let usbMicBusy = false;
let usbMicLegBusy = false;

const el = (id) => document.getElementById(id);

function setText(id, value) {
  const node = el(id);
  if (node) node.textContent = value || "—";
}

function profileInputs() {
  return Array.from(document.querySelectorAll('input[name="profile-choice"]'));
}

function choicesByProfile(settings) {
  const out = {};
  const echoChoices = ((settings.echo || {}).choices || []);
  echoChoices.forEach((choice) => {
    if (choice && choice.profile) out[choice.profile] = choice;
  });
  const validation = (settings.advanced || {}).validation_profile || {};
  if (validation.profile) out[validation.profile] = validation;
  return out;
}

function firmwareMeta(fw) {
  const target = fw.target || {};
  const current = fw.current || {};
  const bits = [];
  if (current.geometry) bits.push("Detected " + current.geometry + " geometry");
  if (target.filename) bits.push("Downloads " + target.filename);
  if (target.sha256) bits.push("SHA256 " + target.sha256.slice(0, 12) + "...");
  return bits.join(" · ") || "—";
}

function applyFirmwareUpdateStatus(s) {
  const fw = s.firmware_update || {};
  const card = el("firmware-update-card");
  const button = el("firmware-update-button");
  if (!card || !button) return;
  const state = fw.state || "unknown";
  const show = ["update_required", "updating", "failed", "unknown", "unsupported", "current"].includes(state);
  card.hidden = !show;
  card.dataset.state = state;
  setText("firmware-update-title", fw.title || "Microphone firmware");
  setText("firmware-update-detail", fw.detail || "—");
  setText("firmware-update-meta", firmwareMeta(fw));
  const action = fw.action || {};
  button.textContent = action.label || "Download and update firmware";
  button.disabled = firmwareUpdateBusy || !action.enabled;
  if (state === "updating") {
    button.textContent = "Updating…";
    button.disabled = true;
  }
}

function applyProfileStatus(s) {
  const settings = s.mic_settings || {};
  const echo = settings.echo || {};
  const choices = choicesByProfile(settings);
  profileChoices = choices;
  const profile = s.audio_profile || {};
  const selection = profile.selection || s.profile || profile.requested || "custom";

  setText("echo-status-title", echo.title || "Microphone input");
  setText("echo-status-detail", echo.detail || profile.reason || "—");

  const warning = el("echo-status-warning");
  const hardware = echo.hardware || {};
  if (warning) {
    const showGateWarning =
      hardware.selected && !hardware.active && hardware.gate_detail;
    warning.hidden = !showGateWarning;
    warning.textContent = showGateWarning ? hardware.gate_detail : "";
  }

  profileInputs().forEach((input) => {
    const id = input.value;
    const row = el("profile-row-" + id);
    if (!input || !row) return;
    const choice = choices[id];
    const visible = choice ? choice.visible !== false : id === selection;
    row.hidden = !visible;
    if (!dirty.profile) {
      input.checked = choice ? !!choice.selected : selection === id;
      input.disabled = choice ? !choice.enabled : true;
    }
    const selected = choice ? !!choice.selected : selection === id;
    row.classList.toggle("is-active", selected);
    row.classList.toggle("is-disabled", input.disabled);
    setText("profile-name-" + id, choice && choice.label);
    setText("profile-desc-" + id, choice && choice.description);
    setText("profile-badge-" + id, choice && choice.badge);
    const status = el("profile-status-" + id);
    if (status) status.textContent = choice && choice.status ? choice.status : "—";
  });
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
  const settings = s.mic_settings || {};
  const micView = settings.mic || {};
  const mic = s.microphone || {};
  const firmware = mic.firmware || {};
  setText("mic-status-name", micView.title || mic.name || "unknown");
  setText("mic-status-firmware", micView.subtitle || firmware.label || "unknown");
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

function applyUsbMicStatus(s) {
  const usbMic = s.usb_mic || {};
  const input = el("usb-mic-toggle");
  const row = el("usb-mic-row");
  if (!input || !row) return;
  if (!usbMicBusy) {
    input.checked = !!usbMic.enabled;
    input.disabled = !usbMic.toggle_enabled;
  }
  row.classList.toggle("is-disabled", input.disabled);
  const state = usbMic.state ? usbMic.state.replaceAll("_", " ") : "unknown";
  setText(
    "usb-mic-status",
    state.charAt(0).toUpperCase() + state.slice(1) + " · " + (usbMic.detail || "—"),
  );
  setText("usb-mic-notice", usbMic.notice || "—");

  const source = usbMic.source_selection || {};
  const select = el("usb-mic-leg-select");
  const choices = Array.isArray(source.choices) ? source.choices : [];
  const requested = typeof source.requested === "string" ? source.requested : "";
  if (select && !usbMicLegBusy) {
    select.replaceChildren();
    choices.forEach((choice) => {
      if (!choice || typeof choice.value !== "string") return;
      const option = document.createElement("option");
      option.value = choice.value;
      option.textContent = choice.label || choice.value;
      if (choice.description) option.title = choice.description;
      select.appendChild(option);
    });
    if (requested && !choices.some((choice) => choice && choice.value === requested)) {
      const unavailable = document.createElement("option");
      unavailable.value = requested;
      unavailable.textContent = requested + " (unavailable)";
      unavailable.disabled = true;
      select.appendChild(unavailable);
    }
    select.value = requested;
    select.dataset.requested = requested;
    select.disabled = choices.length === 0;
  }
  const requestedChoice = choices.find(
    (choice) => choice && choice.value === requested,
  );
  const requestedLabel = (requestedChoice && requestedChoice.label) || requested || "—";
  const applied = source.applied;
  setText(
    "usb-mic-leg-status",
    applied
      ? "Requested " + requestedLabel + " · Applied " +
        (applied.effective_label || applied.leg || "—")
      : "Requested " + requestedLabel + " · Waiting for the microphone bridge",
  );
}

// Reconcile server state into the toggles + slider. Skips any control the user
// is mid-interaction with (tracked via `dirty` / the slider's unsaved state).
function applyState(s) {
  const settings = s.mic_settings || {};
  const fusion = settings.fusion || {};
  const toggles = {};
  ((fusion && fusion.toggles) || []).forEach((toggle) => {
    if (toggle && toggle.id) toggles[toggle.id] = toggle;
  });
  fusionToggles = toggles;

  applyProfileStatus(s);
  applyMicStatus(s);
  applyUsbMicStatus(s);
  applyFirmwareUpdateStatus(s);

  setText("fusion-summary", fusion.summary || "—");
  LAYERS.forEach((name) => {
    const toggle = toggles[name] || {};
    const input = el("layer-" + name);
    const row = el("layer-row-" + name);
    if (!input || !row) return;
    if (!dirty[name]) {
      input.checked = !!toggle.checked;
      input.disabled = !toggle.enabled;
    }
    const reason = toggle.disabled_reason || "";
    setText("layer-name-" + name, toggle.label);
    setText("layer-desc-" + name, toggle.description);
    setText("layer-meta-" + name, toggle.cost);
    setText("layer-status-" + name, reason || (toggle.status || "—"));
    row.classList.toggle("is-disabled", input.disabled);
  });

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
      setText("layer-status-" + name, "Disconnected");
    });
    profileInputs().forEach((input) => {
      input.disabled = true;
      const row = el("profile-row-" + input.value);
      if (row) row.classList.add("is-disabled");
      setText("profile-status-" + input.value, "Disconnected");
    });
    setText("echo-status-title", "Disconnected");
    setText("echo-status-detail", "Could not reach jasper-control.");
    const fwCard = el("firmware-update-card");
    const fwButton = el("firmware-update-button");
    if (fwCard) fwCard.hidden = true;
    if (fwButton) fwButton.disabled = true;
    setText("mic-status-name", "Disconnected");
    setText("mic-status-firmware", "—");
    setText("mic-status-mode", "—");
    setText("mic-status-session-source", "—");
    setText("mic-status-wake-legs", "—");
    setText("mic-status-wake-word", "—");
    const usbMic = el("usb-mic-toggle");
    if (usbMic) usbMic.disabled = true;
    const usbMicRow = el("usb-mic-row");
    if (usbMicRow) usbMicRow.classList.add("is-disabled");
    setText("usb-mic-status", "Disconnected");
    const usbMicLeg = el("usb-mic-leg-select");
    if (usbMicLeg) usbMicLeg.disabled = true;
    setText("usb-mic-leg-status", "Disconnected");
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

async function postUsbMic(wanted) {
  const input = el("usb-mic-toggle");
  usbMicBusy = true;
  input.disabled = true;
  setText("usb-mic-status", wanted ? "Adding microphone…" : "Removing microphone…");
  try {
    const r = await fetch("usb-mic", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ enabled: wanted }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
    usbMicBusy = false;
    applyState(body);
    // Re-enumeration may briefly take this page's USB network path down.
    ignorePollUntil = Date.now() + 2500;
  } catch (err) {
    usbMicBusy = false;
    input.checked = !wanted;
    await jtsAlert("USB microphone change failed: " + err.message);
    setTimeout(pollDetection, 500);
  }
}

async function postUsbMicLeg(leg) {
  const select = el("usb-mic-leg-select");
  usbMicLegBusy = true;
  select.disabled = true;
  setText("usb-mic-leg-status", "Applying computer microphone source…");
  try {
    const r = await fetch("usb-mic-leg", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ leg }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
    usbMicLegBusy = false;
    applyState(body);
    ignorePollUntil = Date.now() + 1500;
  } catch (err) {
    usbMicLegBusy = false;
    await jtsAlert("Computer microphone source change failed: " + err.message);
    setTimeout(pollDetection, 250);
  }
}

profileInputs().forEach((input) => {
  const profile = input.value;
  if (!input) return;
  input.addEventListener("change", async () => {
    if (!input.checked) return;
    const confirm = (profileChoices[profile] || {}).confirm;
    if (confirm) {
      const message = [confirm.title || "", confirm.body || ""]
        .filter(Boolean)
        .join("\n\n");
      if (!(await jtsConfirm(message, { danger: !!confirm.danger }))) {
        setTimeout(pollDetection, 0);
        return;
      }
    }
    postProfile(profile);
  });
});

// Wire each advanced stream toggle. Confirm text comes from the backend view
// model so the browser does not own per-channel resource policy.
LAYERS.forEach((name) => {
  el("layer-" + name).addEventListener("change", async () => {
    const cb = el("layer-" + name);
    const confirm = (fusionToggles[name] || {}).confirm;
    if (cb.checked && confirm) {
      const message = [confirm.title || "", confirm.body || ""]
        .filter(Boolean)
        .join("\n\n");
      if (!(await jtsConfirm(message, { danger: !!confirm.danger }))) {
        cb.checked = false;
        return;
      }
    }
    postLayer(name, cb.checked);
  });
});

const usbMicToggle = el("usb-mic-toggle");
if (usbMicToggle) {
  usbMicToggle.addEventListener("change", () => {
    postUsbMic(usbMicToggle.checked);
  });
}

const usbMicLegSelect = el("usb-mic-leg-select");
if (usbMicLegSelect) {
  usbMicLegSelect.addEventListener("change", () => {
    postUsbMicLeg(usbMicLegSelect.value);
  });
}

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

const firmwareButton = el("firmware-update-button");
if (firmwareButton) {
  firmwareButton.addEventListener("click", async () => {
    if (!(await jtsConfirm(
      "Update microphone firmware?\n\n" +
        "JTS will download the hash-pinned firmware from Seeed's GitHub, " +
        "stop voice briefly, flash the microphone over DFU, then reconcile AEC. " +
        "Keep the microphone plugged in until the update finishes.",
      { danger: true },
    ))) {
      return;
    }
    firmwareUpdateBusy = true;
    firmwareButton.disabled = true;
    firmwareButton.textContent = "Starting…";
    try {
      const body = await postJSON("firmware/update", {});
      applyState(body);
    } catch (err) {
      await jtsAlert("Firmware update failed to start: " + err.message);
    }
    firmwareUpdateBusy = false;
    setTimeout(pollDetection, 500);
  });
}

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

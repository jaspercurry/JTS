// main.js — /wake/ detection-layers card + model-form submit affordance.
//
// The page is server-rendered. Two pieces of behaviour ride on top:
//
//   1. The "Wake detection" card is live: it polls jasper-control's /aec state
//      (proxied through this page's /detection.json) every few seconds and
//      reconciles the layer toggles (AEC master, raw leg, DTLN leg, and the
//      hardware-conditional chip-AEC beams — mutually exclusive with raw +
//      DTLN) plus a sensitivity slider. User interaction POSTs back to
//      /layer/<name> and
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

import { jsonHeaders } from "/assets/shared/js/http.js";
import { jtsConfirm, jtsAlert } from "/assets/shared/js/dialog.js";

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

// Reconcile server state into the toggles + slider. Skips any control the user
// is mid-interaction with (tracked via `dirty` / the slider's unsaved state).
function applyState(s) {
  const mode = s.mode;
  const bridgeOn = !!s.bridge_active;
  const legs = s.legs || {};
  const aecOn = mode === "auto";
  const rawOn = !!(legs.raw && legs.raw.configured);
  const dtlnOn = !!(legs.dtln && legs.dtln.configured);
  const chipOn = !!(legs.chip_aec && legs.chip_aec.configured);
  // Hardware gate: the chip-AEC beams only exist on the 6-ch firmware.
  const chipAvailable = !!(legs.chip_aec && legs.chip_aec.available);

  // AEC master row.
  if (!dirty.aec) {
    el("layer-aec").checked = aecOn;
    el("layer-aec").disabled = false;
  }
  el("layer-status-aec").textContent = aecOn
    ? bridgeOn
      ? "✓ active"
      : "⏳ starting (or chip not on 6-ch firmware)"
    : "— disabled";
  el("layer-row-aec").classList.toggle("is-disabled", !aecOn);

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

  // Chip-AEC beams: require AEC + the 6-ch firmware. Disabled (greyed) when
  // AEC is off or the chip isn't on the 6-ch firmware; enabling pauses the
  // raw + DTLN layers (mutual exclusion, handled above).
  const chipBlocked = !aecOn || !chipAvailable;
  if (!dirty.chip_aec) {
    el("layer-chip_aec").checked = chipOn;
    el("layer-chip_aec").disabled = chipBlocked;
  }
  el("layer-status-chip_aec").textContent = !aecOn
    ? "— requires AEC on"
    : !chipAvailable
      ? "— needs 6-channel firmware"
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

// Wire each toggle. AEC-off and DTLN-on get an extra confirm because both carry
// a real cost (restart + RAM); the others apply immediately.
LAYERS.forEach((name) => {
  el("layer-" + name).addEventListener("change", async () => {
    const cb = el("layer-" + name);
    if (
      name === "aec" &&
      !cb.checked &&
      !(await jtsConfirm(
        "Disable AEC echo cancellation?\n\n" +
          "jasper-voice will restart — wake unavailable ~15 s. Turning AEC " +
          "off also pauses the raw + DTLN layers (they need the bridge running).",
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
          "150°/210° beams and PAUSES the raw + DTLN layers — the chip " +
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

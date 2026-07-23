// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { h } from "/assets/shared/js/dom.js";
import { collapsible, actionButton } from "./components.js";
import { postJSON } from "/assets/shared/js/http.js";
import { jtsConfirm } from "/assets/shared/js/dialog.js";

export function buildUsbForensicsCard() {
  const summary = h("p.info-card__note", { "attr:aria-live": "polite" });
  const detail = h("p.info-card__note.muted");
  const toggle = h("input.debug-row__cb", { type: "checkbox",
    "attr:aria-label": "USB gadget forensics" });
  const capture = actionButton("Capture now", { variant: "default" });
  const repair = actionButton("Capture & repair USB", { variant: "danger" });

  async function send(body, message) {
    summary.textContent = message;
    toggle.disabled = capture.disabled = repair.disabled = true;
    try {
      update(await postJSON("/system/usb-forensics", body));
    } catch (e) {
      console.error("system: USB forensics action failed", e);
      summary.textContent = "Failed: " + e.message;
      toggle.disabled = false;
    }
  }

  toggle.onchange = async () => {
    const enabled = toggle.checked;
    if (!await jtsConfirm((enabled ? "Enable" : "Disable") +
        " USB forensics? This keeps a bounded timeline in RAM.")) {
      toggle.checked = !enabled; return;
    }
    send({ action: "set_enabled", enabled }, enabled ? "Starting…" : "Stopping…");
  };
  capture.onclick = async () => {
    if (await jtsConfirm("Freeze the current USB timeline into an incident snapshot?"))
      send({ action: "capture" }, "Capture queued…");
  };
  repair.onclick = async () => {
    if (await jtsConfirm(
      "Capture evidence and restart the JTS USB connection? Audio will disconnect briefly.",
      { danger: true },
    )) send({ action: "repair" }, "Repair queued…");
  };

  function update(state) {
    toggle.checked = !!state.enabled; toggle.disabled = false;
    const ready = !!state.running; capture.disabled = repair.disabled = !ready;
    summary.textContent = state.pending_action
      ? (state.pending_action === "repair" ? "USB repair queued…" : "Capture queued…")
      : !state.enabled ? "Off — no sampler is running."
      : ready ? "Recording a bounded USB timeline in RAM."
        : "Enabled — sampler is starting or needs attention.";
    const cap = state.ram_cap_bytes
      ? "RAM cap " + (state.ram_cap_bytes / 1048576).toFixed(1) + " MiB"
      : "RAM buffer inactive";
    detail.textContent = cap + " · no continuous disk writes · latest " +
      (state.latest_artifact || "none yet");
  }

  const body = h("div.info-card", null,
    h("p.info-card__note", null,
      "Leave this on while reproducing the USB audio fault. It survives deploys and reboots until turned off."),
    h("label.debug-row", null, toggle,
      h("span.debug-row__label", null, "USB gadget forensics")),
    summary, h("div.btn-row", null, capture, repair), detail);
  return { card: collapsible({ title: "USB forensics", open: false, body }), update };
}

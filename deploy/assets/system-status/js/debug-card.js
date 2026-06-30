// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// debug-card.js — the /system "Debug logging" card.
//
// Self-contained: fetches its own state from the long-lived control
// daemon (GET/POST /debug, an *absolute* path nginx proxies to :8780 —
// this card's host page on :8772 idle-exits, so the toggle's auto-expiry
// timer lives in control, not here), renders one checkbox per subsystem,
// and runs a client-side expiry countdown.
//
// Additive-only by construction: a toggle can only raise a subsystem to
// DEBUG; it never lowers logging below the always-on WARN/`event=` floor.
// Text content goes through dom.js (escaped by construction) — no
// innerHTML path. The collapsible's open state survives row re-renders.

import { h } from "/assets/shared/js/dom.js";
import { collapsible } from "./components.js";
import { getJSON, jsonHeaders } from "./api.js";
import { jtsConfirm } from "/assets/shared/js/dialog.js";

export function buildDebugCard() {
  const rows = h("div.debug-rows");
  const countdown = h("p.info-card__note", { "attr:aria-live": "polite" });
  const body = h("div.info-card", null,
    h("p.info-card__note", null,
      "Raise one subsystem's logging to DEBUG for troubleshooting. " +
      "Most daemons restart briefly to apply; it auto-expires on its own."),
    rows,
    countdown);
  const card = collapsible({ title: "Debug logging", open: false, body });

  let timer = null;

  function render(state) {
    rows.replaceChildren(...(state.subsystems || []).map(rowFor));
    startCountdown(state.remaining_sec, state.any_active);
  }

  function rowFor(s) {
    const status = h("span.debug-row__status.muted");
    const cb = h("input.debug-row__cb", {
      type: "checkbox", checked: s.enabled,
      "attr:aria-label": "Debug logging for " + s.label,
      onchange: (e) => toggle(s, e.target, status),
    });
    return h("label.debug-row", null,
      cb, h("span.debug-row__label", null, s.label), status);
  }

  async function toggle(s, cb, status) {
    const on = cb.checked;
    const applyText = s.apply_policy === "in_process"
      ? "Applies immediately."
      : s.apply_policy === "restart_if_active"
        ? "Applies now if it is running; otherwise applies next time it starts."
        : "This restarts " + s.label + " briefly to apply.";
    const msg = on
      ? "Turn on DEBUG logging for " + s.label + "? " +
        applyText
      : "Turn off DEBUG logging for " + s.label + "?";
    if (!await jtsConfirm(msg)) { cb.checked = !on; return; }
    cb.disabled = true;
    status.textContent = "Applying…";
    try {
      const r = await fetch("/debug", {
        method: "POST", headers: jsonHeaders(),
        body: JSON.stringify({ subsystem: s.id, enabled: on }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
      status.textContent = "";
      render(data);  // POST returns the fresh snapshot; rebuilds the rows
    } catch (e) {
      console.error("system: debug toggle failed", e);
      status.textContent = "Failed: " + e.message;
      cb.checked = !on;       // revert the optimistic flip
      cb.disabled = false;
    }
  }

  function startCountdown(remaining, anyActive) {
    if (timer) { clearInterval(timer); timer = null; }
    if (!anyActive || !remaining) { countdown.textContent = ""; return; }
    let left = Math.round(remaining);
    const tick = () => {
      if (left <= 0) {
        clearInterval(timer); timer = null;
        countdown.textContent = "";
        load();  // re-sync: control should have auto-quieted by now
        return;
      }
      const m = Math.floor(left / 60);
      const s = String(left % 60).padStart(2, "0");
      countdown.textContent = "Debug auto-expires in " + m + ":" + s + ".";
      left -= 1;
    };
    tick();
    timer = setInterval(tick, 1000);
  }

  async function load() {
    try {
      render(await getJSON("/debug"));
    } catch (e) {
      console.error("system: debug state load failed", e);
      rows.replaceChildren(h("p.info-card__note", null,
        "Couldn't load debug state — see the console."));
    }
  }

  load();
  return card;
}

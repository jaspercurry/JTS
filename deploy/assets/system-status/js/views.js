// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// views.js — builds the dashboard structure once, then refreshes the live
// sections each poll. Two design choices worth knowing:
//
//  * build-once + update (not full re-render): interaction state survives a
//    poll — the per-service collapsible stays open, action buttons keep their
//    in-flight label, the audio toggle doesn't flicker mid-apply.
//  * renderSection isolates + memoises each card: a builder that throws logs
//    to the console and shows an inline note in *that* card only (never blanks
//    the page, never masquerades as a disconnect); a card whose data is
//    unchanged is skipped (no churn, no lost text selection).

import { h } from "/assets/shared/js/dom.js";
import {
  header, livePill, titledCard, actionButton, collapsible, renderSection,
} from "./components.js";
import {
  vitalsCards, softwareList, haBody, networkList, servicesTable, waitingNote,
} from "./sections.js";
import { buildDebugCard } from "./debug-card.js";

export function buildPage(root, handlers) {
  const live = livePill();

  // Data sections: title built once, body re-rendered (when changed) per poll.
  const vitals = h("section.stat-grid");
  const software = titledCard("Software");
  const ha = titledCard("Home Assistant");
  const network = titledCard("Network");

  // Actions — built once, never touched by a poll. actionsStatus is an
  // aria-live region for post-action feedback (esp. reboot/power-off, which
  // take the page offline, so the button label alone isn't enough).
  const actionsStatus = h("p.info-card__note", { "attr:aria-live": "polite" });
  const capabilityNote = h("p.info-card__note", null, "");
  capabilityNote.hidden = true;
  const restartVoice = actionButton("Restart voice", {
    variant: "default", onClick: handlers.restartVoice,
  });
  const restartAudio = actionButton("Restart audio", {
    variant: "default", onClick: handlers.restartAudio,
  });
  // Re-enabled from /system/snapshot capabilities on the first successful
  // poll. This avoids a tiny cold-load window where a satellite endpoint
  // could show full-speaker actions before its profile arrives.
  restartVoice.disabled = true;
  restartAudio.disabled = true;
  const actions = titledCard("Actions");
  actions.section.classList.add("system-actions");
  actions.body.append(
    h("p.info-card__note", null,
      "Restart services or shut down the Pi. Anyone on this Wi-Fi can run these actions."),
    capabilityNote,
    h("div.btn-row", null,
      restartVoice,
      restartAudio,
      actionButton("Reboot speaker", { variant: "danger", onClick: handlers.reboot }),
      actionButton("Power off", { variant: "danger", onClick: handlers.poweroff })),
    actionsStatus,
    h("p.info-card__note", null,
      "Power off before unplugging or changing cables; it stays off until power is re-plugged."),
  );

  // Run diagnostics — built once; the output region persists between polls.
  const diagOutput = h("div.diag-output", { style: { display: "none" } });
  const diagButton = actionButton("Run diagnostics now", {
    variant: "default", onClick: () => handlers.runDiagnostics(diagButton, diagOutput),
  });
  const diag = titledCard("Run diagnostics", { accent: true });
  diag.body.append(
    h("p.info-card__note", null,
      "Shows the latest ", h("code", null, "jasper-doctor"),
      " snapshot and refreshes stale results in the background."),
    h("div", null, diagButton),
    diagOutput,
  );

  // Per-service usage — collapsible shell built once (open state persists);
  // the warn banner + table re-render into svcBody each poll.
  const svcBody = h("div");
  const services = collapsible({
    title: "Per-service usage", open: true,
    body: h("div.info-card", null,
      h("p.info-card__note", null,
        "Cgroup CPU and memory by service; totals show unlisted system work."),
      svcBody),
  });

  // Debug logging — self-contained collapsible (built once; fetches its
  // own /debug state from control and self-manages). Not poll-driven.
  const debugCard = buildDebugCard();

  root.replaceChildren(
    header({ title: "Status", backHref: "/", activeView: "system" }),
    h("main.app-main", null,
      live.el, vitals, software.section, ha.section,
      network.section, actions.section,
      diag.section, debugCard, services),
  );
  root.setAttribute("aria-busy", "false");

  return {
    staleness: live.label,
    vitals, software: software.body, ha: ha.body,
    network: network.body, svc: svcBody,
    actionsStatus, capabilityNote,
    actionButtons: { restartVoice, restartAudio },
    _memo: {},
  };
}

function capabilityAllows(caps, key) {
  return !caps || caps[key] !== false;
}

function setActionAvailable(btn, allowed) {
  btn.hidden = !allowed;
  btn.disabled = !allowed;
}

function applySystemCapabilities(refs, caps) {
  refs.systemCapabilities = caps || null;
  const canRestartVoice = capabilityAllows(caps, "restart_voice");
  const canRestartAudio = capabilityAllows(caps, "restart_audio");

  setActionAvailable(refs.actionButtons.restartVoice, canRestartVoice);
  setActionAvailable(refs.actionButtons.restartAudio, canRestartAudio);

  // The capability map no longer carries a per-profile explanation string
  // (the removed endpoint tier was its only producer), so keep the note
  // hidden rather than reading a field that is always absent.
  refs.capabilityNote.hidden = true;
  refs.capabilityNote.textContent = "";
}

export function update(refs, snap) {
  snap = snap || {};
  const hasMetrics = !!snap.metrics;
  const m = snap.metrics || {};
  const cur = m.current || {};
  const hist = m.history || {};
  const services = m.services || [];
  const cores = cur.per_core_cpu_pct || [];

  if (!hasMetrics) {
    refs.staleness.textContent = "No metrics yet (sampler warming up?).";
  } else {
    const lastSampled = m.last_sample_at;
    const stale = lastSampled ? Math.max(0, Date.now() / 1000 - lastSampled) : null;
    refs.staleness.textContent = lastSampled
      ? "Live · sampler " + (stale < 12 ? "OK" : "stale " + Math.round(stale) + "s")
      : "Sampler not running.";
  }

  // Metrics-dependent cards: real content once sampling starts, else a
  // placeholder (rather than empty cards) during warm-up.
  if (hasMetrics) {
    renderSection(refs, "vitals", refs.vitals, { cur, hist, cores }, () => vitalsCards(cur, hist, cores));
    renderSection(refs, "network", refs.network, cur, () => networkList(cur));
    renderSection(refs, "services", refs.svc, m, () => servicesTable(m, services));
  } else {
    renderSection(refs, "vitals", refs.vitals, "warmup", () => waitingNote(true));
    renderSection(refs, "network", refs.network, "warmup", () => waitingNote());
    renderSection(refs, "services", refs.svc, "warmup", () => waitingNote());
  }

  // Top-level cards: render with or without metrics.
  renderSection(refs, "software", refs.software,
    { b: snap.build, u: cur.uptime_sec, p: snap.voice_provider }, () => softwareList(snap, cur));
  renderSection(refs, "ha", refs.ha, snap.home_assistant, () => haBody(snap.home_assistant));
  try {
    applySystemCapabilities(refs, snap.system_capabilities);
  } catch (e) {
    console.error("system: applying capabilities failed", e);
  }
}

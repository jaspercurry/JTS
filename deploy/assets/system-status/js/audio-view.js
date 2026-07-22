// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// audio-view.js — build-once + poll-update orchestration for /system/audio/.
// The normalized audio_health payload is the single source of truth; the view
// only composes shared cards and keeps optional failures isolated.

import { h } from "/assets/shared/js/dom.js";
import {
  livePill, titledCard, choiceCard, collapsible, renderSection,
} from "./components.js";
import { AUDIO_OPTIONS, updateAudioQuality } from "./sections.js";
import { fmtEpochAgo } from "./format.js";
import {
  unavailableBody, currentStreamBody, currentIncident, currentIncidentBody,
  issuesBody, otherSources, sourcesBody, technicalBody, refreshRelativeTimes,
} from "./audio-sections.js";

function buildAudioQuality(handlers) {
  const requested = h("dd", null, "—");
  const active = h("dd", null, "—");
  const status = h("p.info-card__note", null, "");
  const buttons = AUDIO_OPTIONS.map((opt) => ({
    converter: opt.converter,
    el: choiceCard({
      title: opt.label, body: opt.body, active: false,
      onClick: () => handlers.setQuality(opt.converter),
    }),
  }));
  const body = h("div.info-card");
  body.append(
    h("dl.deflist", null,
      h("dt", null, "Requested"), requested,
      h("dt", null, "Active"), active),
    h("p.info-card__note", null,
      "Medium is recommended for most hardware and keeps the speech/AEC " +
      "band clean. Best preserves the extreme top edge of hearing but " +
      "uses more CPU. Changing this restarts music renderers briefly."),
    h("div.choice-grid", null, buttons.map((button) => button.el)),
    status,
  );
  const section = collapsible({ title: "Audio conversion", open: false, body });
  return { section, requested, active, status, buttons };
}

export function buildAudioPanel(handlers) {
  const live = livePill();
  const stream = titledCard("Current stream", { accent: true });
  const currentIssue = titledCard("Current issue");
  currentIssue.section.hidden = true;
  const issues = titledCard("Recent issues");
  const sources = titledCard("Other sources");
  const technicalBodyHost = h("div.info-card");
  const technical = collapsible({
    title: "Technical evidence", open: false, body: technicalBodyHost,
  });
  const quality = buildAudioQuality(handlers);

  const panel = h("main.app-main.audio-main", {
    "attr:data-status-view": "audio",
  },
    live.el,
    stream.section,
    currentIssue.section,
    issues.section,
    sources.section,
    technical,
    quality.section,
  );

  const refs = {
    staleness: live.label,
    stream: stream.body,
    currentIncidentSection: currentIssue.section,
    currentIncident: currentIssue.body,
    issues: issues.body,
    sourcesSection: sources.section,
    sources: sources.body,
    technical: technicalBodyHost,
    qualitySection: quality.section,
    aq: quality,
    _memo: {},
  };
  refs.panel = panel;
  // Exactly one local clock for this build-once panel. It updates the handful
  // of <time> nodes without another network request or DOM rebuild.
  window.setInterval(() => {
    if (!panel.hidden) refreshRelativeTimes(panel);
  }, 1000);
  return { panel, refs };
}

function applyCapabilities(refs, caps) {
  refs.systemCapabilities = caps || null;
  const allowed = !caps || caps.audio_quality !== false;
  refs.qualitySection.hidden = !allowed;
  refs.aq.buttons.forEach((button) => {
    if (!allowed) button.el.disabled = true;
    else if (!button.el.dataset.applying) button.el.disabled = false;
  });
}

export function updateAudio(refs, snap) {
  snap = snap || {};
  const health = snap.audio_health;
  refs.staleness.textContent = health
    ? "Audio monitor · sampled " + fmtEpochAgo(health.sampled_at)
    : "Audio monitor unavailable · retrying";

  const healthSources = health && Array.isArray(health.sources)
    ? health.sources : [];
  renderSection(refs, "stream", refs.stream, health && {
    current_stream: health.current_stream,
    session_summary: health.session_summary,
    overall: health.overall,
    signal_path: health.signal_path,
    sources: healthSources,
  }, () => health ? currentStreamBody(health) : unavailableBody());

  const incident = health ? currentIncident(health) : null;
  refs.currentIncidentSection.hidden = !incident;
  if (incident) {
    renderSection(refs, "currentIncident", refs.currentIncident, incident,
      () => currentIncidentBody(health));
  }

  renderSection(refs, "issues", refs.issues, health && {
    recent_incidents: health.recent_incidents,
    issues: health.recent_incidents ? undefined : health.issues,
    incident_window_label: health.incident_window_label,
  },
    () => health ? issuesBody(health) : h("p.audio-empty", null, "No issue history available."));

  const remainingSources = health ? otherSources(health) : [];
  refs.sourcesSection.hidden = !!health && !remainingSources.length;
  renderSection(refs, "sources", refs.sources, health && {
    current_stream: health.current_stream,
    overall: health.overall,
    sources: healthSources,
  },
    () => health ? sourcesBody(health) : h("p.audio-empty", null, "No source health available."));
  renderSection(refs, "technical", refs.technical, health && health.technical,
    () => health ? technicalBody(health) : h("p.audio-empty", null, "No technical snapshot available."));
  refreshRelativeTimes(refs.panel);

  try {
    updateAudioQuality(refs.aq, snap.audio_quality);
  } catch (e) {
    console.error("audio status: updating audio-quality failed", e);
  }
  try {
    applyCapabilities(refs, snap.system_capabilities);
  } catch (e) {
    console.error("audio status: applying capabilities failed", e);
  }
}

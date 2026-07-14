// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// audio-view.js — build-once + poll-update orchestration for /system/audio/.
// The normalized audio_health payload is the single source of truth; the view
// only composes shared cards and keeps optional failures isolated.

import { h } from "/assets/shared/js/dom.js";
import {
  header, livePill, titledCard, choiceCard, collapsible, renderSection,
} from "./components.js";
import { AUDIO_OPTIONS, updateAudioQuality } from "./sections.js";
import { fmtEpochAgo } from "./format.js";
import {
  unavailableBody, overviewBody, latencyBody, issuesBody, sourcesBody,
  technicalBody,
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
  const card = titledCard("Audio conversion");
  card.body.append(
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
  return { section: card.section, requested, active, status, buttons };
}

export function buildAudioPage(root, handlers) {
  const live = livePill();
  const overview = titledCard("General", { accent: true });
  const latency = titledCard("Latency");
  latency.section.hidden = true;
  const issues = titledCard("Recent issues");
  const sources = titledCard("Sources");
  const technicalBodyHost = h("div.info-card");
  const technical = collapsible({
    title: "Technical details", open: false, body: technicalBodyHost,
  });
  const quality = buildAudioQuality(handlers);

  root.replaceChildren(
    header({ title: "Status", backHref: "/", activeView: "audio" }),
    h("main.app-main.audio-main", null,
      live.el,
      overview.section,
      latency.section,
      issues.section,
      sources.section,
      technical,
      quality.section,
    ),
  );
  root.setAttribute("aria-busy", "false");

  return {
    staleness: live.label,
    overview: overview.body,
    latencySection: latency.section,
    latency: latency.body,
    issues: issues.body,
    sources: sources.body,
    technical: technicalBodyHost,
    qualitySection: quality.section,
    aq: quality,
    _memo: {},
  };
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
  const ageBucket = Math.floor(Date.now() / 10000);
  const overviewData = health ? {
    overall: health.overall,
    signal_path: health.signal_path,
    sources: healthSources.map((source) => ({
      id: source.id, label: source.label,
    })),
    ageBucket,
  } : null;
  renderSection(refs, "overview", refs.overview, overviewData,
    () => health ? overviewBody(health) : unavailableBody());
  renderSection(refs, "issues", refs.issues, health && {
    issues: health.issues, ageBucket,
  },
    () => health ? issuesBody(health) : h("p.audio-empty", null, "No issue history available."));
  renderSection(refs, "sources", refs.sources, health && health.sources,
    () => health ? sourcesBody(health) : h("p.audio-empty", null, "No source health available."));
  renderSection(refs, "technical", refs.technical, health && health.technical,
    () => health ? technicalBody(health) : h("p.audio-empty", null, "No technical snapshot available."));

  const latency = health && health.latency;
  const showRouteLatency = !!(
    latency && latency.applicable && latency.kind === "route_latency"
  );
  refs.latencySection.hidden = !showRouteLatency;
  if (showRouteLatency) {
    renderSection(refs, "latency", refs.latency, { latency, ageBucket },
      () => latencyBody(health));
  }

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

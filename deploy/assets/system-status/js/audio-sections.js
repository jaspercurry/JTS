// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// audio-sections.js — normalized audio-health model -> DOM.
//
// The backend owns diagnosis, classifications, presentation-ready summaries,
// and incident lifecycle. This module owns only information hierarchy: one
// current-stream snapshot, one optional current incident, a short incident
// history, compact readiness for the other sources, and raw evidence behind a
// disclosure. Missing optional facts disappear instead of becoming alarming
// "Unknown" rows.

import { h } from "/assets/shared/js/dom.js";
import { badge, defList } from "./components.js";
import { fmtEpochAgo } from "./format.js";

const STATUS_TONE = {
  ok: "ok",
  warn: "warn",
  issue: "danger",
  danger: "danger",
  unknown: "idle",
  idle: "idle",
};

function tone(status) {
  return STATUS_TONE[status] || "idle";
}

function text(value) {
  if (value == null) return "";
  const out = String(value).trim();
  return out === "—" ? "" : out;
}

function summary(value) {
  if (typeof value === "string" || typeof value === "number") return text(value);
  if (!value || typeof value !== "object") return "";
  return text(value.summary || value.headline || value.value || value.label);
}

function detail(value) {
  return value && typeof value === "object" ? text(value.detail) : "";
}

function detailRows(value) {
  const raw = value && typeof value === "object" ? value.details : null;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((row) => {
    if (Array.isArray(row) && row.length >= 2 && text(row[0]) && text(row[1])) {
      return [[text(row[0]), text(row[1])]];
    }
    if (row && typeof row === "object" && text(row.label) && text(row.value)) {
      return [[text(row.label), text(row.value)]];
    }
    return [];
  });
}

function epoch(value) {
  if (value == null || value === "") return null;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed / 1000 : null;
}

function relativeTime(value, prefix = "") {
  const at = epoch(value);
  if (at == null) return null;
  return h("time.relative-time", {
    dateTime: new Date(at * 1000).toISOString(),
    dataset: { relativeEpoch: String(at), relativePrefix: prefix },
  }, prefix + fmtEpochAgo(at));
}

// Relative time is presentation state, not new health telemetry. One tiny
// browser-side tick updates at most the visible incident/session timestamps;
// it never causes a fetch or a backend sample.
export function refreshRelativeTimes(container) {
  if (!container || typeof container.querySelectorAll !== "function") return;
  container.querySelectorAll("[data-relative-epoch]").forEach((node) => {
    const at = Number(node.dataset.relativeEpoch);
    if (!Number.isFinite(at)) return;
    node.textContent = (node.dataset.relativePrefix || "") + fmtEpochAgo(at);
  });
}

function sourceFor(health, sourceId) {
  return (health.sources || []).find((item) => item && item.id === sourceId) || null;
}

function sourceName(health, sourceId) {
  const source = sourceFor(health, sourceId);
  return source && text(source.label) ? text(source.label) : text(sourceId);
}

// A narrow compatibility seam for a rolling deploy: the new backend supplies
// current_stream. An older snapshot can still identify the active source, but
// we intentionally do not reproduce the old latency/validation warnings here.
export function currentStream(health) {
  if (health.current_stream && typeof health.current_stream === "object") {
    return health.current_stream;
  }
  const sourceId = health.overall && health.overall.active_source;
  if (!sourceId) return null;
  const source = sourceFor(health, sourceId) || {};
  return {
    source_id: sourceId,
    label: source.label || sourceId,
    signal: health.signal_path,
  };
}

function factBlock(label, value) {
  const headline = summary(value);
  const note = detail(value);
  const rows = detailRows(value);
  if (!headline && !note && !rows.length) return null;
  return h("section.stream-fact", null,
    h("h3.eyebrow", null, label),
    headline ? h("p.stream-fact__value", null, headline) : null,
    note ? h("p.stream-fact__detail", null, note) : null,
    rows.length ? defList(rows) : null,
  );
}

function sessionRollup(session) {
  if (!session || typeof session !== "object") return null;
  const headline = summary(session);
  const note = detail(session);
  const rows = detailRows(session);
  const since = relativeTime(session.started_at, "Started ");
  if (!headline && !note && !rows.length && !since) return null;
  return h("section.session-rollup", null,
    h("div.session-rollup__head", null,
      h("h3.eyebrow", null, "This session"),
      since),
    headline ? h("p.session-rollup__summary", null, headline) : null,
    note ? h("p.session-rollup__detail", null, note) : null,
    rows.length ? defList(rows) : null,
  );
}

export function unavailableBody() {
  return h("div.stream-empty", null,
    h("p.stream-empty__title", null, "Waiting for audio diagnostics"),
    h("p.info-card__note", null,
      "The audio monitor is unavailable right now. This view will retry automatically."),
  );
}

export function currentStreamBody(health) {
  const stream = currentStream(health);
  if (!stream) {
    const overall = health && health.overall;
    if (overall && overall.status === "unknown") {
      return h("div.stream-empty", null,
        h("p.stream-empty__title", null,
          text(overall.headline) || "Playback activity unavailable"),
        h("p.info-card__note", null,
          text(overall.detail) || "Waiting for a fresh audio-health sample."),
      );
    }
    return h("div.stream-empty", null,
      h("p.stream-empty__title", null, "No active stream"),
      h("p.info-card__note", null,
        "Stream quality and route diagnostics will appear when a source is active."),
    );
  }

  const sourceId = stream.source_id || stream.id;
  const sourceLabel = text(stream.label || stream.source_label) || sourceName(health, sourceId) || "Current source";
  const groups = [
    ["Audio quality", stream.quality || stream.media],
    ["Processing", stream.processing],
    ["Output", stream.output],
    ["Latency", stream.latency],
    ["Signal", stream.signal],
    ["Reliability", stream.reliability],
  ].map(([label, value]) => factBlock(label, value)).filter(Boolean);
  const session = sessionRollup(stream.session || health.session_summary);

  return h("div.current-stream", null,
    h("div.current-stream__head", null,
      h("div", null,
        h("p.eyebrow", null, "Current source"),
        h("p.current-stream__source", null, sourceLabel),
        summary(stream) && summary(stream) !== sourceLabel
          ? h("p.current-stream__summary", null, summary(stream)) : null),
    ),
    groups.length
      ? h("div.stream-facts", null, groups)
      : h("p.audio-empty", null, "Stream diagnostics are still warming up."),
    session,
  );
}

export function currentIncident(health) {
  if (health.current_incident && typeof health.current_incident === "object") {
    return health.current_incident.status === "recovered" ? null : health.current_incident;
  }
  const recent = Array.isArray(health.recent_incidents)
    ? health.recent_incidents : (Array.isArray(health.issues) ? health.issues : []);
  return recent.find((issue) => issue && issue.status === "ongoing") || null;
}

function incidentRecurrence(issue) {
  const recurrence = issue && issue.recurrence;
  if (typeof recurrence === "string") return text(recurrence);
  if (recurrence && typeof recurrence === "object") {
    const explicit = text(recurrence.summary || recurrence.label);
    if (explicit) return explicit;
  }
  const count = Number(issue && issue.count);
  return count > 1 ? `${count} occurrences` : "";
}

function incidentDuration(issue) {
  const explicit = text(issue && (issue.duration_label || issue.duration_summary));
  if (explicit && !/^0(?:\.0+)?\s*(?:ms|s|sec(?:ond)?s?)\b/i.test(explicit)) return explicit;
  const seconds = Number(issue && issue.duration_seconds);
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function incidentEvidence(issue) {
  const rows = [];
  const prose = text(issue && issue.detail);
  const push = (label, value) => {
    const out = summary(value);
    if (out && out !== prose) rows.push([label, out]);
  };
  push("Impact", issue.impact);
  push("Observed", issue.observed);
  push("Likely area", issue.likely_area);
  rows.push(...detailRows({ details: issue.evidence }));
  return rows;
}

export function currentIncidentBody(health) {
  const issue = currentIncident(health);
  if (!issue) return null;
  const recurrence = incidentRecurrence(issue);
  const meta = [relativeTime(issue.started_at, "Started ")].filter(Boolean);
  if (recurrence) meta.push(h("span", null, recurrence));
  const evidence = incidentEvidence(issue);
  const issueTone = tone(issue.severity || "warn");

  const card = h("article.current-incident", {
    style: { "--tone": `var(--status-${issueTone})` },
  },
    h("div.current-incident__head", null,
      h("p.current-incident__title", null, text(issue.title) || "Audio issue observed"),
      badge("Ongoing", issueTone)),
    text(issue.detail) ? h("p.current-incident__detail", null, text(issue.detail)) : null,
    meta.length ? h("p.current-incident__meta", null,
      meta.flatMap((item, index) => index ? [h("span", { "attr:aria-hidden": "true" }, " · "), item] : [item])) : null,
    evidence.length ? defList(evidence) : null,
  );
  return card;
}

function incidentTime(issue) {
  if (issue.status === "ongoing") return relativeTime(issue.started_at, "Started ");
  return relativeTime(issue.recovered_at || issue.last_seen_at || issue.started_at);
}

function incidentBody(issue, health) {
  const rows = incidentEvidence(issue);
  const recurrence = incidentRecurrence(issue);
  if (recurrence) rows.unshift(["Recurrence", recurrence]);
  if (issue.source_id) rows.unshift(["Source", sourceName(health, issue.source_id)]);
  const children = [];
  if (text(issue.detail)) children.push(h("p.incident-row__detail", null, text(issue.detail)));
  if (rows.length) children.push(defList(rows));
  return children;
}

function incidentRow(issue, health) {
  const when = incidentTime(issue) || h("span", null, "Recently");
  const recurrence = incidentRecurrence(issue);
  const duration = incidentDuration(issue);
  const ongoing = issue.status === "ongoing";
  const body = incidentBody(issue, health);
  const rowTone = tone(issue.severity || (ongoing ? "warn" : "idle"));
  // <summary> accepts phrasing content only, so this wrapper and every child
  // stay inline-level even though CSS lays them out as a grid.
  const summaryRow = h("span.incident-row__summary", null,
    h("span.incident-row__when", null, when),
    h("span.incident-row__title", null, text(issue.title) || "Audio issue"),
    h("span.incident-row__state", null,
      recurrence ? h("span.incident-row__recurrence", null, recurrence) : null,
      duration && !ongoing
        ? h("span.incident-row__duration", null, `Lasted ${duration}`) : null,
      ongoing ? badge("Ongoing", rowTone) : h("span.incident-row__recovered", null, "Recovered")),
    body.length ? h("span.incident-row__chevron", { "attr:aria-hidden": "true" }, "›") : null,
  );

  if (!body.length) {
    return h("article.incident-row", {
      style: { "--tone": `var(--status-${rowTone})` },
    }, summaryRow);
  }
  return h("details.incident-row", {
    style: { "--tone": `var(--status-${rowTone})` },
  },
    h("summary", null, summaryRow),
    h("div.incident-row__body", null, body),
  );
}

export function recentIncidents(health) {
  const issues = Array.isArray(health.recent_incidents)
    ? health.recent_incidents : (Array.isArray(health.issues) ? health.issues : []);
  const active = currentIncident(health);
  const activeId = active && text(active.id);
  return issues.filter((issue) => {
    if (!issue) return false;
    if (issue === active) return false;
    return !(activeId && text(issue.id) === activeId);
  }).slice(0, 5);
}

export function issuesBody(health) {
  const issues = recentIncidents(health);
  if (!issues.length) {
    const windowLabel = text(health.incident_window_label) || "recently";
    return h("p.audio-empty", null, `No audio-path incidents observed ${windowLabel}.`);
  }
  return h("div.incident-list", null, issues.map((issue) => incidentRow(issue, health)));
}

export function otherSources(health) {
  const stream = currentStream(health);
  const activeId = stream && (stream.source_id || stream.id);
  const sources = Array.isArray(health.sources) ? health.sources : [];
  return sources.filter((source) => source && source.id !== activeId);
}

export function sourcesBody(health) {
  const sources = otherSources(health);
  if (!sources.length) return h("p.audio-empty", null, "No other playback sources reported.");
  return h("div.source-readiness", null, sources.map((source) => {
    const state = text(source.headline) || text(source.state) || "Status unavailable";
    const noteworthy = source.status === "issue" || source.status === "warn";
    return h("div.source-readiness__row", null,
      h("p.source-readiness__name", null, text(source.label) || text(source.id) || "Source"),
      h("p.source-readiness__state", null, state),
      noteworthy ? badge(text(source.status_label) || "Attention", tone(source.status)) : null,
    );
  }));
}

function technicalLabel(key) {
  if (key === "airplay") return "AirPlay";
  if (key === "fanin") return "Fan-in";
  if (key === "outputd") return "Output";
  return key.replaceAll("_", " ");
}

export function technicalBody(health) {
  const technical = health.technical && typeof health.technical === "object"
    ? health.technical : {};
  const entries = Object.entries(technical).filter(([, value]) => value != null);
  if (!entries.length) {
    return h("p.audio-empty", null, "No technical evidence available.");
  }
  return h("div.technical-stack", null, entries.map(([key, value]) =>
    h("section.technical-block", null,
      h("h3.eyebrow", null, technicalLabel(key)),
      h("pre.technical-json", null, JSON.stringify(value, null, 2)),
    ),
  ));
}

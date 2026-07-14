// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// audio-sections.js — normalized audio-health model -> DOM.
//
// The backend owns every health classification, likely-cause explanation,
// latency verdict, and issue lifecycle. This module deliberately contains no
// counter thresholds or L0/L1/L2 interpretation; it only presents the stable
// semantic fields from snapshot.audio_health.

import { h } from "/assets/shared/js/dom.js";
import { badge, defList } from "./components.js";
import { fmtAgo, fmtEpochAgo } from "./format.js";

const STATUS_TONE = {
  ok: "ok",
  warn: "warn",
  issue: "danger",
  unknown: "idle",
  idle: "idle",
};

const STATUS_LABEL = {
  ok: "Healthy",
  warn: "Attention",
  issue: "Issue",
  unknown: "Unknown",
  idle: "Idle",
};

function tone(status) {
  return STATUS_TONE[status] || "idle";
}

function statusLabel(status) {
  return STATUS_LABEL[status] || "Unknown";
}

function sourceLabel(health, sourceId) {
  if (!sourceId) return "None";
  const source = (health.sources || []).find((item) => item && item.id === sourceId);
  return source && source.label ? source.label : sourceId;
}

function statusLine(status, headline) {
  return h("div.badge-row", null,
    badge(statusLabel(status), tone(status)),
    headline ? h("span", null, headline) : null,
  );
}

export function unavailableBody() {
  return h("div.audio-overview", null,
    statusLine("unknown", "Audio health unavailable"),
    h("p.audio-overview__headline", null, "Waiting for the audio monitor"),
    h("p.info-card__note", null,
      "The rest of the status dashboard is still available. This view will retry automatically."),
  );
}

export function overviewBody(health) {
  const overall = health.overall || {};
  const path = health.signal_path || {};
  const activeSource = sourceLabel(health, overall.active_source);
  const rows = [
    ["Active source", activeSource],
    ["Signal path", h("span.inline-status", null,
      badge(statusLabel(path.status), tone(path.status)),
      h("span", null, path.headline || "No path summary"))],
  ];
  if (overall.since != null) rows.push(["Since", fmtEpochAgo(overall.since)]);

  return h("div.audio-overview", null,
    statusLine(overall.status),
    h("p.audio-overview__headline", null, overall.headline || "Audio status unavailable"),
    overall.detail ? h("p.audio-overview__detail", null, overall.detail) : null,
    defList(rows),
    path.detail ? h("p.info-card__hint", null, path.detail) : null,
  );
}

function verificationText(verification) {
  if (!verification) return "No verification evidence";
  const label = String(verification.status || "unverified").replaceAll("_", " ");
  const when = verification.validated_at ? fmtAgo(verification.validated_at) : "";
  return label.charAt(0).toUpperCase() + label.slice(1) + (when !== "—" && when ? " · " + when : "");
}

function measuredLatency(verification) {
  if (!verification) return null;
  const parts = [];
  if (verification.p95_ms != null) parts.push("p95 " + Number(verification.p95_ms).toFixed(1) + " ms");
  if (verification.p99_ms != null) parts.push("p99 " + Number(verification.p99_ms).toFixed(1) + " ms");
  return parts.length ? parts.join(" · ") : null;
}

export function latencyBody(health) {
  const latency = health.latency || {};
  const measured = measuredLatency(latency.verification);
  const rows = [
    ["Source", sourceLabel(health, latency.source_id)],
    ["Evidence", verificationText(latency.verification)],
  ];
  if (measured) rows.push(["Measured", measured]);

  return h("div.timing-summary", null,
    statusLine(latency.status, latency.headline),
    latency.detail ? h("p.info-card__note", null, latency.detail) : null,
    defList(rows),
  );
}

function issueWhen(issue) {
  if (issue.status === "ongoing") {
    return issue.started_at == null ? "Ongoing" : "Started " + fmtEpochAgo(issue.started_at);
  }
  const at = issue.recovered_at == null ? issue.last_seen_at : issue.recovered_at;
  return at == null ? "Recovered" : "Recovered " + fmtEpochAgo(at);
}

export function issuesBody(health) {
  const issues = Array.isArray(health.issues) ? health.issues : [];
  if (!issues.length) {
    return h("p.audio-empty", null, "No recent audio issues.");
  }
  return h("div.issue-list", null, issues.map((issue) => {
    const ongoing = issue.status === "ongoing";
    const meta = [issueWhen(issue)];
    if (issue.source_id) meta.push(sourceLabel(health, issue.source_id));
    if (Number(issue.count) > 1) meta.push("repeated " + issue.count + " times");
    const issueTone = tone(issue.severity);
    return h("article.issue-row", {
      class: ongoing ? "is-ongoing" : "is-recovered",
      style: { "--tone": `var(--status-${issueTone})` },
    },
      h("div.issue-row__head", null,
        h("p.issue-row__title", null, issue.title || "Audio issue"),
        badge(ongoing ? "Ongoing" : "Recovered", ongoing ? tone(issue.severity) : "idle")),
      issue.detail ? h("p.issue-row__detail", null, issue.detail) : null,
      h("p.issue-row__meta", null, meta.join(" · ")),
    );
  }));
}

function sourceTiming(timing) {
  if (!timing || timing.applicable === false) return null;
  return h("div.source-card__timing", null,
    h("p.eyebrow", null, timing.kind === "sync" ? "Sync timing" : "Timing"),
    h("div.inline-status", null,
      badge(statusLabel(timing.status), tone(timing.status)),
      h("span", null, timing.headline || "Timing status unavailable")),
    timing.detail ? h("p.source-card__detail", null, timing.detail) : null,
  );
}

export function sourcesBody(health) {
  const sources = Array.isArray(health.sources) ? health.sources : [];
  if (!sources.length) {
    return h("p.audio-empty", null, "No playback sources reported.");
  }
  return h("div.source-grid", null, sources.map((source) => {
    const badgeText = source.state === "active" && source.status === "ok"
      ? "Active" : statusLabel(source.status);
    return h("article.source-card", null,
      h("div.source-card__head", null,
        h("h3.source-card__title", null, source.label || source.id || "Source"),
        badge(badgeText, tone(source.status))),
      h("p.source-card__headline", null, source.headline || "Status unavailable"),
      source.detail ? h("p.source-card__detail", null, source.detail) : null,
      sourceTiming(source.timing),
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
    return h("p.audio-empty", null, "No technical snapshot available.");
  }
  return h("div.technical-stack", null, entries.map(([key, value]) =>
    h("section.technical-block", null,
      h("h3.eyebrow", null, technicalLabel(key)),
      h("pre.technical-json", null, JSON.stringify(value, null, 2)),
    ),
  ));
}

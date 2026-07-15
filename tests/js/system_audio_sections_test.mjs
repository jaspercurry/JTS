// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Executes the audio renderer against a tiny structural DOM seam. This pins
// the information hierarchy and fail-soft optional-field contract without a
// browser dependency; layout remains covered by the static CSS guards.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const modulePath = process.argv[2];
if (!modulePath) throw new Error("usage: node system_audio_sections_test.mjs <audio-sections.js>");

const source = readFileSync(modulePath, "utf8")
  .replace(/^import[\s\S]*?;\n/gm, "")
  .replace(/^export /gm, "");
assert.doesNotMatch(source, /^\s*(?:import|export)\s/m);

function flatten(items) {
  return items.flatMap((item) => Array.isArray(item) ? flatten(item) : [item]);
}

function h(tag, props, ...children) {
  return {
    tag,
    props: props || {},
    dataset: (props && props.dataset) || {},
    children: flatten(children).filter((child) => child != null && child !== false),
    textContent: "",
  };
}
const badge = (label, badgeTone) => h("badge", { badgeTone }, label);
const defList = (rows) => h("deflist", null,
  rows.map(([label, value]) => h("row", null, label, value)));
const fmtEpochAgo = (at) => `${Math.max(0, 1000 - Number(at))}s ago`;

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction("h", "badge", "defList", "fmtEpochAgo", `${source}\nreturn {
  currentStreamBody, currentIncidentBody, recentIncidents, issuesBody,
  otherSources, sourcesBody, refreshRelativeTimes,
};`);
const api = await run(h, badge, defList, fmtEpochAgo);

function strings(node) {
  if (node == null) return [];
  if (typeof node === "string" || typeof node === "number") return [String(node)];
  return flatten(node.children || []).flatMap(strings);
}

const current = {
  source_id: "usbsink",
  label: "USB Audio",
  media: { summary: "48 kHz PCM · shared path" },
  latency: { status: "ok", summary: "Lowest-latency route", detail: "Clock stable." },
  reliability: { status: "ok", summary: "No drops or underruns" },
  session: {
    started_at: 900,
    summary: "0 dropouts · 2 brief clock fallbacks",
    details: [{ label: "Total fallback", value: "18 seconds" }],
  },
};
const ongoing = {
  id: "clock-1", status: "ongoing", severity: "warn",
  title: "USB latency increased", started_at: 950, count: 3,
  duration_label: "50s",
  recurrence: { summary: "3 occurrences in 30 minutes" },
  impact: "Audio continues with higher latency.",
  observed: "USB clocking moved to fallback.",
  evidence: [{ label: "Clock mode", value: "l2_fallback" }],
};
const recovered = Array.from({ length: 6 }, (_, index) => ({
  id: `recovered-${index}`, status: "recovered", severity: "warn",
  title: `Recovered issue ${index}`, recovered_at: 940 - index,
  duration_seconds: index ? undefined : 2.4,
  detail: "A bounded freeze-frame was captured.",
}));
recovered[0].recurrence = { summary: "At least 3 occurrences" };
recovered[1].duration_label = "0 ms";
const health = {
  current_stream: current,
  current_incident: ongoing,
  recent_incidents: [ongoing, ...recovered],
  sources: [
    { id: "usbsink", label: "USB Audio", headline: "Playing", status: "ok" },
    { id: "airplay", label: "AirPlay", headline: "Ready", status: "ok" },
    { id: "spotify", label: "Spotify", headline: "Unavailable", status: "issue" },
  ],
};

const streamText = strings(api.currentStreamBody(health)).join(" | ");
assert.match(streamText, /USB Audio/);
assert.match(streamText, /48 kHz PCM · shared path/);
assert.match(streamText, /Lowest-latency route/);
assert.match(streamText, /0 dropouts · 2 brief clock fallbacks/);
assert.doesNotMatch(streamText, /Processing|Output|Signal|Unknown/,
  "absent diagnostic groups are omitted rather than filled with noise");

const unknownStreamText = strings(api.currentStreamBody({
  overall: {
    status: "unknown",
    headline: "Playback activity unavailable",
    detail: "JTS could not read the mux's canonical source state.",
  },
})).join(" | ");
assert.match(unknownStreamText, /Playback activity unavailable/);
assert.match(unknownStreamText, /canonical source state/);
assert.doesNotMatch(unknownStreamText, /No active stream/,
  "missing activity truth never renders as confident idle");

const issueText = strings(api.currentIncidentBody(health)).join(" | ");
assert.match(issueText, /USB latency increased/);
assert.match(issueText, /3 occurrences in 30 minutes/);
assert.match(issueText, /Audio continues with higher latency/);
assert.match(issueText, /Clock mode \| Stable fallback/);
assert.doesNotMatch(issueText, /l2_fallback/,
  "primary incident evidence translates internal clock modes for households");
for (const [rawMode, householdLabel] of [
  ["l0_locked", "Low latency stable"],
  ["l1_warn", "Clock adjusting"],
  ["l2_fallback", "Stable fallback"],
  ["probing", "Timing check in progress"],
  ["disabled", "Standard buffering"],
]) {
  const translated = strings(api.currentIncidentBody({
    ...health,
    current_incident: {
      ...ongoing,
      evidence: [{ label: "Clock mode", value: rawMode }],
    },
  })).join(" | ");
  assert.match(translated, new RegExp(`Clock mode \\| ${householdLabel}`));
  assert.doesNotMatch(translated, new RegExp(rawMode));
}
assert.doesNotMatch(issueText, /50s so far/,
  "current issue age is stated once by its live Started timestamp");

const recent = api.recentIncidents(health);
assert.equal(recent.length, 5, "history is bounded to five rows");
assert.ok(recent.every((issue) => issue.id !== ongoing.id),
  "the detailed current incident is not repeated in history");
assert.equal(api.otherSources(health).length, 2, "active source is omitted from readiness");
const sourceText = strings(api.sourcesBody(health)).join(" | ");
assert.doesNotMatch(sourceText, /USB Audio/);
assert.match(sourceText, /AirPlay | Ready/);
assert.match(sourceText, /Spotify | Unavailable | Attention/);
const historyText = strings(api.issuesBody(health)).join(" | ");
assert.match(historyText, /Lasted 2s/,
  "backend-supplied incident duration has a compact history seam");
assert.match(historyText, /Recurrence | At least 3 occurrences/,
  "recurrence remains available inside the row disclosure on narrow screens");
assert.doesNotMatch(historyText, /Lasted 0 ms/,
  "an unobserved point event does not claim a zero-length duration");

const timeNodes = [
  { dataset: { relativeEpoch: "995", relativePrefix: "Started " }, textContent: "old" },
  { dataset: { relativeEpoch: "bad", relativePrefix: "" }, textContent: "unchanged" },
];
api.refreshRelativeTimes({ querySelectorAll: () => timeNodes });
assert.equal(timeNodes[0].textContent, "Started 5s ago");
assert.equal(timeNodes[1].textContent, "unchanged");

process.stdout.write(JSON.stringify({ ok: true }));

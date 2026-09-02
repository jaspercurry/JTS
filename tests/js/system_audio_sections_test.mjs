// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Executes the audio renderer against a tiny structural DOM seam. This pins
// the information hierarchy and fail-soft optional-field contract without a
// browser dependency; layout remains covered by the static CSS guards.

import assert from "node:assert/strict";
import { buildFunction } from "./_loader.mjs";

const modulePath = process.argv[2];
if (!modulePath) throw new Error("usage: node system_audio_sections_test.mjs <audio-sections.js>");

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

const api = await buildFunction(modulePath, {
  rewrite: [[/^import[\s\S]*?;\n/gm, ""], [/^export /gm, ""]],
  guardNoImports: true,
  async: true,
  params: ["h", "badge", "defList", "fmtEpochAgo"],
  returns: [
    "currentStreamBody", "recentIncidents", "issuesBody",
    "otherSources", "sourcesBody", "refreshRelativeTimes",
    "outputAlert", "outputAlertBody", "usbSourceOff",
  ],
})(h, badge, defList, fmtEpochAgo);

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
assert.doesNotMatch(streamText, /0 dropouts · 2 brief clock fallbacks/,
  "the current-stream card does not duplicate the session trend summary");
assert.doesNotMatch(streamText, /Processing|Output|Signal|Unknown/,
  "absent diagnostic groups are omitted rather than filled with noise");

const unknownStreamText = strings(api.currentStreamBody({
  overall: {
    status: "unknown",
    headline: "Playback activity unavailable",
    detail: "JTS cannot tell which source is playing right now.",
  },
})).join(" | ");
assert.match(unknownStreamText, /Playback activity unavailable/);
assert.match(unknownStreamText, /which source is playing/);
assert.doesNotMatch(unknownStreamText, /No active stream/,
  "missing activity truth never renders as confident idle");

// #2381: a parked speaker emits nothing while every daemon looks healthy. With
// no source selected there is no stream to describe, and "No active stream"
// read as idle-and-fine — the household's only two audio surfaces both said
// nothing was wrong. Both now carry the backend's own parked sentence.
const PARKED = {
  status: "issue",
  headline: "Sound cannot come out of the speaker",
  detail:
    "InnoMaker HiFi AMP Pro cannot drive an active speaker layout, so " +
    "nothing can play. Choose a passive speaker layout at /sound/setup/ " +
    "(passive sends full-range to every output; requires a built-in passive " +
    "crossover) or attach an active-capable DAC.",
  active_source: null,
};
const parkedStreamText = strings(api.currentStreamBody({ overall: PARKED })).join(" | ");
assert.match(parkedStreamText, /Sound cannot come out/);
assert.match(parkedStreamText, /\/sound\/setup\//);
assert.doesNotMatch(parkedStreamText, /No active stream/,
  "a speaker that cannot reach its drivers never renders as confident idle");

const parkedAlert = api.outputAlert({ overall: PARKED });
assert.ok(parkedAlert, "a broken signal path raises the System-view audio alert");
assert.equal(parkedAlert.headline, PARKED.headline,
  "the alert carries the backend's sentence verbatim — it composes none of its own");
const parkedAlertNode = api.outputAlertBody(parkedAlert);
const parkedAlertText = strings(parkedAlertNode).join(" | ");
assert.match(parkedAlertText, /Sound cannot come out/);
assert.match(parkedAlertText, /InnoMaker HiFi AMP Pro/);
assert.match(parkedAlertText, /Needs attention/);
assert.equal(parkedAlertNode.props.style["--tone"], "var(--status-danger)",
  "the alert is toned danger, not the warn default of the shared incident block");

// The alert is hidden for every state that is not "the path cannot carry
// audio". A warn/unknown/idle front-page alarm would train the household to
// ignore the one that matters, and an ok box must show no card at all.
for (const status of ["ok", "warn", "idle", "unknown", "recovered", ""]) {
  assert.equal(api.outputAlert({ overall: { ...PARKED, status } }), null,
    `overall.status=${status || "(empty)"} must not raise the audio alert`);
}
assert.equal(api.outputAlert({}), null, "a snapshot without overall raises nothing");
assert.equal(api.outputAlert(null), null, "a missing audio_health raises nothing");
assert.equal(api.outputAlert({ overall: { status: "issue", headline: "  " } }), null,
  "an issue the backend has no words for renders no empty red card");
assert.equal(
  api.outputAlert({
    ...health,
    overall: { status: "warn", headline: "Audio is playing", detail: "USB latency increased." },
  }),
  null,
  "a playing speaker with a warn-level incident keeps the front page quiet");

const issueText = strings(api.issuesBody(health)).join(" | ");
assert.match(issueText, /This session/);
assert.match(issueText, /0 dropouts · 2 brief clock fallbacks/);
assert.match(issueText, /USB latency increased/);
assert.match(issueText, /3 occurrences in 30 minutes/);
assert.match(issueText, /Audio continues with higher latency/);
assert.match(issueText, /Clock mode \| Stable fallback/);
assert.doesNotMatch(issueText, /l2_fallback/,
  "incident evidence translates internal clock modes for households");
for (const [rawMode, householdLabel] of [
  ["l0_locked", "Low latency stable"],
  ["l1_warn", "Clock adjusting"],
  ["l2_fallback", "Stable fallback"],
  ["probing", "Timing check in progress"],
  ["disabled", "Standard buffering"],
]) {
  const translated = strings(api.issuesBody({
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
assert.equal(recent[0].id, ongoing.id,
  "the ongoing incident is the first trend row");
assert.equal(recent.filter((issue) => issue.id === ongoing.id).length, 1,
  "the ongoing incident appears once when current and recent data overlap");
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

// --- the USB cards follow the household intent ------------------------------
for (const [expected, probe] of [
  [true, { sources: [{ id: "usbsink", state: "off" }] }],
  [false, undefined],
  [false, {}],
  [false, { sources: [{ id: "usbsink", state: "unavailable" }] }],
  [false, { sources: [{ id: "usbsink", state: "ready" }] }],
  [false, { sources: [{ id: "airplay", state: "off" }] }],
]) {
  assert.equal(api.usbSourceOff(probe), expected,
    `usbSourceOff(${JSON.stringify(probe)})`);
}

process.stdout.write(JSON.stringify({ ok: true }));

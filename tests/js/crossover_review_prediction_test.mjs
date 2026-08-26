// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// The review screen's THIRD curve — the predicted response (two-stage
// commission work order D3, issue #1806; PR-T2).
//
// Two modules are pinned here because the honesty property is split across
// them. cloud.js decides WHAT reaches the chart (which spec source frames the
// corridor, which reference each curve is plotted against, which legend
// swatches may claim a series); chart.js decides HOW the predicted curve is
// drawn (dashed, because it is a model rather than a measurement, and voting
// in the y-domain so an overshoot is visible rather than clipped).
//
// Unlike crossover_cloud_callouts_test.mjs — which stubs the chart out
// entirely because "CI cannot see pixels" — this harness drives the real
// drawCloudChart through a recording 2D-context double. That does not verify
// pixels either, but it does verify the DRAW CALLS: how many curves were
// stroked, in what colour, and with which dash pattern. Those are the facts
// the review screen's honesty rests on, and they were otherwise unpinned.
//
//   node tests/js/crossover_review_prediction_test.mjs

import assert from "node:assert/strict";
import { loadEsm, repoPath } from "./_loader.mjs";
import { FakeElement } from "./_dom.mjs";

let passed = 0;
function check(condition, message, context) {
  assert.ok(condition, context ? `${message} — got ${JSON.stringify(context)}` : message);
  passed += 1;
}

// --------------------------------------------------------------------------
// Part 1 — cloud.js: what reaches the chart
// --------------------------------------------------------------------------

globalThis.document = { createElement: (tag) => new FakeElement(tag) };

const els = {
  cloud: new FakeElement("crossover-cloud"),
  cloudProvenance: new FakeElement("crossover-cloud-provenance"),
  cloudChart: new FakeElement("crossover-cloud-chart"),
  cloudGeometry: new FakeElement("crossover-cloud-geometry"),
  cloudCallouts: new FakeElement("crossover-cloud-callouts"),
  cloudPending: new FakeElement("crossover-cloud-pending"),
  legendMeasure: new FakeElement("crossover-chart-legend-measure"),
  legendVerify: new FakeElement("crossover-chart-legend-verify"),
  legendPredicted: new FakeElement("crossover-chart-legend-predicted"),
  legendCorridor: new FakeElement("crossover-chart-legend-corridor"),
  legendExcluded: new FakeElement("crossover-chart-legend-excluded"),
};

let lastChartPayload = null;
globalThis.__drawCloudChart = (canvas, payload) => { lastChartPayload = payload; };

const { renderCloud } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/cloud.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: "const drawCloudChart = globalThis.__drawCloudChart;\n",
  },
);

const CLOUD_MEASURE = "cloud_measure";
const CLOUD_VERIFY = "cloud_verify";

const SPEC_BANDS = [
  { f_lo_hz: 250, f_hi_hz: 500, passed: false, max_deviation_db: -9.04, tolerance_db: 3 },
  { f_lo_hz: 500, f_hi_hz: 2000, passed: true, max_deviation_db: 1.1, tolerance_db: 3 },
];

// The review envelope: a pre-apply cloud, a prediction, and NO cloud_verify —
// nothing has been applied, so the post-apply group does not exist yet.
const reviewEnvelope = {
  tier: "full",
  cloud: {
    [CLOUD_MEASURE]: { reference_db: -27.3, spec_bands: SPEC_BANDS, carve_outs: [] },
  },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28] } },
  },
  prediction: {
    curve: { freqs_hz: [300, 1000], magnitude_db: [-30.1, -30.4] },
    spec_bands: SPEC_BANDS,
    overall_passed: false,
    reference_db: -30.2,
  },
};

renderCloud(els, reviewEnvelope);

check(els.cloud.hidden === false, "review: the section is visible");
check(
  lastChartPayload.predictedCurve.freqs_hz.length === 2,
  "review: the predicted curve reaches the chart payload",
);
check(
  lastChartPayload.predictedReferenceDb === -30.2,
  "review: the predicted curve carries its OWN reference from the stored spec " +
  "report, never the measured phase's (review B-1's per-curve rule)",
  { got: lastChartPayload.predictedReferenceDb },
);
check(
  lastChartPayload.measureReferenceDb === -27.3,
  "review: the measured curve keeps its own reference alongside it",
);
// THE Full-tier trap: specSourceFor() reads CLOUD_VERIFY for Full everywhere
// else, and on this screen CLOUD_VERIFY does not exist. Without D3.1's
// pre-apply source the corridor would be empty on exactly the screen whose
// job is to show whether the prediction clears it.
check(
  lastChartPayload.specBands.length === 2,
  "review at FULL tier: the tolerance corridor comes from the pre-apply cloud " +
  "(D3.1), not from a cloud_verify that has not been walked yet",
  { got: lastChartPayload.specBands },
);
check(
  els.legendPredicted.hidden === false,
  "review: the predicted swatch is shown",
);
check(
  els.legendVerify.hidden === true,
  "review: the 'After correction' swatch stays hidden — nothing was applied, " +
  "so no measured after-curve exists",
);
check(
  els.cloudPending.hidden === true,
  "review: the 'after-correction curve is still coming' caption is suppressed " +
  "— on this screen the household has not decided yet, so neither that " +
  "sentence nor express's 'there is no after curve' would be true",
);

// An UNGRADEABLE prediction (state 2: a curve with no stored report) has no
// reference to plot against. It must not claim a series it cannot draw.
renderCloud(els, {
  ...reviewEnvelope,
  prediction: {
    curve: { freqs_hz: [300, 1000], magnitude_db: [-30.1, -30.4] },
    spec_bands: [],
    overall_passed: null,
    reference_db: null,
  },
});
check(
  els.legendPredicted.hidden === true,
  "ungradeable prediction: the swatch is hidden — the curve cannot be plotted " +
  "without a reference, and a legend must describe what is on the canvas",
);

// The refusal lane (state 4: a report with no curve) draws nothing new, and
// with no measured curve either the whole section stays hidden.
renderCloud(els, {
  tier: "full",
  cloud: null,
  cloud_chart: null,
  prediction: {
    curve: null, spec_bands: SPEC_BANDS, overall_passed: false, reference_db: -30.2,
  },
});
check(
  els.cloud.hidden === true,
  "refusal lane (report, no curve) with nothing else measured: section hidden, " +
  "never an empty chart frame",
);

// Every screen that is NOT the review one sends no prediction at all, so the
// shipped before/after chart is untouched.
renderCloud(els, {
  tier: "full",
  cloud: { [CLOUD_VERIFY]: { reference_db: -30.0, spec_bands: SPEC_BANDS, carve_outs: [] } },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28] } },
    [CLOUD_VERIFY]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-30, -30] } },
  },
});
check(
  lastChartPayload.predictedCurve === null && els.legendPredicted.hidden === true,
  "done screen (no prediction key): no third curve, no third swatch — the " +
  "shipped chart is byte-identical to before this change",
);
check(
  els.legendVerify.hidden === false && els.cloudPending.hidden === true,
  "done screen: the measured after-curve and its swatch still render normally",
);

// --------------------------------------------------------------------------
// Part 2 — chart.js: how the predicted curve is drawn
// --------------------------------------------------------------------------

// A recording 2D context. Only the calls this assertion set reads are
// captured; everything else is a no-op so the real draw path runs unmodified.
function recordingContext() {
  const strokes = [];
  let dash = [];
  let style = "";
  return {
    strokes,
    setTransform() {}, scale() {}, clearRect() {}, save() {}, restore() {},
    beginPath() {}, moveTo() {}, lineTo() {}, rect() {}, clip() {},
    fillRect() {}, fillText() {},
    setLineDash(value) { dash = value.slice(); },
    stroke() { strokes.push({ style, dash: dash.slice() }); },
    set strokeStyle(value) { style = value; },
    get strokeStyle() { return style; },
    set fillStyle(_v) {}, get fillStyle() { return ""; },
    set lineWidth(_v) {}, get lineWidth() { return 1; },
    set font(_v) {}, get font() { return ""; },
    set globalAlpha(_v) {}, get globalAlpha() { return 1; },
  };
}

const COLORS = {
  "--crossover-chart-measure": "MEASURE",
  "--crossover-chart-verify": "VERIFY",
  "--crossover-chart-predicted": "PREDICTED",
  "--crossover-chart-corridor": "CORRIDOR",
  "--crossover-chart-excluded": "EXCLUDED",
  "--border-strong": "GRID",
  "--muted": "TEXT",
};
globalThis.getComputedStyle = () => ({
  getPropertyValue: (name) => COLORS[name] || "",
});
globalThis.window = { devicePixelRatio: 1 };

const { drawCloudChart } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/chart.js"),
  { rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]] },
);

function drawWith(payload) {
  const ctx = recordingContext();
  const canvas = {
    getBoundingClientRect: () => ({ width: 640, height: 240 }),
    getContext: () => ctx,
    width: 0,
    height: 0,
  };
  const drew = drawCloudChart(canvas, payload);
  return { drew, ctx };
}

const curve = (db) => ({ freqs_hz: [300, 700, 1000], magnitude_db: [db, db, db] });

{
  const { drew, ctx } = drawWith({
    measureCurve: curve(-26),
    verifyCurve: null,
    predictedCurve: curve(-30.1),
    measureReferenceDb: -27.3,
    verifyReferenceDb: null,
    predictedReferenceDb: -30.2,
    specBands: SPEC_BANDS,
    excludedIntervals: [],
  });
  check(drew === true, "chart: a measured + predicted payload draws");
  const curves = ctx.strokes.filter((s) => s.style === "MEASURE" || s.style === "PREDICTED");
  check(curves.length === 2, "chart: both the measured and predicted curves are stroked",
    { strokes: ctx.strokes.map((s) => s.style) });
  const predicted = ctx.strokes.find((s) => s.style === "PREDICTED");
  check(
    predicted && predicted.dash.length === 2 && predicted.dash[0] === 6,
    "chart: the PREDICTED curve is stroked DASHED — it is a model, and that is " +
    "the one difference that survives a greyscale screenshot",
    { got: predicted },
  );
  const measured = ctx.strokes.find((s) => s.style === "MEASURE");
  check(
    measured && measured.dash.length === 0,
    "chart: the measured curve stays SOLID — the dash never leaks onto a " +
    "measured series",
    { got: measured },
  );
}

{
  // A predicted curve with no reference contributes no points and draws
  // nothing, rather than being plotted against someone else's frame.
  const { ctx } = drawWith({
    measureCurve: curve(-26),
    verifyCurve: null,
    predictedCurve: curve(-30.1),
    measureReferenceDb: -27.3,
    verifyReferenceDb: null,
    predictedReferenceDb: null,
    specBands: SPEC_BANDS,
    excludedIntervals: [],
  });
  check(
    !ctx.strokes.some((s) => s.style === "PREDICTED"),
    "chart: an ungradeable prediction (no reference) is NOT drawn — plotting " +
    "it against another phase's reference would invent the one frame this " +
    "screen exists to state truthfully",
  );
}

{
  // A prediction alone is enough to draw: the refusal lane aside, a session
  // whose measured curve is missing must still be able to show what was
  // predicted rather than returning "nothing to draw".
  const { drew } = drawWith({
    measureCurve: null,
    verifyCurve: null,
    predictedCurve: curve(-30.1),
    measureReferenceDb: null,
    verifyReferenceDb: null,
    predictedReferenceDb: -30.2,
    specBands: SPEC_BANDS,
    excludedIntervals: [],
  });
  check(drew === true, "chart: a predicted curve alone is drawable");
}

{
  // The y-domain must contain the prediction's overshoot. A prediction that
  // misses the corridor by 6 dB has to be VISIBLE missing it — that overshoot
  // is the number the verdict copy names in words.
  const { ctx } = drawWith({
    measureCurve: curve(-26),
    verifyCurve: null,
    predictedCurve: { freqs_hz: [300, 700], magnitude_db: [-40.2, -40.2] },
    measureReferenceDb: -27.3,
    verifyReferenceDb: null,
    predictedReferenceDb: -30.2,   // a -10 dB deviation, well past ±3 dB
    specBands: SPEC_BANDS,
    excludedIntervals: [],
  });
  check(
    ctx.strokes.some((s) => s.style === "PREDICTED"),
    "chart: a prediction that overshoots the corridor is still stroked (it " +
    "votes in the y-domain rather than being clipped flat against the edge)",
  );
}

console.log(JSON.stringify({ ok: true, passed }));

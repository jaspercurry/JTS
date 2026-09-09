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
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28], display: { deviation_db: [0.1, -0.2], untrusted_intervals_hz: [] } } },
  },
  prediction: {
    curve: { freqs_hz: [300, 1000], magnitude_db: [-30.1, -30.4], display: { deviation_db: [0.1, -0.2], untrusted_intervals_hz: [] } },
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
    curve: { freqs_hz: [300, 1000], magnitude_db: [-30.1, -30.4], display: { deviation_db: [null, null], untrusted_intervals_hz: [] } },
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
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28], display: { deviation_db: [0.1, -0.2], untrusted_intervals_hz: [] } } },
    [CLOUD_VERIFY]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-30, -30], display: { deviation_db: [0.1, -0.2], untrusted_intervals_hz: [] } } },
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
  const fills = [];
  const labels = [];
  let dash = [];
  let style = "";
  let fillStyle = "";
  let alpha = 1;
  let path = [];
  return {
    strokes, fills, labels,
    setTransform() {}, scale() {}, clearRect() {}, save() {}, restore() {},
    beginPath() { path = []; },
    moveTo(x, y) { path.push({ op: "move", x, y }); },
    lineTo(x, y) { path.push({ op: "line", x, y }); },
    rect(x, y, width, height) { path.push({ x, y, width, height }); },
    clip() {},
    fill() { fills.push(...path.map((rect) => ({ style: fillStyle, alpha, ...rect }))); },
    fillRect(x, y, width, height) {
      fills.push({ style: fillStyle, alpha, x, y, width, height });
    },
    fillText(text, x, y) { labels.push({ text, x, y }); },
    measureText(text) { return { width: text.length * 6 }; },
    setLineDash(value) { dash = value.slice(); },
    stroke() { strokes.push({ style, dash: dash.slice(), path: path.slice() }); },
    set strokeStyle(value) { style = value; },
    get strokeStyle() { return style; },
    set fillStyle(value) { fillStyle = value; }, get fillStyle() { return fillStyle; },
    set lineWidth(_v) {}, get lineWidth() { return 1; },
    set font(_v) {}, get font() { return ""; },
    set globalAlpha(value) { alpha = value; }, get globalAlpha() { return alpha; },
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

const { cssColor, drawFrequencyChart } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/frequency-chart.js"),
);
globalThis.__cssColor = cssColor;
globalThis.__drawFrequencyChart = drawFrequencyChart;

const { drawCloudChart } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/chart.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: "const cssColor = globalThis.__cssColor; const drawFrequencyChart = globalThis.__drawFrequencyChart;\n",
  },
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

function drawGeneric(payload, width = 640) {
  const ctx = recordingContext();
  const canvas = {
    getBoundingClientRect: () => ({ width, height: 240 }),
    getContext: () => ctx,
    width: 0,
    height: 0,
  };
  const drew = drawFrequencyChart(canvas, payload);
  return { drew, ctx };
}

const curve = (deviation, untrusted = []) => ({
  freqs_hz: [300, 700, 1000],
  display: { deviation_db: [deviation, deviation, deviation], untrusted_intervals_hz: untrusted },
});

for (const width of [240, 320, 640]) {
  const { ctx } = drawGeneric({ series: [{ curve: curve(0) }] }, width);
  const labels = ctx.labels.filter((label) => label.y === 232);
  check(labels.length >= 2 && labels.every((label, index) =>
    label.x >= 0 && label.x + ctx.measureText(label.text).width <= width &&
    (index === 0 || label.x >= labels[index - 1].x + ctx.measureText(labels[index - 1].text).width + 6)),
  'frequency labels stay inside the canvas without overlapping on narrow screens');
}

{
  const { drew, ctx } = drawWith({
    measureCurve: curve(1.3), predictedCurve: curve(0.1), specBands: SPEC_BANDS,
  });
  check(drew, "prepared measured and predicted curves draw");
  const predicted = ctx.strokes.find((s) => s.style === "PREDICTED");
  const measured = ctx.strokes.find((s) => s.style === "MEASURE");
  check(predicted.dash[0] === 6 && measured.dash.length === 0, "only the model is dashed");
  check(drawWith({ predictedCurve: curve(0.1) }).drew, "prediction alone draws");
  check(!drawWith({ predictedCurve: curve(null) }).drew, "missing reference supplies no drawable points");
  const missing = drawWith({ measureCurve: curve(1), predictedCurve: curve(null) });
  check(missing.drew && !missing.ctx.strokes.some((s) => s.style === "PREDICTED"), "a missing prediction reference keeps the measured curve visible");
  check(drawWith({ predictedCurve: curve(-10), specBands: SPEC_BANDS }).ctx.strokes.some((s) => s.style === "PREDICTED"), "out-of-spec predictions remain visible");
}

{
  const base = { freqs_hz: [300, 700], display: { deviation_db: [0, 1] } };
  const extreme = { freqs_hz: [300, 700, 19900], display: { deviation_db: [0, 1, -73] } };
  const clean = drawWith({ measureCurve: base, specBands: SPEC_BANDS });
  const wide = drawWith({ measureCurve: extreme, specBands: SPEC_BANDS });
  const path = (result) => result.ctx.strokes.find((s) => s.style === "MEASURE").path;
  check(path(clean)[0].y === path(wide)[0].y, "ungraded extremes do not change graded-range scaling");
  const gap = drawWith({ measureCurve: { freqs_hz: [300, 700, 1000], display: { deviation_db: [1, null, 2] } } });
  check(path(gap).map((p) => p.op).join() === "move,move", "invalid bins break the line instead of joining across missing data");
}

{
  const { ctx } = drawWith({ measureCurve: curve(1, [[1, 10], [500, 700], [30000, 40000]]) });
  check(ctx.fills.filter((f) => f.style === "EXCLUDED" && f.width > 0).length === 1,
    "only in-range untrusted areas are shaded");
}

{
  const visible = {
    curve: { freqs_hz: [300, 700], display: { deviation_db: [0, 1] } },
    color: "VISIBLE",
    draw: true,
  };
  const hidden = {
    curve: { freqs_hz: [300, 700], display: { deviation_db: [-20, -20] } },
    color: "HIDDEN",
    draw: false,
  };
  const before = drawGeneric({
    series: [visible, hidden],
    frequencyRangeHz: [20, 20000],
    domainRangeHz: [250, 2000],
  });
  const after = drawGeneric({
    series: [visible, { ...hidden, draw: true }],
    frequencyRangeHz: [20, 20000],
    domainRangeHz: [250, 2000],
  });
  const beforePath = before.ctx.strokes.find((stroke) => stroke.style === "VISIBLE").path;
  const afterPath = after.ctx.strokes.find((stroke) => stroke.style === "VISIBLE").path;
  check(
    beforePath[1].y < afterPath[1].y,
    "chart: hidden curves do not expand the visible response scale",
  );
}

{
  const series = {
    curve: { freqs_hz: [20, 100, 1000, 19000, 20000], display: { deviation_db: [-45, -2, 2, 0, -45] } },
    color: 'VISIBLE',
  };
  for (const [range, untrusted, bound] of [
    [[20, 20000], [], 46],
    [[100, 19000], [], 5],
    [[20, 19000], [[20, 50]], 5],
  ]) {
    const { ctx } = drawGeneric({
      series: [{ ...series, curve: { ...series.curve, display: {
        ...series.curve.display, untrusted_intervals_hz: untrusted,
      } } }], frequencyRangeHz: range, minSpanDb: 10, padDb: 1,
    });
    const path = ctx.strokes.find((stroke) => stroke.style === 'VISIBLE').path;
    const peak = path.find((point) => point.y < 114);
    check(Math.abs(peak.y - (114 - 208 / bound)) < 1e-9, 'visible trusted data sets the symmetric dB scale');
    if (bound === 5) {
      check(ctx.labels.some((label) => label.text === '-5 dB') &&
        ctx.labels.some((label) => label.text === '5 dB'), 'tight responses show the ±5 dB limits');
    }
  }
  for (const payload of [
    { series: [{ ...series, draw: false }] },
    { series: [series], frequencyRangeHz: [200, 300] },
  ]) check(!drawGeneric(payload).drew, 'empty frequency windows and hidden traces clear the plot');
}

console.log(JSON.stringify({ ok: true, passed }));

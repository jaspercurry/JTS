// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// The shared frequency-response canvas (frequency-chart.js), driven through a
// recording 2D-context double. That does not verify pixels, but it does verify
// the draw calls: which curves were stroked, in what colour, with which dash
// pattern, and at what scale.
//
//   node tests/js/crossover_frequency_chart_test.mjs

import assert from "node:assert/strict";
import { loadEsm, repoPath } from "./_loader.mjs";

let passed = 0;
function check(condition, message, context) {
  assert.ok(condition, context ? `${message} — got ${JSON.stringify(context)}` : message);
  passed += 1;
}

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

globalThis.getComputedStyle = () => ({
  getPropertyValue: (name) => (name === "--chart-token" ? " TOKEN " : ""),
});
globalThis.window = { devicePixelRatio: 1 };

const { cssColor, drawFrequencyChart } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/frequency-chart.js"),
);

check(
  cssColor({}, "--chart-token", "#fff") === "TOKEN" && cssColor({}, "--unset", "#fff") === "#fff",
  "cssColor reads the design token and falls back when it is unset",
);

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

const SPEC_BANDS = [
  { f_lo_hz: 250, f_hi_hz: 500, within_target: false, max_deviation_db: -9.04, tolerance_db: 3 },
  { f_lo_hz: 500, f_hi_hz: 2000, within_target: true, max_deviation_db: 1.1, tolerance_db: 3 },
];
const graded = { domainRangeHz: [250, 2000], corridorBands: SPEC_BANDS };

const curve = (deviation, untrusted = []) => ({
  freqs_hz: [300, 700, 1000],
  display: { deviation_db: [deviation, deviation, deviation], untrusted_intervals_hz: untrusted },
});
const solid = (c) => ({ curve: c, color: "SOLID" });
const dashed = (c) => ({ curve: c, color: "DASHED", dash: [6, 4] });

for (const width of [240, 320, 640]) {
  const { ctx } = drawGeneric({ series: [{ curve: curve(0) }] }, width);
  const labels = ctx.labels.filter((label) => label.y === 232);
  check(labels.length >= 2 && labels.every((label, index) =>
    label.x >= 0 && label.x + ctx.measureText(label.text).width <= width &&
    (index === 0 || label.x >= labels[index - 1].x + ctx.measureText(labels[index - 1].text).width + 6)),
  'frequency labels stay inside the canvas without overlapping on narrow screens');
}

{
  const { drew, ctx } = drawGeneric({ series: [solid(curve(1.3)), dashed(curve(0.1))], ...graded });
  check(drew, "two prepared curves draw");
  const dashedStroke = ctx.strokes.find((s) => s.style === "DASHED");
  const solidStroke = ctx.strokes.find((s) => s.style === "SOLID");
  check(dashedStroke.dash[0] === 6 && solidStroke.dash.length === 0, "only the curve that asks for a dash is dashed");
  check(drawGeneric({ series: [dashed(curve(0.1))] }).drew, "one curve alone draws");
  check(!drawGeneric({ series: [dashed(curve(null))] }).drew, "a curve with no reference supplies no drawable points");
  const missing = drawGeneric({ series: [solid(curve(1)), dashed(curve(null))] });
  check(missing.drew && !missing.ctx.strokes.some((s) => s.style === "DASHED"), "a curve with no reference leaves the others visible");
  check(drawGeneric({ series: [dashed(curve(-10))], ...graded }).ctx.strokes.some((s) => s.style === "DASHED"), "a curve outside the tolerance corridor stays visible");
}

{
  const base = { freqs_hz: [300, 700], display: { deviation_db: [0, 1] } };
  const extreme = { freqs_hz: [300, 700, 19900], display: { deviation_db: [0, 1, -73] } };
  const path = (result) => result.ctx.strokes.find((s) => s.style === "SOLID").path;
  const clean = drawGeneric({ series: [solid(base)], ...graded });
  const wide = drawGeneric({ series: [solid(extreme)], ...graded });
  check(path(clean)[0].y === path(wide)[0].y, "points outside the domain range do not change its scaling");
  const gap = drawGeneric({ series: [solid({ freqs_hz: [300, 700, 1000], display: { deviation_db: [1, null, 2] } })] });
  check(path(gap).map((p) => p.op).join() === "move,move", "invalid bins break the line instead of joining across missing data");
}

{
  const { ctx } = drawGeneric({
    series: [solid(curve(1, [[1, 10], [500, 700], [30000, 40000]]))],
    theme: { excluded: "EXCLUDED" },
  });
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

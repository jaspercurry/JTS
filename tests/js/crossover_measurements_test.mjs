// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";
import { loadEsm, repoPath } from "./_loader.mjs";

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.textContent = "";
    this.value = "";
    this.checked = false;
  }
  addEventListener(type, listener) {
    (this.listeners[type] = this.listeners[type] || []).push(listener);
  }
  async dispatch(type) {
    for (const listener of this.listeners[type] || []) {
      await listener({ currentTarget: this });
    }
  }
  replaceChildren(...children) { this.children = children; }
}

function h(tag, props, ...children) {
  const element = new Element(tag.split(/[.#]/, 1)[0]);
  for (const [key, value] of Object.entries(props || {})) {
    if (key.startsWith("on") && typeof value === "function") {
      element.addEventListener(key.slice(2).toLowerCase(), value);
    } else {
      element[key] = value;
    }
  }
  element.children = children.flat(Infinity).filter((child) => child != null);
  return element;
}

function descendants(root, tag) {
  const found = [];
  for (const child of root.children || []) {
    if (child && child.tag === tag) found.push(child);
    if (child && typeof child === "object") found.push(...descendants(child, tag));
  }
  return found;
}

const elements = new Map([
  ["measurement-run-a", new Element("select")],
  ["measurement-run-b", new Element("select")],
  ["measurement-chart", new Element("canvas")],
  ["measurement-chart-status", new Element("p")],
  ["measurement-series", new Element("div")],
  ["measurement-metadata", new Element("section")],
]);
globalThis.document = { getElementById: (id) => elements.get(id) };
globalThis.window = { addEventListener() {} };

const responseSeries = (id, kind, visible, extra = {}) => ({
  id,
  label: id,
  kind,
  freqs_hz: [100, 1000, 10000],
  magnitude_db: [-25, -24, -26],
  reference_db: -24,
  smoothing_fractional_octave: kind === "position" ? 6 : 3,
  visible_by_default: visible,
  ...extra,
});

function responseRun(slot, id) {
  return {
    slot,
    id,
    label: `Run ${slot.toUpperCase()}`,
    started_at: 1000,
    state: "applied",
    metadata: {
      position_count: 2,
      angles_deg: [0, 7, 14],
      trusted_floor_hz: 200,
      excluded_bands_hz: [[900, 1100]],
      smoothing: {
        average_fractional_octave: 3,
        positions_fractional_octave: 6,
      },
    },
    series: [
      responseSeries("average", "average", true),
      responseSeries("7-deg", "measurement", false, { validity_floor_hz: 250 }),
      responseSeries("14-deg", "analysis", false, { validity_floor_hz: 250 }),
    ],
  };
}

const catalog = [
  { id: "a", name: "a", origin: "live", started_at: 1000, state: "applied" },
  { id: "round:r3", name: "r3", origin: "banked", started_at: 900, state: "applied" },
];
let mode = "normal";
const requests = [];
async function getJSON(url) {
  requests.push(url);
  if (mode === "error") throw new Error("load failed");
  if (mode === "empty") {
    return { catalog: [], selected: { a: null, b: null }, view: null };
  }
  const withB = url.includes("b=");
  return {
    catalog,
    selected: { a: "a", b: withB ? "round:r3" : null },
    view: { runs: withB ? [responseRun("a", "a"), responseRun("b", "b")] : [responseRun("a", "a")] },
  };
}

const chartPayloads = [];
function drawFrequencyChart(_canvas, payload) {
  chartPayloads.push(payload);
  return (payload.series || []).some((series) => series.draw !== false);
}

globalThis.__h = h;
globalThis.__svg = h;
globalThis.__getJSON = getJSON;
globalThis.__cssColor = (_canvas, _name, fallback) => fallback;
globalThis.__drawFrequencyChart = drawFrequencyChart;

await loadEsm(repoPath("deploy/assets/correction/js/measurements.js"), {
  stripImports: true,
  guardNoImports: true,
  prelude: [
    "const h = globalThis.__h;",
    "const svg = globalThis.__svg;",
    "const getJSON = globalThis.__getJSON;",
    "const cssColor = globalThis.__cssColor;",
    "const drawFrequencyChart = globalThis.__drawFrequencyChart;",
  ].join(" ") + "\n",
});
await new Promise((resolve) => setImmediate(resolve));

check(requests[0] === "data", "initial load uses the local read-only data route");
check(
  elements.get("measurement-run-a").value === "a" &&
  elements.get("measurement-run-b").value === "",
  "initial selection is run A with no run B",
);
check(
  descendants(elements.get("measurement-run-a"), "option")
    .map((option) => option.children[0]).join("|").includes("banked r3"),
  "the picker says which entries are banked rounds",
);
let chart = chartPayloads.at(-1);
check(
  chart.series.map((series) => series.draw).join(",") === "true,false,false",
  "only the aggregate is visible by default",
);
check(
  JSON.stringify(chart.series[1].dash) !== JSON.stringify(chart.series[2].dash),
  "generic detail curves have stable, distinct line styles",
);
const swatchLines = descendants(elements.get("measurement-series"), "line");
check(
  swatchLines[1]["stroke-dasharray"] !== swatchLines[2]["stroke-dasharray"],
  "each detail control shows the same distinct style used on the chart",
);
check(
  chart.excludedIntervals.some((band) => band.f_lo_hz === 20 && band.f_hi_hz === 200) &&
  chart.excludedIntervals.some((band) => band.f_lo_hz === 900 && band.f_hi_hz === 1100),
  "the trusted floor and stored exclusions reach the chart",
);

const inputs = descendants(elements.get("measurement-series"), "input");
inputs[1].checked = true;
await inputs[1].dispatch("change");
chart = chartPayloads.at(-1);
check(
  chart.series[1].draw === true &&
  chart.excludedIntervals.some((band) => band.f_lo_hz === 20 && band.f_hi_hz === 250),
  "revealing a position updates its trace and its own validity shading",
);

elements.get("measurement-run-b").value = "round:r3";
await elements.get("measurement-run-b").dispatch("change");
chart = chartPayloads.at(-1);
check(
  requests.at(-1) === "data?a=a&b=round%3Ar3" && chart.series.length === 6,
  "selecting a banked round as run B loads and draws the A/B view",
);

mode = "empty";
await elements.get("measurement-run-a").dispatch("change");
check(
  elements.get("measurement-chart-status").textContent.includes("No saved") &&
  chartPayloads.at(-1).series.length === 0,
  "empty results clear stale chart data",
);

mode = "error";
await elements.get("measurement-run-a").dispatch("change");
check(
  elements.get("measurement-chart-status").textContent === "load failed" &&
  chartPayloads.at(-1).series.length === 0,
  "load failures clear stale chart data and show the error",
);

console.log(JSON.stringify({ ok: true, passed }));

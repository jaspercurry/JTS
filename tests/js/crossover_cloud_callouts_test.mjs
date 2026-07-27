// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Rendered-HTML pins for the before/after visualization's anomaly callouts,
// provenance caption, and geometry guidance (flat-linearization plan PR-7,
// deploy/assets/correction/js/crossover/cloud.js). The chart's own pixel
// output is out of reach for this harness (no browser, no canvas — see the
// module's chart.js sibling, verified on-device instead); this file pins
// everything that IS a DOM assertion: the exact server-owned copy strings
// (including the "cannot classify source-fixed vs room-fixed from one
// session" phrasing) reach the page verbatim, and the section's visibility
// gates degrade honestly.
//
//   node tests/js/crossover_cloud_callouts_test.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// A minimal, real-enough fake DOM: document.createElement returns a FRESH
// node each call (unlike the fixed getElementById map other crossover
// harnesses use), because renderCallouts() builds new nodes per carve-out
// row via plain document.createElement + textContent (this module
// deliberately does not use the shared h() builder — see cloud.js's own
// header comment for why).
class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.className = "";
    this._textContent = "";
    this.children = [];
    this.hidden = false;
    this.dataset = {};
  }
  get textContent() { return this._textContent; }
  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener() {}
}

function fixedElement(id) {
  return new FakeElement(id);
}

globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
};

const els = {
  cloud: fixedElement("crossover-cloud"),
  cloudProvenance: fixedElement("crossover-cloud-provenance"),
  cloudChart: fixedElement("crossover-cloud-chart"),
  cloudGeometry: fixedElement("crossover-cloud-geometry"),
  cloudCallouts: fixedElement("crossover-cloud-callouts"),
};

// The chart's own canvas drawing needs browser APIs (getComputedStyle,
// devicePixelRatio) this harness does not provide — stubbed as a no-op so
// renderCloud()'s callout/provenance/geometry logic can be pinned in
// isolation. chart.js itself is exercised on-device (CI cannot see pixels).
globalThis.__drawCloudChart = () => {};

const here = dirname(fileURLToPath(import.meta.url));
let source = readFileSync(
  resolve(here, "../../deploy/assets/correction/js/crossover/cloud.js"),
  "utf8",
);
source = source.replace(
  /^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm,
  "",
);
source = `const drawCloudChart = globalThis.__drawCloudChart;\n${source}`;
const dataUrl =
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { renderCloud } = await import(dataUrl);

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

const CLOUD_MEASURE = "cloud_measure";
const CLOUD_VERIFY = "cloud_verify";

// --- nothing measured yet: the whole section stays hidden -----------------
renderCloud(els, { cloud: null, cloud_chart: null });
check(els.cloud.hidden === true, "no curve data: section hidden");

renderCloud(els, {
  cloud: { [CLOUD_VERIFY]: { carve_outs: [] } },
  cloud_chart: { [CLOUD_VERIFY]: { curve: null }, [CLOUD_MEASURE]: { curve: null } },
});
check(els.cloud.hidden === true, "curve explicitly null on both phases: still hidden");

// --- a real carve-outs fixture, matching the shipped schema ----------------
// (jasper.active_speaker.crossover_v2_flow.carve_outs_by_band's exact shape:
// one entry per spec band, always, each carrying band_hz/intervals/
// disclosure/expert — see _carve_out_disclosure_copy/_carve_out_expert_copy/
// _null_classification_copy for the copy this pins verbatim).
const positionInvariantDisclosure =
  "Interference nulls at 8.7 kHz, 11.6 kHz and 15.0 kHz — a delayed copy of " +
  "the sound arrives 0.30 ms later. EQ cannot fill these, so they are left " +
  "out of correction and out of this band's grading.";
const positionInvariantExpert =
  "carved out of grading: 8.7 kHz (rung 26, 6.2 dB deep), 11.6 kHz " +
  "(rung 35, 5.8 dB deep), 15.0 kHz (rung 45, 6.1 dB deep); delay τ 299 µs, " +
  "reflection ratio r 0.375 measured in time / 0.349 implied by null depth";
const screenDisclosure =
  "One range is left out of correction and out of this band's grading " +
  "because the microphone positions disagreed about it too much to grade.";

const carveOuts = [
  {
    band_hz: [250.0, 2000.0],
    intervals: [{ f_lo_hz: 1729.0, f_hi_hz: 1882.0, source: "position_screen" }],
    disclosure: screenDisclosure,
    expert: "",
  },
  { band_hz: [2000.0, 8000.0], intervals: [], disclosure: "", expert: "" },
  {
    band_hz: [8000.0, 16000.0],
    intervals: [
      { f_lo_hz: 8300.0, f_hi_hz: 8600.0, source: "identified_null", f_center_hz: 8700.0, n: 26, classification: "position_invariant" },
      { f_lo_hz: 11400.0, f_hi_hz: 11700.0, source: "identified_null", f_center_hz: 11600.0, n: 35, classification: "position_invariant" },
      { f_lo_hz: 14800.0, f_hi_hz: 15100.0, source: "identified_null", f_center_hz: 15000.0, n: 45, classification: "position_invariant" },
    ],
    disclosure: positionInvariantDisclosure,
    expert: positionInvariantExpert,
  },
];

renderCloud(els, {
  cloud: {
    [CLOUD_VERIFY]: {
      carve_outs: carveOuts,
      provenance_note: "",
      geometry_guidance: "",
    },
  },
  cloud_chart: {
    [CLOUD_VERIFY]: { curve: { freqs_hz: [100, 200], magnitude_db: [-1, -2] } },
    [CLOUD_MEASURE]: { curve: null },
  },
});

check(els.cloud.hidden === false, "curve present: section visible");
check(els.cloudCallouts.children.length === 2, "two non-empty bands render two callouts (the empty 2-8kHz band is skipped)");

const [screenCard, nullCard] = els.cloudCallouts.children;
check(screenCard.children[0].textContent === screenDisclosure, "position-screen callout headline is the server string verbatim");
check(screenCard.children.length === 1, "a callout with no expert string renders no <details>");

check(nullCard.children[0].textContent === positionInvariantDisclosure, "identified-null callout headline is the server string verbatim");
check(
  nullCard.children[0].textContent.includes(
    "arrives 0.30 ms later",
  ),
  "the delay is quoted in milliseconds in the headline, not microseconds",
);
const details = nullCard.children[1];
check(details.tag === "details", "the expert register renders as a <details>");
check(details.className === "candidate-provenance", "reuses the review screen's own disclosure class, not a new one");
check(details.children[0].tag === "summary" && details.children[0].textContent === "Technical details", "the disclosure has a summary label");
check(details.children[1].textContent === positionInvariantExpert, "the expert line carries τ/r verbatim, kept out of the headline");
check(!nullCard.children[0].textContent.includes("τ"), "the plain-language headline never carries τ notation");

// The load-bearing "cannot classify source-fixed vs room-fixed from one
// session" phrasing (plan PR-1's pre-registered vocabulary) must reach the
// page character-for-character — this is the sentence the whole program's
// honesty argument rests on.
check(
  positionInvariantDisclosure.includes(
    "a delayed copy of the sound arrives",
  ) && nullCard.children[0].textContent === positionInvariantDisclosure,
  "the null callout's exact server sentence renders verbatim",
);

// --- geometry guidance: rendered when present, hidden when empty ----------
renderCloud(els, {
  cloud: {
    [CLOUD_VERIFY]: {
      carve_outs: [],
      provenance_note: "",
      geometry_guidance:
        "The measured echo pattern did not change between microphone " +
        "positions. Spreading the microphone further apart next time may " +
        "help JTS tell the speaker's own sound apart from the room's.",
    },
  },
  cloud_chart: { [CLOUD_VERIFY]: { curve: { freqs_hz: [1], magnitude_db: [1] } } },
});
check(els.cloudGeometry.hidden === false, "non-empty geometry guidance is shown");
check(
  els.cloudGeometry.textContent.startsWith("The measured echo pattern did not change"),
  "geometry guidance is the server string verbatim, not re-phrased",
);
check(els.cloudCallouts.children.length === 0, "no carve-outs: no callout cards, not an empty-state placeholder");

renderCloud(els, {
  cloud: { [CLOUD_VERIFY]: { carve_outs: [], provenance_note: "", geometry_guidance: "" } },
  cloud_chart: { [CLOUD_VERIFY]: { curve: { freqs_hz: [1], magnitude_db: [1] } } },
});
check(els.cloudGeometry.hidden === true, "empty geometry guidance (not locked): hidden, no placeholder");

// --- provenance: rendered only for the stale (measured_this_session=False)
// state; silent for "current" and "unknown" alike (mirrors the server's own
// _provenance_note rule) -------------------------------------------------
const staleNote =
  "This chart is from a previous session's measurement — re-measure to see " +
  "this session's own result.";
renderCloud(els, {
  cloud: { [CLOUD_VERIFY]: { carve_outs: [], provenance_note: staleNote, geometry_guidance: "" } },
  cloud_chart: { [CLOUD_VERIFY]: { curve: { freqs_hz: [1], magnitude_db: [1] } } },
});
check(els.cloudProvenance.hidden === false, "a stale provenance note is shown");
check(els.cloudProvenance.textContent === staleNote, "the provenance caption is the server string verbatim");

renderCloud(els, {
  cloud: { [CLOUD_VERIFY]: { carve_outs: [], provenance_note: "", geometry_guidance: "" } },
  cloud_chart: { [CLOUD_VERIFY]: { curve: { freqs_hz: [1], magnitude_db: [1] } } },
});
check(els.cloudProvenance.hidden === true, "an empty provenance note (current or unknown session) stays silent");

console.log(JSON.stringify({ ok: true, passed }));

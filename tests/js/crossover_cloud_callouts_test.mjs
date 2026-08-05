// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Rendered-HTML pins for the before/after visualization's anomaly callouts,
// provenance caption, and geometry guidance (flat-linearization plan PR-7,
// deploy/assets/correction/js/crossover/cloud.js). The chart's own pixel
// output is out of reach for this harness (no browser, no canvas — see the
// module's chart.js sibling; CI cannot see pixels, so that verification is
// still owed to the HW product smoke — docs/flat-linearization-
// productization-plan.md's PR-7 section, NOT done here or anywhere else in
// this branch). This file pins everything that IS a DOM assertion: the
// exact server-owned copy strings (including the "cannot classify
// source-fixed vs room-fixed from one session" phrasing) reach the page
// verbatim, and the section's visibility gates degrade honestly.
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
    this.attributes = {};
  }
  get textContent() { return this._textContent; }
  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener() {}
  // #2152: the section's aria-label is part of the honesty claim (it is what
  // a screen reader announces in place of the heading), so the fake has to
  // support the real DOM call that sets it.
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name] : null;
  }
}

function fixedElement(id) {
  return new FakeElement(id);
}

globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
};

const els = {
  cloud: fixedElement("crossover-cloud"),
  cloudEyebrow: fixedElement("crossover-cloud-eyebrow"),
  cloudTitle: fixedElement("crossover-cloud-title"),
  cloudBasis: fixedElement("crossover-cloud-basis"),
  cloudProvenance: fixedElement("crossover-cloud-provenance"),
  cloudChart: fixedElement("crossover-cloud-chart"),
  cloudGeometry: fixedElement("crossover-cloud-geometry"),
  cloudCallouts: fixedElement("crossover-cloud-callouts"),
  cloudPending: fixedElement("crossover-cloud-pending"),
  legendMeasure: fixedElement("crossover-chart-legend-measure"),
  legendVerify: fixedElement("crossover-chart-legend-verify"),
  legendPredicted: fixedElement("crossover-chart-legend-predicted"),
  legendCorridor: fixedElement("crossover-chart-legend-corridor"),
  legendExcluded: fixedElement("crossover-chart-legend-excluded"),
};

// The chart's own canvas drawing needs browser APIs (getComputedStyle,
// devicePixelRatio) this harness does not provide — stubbed so
// renderCloud()'s callout/provenance/geometry/legend logic can be pinned in
// isolation. chart.js itself is exercised on-device (CI cannot see pixels).
// The stub RECORDS its call so this file can also pin the payload handed to
// it (review B-1: each phase's OWN reference_db, not one shared value).
let lastChartPayload = null;
globalThis.__drawCloudChart = (canvas, payload) => {
  lastChartPayload = payload;
};

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

// --- measure-only (review S-5): the pre-correction cloud has closed but
// verify has not, so there is no verify curve, no spec bands, and no
// carve-outs yet. The legend must not advertise series that are not on the
// canvas, and a plain caption should say more is coming. ------------------
renderCloud(els, {
  cloud: { [CLOUD_MEASURE]: { reference_db: -27.3 } },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28] } },
  },
});
check(els.cloud.hidden === false, "measure-only: section is visible (something was measured)");
check(els.legendMeasure.hidden === false, "measure-only: 'Before correction' swatch shown");
check(els.legendVerify.hidden === true, "measure-only: 'After correction' swatch hidden — not on canvas yet");
check(els.legendCorridor.hidden === true, "measure-only: 'Spec tolerance' swatch hidden — no spec bands yet");
check(els.legendExcluded.hidden === true, "measure-only: 'Excluded' swatch hidden — no carve-outs yet");
check(els.cloudPending.hidden === false, "measure-only: the 'still coming' caption is shown");
check(
  els.cloudPending.textContent ===
    "The after-correction curve appears once the second measurement pass finishes.",
  "the pending caption is plain-language and hardware-blind, no numeric promise",
);
check(els.cloudCallouts.children.length === 0, "measure-only: no callouts (verify has no carve_outs yet)");
check(
  lastChartPayload.measureReferenceDb === -27.3 && lastChartPayload.verifyReferenceDb === null,
  "measure-only: chart payload carries MEASURE's own reference, VERIFY's is null",
);

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
    [CLOUD_MEASURE]: { reference_db: -24.1 },
    [CLOUD_VERIFY]: {
      carve_outs: carveOuts,
      provenance_note: "",
      geometry_guidance: "",
      reference_db: -27.3,
      spec_bands: [
        { f_lo_hz: 250.0, f_hi_hz: 2000.0, tolerance_db: 1.5 },
        { f_lo_hz: 2000.0, f_hi_hz: 8000.0, tolerance_db: 2.0 },
        { f_lo_hz: 8000.0, f_hi_hz: 16000.0, tolerance_db: 2.5 },
      ],
    },
  },
  cloud_chart: {
    [CLOUD_VERIFY]: { curve: { freqs_hz: [100, 200], magnitude_db: [-1, -2] } },
    [CLOUD_MEASURE]: { curve: { freqs_hz: [100, 200], magnitude_db: [-3, -4] } },
  },
});

check(els.cloud.hidden === false, "curve present: section visible");
check(els.cloudCallouts.children.length === 2, "two non-empty bands render two callouts (the empty 2-8kHz band is skipped)");

// Review S-5: full state (both curves, spec bands, and carve-outs all
// present) shows every legend entry — nothing progressively hidden once
// every series actually has data.
check(els.legendMeasure.hidden === false, "full state: 'Before correction' shown");
check(els.legendVerify.hidden === false, "full state: 'After correction' shown");
check(els.legendCorridor.hidden === false, "full state: 'Spec tolerance' shown (spec bands exist)");
check(els.legendExcluded.hidden === false, "full state: 'Excluded' shown (carve-outs exist)");
check(els.cloudPending.hidden === true, "full state: no 'still coming' caption once verify exists");

// Review B-1: each curve is plotted relative to its OWN reference — the
// payload handed to the chart carries both, never one shared value.
check(
  lastChartPayload.measureReferenceDb === -24.1,
  "chart payload carries MEASURE's own reference_db",
);
check(
  lastChartPayload.verifyReferenceDb === -27.3,
  "chart payload carries VERIFY's own reference_db (they differ — cut-only correction)",
);
check(
  lastChartPayload.specBands.length === 3 &&
    lastChartPayload.specBands[2].tolerance_db === 2.5,
  "chart payload carries the spec bands verbatim, including tolerance_db",
);

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

// --- express (M=1, flow-simplification §1.3): no post-apply cloud EVER,
// not merely "not yet" — the pending caption must not promise a curve that
// will never appear. ---------------------------------------------------
renderCloud(els, {
  cloud: { [CLOUD_MEASURE]: { reference_db: -27.3 } },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28] } },
  },
  tier: "express",
});
check(els.cloud.hidden === false, "express, measure-only: section is visible (something was measured)");
check(els.cloudPending.hidden === false, "express, measure-only: a caption is shown");
check(
  els.cloudPending.textContent !==
    "The after-correction curve appears once the second measurement pass finishes.",
  "express must not reuse the full-tier 'second pass' wording — there is no second pass",
);
check(
  els.cloudPending.textContent.includes("no after-correction curve"),
  "express says plainly there is no after-correction curve for this measurement",
);
check(
  els.cloudPending.textContent.includes("Full measurement"),
  "express names the Full-measurement upgrade path",
);

// A full-tier (or tier-unknown, e.g. pre-tier durable state) session mid-
// cloud keeps the ordinary "still coming" wording — unchanged by this fix.
renderCloud(els, {
  cloud: { [CLOUD_MEASURE]: { reference_db: -27.3 } },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28] } },
  },
  tier: "full",
});
check(
  els.cloudPending.textContent ===
    "The after-correction curve appears once the second measurement pass finishes.",
  "full tier keeps the ordinary 'still coming' wording",
);
renderCloud(els, {
  cloud: { [CLOUD_MEASURE]: { reference_db: -27.3 } },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28] } },
  },
  tier: null,
});
check(
  els.cloudPending.textContent ===
    "The after-correction curve appears once the second measurement pass finishes.",
  "tier-unknown (pre-tier durable state) keeps the ordinary 'still coming' wording, not the express one",
);

// --- B1 (adversarial review of PR #1780): express's spec corridor, carve-out
// callouts, and provenance/geometry copy come from CLOUD_MEASURE — the ONLY
// cloud express ever produces — never from CLOUD_VERIFY (which express never
// closes, so reading it there would silently show nothing). -----------------
const expressCarveOuts = [
  {
    band_hz: [2000.0, 8000.0],
    intervals: [{ f_lo_hz: 8300.0, f_hi_hz: 8600.0 }],
    disclosure: "A range is left out of correction.",
    expert: "carved out of grading: 8.4 kHz (rung 26, 6.2 dB deep)",
  },
];
renderCloud(els, {
  cloud: {
    [CLOUD_MEASURE]: {
      reference_db: -24.1,
      carve_outs: expressCarveOuts,
      provenance_note: "",
      geometry_guidance: "Spread the microphone further apart next time.",
      spec_bands: [{ f_lo_hz: 2000.0, f_hi_hz: 8000.0, tolerance_db: 2.0 }],
    },
  },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [100, 200], magnitude_db: [-3, -4] } },
  },
  tier: "express",
});
check(els.cloud.hidden === false, "express: section visible with only a measure-phase curve");
check(
  els.cloudGeometry.hidden === false
    && els.cloudGeometry.textContent === "Spread the microphone further apart next time.",
  "express: geometry guidance is read from CLOUD_MEASURE (never silently dropped)",
);
check(
  els.legendCorridor.hidden === false,
  "express: the spec-tolerance legend swatch shows — the corridor draws against the BEFORE curve",
);
check(
  els.legendExcluded.hidden === false,
  "express: the excluded-interval legend swatch shows from CLOUD_MEASURE's own carve-outs",
);
check(
  els.cloudCallouts.children.length === 1
    && els.cloudCallouts.children[0].children[0].textContent === "A range is left out of correction.",
  "express: the carve-out callout renders VERBATIM from CLOUD_MEASURE — owner decision 1 applies to every tier",
);
check(
  lastChartPayload.specBands.length === 1 && lastChartPayload.specBands[0].tolerance_db === 2.0,
  "express: the chart payload's spec bands come from CLOUD_MEASURE, drawn on the before curve",
);
check(
  lastChartPayload.excludedIntervals.length === 1
    && lastChartPayload.excludedIntervals[0].f_lo_hz === 8300.0,
  "express: the chart payload's excluded intervals come from CLOUD_MEASURE",
);

// A full-tier session with an equivalent CLOUD_MEASURE-only shape (mid pre-
// apply cloud, its own group already closed with carve-outs) must NOT show
// them — Full's spec surface is VERIFY's, the current graded truth, and the
// pre-apply cloud exists to be out of spec (unchanged by this fix).
renderCloud(els, {
  cloud: {
    [CLOUD_MEASURE]: {
      reference_db: -24.1,
      carve_outs: expressCarveOuts,
      provenance_note: "",
      geometry_guidance: "Spread the microphone further apart next time.",
      spec_bands: [{ f_lo_hz: 2000.0, f_hi_hz: 8000.0, tolerance_db: 2.0 }],
    },
  },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [100, 200], magnitude_db: [-3, -4] } },
  },
  tier: "full",
});
check(
  els.cloudGeometry.hidden === true,
  "full tier: CLOUD_MEASURE's geometry guidance stays silent — Full's surface is VERIFY's, unchanged",
);
check(
  els.legendCorridor.hidden === true && els.legendExcluded.hidden === true,
  "full tier: no corridor/excluded legend from a pre-apply cloud that exists to be out of spec",
);
check(els.cloudCallouts.children.length === 0, "full tier: no callouts from CLOUD_MEASURE's carve-outs");

// --- #2152: the section may not claim measurement it does not have --------
//
// THE DEFECT. R15 removed the pre-apply CLOUD_MEASURE by design (#2106), so on
// the driver-only review screen the only curve is the prediction. The heading
// still read "Before and after / What the microphone heard" — directly above a
// legend reading "(not measured)" — while asking the household to approve a
// change on the strength of it. This block pins that the frame follows the
// canvas.

// Words that assert the microphone did something. None may appear anywhere in
// the framing of a screen whose only curve is a model.
const HEARD_CLAIM = /\b(heard|hear|measured|measurement)\b/i;

function renderPredictionOnly() {
  renderCloud(els, {
    cloud: { [CLOUD_MEASURE]: { reference_db: -27.3, carve_outs: [] } },
    cloud_chart: null,
    prediction: {
      curve: { freqs_hz: [300, 1000], magnitude_db: [-27, -27.4] },
      reference_db: -27.3,
    },
  });
}

renderPredictionOnly();
check(els.cloud.hidden === false, "prediction-only: the section is shown");
check(
  els.legendPredicted.hidden === false
    && els.legendMeasure.hidden === true
    && els.legendVerify.hidden === true,
  "prediction-only: the model is the ONLY series on the canvas",
);
check(
  els.cloudTitle.textContent === "What JTS expects after correction",
  "prediction-only: the heading names a model, not a measurement",
);
check(
  !HEARD_CLAIM.test(els.cloudTitle.textContent)
    && !HEARD_CLAIM.test(els.cloudEyebrow.textContent),
  "prediction-only: neither heading nor eyebrow claims the microphone heard anything",
);
check(
  els.cloud.getAttribute("aria-label") === "Predicted frequency response after correction",
  "prediction-only: the aria-label is corrected too — not a sighted-only fix",
);
check(
  els.cloudBasis.hidden === false
    && els.cloudBasis.textContent.includes("a prediction, not a measurement")
    && els.cloudBasis.textContent.includes("measurements JTS just took"),
  "prediction-only: the basis line says where the curve came from and what it is not",
);
check(
  !/\b(driver|drivers|tweeter|woofer|horn)\b/i.test(els.cloudBasis.textContent),
  "prediction-only: the basis line stays hardware-blind — evidence, not device taxonomy",
);
check(
  els.cloudBasis.textContent.includes("right after you apply"),
  "prediction-only: …and when a real measurement arrives, so this reads as a step not a hedge",
);

// THE OTHER HALF, and the one that keeps this fix from over-correcting: the
// POST-VERIFY chart plots measured curves and its fidelity is confirmed to
// 0.02 dB (#2152 scope note). Its framing must stay exactly as it was.
renderCloud(els, {
  cloud: {
    [CLOUD_MEASURE]: { reference_db: -27.3 },
    [CLOUD_VERIFY]: { reference_db: -28.0, carve_outs: [] },
  },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28] } },
    [CLOUD_VERIFY]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-27, -28] } },
  },
});
check(
  els.cloudTitle.textContent === "What the microphone heard"
    && els.cloudEyebrow.textContent === "Before and after",
  "measured: the post-verify framing is untouched",
);
check(
  els.cloud.getAttribute("aria-label") === "Before and after measurement",
  "measured: the aria-label returns to the measured wording",
);
check(
  els.cloudBasis.hidden === true && els.cloudBasis.textContent === "",
  "measured: no basis line — there is nothing to disclaim about a measurement",
);

// A single measured curve is still a measurement: the honest framing is keyed
// on whether ANY measured series is on the canvas, not on having both.
renderCloud(els, {
  cloud: { [CLOUD_MEASURE]: { reference_db: -27.3 } },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28] } },
  },
  prediction: {
    curve: { freqs_hz: [300, 1000], magnitude_db: [-27, -27.4] },
    reference_db: -27.3,
  },
});
check(
  els.cloudTitle.textContent === "What the microphone heard",
  "measured + predicted together: the microphone DID hear something, so the heading stands",
);

// And it goes back. A household walking review -> apply -> verify sees the
// section re-framed each time; a one-way switch would strand the measured
// screen under the predicted heading for the rest of the session.
renderPredictionOnly();
check(
  els.cloudTitle.textContent === "What JTS expects after correction"
    && els.cloudBasis.hidden === false,
  "the framing is re-derived on every render, not latched",
);

console.log(JSON.stringify({ ok: true, passed }));

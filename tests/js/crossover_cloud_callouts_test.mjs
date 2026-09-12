// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Rendered-HTML pins for the before/after visualization's anomaly callouts,
// provenance caption, and geometry guidance (flat-linearization plan PR-7,
// deploy/assets/correction/js/crossover/cloud.js). The chart's own pixel
// output is out of reach for this harness (no browser, no canvas — see the
// module's chart.js sibling; CI cannot see pixels, so that verification is
// still owed to the HW product smoke —
// docs/historical/linearization-campaign-2026-07.md's PR-7 section, NOT done
// here or anywhere else in
// this branch). This file pins everything that IS a DOM assertion: the
// exact server-owned copy strings (including the "cannot classify
// source-fixed vs room-fixed from one session" phrasing) reach the page
// verbatim, and the section's visibility gates degrade honestly.
//
//   node tests/js/crossover_cloud_callouts_test.mjs

import assert from "node:assert/strict";
import { loadEsm, repoPath } from "./_loader.mjs";
// A minimal, real-enough fake DOM: document.createElement returns a FRESH
// node each call (unlike the fixed getElementById map other crossover
// harnesses use), because renderCallouts() builds new nodes per carve-out
// row via plain document.createElement + textContent (this module
// deliberately does not use the shared h() builder — see cloud.js's own
// header comment for why).
import { FakeElement } from "./_dom.mjs";

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

const { renderCloud } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/cloud.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: "const drawCloudChart = globalThis.__drawCloudChart;\n",
  },
);

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
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28], display: { deviation_db: [1, -1], untrusted_intervals_hz: [] } } },
  },
});
check(els.cloud.hidden === false, "measure-only: section is visible (something was measured)");
check(els.legendMeasure.hidden === false, "measure-only: 'Before correction' swatch shown");
check(els.legendVerify.hidden === true, "measure-only: 'After correction' swatch hidden — not on canvas yet");
check(els.legendCorridor.hidden === true, "measure-only: 'Spec tolerance' swatch hidden — no spec bands yet");
check(els.legendExcluded.hidden === true, "measure-only: 'Excluded' swatch hidden — no carve-outs yet");
check(els.cloudPending.hidden === false, "measure-only: the 'still coming' caption is shown");

check(els.cloudCallouts.children.length === 0, "measure-only: no callouts (verify has no carve_outs yet)");

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
    [CLOUD_VERIFY]: { curve: { freqs_hz: [100, 200], magnitude_db: [-1, -2], display: { deviation_db: [1, -1], untrusted_intervals_hz: [[8300, 8600]] } } },
    [CLOUD_MEASURE]: { curve: { freqs_hz: [100, 200], magnitude_db: [-3, -4], display: { deviation_db: [1, -1], untrusted_intervals_hz: [[8300, 8600]] } } },
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
  cloud_chart: { [CLOUD_VERIFY]: { curve: { freqs_hz: [1], magnitude_db: [1], display: { deviation_db: [1, -1], untrusted_intervals_hz: [] } } } },
});
check(els.cloudGeometry.hidden === false, "non-empty geometry guidance is shown");
check(
  els.cloudGeometry.textContent.startsWith("The measured echo pattern did not change"),
  "geometry guidance is the server string verbatim, not re-phrased",
);
check(els.cloudCallouts.children.length === 0, "no carve-outs: no callout cards, not an empty-state placeholder");

renderCloud(els, {
  cloud: { [CLOUD_VERIFY]: { carve_outs: [], provenance_note: "", geometry_guidance: "" } },
  cloud_chart: { [CLOUD_VERIFY]: { curve: { freqs_hz: [1], magnitude_db: [1], display: { deviation_db: [1, -1], untrusted_intervals_hz: [] } } } },
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
  cloud_chart: { [CLOUD_VERIFY]: { curve: { freqs_hz: [1], magnitude_db: [1], display: { deviation_db: [1, -1], untrusted_intervals_hz: [] } } } },
});
check(els.cloudProvenance.hidden === false, "a stale provenance note is shown");
check(els.cloudProvenance.textContent === staleNote, "the provenance caption is the server string verbatim");

renderCloud(els, {
  cloud: { [CLOUD_VERIFY]: { carve_outs: [], provenance_note: "", geometry_guidance: "" } },
  cloud_chart: { [CLOUD_VERIFY]: { curve: { freqs_hz: [1], magnitude_db: [1], display: { deviation_db: [1, -1], untrusted_intervals_hz: [] } } } },
});
check(els.cloudProvenance.hidden === true, "an empty provenance note (current or unknown session) stays silent");

const measureCarveOuts = [
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
      carve_outs: measureCarveOuts,
      provenance_note: "",
      geometry_guidance: "Spread the microphone further apart next time.",
      spec_bands: [{ f_lo_hz: 2000.0, f_hi_hz: 8000.0, tolerance_db: 2.0 }],
    },
  },
  cloud_chart: {
    [CLOUD_MEASURE]: { curve: { freqs_hz: [100, 200], magnitude_db: [-3, -4], display: { deviation_db: [1, -1], untrusted_intervals_hz: [[8300, 8600]] } } },
  },
});
check(els.cloud.hidden === false, "measure-only: section visible with only a measure-phase curve");
check(
  els.cloudGeometry.hidden === false
    && els.cloudGeometry.textContent === "Spread the microphone further apart next time.",
  "measure-only: geometry guidance is read from CLOUD_MEASURE (never silently dropped)",
);
check(
  els.legendCorridor.hidden === false,
  "measure-only: the spec-tolerance legend swatch shows — the corridor draws against the BEFORE curve",
);
check(
  els.legendExcluded.hidden === false,
  "measure-only: the excluded-interval legend swatch shows from CLOUD_MEASURE's own carve-outs",
);
check(
  els.cloudCallouts.children.length === 1
    && els.cloudCallouts.children[0].children[0].textContent === "A range is left out of correction.",
  "measure-only: the carve-out callout renders VERBATIM from CLOUD_MEASURE",
);
check(
  lastChartPayload.specBands.length === 1 && lastChartPayload.specBands[0].tolerance_db === 2.0,
  "measure-only: the chart payload's spec bands come from CLOUD_MEASURE, drawn on the before curve",
);
check(
  lastChartPayload.measureCurve.display.untrusted_intervals_hz[0][0] === 8300.0,
  "measure-only: the chart payload's excluded intervals come from CLOUD_MEASURE",
);

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
      curve: { freqs_hz: [300, 1000], magnitude_db: [-27, -27.4], display: { deviation_db: [1, -1], untrusted_intervals_hz: [] } },
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
// BOTH aria-labels. The markup carries one on the <section> and one on the
// <canvas>; correcting only the section would leave the chart element itself
// announcing "before and after correction" on a screen with neither.
check(
  els.cloud.getAttribute("aria-label") === "Predicted frequency response after correction",
  "prediction-only: the SECTION aria-label is corrected — not a sighted-only fix",
);
check(
  els.cloudChart.getAttribute("aria-label")
    === "Predicted frequency response after correction, not measured",
  "prediction-only: the CANVAS aria-label is corrected too, and carries 'not measured'",
);
check(
  !HEARD_CLAIM.test(els.cloud.getAttribute("aria-label"))
    && /not measured/.test(els.cloudChart.getAttribute("aria-label")),
  "prediction-only: the words do for a screen reader what the dashed stroke does for the eye",
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
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28], display: { deviation_db: [1, -1], untrusted_intervals_hz: [] } } },
    [CLOUD_VERIFY]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-27, -28], display: { deviation_db: [1, -1], untrusted_intervals_hz: [] } } },
  },
});
check(
  els.cloudTitle.textContent === "What the microphone heard"
    && els.cloudEyebrow.textContent === "Before and after",
  "measured: the post-verify framing is untouched",
);
check(
  els.cloud.getAttribute("aria-label") === "Before and after measurement",
  "measured: the SECTION aria-label returns to the measured wording",
);
check(
  els.cloudChart.getAttribute("aria-label")
    === "Frequency response before and after correction",
  "measured: the CANVAS aria-label returns to the exact wording the markup ships",
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
    [CLOUD_MEASURE]: { curve: { freqs_hz: [300, 1000], magnitude_db: [-26, -28], display: { deviation_db: [1, -1], untrusted_intervals_hz: [] } } },
  },
  prediction: {
    curve: { freqs_hz: [300, 1000], magnitude_db: [-27, -27.4], display: { deviation_db: [1, -1], untrusted_intervals_hz: [] } },
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

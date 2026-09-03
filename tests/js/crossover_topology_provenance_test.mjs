// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// WHAT RUNS THIS: `tests/test_crossover_wizard_js.py`, which discovers
// `crossover_*_test.mjs` BY GLOB (with a loud fail-on-empty assertion) and
// executes each one through the pytest matrix — so this file is covered by CI
// the moment it exists, with no workflow edit. See
// `tests/js/crossover_polarity_provenance_test.mjs` for the full rationale
// behind that discovery mechanism; this harness is its twin one level up.
//
// A topology prescription pins a crossover CORNER + ORDER for one measurement
// round — exactly the shape a pinned polarity already has, one level up the
// candidate. `_candidate_review_payload` (crossover_envelope_v2.py) says why
// the two travel the same rule: "the reviewed graph crosses, and whether an
// operator PINNED it there... the renderer's rule is the polarity row's rule:
// a pinned number is worded '(pinned for this round)' and never as something
// the round measured." main.js's own comment above `crossoverPinned` calls
// this "the SAME rule one level up" — this harness pins that the renderer
// actually holds to it:
//   - a pinned corner is worded "(pinned for this round)";
//   - the SAME corner, unpinned, is the bare numbers — no pin marker, and
//     (unlike the polarity row) the crossover row never claims "measured" to
//     begin with, so the risk here is narrower: inventing a pin, not a false
//     measurement claim;
//   - the Crossover row sits ABOVE the trims it governs, since every trim,
//     delay, and polarity decision below it is made AT that corner;
//   - a candidate with no topology of its own (`crossover: null`) renders no
//     row at all, pinned bit or not — the bit says HOW a corner was decided,
//     never THAT one exists; and
//   - `crossover_pinned` is read `=== true`, exactly as strict as
//     `polarityPinned`, so a truthy-but-not-boolean value can never invent
//     the pinned wording.

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { CROSSOVER_IDS, installFixedDocument } from "./_dom.mjs";

const elements = installFixedDocument(CROSSOVER_IDS);
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.__getJSON = async () => ({});
globalThis.__postJSON = async () => ({});
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

const { render } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/main.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: aliasGlobals([
      "getJSON", "postJSON", "renderCloud", "redrawCloudChart",
    ]),
    truncateBefore: "\nrefresh().catch((error) => {",
    exportNames: ["render"],
  },
);

const baseEnvelope = {
  verdict_text: "",
  steps: [],
  nudges: [],
  relay: null,
  next_action: null,
  alternate_actions: [],
};

let passed = 0;
function check(condition, message, context) {
  assert.ok(condition, context ? `${message} — got ${JSON.stringify(context)}` : message);
  passed += 1;
}

function candidateReview(overrides) {
  return Object.assign({
    trims: [{ role: "woofer", attenuation_db: -2.5 }],
    delay: { role: "woofer", delay_ms: 0.0375 },
    polarity: "keep",
    confidence: 0.71,
    fingerprint: "cand-proof",
    program_id: "prog-1",
  }, overrides || {});
}

// Reads a row's meta text by its title — every measurement-row on this
// screen nests a title <p> and a meta <p> one div deep (see
// `renderCandidateReview` in main.js).
function rowText(title) {
  const list = elements.get("crossover-review-body").children[0];
  const row = list.children.find((child) => {
    const heading = child.children && child.children[0] && child.children[0].children[0];
    return heading && heading.textContent === title;
  });
  assert.ok(row, `a ${title} row rendered`);
  return row.children[0].children[1].textContent;
}

// Whether ANY row with this title rendered. `rowText` itself asserts a row
// exists, which is the wrong shape for the "no row at all" cases below —
// this is the reader those cases use instead.
function hasRow(title) {
  const list = elements.get("crossover-review-body").children[0];
  return list.children.some((child) => {
    const heading = child.children && child.children[0] && child.children[0].children[0];
    return heading && heading.textContent === title;
  });
}

// The title text of the row at a given position. Used only by the ordering
// case below, where WHICH row comes first is the entire point.
function rowTitleAt(index) {
  const list = elements.get("crossover-review-body").children[0];
  const row = list.children[index];
  const heading = row && row.children && row.children[0] && row.children[0].children &&
    row.children[0].children[0];
  return heading ? heading.textContent : null;
}

const PINNED_CROSSOVER = { fc_hz: 2400, order: 4, slope_db_per_octave: 24 };

// --- 1. A PINNED corner is worded as an instruction, not a measurement -----
render({
  ...baseEnvelope,
  candidate_review: candidateReview({
    crossover: PINNED_CROSSOVER,
    crossover_pinned: true,
  }),
});
check(rowText("Crossover") === "2400 Hz, 24 dB/octave (pinned for this round)",
  "a pinned crossover row names the corner, the slope, and the pin",
  { got: rowText("Crossover") });

// --- 2. The SAME corner, UNPINNED, carries no pin marker -------------------
// The control that makes the bit load-bearing: identical numbers, no pin —
// this is what fails if the renderer ever starts inventing the marker from
// the numbers alone instead of the bit beside them. Unlike the polarity row,
// the crossover row has no "measured" wording to begin with, so this only
// has to rule out an invented pin, not an invented measurement claim.
render({
  ...baseEnvelope,
  candidate_review: candidateReview({ crossover: PINNED_CROSSOVER }),
});
check(rowText("Crossover") === "2400 Hz, 24 dB/octave",
  "an unpinned crossover row is the bare numbers", { got: rowText("Crossover") });
check(!rowText("Crossover").includes("(pinned"),
  "an unpinned crossover row carries no pin marker", { got: rowText("Crossover") });
check(!/measured/i.test(rowText("Crossover")),
  "an unpinned crossover row never claims a measurement either",
  { got: rowText("Crossover") });

// --- 3. The Crossover row sits ABOVE the trims it governs ------------------
// main.js pushes it first "because it is the topology every row below sits
// inside" — the trim, delay, and polarity rows are all decisions made AT
// this corner. `candidateReview()` always carries one woofer trim.
render({
  ...baseEnvelope,
  candidate_review: candidateReview({ crossover: PINNED_CROSSOVER }),
});
check(rowTitleAt(0) === "Crossover",
  "the Crossover row renders first", { got: rowTitleAt(0) });
check(rowTitleAt(1) === "woofer level",
  "the first trim row follows the Crossover row", { got: rowTitleAt(1) });

// --- 4. No topology of its own: crossover: null renders no row -------------
// A candidate whose `crossover` field is absent or null did not depend on
// having measured a corner (e.g. a trims/delay/polarity-only candidate from
// a flow that never ran the Fc side). No row, and no exception — the render
// call below is itself the "does not throw" proof: an uncaught exception
// here would abort the whole harness before this check ever runs.
render({
  ...baseEnvelope,
  candidate_review: candidateReview({ crossover: null }),
});
check(!hasRow("Crossover"),
  "crossover: null renders no Crossover row", {});

// --- 5. The pinned BIT alone does not invent a corner -----------------------
// `crossover_pinned` says HOW a corner was decided, not THAT one exists.
render({
  ...baseEnvelope,
  candidate_review: candidateReview({ crossover: null, crossover_pinned: true }),
});
check(!hasRow("Crossover"),
  "crossover_pinned:true beside crossover:null still renders no row", {});

// --- 6. `crossover_pinned` is read `=== true`, nothing weaker --------------
// Exactly as strict as `polarityPinned`: a value that is truthy but not
// literally `true` must read like absent, not like pinned.
for (const value of ["yes", 1]) {
  render({
    ...baseEnvelope,
    candidate_review: candidateReview({
      crossover: PINNED_CROSSOVER,
      crossover_pinned: value,
    }),
  });
  const text = rowText("Crossover");
  check(text === "2400 Hz, 24 dB/octave",
    `crossover_pinned:${JSON.stringify(value)} reads exactly like unpinned`,
    { got: text });
  check(!text.includes("(pinned"),
    `crossover_pinned:${JSON.stringify(value)} never says pinned`,
    { got: text });
}

console.log(JSON.stringify({ ok: true, passed }));

// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// WHAT RUNS THIS: `tests/test_crossover_wizard_js.py`, which discovers
// `crossover_*_test.mjs` BY GLOB and executes each one through the pytest
// matrix — so this file is covered by CI the moment it exists, with no
// workflow edit. See `tests/js/crossover_polarity_provenance_test.mjs` for the
// full rationale behind that discovery mechanism.
//
// A driver prescription may PIN one driver's trim: the fitter re-solves every
// trim each round, so a filter chain carried over from an earlier round rides
// a level it was not shaped against unless the level travels with it. A pinned
// trim is therefore CARRIED, and the third thing on this screen that must never
// be worded as something the round measured. Unlike the pinned polarity and the
// pinned crossover, this one is per-ROLE — the bit rides each trim row — which
// is what this harness pins:
//   - a pinned trim row says "(pinned for this round)";
//   - the SAME level, unpinned, is the bare number;
//   - one pinned role does not mark the roles beside it; and
//   - `pinned` is read `=== true`, as strict as its two siblings, so a
//     truthy-but-not-boolean value can never invent the pinned wording.

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
  capture: null,
  next_action: null,
  alternate_actions: [],
};

let passed = 0;
function check(condition, message, context) {
  assert.ok(condition, context ? `${message} — got ${JSON.stringify(context)}` : message);
  passed += 1;
}

function candidateReview(trims) {
  return {
    trims,
    delay: { role: "woofer", delay_ms: 0.0375 },
    polarity: "keep",
    confidence: 0.71,
    fingerprint: "cand-proof",
    program_id: "prog-1",
  };
}

// Reads a row's meta text by its title — every measurement-row on this screen
// nests a title <p> and a meta <p> one div deep (see `renderCandidateReview`).
function rowText(title) {
  const list = elements.get("crossover-review-body").children[0];
  const row = list.children.find((child) => {
    const heading = child.children && child.children[0] && child.children[0].children[0];
    return heading && heading.textContent === title;
  });
  assert.ok(row, `a ${title} row rendered`);
  return row.children[0].children[1].textContent;
}

// --- 1. A PINNED trim is worded as an instruction, not a measurement -------
render({
  ...baseEnvelope,
  candidate_review: candidateReview([
    { role: "tweeter", attenuation_db: -7.25, pinned: true },
  ]),
});
check(rowText("tweeter level") === "-7.3 dB (pinned for this round)",
  "a pinned trim row names the level and the pin",
  { got: rowText("tweeter level") });

// --- 2. The SAME level, UNPINNED, carries no pin marker --------------------
// The control that makes the bit load-bearing: identical number, no pin. This
// is what fails if the renderer ever starts inventing the marker from the
// number alone instead of the bit beside it.
render({
  ...baseEnvelope,
  candidate_review: candidateReview([
    { role: "tweeter", attenuation_db: -7.25, pinned: false },
  ]),
});
check(rowText("tweeter level") === "-7.3 dB",
  "an unpinned trim row is the bare number", { got: rowText("tweeter level") });
check(!rowText("tweeter level").includes("(pinned"),
  "an unpinned trim row carries no pin marker", { got: rowText("tweeter level") });

// --- 3. The pin is PER ROLE and does not leak to the row beside it ---------
// The whole reason this bit rides the row rather than sitting flat beside the
// list: a round pins the transplanted driver and lets the other one re-solve.
render({
  ...baseEnvelope,
  candidate_review: candidateReview([
    { role: "woofer", attenuation_db: -2.5 },
    { role: "tweeter", attenuation_db: -7.25, pinned: true },
  ]),
});
check(rowText("tweeter level") === "-7.3 dB (pinned for this round)",
  "the pinned role keeps its marker beside an unpinned sibling",
  { got: rowText("tweeter level") });
check(rowText("woofer level") === "-2.5 dB",
  "the role the round solved is worded as an ordinary level",
  { got: rowText("woofer level") });

// --- 4. `pinned` is read `=== true`, nothing weaker ------------------------
// Exactly as strict as `polarityPinned` and `crossoverPinned`: a value that is
// truthy but not literally `true` must read like absent, not like pinned.
for (const value of ["yes", 1]) {
  render({
    ...baseEnvelope,
    candidate_review: candidateReview([
      { role: "tweeter", attenuation_db: -7.25, pinned: value },
    ]),
  });
  const text = rowText("tweeter level");
  check(text === "-7.3 dB",
    `pinned:${JSON.stringify(value)} reads exactly like unpinned`, { got: text });
  check(!text.includes("(pinned"),
    `pinned:${JSON.stringify(value)} never says pinned`, { got: text });
}

console.log(JSON.stringify({ ok: true, passed }));

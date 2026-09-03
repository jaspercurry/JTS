// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// WHAT RUNS THIS: `tests/test_crossover_wizard_js.py`, which discovers
// `crossover_*_test.mjs` BY GLOB (with a loud fail-on-empty assertion) and
// executes each one through the pytest matrix — so this file is covered by CI
// the moment it exists, with no workflow edit. It is deliberately NOT on the
// `js` job's hand-maintained list; that list is the drift the glob exists to
// prevent, and a reader checking only the workflow will wrongly conclude this
// harness never runs.
//
// #2607 (S3, hearing-safety/resilience lens): the review row rendered
// "Inverted (measured)" purely from the polarity string, so a candidate whose
// polarity the flow DECLINED to measure — the low-SNR path, which commits the
// polarity the preset declares — would have been worded to the household as a
// measured result. "Measured" is the one word a household reads as "we
// checked", and on that path nothing checked.
//
// This harness pins the cases renderCandidateReview()
// (deploy/assets/correction/js/crossover/main.js) has to tell apart:
//   - a measured inversion still says so;
//   - a measured keep still says so;
//   - a declared-design commitment says it was NOT checked, and never uses the
//     word "measured", whatever polarity string rides with it;
//   - and (§5, the basin pin) a round whose polarity the REQUEST held says so
//     in its own words — the same defect reopened by a new route, since a
//     pinned round commits an objective that is in neither list above.

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

function rowText(title) {
  const list = elements.get("crossover-review-body").children[0];
  const row = list.children.find((child) => {
    const heading = child.children && child.children[0] && child.children[0].children[0];
    return heading && heading.textContent === title;
  });
  assert.ok(row, `a ${title} row rendered`);
  return row.children[0].children[1].textContent;
}

function polarityRowText() {
  const list = elements.get("crossover-review-body").children[0];
  const row = list.children.find((child) => {
    const title = child.children && child.children[0] && child.children[0].children[0];
    return title && title.textContent === "Polarity";
  });
  assert.ok(row, "a Polarity row rendered");
  return row.children[0].children[1].textContent;
}

// --- 1. A MEASURED inversion still says it was measured -------------------
render({
  ...baseEnvelope,
  candidate_review: candidateReview({
    polarity: "invert",
    alignment_objective: "flat_sum_committed",
  }),
});
check(polarityRowText() === "Inverted (measured)",
  "a flat-sum inversion is still worded as measured", { got: polarityRowText() });

// --- 2. A MEASURED keep is unchanged --------------------------------------
render({
  ...baseEnvelope,
  candidate_review: candidateReview({
    polarity: "keep",
    alignment_objective: "flat_sum_committed",
  }),
});
check(polarityRowText() === "Kept as set",
  "a flat-sum keep is unchanged", { got: polarityRowText() });

// --- 3. A DECLARED-DESIGN commitment never claims a measurement -----------
// The low-SNR path always commits "keep", but the assertion is written against
// the OBJECTIVE and not the polarity string: the copy must be safe whatever
// polarity a future declared commitment carries.
// Every member of the refusal's objective set, not just the first one: the
// delay half of the same refusal commits a DIFFERENT objective when the
// speaker already carries an applied alignment to hold (#2617), and its
// polarity is just as unmeasured. A one-string check here is what would let
// that second commitment reach a household as "Inverted (measured)". #2662's
// prescribed delay is the fourth: the DELAY there was measured, on a bench,
// but the polarity beside it still was not — so it belongs in this loop and
// not in the measured one below.
for (const objective of [
  "declared_committed_after_low_snr",
  "applied_alignment_held_after_low_snr",
  "no_delay_committed_after_unreadable_apply",
  "explicit_prescription_held_after_low_snr",
]) {
  for (const polarity of ["keep", "invert"]) {
    render({
      ...baseEnvelope,
      candidate_review: candidateReview({
        polarity,
        alignment_objective: objective,
      }),
    });
    const text = polarityRowText();
    check(text === "As designed — this measurement could not check it",
      `${objective} (${polarity}) says it was not checked`,
      { got: text });
    check(!/measured/i.test(text),
      `${objective} (${polarity}) never says "measured"`,
      { got: text });
  }
}

// --- 3b. The DELAY row does not read as measured either (#2617 S-SF3) -----
// The refusal commits a delay this capture did not supply — the one the
// speaker already plays, or none. Rendering it as a bare number is the same
// "we checked" the polarity row was fixed for: a household reading
// "0.037 ms on the woofer" beside "this measurement could not check it" would
// take the number as the half that WAS measured.
for (const objective of [
  "declared_committed_after_low_snr",
  "applied_alignment_held_after_low_snr",
  "no_delay_committed_after_unreadable_apply",
  "explicit_prescription_held_after_low_snr",
]) {
  render({
    ...baseEnvelope,
    candidate_review: candidateReview({polarity: "keep", alignment_objective: objective}),
  });
  const text = rowText("Alignment delay");
  check(text === "0.037 ms on the woofer — kept as set, not measured this time",
    `${objective} says the delay was not measured this time`, { got: text });
}

// …and a MEASURED round's delay row is untouched.
render({
  ...baseEnvelope,
  candidate_review: candidateReview({
    polarity: "keep",
    alignment_objective: "flat_sum_committed",
  }),
});
check(rowText("Alignment delay") === "0.037 ms on the woofer",
  "a flat-sum delay row is unchanged", { got: rowText("Alignment delay") });

// --- 4. A payload with NO objective renders exactly as before -------------
// Older candidates carry no objective; the row must not change for them.
render({
  ...baseEnvelope,
  candidate_review: candidateReview({ polarity: "invert" }),
});
check(polarityRowText() === "Inverted (measured)",
  "a candidate with no objective renders unchanged", { got: polarityRowText() });

// --- 5. A PINNED basin is worded as an instruction, not a measurement -----
// The basin pin reopened #2607 S3 by a new route. A pinned round commits
// `explicit_prescription_committed`, which is in NEITHER list above — not the
// declared-design set, and not "no objective" — so both existing guards were
// structurally blind to it and the row rendered "Inverted (measured)" for a
// polarity nothing measured. The discriminator is the payload bit, so these
// cases differ from §1 only by `polarity_pinned`.
for (const [polarity, expected] of [
  ["invert", "Inverted (pinned for this round)"],
  ["keep", "Kept as set (pinned for this round)"],
]) {
  render({
    ...baseEnvelope,
    candidate_review: candidateReview({
      polarity,
      alignment_objective: "explicit_prescription_committed",
      polarity_pinned: true,
    }),
  });
  const text = polarityRowText();
  check(text === expected,
    `a pinned ${polarity} says it was pinned`, { got: text });
  check(!/measured/i.test(text),
    `a pinned ${polarity} never says "measured"`, { got: text });
}

// The overlap arm, and the reason the pin is tested BEFORE the design list:
// a pinned prescription on a capture the SNR verdict refused is in the
// declared-design set AND pinned. The pin is what actually shipped, so "as
// designed" would name the wrong author.
render({
  ...baseEnvelope,
  candidate_review: candidateReview({
    polarity: "invert",
    alignment_objective: "explicit_prescription_held_after_low_snr",
    polarity_pinned: true,
  }),
});
check(polarityRowText() === "Inverted (pinned for this round)",
  "a pinned low-SNR arm credits the pin, not the design",
  { got: polarityRowText() });

// The CONTROL that makes the bit load-bearing: the identical payload without
// it renders exactly as it did before this change. This is what fails if the
// renderer ever starts inferring the pin from the objective instead.
render({
  ...baseEnvelope,
  candidate_review: candidateReview({
    polarity: "invert",
    alignment_objective: "explicit_prescription_committed",
  }),
});
check(polarityRowText() === "Inverted (measured)",
  "an UNPINNED prescription is still worded as measured", { got: polarityRowText() });

// …and an explicit `false` is the same as absent, so a payload that always
// carries the key cannot change an unpinned round's copy.
render({
  ...baseEnvelope,
  candidate_review: candidateReview({
    polarity: "invert",
    alignment_objective: "explicit_prescription_committed",
    polarity_pinned: false,
  }),
});
check(polarityRowText() === "Inverted (measured)",
  "polarity_pinned:false reads exactly like absent", { got: polarityRowText() });

console.log(JSON.stringify({ ok: true, passed }));

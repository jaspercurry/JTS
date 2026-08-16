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
// This harness pins the three cases renderCandidateReview()
// (deploy/assets/correction/js/crossover/main.js) has to tell apart:
//   - a measured inversion still says so;
//   - a measured keep still says so;
//   - a declared-design commitment says it was NOT checked, and never uses the
//     word "measured", whatever polarity string rides with it.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function element(id = "") {
  return {
    id,
    children: [],
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    dataset: {},
    disabled: false,
    textContent: "",
    hidden: false,
    addEventListener() {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(key, value) { this[key] = String(value); },
  };
}

const ids = [
  "crossover-verdict",
  "crossover-applied",
  "crossover-start-over",
  "crossover-steps",
  "crossover-nudges",
  "crossover-review",
  "crossover-review-body",
  "crossover-action",
  "crossover-relay",
  "crossover-relay-status",
  "crossover-relay-link",
  "crossover-relay-qr",
  "crossover-relay-stop",
  "capture-status",
];
const elements = new Map(ids.map((id) => [id, element(id)]));
globalThis.document = {
  visibilityState: "visible",
  addEventListener() {},
  createElement: (tag) => element(tag),
  getElementById: (id) => elements.get(id),
};
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.__getJSON = async () => ({});
globalThis.__postJSON = async () => ({});
globalThis.__renderRelayQr = () => {};
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

const here = dirname(fileURLToPath(import.meta.url));
let source = readFileSync(
  resolve(here, "../../deploy/assets/correction/js/crossover/main.js"),
  "utf8",
);
source = source.replace(
  /^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm,
  "",
);
source =
  "const getJSON = globalThis.__getJSON; const postJSON = globalThis.__postJSON; " +
  "const renderRelayQr = globalThis.__renderRelayQr; " +
  "const renderCloud = globalThis.__renderCloud; " +
  "const redrawCloudChart = globalThis.__redrawCloudChart;\n" + source;
const bootStart = source.lastIndexOf("\nrefresh().catch((error) => {");
if (bootStart < 0) throw new Error("crossover module boot call not found");
source = source.slice(0, bootStart).concat("\nexport { render };\n");
const dataUrl =
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { render } = await import(dataUrl);

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
// that second commitment reach a household as "Inverted (measured)".
for (const objective of [
  "declared_committed_after_low_snr",
  "applied_alignment_held_after_low_snr",
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

// --- 4. A payload with NO objective renders exactly as before -------------
// Older candidates carry no objective; the row must not change for them.
render({
  ...baseEnvelope,
  candidate_review: candidateReview({ polarity: "invert" }),
});
check(polarityRowText() === "Inverted (measured)",
  "a candidate with no objective renders unchanged", { got: polarityRowText() });

console.log(JSON.stringify({ ok: true, passed }));

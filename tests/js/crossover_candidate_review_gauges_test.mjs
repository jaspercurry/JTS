// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Gauge fix (2026-07-24, S1 adversarial-review follow-up): the client render
// of the linearization-outcome and top-octave-deficit gauges was unpinned —
// the Python payload contract was tested, but nothing asserted what
// renderCandidateReview() (deploy/assets/correction/js/crossover/main.js)
// actually draws from it. This harness pins:
//   - the LINEARIZATION_OUTCOME_TEXT enum->prose mapping, for a "ran" value
//     ("fitted") and a "skipped" value ("ineligible_mic_tier");
//   - the per-role octave formatting ("8k -0.3 dB" shape) for a known
//     multi-band, multi-role payload;
//   - that a candidate_review payload WITHOUT either field (the shape
//     crossover_stop_render_test.mjs's own candidate_review test already
//     covers) renders byte-for-byte the same "Technical details" text as
//     before this gauge existed — additive fields must never perturb the
//     pre-existing disclosure for older/ineligible candidates.

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
  "const renderRelayQr = globalThis.__renderRelayQr;\n" + source;
const bootStart = source.lastIndexOf("\nrefresh().catch((error) => {");
if (bootStart < 0) throw new Error("crossover module boot call not found");
source = source.slice(0, bootStart).concat(
  "\nexport { render };\n",
);
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

// Same base shape crossover_stop_render_test.mjs's own candidate_review
// render uses (trims/delay/polarity/confidence/fingerprint/program_id, no
// linearization fields) — the "unchanged for an older/ineligible candidate"
// control every case below is compared against.
function baseCandidateReview(overrides) {
  return Object.assign({
    trims: [
      { role: "woofer", attenuation_db: -2.5 },
      { role: "tweeter", attenuation_db: 0 },
    ],
    delay: { role: "woofer", delay_ms: 0.0375 },
    polarity: "keep",
    confidence: 0.71,
    fingerprint: "cand-proof",
    program_id: "prog-1",
  }, overrides || {});
}

function technicalDetailsText() {
  const list = elements.get("crossover-review-body").children[0];
  const details = list && list.children.find((c) => c.className === "candidate-provenance");
  assert.ok(details, "a Technical details disclosure rendered");
  const summary = details.children[0];
  const paragraph = details.children[1];
  check(summary.textContent === "Technical details",
    "disclosure summary reads 'Technical details'", { got: summary.textContent });
  return paragraph.textContent;
}

// --- 1. WITHOUT the new fields: byte-for-byte the pre-gauge-fix text -------
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview(),
});
{
  const text = technicalDetailsText();
  check(text === "alignment confidence 0.71; candidate cand-proof.",
    "a candidate_review with no linearization fields renders unchanged",
    { got: text });
}

// --- 2. LINEARIZATION_OUTCOME_TEXT: a "ran" value + per-role octaves -------
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    linearization_outcome: "fitted",
    linearization_octaves: [
      {
        role: "woofer",
        bands: [
          { hz: 8000, delta_db: -0.3 },
          { hz: 12000, delta_db: -1.1 },
          { hz: 16000, delta_db: -4.7 },
        ],
      },
      { role: "tweeter", bands: [{ hz: 8000, delta_db: -0.1 }] },
    ],
  }),
});
{
  const text = technicalDetailsText();
  check(
    text === (
      "alignment confidence 0.71; candidate cand-proof; " +
      "driver linearization: fitted; " +
      "woofer measured vs fit target: 8k -0.3 dB, 12k -1.1 dB, 16k -4.7 dB; " +
      "tweeter measured vs fit target: 8k -0.1 dB."
    ),
    "'fitted' + a known octave payload render the exact enum prose and " +
    "the '8k -X.X dB' formatting per role",
    { got: text },
  );
}

// --- 3. LINEARIZATION_OUTCOME_TEXT: a "skipped" value, no octaves ----------
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    linearization_outcome: "ineligible_mic_tier",
  }),
});
{
  const text = technicalDetailsText();
  check(
    text === (
      "alignment confidence 0.71; candidate cand-proof; " +
      "driver linearization: skipped — needs a reference-tier mic."
    ),
    "a skip outcome renders its own enum prose and no octave line",
    { got: text },
  );
}

// --- 4. An unrecognized/empty outcome renders no linearization line at all -
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({ linearization_outcome: "" }),
});
{
  const text = technicalDetailsText();
  check(text === "alignment confidence 0.71; candidate cand-proof.",
    "an empty outcome (\"not evaluated\") renders no linearization line, " +
    "identical to the no-fields case",
    { got: text });
}

console.log(JSON.stringify({ ok: true, passed }));

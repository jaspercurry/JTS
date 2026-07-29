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
// PR-7's before/after visualization (./cloud.js) is out of scope for this
// harness — it only pins the candidate-review gauges — so a no-op stands
// in, same shape as the __renderRelayQr stub above.
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
      "woofer fit residual vs target (design-axis capture, not the spatial " +
      "measurement): 8k -0.3 dB, 12k -1.1 dB, 16k -4.7 dB; " +
      "tweeter fit residual vs target (design-axis capture, not the spatial " +
      "measurement): 8k -0.1 dB."
    ),
    "'fitted' + a known octave payload render the exact enum prose and " +
    "the '8k -X.X dB' formatting per role, under the flat-linearization " +
    "PR-5 label that names these as FIT diagnostics, not the measurement",
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

// --- 5. headroom_cost: the era-stamped level disclosure (two-stage D3.2) ---
// PR-T1 put `headroom_cost` on the candidate payload as a {db, basis}
// compound; nothing rendered it. The review screen is where D4's claim that
// this disclosure "lives on the browser-visible candidate summary" becomes
// true. Cases 1-4 above already prove the ABSENT case renders unchanged.
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    headroom_cost: { db: 5.2, basis: "realized_peak" },
  }),
});
{
  const text = technicalDetailsText();
  check(
    text === (
      "alignment confidence 0.71; candidate cand-proof; " +
      "costs 5.2 dB of maximum volume."
    ),
    "a current-era headroom cost renders as a plain level charge",
    { got: text },
  );
}

// The era trap, and the reason this is a compound rather than a float: the
// charge's derivation changed under #1808 and the stamp is NOT re-derived on
// load, so a pre-amendment candidate discloses ~22.5 dB where the same
// correction now costs ~5. Rendering that bare would put an order-of-magnitude
// wrong level cost on the one screen whose purpose is honesty.
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    headroom_cost: { db: 22.5, basis: "unknown" },
  }),
});
{
  const text = technicalDetailsText();
  check(
    text.includes("22.5 dB") && text.includes("no longer uses"),
    "a pre-amendment headroom cost NEVER renders bare — the number is stated " +
    "with the era that stamped it, and points at a re-measure",
    { got: text },
  );
}

// `db: null` is "we do not know", and zero is a real, common answer here
// (every cut-only correction charges nothing) — so an absent number must not
// render as a free correction.
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    headroom_cost: { db: null, basis: "unknown" },
  }),
});
{
  const text = technicalDetailsText();
  check(
    text === "alignment confidence 0.71; candidate cand-proof.",
    "an unknown headroom cost renders NO level line at all — never '0 dB', " +
    "which is a real measurement this payload does not have",
    { got: text },
  );
}

render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    headroom_cost: { db: 0, basis: "realized_peak" },
  }),
});
{
  const text = technicalDetailsText();
  check(
    text.includes("costs 0.0 dB of maximum volume"),
    "a genuine zero DOES render — a cut-only correction costing nothing is a " +
    "measured answer, not a missing one",
    { got: text },
  );
}

console.log(JSON.stringify({ ok: true, passed }));

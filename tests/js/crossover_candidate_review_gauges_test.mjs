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
// Audit item 4i (silent-deadends-gain-pointers) added a fourth case: an
// undeclared driver_class's LIMITED_BY_CLASS_PRIOR band gains one remedy
// sentence naming /sound/setup/, gated so an ALREADY-declared class's own
// real prior — or a candidate with no driver_class at all — never gets a
// remedy it cannot honestly attach.

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { CROSSOVER_IDS, installFixedDocument } from "./_dom.mjs";

const elements = installFixedDocument(CROSSOVER_IDS);
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

globalThis.__getJSON = async () => ({});
globalThis.__postJSON = async () => ({});
// PR-7's before/after visualization (./cloud.js) is out of scope for this
// harness — it only pins the candidate-review gauges — so a no-op stands in.
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

// --- 6. #2638: an out-of-band octave is NAMED, never numbered -------------
// The residual runs to 20 kHz. Past the woofer's own band the crossover
// target dives at 24 dB/oct while the measurement floor stays put, so the
// subtraction returns a large POSITIVE number — stopband arithmetic, not
// performance. On 2026-08-16 a healthy candidate (largest filter gain
// anywhere +2.5 dB) rendered "+23.0 dB" on this line and nearly got indicted
// for a runaway boost. The fit engine already labelled those octaves; this
// pins that the label changes what the household reads.
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    linearization_outcome: "fitted",
    linearization_octaves: [
      {
        role: "woofer",
        bands: [
          { hz: 8000, delta_db: -0.3, reason: "envelope_fitted" },
          { hz: 12000, delta_db: 4.1, reason: "envelope_out_of_band" },
          { hz: 16000, delta_db: 23.0, reason: "envelope_out_of_band" },
        ],
      },
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
      "measurement): 8k -0.3 dB; " +
      "woofer 12k, 16k: outside this driver’s band — not corrected."
    ),
    "an out-of-band octave's number never reaches the screen, and the octave " +
    "is still named — nothing hidden, no stopband arithmetic presented as " +
    "passband performance",
    { got: text },
  );
  check(
    !text.includes("23.0"),
    "the +23.0 dB stopband artifact from the 2026-08-16 candidate is gone " +
    "from the household row",
    { got: text },
  );
}

// A driver whose whole top ladder is out of band still discloses the octaves
// — the row must not vanish, or "past this driver's band" and "the fit never
// ran" become the same silence.
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    linearization_octaves: [
      {
        role: "woofer",
        bands: [
          { hz: 8000, delta_db: 12.0, reason: "envelope_out_of_band" },
          { hz: 16000, delta_db: 49.8, reason: "envelope_out_of_band" },
        ],
      },
    ],
  }),
});
{
  const text = technicalDetailsText();
  check(
    text === (
      "alignment confidence 0.71; candidate cand-proof; " +
      "woofer 8k, 16k: outside this driver’s band — not corrected."
    ),
    "an all-out-of-band role still names its octaves and prints no residual " +
    "line at all",
    { got: text },
  );
}

// Every OTHER reason code describes a band the driver does radiate, where the
// number is a real residual — those render exactly as case 2 does, including
// the beyond-confidence octaves of a fired CD-horn continuation stage.
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    linearization_octaves: [
      {
        role: "tweeter",
        bands: [
          { hz: 8000, delta_db: -0.1, reason: "envelope_fitted" },
          {
            hz: 16000,
            delta_db: -9.4,
            reason: "envelope_beyond_measurement_confidence",
          },
        ],
      },
    ],
  }),
});
{
  const text = technicalDetailsText();
  check(
    text === (
      "alignment confidence 0.71; candidate cand-proof; " +
      "tweeter fit residual vs target (design-axis capture, not the spatial " +
      "measurement): 8k -0.1 dB, 16k -9.4 dB."
    ),
    "a non-out-of-band reason code leaves the residual line untouched — the " +
    "fix is scoped to the one code whose number is not performance",
    { got: text },
  );
}

// --- 7. Audit item 4i: an undeclared driver_class gets a remedy pointer ---
// LIMITED_BY_CLASS_PRIOR still shows its number (unlike OUT_OF_BAND above,
// which suppresses it) — this is a real, measured residual, only capped by
// the class prior — and gains ONE extra sentence naming /sound/setup/.
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    linearization_outcome: "fitted",
    linearization_octaves: [
      {
        role: "tweeter",
        driver_class: "unknown",
        bands: [
          { hz: 8000, delta_db: -0.2, reason: "envelope_fitted" },
          { hz: 12000, delta_db: -6.5, reason: "envelope_limited_by_class_prior" },
          { hz: 16000, delta_db: -11.0, reason: "envelope_limited_by_class_prior" },
        ],
      },
    ],
  }),
});
{
  const text = technicalDetailsText();
  check(
    text === (
      "alignment confidence 0.71; candidate cand-proof; " +
      "driver linearization: fitted; " +
      "tweeter fit residual vs target (design-axis capture, not the spatial " +
      "measurement): 8k -0.2 dB, 12k -6.5 dB, 16k -11.0 dB; " +
      "tweeter: this driver's technology class is not declared, so " +
      "correction above this range is capped conservatively — declare it " +
      "at /sound/setup/ for a less conservative limit."
    ),
    "an undeclared driver_class keeps its residual numbers AND gains one " +
    "remedy sentence naming /sound/setup/",
    { got: text },
  );
}

// The correctness case: the SAME reason code fires for an ALREADY-declared
// class's own real prior, where the remedy would be FALSE — the household
// named the class, and there is nothing left to declare. No sentence must
// render, ever, for a real class value.
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    linearization_outcome: "fitted",
    linearization_octaves: [
      {
        role: "tweeter",
        driver_class: "soft_dome",
        bands: [
          { hz: 12000, delta_db: -6.5, reason: "envelope_limited_by_class_prior" },
        ],
      },
    ],
  }),
});
{
  const text = technicalDetailsText();
  check(
    text === (
      "alignment confidence 0.71; candidate cand-proof; " +
      "driver linearization: fitted; " +
      "tweeter fit residual vs target (design-axis capture, not the spatial " +
      "measurement): 12k -6.5 dB."
    ),
    "an ALREADY-declared driver_class never gets the remedy sentence — only " +
    "the undeclared default earns it",
    { got: text },
  );
  check(
    !text.includes("/sound/setup/"),
    "no /sound/setup/ pointer renders for a class the household already named",
    { got: text },
  );
}

// Absent driver_class (a pre-#4i candidate, or a role _candidate_summary
// never resolved a class for) must fail the SAME way as an unrecognized
// one — never rendering a remedy it cannot honestly attach.
render({
  ...baseEnvelope,
  candidate_review: baseCandidateReview({
    linearization_outcome: "fitted",
    linearization_octaves: [
      {
        role: "tweeter",
        bands: [
          { hz: 12000, delta_db: -6.5, reason: "envelope_limited_by_class_prior" },
        ],
      },
    ],
  }),
});
{
  const text = technicalDetailsText();
  check(
    !text.includes("/sound/setup/"),
    "no driver_class at all also renders no remedy sentence",
    { got: text },
  );
}

console.log(JSON.stringify({ ok: true, passed }));

// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Unit tests for the pure active-speaker level-match UI helpers.
//
// active-speaker-ui.js is a dependency-free ES module, so node can import it
// directly (no harness/DOM stubbing needed). Run via test_sound_setup.py.
import assert from "node:assert/strict";

import {
  CALIBRATED_ALIGNMENT_GUIDANCE,
  NEARFIELD_LEVEL_MATCH_GUIDANCE,
  commissionPayloadFailure,
  crossoverAlignmentSummary,
  levelMatchSummary,
  nearfieldCaptureHint,
} from "../../deploy/assets/sound-profile/js/active-speaker-ui.js";

// Measured override: each driver's trim is "Measured", config is not provisional.
{
  const s = levelMatchSummary({
    corrections: { woofer: { gain_db: 0 }, tweeter: { gain_db: -18 } },
    corrections_source: { woofer: "measured", tweeter: "measured" },
    provisional: false,
  });
  assert.equal(s.available, true);
  assert.equal(s.provisional, false);
  assert.equal(s.rows.length, 2);
  assert.equal(s.rows[0].role, "woofer");
  assert.equal(s.rows[1].role, "tweeter");
  assert.equal(s.rows[1].trimDb, -18);
  assert.equal(s.rows[1].sourceLabel, "Measured");
}

// Provisional datasheet fallback: tweeter trim flagged a datasheet estimate, and
// the near-field guidance is surfaced.
{
  const s = levelMatchSummary({
    corrections: { woofer: { gain_db: 0 }, tweeter: { gain_db: -25.2 } },
    corrections_source: { woofer: "none", tweeter: "sensitivity" },
    provisional: true,
  });
  assert.equal(s.provisional, true);
  assert.equal(s.rows[1].sourceLabel, "Datasheet estimate");
  assert.ok(s.guidance.includes("2–5 cm"));
  assert.ok(/datasheet estimates/i.test(s.note));
  // The datasheet fallback is framed as a fine, optional-to-improve state.
  assert.ok(/optional/i.test(s.note));
}

// Blocked / empty baseline payloads render nothing.
assert.equal(levelMatchSummary({}).available, false);
assert.equal(levelMatchSummary(null).available, false);
assert.equal(levelMatchSummary({ corrections: {} }).available, false);

// A measurement-in-progress refusal must NOT show the "another driver" message;
// it has its own distinct, actionable copy naming room correction / balance / sync.
{
  const measurementRefusal = commissionPayloadFailure({
    status: "refused",
    reason: "measurement_in_progress",
  });
  assert.ok(/room correction|balance|sync/i.test(measurementRefusal));
  assert.ok(!/another driver/i.test(measurementRefusal));
  // The pre-existing "another driver armed" refusal keeps its own message.
  const driverRefusal = commissionPayloadFailure({ status: "refused" });
  assert.ok(/another driver/i.test(driverRefusal));
}

// Near-field copy — the level match is OPTIONAL and the copy must say so.
assert.ok(nearfieldCaptureHint("Tweeter").includes("Tweeter"));
assert.ok(nearfieldCaptureHint("Tweeter").includes("2–5 cm"));
assert.ok(/optional/i.test(nearfieldCaptureHint("Tweeter")));
assert.ok(NEARFIELD_LEVEL_MATCH_GUIDANCE.includes("2–5 cm"));
assert.ok(/optional/i.test(NEARFIELD_LEVEL_MATCH_GUIDANCE));
assert.ok(/skip/i.test(NEARFIELD_LEVEL_MATCH_GUIDANCE));

// --- crossover alignment (L2) summary ---------------------------------------

// Authorized phase-aware proposal: horn tweeter later → delay the woofer; flat
// in-phase + deep reverse null → keep polarity.
{
  const s = crossoverAlignmentSummary({
    status: "ok",
    mode: { mode: "phase_aware", downgraded: false },
    proposal: {
      authorized: true,
      delay_ms: 0.6,
      delay_target_role: "woofer",
      delay_confidence: "estimate",
      polarity: "normal",
      polarity_action: "keep",
      in_phase_null_depth_db: 2,
      reverse_null_depth_db: 27,
      issues: [{ code: "delay_is_estimate", message: "validate with the null" }],
    },
  });
  assert.equal(s.available, true);
  assert.equal(s.authorized, true);
  assert.equal(s.needsCalibratedMic, false);
  assert.ok(/Woofer/.test(s.delayText) && /0\.60 ms/.test(s.delayText));
  assert.ok(/keep/i.test(s.polarityText));
  assert.ok(/in-phase 2 dB/.test(s.nullText) && /reverse 27 dB/.test(s.nullText));
  assert.equal(s.issues.length, 1);
}

// Downgraded (phone): proposal unauthorized → no delay/polarity, needs a cal mic.
{
  const s = crossoverAlignmentSummary({
    status: "ok",
    mode: { mode: "magnitude_only", downgraded: true, reason: "no_calibrated_mic" },
    proposal: {
      authorized: false,
      delay_ms: null,
      delay_target_role: null,
      polarity: "normal",
      polarity_action: "review",
      issues: [{ code: "requires_calibrated_mic", message: "needs a calibrated mic" }],
    },
  });
  assert.equal(s.authorized, false);
  assert.equal(s.needsCalibratedMic, true);
  assert.equal(s.delayText, "—");
  assert.equal(s.polarityText, "—");
  assert.ok(/calibrated measurement mic/i.test(s.note));
}

// Aligned (within jitter) reads as already time-aligned, not a delay value.
{
  const s = crossoverAlignmentSummary({
    mode: { mode: "phase_aware" },
    proposal: { authorized: true, delay_confidence: "aligned", delay_ms: 0,
      polarity_action: "keep" },
  });
  assert.ok(/time-aligned/i.test(s.delayText));
}

// No proposal yet → not available, with an actionable next-step note.
{
  const s = crossoverAlignmentSummary({ status: "no_measurements", proposal: null });
  assert.equal(s.available, false);
  assert.ok(/Measure each driver/i.test(s.note));
}
assert.equal(crossoverAlignmentSummary(null).available, false);
assert.ok(/calibrated measurement mic/i.test(CALIBRATED_ALIGNMENT_GUIDANCE));

console.log(JSON.stringify({ ok: true }));

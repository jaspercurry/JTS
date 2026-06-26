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
  DEFAULT_SUB_CROSSOVER_HZ,
  NEARFIELD_LEVEL_MATCH_GUIDANCE,
  SUB_CROSSOVER_HZ_HI,
  SUB_CROSSOVER_HZ_LO,
  activeSpeakerStepState,
  clampSubwooferCrossoverFcHz,
  commissionPayloadFailure,
  crossoverAlignmentSummary,
  defaultActiveSpeakerStep,
  levelMatchSummary,
  localSubwooferGroup,
  nearfieldCaptureHint,
  subwooferCrossoverBand,
  subwooferCrossoverFcHz,
  SUMMED_TEST_GENERIC_RETRY_HINT,
  summedGroupFailureHint,
} from "../../deploy/assets/sound-profile/js/active-speaker-ui.js";

// A saved topology whose current observed hardware no longer matches must stay
// on the layout step; later active-speaker actions remain unavailable.
{
  const ctx = {
    hasLayout: true,
    dirty: false,
    hardwareMatchesSaved: false,
    driverResearchSatisfied: true,
    outputIdentityComplete: true,
    driverChecksComplete: true,
    baselineProfileApplied: false,
  };
  assert.equal(defaultActiveSpeakerStep(ctx), "layout");
  assert.equal(activeSpeakerStepState("layout", ctx), "active");
  assert.equal(activeSpeakerStepState("research", ctx), "todo");
  assert.equal(activeSpeakerStepState("map", ctx), "todo");
  assert.equal(activeSpeakerStepState("safety", ctx), "todo");
  assert.equal(activeSpeakerStepState("profile", ctx), "todo");
}

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

// Stage-5 ordering has its own copy: it should not be described as an expired
// tone session, because the action is to confirm the lower-frequency driver.
{
  const roleOrder = commissionPayloadFailure({
    status: "gate_blocked",
    issues: [{ code: "stage5_ramp_role_order_woofer_first" }],
  });
  assert.ok(/woofer first/i.test(roleOrder));
  assert.ok(!/no longer open|expired/i.test(roleOrder));
}

// An expired pending ramp ack must invite a quiet restart, not imply the setup
// path is incomplete.
{
  const expiredAck = commissionPayloadFailure({
    status: "expired",
    issues: [{ code: "commission_ramp_ack_expired" }],
  });
  assert.ok(/start it again/i.test(expiredAck));
  assert.ok(/reopen it quietly/i.test(expiredAck));
  assert.ok(!/earlier setup/i.test(expiredAck));
}

// Ramp-step load failures wrap the actual backend load payload one level deeper
// than arm failures. The UI must still surface the specific output-path reason.
{
  const reconcileFailure = commissionPayloadFailure({
    status: "load_failed",
    issues: [{ code: "stage5_ramp_load_failed" }],
    load: {
      load: {
        status: "failed",
        issues: [{ code: "commission_output_hardware_reconcile_failed" }],
      },
    },
  });
  assert.ok(/speaker output path/i.test(reconcileFailure));
  assert.ok(!/earlier setup/i.test(reconcileFailure));
}

// Near-field copy — the level match is OPTIONAL and the copy must say so.
assert.ok(nearfieldCaptureHint("Tweeter").includes("Tweeter"));
assert.ok(nearfieldCaptureHint("Tweeter").includes("2–5 cm"));
assert.ok(/optional/i.test(nearfieldCaptureHint("Tweeter")));
assert.ok(NEARFIELD_LEVEL_MATCH_GUIDANCE.includes("2–5 cm"));
assert.ok(/optional/i.test(NEARFIELD_LEVEL_MATCH_GUIDANCE));
assert.ok(/skip/i.test(NEARFIELD_LEVEL_MATCH_GUIDANCE));

// --- crossover alignment (L2) summary ---------------------------------------

// Authorized phase-aware proposal: flat in-phase + deep reverse null → keep
// polarity; flat in-phase → delay status "aligned" (the value is the walk's job).
{
  const s = crossoverAlignmentSummary({
    status: "ok",
    mode: { mode: "phase_aware", downgraded: false },
    proposal: {
      authorized: true,
      polarity: "normal",
      polarity_action: "keep",
      polarity_margin_db: 25,
      delay_status: "aligned",
      in_phase_null_depth_db: 2,
      reverse_null_depth_db: 27,
      issues: [{ code: "reverse_null_not_captured", message: "flat sum" }],
    },
  });
  assert.equal(s.available, true);
  assert.equal(s.authorized, true);
  assert.equal(s.needsCalibratedMic, false);
  assert.ok(/time-aligned/i.test(s.delayText));
  assert.ok(/keep/i.test(s.polarityText));
  assert.ok(/in-phase 2 dB/.test(s.nullText) && /reverse 27 dB/.test(s.nullText));
  assert.equal(s.issues.length, 1);
}

// Deep in-phase null → delay status "needs_alignment" (run the walk).
{
  const s = crossoverAlignmentSummary({
    mode: { mode: "phase_aware" },
    proposal: {
      authorized: true,
      polarity: "invert_tweeter",
      polarity_action: "invert",
      delay_status: "needs_alignment",
      in_phase_null_depth_db: 18,
    },
  });
  assert.ok(/alignment walk/i.test(s.delayText));
  assert.ok(/Invert/i.test(s.polarityText));
}

// Downgraded (phone): proposal unauthorized → no polarity decision, needs a cal mic.
{
  const s = crossoverAlignmentSummary({
    status: "ok",
    mode: { mode: "magnitude_only", downgraded: true, reason: "no_calibrated_mic" },
    proposal: {
      authorized: false,
      polarity: "normal",
      polarity_action: "review",
      delay_status: "unknown",
      issues: [{ code: "requires_calibrated_mic", message: "needs a calibrated mic" }],
    },
  });
  assert.equal(s.authorized, false);
  assert.equal(s.needsCalibratedMic, true);
  assert.equal(s.delayText, "—");
  assert.equal(s.polarityText, "—");
  assert.ok(/calibrated measurement mic/i.test(s.note));
}

// No proposal yet → not available, with an actionable next-step note.
{
  const s = crossoverAlignmentSummary({ status: "no_measurements", proposal: null });
  assert.equal(s.available, false);
  assert.ok(/Measure each driver/i.test(s.note));
}
assert.equal(crossoverAlignmentSummary(null).available, false);
assert.ok(/calibrated measurement mic/i.test(CALIBRATED_ALIGNMENT_GUIDANCE));

// --- Local-subwoofer crossover helpers --------------------------------------
const STEREO_NO_SUB = {
  speaker_groups: [
    { id: "left", kind: "left", mode: "full_range_passive",
      channels: [{ role: "full_range", physical_output_index: 0 }] },
    { id: "right", kind: "right", mode: "full_range_passive",
      channels: [{ role: "full_range", physical_output_index: 1 }] },
  ],
};
const STEREO_WITH_SUB = {
  speaker_groups: STEREO_NO_SUB.speaker_groups.concat([
    { id: "sub", kind: "subwoofer", mode: "subwoofer",
      channels: [{ role: "subwoofer", physical_output_index: 2, crossover_fc_hz: 110 }] },
  ]),
};
const STEREO_WITH_SUB_UNSET_FC = {
  speaker_groups: STEREO_NO_SUB.speaker_groups.concat([
    { id: "sub", kind: "subwoofer", mode: "subwoofer",
      channels: [{ role: "subwoofer", physical_output_index: 2 }] },
  ]),
};

// No sub routed → no group, no called-out band, default Fc.
{
  assert.equal(localSubwooferGroup(STEREO_NO_SUB), null);
  assert.equal(subwooferCrossoverBand(STEREO_NO_SUB), null);
  assert.equal(subwooferCrossoverFcHz(STEREO_NO_SUB), DEFAULT_SUB_CROSSOVER_HZ);
  assert.equal(localSubwooferGroup(null), null);
  assert.equal(subwooferCrossoverBand(null), null);
  assert.equal(subwooferCrossoverBand(undefined), null);
}

// Sub present with an explicit Fc → a locked GAINLESS high-pass band at that Fc.
{
  assert.ok(localSubwooferGroup(STEREO_WITH_SUB));
  assert.equal(subwooferCrossoverFcHz(STEREO_WITH_SUB), 110);
  const band = subwooferCrossoverBand(STEREO_WITH_SUB);
  assert.ok(band);
  assert.equal(band.type, "Highpass"); // a GAINLESS_TYPES entry — no user gain term
  assert.equal(band.freq_hz, 110);
  assert.equal(band.gain_db, 0);
  assert.equal(band.systemManaged, true);
  assert.ok(/110\s*Hz/.test(band.detail));
  assert.ok(/subwoofer card/i.test(band.editedVia));
}

// Sub present but Fc unset → falls back to the shared default corner.
{
  assert.equal(subwooferCrossoverFcHz(STEREO_WITH_SUB_UNSET_FC), DEFAULT_SUB_CROSSOVER_HZ);
  const band = subwooferCrossoverBand(STEREO_WITH_SUB_UNSET_FC);
  assert.equal(band.freq_hz, DEFAULT_SUB_CROSSOVER_HZ);
}

// Clamp keeps the corner inside the safe bass-management band; blank → default.
{
  assert.equal(clampSubwooferCrossoverFcHz(""), DEFAULT_SUB_CROSSOVER_HZ);
  assert.equal(clampSubwooferCrossoverFcHz("not-a-number"), DEFAULT_SUB_CROSSOVER_HZ);
  assert.equal(clampSubwooferCrossoverFcHz(10), SUB_CROSSOVER_HZ_LO);
  assert.equal(clampSubwooferCrossoverFcHz(500), SUB_CROSSOVER_HZ_HI);
  assert.equal(clampSubwooferCrossoverFcHz(120), 120);
  // An out-of-range stored value is normalized when surfaced as the band.
  const hot = subwooferCrossoverBand({
    speaker_groups: [
      { id: "sub", kind: "subwoofer", mode: "subwoofer",
        channels: [{ role: "subwoofer", physical_output_index: 2, crossover_fc_hz: 999 }] },
    ],
  });
  assert.equal(hot.freq_hz, SUB_CROSSOVER_HZ_HI);
}

// --- C3a-1: backend owns the combined-test failure copy ---------------------
//
// summedGroupFailureHint renders the backend groupView.failure_message verbatim
// (the per-failure-code ladder lives in the Python coordinator, not the browser).
// The single generic string is ONLY the degraded fallback when the view is
// unavailable. This pins that the browser never re-derives a parallel per-code
// ladder again (the "to retry"/"to try again" drift this replaced).
{
  // Backend view present -> its failure_message is authoritative (verbatim).
  assert.equal(
    summedGroupFailureHint({ failure_message: "Re-check Confirm outputs before retrying." }),
    "Re-check Confirm outputs before retrying.",
  );
  // Backend view present with no failure -> empty (nothing to report).
  assert.equal(summedGroupFailureHint({ failure_message: "" }), "");
  assert.equal(summedGroupFailureHint({}), "");
  // Audible test exists -> suppressed regardless of any stale message.
  assert.equal(
    summedGroupFailureHint({ failure_message: "stale" }, { suppress: true }),
    "",
  );
  // Degraded: no backend view -> the ONE generic fallback line, not a ladder.
  assert.equal(summedGroupFailureHint(null), SUMMED_TEST_GENERIC_RETRY_HINT);
  assert.equal(summedGroupFailureHint(undefined), SUMMED_TEST_GENERIC_RETRY_HINT);
  assert.equal(summedGroupFailureHint(null, { suppress: true }), "");
}

console.log(JSON.stringify({ ok: true }));

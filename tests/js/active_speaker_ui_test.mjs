// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Unit tests for the pure active-speaker level-match UI helpers.
//
// active-speaker-ui.js is a dependency-free ES module, so node can import it
// directly (no harness/DOM stubbing needed). Run via test_sound_setup.py.
import assert from "node:assert/strict";

import {
  DEFAULT_SUB_CROSSOVER_HZ,
  NEARFIELD_LEVEL_MATCH_GUIDANCE,
  SUB_CROSSOVER_HZ_HI,
  SUB_CROSSOVER_HZ_LO,
  activeSpeakerStepState,
  clampSubwooferCrossoverFcHz,
  commissionPayloadFailure,
  commissioningStepFooter,
  defaultActiveSpeakerStep,
  levelMatchSummary,
  localSubwooferGroup,
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

// One rung at a time. This client-side ladder is the fallback main.js uses
// while a draft is being edited (driverResearch.dirty short-circuits the
// backend view even when the saved topology is clean), so it is what a
// household sees on exactly the screen where the jts5 stuck state was found:
// outputs and drivers already confirmed, crossover values not yet re-saved.
// Each rung used to answer "am I active?" from its own predicate alone, so
// `research` and `safety` both came back active and the combined-test card
// three rungs down invited a click the graph could not honour.
{
  const ctx = {
    hasLayout: true,
    dirty: false,
    hardwareMatchesSaved: true,
    driverResearchSatisfied: false,
    outputIdentityComplete: true,
    driverChecksComplete: true,
    driverTargetProofComplete: true,
    summedValidationComplete: false,
    baselineProfileApplied: false,
  };
  const states = ["layout", "research", "map", "safety", "profile"]
    .map((step) => [step, activeSpeakerStepState(step, ctx)]);
  assert.deepEqual(
    states.filter(([, state]) => state === "active").map(([step]) => step),
    ["research"],
    `exactly one rung may be active: ${JSON.stringify(states)}`
  );
  assert.equal(activeSpeakerStepState("safety", ctx), "todo");
  assert.equal(defaultActiveSpeakerStep(ctx), "research");
}

// The same ladder, once the values ARE saved: the baton moves on rather than
// being held by two rungs. Mutation guard for the collapse above — restore any
// rung's independent `active` branch and the previous block fails.
{
  const ctx = {
    hasLayout: true,
    dirty: false,
    hardwareMatchesSaved: true,
    driverResearchSatisfied: true,
    outputIdentityComplete: true,
    driverChecksComplete: true,
    driverTargetProofComplete: true,
    summedValidationComplete: false,
    baselineProfileApplied: false,
  };
  const states = ["layout", "research", "map", "safety", "profile"]
    .map((step) => [step, activeSpeakerStepState(step, ctx)]);
  assert.deepEqual(
    states.filter(([, state]) => state === "active").map(([step]) => step),
    ["safety"],
    `the combined test is the one live rung: ${JSON.stringify(states)}`
  );
  assert.equal(activeSpeakerStepState("research", ctx), "done");
  assert.equal(activeSpeakerStepState("map", ctx), "done");
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
// the level-match guidance is surfaced — the pointer to where measuring happens.
{
  const s = levelMatchSummary({
    corrections: { woofer: { gain_db: 0 }, tweeter: { gain_db: -25.2 } },
    corrections_source: { woofer: "none", tweeter: "sensitivity" },
    provisional: true,
  });
  assert.equal(s.provisional, true);
  assert.equal(s.rows[1].sourceLabel, "Datasheet estimate");
  assert.ok(s.guidance.includes("jts.local/sound/speaker/crossover"));
  assert.ok(/safe starting estimates/i.test(s.note));
  assert.ok(/not acoustic measurements/i.test(s.note));
}

// Blocked / empty baseline payloads render nothing.
assert.equal(levelMatchSummary({}).available, false);
assert.equal(levelMatchSummary(null).available, false);
assert.equal(levelMatchSummary({ corrections: {} }).available, false);

{
  const s = levelMatchSummary({
    corrections: { woofer: { gain_db: 0 }, tweeter: { gain_db: -11 } },
    corrections_source: { woofer: "operator_pinned", tweeter: "operator_pinned" },
    provisional: false,
  });
  assert.equal(s.badge, "manual");
  assert.ok(/valid for room correction/i.test(s.note));
  assert.ok(/explicit apply/i.test(s.note));
}

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

// A heard driver is not a successful confirmation unless the backend saved the
// output identity proof.
{
  const identitySaveFailure = commissionPayloadFailure({
    status: "failed",
    issues: [{ code: "driver_target_identity_save_failed" }],
  });
  assert.ok(/could not save/i.test(identitySaveFailure));
  assert.ok(/output confirmation/i.test(identitySaveFailure));
}

// Level-match copy — manual and automatic are both valid ownership paths. An
// automatic result cannot silently overwrite manual pins.
assert.ok(/safe applied manual crossover/i.test(NEARFIELD_LEVEL_MATCH_GUIDANCE));
assert.ok(/explicitly apply/i.test(NEARFIELD_LEVEL_MATCH_GUIDANCE));

// ...and it must POINT at the experience that can actually measure. /sound/ is
// plain HTTP, so it has no getUserMedia and no recorder in this bundle; the
// capture lives on the HTTPS /sound/speaker/crossover/ page. BOTH halves are
// pinned: the typeable path (the host alone lands nowhere useful) and the
// destination's label, which correction_hub.SECTIONS owns.
assert.ok(
  NEARFIELD_LEVEL_MATCH_GUIDANCE.includes("jts.local/sound/speaker/crossover"),
);
assert.ok(NEARFIELD_LEVEL_MATCH_GUIDANCE.includes("Active speaker"));

// It must NOT carry a microphone-placement instruction. The canonical capture
// geometry is owned by jasper/active_speaker/capture_geometry.py and rendered
// by the capture page; a copy here is a second owner that silently goes stale
// — which is exactly what happened to the "2–5 cm" sentence this guard
// replaced: the canonical geometry had already narrowed that range to one
// fixed value (DRIVER_PLACEMENT_TARGET_CM). These two negatives fail if that
// sentence comes back, or any distance figure in any casing or spelled-out
// unit ("3 cm", "3 CM", "three centimetres", inches).
assert.ok(!/hold the microphone/i.test(NEARFIELD_LEVEL_MATCH_GUIDANCE));
assert.ok(!/\d\s*cm|centimet|\binch/i.test(NEARFIELD_LEVEL_MATCH_GUIDANCE));

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

// --- commissioningStepFooter: backend view-model on the clean path ----------
//
// The research/map step footers used to re-derive readiness in the browser
// (driverResearchStepSatisfied / crossoverPreviewReadyForProtectedStaging /
// outputIdentityComplete). commissioningStepFooter consumes the backend
// commissioning view-model on a CLEAN draft (`source:'backend'`) and only falls
// back to the client descriptor for the dirty/saving cases the backend cannot
// see (`source:'client'`). These tests pin that boundary.

// Backend next_action == save the design draft -> footer renders that, verbatim
// label, from the backend (not a client-derived "Save values").
{
  const f = commissioningStepFooter(
    "research",
    { next_action: { id: "save_driver_values", label: "Save values",
      endpoint: "./active-speaker/design-draft", enabled: true } },
    { previewInputsReady: true },
  );
  assert.equal(f.source, "backend");
  assert.equal(f.act, "save-driver-design");
  assert.equal(f.label, "Save values");
  assert.equal(f.disabled, false);
}

// Backend next_action == preview the crossover -> footer renders that. The LABEL
// is backend; the ENABLED state is refined by the client preview-inputs signal
// (the one documented divergence the browser keeps owning).
{
  const ready = commissioningStepFooter(
    "research",
    { next_action: { id: "preview_crossover", label: "Preview crossover",
      endpoint: "./active-speaker/crossover-preview", enabled: true } },
    { previewInputsReady: true },
  );
  assert.equal(ready.source, "backend");
  assert.equal(ready.act, "prepare-crossover-preview");
  assert.equal(ready.label, "Preview crossover");
  assert.equal(ready.disabled, false);

  const missingInputs = commissioningStepFooter(
    "research",
    { next_action: { id: "preview_crossover", label: "Preview crossover",
      endpoint: "./active-speaker/crossover-preview", enabled: true } },
    { previewInputsReady: false },
  );
  // Backend says enabled; the client keeps it disabled until inputs exist.
  assert.equal(missingInputs.source, "backend");
  assert.equal(missingInputs.disabled, true);
  assert.equal(missingInputs.label, "Preview crossover");
}

// Backend next_action points PAST the research step (confirm outputs etc.) ->
// the saved design+preview are complete, so the footer advances ("Continue").
{
  const f = commissioningStepFooter(
    "research",
    { next_action: { id: "confirm_outputs", label: "Confirm outputs",
      method: "GET", enabled: true } },
    { previewInputsReady: true },
  );
  assert.equal(f.source, "backend");
  assert.equal(f.act, "output-step-next");
  assert.equal(f.step, "research");
  assert.equal(f.label, "Continue");
}

// Dirty / saving draft -> the backend (which only reads saved state) cannot
// reflect the unsaved edit, so the client fallback is used verbatim.
{
  const saving = commissioningStepFooter(
    "research",
    { next_action: { id: "preview_crossover", label: "Preview crossover",
      endpoint: "./active-speaker/crossover-preview", enabled: true } },
    { saving: true, previewInputsReady: true,
      clientFallback: { label: "Saving", disabled: true } },
  );
  assert.equal(saving.source, "client");
  assert.equal(saving.label, "Saving");
  assert.equal(saving.disabled, true);

  const draftDirty = commissioningStepFooter(
    "research",
    { next_action: { id: "preview_crossover", label: "Preview crossover",
      endpoint: "./active-speaker/crossover-preview", enabled: true } },
    { draftDirty: true, previewInputsReady: true,
      clientFallback: { label: "Save values", act: "save-driver-design" } },
  );
  assert.equal(draftDirty.source, "client");
  assert.equal(draftDirty.act, "save-driver-design");

  const layoutDirty = commissioningStepFooter(
    "research",
    { next_action: { id: "save_driver_values", label: "Save values",
      endpoint: "./active-speaker/design-draft", enabled: true } },
    { layoutDirty: true, previewInputsReady: true,
      clientFallback: { label: "Save layout first", disabled: true } },
  );
  assert.equal(layoutDirty.source, "client");
  assert.equal(layoutDirty.label, "Save layout first");
}

// No view yet (pre-first-load) -> client fallback, never a guessed backend path.
{
  const f = commissioningStepFooter("research", null, {
    clientFallback: { label: "Save values", act: "save-driver-design" },
  });
  assert.equal(f.source, "client");
  assert.equal(f.act, "save-driver-design");
}

// Map step: readiness comes from the backend driver_target_proof.complete signal.
{
  const incomplete = commissioningStepFooter(
    "map",
    { driver_target_proof: { complete: false }, next_action: {} },
    {},
  );
  assert.equal(incomplete.source, "backend");
  assert.equal(incomplete.label, "Confirm drivers");
  assert.equal(incomplete.disabled, true);
  assert.equal(incomplete.act, ""); // waiting affordance: confirm in the card

  const complete = commissioningStepFooter(
    "map",
    { driver_target_proof: { complete: true }, next_action: {} },
    {},
  );
  assert.equal(complete.source, "backend");
  assert.equal(complete.act, "output-step-next");
  assert.equal(complete.step, "map");
  assert.equal(complete.label, "Continue");

  // Dirty layout -> client "Save" fallback (the map card owns the save).
  const dirty = commissioningStepFooter(
    "map",
    { driver_target_proof: { complete: true } },
    { layoutDirty: true,
      clientFallback: { label: "Save", act: "save-output-topology" } },
  );
  assert.equal(dirty.source, "client");
  assert.equal(dirty.act, "save-output-topology");
  assert.equal(dirty.label, "Save");
}

// #2344, re-pointed by #2412 Wave 3 — the ring refusal is retired, and what has
// to REACH the household now is the two arming states and the ends-disagree
// defect, from every array the backend can park them in. Behavioural, not a
// substring check on the source: a rung whose code is misspelled still contains
// the code as a substring, and a walker that dropped an array still mentions it
// in a comment.
{
  const FEED = "commissioning_ring_feed_unarmed";
  const ENDPOINT = "commissioning_active_endpoint_unarmed";
  const ENDS = "commissioning_transport_ends_disagree";
  const WIRE = "ring_wire_declaration_invalid";
  const RETIRED = "commissioning_ring_transport_unsupported";
  const unarmed =
    "This speaker’s output path isn’t finished setting up, so driver tests " +
    "can’t run yet. Open System status.";
  const ends =
    "JTS could not prepare the driver test for this speaker’s output " +
    "connection. Open System status.";
  const wire =
    "This speaker’s output connection is set to something JTS doesn’t " +
    "recognise, so driver tests can’t run. Open System status.";

  // The blocked driver-test payload: the preflight's issues are copied into
  // `load.issues` by load_driver_commissioning_config. Both arming codes reach
  // the same sentence because the household ACTION is the same for both; the
  // two operator remedies differ and belong to two different reconcilers.
  for (const code of [FEED, ENDPOINT]) {
    assert.equal(
      commissionPayloadFailure({ status: "blocked", load: { issues: [{ code }] } }),
      unarmed,
    );
  }
  assert.equal(
    commissionPayloadFailure({ status: "blocked", load: { issues: [{ code: ENDS }] } }),
    ends,
  );
  // Mapped at the gate lift (#2412 correction 4): it was mapped in the Python
  // coordinator and absent from this ladder, so the household fell through to
  // written copy while the operator's daemon name sat one code away.
  assert.equal(
    commissionPayloadFailure({ status: "blocked", load: { issues: [{ code: WIRE }] } }),
    wire,
  );

  // A blocked RAMP step reports `ramp_prepare_failed` at the top level and parks
  // the reason that explains it in a sibling array. Before #2344 the walker never
  // read that array, so the household got the generic sentence.
  assert.equal(
    commissionPayloadFailure({
      status: "blocked",
      issues: [{ code: "ramp_prepare_failed" }],
      prepare_issues: [{ code: FEED }],
    }),
    unarmed,
  );

  // They outrank step-level advice: while the output path is unfinished, "start
  // the tone again" is true-but-useless for something a retry cannot fix.
  assert.equal(
    commissionPayloadFailure({
      status: "blocked",
      issues: [{ code: "commission_not_loaded" }, { code: ENDPOINT }],
    }),
    unarmed,
  );

  // THE RETIRED RUNG IS ASSERTED ABSENT, behaviourally: it no longer produces
  // copy of its own. Asserting the new rungs present is only half a re-point.
  const retiredOnly = commissionPayloadFailure({
    status: "blocked",
    load: { issues: [{ code: RETIRED }] },
  });
  assert.equal(
    retiredOnly,
    "This driver can’t be tested yet — finish the earlier setup steps first.",
  );
  assert.ok(!retiredOnly.includes("ring output mode"));

  // No household surface may carry an operator's shell command — not the
  // retired `baseline-reemit`, and not either new reconciler invocation.
  for (const copy of [unarmed, ends, wire]) {
    assert.ok(!copy.includes("baseline-reemit"));
    assert.ok(!copy.includes("jasper-"));
    assert.ok(!copy.includes("systemctl"));
    assert.ok(!copy.includes("sudo"));
  }

  // The gate path: when no issue code matched, the preflight gates are what the
  // renderer falls back to, and neither may degrade to the generic sentence.
  for (const [id, marker] of [
    ["commissioning_transport_supported", "output connection"],
    ["commissioning_transport_armed", "output path isn’t finished"],
  ]) {
    const gateOnly = commissionPayloadFailure({
      status: "blocked",
      preflight: { required_gates: [{ id, passed: false }] },
    });
    assert.ok(
      gateOnly.includes(marker) && !gateOnly.includes("finish the earlier setup"),
      `${id} rendered the generic fallback: ${gateOnly}`,
    );
    assert.ok(!gateOnly.includes("ring output mode"));
    assert.ok(!gateOnly.includes("baseline-reemit"));
  }
}

console.log(JSON.stringify({ ok: true }));

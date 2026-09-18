// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Run: node tests/js/active_speaker_ui_test.mjs
import assert from "node:assert/strict";

const bounds = {default_hz: 85, lo_hz: 55, hi_hz: 185};
globalThis.document = {getElementById: id => id === 'jts-sub-crossover-bounds'
  ? {textContent: JSON.stringify(bounds)} : null};
const {
  activeSpeakerStepState,
  clampSubwooferCrossoverFcHz,
  commissionPayloadFailure,
  nextActionAct,
  defaultActiveSpeakerStep,
  levelMatchSummary,
  localSubwooferGroup,
  subwooferCrossoverBand,
  subwooferCrossoverFcHz,
} = await import("../../deploy/assets/sound-profile/js/active-speaker-ui.js");

for (const dirty of [false, true]) {
  const ctx = {
    hasLayout: true, dirty, driverResearchSatisfied: true,
    currentStep: "experiment",
    steps: [
      {id: "layout", status: "done"}, {id: "research", status: "done"},
      {id: "experiment", status: "active"}, {id: "profile", status: "todo"},
    ],
  };
  assert.equal(defaultActiveSpeakerStep(ctx), dirty ? "layout" : "experiment");
  for (const step of ctx.steps) {
    assert.equal(activeSpeakerStepState(step.id, ctx),
      dirty ? (step.id === "layout" ? "active" : "todo") : step.status);
  }
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
  assert.equal(subwooferCrossoverFcHz(STEREO_NO_SUB), bounds.default_hz);
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
  assert.equal(subwooferCrossoverFcHz(STEREO_WITH_SUB_UNSET_FC), bounds.default_hz);
  const band = subwooferCrossoverBand(STEREO_WITH_SUB_UNSET_FC);
  assert.equal(band.freq_hz, bounds.default_hz);
}

// Clamp keeps the corner inside the safe bass-management band; blank → default.
{
  assert.equal(clampSubwooferCrossoverFcHz(""), bounds.default_hz);
  assert.equal(clampSubwooferCrossoverFcHz("not-a-number"), bounds.default_hz);
  assert.equal(clampSubwooferCrossoverFcHz(50), bounds.lo_hz);
  assert.equal(clampSubwooferCrossoverFcHz(190), bounds.hi_hz);
  assert.equal(clampSubwooferCrossoverFcHz(120), 120);
  // An out-of-range stored value is normalized when surfaced as the band.
  const hot = subwooferCrossoverBand({
    speaker_groups: [
      { id: "sub", kind: "subwoofer", mode: "subwoofer",
        channels: [{ role: "subwoofer", physical_output_index: 2, crossover_fc_hz: 999 }] },
    ],
  });
  assert.equal(hot.freq_hz, bounds.hi_hz);
}

for (const [id, act, step] of [
  ['declare_speaker', 'open-output-layout', 'layout'],
  ['save_driver_values', 'save-driver-design', 'research'],
  ['apply_candidate', 'save-apply-baseline-profile', 'profile'],
  ['copy_prompt', 'copy-tuning-handoff', ''],
  ['run_speaker_program', '', 'experiment'],
  ['run_program', '', 'experiment'],
]) {
  for (const program of ['speaker', 'room', 'bass']) {
    const behavior = nextActionAct({id, program});
    assert.equal(behavior.act, act);
    assert.equal(behavior.step, step);
    if (id.startsWith('run_')) {
      assert.equal(behavior.command, 'sudo /opt/jasper/.venv/bin/jasper-round run --program ' + program);
    }
    if (id === 'copy_prompt') assert.equal(behavior.program, program);
  }
}
assert.equal(nextActionAct({id: 'run_speaker_program'}).program, 'speaker');

// #2344, re-pointed by #2412 Wave 3 — the ring refusal is retired, and what has
// to REACH the household now is the arming state and the ends-disagree defect,
// from every array the backend can park them in. Behavioural, not a substring
// check on the source: a rung whose code is misspelled still contains the code
// as a substring, and a walker that dropped an array still mentions it in a
// comment.
{
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
  // `load.issues` by load_driver_commissioning_config.
  assert.equal(
    commissionPayloadFailure({
      status: "blocked",
      load: { issues: [{ code: ENDPOINT }] },
    }),
    unarmed,
  );
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
      prepare_issues: [{ code: ENDPOINT }],
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

globalThis.document = {getElementById: () => null};
const missingBounds = await import("../../deploy/assets/sound-profile/js/active-speaker-ui.js?missing-bounds");
assert.throws(() => missingBounds.clampSubwooferCrossoverFcHz(100), /jts-sub-crossover-bounds/);
assert.equal(clampSubwooferCrossoverFcHz(50), bounds.lo_hz);

console.log(JSON.stringify({ ok: true }));

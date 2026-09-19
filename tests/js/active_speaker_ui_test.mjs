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

globalThis.document = {getElementById: () => null};
const missingBounds = await import("../../deploy/assets/sound-profile/js/active-speaker-ui.js?missing-bounds");
assert.throws(() => missingBounds.clampSubwooferCrossoverFcHz(100), /jts-sub-crossover-bounds/);
assert.equal(clampSubwooferCrossoverFcHz(50), bounds.lo_hz);

console.log(JSON.stringify({ ok: true }));

// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Harness for realized-constraints verify + per-kind decision (relay step 6).
//
// Proves measurement validity is LOUD (plan §9): EC/AGC/NS silently left on is
// caught from track.getSettings(), and the per-kind policy decides refuse vs
// labeled-degrade vs proceed — with the iOS device-capability fallback that
// never dead-ends an iPhone. Prints {"ok":true}.
//
//   node tests/js/capture_constraints_test.mjs

import assert from "node:assert/strict";

import {
  verifyRealizedConstraints,
  constraintDecision,
} from "../../capture-page/js/constraints.js";

let passed = 0;
function ok() {
  passed += 1;
}

const cleanWanted = {
  constraints: {
    echoCancellation: false,
    autoGainControl: false,
    noiseSuppression: false,
  },
  sample_rate_hz: 48000,
  channels: 1,
};

function refuseSpec(fallback) {
  return {
    ...cleanWanted,
    validity: { clean_capture: "refuse", allow_capability_fallback: fallback },
  };
}

function testCleanProceeds() {
  const realized = verifyRealizedConstraints(
    {
      echoCancellation: false,
      autoGainControl: false,
      noiseSuppression: false,
      sampleRate: 48000,
      channelCount: 1,
    },
    cleanWanted,
  );
  assert.equal(realized.clean, true);
  assert.deepEqual(constraintDecision(realized, refuseSpec(true)), {
    action: "proceed",
    degraded: false,
    reason: "",
  });
  ok();
}

function testEcStillOnIsDirty() {
  const realized = verifyRealizedConstraints(
    { echoCancellation: true, sampleRate: 48000, channelCount: 1 },
    cleanWanted,
  );
  assert.equal(realized.clean, false);
  assert.deepEqual(realized.dirtyFlags, ["echoCancellation"]);
  ok();
}

function testRefuseWithFallbackDegradesNeverDeadEnds() {
  // The §9 iOS path: refuse policy + a device that ignored the flag -> labeled
  // degrade, NOT a dead-end refuse.
  const realized = verifyRealizedConstraints(
    { echoCancellation: true, sampleRate: 48000, channelCount: 1 },
    cleanWanted,
  );
  const decision = constraintDecision(realized, refuseSpec(true));
  assert.equal(decision.action, "degrade");
  assert.equal(decision.degraded, true);
  assert.match(decision.reason, /echoCancellation/);
  ok();
}

function testRefuseWithoutFallbackRefuses() {
  const realized = verifyRealizedConstraints(
    { noiseSuppression: true, sampleRate: 48000, channelCount: 1 },
    cleanWanted,
  );
  const decision = constraintDecision(realized, refuseSpec(false));
  assert.equal(decision.action, "refuse");
  assert.equal(decision.degraded, false);
  ok();
}

function testWarnPolicyProceedsLabeled() {
  const realized = verifyRealizedConstraints(
    { autoGainControl: true, sampleRate: 48000, channelCount: 1 },
    cleanWanted,
  );
  const decision = constraintDecision(realized, {
    ...cleanWanted,
    validity: { clean_capture: "warn", allow_capability_fallback: true },
  });
  assert.equal(decision.action, "proceed");
  assert.equal(decision.degraded, true);
  ok();
}

function testSampleRateAndChannelMismatchAreDirty() {
  const rate = verifyRealizedConstraints(
    { sampleRate: 44100, channelCount: 1 },
    cleanWanted,
  );
  assert.equal(rate.sampleRateOk, false);
  assert.equal(rate.clean, false);
  const ch = verifyRealizedConstraints(
    { sampleRate: 48000, channelCount: 2 },
    cleanWanted,
  );
  assert.equal(ch.channelsOk, false);
  ok();
}

function testMissingSettingsAreTolerated() {
  // Browsers that do not report sampleRate/channelCount must not be treated as
  // dirty on those axes (only the processing flags are decisive there).
  const realized = verifyRealizedConstraints(
    { echoCancellation: false, autoGainControl: false, noiseSuppression: false },
    cleanWanted,
  );
  assert.equal(realized.clean, true);
  ok();
}

const tests = [
  testCleanProceeds,
  testEcStillOnIsDirty,
  testRefuseWithFallbackDegradesNeverDeadEnds,
  testRefuseWithoutFallbackRefuses,
  testWarnPolicyProceedsLabeled,
  testSampleRateAndChannelMismatchAreDirty,
  testMissingSettingsAreTolerated,
];

let failure = null;
for (const t of tests) {
  try {
    t();
  } catch (e) {
    failure = { test: t.name, error: String(e && e.stack ? e.stack : e) };
    break;
  }
}

if (failure) {
  console.error(failure.error);
  console.log(JSON.stringify({ ok: false, ...failure }));
  process.exit(1);
} else {
  console.log(JSON.stringify({ ok: true, passed }));
}

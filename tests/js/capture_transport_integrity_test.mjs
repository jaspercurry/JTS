// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";

import {
  createTransportIntegrity,
  verifyAndParseCaptureSpec,
} from "../../capture-page/js/transport-integrity.js";

let passed = 0;
function ok() { passed += 1; }

const KEY = Buffer.from(Array.from({ length: 32 }, (_, index) => index))
  .toString("base64url");
const SESSION = "cap_integrity_test";
// Keep byte-identical to `tests/test_capture_relay_integrity.py` — this is
// the cross-language pin, so both sides move together or not at all.
const SPEC = '{"kind":"crossover_sweep","capture_protocol_version":3}';
const SPEC_MAC = "SnRu5CNZlynsM-Wjk4sfI_6Q2dAVrJdwMHOwZR1Ybqo";

async function testCrossLanguageSpecVectorAndTamperRefusal() {
  const integrity = await createTransportIntegrity(KEY, SESSION);
  assert.equal(await integrity.captureSpecMac(SPEC), SPEC_MAC);
  await integrity.verifyCaptureSpec(SPEC, SPEC_MAC);
  await assert.rejects(
    () => integrity.verifyCaptureSpec(SPEC.replace("crossover", "room"), SPEC_MAC),
    /integrity check failed/,
  );
  ok();
}

async function testCrossLanguageAuthenticatedEventVector() {
  const integrity = await createTransportIntegrity(KEY, SESSION);
  const envelope = await integrity.authenticatePhoneEvent({
    armed: true,
    capture_page: { capture_protocol_version: 3 },
  }, 1);
  assert.deepEqual(envelope, {
    authenticated_event: {
      schema_version: 1,
      sequence: 1,
      payload: '{"armed":true,"capture_page":{"capture_protocol_version":3}}',
      mac: "uzqEqrfokAMfNeEoL1_ycE8vqIOzKmNFNjYnRWUun88",
    },
  });
  ok();
}

async function testEveryCaptureSpecRequiresALinkMac() {
  // The spec MAC is mandatory for EVERY spec. Protocol-1 links carried none
  // and were parsed unverified; that protocol never shipped and is deleted,
  // so a MAC-less spec is simply unauthenticated — including one that omits
  // the protocol entirely, which used to be read as legacy protocol 1.
  for (const specText of [
    SPEC,
    '{"kind":"room_sweep","capture_protocol_version":3}',
    '{"kind":"room_sweep"}',
  ]) {
    await assert.rejects(
      () => verifyAndParseCaptureSpec(specText, {
        contentKeyB64: KEY,
        sessionId: SESSION,
        specMac: "",
      }),
      /integrity proof is missing/,
    );
  }
  const verified = await verifyAndParseCaptureSpec(SPEC, {
    contentKeyB64: KEY,
    sessionId: SESSION,
    specMac: SPEC_MAC,
  });
  assert.equal(verified.spec.kind, "crossover_sweep");
  ok();
}

const tests = [
  testCrossLanguageSpecVectorAndTamperRefusal,
  testCrossLanguageAuthenticatedEventVector,
  testEveryCaptureSpecRequiresALinkMac,
];

let failure = null;
for (const test of tests) {
  try {
    await test();
  } catch (error) {
    failure = { test: test.name, error: String(error && error.stack ? error.stack : error) };
    break;
  }
}

if (failure) {
  console.error(failure.error);
  console.log(JSON.stringify({ ok: false, ...failure }));
  process.exit(1);
}
console.log(JSON.stringify({ ok: true, passed }));


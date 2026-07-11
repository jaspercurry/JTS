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
const SPEC = '{"kind":"crossover_sweep","capture_protocol_version":2}';
const SPEC_MAC = "bGlwzjxko5SkN3PLp8ZP6vdPuj2SXGQYMWaLZ3yGFe0";

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
    capture_page: { capture_protocol_version: 2 },
  }, 1);
  assert.deepEqual(envelope, {
    authenticated_event: {
      schema_version: 1,
      sequence: 1,
      payload: '{"armed":true,"capture_page":{"capture_protocol_version":2}}',
      mac: "TiRz4S4EjBn2YcqQg-lI_Ys_ikO4oIlrwW6fOGwC57A",
    },
  });
  ok();
}

async function testProtocolTwoRequiresLinkMacButLegacyOneRemainsReadable() {
  await assert.rejects(
    () => verifyAndParseCaptureSpec(SPEC, {
      contentKeyB64: KEY,
      sessionId: SESSION,
      specMac: "",
    }),
    /integrity proof is missing/,
  );
  const legacy = await verifyAndParseCaptureSpec(
    '{"kind":"room_sweep","capture_protocol_version":1}',
    { contentKeyB64: KEY, sessionId: SESSION, specMac: "" },
  );
  assert.equal(legacy.spec.kind, "room_sweep");
  ok();
}

const tests = [
  testCrossLanguageSpecVectorAndTamperRefusal,
  testCrossLanguageAuthenticatedEventVector,
  testProtocolTwoRequiresLinkMacButLegacyOneRemainsReadable,
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


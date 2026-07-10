// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";
import {
  assertCaptureProtocolCompatible,
  requiredCaptureProtocol,
  validateCapturePageIdentity,
} from "../../capture-page/js/capture-protocol.js";

let passed = 0;
const page = validateCapturePageIdentity({
  schema_version: 1,
  capture_protocol_version: 2,
  supported_capture_protocol_versions: [1, 2],
  capture_page_build: "20260710.2",
});

// Page-first releases remain compatible with the pre-handshake Pi spec.
assert.equal(requiredCaptureProtocol({ kind: "room_sweep" }), 1);
assert.equal(assertCaptureProtocolCompatible({ kind: "room_sweep" }, page), 1);
passed += 1;

assert.equal(assertCaptureProtocolCompatible({ capture_protocol_version: 2 }, page), 2);
assert.throws(
  () => assertCaptureProtocolCompatible({ capture_protocol_version: 3 }, page),
  /incompatible/,
);
passed += 1;

assert.throws(
  () => validateCapturePageIdentity({ ...page, supported_capture_protocol_versions: [] }),
  /version is invalid/,
);
passed += 1;

console.log(JSON.stringify({ ok: true, passed }));

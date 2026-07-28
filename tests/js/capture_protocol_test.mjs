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
  capture_protocol_version: 3,
  supported_capture_protocol_versions: [3],
  capture_page_build: "20260727.2",
});

// A spec that states no protocol is INCOMPATIBLE, not legacy. The old rule
// read a missing version as protocol 1 so the page could ship ahead of the
// Pi; protocols 1 and 2 were deleted (the flow has never shipped outside the
// lab, and no lab Pi emits them), so that mapping would now silently accept a
// spec no Pi emits.
assert.equal(requiredCaptureProtocol({ kind: "room_sweep" }), null);
assert.throws(
  () => assertCaptureProtocolCompatible({ kind: "room_sweep" }, page),
  /incompatible/,
);
passed += 1;

assert.equal(assertCaptureProtocolCompatible({ capture_protocol_version: 3 }, page), 3);
// The deleted protocols are refused by the same handshake as any other
// unsupported number — there is no negotiated downgrade path.
for (const bogus of [1, 2, 4]) {
  assert.throws(
    () => assertCaptureProtocolCompatible({ capture_protocol_version: bogus }, page),
    /incompatible/,
  );
}
passed += 1;

// The page deployed RIGHT NOW still advertises [1, 2, 3], and a protocol-3
// spec must be compatible with it. This release therefore ships Pi-first,
// page-second — the reverse order would break every un-upgraded Pi the moment
// the [3] page went live. Mirrors the Python pin in test_capture_relay_spec.py.
const deployedPageToday = validateCapturePageIdentity({
  schema_version: 1,
  capture_protocol_version: 3,
  supported_capture_protocol_versions: [1, 2, 3],
  capture_page_build: "20260727.1",
});
assert.equal(
  assertCaptureProtocolCompatible({ capture_protocol_version: 3 }, deployedPageToday),
  3,
);
passed += 1;

assert.throws(
  () => validateCapturePageIdentity({ ...page, supported_capture_protocol_versions: [] }),
  /version is invalid/,
);
passed += 1;

console.log(JSON.stringify({ ok: true, passed }));

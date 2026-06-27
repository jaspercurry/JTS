// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Harness for the capture page's fragment parser (build step 3). The session
// handle + E2E key ride the URL fragment (never transmitted to a server), so a
// malformed/incomplete link must fail loud. Prints {"ok":true}.
//
//   node tests/js/capture_fragment_test.mjs

import assert from "node:assert/strict";

import {
  parseFragment,
  recordWindowMs,
  withinUploadCap,
  FragmentError,
} from "../../capture-page/js/fragment.js";

let passed = 0;
function ok() {
  passed += 1;
}

function k() {
  // a plausible 43-char base64url key
  return "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ";
}

function testParsesFullFragment() {
  const f = parseFragment(`#s=sess-1&u=up-token&k=${k()}`);
  assert.equal(f.sessionId, "sess-1");
  assert.equal(f.uploadToken, "up-token");
  assert.equal(f.contentKeyB64, k());
  ok();
}

function testAcceptsNoLeadingHash() {
  const f = parseFragment(`s=a&u=b&k=${k()}`);
  assert.equal(f.sessionId, "a");
  ok();
}

function testRejectsMissingParts() {
  assert.throws(() => parseFragment(`#s=a&u=b`), FragmentError);
  assert.throws(() => parseFragment(`#s=a&k=${k()}`), FragmentError);
  assert.throws(() => parseFragment(""), FragmentError);
  ok();
}

function testRejectsMalformedKey() {
  assert.throws(() => parseFragment("#s=a&u=b&k=short"), FragmentError);
  assert.throws(() => parseFragment("#s=a&u=b&k=has spaces and+slashes/=="), FragmentError);
  ok();
}

function testRecordWindowDefaults() {
  assert.equal(recordWindowMs({ duration_ms: 11500 }), 11500);
  assert.equal(recordWindowMs({}), 12000);
  assert.equal(recordWindowMs(null), 12000);
  assert.equal(recordWindowMs({ duration_ms: -5 }), 12000);
  ok();
}

function testWithinUploadCap() {
  // Page half of the dual size cap.
  assert.equal(withinUploadCap(1000, { max_upload_bytes: 2000 }), true);
  assert.equal(withinUploadCap(3000, { max_upload_bytes: 2000 }), false);
  assert.equal(withinUploadCap(3000, { max_upload_bytes: 3000 }), true); // boundary
  // Absent/invalid cap defers to the Worker.
  assert.equal(withinUploadCap(1 << 30, {}), true);
  assert.equal(withinUploadCap(1 << 30, { max_upload_bytes: 0 }), true);
  ok();
}

const tests = [
  testParsesFullFragment,
  testAcceptsNoLeadingHash,
  testRejectsMissingParts,
  testRejectsMalformedKey,
  testRecordWindowDefaults,
  testWithinUploadCap,
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

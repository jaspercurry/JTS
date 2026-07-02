// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";

import { safeReturnUrl } from "../../capture-page/js/return-url.js";

let passed = 0;
function ok() {
  passed += 1;
}

function testAllowsLocalHttpSpeakerUrl() {
  assert.equal(
    safeReturnUrl({ return_url: "http://jts5.local/correction/" }),
    "http://jts5.local/correction/",
  );
  ok();
}

function testAllowsHostPortForDevAndDirectServiceTesting() {
  assert.equal(
    safeReturnUrl({ return_url: "http://jts5.local:8770/correction/" }),
    "http://jts5.local:8770/correction/",
  );
  ok();
}

function testRejectsScriptishAndCredentialUrls() {
  assert.equal(safeReturnUrl({ return_url: "javascript:alert(1)" }), "");
  assert.equal(safeReturnUrl({ return_url: "data:text/html,hi" }), "");
  assert.equal(safeReturnUrl({ return_url: "http://user:pass@jts.local/" }), "");
  assert.equal(safeReturnUrl({ return_url: "http://jts.local/#frag" }), "");
  ok();
}

function testMissingOrMalformedUrlIsBlank() {
  assert.equal(safeReturnUrl({}), "");
  assert.equal(safeReturnUrl({ return_url: "/correction/" }), "");
  assert.equal(safeReturnUrl({ return_url: "http://bad\nhost/correction/" }), "");
  ok();
}

const tests = [
  testAllowsLocalHttpSpeakerUrl,
  testAllowsHostPortForDevAndDirectServiceTesting,
  testRejectsScriptishAndCredentialUrls,
  testMissingOrMalformedUrlIsBlank,
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
  console.error(JSON.stringify({ ok: false, ...failure }));
  process.exit(1);
}

console.log(JSON.stringify({ ok: true, passed }));

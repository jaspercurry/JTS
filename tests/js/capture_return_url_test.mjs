// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";

import { safeReturnUrl } from "../../capture-page/js/return-url.js";
import { runTestFunctions } from "./run_test_functions.mjs";

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

await runTestFunctions(tests, () => passed);

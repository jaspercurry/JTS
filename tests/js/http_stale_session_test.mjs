// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Issue #1926: a stale wizard page's mutating POST 403'd with a bare
// synthesized "HTTP 403" / "Request rejected (HTTP 403)..." string — the
// shared fetch helper (deploy/assets/shared/js/http.js) discarded the
// server's own honest expiry copy (jasper.web._common.reject_csrf's HTML
// "Session expired" page) entirely, since it only ever tried r.json().
//
// This pins postJSON's fix: a mutating-POST 403 with a non-JSON body (the
// only shape reject_csrf / guard_mutating_host produce) shows the honest
// "went stale, reloading" copy and reloads, instead of surfacing a synthetic
// status-code string. It also pins that this is scoped correctly — a JSON
// 403/500 error payload still surfaces the route's own message untouched
// (regression guard: JSON error payloads must keep surfacing their own
// copy), and the JSON control-token-required 403 shape is never classified
// as a stale session (it has its own prompt-and-retry flow).
//
// Run via tests/test_web_http_helper.py against the canonical module
// (deploy/assets/shared/js/http.js). http.js has no internal imports, so a
// direct dynamic import works here (mirrors tests/js/dom_test.mjs).
import assert from "node:assert/strict";

let reloadCalled = false;
let bannerNode = null;
const bodyChildren = [];
globalThis.document = {
  querySelector() { return null; }, // no <meta name=jts-csrf> in this harness
  createElement(tag) {
    return { tagName: tag, className: "", textContent: "", attrs: {},
      setAttribute(k, v) { this.attrs[k] = v; } };
  },
  body: {
    prepend(node) { bannerNode = node; bodyChildren.unshift(node); },
  },
};
globalThis.location = { reload() { reloadCalled = true; } };

let nextResponse = null;
globalThis.fetch = async () => nextResponse;

function jsonResponse(status, jsonBody) {
  return { ok: status >= 200 && status < 300, status, async json() { return jsonBody; } };
}
function htmlResponse(status) {
  return {
    ok: status >= 200 && status < 300, status,
    async json() { throw new SyntaxError("Unexpected token < in JSON"); },
  };
}

const { postJSON, isStaleSessionRejection, isControlTokenRequired } = await import(
  "../../deploy/assets/shared/js/http.js"
);

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

// --- isStaleSessionRejection: classification unit coverage -----------------
check(
  isStaleSessionRejection({ status: 403, body: null }) === true,
  "403 with a non-JSON (null) body classifies as a stale session",
);
check(
  isStaleSessionRejection({ status: 403, body: { error: "control_token_required" } }) === false,
  "403 with a JSON control-token-required body is NOT a stale session",
);
check(
  isStaleSessionRejection({ status: 403, body: { error: "some_domain_reason" } }) === false,
  "403 with any other JSON body is NOT a stale session (route keeps its own copy)",
);
check(
  isStaleSessionRejection({ status: 500, body: null }) === false,
  "a non-403 status is never classified as a stale session",
);
check(
  isControlTokenRequired({ status: 403, body: null }) === false,
  "control-token-required check does not misfire on the stale-session shape",
);

// --- postJSON integration: the stale-session 403 path -----------------------
{
  reloadCalled = false;
  bannerNode = null;
  nextResponse = htmlResponse(403); // reject_csrf's shape: 403, HTML body
  let thrown = null;
  try {
    await postJSON("/sound/speaker/crossover/capture-cancel", {});
  } catch (err) {
    thrown = err;
  }
  check(thrown !== null, "postJSON still throws on the stale-session 403 (callers keep their try/catch)");
  check(reloadCalled === true, "postJSON triggers location.reload() on a stale-session 403");
  check(
    bannerNode !== null && bannerNode.textContent === "This page went stale while idle — reloading…",
    "postJSON shows the honest stale-session copy, not a bare status code",
  );
  check(
    thrown.message === "This page went stale while idle — reloading…",
    `thrown error carries the honest copy, not a generic string (got: ${JSON.stringify(thrown && thrown.message)})`,
  );
  check(
    thrown.message !== "HTTP 403" &&
      thrown.message !== "Request rejected (HTTP 403). Reload the page and try again.",
    "thrown error is NOT the old generic synthesized string",
  );
}

// --- regression: a JSON error payload still surfaces its own copy ----------
{
  reloadCalled = false;
  nextResponse = jsonResponse(500, { error: "solve failed: singular matrix" });
  let thrown = null;
  try {
    await postJSON("/sound/speaker/crossover/v2/session", {});
  } catch (err) {
    thrown = err;
  }
  check(thrown !== null, "postJSON still throws on a JSON error payload");
  check(
    thrown.message === "solve failed: singular matrix",
    `JSON error payloads keep surfacing their own message (got: ${JSON.stringify(thrown && thrown.message)})`,
  );
  check(reloadCalled === false, "a JSON error payload never triggers the stale-session reload");
}

// --- regression: a JSON 403 with its own domain error is not reload-hijacked
{
  reloadCalled = false;
  nextResponse = jsonResponse(403, { error: "capture_geometry not permitted here" });
  let thrown = null;
  try {
    await postJSON("/sound/speaker/crossover/v2/session", {});
  } catch (err) {
    thrown = err;
  }
  check(thrown !== null, "postJSON still throws on a JSON 403 with its own error field");
  check(
    thrown.message === "capture_geometry not permitted here",
    `a route's own JSON 403 copy survives untouched (got: ${JSON.stringify(thrown && thrown.message)})`,
  );
  check(reloadCalled === false, "a route's own JSON 403 never triggers the stale-session reload");
}

console.log(JSON.stringify({ ok: true, passed }));

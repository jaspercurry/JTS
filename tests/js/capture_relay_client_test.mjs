// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Harness for the capture page's relay client (build step 3). Asserts the
// phone-side request contract (URLs, upload-token bearer auth, integrity
// headers, body) against a mock fetch — no network. Prints {"ok":true}.
//
//   node tests/js/capture_relay_client_test.mjs

import assert from "node:assert/strict";

import {
  RELAY_CONTROL_TIMEOUT_MS,
  RelayClient,
  RelayError,
} from "../../capture-page/js/relay-client.js";

let passed = 0;
function ok() {
  passed += 1;
}

function mockFetch(handler) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return handler(url, init, calls.length - 1);
  };
  fn.calls = calls;
  return fn;
}

function res(status, body, { isJson = true } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      if (!isJson) throw new Error("not json");
      return body;
    },
    async text() {
      return typeof body === "string" ? body : JSON.stringify(body);
    },
  };
}

function makeClient(fetchImpl) {
  const client = new RelayClient({
    baseUrl: "https://relay.test/",
    sessionId: "sess-1",
    uploadToken: "up-token",
    fetchImpl,
  });
  client.setCapturePageIdentity({
    schema_version: 1,
    capture_protocol_version: 1,
    supported_capture_protocol_versions: [1],
    capture_page_build: "20260710.1",
  });
  return client;
}

async function testFetchSpec() {
  const spec = { kind: "room_sweep", duration_ms: 11500 };
  const f = mockFetch(() => res(200, spec));
  const client = makeClient(f);
  const got = await client.fetchSpec();
  assert.deepEqual(got, spec);
  const call = f.calls[0];
  assert.equal(call.url, "https://relay.test/sessions/sess-1/spec");
  assert.equal(call.init.method, "GET");
  assert.equal(call.init.headers.Authorization, "Bearer up-token");
  ok();
}

async function testPostEvent() {
  const f = mockFetch(() => res(200, { ok: true }));
  const client = makeClient(f);
  await client.postEvent({ armed: true });
  const call = f.calls[0];
  assert.equal(call.url, "https://relay.test/sessions/sess-1/event");
  assert.equal(call.init.method, "POST");
  assert.equal(call.init.headers["Content-Type"], "application/json");
  assert.equal(call.init.headers.Authorization, "Bearer up-token");
  assert.deepEqual(JSON.parse(call.init.body), {
    armed: true,
    capture_page: {
      schema_version: 1,
      capture_protocol_version: 1,
      supported_capture_protocol_versions: [1],
      capture_page_build: "20260710.1",
    },
  });
  ok();
}

async function testProtocolTwoPostEventUsesAuthenticatedEnvelope() {
  const f = mockFetch(() => res(200, { ok: true }));
  const client = makeClient(f);
  const seen = [];
  client.setTransportIntegrity({
    async authenticatePhoneEvent(payload, sequence) {
      seen.push({ payload, sequence });
      return { authenticated_event: { sequence, payload: JSON.stringify(payload), mac: "tag" } };
    },
  }, { required: true });
  await client.postEvent({ armed: true });
  const posted = JSON.parse(f.calls[0].init.body);
  assert.equal(posted.authenticated_event.sequence, 1);
  assert.equal(posted.authenticated_event.mac, "tag");
  assert.equal(seen[0].payload.armed, true);
  assert.equal(seen[0].payload.capture_page.capture_protocol_version, 1);
  ok();
}

async function testFetchPhoneStatus() {
  const payload = { state: "pending", host_event: { phase: "sweep_complete" } };
  const f = mockFetch(() => res(200, payload));
  const client = makeClient(f);
  const got = await client.fetchPhoneStatus();
  assert.deepEqual(got, payload);
  const call = f.calls[0];
  assert.equal(call.url, "https://relay.test/sessions/sess-1/phone-status");
  assert.equal(call.init.method, "GET");
  assert.equal(call.init.headers.Authorization, "Bearer up-token");
  assert.ok(call.init.signal instanceof AbortSignal);
  ok();
}

async function testControlFetchAbortsBeforePiFeedLossWindow() {
  const f = mockFetch((_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener("abort", () => {
      const error = new Error("control request timed out");
      error.name = "AbortError";
      reject(error);
    }, { once: true });
  }));
  const client = makeClient(f);
  await assert.rejects(
    () => client.fetchPhoneStatus({ timeoutMs: 1 }),
    (error) => error && error.name === "AbortError",
  );
  assert.ok(RELAY_CONTROL_TIMEOUT_MS < 8000);
  assert.equal(f.calls.length, 1);
  ok();
}

async function testPutBlob() {
  const f = mockFetch(() => res(200, { ok: true, state: "ready" }));
  const client = makeClient(f);
  const blob = new Uint8Array([10, 20, 30, 40]);
  await client.putBlob(blob, 3, "a".repeat(64));
  const call = f.calls[0];
  assert.equal(call.url, "https://relay.test/sessions/sess-1/blob");
  assert.equal(call.init.method, "PUT");
  assert.equal(call.init.headers["Content-Type"], "application/octet-stream");
  assert.equal(call.init.headers["X-Plaintext-Length"], "3");
  assert.equal(call.init.headers["X-Plaintext-Sha256"], "a".repeat(64));
  assert.equal(call.init.headers["Content-Length"], "4");
  assert.deepEqual([...call.init.body], [...blob]);
  ok();
}

async function testErrorThrowsRelayError() {
  const f = mockFetch(() => res(429, { error: "rate_limited" }));
  const client = makeClient(f);
  await assert.rejects(
    () => client.fetchSpec(),
    (err) => {
      assert.ok(err instanceof RelayError);
      assert.equal(err.status, 429);
      assert.equal(err.message, "rate_limited");
      return true;
    },
  );
  ok();
}

async function testConstructorValidates() {
  assert.throws(() => new RelayClient({ sessionId: "x", uploadToken: "y" }), /baseUrl/);
  assert.throws(
    () => new RelayClient({ baseUrl: "x", uploadToken: "y" }),
    /sessionId/,
  );
  const client = new RelayClient({
    baseUrl: "x", sessionId: "y", uploadToken: "z", fetchImpl: async () => res(200, {}),
  });
  await assert.rejects(
    () => client.postEvent({ armed: true }),
    /compatibility was not established/,
  );
  ok();
}

const tests = [
  testFetchSpec,
  testPostEvent,
  testProtocolTwoPostEventUsesAuthenticatedEnvelope,
  testFetchPhoneStatus,
  testControlFetchAbortsBeforePiFeedLossWindow,
  testPutBlob,
  testErrorThrowsRelayError,
  testConstructorValidates,
];

let failure = null;
for (const t of tests) {
  try {
    await t();
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

// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Hardware-free harness for the phone-mic relay Worker (build step 2).
//
// Exercises the pure `handle(request, store, env)` router against an in-memory
// store, so the whole relay contract is testable with no Cloudflare runtime.
// Run directly (`node tests/js/relay_worker_test.mjs`) or via the pytest wrapper
// tests/test_relay_worker_js.py. Prints a final JSON line {"ok":true,...}.
//
// The load-bearing invariants from docs/phone-mic-relay-plan.md §§7,8,15 are the
// point of this suite: OPAQUE relay, hashed tokens, dual size cap, per-session
// rate limit, TTL, and zero-relay-change-for-a-new-kind.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { handle, makeMemoryStore, sha256Hex } from "../../relay/src/worker.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const WORKER_PATH = resolve(HERE, "../../relay/src/worker.js");

const ORIGIN = "https://capture.jasper.tech";
let passed = 0;
function ok(name) {
  passed += 1;
}

function req(method, path, opts = {}) {
  const h = new Headers(opts.headers || {});
  if (opts.token) h.set("Authorization", "Bearer " + opts.token);
  if (opts.origin) h.set("Origin", opts.origin);
  const init = { method, headers: h };
  if (opts.raw !== undefined) {
    init.body = opts.raw;
    h.set("content-length", String(opts.raw.length ?? opts.raw.byteLength));
    if (!h.has("content-type")) h.set("content-type", "application/octet-stream");
  } else if (opts.body !== undefined) {
    const s = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
    init.body = s;
    h.set("content-length", String(new TextEncoder().encode(s).length));
    if (!h.has("content-type")) h.set("content-type", "application/json");
  }
  return new Request("https://relay.test" + path, init);
}

// Standard registration body.
function registration(overrides = {}) {
  return {
    session_id: "sess-" + Math.random().toString(36).slice(2),
    capture_spec: JSON.stringify({ kind: "room_sweep", sample_rate_hz: 48000 }),
    upload_token: "up-" + Math.random().toString(36).slice(2),
    pull_token: "pu-" + Math.random().toString(36).slice(2),
    ttl_s: 900,
    max_upload_bytes: 32 * 1024 * 1024,
    ...overrides,
  };
}

async function register(store, env, body, opts = {}) {
  // Unique client IP per call so the per-IP registration limiter (keyed by
  // cf-connecting-ip in a module-level, never-expiring-under-the-fixed-clock
  // map) doesn't accumulate across unrelated tests. The flood test below uses a
  // fixed IP on purpose to exercise the limit.
  const ip = `ip-${Math.random().toString(36).slice(2)}`;
  return handle(
    req("POST", "/sessions", {
      body,
      origin: ORIGIN,
      headers: { "cf-connecting-ip": ip, ...(opts.headers || {}) },
    }),
    store,
    env,
  );
}

// A fixed clock so TTL is deterministic.
function fixedEnv(extra = {}) {
  let t = 1_000_000;
  return {
    now: () => t,
    _setNow: (v) => {
      t = v;
    },
    ...extra,
  };
}

// === Tests ===================================================================

async function testHappyPath() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();

  // POST /sessions
  let res = await register(store, env, reg);
  assert.equal(res.status, 201, "register 201");
  let j = await res.json();
  assert.equal(j.state, "pending");

  // GET /sessions/:id/spec  (upload token) — opaque, byte-identical
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/spec`, { token: reg.upload_token, origin: ORIGIN }),
    store,
    env,
  );
  assert.equal(res.status, 200, "spec 200");
  assert.equal(res.headers.get("content-type"), "application/json");
  assert.equal(await res.text(), reg.capture_spec, "spec round-trips verbatim");

  // POST /sessions/:id/event {armed:true}
  res = await handle(
    req("POST", `/sessions/${reg.session_id}/event`, {
      token: reg.upload_token,
      body: { armed: true },
      origin: ORIGIN,
    }),
    store,
    env,
  );
  assert.equal(res.status, 200, "event 200");

  // GET status (pull token) — relays the armed event to the Pi
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  j = await res.json();
  assert.equal(j.state, "pending");
  assert.deepEqual(j.event, { armed: true }, "armed event relayed in status");

  // POST host progress with the Pi pull token. The phone can poll this with its
  // upload token, but still cannot see the pull-only blob/integrity details.
  res = await handle(
    req("POST", `/sessions/${reg.session_id}/host-event`, {
      token: reg.pull_token,
      body: { phase: "sweep_complete", position: 1, total_positions: 5 },
    }),
    store,
    env,
  );
  assert.equal(res.status, 200, "host event 200");
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/phone-status`, {
      token: reg.upload_token,
      origin: ORIGIN,
    }),
    store,
    env,
  );
  assert.equal(res.status, 200, "phone status 200");
  j = await res.json();
  assert.equal(j.state, "pending");
  assert.deepEqual(
    j.host_event,
    { phase: "sweep_complete", position: 1, total_positions: 5 },
    "host progress is visible to phone",
  );
  assert.equal(j.integrity, undefined, "phone status omits pull-side integrity");

  // PUT blob (upload token)
  const blob = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8]);
  const sha = await sha256Hex(new Uint8Array([9, 9, 9])); // pretend-plaintext digest
  res = await handle(
    req("PUT", `/sessions/${reg.session_id}/blob`, {
      token: reg.upload_token,
      raw: blob,
      headers: { "X-Plaintext-Length": "3", "X-Plaintext-Sha256": sha },
      origin: ORIGIN,
    }),
    store,
    env,
  );
  assert.equal(res.status, 200, "blob put 200");

  // status -> ready, integrity relayed
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  j = await res.json();
  assert.equal(j.state, "ready");
  assert.equal(j.size, 8);
  assert.deepEqual(j.integrity, { plaintext_len: 3, sha256: sha });

  // GET blob (pull token) -> ciphertext + integrity headers
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/blob`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.equal(res.status, 200, "blob get 200");
  assert.equal(res.headers.get("X-Plaintext-Length"), "3");
  assert.equal(res.headers.get("X-Plaintext-Sha256"), sha);
  const got = new Uint8Array(await res.arrayBuffer());
  assert.deepEqual([...got], [...blob], "blob bytes round-trip");

  // GET blob again — non-destructive (retry-safe)
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/blob`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.equal(res.status, 200, "blob get is non-destructive");

  // DELETE (pull token)
  res = await handle(
    req("DELETE", `/sessions/${reg.session_id}`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.equal(res.status, 204, "delete 204");

  // After delete -> 404
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.equal(res.status, 404, "gone after delete");
  ok("happy path");
}

async function testOpaqueNonJsonSpec() {
  // The relay must store/serve the spec as opaque bytes. A spec that is NOT
  // valid JSON must round-trip byte-identically and never make the Worker error
  // — proving the relay never parses it.
  const store = makeMemoryStore();
  const env = fixedEnv();
  const weird = "this is \u0000 not json at all }{<script>";
  const reg = registration({ capture_spec: weird });
  let res = await register(store, env, reg);
  assert.equal(res.status, 201, "non-JSON spec registers fine");
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/spec`, { token: reg.upload_token }),
    store,
    env,
  );
  assert.equal(await res.text(), weird, "opaque non-JSON spec round-trips verbatim");
  ok("opaque non-JSON spec");
}

async function testNewKindNeedsNoRelayChange() {
  // Plan §15: adding kind=balance_burst is a Pi+page change with ZERO relay
  // change. A never-before-seen kind must register + round-trip unchanged.
  const store = makeMemoryStore();
  const env = fixedEnv();
  const spec = JSON.stringify({ kind: "totally_new_kind_42", whatever: [1, 2, 3] });
  const reg = registration({ capture_spec: spec });
  let res = await register(store, env, reg);
  assert.equal(res.status, 201);
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/spec`, { token: reg.upload_token }),
    store,
    env,
  );
  assert.equal(await res.text(), spec);
  ok("new kind needs no relay change");
}

function testWorkerSourceNeverParsesSpec() {
  // Best-effort LINT, not the proof: the behavioral round-trip tests
  // (testOpaqueNonJsonSpec / testNewKindNeedsNoRelayChange) are the real opacity
  // guarantee — a non-JSON spec must survive byte-identically, which a parser
  // could not do. This scan catches the obvious regressions a future edit might
  // introduce: dotted field access (`capture_spec.kind`), bracket access
  // (`meta["capture_spec"]` used to dig in), and JSON.parse of the spec. It is
  // defeatable (e.g. copy-to-local then parse), so it is a tripwire, not a seal.
  const src = readFileSync(WORKER_PATH, "utf8");
  // Strip line comments so the invariant prose above worker functions doesn't
  // false-trip the scan.
  const code = src.replace(/\/\/[^\n]*/g, "");
  assert.ok(!code.includes("capture_spec."), "no dotted field access into capture_spec");
  assert.ok(
    !/\[\s*['"]capture_spec['"]\s*\]/.test(code),
    "no bracket access into capture_spec",
  );
  assert.ok(
    !/JSON\.parse\([^)]*capture_spec/.test(code),
    "capture_spec is never JSON.parsed",
  );
  ok("worker source never parses spec (lint)");
}

async function testTokensHashedNotStored() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);
  const meta = JSON.parse(store._rawMeta.get(reg.session_id));
  assert.ok(!("upload_token" in meta), "raw upload token not stored");
  assert.ok(!("pull_token" in meta), "raw pull token not stored");
  assert.ok(!JSON.stringify(meta).includes(reg.upload_token), "raw token absent from meta");
  assert.equal(meta.upload_token_hash, await sha256Hex(reg.upload_token));
  assert.equal(meta.pull_token_hash, await sha256Hex(reg.pull_token));
  ok("tokens hashed, never stored raw");
}

async function testNoContentKeyAnywhere() {
  // E2E proof at the relay layer: there is no field for content_key, the meta
  // carries no key, and the blob is opaque bytes.
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration({ content_key: "SHOULD-BE-IGNORED" });
  await register(store, env, reg);
  const meta = JSON.parse(store._rawMeta.get(reg.session_id));
  assert.ok(!("content_key" in meta), "content_key never stored");
  assert.ok(!JSON.stringify(meta).includes("SHOULD-BE-IGNORED"), "extra field ignored");
  ok("no content_key reaches the relay");
}

async function testRegistrationSecret() {
  const store = makeMemoryStore();

  // OSS/dev compatibility: absent the Worker secret, registration stays open.
  let env = fixedEnv();
  let reg = registration();
  let res = await register(store, env, reg);
  assert.equal(res.status, 201, "open registration still works when unconfigured");

  // Production hardening: with RELAY_REGISTRATION_TOKEN set, only a Pi that
  // presents the matching registration header can mint sessions.
  env = fixedEnv({ RELAY_REGISTRATION_TOKEN: "secret-123" });
  reg = registration();
  res = await register(store, env, reg);
  assert.equal(res.status, 401, "missing registration secret rejected");
  assert.equal(store._rawMeta.has(reg.session_id), false, "rejected registration stores nothing");

  res = await register(store, env, reg, {
    headers: { "X-JTS-Relay-Registration-Token": "wrong" },
  });
  assert.equal(res.status, 401, "wrong registration secret rejected");

  res = await register(store, env, reg, {
    headers: { "X-JTS-Relay-Registration-Token": " secret-123 " },
  });
  assert.equal(res.status, 201, "matching registration secret accepted");

  // The shared registration secret is not needed after the session exists; the
  // per-session upload/pull tokens remain the capture auth boundary.
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/spec`, { token: reg.upload_token }),
    store,
    env,
  );
  assert.equal(res.status, 200, "phone endpoints keep using per-session tokens");
  ok("registration secret");
}

async function testTokenGating() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);

  // Missing token on a phone endpoint.
  let res = await handle(req("GET", `/sessions/${reg.session_id}/spec`), store, env);
  assert.equal(res.status, 401, "spec needs token");

  // pull token cannot read the upload-only spec endpoint.
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/spec`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.equal(res.status, 401, "pull token rejected on upload endpoint");

  // upload token cannot read pull-only status/blob/delete.
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.upload_token }),
    store,
    env,
  );
  assert.equal(res.status, 401, "upload token rejected on status");
  res = await handle(
    req("DELETE", `/sessions/${reg.session_id}`, { token: reg.upload_token }),
    store,
    env,
  );
  assert.equal(res.status, 401, "upload token rejected on delete");
  ok("token gating");
}

async function testDualSizeCap() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration({ max_upload_bytes: 1024 });
  await register(store, env, reg);

  // Declared Content-Length over cap -> 413 BEFORE buffering.
  const big = new Uint8Array(2048);
  let res = await handle(
    req("PUT", `/sessions/${reg.session_id}/blob`, {
      token: reg.upload_token,
      raw: big,
      headers: { "X-Plaintext-Length": "10", "X-Plaintext-Sha256": "a".repeat(64) },
    }),
    store,
    env,
  );
  assert.equal(res.status, 413, "oversize blob rejected by cap");

  // Registration max_upload_bytes is clamped to the worker hard ceiling.
  const reg2 = registration({ max_upload_bytes: 10 * 1024 * 1024 * 1024 });
  await register(store, env, reg2);
  const meta = JSON.parse(store._rawMeta.get(reg2.session_id));
  assert.ok(meta.max_upload_bytes <= 64 * 1024 * 1024, "cap clamped to hard ceiling");
  ok("dual size cap");
}

async function testBlobRequiresIntegrityHeaders() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);
  const blob = new Uint8Array([1, 2, 3]);
  // No integrity headers.
  let res = await handle(
    req("PUT", `/sessions/${reg.session_id}/blob`, { token: reg.upload_token, raw: blob }),
    store,
    env,
  );
  assert.equal(res.status, 400, "blob requires plaintext length+sha");
  ok("blob requires integrity headers");
}

async function testIntegrityRelayedVerbatim() {
  // The relay stores and relays the integrity CLAIM; it never computes or
  // verifies it (it has no plaintext). Even a deliberately-wrong sha is relayed.
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);
  const blob = new Uint8Array([1, 2, 3]);
  const wrongSha = "0".repeat(64);
  await handle(
    req("PUT", `/sessions/${reg.session_id}/blob`, {
      token: reg.upload_token,
      raw: blob,
      headers: { "X-Plaintext-Length": "999", "X-Plaintext-Sha256": wrongSha },
    }),
    store,
    env,
  );
  const res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  const j = await res.json();
  assert.deepEqual(j.integrity, { plaintext_len: 999, sha256: wrongSha });
  ok("integrity relayed verbatim, never verified by relay");
}

async function testRateLimitFallback() {
  const store = makeMemoryStore();
  const env = fixedEnv(); // no RELAY_RATELIMIT binding -> fallback path
  const reg = registration();
  await register(store, env, reg);
  let limited = false;
  for (let i = 0; i < 200; i++) {
    const res = await handle(
      req("GET", `/sessions/${reg.session_id}/spec`, { token: reg.upload_token }),
      store,
      env,
    );
    if (res.status === 429) {
      limited = true;
      break;
    }
  }
  assert.ok(limited, "per-session rate limit eventually 429s the upload token");
  ok("rate limit fallback");
}

async function testRateLimitBinding() {
  // When the atomic binding is present and denies, the Worker 429s.
  const store = makeMemoryStore();
  const reg = registration();
  await register(store, fixedEnv(), reg); // seed meta with known tokens
  const denyEnv = fixedEnv({
    RELAY_RATELIMIT: { limit: async () => ({ success: false }) },
  });
  const res = await handle(
    req("GET", `/sessions/${reg.session_id}/spec`, { token: reg.upload_token }),
    store,
    denyEnv,
  );
  assert.equal(res.status, 429, "binding deny -> 429");
  ok("rate limit binding deny path");
}

async function testRateLimitDoesNotClobberState() {
  // Regression for the meta-RMW clobber: the fallback rate limiter must NOT
  // rewrite meta/<id>, so heavy read-only GET /spec traffic can never erase the
  // ready/blob/armed control state and strand the Pi.
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);
  // Phone arms + uploads -> state ready.
  await handle(
    req("POST", `/sessions/${reg.session_id}/event`, {
      token: reg.upload_token,
      body: { armed: true },
    }),
    store,
    env,
  );
  await handle(
    req("PUT", `/sessions/${reg.session_id}/blob`, {
      token: reg.upload_token,
      raw: new Uint8Array([1, 2, 3]),
      headers: { "X-Plaintext-Length": "3", "X-Plaintext-Sha256": "a".repeat(64) },
    }),
    store,
    env,
  );
  // Hammer GET /spec (the formerly-clobbering read path).
  for (let i = 0; i < 30; i++) {
    await handle(
      req("GET", `/sessions/${reg.session_id}/spec`, { token: reg.upload_token }),
      store,
      env,
    );
  }
  const res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  const j = await res.json();
  assert.equal(j.state, "ready", "ready state survives read traffic");
  assert.equal(j.size, 3, "blob size not clobbered");
  assert.deepEqual(j.event, { armed: true }, "armed event not clobbered");
  ok("rate limit does not clobber state");
}

async function testRegistrationRateLimited() {
  // Open POST /sessions is bounded per client IP so a flood can't fill the bucket.
  const store = makeMemoryStore();
  const env = fixedEnv();
  let limited = false;
  for (let i = 0; i < 200; i++) {
    const res = await handle(
      req("POST", "/sessions", {
        body: registration(),
        headers: { "cf-connecting-ip": "203.0.113.7" },
      }),
      store,
      env,
    );
    if (res.status === 429) {
      limited = true;
      break;
    }
  }
  assert.ok(limited, "registration flood from one IP eventually 429s");
  ok("registration rate limit");
}

async function testEventRequiresContentLength() {
  // A POST /event with NO Content-Length must be rejected, not read unbounded.
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);
  const h = new Headers({ Authorization: `Bearer ${reg.upload_token}` });
  // Deliberately omit Content-Length (Request does not synthesize the header).
  const r = new Request(`https://relay.test/sessions/${reg.session_id}/event`, {
    method: "POST",
    headers: h,
    body: JSON.stringify({ armed: true }),
  });
  r.headers.delete("content-length");
  const res = await handle(r, store, env);
  assert.equal(res.status, 411, "missing Content-Length on event -> 411");
  ok("event requires content-length");
}

async function testTtlExpiry() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration({ ttl_s: 60 });
  await register(store, env, reg);
  // Jump past expiry.
  env._setNow(1_000_000 + 61_000);
  const res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.equal(res.status, 404, "expired session is gone");
  assert.equal(store._rawMeta.has(reg.session_id), false, "expired meta deleted");
  ok("TTL expiry self-cleans");
}

async function testEventDoesNotClobberHostEvent() {
  // THE RACE, pinned. Reproduces the 2026-07-15 JTS3 false phone timeout:
  // request handling reads `meta` at request start; pre-fix, postEvent and
  // postHostEvent each wrote the WHOLE meta object back, so a phone POST
  // /event whose request-start read predates the Pi's terminal POST
  // /host-event erases that terminal when the phone's (stale) write lands
  // afterward. Structural fix: event/host_event now live in their own keys,
  // so postEvent never touches host_event at all, regardless of timing.
  //
  // Deterministic construction (no real concurrency needed): wrap the store
  // so the phone's request-start `getMeta` call captures its snapshot NOW
  // (before the Pi writes), but does not RETURN that snapshot until the test
  // releases a gate. While the gate is held, the Pi's POST /host-event runs
  // to completion against the real store. Releasing the gate then lets the
  // phone's POST /event proceed using a meta snapshot that predates the
  // Pi's write — exactly the race window.
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);

  let armPhoneRead = false;
  let releaseGate;
  const gate = new Promise((resolve) => {
    releaseGate = resolve;
  });
  const racingStore = {
    ...store,
    async getMeta(id) {
      if (armPhoneRead && id === reg.session_id) {
        armPhoneRead = false;
        const snapshot = await store.getMeta(id); // read BEFORE the Pi's write
        await gate; // held until the test releases it, below
        return snapshot; // stale: predates the Pi's host-event write
      }
      return store.getMeta(id);
    },
  };

  armPhoneRead = true;
  const phonePromise = handle(
    req("POST", `/sessions/${reg.session_id}/event`, {
      token: reg.upload_token,
      body: { armed: true },
    }),
    racingStore,
    env,
  );

  // The Pi's terminal host-event completes fully while the phone's request
  // is blocked holding its stale snapshot.
  const hostRes = await handle(
    req("POST", `/sessions/${reg.session_id}/host-event`, {
      token: reg.pull_token,
      body: { phase: "sweep_complete", terminal: true },
    }),
    store,
    env,
  );
  assert.equal(hostRes.status, 200, "Pi host-event lands first");

  releaseGate();
  const phoneRes = await phonePromise;
  assert.equal(phoneRes.status, 200, "phone event also lands");

  const res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  const j = await res.json();
  assert.deepEqual(
    j.host_event,
    { phase: "sweep_complete", terminal: true },
    "Pi's terminal host_event survives the interleaved phone event post",
  );
  assert.deepEqual(j.event, { armed: true }, "phone event still lands in its own key");
  ok("race: interleaved phone event does not clobber Pi host_event");
}

async function testLegacyMetaFallback() {
  // Read-fallback for the <=900s compat window: a session created by
  // pre-deploy code has event/host_event embedded directly in meta (no split
  // key was ever written for it). Both pull-side /status and phone-side
  // /phone-status must still surface those values.
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);
  const meta = await store.getMeta(reg.session_id);
  meta.event = { armed: true, legacy: true };
  meta.host_event = { phase: "sweep_complete", legacy: true };
  await store.putMeta(reg.session_id, meta);

  let res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  let j = await res.json();
  assert.deepEqual(
    j.host_event,
    { phase: "sweep_complete", legacy: true },
    "status falls back to legacy meta.host_event when no split key exists",
  );
  assert.deepEqual(
    j.event,
    { armed: true, legacy: true },
    "status falls back to legacy meta.event when no split key exists",
  );

  res = await handle(
    req("GET", `/sessions/${reg.session_id}/phone-status`, {
      token: reg.upload_token,
      origin: ORIGIN,
    }),
    store,
    env,
  );
  j = await res.json();
  assert.deepEqual(
    j.host_event,
    { phase: "sweep_complete", legacy: true },
    "phone-status falls back to legacy meta.host_event",
  );

  // A post-deploy write creates the split key, which then wins over the
  // stale legacy meta field for good — no version flag needed, TTL retires
  // the legacy shape.
  await handle(
    req("POST", `/sessions/${reg.session_id}/host-event`, {
      token: reg.pull_token,
      body: { phase: "new_terminal" },
    }),
    store,
    env,
  );
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  j = await res.json();
  assert.deepEqual(
    j.host_event,
    { phase: "new_terminal" },
    "split key takes precedence once any post-deploy write lands",
  );
  ok("legacy meta fallback for event/host_event");
}

async function testExpiryPurgesSplitKeys() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration({ ttl_s: 60 });
  await register(store, env, reg);
  await handle(
    req("POST", `/sessions/${reg.session_id}/event`, { token: reg.upload_token, body: { armed: true } }),
    store,
    env,
  );
  await handle(
    req("POST", `/sessions/${reg.session_id}/host-event`, { token: reg.pull_token, body: { phase: "x" } }),
    store,
    env,
  );
  assert.ok(store._rawEvent.has(reg.session_id), "event key exists before expiry");
  assert.ok(store._rawHostEvent.has(reg.session_id), "hostevent key exists before expiry");

  env._setNow(1_000_000 + 61_000);
  const res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.equal(res.status, 404, "expired session is gone");
  assert.equal(store._rawEvent.has(reg.session_id), false, "expired event key purged");
  assert.equal(store._rawHostEvent.has(reg.session_id), false, "expired hostevent key purged");
  ok("expiry purges split event/host_event keys");
}

async function testDeleteSessionPurgesSplitKeys() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);
  await handle(
    req("POST", `/sessions/${reg.session_id}/event`, { token: reg.upload_token, body: { armed: true } }),
    store,
    env,
  );
  await handle(
    req("POST", `/sessions/${reg.session_id}/host-event`, { token: reg.pull_token, body: { phase: "x" } }),
    store,
    env,
  );
  await handle(
    req("DELETE", `/sessions/${reg.session_id}`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.equal(store._rawEvent.has(reg.session_id), false, "DELETE purges event key");
  assert.equal(store._rawHostEvent.has(reg.session_id), false, "DELETE purges hostevent key");
  ok("DELETE purges split event/host_event keys");
}

async function testRegistrationGuards() {
  const store = makeMemoryStore();
  const env = fixedEnv();

  // identical tokens rejected
  let res = await register(store, env, registration({ upload_token: "x", pull_token: "x" }));
  assert.equal(res.status, 400, "identical tokens rejected");

  // capture_spec must be a string (opaque), not an object
  res = await register(store, env, registration({ capture_spec: { kind: "x" } }));
  assert.equal(res.status, 400, "object spec rejected (must be opaque string)");

  // duplicate session id
  const reg = registration();
  res = await register(store, env, reg);
  assert.equal(res.status, 201);
  res = await register(store, env, reg);
  assert.equal(res.status, 409, "duplicate session rejected");

  // oversize spec
  res = await register(store, env, registration({ capture_spec: "x".repeat(100_000) }));
  assert.equal(res.status, 413, "oversize spec rejected");
  ok("registration guards");
}

async function testMethodAndPath() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  // wrong method on /sessions
  let res = await handle(req("GET", "/sessions"), store, env);
  assert.equal(res.status, 405);
  // unknown path
  res = await handle(req("GET", "/nope"), store, env);
  assert.equal(res.status, 404);
  // healthz
  res = await handle(req("GET", "/healthz"), store, env);
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "ok");
  ok("method + path");
}

async function testCors() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  // preflight from allowed origin
  let res = await handle(req("OPTIONS", "/sessions", { origin: ORIGIN }), store, env);
  assert.equal(res.status, 204);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  // disallowed origin -> no ACAO
  res = await handle(
    req("OPTIONS", "/sessions", { origin: "https://evil.example" }),
    store,
    env,
  );
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
  ok("CORS");
}

const tests = [
  testHappyPath,
  testOpaqueNonJsonSpec,
  testNewKindNeedsNoRelayChange,
  testWorkerSourceNeverParsesSpec,
  testTokensHashedNotStored,
  testNoContentKeyAnywhere,
  testRegistrationSecret,
  testTokenGating,
  testDualSizeCap,
  testBlobRequiresIntegrityHeaders,
  testIntegrityRelayedVerbatim,
  testRateLimitFallback,
  testRateLimitBinding,
  testRateLimitDoesNotClobberState,
  testRegistrationRateLimited,
  testEventRequiresContentLength,
  testTtlExpiry,
  testEventDoesNotClobberHostEvent,
  testLegacyMetaFallback,
  testExpiryPurgesSplitKeys,
  testDeleteSessionPurgesSplitKeys,
  testRegistrationGuards,
  testMethodAndPath,
  testCors,
  testIndexedBlobLifecycle,
  testIndexedBlobBadIndexRejected,
  testLegacyUploadKeepsPreIndexStatusShape,
  testDeleteAndExpiryPurgeIndexedBlobs,
  testV3HostEventPhasesRelayVerbatim,
];

// === Session-spanning capture plans (Pi protocol v3): indexed blobs =========

// PUT one blob at ?index=N (or unindexed when index === null).
async function putBlobAt(store, env, reg, index, bytes, plaintextLen = 3) {
  const sha = await sha256Hex(new Uint8Array([7, index === null ? 0 : index, 7]));
  const suffix = index === null ? "" : `?index=${index}`;
  const res = await handle(
    req("PUT", `/sessions/${reg.session_id}/blob${suffix}`, {
      token: reg.upload_token,
      raw: bytes,
      headers: {
        "X-Plaintext-Length": String(plaintextLen),
        "X-Plaintext-Sha256": sha,
      },
      origin: ORIGIN,
    }),
    store,
    env,
  );
  return { res, sha };
}

async function testIndexedBlobLifecycle() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);

  // Attempt 1 rides an EXPLICIT index 0 — it aliases the legacy slot.
  const b0 = new Uint8Array([10, 10, 10, 10]);
  let { res, sha } = await putBlobAt(store, env, reg, 0, b0);
  assert.equal(res.status, 200, "index 0 put 200");
  let j = await res.json();
  assert.equal(j.state, "ready", "index 0 keeps the legacy ready response");
  const sha0 = sha;

  // Attempts 2 and 3 ride their own keys; the session state stays untouched.
  const b1 = new Uint8Array([11, 11, 11, 11, 11]);
  ({ res, sha } = await putBlobAt(store, env, reg, 1, b1));
  assert.equal(res.status, 200, "index 1 put 200");
  j = await res.json();
  assert.equal(j.capture_index, 1);
  const sha1 = sha;
  const b2 = new Uint8Array([12, 12]);
  ({ res } = await putBlobAt(store, env, reg, 2, b2));
  assert.equal(res.status, 200, "index 2 put 200");

  // Status: legacy fields intact + the additive per-index summary.
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  j = await res.json();
  assert.equal(j.state, "ready");
  assert.equal(j.size, 4, "legacy size is index 0's");
  assert.deepEqual(j.integrity, { plaintext_len: 3, sha256: sha0 });
  assert.equal(Object.keys(j.blobs).length, 3, "blobs summary has all indexes");
  assert.equal(j.blobs["1"].size, 5);
  assert.deepEqual(j.blobs["1"].integrity, { plaintext_len: 3, sha256: sha1 });

  // Pull side: unindexed GET serves index 0; ?index=N serves its own bytes
  // with ITS integrity claim.
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/blob`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.deepEqual([...new Uint8Array(await res.arrayBuffer())], [...b0]);
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/blob?index=1`, {
      token: reg.pull_token,
    }),
    store,
    env,
  );
  assert.equal(res.status, 200, "indexed blob get 200");
  assert.equal(res.headers.get("X-Plaintext-Sha256"), sha1);
  assert.deepEqual([...new Uint8Array(await res.arrayBuffer())], [...b1]);

  // One upload per index: duplicates 409 for indexed AND legacy slots.
  ({ res } = await putBlobAt(store, env, reg, 1, b1));
  assert.equal(res.status, 409, "duplicate indexed upload rejected");
  ({ res } = await putBlobAt(store, env, reg, 0, b0));
  assert.equal(res.status, 409, "duplicate index-0 upload rejected");

  // An index nobody uploaded is not ready — never falls back to another blob.
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/blob?index=3`, {
      token: reg.pull_token,
    }),
    store,
    env,
  );
  assert.equal(res.status, 409, "missing indexed blob 409");
  j = await res.json();
  assert.equal(j.error, "not_ready");
  ok("indexed blob lifecycle");
}

async function testIndexedBlobBadIndexRejected() {
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);
  const bytes = new Uint8Array([1, 2, 3]);
  // At/above the bound, negative, non-integer, junk: 400 on both directions,
  // so a hostile index can never mint an R2 key no attempt could ever be
  // authorized for. "8" is the exact boundary: valid indexes are 0..7
  // (capture_index = attempt - 1, attempt <= MAX_CAPTURE_PLAN_ATTEMPTS = 8).
  for (const bad of ["8", "9", "-1", "1.5", "abc", "01"]) {
    const { res } = await putBlobAt(store, env, reg, bad, bytes);
    assert.equal(res.status, 400, `put index=${bad} rejected`);
    assert.equal((await res.json()).error, "bad_capture_index");
    const getRes = await handle(
      req("GET", `/sessions/${reg.session_id}/blob?index=${bad}`, {
        token: reg.pull_token,
      }),
      store,
      env,
    );
    assert.equal(getRes.status, 400, `get index=${bad} rejected`);
  }
  // The last authorizable slot (attempt 8 → index 7) is accepted.
  const { res: topRes } = await putBlobAt(store, env, reg, 7, bytes);
  assert.equal(topRes.status, 200, "index 7 (attempt cap - 1) accepted");
  ok("bad capture index rejected");
}

async function testLegacyUploadKeepsPreIndexStatusShape() {
  // A pre-v3 phone never sends ?index= — its session's status must keep the
  // exact historical shape (no `blobs` field appears).
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);
  const { res } = await putBlobAt(store, env, reg, null, new Uint8Array([1, 2]));
  assert.equal(res.status, 200);
  const statusRes = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  const j = await statusRes.json();
  assert.equal(j.state, "ready");
  assert.equal("blobs" in j, false, "legacy status carries no blobs field");
  ok("legacy upload keeps pre-index status shape");
}

async function testDeleteAndExpiryPurgeIndexedBlobs() {
  // DELETE path.
  let store = makeMemoryStore();
  let env = fixedEnv();
  let reg = registration();
  await register(store, env, reg);
  await putBlobAt(store, env, reg, 0, new Uint8Array([1]));
  await putBlobAt(store, env, reg, 2, new Uint8Array([2]));
  assert.equal(store._rawBlob.size, 2, "two blob keys stored");
  let res = await handle(
    req("DELETE", `/sessions/${reg.session_id}`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.equal(res.status, 204);
  assert.equal(store._rawBlob.size, 0, "DELETE purges every indexed blob");

  // TTL-expiry path.
  store = makeMemoryStore();
  env = fixedEnv();
  reg = registration({ ttl_s: 60 });
  await register(store, env, reg);
  await putBlobAt(store, env, reg, 0, new Uint8Array([1]));
  await putBlobAt(store, env, reg, 1, new Uint8Array([2]));
  env._setNow(1_000_000 + 61_000);
  res = await handle(
    req("GET", `/sessions/${reg.session_id}/status`, { token: reg.pull_token }),
    store,
    env,
  );
  assert.equal(res.status, 404, "expired session gone");
  assert.equal(store._rawBlob.size, 0, "expiry purges every indexed blob");
  ok("delete/expiry purge indexed blobs");
}

async function testV3HostEventPhasesRelayVerbatim() {
  // The v3 choreography events (capture_authorized / capture_result /
  // capture_refused / set terminals) are ordinary opaque host events to the
  // relay — stored and served verbatim, never parsed. Zero relay knowledge of
  // the new vocabulary is the invariant.
  const store = makeMemoryStore();
  const env = fixedEnv();
  const reg = registration();
  await register(store, env, reg);
  const events = [
    { phase: "capture_authorized", index: 1, attempt: 1 },
    { phase: "capture_result", index: 1, attempt: 1, accepted: true, estimated_snr_db: 30.5 },
    { phase: "capture_refused", index: 2, attempt: 2, code: "repeat_admission_refused", error: "budget spent" },
    { phase: "capture_set_complete", accepted: 3, capture_target: 3 },
  ];
  for (const event of events) {
    let res = await handle(
      req("POST", `/sessions/${reg.session_id}/host-event`, {
        token: reg.pull_token,
        body: event,
      }),
      store,
      env,
    );
    assert.equal(res.status, 200);
    res = await handle(
      req("GET", `/sessions/${reg.session_id}/phone-status`, {
        token: reg.upload_token,
        origin: ORIGIN,
      }),
      store,
      env,
    );
    const j = await res.json();
    assert.deepEqual(j.host_event, event, `${event.phase} relayed verbatim`);
  }
  ok("v3 host-event phases relay verbatim");
}

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

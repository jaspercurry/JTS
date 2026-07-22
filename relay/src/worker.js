// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Phone-mic capture relay — stateless, dumb, opaque dead-drop (build step 2).
//
// This is the entire relay: one Cloudflare Worker + one R2 bucket serving the
// whole fleet identically. It is O(1) and security-minimal by construction — no
// per-device record, no cert, and nothing to renew. Production may set one
// optional fleet registration secret so only configured Pis can mint sessions;
// that secret never decrypts audio and is not part of the open-source repo. See
// docs/phone-mic-relay-plan.md §§2, 4, 7, 8.
//
// LOAD-BEARING INVARIANTS (enforced by tests/js/relay_worker_test.mjs):
//
//   1. OPAQUE. The relay NEVER parses the `capture_spec` or the encrypted blob.
//      The spec is stored and served as an opaque string; the blob as opaque
//      bytes. Adding a measurement kind therefore needs ZERO relay changes. The
//      Worker reads only its OWN control fields (session id, tokens, ttl, size
//      cap) and the relay-control `event` envelope — never the payloads.
//   2. E2E. The relay never receives the `content_key` (it rides the page URL
//      fragment, which browsers never transmit). It stores ciphertext only and
//      cannot read room audio. The plaintext-integrity claim it relays is
//      verified by the Pi AFTER decrypt — the Worker cannot and does not check
//      it.
//   3. BOUNDED. Tokens are stored as SHA-256 hashes; sessions auto-expire at
//      ttl_s and are deleted-after-pull by the Pi; the upload size is capped at
//      BOTH the Worker and the Pi; phone-facing endpoints are per-session
//      rate-limited so a leaked upload_token cannot hammer the bucket.

// --- Tunables ----------------------------------------------------------------

const DEFAULT_TTL_S = 900; // 15 min
const MIN_TTL_S = 60;
const MAX_TTL_S = 3600;

// Hard ceiling enforced regardless of the per-session cap the Pi registers, so a
// buggy/hostile registration cannot authorize a gigabyte upload. Mirrors the
// Pi-side HARD_MAX_UPLOAD_BYTES.
const WORKER_HARD_MAX_UPLOAD_BYTES = 64 * 1024 * 1024;
const DEFAULT_MAX_UPLOAD_BYTES = 32 * 1024 * 1024;

// A capture spec is ~1 KB; cap the opaque string well above that but bounded.
const MAX_SPEC_BYTES = 64 * 1024;

// Session-spanning capture plans (Pi protocol v3, SPEC W2.3) key each admitted
// attempt's blob by a small integer `?index=` (absent = 0 → the legacy
// un-indexed key, byte-identical back-compat). One blob per admitted attempt,
// `capture_index = attempt - 1`, so the valid indexes are EXACTLY
// 0..MAX_CAPTURE_PLAN_ATTEMPTS-1 — the same value as the Pi-side plan attempt
// cap, applied to indexes with a strict inequality. Keep in lockstep with
// MAX_CAPTURE_PLAN_ATTEMPTS in jasper/capture_relay/spec.py (pinned by
// tests/test_capture_relay_spec.py).
const MAX_CAPTURE_PLAN_ATTEMPTS = 8;
// Relay-control event envelopes carry setup/progress metadata: phone
// {setup_validate:true, setup:{...}} / {armed:true, noise_floor:{...}} and host
// {phase:"setup_validated"|"sweep_complete"}. They are not audio payloads; the
// relay stores and relays them as bounded opaque JSON control state. The cap
// mirrors the Pi's calibration-upload JSON cap so a phone wizard can carry a
// user-provided mic calibration file without another endpoint.
const MAX_EVENT_BYTES = 1024 * 1024;
const MAX_SESSION_ID_LEN = 128;
const MAX_TOKEN_LEN = 512;

// Per-session fallback rate limit (used only when env.RELAY_RATELIMIT binding is
// absent — production uses Cloudflare's managed Rate Limit binding).
// Phone-facing endpoints only; the Pi's pull_token poll is not limited.
const RATE_WINDOW_MS = 10_000;
const RATE_MAX_REQUESTS = 80;

const DEFAULT_CAPTURE_ORIGINS = "https://capture.jasper.tech";
const REGISTRATION_TOKEN_HEADER = "X-JTS-Relay-Registration-Token";

// --- Storage abstraction ------------------------------------------------------
// The router is storage-agnostic so it is unit-testable with an in-memory store.
// Production binds an R2 bucket; tests inject makeMemoryStore().

// Blob key for one capture index. Index 0 keeps the historical un-indexed key
// so every pre-v3 session (and a v3 plan's first attempt) is byte-identical.
function blobKey(id, index) {
  return index ? `blob/${id}/${index}` : `blob/${id}`;
}

export function makeR2Store(bucket) {
  return {
    async getMeta(id) {
      const obj = await bucket.get(`meta/${id}`);
      if (!obj) return null;
      // NB: this parses the relay's OWN meta record — never the capture_spec,
      // which lives inside it as an opaque string.
      return JSON.parse(await obj.text());
    },
    async putMeta(id, meta) {
      await bucket.put(`meta/${id}`, JSON.stringify(meta));
    },
    async deleteMeta(id) {
      await bucket.delete(`meta/${id}`);
    },
    async getBlob(id, index = 0) {
      const obj = await bucket.get(blobKey(id, index));
      if (!obj) return null;
      return new Uint8Array(await obj.arrayBuffer());
    },
    async putBlob(id, bytes, index = 0) {
      await bucket.put(blobKey(id, index), bytes);
    },
    async deleteBlob(id, index = 0) {
      await bucket.delete(blobKey(id, index));
    },
    // Phone-owned control envelope — its OWN key so a phone POST /event can
    // never read-modify-write the Pi's host_event (see postEvent/postHostEvent
    // below for the clobber this eliminates).
    async getEvent(id) {
      const obj = await bucket.get(`event/${id}`);
      if (!obj) return null;
      return JSON.parse(await obj.text());
    },
    async putEvent(id, event) {
      await bucket.put(`event/${id}`, JSON.stringify(event));
    },
    async deleteEvent(id) {
      await bucket.delete(`event/${id}`);
    },
    // Pi-owned control envelope — its OWN key, symmetric with getEvent above.
    async getHostEvent(id) {
      const obj = await bucket.get(`hostevent/${id}`);
      if (!obj) return null;
      return JSON.parse(await obj.text());
    },
    async putHostEvent(id, event) {
      await bucket.put(`hostevent/${id}`, JSON.stringify(event));
    },
    async deleteHostEvent(id) {
      await bucket.delete(`hostevent/${id}`);
    },
  };
}

export function makeMemoryStore() {
  const meta = new Map();
  const blob = new Map();
  const event = new Map();
  const hostEvent = new Map();
  return {
    async getMeta(id) {
      const v = meta.get(id);
      return v ? JSON.parse(v) : null;
    },
    async putMeta(id, m) {
      meta.set(id, JSON.stringify(m));
    },
    async deleteMeta(id) {
      meta.delete(id);
    },
    async getBlob(id, index = 0) {
      const v = blob.get(blobKey(id, index));
      return v ? new Uint8Array(v) : null;
    },
    async putBlob(id, bytes, index = 0) {
      blob.set(blobKey(id, index), new Uint8Array(bytes));
    },
    async deleteBlob(id, index = 0) {
      blob.delete(blobKey(id, index));
    },
    async getEvent(id) {
      const v = event.get(id);
      return v ? JSON.parse(v) : null;
    },
    async putEvent(id, e) {
      event.set(id, JSON.stringify(e));
    },
    async deleteEvent(id) {
      event.delete(id);
    },
    async getHostEvent(id) {
      const v = hostEvent.get(id);
      return v ? JSON.parse(v) : null;
    },
    async putHostEvent(id, e) {
      hostEvent.set(id, JSON.stringify(e));
    },
    async deleteHostEvent(id) {
      hostEvent.delete(id);
    },
    // Test-only introspection.
    _rawMeta: meta,
    _rawBlob: blob,
    _rawEvent: event,
    _rawHostEvent: hostEvent,
  };
}

// --- Crypto / helpers ---------------------------------------------------------

export async function sha256Hex(input) {
  const bytes =
    typeof input === "string" ? new TextEncoder().encode(input) : input;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// Constant-time-ish hex comparison: equal length required, XOR-accumulate so the
// timing does not leak which character differs. Belt-and-suspenders: both inputs
// here are already SHA-256 *hex digests* (the presented token is hashed before
// this call), always 64 chars, so the length check can never leak token length,
// and a timing oracle could at worst leak digest bytes — uninvertible to the
// token. JS charCodeAt/`|=` is not a hardware-constant-time primitive, but that
// is acceptable given the inputs are pre-hashed, not the secrets themselves.
function timingSafeEqualHex(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function nowMs(env) {
  return (env && typeof env.now === "function" ? env.now : Date.now)();
}

function allowedOrigins(env) {
  return (env.CAPTURE_ORIGIN || DEFAULT_CAPTURE_ORIGINS)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function corsHeaders(env, request) {
  const origin = request.headers.get("Origin");
  const headers = {
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Access-Control-Allow-Headers":
      "Authorization,Content-Type,X-Plaintext-Length,X-Plaintext-Sha256",
    "Access-Control-Expose-Headers": "X-Plaintext-Length,X-Plaintext-Sha256",
    "Access-Control-Max-Age": "600",
    Vary: "Origin",
  };
  if (origin && allowedOrigins(env).includes(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
  }
  return headers;
}

function json(body, status, headers) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...(headers || {}) },
  });
}

function bearer(request) {
  const auth = request.headers.get("Authorization") || "";
  const m = /^Bearer\s+(.+)$/i.exec(auth);
  return m ? m[1].trim() : "";
}

// --- Auth + lifecycle ---------------------------------------------------------

async function authorize(meta, request, which) {
  const token = bearer(request);
  if (!token) return false;
  const presented = await sha256Hex(token);
  const expected =
    which === "upload" ? meta.upload_token_hash : meta.pull_token_hash;
  return timingSafeEqualHex(presented, expected);
}

async function registrationAuthorized(env, request) {
  const expectedToken = String(env.RELAY_REGISTRATION_TOKEN || "").trim();
  if (!expectedToken) return true;

  const presentedToken = (request.headers.get(REGISTRATION_TOKEN_HEADER) || "").trim();
  if (!presentedToken) return false;

  const [presented, expected] = await Promise.all([
    sha256Hex(presentedToken),
    sha256Hex(expectedToken),
  ]);
  return timingSafeEqualHex(presented, expected);
}

// Delete every stored artefact of a session: the legacy blob key, every
// indexed blob the meta records (session-spanning plans), and the meta/event
// keys. Shared by DELETE and proactive TTL expiry.
async function deleteSessionState(store, id, meta) {
  await store.deleteBlob(id, 0);
  const indexed = meta && meta.blobs ? Object.keys(meta.blobs) : [];
  for (const key of indexed) {
    const index = Number(key);
    if (Number.isInteger(index) && index > 0) {
      await store.deleteBlob(id, index);
    }
  }
  await store.deleteMeta(id);
  await store.deleteEvent(id);
  await store.deleteHostEvent(id);
}

// Returns the live meta, or null if missing/expired. Expired sessions are
// proactively deleted so the bucket self-cleans even before the R2 lifecycle
// rule fires.
async function loadLive(store, id, env) {
  const meta = await store.getMeta(id);
  if (!meta) return null;
  if (nowMs(env) > meta.expires_at) {
    await deleteSessionState(store, id, meta);
    return null;
  }
  return meta;
}

// Parse `?index=` off a blob route (session-spanning plans). Absent = 0 (the
// legacy key). Returns null for anything but an integer in
// 0..MAX_CAPTURE_PLAN_ATTEMPTS-1 (one slot per admitted attempt,
// capture_index = attempt - 1), so a hostile/buggy index can never mint a key
// no attempt could ever be authorized for.
function parseCaptureIndex(url) {
  const raw = url.searchParams.get("index");
  if (raw === null) return 0;
  if (!/^(0|[1-9][0-9]*)$/.test(raw)) return null;
  const index = Number(raw);
  return index >= MAX_CAPTURE_PLAN_ATTEMPTS ? null : index;
}

// --- Split-field readers (event / host_event) ---------------------------------
//
// event and host_event are written to their OWN keys (see putEvent/
// putHostEvent below) so a phone POST /event and a Pi POST /host-event can
// never read-modify-write the same meta object and clobber each other — the
// root cause of the 2026-07-15 JTS3 false phone timeout (mitigated, not
// fixed, by the Pi's #1507 terminal re-post latch).
//
// COMPAT WINDOW: a session registered by pre-deploy code still carries the
// value embedded in meta (the old registerSession/postEvent/postHostEvent
// wrote there), with no split key ever created for it. Prefer the split key;
// fall back to the legacy meta field only when the split key is absent. Once
// any post-deploy write lands, the split key exists and wins from then on.
// No version flag or migration step is needed — every session's TTL is
// <=3600s (MAX_TTL_S), so the legacy shape naturally retires within an hour
// of deploy.
async function readEvent(store, meta, id) {
  const v = await store.getEvent(id);
  return v !== null ? v : (meta.event ?? null);
}
async function readHostEvent(store, meta, id) {
  const v = await store.getHostEvent(id);
  return v !== null ? v : (meta.host_event ?? null);
}

// Module-level, per-isolate fallback rate state. Used ONLY when the
// Cloudflare-managed RELAY_RATELIMIT binding is absent (production declares it
// in wrangler.toml).
// It deliberately NEVER touches R2: an earlier version persisted the counter
// into meta/<id>, which (a) wrote R2 on every read-only GET /spec and (b)
// read-modify-wrote the shared state object, so a concurrent spec/event fetch
// could clobber the `ready`/`armed` control state and strand the Pi. Keeping the
// counter in isolate memory removes both: it cannot corrupt the durable state
// machine. It is per-isolate (not globally consistent), which is acceptable for
// a best-effort safety cap whose real bounds are the hard size cap + TTL.
const _fallbackRate = new Map();
const _FALLBACK_RATE_MAX_KEYS = 10000;

function fallbackRateLimited(key, now) {
  let r = _fallbackRate.get(key);
  if (!r || now - r.windowStart > RATE_WINDOW_MS) {
    r = { windowStart: now, count: 0 };
  }
  r.count += 1;
  _fallbackRate.set(key, r);
  if (_fallbackRate.size > _FALLBACK_RATE_MAX_KEYS) {
    for (const [k, v] of _fallbackRate) {
      if (now - v.windowStart > RATE_WINDOW_MS) _fallbackRate.delete(k);
    }
  }
  return r.count > RATE_MAX_REQUESTS;
}

// Per-session limit on the phone-facing endpoints (a leaked upload_token).
async function rateLimited(env, id) {
  if (env.RELAY_RATELIMIT && typeof env.RELAY_RATELIMIT.limit === "function") {
    const { success } = await env.RELAY_RATELIMIT.limit({ key: id });
    return !success;
  }
  return fallbackRateLimited(id, nowMs(env));
}

// Per-IP limit on OPEN registration so a flood of POST /sessions cannot fill the
// bucket with short-lived sessions (each is otherwise only bounded by TTL +
// per-session caps). Keyed distinctly from the session-id limiter.
async function registrationRateLimited(env, request) {
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  const key = `reg:${ip}`;
  if (env.RELAY_RATELIMIT && typeof env.RELAY_RATELIMIT.limit === "function") {
    const { success } = await env.RELAY_RATELIMIT.limit({ key });
    return !success;
  }
  return fallbackRateLimited(key, nowMs(env));
}

// --- Endpoint handlers --------------------------------------------------------

async function registerSession(request, store, env, cors) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid_json" }, 400, cors);
  }
  const id = body.session_id;
  const spec = body.capture_spec; // OPAQUE STRING — never parsed by the relay.
  const uploadToken = body.upload_token;
  const pullToken = body.pull_token;

  if (typeof id !== "string" || !id || id.length > MAX_SESSION_ID_LEN) {
    return json({ error: "bad_session_id" }, 400, cors);
  }
  if (typeof spec !== "string" || spec.length === 0) {
    return json({ error: "capture_spec_must_be_string" }, 400, cors);
  }
  if (new TextEncoder().encode(spec).length > MAX_SPEC_BYTES) {
    return json({ error: "capture_spec_too_large" }, 413, cors);
  }
  if (
    typeof uploadToken !== "string" ||
    !uploadToken ||
    uploadToken.length > MAX_TOKEN_LEN ||
    typeof pullToken !== "string" ||
    !pullToken ||
    pullToken.length > MAX_TOKEN_LEN
  ) {
    return json({ error: "bad_tokens" }, 400, cors);
  }
  if (uploadToken === pullToken) {
    // The two tokens gate different parties; identical tokens collapse the
    // privilege split.
    return json({ error: "tokens_must_differ" }, 400, cors);
  }
  if (await store.getMeta(id)) {
    return json({ error: "session_exists" }, 409, cors);
  }

  let ttl = Number.isInteger(body.ttl_s) ? body.ttl_s : DEFAULT_TTL_S;
  ttl = Math.max(MIN_TTL_S, Math.min(MAX_TTL_S, ttl));
  let cap = Number.isInteger(body.max_upload_bytes)
    ? body.max_upload_bytes
    : DEFAULT_MAX_UPLOAD_BYTES;
  cap = Math.max(1, Math.min(WORKER_HARD_MAX_UPLOAD_BYTES, cap));

  const now = nowMs(env);
  const meta = {
    session_id: id,
    capture_spec: spec, // opaque
    upload_token_hash: await sha256Hex(uploadToken),
    pull_token_hash: await sha256Hex(pullToken),
    ttl_s: ttl,
    created_at: now,
    expires_at: now + ttl * 1000,
    max_upload_bytes: cap,
    state: "pending",
    // event / host_event deliberately NOT stored here — they live in their
    // own keys (see readEvent/readHostEvent). A freshly-registered session
    // has neither key yet; readers default to null via the ?? fallback.
    integrity: null,
    size: 0,
  };
  await store.putMeta(id, meta);
  return json({ session_id: id, state: "pending", expires_at: meta.expires_at }, 201, cors);
}

async function getSpec(meta, store, id, env, cors) {
  // Serve the opaque spec verbatim. We DECODE nothing about its structure.
  return new Response(meta.capture_spec, {
    status: 200,
    headers: { "content-type": "application/json", ...cors },
  });
}

async function postEvent(request, store, meta, id, env, cors) {
  // Require Content-Length and cap it BEFORE reading. A missing header must NOT
  // default to 0 (which would skip the cap and read an unbounded chunked body
  // into the Worker, then persist + echo it on every Pi poll). Mirror putBlob.
  const len = Number(request.headers.get("content-length") || "-1");
  if (!Number.isFinite(len) || len <= 0) {
    return json({ error: "content_length_required" }, 411, cors);
  }
  if (len > MAX_EVENT_BYTES) {
    return json({ error: "event_too_large" }, 413, cors);
  }
  let event;
  try {
    event = await request.json();
  } catch {
    return json({ error: "invalid_json" }, 400, cors);
  }
  if (typeof event !== "object" || event === null) {
    return json({ error: "event_must_be_object" }, 400, cors);
  }
  // The event is the relay's own control envelope (NOT a capture payload). We
  // relay it verbatim; the Pi interprets fields like `armed`. Written to its
  // OWN key — NEVER read-modify-write meta, which is what let a phone event
  // post clobber a concurrently-landing Pi host_event (see readEvent above).
  await store.putEvent(id, event);
  return json({ ok: true }, 200, cors);
}

async function postHostEvent(request, store, meta, id, env, cors) {
  const len = Number(request.headers.get("content-length") || "-1");
  if (!Number.isFinite(len) || len <= 0) {
    return json({ error: "content_length_required" }, 411, cors);
  }
  if (len > MAX_EVENT_BYTES) {
    return json({ error: "event_too_large" }, 413, cors);
  }
  let event;
  try {
    event = await request.json();
  } catch {
    return json({ error: "invalid_json" }, 400, cors);
  }
  if (typeof event !== "object" || event === null) {
    return json({ error: "event_must_be_object" }, 400, cors);
  }
  // Own key, symmetric with postEvent above — the Pi's terminal write can
  // never be reverted by an interleaved phone event post.
  await store.putHostEvent(id, event);
  return json({ ok: true }, 200, cors);
}

async function putBlob(request, store, meta, id, env, cors, url) {
  const index = parseCaptureIndex(url);
  if (index === null) {
    return json(
      { error: "bad_capture_index", max: MAX_CAPTURE_PLAN_ATTEMPTS - 1 }, 400, cors,
    );
  }
  const explicitIndex = url.searchParams.get("index") !== null;
  // One upload per key: the legacy/index-0 slot keeps its historical
  // state-based guard; indexed slots (plans) are guarded per index.
  const indexTaken =
    meta.blobs && Object.prototype.hasOwnProperty.call(meta.blobs, String(index));
  if (index === 0 ? meta.state === "ready" : indexTaken) {
    return json({ error: "already_uploaded" }, 409, cors);
  }
  const contentLength = Number(request.headers.get("content-length") || "-1");
  if (!Number.isFinite(contentLength) || contentLength <= 0) {
    return json({ error: "content_length_required" }, 411, cors);
  }
  // Dual cap: reject by declared length BEFORE buffering so a leaked token can
  // never stream more than the cap into the bucket.
  if (contentLength > meta.max_upload_bytes) {
    return json({ error: "blob_too_large", max: meta.max_upload_bytes }, 413, cors);
  }
  const plaintextLen = Number(request.headers.get("X-Plaintext-Length") || "-1");
  const plaintextSha = (request.headers.get("X-Plaintext-Sha256") || "").toLowerCase();
  if (!Number.isInteger(plaintextLen) || plaintextLen < 0) {
    return json({ error: "missing_plaintext_length" }, 400, cors);
  }
  if (!/^[0-9a-f]{64}$/.test(plaintextSha)) {
    return json({ error: "missing_plaintext_sha256" }, 400, cors);
  }

  const buf = new Uint8Array(await request.arrayBuffer());
  // Defense in depth: enforce the cap on actual bytes too.
  if (buf.length > meta.max_upload_bytes) {
    return json({ error: "blob_too_large", max: meta.max_upload_bytes }, 413, cors);
  }
  if (buf.length === 0) {
    return json({ error: "empty_blob" }, 400, cors);
  }

  await store.putBlob(id, buf, index);
  // The relay stores the integrity CLAIM and relays it; it cannot verify it (it
  // never sees plaintext). The Pi verifies after decrypt.
  const integrity = { plaintext_len: plaintextLen, sha256: plaintextSha };
  if (explicitIndex) {
    // Per-index summary for plan-aware Pis. Recorded only for requests that
    // opted into indexing, so a legacy upload's meta stays byte-identical.
    meta.blobs = {
      ...(meta.blobs || {}),
      [String(index)]: { integrity, size: buf.length },
    };
  }
  if (index === 0) {
    meta.integrity = integrity;
    meta.size = buf.length;
    meta.state = "ready";
    await store.putMeta(id, meta);
    return json({ ok: true, state: "ready", size: buf.length }, 200, cors);
  }
  await store.putMeta(id, meta);
  return json(
    { ok: true, capture_index: index, size: buf.length }, 200, cors,
  );
}

async function getStatus(store, meta, id, cors) {
  const [event, hostEvent] = await Promise.all([
    readEvent(store, meta, id),
    readHostEvent(store, meta, id),
  ]);
  return json(
    {
      state: meta.state,
      size: meta.size,
      integrity: meta.integrity,
      event,
      host_event: hostEvent,
      expires_at: meta.expires_at,
      // Per-index blob summary (session-spanning plans). Additive: present
      // only once an indexed upload landed, so pre-v3 sessions keep the
      // exact historical status shape.
      ...(meta.blobs ? { blobs: meta.blobs } : {}),
    },
    200,
    cors,
  );
}

async function getPhoneStatus(store, meta, id, cors) {
  const hostEvent = await readHostEvent(store, meta, id);
  return json(
    {
      state: meta.state,
      host_event: hostEvent,
      expires_at: meta.expires_at,
    },
    200,
    cors,
  );
}

async function getBlob(meta, store, id, cors, url) {
  const index = parseCaptureIndex(url);
  if (index === null) {
    return json(
      { error: "bad_capture_index", max: MAX_CAPTURE_PLAN_ATTEMPTS - 1 }, 400, cors,
    );
  }
  if (index > 0) {
    const entry = meta.blobs ? meta.blobs[String(index)] : undefined;
    if (!entry) {
      return json({ error: "not_ready", capture_index: index }, 409, cors);
    }
    const bytes = await store.getBlob(id, index);
    if (!bytes) {
      return json({ error: "blob_missing" }, 410, cors);
    }
    return new Response(bytes, {
      status: 200,
      headers: {
        "content-type": "application/octet-stream",
        "X-Plaintext-Length": String(entry.integrity?.plaintext_len ?? ""),
        "X-Plaintext-Sha256": entry.integrity?.sha256 ?? "",
        ...cors,
      },
    });
  }
  if (meta.state !== "ready") {
    return json({ error: "not_ready", state: meta.state }, 409, cors);
  }
  const bytes = await store.getBlob(id);
  if (!bytes) {
    return json({ error: "blob_missing" }, 410, cors);
  }
  const headers = {
    "content-type": "application/octet-stream",
    "X-Plaintext-Length": String(meta.integrity?.plaintext_len ?? ""),
    "X-Plaintext-Sha256": meta.integrity?.sha256 ?? "",
    ...cors,
  };
  // Non-destructive: the Pi DELETEs explicitly after a successful
  // decrypt+verify, so a transient decrypt failure can retry the pull. TTL is
  // the backstop if the Pi never deletes.
  return new Response(bytes, { status: 200, headers });
}

async function deleteSession(store, id, meta, cors) {
  await deleteSessionState(store, id, meta);
  return new Response(null, { status: 204, headers: cors });
}

// --- Router -------------------------------------------------------------------

export async function handle(request, store, env) {
  env = env || {};
  const cors = corsHeaders(env, request);

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: cors });
  }

  const url = new URL(request.url);
  const parts = url.pathname.split("/").filter(Boolean); // e.g. ["sessions","ID","blob"]

  // POST /sessions
  if (parts.length === 1 && parts[0] === "sessions") {
    if (request.method !== "POST") {
      return json({ error: "method_not_allowed" }, 405, cors);
    }
    if (!(await registrationAuthorized(env, request))) {
      return json({ error: "registration_unauthorized" }, 401, cors);
    }
    if (await registrationRateLimited(env, request)) {
      return json({ error: "rate_limited" }, 429, cors);
    }
    return registerSession(request, store, env, cors);
  }

  // /sessions/:id[/sub]
  if (parts.length >= 2 && parts[0] === "sessions") {
    const id = decodeURIComponent(parts[1]);
    const sub = parts[2] || "";
    if (parts.length > 3) return json({ error: "not_found" }, 404, cors);

    const meta = await loadLive(store, id, env);
    if (!meta) return json({ error: "not_found" }, 404, cors);

    // DELETE /sessions/:id  (pull_token)
    if (sub === "" && request.method === "DELETE") {
      if (!(await authorize(meta, request, "pull"))) {
        return json({ error: "unauthorized" }, 401, cors);
      }
      return deleteSession(store, id, meta, cors);
    }

    // GET /sessions/:id/status  (pull_token)
    if (sub === "status" && request.method === "GET") {
      if (!(await authorize(meta, request, "pull"))) {
        return json({ error: "unauthorized" }, 401, cors);
      }
      return getStatus(store, meta, id, cors);
    }

    // POST /sessions/:id/host-event  (pull_token)
    if (sub === "host-event" && request.method === "POST") {
      if (!(await authorize(meta, request, "pull"))) {
        return json({ error: "unauthorized" }, 401, cors);
      }
      return postHostEvent(request, store, meta, id, env, cors);
    }

    // GET /sessions/:id/blob[?index=N]  (pull_token)
    if (sub === "blob" && request.method === "GET") {
      if (!(await authorize(meta, request, "pull"))) {
        return json({ error: "unauthorized" }, 401, cors);
      }
      return getBlob(meta, store, id, cors, url);
    }

    // --- phone-facing (upload_token), rate-limited ---
    const phoneRoute =
      (sub === "spec" && request.method === "GET") ||
      (sub === "phone-status" && request.method === "GET") ||
      (sub === "event" && request.method === "POST") ||
      (sub === "blob" && request.method === "PUT");
    if (phoneRoute) {
      if (!(await authorize(meta, request, "upload"))) {
        return json({ error: "unauthorized" }, 401, cors);
      }
      if (await rateLimited(env, id)) {
        return json({ error: "rate_limited" }, 429, cors);
      }
      if (sub === "spec") return getSpec(meta, store, id, env, cors);
      if (sub === "phone-status") return getPhoneStatus(store, meta, id, cors);
      if (sub === "event") return postEvent(request, store, meta, id, env, cors);
      if (sub === "blob") return putBlob(request, store, meta, id, env, cors, url);
    }

    return json({ error: "not_found" }, 404, cors);
  }

  if (parts.length === 1 && parts[0] === "healthz") {
    return new Response("ok", { status: 200, headers: cors });
  }

  return json({ error: "not_found" }, 404, cors);
}

// --- Cloudflare entrypoint ----------------------------------------------------

export default {
  async fetch(request, env) {
    if (!env.RELAY_BUCKET) {
      return json({ error: "relay_misconfigured" }, 500, {});
    }
    return handle(request, makeR2Store(env.RELAY_BUCKET), env);
  },
};

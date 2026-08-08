// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Phone-side relay client for the capture page (build step 3).
//
// Talks to the relay Worker (relay/src/worker.js) with the upload_token only
// (the pull_token stays on the Pi). All requests are plain HTTPS fetches to the
// trusted relay origin; the page and the Pi never talk directly. `fetchImpl` is
// injectable so the contract is testable without a network
// (tests/js/capture_relay_client_test.mjs).

export class RelayError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = "RelayError";
    this.status = status;
    this.body = body;
  }
}

// The Pi refuses a level feed after eight seconds without a fresh batch.  Keep
// each small control request well inside that safety window so a stalled fetch
// cannot freeze the page's serialized meter loop. Blob uploads are intentionally
// excluded; their bounded size and transfer time are a different contract.
export const RELAY_CONTROL_TIMEOUT_MS = 3000;
const EVENT_POST_TAILS = new Map();

// The rejection a timed-out control request produces, carrying a MACHINE tag.
//
// Why a tag and not a name/message test: `_controlFetch` aborts with a named
// reason so the household never reads the browser's "signal is aborted without
// reason." (the run-19 defect), and per the AbortController spec fetch then
// rejects with THAT value — an ordinary Error whose `name` is "Error" and whose
// message says nothing about aborting. So the very fix that made the copy
// friendly also made every `name === "AbortError"` / `/signal is aborted/`
// classifier silently stop matching real timeouts. Callers MUST key on
// `err.relayTimeout`; the string is for humans only and may be reworded freely.
export class RelayTimeoutError extends Error {
  constructor(message) {
    super(message);
    this.name = "RelayTimeoutError";
    // Own property, not just the class: a page bundle and this module can be
    // loaded from different URLs (each import stamp is its own module
    // instance), so `instanceof` is not a reliable cross-module test here.
    this.relayTimeout = true;
  }
}

export class RelayClient {
  constructor({ baseUrl, sessionId, uploadToken, fetchImpl } = {}) {
    if (!baseUrl) throw new Error("baseUrl required");
    if (!sessionId) throw new Error("sessionId required");
    if (!uploadToken) throw new Error("uploadToken required");
    this.baseUrl = String(baseUrl).replace(/\/+$/, "");
    this.sessionId = sessionId;
    this.uploadToken = uploadToken;
    this.capturePageIdentity = null;
    this.transportIntegrity = null;
    this.authenticatedEventsRequired = false;
    this._eventSequence = 0;
    this._eventSequenceStorageKey = `jts.capture.event-sequence.${sessionId}`;
    this._fetch = fetchImpl || ((...a) => globalThis.fetch(...a));
  }

  setCapturePageIdentity(identity) {
    if (!identity || typeof identity !== "object" || Array.isArray(identity)) {
      throw new Error("capture page identity required");
    }
    this.capturePageIdentity = Object.freeze({
      schema_version: Number(identity.schema_version),
      capture_protocol_version: Number(identity.capture_protocol_version),
      supported_capture_protocol_versions: Array.isArray(
        identity.supported_capture_protocol_versions
      ) ? identity.supported_capture_protocol_versions.map(Number) : [],
      capture_page_build: String(identity.capture_page_build || ""),
    });
  }

  setTransportIntegrity(integrity, { required = false } = {}) {
    if (
      !integrity ||
      typeof integrity.authenticatePhoneEvent !== "function"
    ) {
      throw new Error("capture transport integrity helper required");
    }
    this.transportIntegrity = integrity;
    this.authenticatedEventsRequired = Boolean(required);
  }

  _url(suffix) {
    return `${this.baseUrl}/sessions/${encodeURIComponent(this.sessionId)}${suffix}`;
  }

  _authHeaders(extra) {
    return { Authorization: `Bearer ${this.uploadToken}`, ...(extra || {}) };
  }

  _storedEventSequence() {
    try {
      const value = Number(
        globalThis.sessionStorage?.getItem(this._eventSequenceStorageKey),
      );
      return Number.isSafeInteger(value) && value >= 0 ? value : 0;
    } catch {
      // Privacy modes may expose storage but throw on access.
      return 0;
    }
  }

  _nextEventSequence() {
    // A bfcache-restored client can predate an intervening reload.
    this._eventSequence = Math.max(
      this._eventSequence,
      this._storedEventSequence(),
    ) + 1;
    try {
      globalThis.sessionStorage?.setItem(
        this._eventSequenceStorageKey,
        String(this._eventSequence),
      );
    } catch {}
    return this._eventSequence;
  }

  _serializeEventPost(operation) {
    const key = this._eventSequenceStorageKey;
    const prior = EVENT_POST_TAILS.get(key) ?? Promise.resolve();
    const queued = prior.then(operation, operation);
    const tail = queued.catch(() => undefined);
    EVENT_POST_TAILS.set(key, tail);
    void tail.then(() => {
      if (EVENT_POST_TAILS.get(key) === tail) EVENT_POST_TAILS.delete(key);
    });
    return queued;
  }

  async _controlFetch(
    suffix,
    init,
    consume,
    timeoutMs = RELAY_CONTROL_TIMEOUT_MS,
  ) {
    const controller = new AbortController();
    const timeout = Math.max(250, Number(timeoutMs) || RELAY_CONTROL_TIMEOUT_MS);
    // A named reason (not a bare `.abort()`) so a timed-out control request
    // never surfaces the browser's default "signal is aborted without
    // reason." to the household — that raw DOMException text was leaking
    // through captureFailureMessage() verbatim (run-19 defect). Fetch
    // rejects with this exact reason value per the AbortController spec,
    // which is why the reason is a TAGGED error: see RelayTimeoutError.
    const timer = setTimeout(
      () => controller.abort(
        new RelayTimeoutError(
          "timed out waiting for the speaker's measurement relay",
        ),
      ),
      timeout,
    );
    try {
      const res = await this._fetch(this._url(suffix), {
        ...(init || {}),
        signal: controller.signal,
      });
      return await consume(res);
    } finally {
      clearTimeout(timer);
    }
  }

  async _failure(res) {
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    return new RelayError(
      (body && body.error) || `relay ${res.status}`,
      res.status,
      body,
    );
  }

  // Fetch the exact opaque spec bytes. Integrity is checked before JSON parsing
  // by the page orchestrator; the relay never parses this string.
  async fetchSpecText() {
    const res = await this._fetch(this._url("/spec"), {
      method: "GET",
      headers: this._authHeaders(),
    });
    if (!res.ok) throw await this._failure(res);
    return res.text();
  }

  async fetchSpec() {
    return JSON.parse(await this.fetchSpecText());
  }

  // Drop a relay-control event (e.g. {armed:true}) the Pi polls for.
  postEvent(event, { timeoutMs = RELAY_CONTROL_TIMEOUT_MS } = {}) {
    return this._serializeEventPost(async () => {
      if (!this.capturePageIdentity) {
        throw new Error("capture page compatibility was not established");
      }
      const payload = { ...event, capture_page: this.capturePageIdentity };
      let body = payload;
      if (this.authenticatedEventsRequired) {
        if (!this.transportIntegrity) {
          throw new Error("authenticated capture events are not configured");
        }
        body = await this.transportIntegrity.authenticatePhoneEvent(
          payload,
          this._nextEventSequence(),
        );
      }
      return this._controlFetch("/event", {
        method: "POST",
        headers: this._authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
      }, async (res) => {
        if (!res.ok) throw await this._failure(res);
        return res.json();
      }, timeoutMs);
    });
  }

  // Poll Pi-side progress for this capture. This uses the upload token, so the
  // Worker returns only phone-safe progress state, never the Pi pull-token
  // integrity/blob details.
  async fetchPhoneStatus({ timeoutMs = RELAY_CONTROL_TIMEOUT_MS } = {}) {
    return this._controlFetch("/phone-status", {
      method: "GET",
      headers: this._authHeaders(),
    }, async (res) => {
      if (!res.ok) throw await this._failure(res);
      return res.json();
    }, timeoutMs);
  }

  // Upload IV‖ciphertext with the plaintext integrity the Pi verifies.
  // `captureIndex` (session-spanning capture plans, SPEC W2.3)
  // is the 0-based relay blob slot for one admitted attempt
  // (`capture_index = attempt - 1`); omitted/undefined keeps today's
  // byte-identical single-capture request (no `?index=`, aliasing the
  // Worker's legacy un-indexed key — see relay/src/worker.js's `blobKey`).
  async putBlob(blob, plaintextLen, sha256Hex, captureIndex) {
    const bytes = blob instanceof Uint8Array ? blob : new Uint8Array(blob);
    const path = Number.isInteger(captureIndex) && captureIndex >= 0
      ? `/blob?index=${captureIndex}`
      : "/blob";
    const res = await this._fetch(this._url(path), {
      method: "PUT",
      headers: this._authHeaders({
        "Content-Type": "application/octet-stream",
        "Content-Length": String(bytes.length),
        "X-Plaintext-Length": String(plaintextLen),
        "X-Plaintext-Sha256": sha256Hex,
      }),
      body: bytes,
    });
    if (!res.ok) throw await this._failure(res);
    return res.json();
  }
}

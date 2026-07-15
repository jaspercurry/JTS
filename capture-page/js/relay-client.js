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

  async _controlFetch(
    suffix,
    init,
    consume,
    timeoutMs = RELAY_CONTROL_TIMEOUT_MS,
  ) {
    const controller = new AbortController();
    const timeout = Math.max(250, Number(timeoutMs) || RELAY_CONTROL_TIMEOUT_MS);
    const timer = setTimeout(() => controller.abort(), timeout);
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
  async postEvent(event, { timeoutMs = RELAY_CONTROL_TIMEOUT_MS } = {}) {
    if (!this.capturePageIdentity) {
      throw new Error("capture page compatibility was not established");
    }
    const payload = { ...event, capture_page: this.capturePageIdentity };
    let body = payload;
    if (this.authenticatedEventsRequired) {
      if (!this.transportIntegrity) {
        throw new Error("authenticated capture events are not configured");
      }
      this._eventSequence += 1;
      body = await this.transportIntegrity.authenticatePhoneEvent(
        payload,
        this._eventSequence,
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
  async putBlob(blob, plaintextLen, sha256Hex) {
    const bytes = blob instanceof Uint8Array ? blob : new Uint8Array(blob);
    const res = await this._fetch(this._url("/blob"), {
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

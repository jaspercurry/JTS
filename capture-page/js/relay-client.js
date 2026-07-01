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

export class RelayClient {
  constructor({ baseUrl, sessionId, uploadToken, fetchImpl } = {}) {
    if (!baseUrl) throw new Error("baseUrl required");
    if (!sessionId) throw new Error("sessionId required");
    if (!uploadToken) throw new Error("uploadToken required");
    this.baseUrl = String(baseUrl).replace(/\/+$/, "");
    this.sessionId = sessionId;
    this.uploadToken = uploadToken;
    this._fetch = fetchImpl || ((...a) => globalThis.fetch(...a));
  }

  _url(suffix) {
    return `${this.baseUrl}/sessions/${encodeURIComponent(this.sessionId)}${suffix}`;
  }

  _authHeaders(extra) {
    return { Authorization: `Bearer ${this.uploadToken}`, ...(extra || {}) };
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

  // Fetch the opaque spec and parse it HERE (the relay never parsed it).
  async fetchSpec() {
    const res = await this._fetch(this._url("/spec"), {
      method: "GET",
      headers: this._authHeaders(),
    });
    if (!res.ok) throw await this._failure(res);
    return res.json();
  }

  // Drop a relay-control event (e.g. {armed:true}) the Pi polls for.
  async postEvent(event) {
    const res = await this._fetch(this._url("/event"), {
      method: "POST",
      headers: this._authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(event),
    });
    if (!res.ok) throw await this._failure(res);
    return res.json();
  }

  // Poll Pi-side progress for this capture. This uses the upload token, so the
  // Worker returns only phone-safe progress state, never the Pi pull-token
  // integrity/blob details.
  async fetchPhoneStatus() {
    const res = await this._fetch(this._url("/phone-status"), {
      method: "GET",
      headers: this._authHeaders(),
    });
    if (!res.ok) throw await this._failure(res);
    return res.json();
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

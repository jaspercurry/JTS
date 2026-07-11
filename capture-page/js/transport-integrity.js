// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// End-to-end integrity for relay-visible control data. The content key arrives
// only in the URL fragment, so the relay cannot forge either the exact spec MAC
// or a phone-event MAC. The matching implementation is
// jasper/capture_relay/integrity.py.

const KEY_DERIVATION_LABEL = "jts-capture-transport-integrity-key-v1";
const MESSAGE_DOMAIN = new TextEncoder().encode("jts-capture-transport-integrity-v1\0");
const SPEC_KIND = "capture-spec";
const EVENT_KIND = "phone-event";
const INTEGRITY_SCHEMA_VERSION = 1;

function base64urlToBytes(value) {
  const b64 = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
  const pad = b64.length % 4 === 0 ? "" : "=".repeat(4 - (b64.length % 4));
  const bin = atob(b64 + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToBase64url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function concat(parts) {
  const size = parts.reduce((total, part) => total + part.length, 0);
  const out = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

function uintBytes(value, width) {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error("invalid integrity length");
  const out = new Uint8Array(width);
  let remaining = value;
  for (let i = width - 1; i >= 0; i -= 1) {
    out[i] = remaining & 0xff;
    remaining = Math.floor(remaining / 256);
  }
  if (remaining !== 0) throw new Error("integrity length overflow");
  return out;
}

function framedMessage(kind, sessionId, payload) {
  const encoder = new TextEncoder();
  const kindBytes = encoder.encode(String(kind || ""));
  const sessionBytes = encoder.encode(String(sessionId || ""));
  const payloadBytes = payload instanceof Uint8Array ? payload : encoder.encode(String(payload));
  if (!kindBytes.length || !sessionBytes.length) {
    throw new Error("capture integrity message is missing identity");
  }
  return concat([
    MESSAGE_DOMAIN,
    uintBytes(kindBytes.length, 2),
    kindBytes,
    uintBytes(sessionBytes.length, 2),
    sessionBytes,
    uintBytes(payloadBytes.length, 8),
    payloadBytes,
  ]);
}

async function importDerivedKey(contentKeyB64) {
  const raw = base64urlToBytes(contentKeyB64);
  if (raw.length !== 32) throw new Error("content key must be exactly 32 bytes");
  const derivationKey = await crypto.subtle.importKey(
    "raw",
    raw,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const derived = new Uint8Array(await crypto.subtle.sign(
    "HMAC",
    derivationKey,
    new TextEncoder().encode(KEY_DERIVATION_LABEL),
  ));
  return crypto.subtle.importKey(
    "raw",
    derived,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

async function sign(key, kind, sessionId, payload) {
  const tag = await crypto.subtle.sign(
    "HMAC",
    key,
    framedMessage(kind, sessionId, payload),
  );
  return bytesToBase64url(new Uint8Array(tag));
}

function constantTimeEqual(left, right) {
  let a;
  let b;
  try {
    a = base64urlToBytes(left);
    b = base64urlToBytes(right);
  } catch {
    return false;
  }
  let mismatch = a.length ^ b.length;
  const width = Math.max(a.length, b.length);
  for (let i = 0; i < width; i += 1) {
    mismatch |= (a[i % (a.length || 1)] || 0) ^ (b[i % (b.length || 1)] || 0);
  }
  return mismatch === 0;
}

export async function createTransportIntegrity(contentKeyB64, sessionId) {
  const key = await importDerivedKey(contentKeyB64);
  const id = String(sessionId || "");
  if (!id) throw new Error("capture session id required for integrity");
  return Object.freeze({
    async captureSpecMac(specText) {
      return sign(key, SPEC_KIND, id, new TextEncoder().encode(String(specText)));
    },
    async verifyCaptureSpec(specText, expectedMac) {
      const observed = await this.captureSpecMac(specText);
      if (!constantTimeEqual(observed, expectedMac)) {
        throw new Error("capture spec integrity check failed");
      }
    },
    async authenticatePhoneEvent(event, sequence) {
      if (!Number.isInteger(sequence) || sequence < 1) {
        throw new Error("phone event sequence must be a positive integer");
      }
      const payload = JSON.stringify(event);
      const mac = await sign(
        key,
        `${EVENT_KIND}:${sequence}`,
        id,
        new TextEncoder().encode(payload),
      );
      return {
        authenticated_event: {
          schema_version: INTEGRITY_SCHEMA_VERSION,
          sequence,
          payload,
          mac,
        },
      };
    },
  });
}

export async function verifyAndParseCaptureSpec(
  specText,
  { contentKeyB64, sessionId, specMac } = {},
) {
  let spec;
  try {
    spec = JSON.parse(String(specText));
  } catch {
    throw new Error("capture spec is invalid");
  }
  if (!spec || typeof spec !== "object" || Array.isArray(spec)) {
    throw new Error("capture spec is invalid");
  }
  const protocol = Number(spec.capture_protocol_version || 1);
  const integrity = await createTransportIntegrity(contentKeyB64, sessionId);
  if (specMac) {
    await integrity.verifyCaptureSpec(String(specText), specMac);
  } else if (protocol >= 2) {
    // Old protocol-1 links did not carry a spec MAC. They remain usable, but
    // can never be downgraded into satisfying a v2 capture.
    throw new Error("capture spec integrity proof is missing");
  }
  return { spec, integrity };
}

export const _internal = {
  framedMessage,
  constantTimeEqual,
};


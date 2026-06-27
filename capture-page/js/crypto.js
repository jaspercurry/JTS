// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Page-side end-to-end encryption for the capture relay (build steps 3 + 5).
//
// The content_key (AES-256-GCM, 32 bytes) is minted by the Pi and delivered to
// this page ONLY in the URL fragment — the one part of a URL browsers never
// transmit — so the relay never sees it and stores ciphertext only. This module
// encrypts the recorded WAV here and computes a PLAINTEXT integrity tag (length
// + SHA-256) that the Pi verifies AFTER decrypt, BEFORE analysis. The matching
// Pi-side decrypt + cross-language round-trip proof land in step 5
// (jasper/capture_relay/crypto.py); the wire format is the contract between them:
//
//   key   : 32 raw bytes, base64url in the fragment
//   blob  : IV(12 bytes) ‖ AES-256-GCM(ciphertext ‖ 16-byte tag)
//   hash  : lowercase hex SHA-256 over the PLAINTEXT WAV bytes
//   length: plaintext WAV byte count
//
// Pinned by tests/js/capture_crypto_test.mjs (and the cross-language test in
// step 5).

const IV_BYTES = 12;

export function base64urlToBytes(b64url) {
  const b64 = String(b64url).replace(/-/g, "+").replace(/_/g, "/");
  const pad = b64.length % 4 === 0 ? "" : "=".repeat(4 - (b64.length % 4));
  const bin = atob(b64 + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function bytesToHex(bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let s = "";
  for (const b of u8) s += b.toString(16).padStart(2, "0");
  return s;
}

export async function sha256Hex(bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const digest = await crypto.subtle.digest("SHA-256", u8);
  return bytesToHex(new Uint8Array(digest));
}

export async function importContentKey(b64urlKey) {
  const raw = base64urlToBytes(b64urlKey);
  if (raw.length !== 32) {
    throw new Error(`content_key must be 32 bytes, got ${raw.length}`);
  }
  return crypto.subtle.importKey("raw", raw, { name: "AES-GCM" }, false, ["encrypt"]);
}

// Encrypt plaintext WAV bytes. Returns the relay blob (IV ‖ ciphertext) and the
// plaintext integrity the Pi verifies after decrypt.
export async function encryptWav(key, wavBytes) {
  const plaintext = wavBytes instanceof Uint8Array ? wavBytes : new Uint8Array(wavBytes);
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const ctBuf = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, plaintext);
  const ciphertext = new Uint8Array(ctBuf);
  const blob = new Uint8Array(iv.length + ciphertext.length);
  blob.set(iv, 0);
  blob.set(ciphertext, iv.length);
  return {
    blob,
    plaintextLen: plaintext.length,
    sha256: await sha256Hex(plaintext),
  };
}

export const _format = { IV_BYTES };

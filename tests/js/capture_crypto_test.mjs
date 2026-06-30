// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Harness for the capture page's E2E crypto (build steps 3 + 5).
//
// Proves the wire format the Pi-side decrypt (step 5) must match: AES-256-GCM,
// 12-byte IV prepended, plaintext SHA-256 + length integrity. Uses WebCrypto
// (global in node) to encrypt with the page module and decrypt independently,
// proving the round-trip. Prints {"ok":true}.
//
//   node tests/js/capture_crypto_test.mjs

import assert from "node:assert/strict";
import { createHash } from "node:crypto";

import {
  importContentKey,
  encryptWav,
  sha256Hex,
  base64urlToBytes,
  bytesToHex,
  _format,
} from "../../capture-page/js/crypto.js";

let passed = 0;
function ok() {
  passed += 1;
}

function b64url(bytes) {
  return Buffer.from(bytes)
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function testEncryptDecryptRoundTrip() {
  const keyBytes = crypto.getRandomValues(new Uint8Array(32));
  const key = await importContentKey(b64url(keyBytes));
  const plaintext = new Uint8Array(1000);
  for (let i = 0; i < plaintext.length; i++) plaintext[i] = (i * 7) & 0xff;

  const { blob, plaintextLen, sha256 } = await encryptWav(key, plaintext);

  // Format: IV(12) ‖ ciphertext ‖ 16-byte GCM tag.
  assert.equal(plaintextLen, plaintext.length);
  assert.equal(blob.length, _format.IV_BYTES + plaintext.length + 16, "blob layout");

  // Decrypt independently with the same raw key.
  const dkey = await crypto.subtle.importKey(
    "raw",
    keyBytes,
    { name: "AES-GCM" },
    false,
    ["decrypt"],
  );
  const iv = blob.slice(0, _format.IV_BYTES);
  const ct = blob.slice(_format.IV_BYTES);
  const dec = new Uint8Array(await crypto.subtle.decrypt({ name: "AES-GCM", iv }, dkey, ct));
  assert.deepEqual([...dec], [...plaintext], "decrypt recovers plaintext");
  ok();
}

async function testIntegrityIsOverPlaintext() {
  const keyBytes = crypto.getRandomValues(new Uint8Array(32));
  const key = await importContentKey(b64url(keyBytes));
  const plaintext = new TextEncoder().encode("RIFF....fake wav payload");
  const { sha256 } = await encryptWav(key, plaintext);

  const nodeSha = createHash("sha256").update(Buffer.from(plaintext)).digest("hex");
  assert.equal(sha256, nodeSha, "sha256 is over plaintext, matches node crypto");
  assert.equal(sha256, await sha256Hex(plaintext), "module sha256Hex matches");
  ok();
}

async function testDistinctIvsPerEncrypt() {
  const keyBytes = crypto.getRandomValues(new Uint8Array(32));
  const key = await importContentKey(b64url(keyBytes));
  const pt = new Uint8Array([1, 2, 3, 4]);
  const a = await encryptWav(key, pt);
  const b = await encryptWav(key, pt);
  // Random IV per call -> different ciphertext for identical plaintext.
  assert.notDeepEqual([...a.blob], [...b.blob], "random IV per encryption");
  ok();
}

async function testKeyMustBe32Bytes() {
  await assert.rejects(() => importContentKey(b64url(new Uint8Array(16))), /32 bytes/);
  ok();
}

function testBase64urlRoundTrip() {
  const bytes = new Uint8Array([0, 1, 2, 250, 251, 255, 16, 64]);
  assert.deepEqual([...base64urlToBytes(b64url(bytes))], [...bytes]);
  assert.equal(bytesToHex(new Uint8Array([0, 255, 16])), "00ff10");
  ok();
}

const tests = [
  testEncryptDecryptRoundTrip,
  testIntegrityIsOverPlaintext,
  testDistinctIvsPerEncrypt,
  testKeyMustBe32Bytes,
  testBase64urlRoundTrip,
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
